"""End-to-end CLI + Source-adapter seam (Wave 1, Slice 1).

Exercises the whole Wave-1-Slice-1 stack: SQLite store + config-loaded recipes
and costs + the existing ``StoreSource`` adapter + the engine's
``build_daily_review``, against a file-backed database (the real persistence
shape) and real config files (the real loader boundary).

The genuine external boundaries here are the filesystem (a tempfile DB, real
YAML files) — no internal module is mocked. The engine functions are exercised
for real, exactly as the PRD testing rules require.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tangerine.config.loader import load_costs, load_recipes
from tangerine.daily_review import build_daily_review
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.store import MenuSnapshot, MenuItem, SaleRecord
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Money, Sale, Segment

D = Decimal


def _seeded_recipes_yaml() -> str:
    """The Wave 1 default recipes (mirrors ``__main__._seeded_source``)."""
    return """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }
"""


def _seeded_costs_yaml() -> str:
    return """
costs:
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }
  beans-arabica: { price: "2", updated_at: "2026-06-01" }
  milk-fresh: { price: "0.025", updated_at: "2026-06-01" }
"""


def _sale_record(
    *, receipt_number: str, item_id: str, day: date, price: str
) -> SaleRecord:
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=day, sell_price=Money(price)),
        receipt_number=receipt_number,
        line_id="li-1",
    )


# --- AC: Source adapter wraps store + config + cost, satisfies the Protocol ---


def test_source_adapter_builds_review_against_sqlite_and_config(
    tmp_path: Path,
) -> None:
    """``StoreSource(SqliteLoyverseStore, recipes, cost)`` feeds the engine and
    produces the same numbers an inline source would.

    Worked example. One Chang @ 120 (cost 35) and one latte @ 120 (cost 45) on
    2026-06-24, recorded into a SQLite file. Recipes and costs loaded from
    YAML. ``build_daily_review`` returns revenue 240, COGS 80, GM 160.
    """
    db_path = str(tmp_path / "tangerine.db")
    recipes_yaml = tmp_path / "recipes.yaml"
    costs_yaml = tmp_path / "costs.yaml"
    recipes_yaml.write_text(_seeded_recipes_yaml(), encoding="utf-8")
    costs_yaml.write_text(_seeded_costs_yaml(), encoding="utf-8")

    store = SqliteLoyverseStore.connect(db_path)
    store.record_sales(
        [
            _sale_record(
                receipt_number="2-1",
                item_id="chang-draft-500",
                day=date(2026, 6, 24),
                price="120",
            ),
            _sale_record(
                receipt_number="2-2",
                item_id="espresso-latte",
                day=date(2026, 6, 24),
                price="120",
            ),
        ]
    )
    catalog = load_recipes(recipes_yaml)
    book = load_costs(costs_yaml)
    source = StoreSource(store=store, recipes=list(catalog.all()), cost=book)

    review = build_daily_review(source=source, review_date=date(2026, 6, 24))

    assert review.revenue == D("240")
    assert review.cogs == D("80")  # 35 + 45
    assert review.gross_margin == D("160")
    store.close()


# --- AC: data persists across a "process restart" ----------------------------


def test_review_is_identical_after_killing_and_reopening_the_store(
    tmp_path: Path,
) -> None:
    """A review built before a restart equals the review built after.

    Worked example. Sales are written and a review is built (revenue 240, GM
    160). The store is closed — simulating a process kill. A fresh store is
    opened against the same file, a fresh source is built, and the review is
    rebuilt. The numbers must match exactly.
    """
    db_path = str(tmp_path / "tangerine.db")
    recipes_yaml = tmp_path / "recipes.yaml"
    costs_yaml = tmp_path / "costs.yaml"
    recipes_yaml.write_text(_seeded_recipes_yaml(), encoding="utf-8")
    costs_yaml.write_text(_seeded_costs_yaml(), encoding="utf-8")

    # First "process": write sales, build review.
    first_store = SqliteLoyverseStore.connect(db_path)
    first_store.record_sales(
        [
            _sale_record(
                receipt_number="2-1",
                item_id="chang-draft-500",
                day=date(2026, 6, 24),
                price="120",
            ),
            _sale_record(
                receipt_number="2-2",
                item_id="espresso-latte",
                day=date(2026, 6, 24),
                price="120",
            ),
        ]
    )
    catalog = load_recipes(recipes_yaml)
    book = load_costs(costs_yaml)
    before = build_daily_review(
        source=StoreSource(
            store=first_store, recipes=list(catalog.all()), cost=book
        ),
        review_date=date(2026, 6, 24),
    )
    first_store.close()

    # Second "process": reopen the same file, rebuild the review.
    second_store = SqliteLoyverseStore.connect(db_path)
    after = build_daily_review(
        source=StoreSource(
            store=second_store, recipes=list(catalog.all()), cost=book
        ),
        review_date=date(2026, 6, 24),
    )

    assert after.revenue == before.revenue == D("240")
    assert after.cogs == before.cogs == D("80")
    assert after.gross_margin == before.gross_margin == D("160")
    second_store.close()


# --- AC: the CLI prints the daily review against SQLite data -----------------


def _run_cli(*, db_path: str, recipes_path: Path, costs_path: Path) -> str:
    """Invoke ``tangerine.__main__.main`` in-process, capturing stdout.

    The CLI's ``main`` reads config + DB paths as arguments so the test does
    not need a subprocess or env-mutation. The real ``__main__`` wrapper (run
    by ``python -m tangerine``) supplies defaults from env vars and delegates
    here.
    """
    import io
    from contextlib import redirect_stdout

    from tangerine.__main__ import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(
            db_path=db_path,
            recipes_path=str(recipes_path),
            costs_path=str(costs_path),
        )
    return buf.getvalue()


def test_cli_against_empty_db_prints_empty_review(
    tmp_path: Path,
) -> None:
    """``python -m tangerine`` against a fresh (empty) DB exits cleanly with an
    empty review (no sales -> zero revenue, zero COGS, zero GM).

    Per the agreed scope: sales arrive via Slice 3 sync, which does not exist
    yet. The CLI must not crash on an empty DB; it surfaces a review with
    zeros so the partner sees the tool is wired up and waiting for data.
    """
    db_path = str(tmp_path / "tangerine.db")
    recipes_yaml = tmp_path / "recipes.yaml"
    costs_yaml = tmp_path / "costs.yaml"
    recipes_yaml.write_text(_seeded_recipes_yaml(), encoding="utf-8")
    costs_yaml.write_text(_seeded_costs_yaml(), encoding="utf-8")

    output = _run_cli(
        db_path=db_path, recipes_path=recipes_yaml, costs_path=costs_yaml
    )

    # The review prints headline numbers; on an empty DB they are all zero.
    assert "Daily 9am review" in output
    assert "revenue:" in output.lower() or "revenue" in output.lower()
    # Exit cleanly is implied: if main() raised, _run_cli would propagate it.


def test_cli_with_persisted_sales_prints_seeded_numbers(
    tmp_path: Path,
) -> None:
    """With the seeded 7-day sales persisted, ``python -m tangerine`` prints the
    same headline numbers the seeded CLI used to.

    The seeded source (``__main__._seeded_source`` in the pre-Slice-1 code)
    sold one Chang + one latte per day for 7 days. Per-day: revenue 240, COGS
    80 (35 + 45), gross margin 160. The CLI now reads those sales back from
    SQLite; the printed review must carry those same numbers.
    """
    db_path = str(tmp_path / "tangerine.db")
    recipes_yaml = tmp_path / "recipes.yaml"
    costs_yaml = tmp_path / "costs.yaml"
    recipes_yaml.write_text(_seeded_recipes_yaml(), encoding="utf-8")
    costs_yaml.write_text(_seeded_costs_yaml(), encoding="utf-8")

    # Seed 7 days of sales (the seeded-source shape) into SQLite directly,
    # mirroring what Slice 3's sync will write.
    store = SqliteLoyverseStore.connect(db_path)
    latest_day = date(2026, 6, 24)
    for offset in range(7):
        day = latest_day - timedelta(days=offset)
        store.record_sales(
            [
                _sale_record(
                    receipt_number=f"2-chang-{offset}",
                    item_id="chang-draft-500",
                    day=day,
                    price="120",
                ),
                _sale_record(
                    receipt_number=f"2-latte-{offset}",
                    item_id="espresso-latte",
                    day=day,
                    price="120",
                ),
            ]
        )
    store.close()

    output = _run_cli(
        db_path=db_path, recipes_path=recipes_yaml, costs_path=costs_yaml
    )

    # Headline numbers match the seeded math: revenue 240, COGS 80, GM 160.
    assert "Daily 9am review for 2026-06-24" in output
    assert "revenue:       240.00 THB" in output
    assert "COGS: 80.00 THB" in output
    assert "gross margin: 160.00 THB" in output


def test_cli_persists_across_invocations(tmp_path: Path) -> None:
    """Running the CLI twice against the same DB reads the same sales back.

    This is the restart half of the persistence AC at the CLI level: the first
    invocation must not consume or mutate the data the second invocation reads.
    """
    db_path = str(tmp_path / "tangerine.db")
    recipes_yaml = tmp_path / "recipes.yaml"
    costs_yaml = tmp_path / "costs.yaml"
    recipes_yaml.write_text(_seeded_recipes_yaml(), encoding="utf-8")
    costs_yaml.write_text(_seeded_costs_yaml(), encoding="utf-8")

    store = SqliteLoyverseStore.connect(db_path)
    store.record_sales(
        [_sale_record(
            receipt_number="2-1",
            item_id="chang-draft-500",
            day=date(2026, 6, 24),
            price="120",
        )]
    )
    store.close()

    first = _run_cli(
        db_path=db_path, recipes_path=recipes_yaml, costs_path=costs_yaml
    )
    second = _run_cli(
        db_path=db_path, recipes_path=recipes_yaml, costs_path=costs_yaml
    )

    assert first == second
    assert "revenue:       120.00 THB" in first


# --- AC: default shipped config files ----------------------------------------


def test_shipped_default_config_loads_cleanly() -> None:
    """The shipped ``config/recipes.yaml`` and ``config/costs.yaml`` load
    cleanly through the production loaders.

    This is the smoke test for the operator-editable defaults that ship in
    the repo. Originally it pinned the pre-Slice-1 seeded fixtures
    (``chang-draft-500`` / ``espresso-latte`` / ``beans-arabica``); commit
    3257cfa replaced those with the real partner-authored recipe book and
    cost workbook, so the assertion now checks the *shape* of the shipped
    config (it loads, it has many recipes, it carries mappings, at least
    one recipe is fully costable) rather than specific named SKUs.

    The shipped book is cafe/kitchen-only (104 cafe recipes, 0 bar), and
    that is the steady state, not a placeholder: bar sales are draught beer,
    wine, RTD cocktails, and soft drinks — items with no recipe to cost,
    just a sell price. They are intentionally unmapped and surface in the
    daily review's "unmapped" section, which is their correct home
    (CONTEXT.md "Regular item"; recipes.yaml header comment). There is no
    "when bar recipes land" milestone to wait for.

    Per ``CONTEXT.md`` "Recipe review", the recipe/cost files go through PR
    review and the partner-authoring is the source of truth — this test
    guards against a malformed file shipping, not against the recipes
    themselves changing.
    """
    repo_root = Path(__file__).resolve().parent.parent
    recipes_path = repo_root / "config" / "recipes.yaml"
    costs_path = repo_root / "config" / "costs.yaml"

    catalog = load_recipes(recipes_path)
    book = load_costs(costs_path)

    recipes = catalog.all()
    assert len(recipes) >= 50  # the partner-authored book is large
    segments = {r.segment for r in recipes}
    assert Segment.CAFE in segments
    # Bar is not "missing recipes" — draught beer, wine, RTD cocktails, soft
    # drinks are sold as-is with no recipe to cost, so they correctly stay
    # unmapped and surface in the daily review's "unmapped" section
    # (recipes.yaml header; CONTEXT.md "Regular item"). Steady state, not a
    # TODO; do not add a ``Segment.BAR in segments`` assertion here.
    assert catalog.mappings(), "shipped recipes.yaml must carry mappings"

    # At least one mapped ingredient SKU is priced, so the daily review has
    # a real margin to show on first run. Exact SKUs drift as partners edit
    # costs; just assert *some* price resolves.
    priced = [
        r for r in recipes
        if all(book.price(ing.sku_id) is not None for ing in r.ingredients)
    ]
    assert priced, "no recipe is fully costable — every item would flag unpriced"
