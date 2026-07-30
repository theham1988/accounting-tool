"""Store-unit tests for the ``loyverse_exports`` paper trail (issue #102).

Slice 2 of the Loyverse cost-mirror (parent spec #100). Slice 1 (#101)
shipped the round-trip CSV and confirm route with no audit trail; this
slice adds the dedicated ``loyverse_exports`` table and the two store
methods that read/write it. Slice 3 (issue #103) adds the drift badge's
backing read :meth:`SqliteConfigStore.cost_edits_since`.

These tests pin the store seam — :meth:`SqliteConfigStore.record_loyverse_export`
and :meth:`SqliteConfigStore.loyverse_exports` — through the public store
interface. The E2E seam (the confirm route hanging the write off) lives in
``tests/test_loyverse_cost_export_e2e.py``; the dedicated-vs-audit-log
decision (issue #70 resolution / spec #100) is what keeps these writes
*off* ``audit_log`` and the 9am "N changes since last review" count.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tangerine.storage.config_store import SqliteConfigStore, seed_config


def _seeded_store(
    tmp_path: Path, *, now: str = "2026-07-29T03:00:00+00:00"
) -> SqliteConfigStore:
    """An empty-but-migrated config store, clock pinned to ``now``."""
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    recipes.write_text("recipes: []\n", encoding="utf-8")
    costs.write_text("costs: {}\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes, costs_path=costs)
    return SqliteConfigStore(conn, now=lambda: now)


def _seeded_store_with_cost(
    tmp_path: Path, *, now: str = "2026-07-29T03:00:00+00:00"
) -> SqliteConfigStore:
    """A migrated store with one seeded ``costs`` row for ``butter``.

    Used by the drift-badge read (issue #103): the badge counts cost *edits*
    since the last export, so the test needs a cost row to re-save against.
    Seeding writes the row directly (not via :meth:`save_cost`), so the seed
    itself does not land in ``audit_log`` — exactly the production shape.
    """
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    recipes.write_text("recipes: []\n", encoding="utf-8")
    costs.write_text("costs:\n  butter: { price: '0.004', updated_at: '2026-06-01' }\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes, costs_path=costs)
    return SqliteConfigStore(conn, now=lambda: now)


# =============================================================================
# AC: record_loyverse_export writes one row, returns its id, holds the lock
# =============================================================================


def test_record_loyverse_export_writes_one_row_and_returns_its_id(
    tmp_path: Path,
) -> None:
    """A record call writes exactly one ``loyverse_exports`` row whose fields
    match the arguments, and returns the new row's id.

    ``confirmed_at`` is **not** a caller argument — the store stamps it from
    its injectable clock (the same pattern ``cash_spend`` and every other
    audited write uses, so tests pin the timestamp via ``now=``)."""
    store = _seeded_store(tmp_path, now="2026-07-29T03:00:00+00:00")
    drift = json.dumps(
        [{"sku": "latte-12oz", "name": "Latte", "loyverse_cost": "0.99",
          "books_cost": "0.20"}]
    )

    row_id = store.record_loyverse_export(
        partner_id="daniel",
        item_count=4,
        changed_count=1,
        drift_payload=drift,
    )
    exports = store.loyverse_exports()

    assert isinstance(row_id, int)
    assert row_id > 0
    (export,) = exports
    assert export.id == row_id
    assert export.partner_id == "daniel"
    assert export.confirmed_at == "2026-07-29T03:00:00+00:00"  # from the store clock
    assert export.item_count == 4
    assert export.changed_count == 1
    assert export.drift_payload == drift


def test_loyverse_exports_returns_rows_newest_first(tmp_path: Path) -> None:
    """``loyverse_exports()`` returns rows newest-first (highest id first) —
    the read order the drift badge (slice 3) and any future history surface
    want."""
    store = _seeded_store(tmp_path, now="2026-07-29T03:00:00+00:00")

    first = store.record_loyverse_export(
        partner_id="daniel",
        item_count=3,
        changed_count=0,
        drift_payload="[]",
    )
    second = store.record_loyverse_export(
        partner_id="noi",
        item_count=5,
        changed_count=2,
        drift_payload="[]",
    )

    exports = store.loyverse_exports()
    assert [e.id for e in exports] == [second, first]
    assert exports[0].partner_id == "noi"


def test_loyverse_exports_empty_when_no_confirm_has_happened(
    tmp_path: Path,
) -> None:
    """Before any confirm, ``loyverse_exports()`` is empty — the null state
    slice 3's badge hides behind (no "stale since forever" message)."""
    store = _seeded_store(tmp_path)
    assert store.loyverse_exports() == []


# =============================================================================
# AC: the 9am "N changes since last review" count is unaffected — these
# writes go to the dedicated table, NOT through _record_audit / audit_log
# =============================================================================


def test_record_loyverse_export_does_not_touch_audit_log(tmp_path: Path) -> None:
    """The dedicated-vs-audit-log decision (issue #70 resolution / spec #100):
    a Loyverse-bound export is not an in-Books config edit, so it must not
    land in ``audit_log`` (which feeds ``unreviewed_changes`` and the 9am
    "N config changes since last review" count). The write goes to the
    dedicated ``loyverse_exports`` table only."""
    store = _seeded_store(tmp_path)

    store.record_loyverse_export(
        partner_id="daniel",
        item_count=2,
        changed_count=1,
        drift_payload="[]",
    )

    assert store.audit_entries() == []
    assert store.unreviewed_changes("daniel") == []


# =============================================================================
# Slice 3 (issue #103): cost_edits_since — the drift badge's backing read
# =============================================================================
#
# The badge asks "how many cost edits have happened in Books since the last
# Loyverse export?" and renders the answer. The store backs that with a single
# count query over ``audit_log`` filtered to ``table_name = 'costs'`` and
# ``changed_at > <most-recent loyverse_exports.confirmed_at>``. This section
# pins that read at the store seam; the badge's rendering lives in the E2E
# test (``tests/test_loyverse_cost_export_e2e.py``).
#
# Three rules the read holds (mirroring the badge's AC):
#
# - Only ``costs`` edits count. A recipe edit (``table_name = 'recipes'``) does
#   not — the mirrored number comes from ``CostResolver`` over the cost book,
#   not the recipe, so a recipe change between two exports does not inflate
#   the badge.
# - The comparison is strict (``changed_at > since``): an edit stamped at the
#   same instant as the confirm is not "since" it. The store's injectable clock
#   pins both timestamps in tests.
# - Zero cost edits returns 0 (never None, never negative).


def test_cost_edits_since_counts_cost_edits_after_the_timestamp(
    tmp_path: Path,
) -> None:
    """``cost_edits_since(since)`` returns the number of ``audit_log`` rows
    with ``table_name = 'costs'`` and ``changed_at > since``.

    Worked example: one export at 03:00 UTC (the badge's "as-of" timestamp),
    then a butter reprice at 09:00 UTC. The read against 03:00 UTC returns 1.
    """
    store = _seeded_store_with_cost(tmp_path, now="2026-07-29T03:00:00+00:00")
    last_export = "2026-07-29T03:00:00+00:00"

    # Advance the clock and re-save the butter cost — one ``costs`` audit row,
    # stamped 09:00 UTC (after the 03:00 export).
    store._now = lambda: "2026-07-29T09:00:00+00:00"  # type: ignore[method-assign]
    store.save_cost(
        "butter",
        pack_price=Decimal("10"),
        pack_quantity=Decimal("2500"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 29),
    )

    assert store.cost_edits_since(last_export) == 1


def test_cost_edits_since_counts_multiple_cost_edits_after_the_timestamp(
    tmp_path: Path,
) -> None:
    """Two cost edits after the export each count — the badge reads the
    partner's edit volume, not a boolean "anything changed"."""
    store = _seeded_store_with_cost(tmp_path, now="2026-07-29T03:00:00+00:00")
    last_export = "2026-07-29T03:00:00+00:00"

    store._now = lambda: "2026-07-29T09:00:00+00:00"  # type: ignore[method-assign]
    store.save_cost(
        "butter",
        pack_price=Decimal("10"),
        pack_quantity=Decimal("2500"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 29),
    )
    store._now = lambda: "2026-07-29T10:00:00+00:00"  # type: ignore[method-assign]
    store.save_cost(
        "butter",
        pack_price=Decimal("11"),
        pack_quantity=Decimal("2500"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 29),
    )

    assert store.cost_edits_since(last_export) == 2


def test_cost_edits_since_excludes_edits_at_or_before_the_timestamp(
    tmp_path: Path,
) -> None:
    """The comparison is strict (``changed_at > since``): an edit stamped at
    exactly the export's timestamp is not "since" it, and an edit before it
    obviously is not either.

    This is what makes the badge read zero immediately after a confirm: the
    confirm writes the ``loyverse_exports`` row at the store's clock, and the
    next badge read compares ``>`` against that same moment — so even an edit
    in the same transaction would not count (it does not happen, but the
    strict-``>`` keeps the semantics honest)."""
    store = _seeded_store_with_cost(tmp_path, now="2026-07-29T03:00:00+00:00")

    # An edit stamped at 03:00 (the export moment) and one before it (02:00).
    store._now = lambda: "2026-07-29T02:00:00+00:00"  # type: ignore[method-assign]
    store.save_cost(
        "butter",
        pack_price=Decimal("8"),
        pack_quantity=Decimal("2500"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 28),
    )
    store._now = lambda: "2026-07-29T03:00:00+00:00"  # type: ignore[method-assign]
    store.save_cost(
        "butter",
        pack_price=Decimal("9"),
        pack_quantity=Decimal("2500"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 29),
    )

    assert store.cost_edits_since("2026-07-29T03:00:00+00:00") == 0


def test_cost_edits_since_ignores_recipe_edits(tmp_path: Path) -> None:
    """Only ``costs`` audit rows count. A recipe edit
    (``table_name = 'recipes'``) does not inflate the count — the mirrored
    cost comes from ``CostResolver`` over the cost book, so a recipe change
    between two exports does not move the number Books would write to
    Loyverse."""
    store = _seeded_store_with_cost(tmp_path, now="2026-07-29T03:00:00+00:00")
    last_export = "2026-07-29T03:00:00+00:00"

    store._now = lambda: "2026-07-29T09:00:00+00:00"  # type: ignore[method-assign]
    # A recipe save against a SKU that has no recipe yet — writes a
    # ``recipes`` audit row, not a ``costs`` one.
    store.save_recipe(
        "croissant",
        ingredients=[("butter", Decimal("50"))],
        yield_qty=Decimal("1"),
        yield_estimated=True,
        updated_by="daniel",
    )

    assert store.cost_edits_since(last_export) == 0


def test_cost_edits_since_is_zero_when_nothing_changed(tmp_path: Path) -> None:
    """Zero cost edits after the timestamp returns 0 (never None). The badge
    renders "0 item costs changed" against this, not a blank or a negative."""
    store = _seeded_store_with_cost(tmp_path, now="2026-07-29T03:00:00+00:00")

    assert store.cost_edits_since("2026-07-29T03:00:00+00:00") == 0


def test_cost_edits_since_counts_a_brand_new_cost_row(tmp_path: Path) -> None:
    """A cost creation (no prior row) counts the same as a reprice. The filter
    is on ``table_name = 'costs'`` — a partner adding a brand-new SKU's cost
    is a cost edit the mirror should reflect on its next export, so the
    ``old=None → new={...}`` creation row counts just like a reprice does."""
    store = _seeded_store_with_cost(tmp_path, now="2026-07-29T03:00:00+00:00")
    last_export = "2026-07-29T03:00:00+00:00"

    store._now = lambda: "2026-07-29T09:00:00+00:00"  # type: ignore[method-assign]
    # A brand-new cost row (no prior) — ``old=None, new={...}``, still
    # ``table_name='costs'``.
    store.save_cost(
        "flour",
        pack_price=Decimal("40"),
        pack_quantity=Decimal("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 29),
    )

    assert store.cost_edits_since(last_export) == 1
