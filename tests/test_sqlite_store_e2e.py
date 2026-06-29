"""End-to-end persistence seam (Wave 1, Slice 1).

The SQLite-backed ``LoyverseStore`` must satisfy the same contract the in-memory
``InMemoryLoyverseStore`` satisfies: sales round-trip, replay is idempotent,
menu snapshots diff into a timestamped change history, and the data survives a
process restart.

Per the PRD testing rules the only genuine boundary here is the SQLite
connection itself: tests use ``:memory:`` for in-process behaviour and a real
temp file for the restart behaviour. No internal module is mocked.

These tests read as worked examples: a synthetic ``SaleRecord`` goes in; the
same ``Sale`` comes back out.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tangerine.loyverse.store import (
    MenuChange,
    MenuChangeKind,
    MenuItem,
    MenuSnapshot,
    SaleRecord,
)
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Money, Sale, Segment

D = Decimal


def _sale_record(
    *,
    receipt_number: str,
    line_id: str,
    item_id: str = "chang-draft-500",
    day: date = date(2026, 6, 24),
    price: str = "120",
    quantity: int = 1,
    segment: Segment | None = None,
) -> SaleRecord:
    """One synthetic sale record for a Chang draft."""
    return SaleRecord(
        sale=Sale(
            item_id=item_id,
            timestamp=day,
            sell_price=Money(price),
            quantity=quantity,
            segment=segment,
        ),
        receipt_number=receipt_number,
        line_id=line_id,
    )


# --- AC: sales round-trip through SQLite -------------------------------------


def test_recorded_sales_are_readable_back(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A sale written via ``record_sales`` is returned by ``sales()`` unchanged.

    Worked example. One Chang draft at 120 THB on 2026-06-24 is recorded; reading
    ``sales()`` back yields that same ``Sale`` (item id, timestamp, price,
    quantity, segment).
    """
    store = SqliteLoyverseStore.connect(":memory:")

    store.record_sales([_sale_record(receipt_number="2-1", line_id="li-1")])

    sales = store.sales()
    assert len(sales) == 1
    sale = sales[0]
    assert sale.item_id == "chang-draft-500"
    assert sale.timestamp == date(2026, 6, 24)
    assert sale.sell_price == D("120")
    assert sale.quantity == 1
    assert sale.segment is None


# --- AC: idempotency at the SQLite level -------------------------------------


def test_replaying_the_same_records_does_not_duplicate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Re-recording the same ``(receipt_number, line_id)`` is a no-op.

    Worked example. The same sale record is recorded twice. The store must keep
    exactly one copy — the unique constraint on ``(receipt_number, line_id)``
    is what makes a replayed sync (slice 3) safe.
    """
    store = SqliteLoyverseStore.connect(":memory:")
    record = _sale_record(receipt_number="2-1", line_id="li-1")

    store.record_sales([record])
    store.record_sales([record])

    assert len(store.sales()) == 1


def test_two_distinct_sales_colliding_on_value_are_both_kept() -> None:
    """Two genuinely different sales with identical value must both survive.

    Two receipts each selling the same item on the same day at the same price
    and quantity would collide under a value-based dedup key. The store dedupes
    on ``(receipt_number, line_id)`` — the Loyverse line identity — so both are
    kept. This is the safety guarantee the idempotency design rests on.
    """
    store = SqliteLoyverseStore.connect(":memory:")

    store.record_sales(
        [
            _sale_record(receipt_number="2-100", line_id="li-1"),
            _sale_record(receipt_number="2-101", line_id="li-1"),
        ]
    )

    assert len(store.sales()) == 2


# --- AC: menu snapshot + change history --------------------------------------


def _item(
    *,
    item_id: str,
    name: str = "Chang Draft 500ml",
    price: str = "120",
    segment: Segment = Segment.BAR,
) -> MenuItem:
    return MenuItem(item_id=item_id, name=name, sell_price=Money(price), segment=segment)


def test_first_menu_snapshot_records_added_changes() -> None:
    """Recording the first snapshot emits one ADDED change per item.

    Worked example. One snapshot with two items is recorded at a known instant.
    The change history carries one ADDED per item, timestamped at the snapshot.
    """
    store = SqliteLoyverseStore.connect(":memory:")
    at = datetime(2026, 6, 23, tzinfo=timezone.utc)

    store.record_menu_snapshot(
        MenuSnapshot(
            items=(
                _item(item_id="i-1", name="Chang Draft", price="120"),
                _item(
                    item_id="i-2",
                    name="Latte",
                    price="80",
                    segment=Segment.CAFE,
                ),
            )
        ),
        at=at,
    )

    history = store.menu_change_history()
    assert {h.change_kind for h in history} == {MenuChangeKind.ADDED}
    by_item = {h.item_id: h for h in history}
    assert set(by_item) == {"i-1", "i-2"}
    assert by_item["i-1"].at == at
    assert by_item["i-1"].from_value is None
    assert by_item["i-1"].to_value == "Chang Draft"


def test_second_snapshot_diffs_price_rename_and_discontinue() -> None:
    """A second snapshot emits PRICE_CHANGE, RENAMED, and DISCONTINUED changes.

    Worked example. First snapshot: Chang @ 120, Leo @ 100. Second snapshot:
    Chang repriced to 140 and renamed to 'Chang Draft 500ml', Leo gone
    (discontinued), Latte added. The history must carry exactly:
      - i-1 (Chang): PRICE_CHANGE 120 -> 140 and RENAMED 'Chang' -> 'Chang Draft 500ml'
      - i-leo: DISCONTINUED
      - i-2 (Latte): ADDED
    """
    store = SqliteLoyverseStore.connect(":memory:")
    first_at = datetime(2026, 6, 23, tzinfo=timezone.utc)
    second_at = datetime(2026, 6, 24, tzinfo=timezone.utc)

    store.record_menu_snapshot(
        MenuSnapshot(
            items=(
                _item(item_id="i-1", name="Chang", price="120"),
                _item(item_id="i-leo", name="Leo", price="100"),
            )
        ),
        at=first_at,
    )
    store.record_menu_snapshot(
        MenuSnapshot(
            items=(
                _item(item_id="i-1", name="Chang Draft 500ml", price="140"),
                _item(
                    item_id="i-2",
                    name="Latte",
                    price="80",
                    segment=Segment.CAFE,
                ),
            )
        ),
        at=second_at,
    )

    history = store.menu_change_history()
    # Filter to the second snapshot's changes (the first snapshot emitted ADDEDs).
    second_changes = [h for h in history if h.at == second_at]
    by_kind: dict[str, list[MenuChange]] = {}
    for h in second_changes:
        by_kind.setdefault(h.change_kind.value, []).append(h)

    assert {c.item_id for c in by_kind.get("price_change", [])} == {"i-1"}
    assert {c.item_id for c in by_kind.get("renamed", [])} == {"i-1"}
    assert {c.item_id for c in by_kind.get("discontinued", [])} == {"i-leo"}
    assert {c.item_id for c in by_kind.get("added", [])} == {"i-2"}

    price_change = by_kind["price_change"][0]
    assert price_change.from_value == "100" or price_change.from_value == "120"
    assert price_change.to_value == "140"


def test_current_menu_reflects_the_latest_snapshot() -> None:
    """``current_menu`` returns the most recent snapshot's items.

    After a first snapshot adds Chang and Leo, then a second snapshot reprices
    Chang and drops Leo (discontinues it) and adds Latte, the current menu must
    show Chang (at its new price) and Latte — not Leo.
    """
    store = SqliteLoyverseStore.connect(":memory:")

    store.record_menu_snapshot(
        MenuSnapshot(
            items=(
                _item(item_id="i-1", name="Chang", price="120"),
                _item(item_id="i-leo", name="Leo", price="100"),
            )
        ),
        at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )
    store.record_menu_snapshot(
        MenuSnapshot(
            items=(
                _item(item_id="i-1", name="Chang", price="140"),
                _item(
                    item_id="i-2",
                    name="Latte",
                    price="80",
                    segment=Segment.CAFE,
                ),
            )
        ),
        at=datetime(2026, 6, 24, tzinfo=timezone.utc),
    )

    menu = store.current_menu()
    assert set(menu) == {"i-1", "i-2"}
    assert menu["i-1"].sell_price == D("140")
    assert menu["i-2"].segment is Segment.CAFE
    assert "i-leo" not in menu


def test_current_menu_is_empty_before_any_snapshot() -> None:
    """A fresh store has no menu snapshots; ``current_menu`` returns ``{}``."""
    store = SqliteLoyverseStore.connect(":memory:")
    assert store.current_menu() == {}
    assert store.menu_change_history() == ()


# --- AC (slice 5): last successful sync timestamp ----------------------------


def test_last_sync_at_is_none_before_any_sync() -> None:
    """A store that has never synced reports no last-sync timestamp.

    Slice 5 surfaces "when did we last pull fresh data?" on the review page and
    a stale-data banner when that is too old. A fresh store has never run a
    sync, so there is no timestamp to show — ``last_sync_at`` returns ``None``
    and the UI renders a "never synced" affordance rather than a broken page.
    """
    store = SqliteLoyverseStore.connect(":memory:")
    assert store.last_sync_at() is None


def test_last_sync_at_returns_latest_snapshot_timestamp() -> None:
    """``last_sync_at`` is the most recent sync's timestamp.

    Every successful sync records a menu snapshot stamped with the sync moment
    (``record_menu_snapshot(..., at=...)``); a failed sync never reaches that
    write. So the latest snapshot's ``at`` is exactly "the last time a sync
    succeeded" — what the review page shows and the staleness banner checks.

    Worked example. Two nightly syncs run at 22:30 UTC on consecutive days.
    ``last_sync_at`` returns the second (most recent) one, not the first.
    """
    store = SqliteLoyverseStore.connect(":memory:")
    first_at = datetime(2026, 6, 23, 22, 30, tzinfo=timezone.utc)
    second_at = datetime(2026, 6, 24, 22, 30, tzinfo=timezone.utc)

    store.record_menu_snapshot(MenuSnapshot(items=()), at=first_at)
    store.record_menu_snapshot(MenuSnapshot(items=()), at=second_at)

    assert store.last_sync_at() == second_at


# --- AC: persistence across a process restart --------------------------------


def test_sales_and_menu_survive_a_connection_reopen(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Data written to a file-backed DB is read back after reopening.

    This is the AC: "killing the process and re-running produces the same review
    numbers (data persisted)." The genuine boundary is the filesystem; the test
    writes via one store instance, closes it, opens a fresh instance against the
    same file, and confirms the sales, menu, and history all round-trip.
    """
    db_path = str(tmp_path / "tangerine.db")

    # First "process": write a sale and a snapshot.
    first = SqliteLoyverseStore.connect(db_path)
    first.record_sales([_sale_record(receipt_number="2-1", line_id="li-1")])
    first.record_menu_snapshot(
        MenuSnapshot(items=(_item(item_id="i-1", name="Chang", price="120"),)),
        at=datetime(2026, 6, 24, tzinfo=timezone.utc),
    )
    first.close()

    # Second "process": reopen and read back.
    second = SqliteLoyverseStore.connect(db_path)
    sales = second.sales()
    assert len(sales) == 1
    assert sales[0].item_id == "chang-draft-500"
    assert sales[0].sell_price == D("120")

    menu = second.current_menu()
    assert set(menu) == {"i-1"}
    assert menu["i-1"].name == "Chang"

    history = second.menu_change_history()
    assert any(h.change_kind is MenuChangeKind.ADDED for h in history)
    second.close()


def test_migration_runner_records_applied_migrations(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A fresh DB records the initial migration as applied; reopening is a no-op.

    The runner is idempotent: the second connection must not re-apply the
    migration. We assert via the ``schema_migrations`` bookkeeping table (the
    runner's own state, not the app tables) so the test pins the contract.
    """
    import sqlite3

    db_path = str(tmp_path / "tangerine.db")

    first = SqliteLoyverseStore.connect(db_path)
    first.close()

    raw = sqlite3.connect(db_path)
    applied = [row[0] for row in raw.execute("SELECT id FROM schema_migrations")]
    raw.close()
    assert 1 in applied, "initial migration (id 1) must be recorded as applied"

    # Reopen: the runner must treat the DB as current (no new migrations applied).
    second = SqliteLoyverseStore.connect(db_path)
    raw2 = sqlite3.connect(db_path)
    reapplied = [row[0] for row in raw2.execute("SELECT id FROM schema_migrations")]
    raw2.close()
    second.close()
    assert reapplied == applied
