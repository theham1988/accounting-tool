"""E2E: price-as-of-date lookup + daily review on as-of pricing (Wave 2 slice 1).

ADR-0004 decision 2: every reporting surface costs sales at the net price in
effect on the sale's date, reconstructed from the audit log (each cost edit
snapshots the row's old/new ``price_per_unit_net`` + ``changed_at``);
pre-cutover sales use the seed price. Issue #28 is the foundation slice: the
lookup itself, plus the daily review moved onto it so a cost edit no longer
re-states history.

Each test is a worked example over the public interfaces (``PriceHistory``,
``SqliteConfigStore``, ``build_daily_review``) — no reaching into internals.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from tangerine.cost import CostBook
from tangerine.daily_review import build_daily_review
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.store import InMemoryLoyverseStore, SaleRecord
from tangerine.price_history import PriceChange, PriceHistory
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.types import Sale


def _seeded_store(
    tmp_path: Path, costs_yaml: str, clock: dict[str, str]
) -> SqliteConfigStore:
    """An in-memory config store seeded from a costs YAML (no recipes).

    ``clock["now"]`` is the injectable audit timestamp — mutate it between
    saves to simulate edits on different days.
    """
    recipes_path = tmp_path / "recipes.yaml"
    recipes_path.write_text("recipes: []\nmappings: []\n", encoding="utf-8")
    costs_path = tmp_path / "costs.yaml"
    costs_path.write_text(costs_yaml, encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes_path, costs_path=costs_path)
    return SqliteConfigStore(conn, now=lambda: clock["now"])


# --- price_as_of: the lookup itself ------------------------------------------


def test_never_edited_sku_resolves_to_its_current_price_on_any_date() -> None:
    """A SKU with no cost-edit history costs at its seed/current price.

    Pre-cutover there is no audit history at all, so every SKU takes this
    path: the price the cost book holds today *is* the price on any past
    date (issue #28: "falling back to the seed/current price when the SKU
    has no cost-edit history").
    """
    history = PriceHistory(
        current=CostBook({"butter": (D("0.50"), date(2026, 6, 1))}),
        changes=[],
    )

    assert history.price_as_of("butter", date(2026, 7, 3)) == D("0.50")
    assert history.price_as_of("butter", date(2025, 1, 1)) == D("0.50")


def test_edited_once_seed_price_before_the_edit_new_price_from_its_day_on() -> None:
    """Issue #28's worked example: butter repriced on the 15th.

    The 3rd (before the edit) still costs at the seed price; the 15th and
    after cost at the new price. The edit's own day takes the new price —
    the partner repriced that morning, so that day's sales carry it.
    """
    history = PriceHistory(
        current=CostBook({"butter": (D("0.60"), date(2026, 7, 15))}),
        changes=[
            PriceChange(
                sku_id="butter",
                changed_on=date(2026, 7, 15),
                old_price=D("0.50"),
                new_price=D("0.60"),
            ),
        ],
    )

    assert history.price_as_of("butter", date(2026, 7, 3)) == D("0.50")
    assert history.price_as_of("butter", date(2026, 7, 15)) == D("0.60")
    assert history.price_as_of("butter", date(2026, 7, 20)) == D("0.60")


def test_edited_more_than_once_dates_between_edits_take_the_price_then_in_effect() -> None:
    """Issue #28 AC: a SKU edited twice resolves correctly between the edits.

    Milk repriced on 10 Jul and again on 20 Jul: the 5th costs at the
    seed, the 14th at the first edit's price, the 25th at the second's.
    """
    history = PriceHistory(
        current=CostBook({"milk": (D("0.030"), date(2026, 7, 20))}),
        changes=[
            PriceChange(
                sku_id="milk",
                changed_on=date(2026, 7, 10),
                old_price=D("0.020"),
                new_price=D("0.025"),
            ),
            PriceChange(
                sku_id="milk",
                changed_on=date(2026, 7, 20),
                old_price=D("0.025"),
                new_price=D("0.030"),
            ),
        ],
    )

    assert history.price_as_of("milk", date(2026, 7, 5)) == D("0.020")
    assert history.price_as_of("milk", date(2026, 7, 14)) == D("0.025")
    assert history.price_as_of("milk", date(2026, 7, 25)) == D("0.030")


def test_sku_created_by_an_edit_has_no_price_before_its_creation() -> None:
    """A cost row born from an edit (no seed) had no price before that day.

    ``price_as_of`` answers ``None`` rather than inventing a price — the
    margin engine already knows how to flag unknown-price rows honestly.
    """
    history = PriceHistory(
        current=CostBook({"oat-milk": (D("0.045"), date(2026, 7, 12))}),
        changes=[
            PriceChange(
                sku_id="oat-milk",
                changed_on=date(2026, 7, 12),
                old_price=None,
                new_price=D("0.045"),
            ),
        ],
    )

    assert history.price_as_of("oat-milk", date(2026, 7, 5)) is None
    assert history.price_as_of("oat-milk", date(2026, 7, 12)) == D("0.045")


def test_unknown_sku_has_no_price_on_any_date() -> None:
    history = PriceHistory(current=CostBook(), changes=[])

    assert history.price_as_of("never-heard-of-it", date(2026, 7, 1)) is None


# --- cost_book_as_of: the margin engine's view --------------------------------


def test_cost_book_as_of_is_a_cost_book_frozen_at_that_date() -> None:
    """The margin engine consumes a ``CostBook``; ``cost_book_as_of`` builds
    one whose every price is the as-of-date answer.

    Butter was repriced on the 15th; oat milk did not exist until the 12th.
    The book as of the 3rd holds butter's seed price and no oat milk at all
    (so a recipe using it is flagged unknown-price, not silently zero-costed).
    """
    history = PriceHistory(
        current=CostBook(
            {
                "butter": (D("0.60"), date(2026, 7, 15)),
                "oat-milk": (D("0.045"), date(2026, 7, 12)),
                "beans": (D("0.80"), date(2026, 6, 1)),
            }
        ),
        changes=[
            PriceChange(
                sku_id="butter",
                changed_on=date(2026, 7, 15),
                old_price=D("0.50"),
                new_price=D("0.60"),
            ),
            PriceChange(
                sku_id="oat-milk",
                changed_on=date(2026, 7, 12),
                old_price=None,
                new_price=D("0.045"),
            ),
        ],
    )

    book = history.cost_book_as_of(date(2026, 7, 3))

    butter = book.price("butter")
    assert butter is not None and butter.price == D("0.50")
    assert book.price("oat-milk") is None
    beans = book.price("beans")
    assert beans is not None and beans.price == D("0.80")


# --- the audit log is the history: SqliteConfigStore.price_history -----------


def test_price_history_reconstructed_from_real_cost_edits(tmp_path: Path) -> None:
    """Cost edits saved through the store become the price history.

    Worked example over the real audit log. Butter is seeded at a net 0.50
    THB/g; on 15 Jul the partner saves a new pack price deriving 0.60. The
    store's ``price_history()`` answers the seed price for the 3rd and the
    new price for the 15th onward — no new table, just the audit trail
    Slice 5 already writes.
    """
    clock = {"now": "2026-07-15T02:00:00+00:00"}
    store = _seeded_store(
        tmp_path,
        """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
""",
        clock,
    )

    store.save_cost(
        "butter",
        pack_price=D("600"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 15),
    )

    history = store.price_history()
    assert history.price_as_of("butter", date(2026, 7, 3)) == D("0.50")
    assert history.price_as_of("butter", date(2026, 7, 15)) == D("0.600000")
    assert history.price_as_of("butter", date(2026, 7, 20)) == D("0.600000")


def test_early_morning_edit_takes_effect_on_the_partners_day_not_the_utc_day(
    tmp_path: Path,
) -> None:
    """An edit before ~7am local lands on the partner's calendar day.

    The audit clock is UTC but the venue runs at UTC+7: a repricing at
    01:30 local on the 16th is stamped ``changed_at`` 18:30 UTC *on the
    15th*. The save also records the partner-facing effective date
    (``updated_on`` = the app's local today, the 16th) in the cost row,
    and that date must govern — otherwise the edit would silently re-cost
    the 15th, a day already reviewed (issue #28: editing a cost never
    changes a previously-viewable day's numbers).
    """
    clock = {"now": "2026-07-15T18:30:00+00:00"}  # 16 Jul, 01:30 at UTC+7
    store = _seeded_store(
        tmp_path,
        """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
""",
        clock,
    )

    store.save_cost(
        "butter",
        pack_price=D("600"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 16),
    )

    history = store.price_history()
    assert history.price_as_of("butter", date(2026, 7, 15)) == D("0.50")
    assert history.price_as_of("butter", date(2026, 7, 16)) == D("0.600000")


def test_price_history_ignores_non_cost_audit_entries(tmp_path: Path) -> None:
    """Recipe/mapping/SKU edits share the audit log but are not price changes."""
    clock = {"now": "2026-07-10T02:00:00+00:00"}
    store = _seeded_store(
        tmp_path,
        """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
""",
        clock,
    )

    store.create_sku("oat-drink", name="Oat drink", unit="ml", created_by="noi")

    history = store.price_history()
    assert history.price_as_of("butter", date(2026, 7, 20)) == D("0.50")


# --- the daily review on as-of-date pricing -----------------------------------


def _sale(item_id: str, day: date, price: str, line: str) -> SaleRecord:
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=day, sell_price=D(price)),
        receipt_number=f"r-{day.isoformat()}",
        line_id=line,
    )


def test_editing_a_cost_does_not_change_an_earlier_days_review(
    tmp_path: Path,
) -> None:
    """Issue #28's end-to-end behavior, through the real stores.

    A croissant (recipe: 10 g butter) sells on 3 Jul and on 16 Jul. On
    15 Jul the partner repricing butter doubles its net cost. Viewed on the
    16th, the 3 Jul review still shows the 3 Jul margin — bit-for-bit what
    it showed before the edit — while the 16 Jul review carries the new
    butter price. Previously both days would have been costed at the
    render-time price (the latent Wave 1 bug ADR-0004 decision 2 fixes).
    """
    clock = {"now": "2026-07-15T02:00:00+00:00"}
    recipes_path = tmp_path / "recipes.yaml"
    recipes_path.write_text(
        """
recipes:
  - sku_id: croissant
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "10" }

mappings:
  - { item_id: i-croissant, sku_id: croissant }
""",
        encoding="utf-8",
    )
    costs_path = tmp_path / "costs.yaml"
    costs_path.write_text(
        """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
""",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes_path, costs_path=costs_path)
    config_store = SqliteConfigStore(conn, now=lambda: clock["now"])

    loyverse_store = InMemoryLoyverseStore()
    loyverse_store.record_sales(
        [
            _sale("i-croissant", date(2026, 7, 3), "80", "l-1"),
            _sale("i-croissant", date(2026, 7, 16), "80", "l-2"),
        ]
    )
    source = StoreSource(store=loyverse_store, config=config_store)

    before_edit = build_daily_review(source=source, review_date=date(2026, 7, 3))
    assert before_edit.cogs == D("5")  # 10 g x 0.50 THB/g

    # 15 Jul: butter now 1000 THB per kg -> net 1.00 THB/g.
    config_store.save_cost(
        "butter",
        pack_price=D("1000"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 15),
    )

    after_edit = build_daily_review(source=source, review_date=date(2026, 7, 3))
    assert after_edit.cogs == D("5")
    assert after_edit.gross_margin == before_edit.gross_margin
    assert after_edit.daily.item_margins == before_edit.daily.item_margins

    new_day = build_daily_review(source=source, review_date=date(2026, 7, 16))
    assert new_day.cogs == D("10")  # 10 g x 1.00 THB/g
