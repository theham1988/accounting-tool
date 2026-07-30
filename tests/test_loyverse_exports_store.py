"""Store-unit tests for the ``loyverse_exports`` paper trail (issue #102).

Slice 2 of the Loyverse cost-mirror (parent spec #100). Slice 1 (#101)
shipped the round-trip CSV and confirm route with no audit trail; this
slice adds the dedicated ``loyverse_exports`` table and the two store
methods that read/write it.

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
