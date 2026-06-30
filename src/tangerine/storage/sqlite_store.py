"""SQLite-backed ``LoyverseStore`` (Wave 1, Slice 1).

Satisfies the same :class:`~tangerine.loyverse.store.LoyverseStore` protocol the
in-memory implementation satisfies: ``record_sales``, ``record_menu_snapshot``,
``sales``, ``current_menu``, ``menu_change_history``. Sales are idempotent on
``(receipt_number, line_id)`` via a ``PRIMARY KEY`` constraint, so a replayed
sync (slice 3) never double-counts.

Connection configuration is the caller's concern: ``connect(":memory:")`` for
tests, ``connect(path_to_db_file)`` for the running tool. The path lives in an
environment variable in production (ADR-0001).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timezone

from ..loyverse.store import (
    MenuChange,
    MenuChangeKind,
    MenuItem,
    MenuSnapshot,
    SaleRecord,
    diff_menu,
)
from ..types import Money, Sale, Segment
from .schema import apply_migrations


class SqliteLoyverseStore:
    """``LoyverseStore`` backed by a SQLite database.

    The store holds an open connection for its lifetime. ``connect(":memory:")``
    builds an in-process database (used by tests); ``connect(path)`` opens or
    creates a file-backed database (used by the running tool, so refreshes and
    cross-device access survive a process restart).

    The connection is serialised by a per-store lock. SQLite connections are
    NOT safe for concurrent use from multiple threads even with
    ``check_same_thread=False`` — that flag only lifts Python's same-thread
    guard, it does not make the C-level connection thread-safe. FastAPI serves
    sync routes from a threadpool and the nightly sync cron runs alongside the
    web app, so two writers race the same connection in production. Without
    the lock, pysqlite surfaces the resulting state corruption as
    ``sqlite3.InterfaceError: bad parameter or other API misuse``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        with self._lock:
            apply_migrations(self._conn)

    @classmethod
    def connect(cls, database: str) -> SqliteLoyverseStore:
        """Open (or create) the database at ``database`` and migrate it.

        ``":memory:"`` builds a fresh in-process database. A filesystem path
        opens or creates that file. Migrations are applied on construction so
        a freshly-created database is immediately usable.
        """
        conn = sqlite3.connect(database)
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    # --- LoyverseStore: sales -------------------------------------------------

    def record_sales(self, records: list[SaleRecord]) -> None:
        """Persist sales, idempotent on each record's ``(receipt_number, line_id)``.

        Re-inserting a record with the same key is a no-op (``INSERT OR IGNORE``
        against the primary key), so a replayed sync never double-counts.
        """
        with self._lock, self._conn:
            for rec in records:
                self._conn.execute(
                    "INSERT OR IGNORE INTO sales"
                    " (receipt_number, line_id, item_id, timestamp,"
                    "  sell_price, quantity, segment)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.receipt_number,
                        rec.line_id,
                        rec.sale.item_id,
                        rec.sale.timestamp.isoformat(),
                        str(rec.sale.sell_price),
                        rec.sale.quantity,
                        rec.sale.segment.value if rec.sale.segment else None,
                    ),
                )

    def sales(self) -> list[Sale]:
        """All stored sales, in insertion order (row id)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, timestamp, sell_price, quantity, segment"
                " FROM sales ORDER BY rowid"
            ).fetchall()
        return [self._row_to_sale(r) for r in rows]

    @staticmethod
    def _row_to_sale(row: tuple[str, str, str, int, str | None]) -> Sale:
        item_id, timestamp, sell_price, quantity, segment = row
        return Sale(
            item_id=item_id,
            timestamp=date.fromisoformat(timestamp),
            sell_price=Money(sell_price),
            quantity=quantity,
            segment=Segment(segment) if segment else None,
        )

    # --- LoyverseStore: menu snapshots ----------------------------------------

    def record_menu_snapshot(self, snapshot: MenuSnapshot, at: datetime) -> None:
        """Record a snapshot, diffing against the previous one into history.

        Delegates the diff to :func:`diff_menu` (shared with the in-memory
        store) so both implementations produce identical change histories for
        the same inputs. The snapshot's items are persisted so
        ``current_menu`` reflects the latest state.

        The previous-menu read and the snapshot write happen under one lock so
        a concurrent snapshot cannot slip between them and produce a stale
        diff (and a duplicated/missed change record).
        """
        incoming = {mi.item_id: mi for mi in snapshot.items}
        at_iso = _datetime_to_iso(at)
        with self._lock, self._conn:
            previous = self._current_menu_locked()
            changes = diff_menu(previous, incoming, at)
            cur = self._conn.execute(
                "INSERT INTO menu_snapshots (at) VALUES (?)", (at_iso,)
            )
            snapshot_id = cur.lastrowid
            for mi in snapshot.items:
                self._conn.execute(
                    "INSERT INTO menu_items"
                    " (snapshot_id, item_id, name, sell_price, segment)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        mi.item_id,
                        mi.name,
                        str(mi.sell_price),
                        mi.segment.value,
                    ),
                )
            for change in changes:
                self._conn.execute(
                    "INSERT INTO menu_changes"
                    " (item_id, change_kind, at, from_value, to_value)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        change.item_id,
                        change.change_kind.value,
                        at_iso,
                        change.from_value,
                        change.to_value,
                    ),
                )

    def current_menu(self) -> dict[str, MenuItem]:
        """The menu as of the most recent snapshot.

        Reads the items belonging to the highest-id snapshot. An empty dict when
        no snapshot has been recorded yet.
        """
        with self._lock:
            return self._current_menu_locked()

    def _current_menu_locked(self) -> dict[str, MenuItem]:
        """``current_menu`` assuming the caller already holds ``self._lock``."""
        latest = self._conn.execute(
            "SELECT id FROM menu_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return {}
        snapshot_id = latest[0]
        rows = self._conn.execute(
            "SELECT item_id, name, sell_price, segment"
            " FROM menu_items WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchall()
        return {
            row[0]: MenuItem(
                item_id=row[0],
                name=row[1],
                sell_price=Money(row[2]),
                segment=Segment(row[3]),
            )
            for row in rows
        }

    def last_sync_at(self) -> datetime | None:
        """The timestamp of the most recent successful sync, or ``None``.

        Every successful sync records a menu snapshot stamped with the sync
        moment (a sync that fails — e.g. an expired Loyverse token — never
        reaches that write), so the latest ``menu_snapshots.at`` is exactly
        "when did a sync last succeed?". Slice 5 surfaces this on the review
        page and uses it to decide whether the stale-data banner shows.

        Returns ``None`` when no snapshot has been recorded yet (a store that
        has never synced).
        """
        row = self._execute_locked(
            "SELECT at FROM menu_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return _iso_to_datetime(row[0])

    def menu_change_history(self) -> tuple[MenuChange, ...]:
        """Every recorded menu change, in chronological then insertion order."""
        rows = self._execute_locked(
            "SELECT item_id, change_kind, at, from_value, to_value"
            " FROM menu_changes ORDER BY id"
        ).fetchall()
        return tuple(
            MenuChange(
                item_id=row[0],
                change_kind=MenuChangeKind(row[1]),
                at=_iso_to_datetime(row[2]),
                from_value=row[3],
                to_value=row[4],
            )
            for row in rows
        )

    def _execute_locked(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        """Run ``self._conn.execute`` under the store lock.

        Read-only helpers (``last_sync_at``, ``menu_change_history``) share this
        so they too are serialised against concurrent writers; without it, a
        read that races a write on the same connection raises the same
        ``InterfaceError`` that ``record_sales`` used to.
        """
        with self._lock:
            return self._conn.execute(sql, params)


def _datetime_to_iso(at: datetime) -> str:
    """Serialize a datetime for storage, normalizing to aware UTC.

    SQLite stores text; round-tripping a tz-aware datetime requires a stable
    format. Naive datetimes are assumed UTC (the sync path always passes aware
    datetimes; this covers hand-built tests).
    """
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).isoformat()


def _iso_to_datetime(value: str) -> datetime:
    """Parse a stored ISO-8601 timestamp back into a tz-aware datetime."""
    return datetime.fromisoformat(value)


__all__ = ["SqliteLoyverseStore"]
