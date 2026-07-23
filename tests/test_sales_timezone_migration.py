"""Issue #66 — the migration that heals historical sales rows.

The UTC→Asia/Bangkok parser fix is correct for *new* syncs, but the sales
table's ``INSERT OR IGNORE`` dedup means a re-sync will not overwrite rows
that are already there with the wrong date/segment. The migration must:

1. Add a ``sales.created_at`` column so the raw Loyverse UTC timestamp is
   preserved going forward (future fixes can re-derive date/segment from it).
2. Drop every pre-existing sales row, so the next sync detects an empty
   sales table, treats it as a first run, and backfills with correct local-
   time date/segment. The venue went live in early July 2026 (map #62), well
   inside ``run_sync``'s 30-day backfill window — so nothing is lost.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from tangerine.loyverse.parser import parse_receipts_to_sales
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Segment


def _store_with_legacy_row(db_path: str) -> None:
    """Build a pre-#66 database at ``db_path`` with one legacy sales row.

    Simulates the live system's state the moment before migration 0008 first
    runs: the ``sales`` table exists without a ``created_at`` column, carrying
    a row whose date and segment were derived from the UTC timestamp (the bug
    — an 18:00 local bar sale stamped *cafe* because 11:00 UTC is inside
    ``[8, 17)``).

    Built by applying migrations 0001–0007 by hand (the runner reads them
    from the importable ``migrations`` subpackage), then writing the legacy
    row, then leaving migration 0008 un-recorded so the next ``connect``
    applies it.
    """
    from importlib import resources

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  id INTEGER PRIMARY KEY, applied_at TEXT NOT NULL"
        ")"
    )
    # Apply migrations 0001 through 0007 only.
    migrations_root = resources.files("tangerine.storage.migrations")
    for f in sorted(migrations_root.iterdir(), key=lambda f: f.name):
        if not f.name.endswith(".sql"):
            continue
        migration_id = int(f.name.split("_", 1)[0])
        if migration_id >= 8:
            continue
        conn.executescript(f.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            (migration_id, "2026-07-14T00:00:00.000Z"),
        )
        conn.commit()
    # Insert the legacy row. No created_at column exists yet — that's the
    # pre-#66 schema.
    conn.execute(
        "INSERT INTO sales"
        " (receipt_number, line_id, item_id, timestamp,"
        "  sell_price, quantity, segment)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-1",
            "li-1",
            "chang-draft-500",
            "2026-07-14",  # the UTC date (the bug)
            "120",
            1,
            "cafe",  # stamped from the UTC hour (the bug)
        ),
    )
    conn.commit()
    conn.close()


def test_migration_drops_pre_existing_sales_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A pre-#66 sales row must not survive the migration.

    Without this, the row sits there with the wrong date/segment forever —
    ``INSERT OR IGNORE`` means the next sync will not overwrite it.
    """
    db_path = str(tmp_path / "tangerine.db")
    _store_with_legacy_row(db_path)

    # Re-opening the store applies migration 0008, which must DELETE the row.
    reopened = SqliteLoyverseStore.connect(db_path)
    raw = sqlite3.connect(db_path)
    remaining = raw.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    raw.close()
    reopened.close()

    assert remaining == 0, (
        "migration 0008 must drop pre-#66 sales rows so the next first-run "
        "backfill repopulates them with correct local-time date/segment"
    )


def test_migration_adds_created_at_column(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """After migration the ``sales`` table has a ``created_at`` column.

    The parser now stamps ``SaleRecord.created_at_utc``; the store writes it
    here so the date/segment can be re-derived in future without re-fetching.
    """
    db_path = str(tmp_path / "tangerine.db")
    store = SqliteLoyverseStore.connect(db_path)
    raw = sqlite3.connect(db_path)
    cols = {row[1] for row in raw.execute("PRAGMA table_info(sales)").fetchall()}
    raw.close()
    store.close()

    assert "created_at" in cols


def test_new_sync_writes_created_at_utc(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A record produced by the parser and stored carries its source UTC time.

    This is the going-forward half of the fix: from #66 on, the table is
    self-healing because the source timestamp is preserved.
    """
    db_path = str(tmp_path / "tangerine.db")
    store = SqliteLoyverseStore.connect(db_path)

    records = parse_receipts_to_sales(
        {"receipts": [
            {
                "receipt_number": "66-m-1",
                "receipt_type": "SALE",
                "refund_for": None,
                "created_at": "2026-07-14T11:00:00.000Z",  # 18:00 local
                "receipt_date": "2026-07-14T11:00:00.000Z",
                "total_money": 120,
                "total_tax": 0,
                "line_items": [
                    {
                        "id": "li-1",
                        "item_id": "i-1",
                        "sku": "chang-draft-500",
                        "quantity": 1,
                        "price": 120,
                    }
                ],
            }
        ]}
    )
    store.record_sales(records)

    raw = sqlite3.connect(db_path)
    row = raw.execute(
        "SELECT timestamp, segment, created_at FROM sales"
        " WHERE receipt_number = ?",
        ("66-m-1",),
    ).fetchone()
    raw.close()
    store.close()

    timestamp, segment, created_at = row
    assert timestamp == "2026-07-14"
    assert segment == "bar"  # 18:00 local is in the bar window — the fix
    assert created_at is not None
    assert created_at.startswith("2026-07-14T11:00"), (
        f"created_at should preserve the Loyverse UTC timestamp, got {created_at!r}"
    )


def test_next_sync_after_migration_repopulates_with_correct_segments(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: after migration, a sync that re-fetches a receipt lands the
    corrected date/segment, not the legacy one.

    This is the healing story in one test: the migration wipes the table, the
    next call to ``record_sales`` (which is what ``run_sync`` does on the
    first-run backfill path) inserts the row afresh with the local-time
    date/segment.
    """
    db_path = str(tmp_path / "tangerine.db")
    _store_with_legacy_row(db_path)

    reopened = SqliteLoyverseStore.connect(db_path)
    # The first-run detection in ``run_sync`` keys off an empty sales table,
    # so confirm the migration made it empty.
    assert reopened.sales() == []

    # Re-fech the same receipt the legacy row came from, this time through the
    # fixed parser.
    reopened.record_sales(
        parse_receipts_to_sales(
            {"receipts": [
                {
                    "receipt_number": "legacy-1",  # same Loyverse identity
                    "receipt_type": "SALE",
                    "refund_for": None,
                    "created_at": "2026-07-14T11:00:00.000Z",
                    "receipt_date": "2026-07-14T11:00:00.000Z",
                    "total_money": 120,
                    "total_tax": 0,
                    "line_items": [
                        {
                            "id": "li-1",
                            "item_id": "i-1",
                            "sku": "chang-draft-500",
                            "quantity": 1,
                            "price": 120,
                        }
                    ],
                }
            ]}
        )
    )

    sales = reopened.sales()
    reopened.close()

    assert len(sales) == 1
    # Local 18:00 → bar (the fix), not cafe (the legacy bug).
    assert sales[0].segment is Segment.BAR
    assert sales[0].timestamp == date(2026, 7, 14)
