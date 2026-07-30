"""SQLite-backed config store + one-time YAML seeder (Wave 1.5, Slice 1).

Implements ADR-0003 decision 1: recipes, costs, and mappings move out of the
shipped YAML files into SQLite. The YAML files become seed-only — read once
on first run if the config tables are empty, never read at runtime after.

The store is the read-side half of the engine's :class:`~tangerine.ingestion.Source`
protocol: ``recipes()`` / ``cost_book()`` / ``mappings()``. ``StoreSource`` delegates
to it once wired up; the engine itself is unchanged.

The seeder reuses the existing YAML loaders (:func:`~tangerine.config.loader.load_recipes`,
:func:`~tangerine.config.loader.load_costs`) so a malformed file still fails
loudly at startup with the same ``ConfigError``. The loaders' parsing behaviour
is unchanged — only their *role* narrows from "called every startup" to "called
once by the migrator".
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ..cash_spend import CashSpendEntry
from ..config.loader import load_costs, load_recipes
from ..cost import CostBook
from ..fixed_costs import FixedCostEntry
from ..price_history import PriceChange, PriceHistory
from ..quantity import estimated_yield
from ..recipes import RecipeCatalog
from ..types import Recipe, RecipeIngredient, Segment, SkuMapping, SkuRecord, Supplier
from .schema import apply_migrations

_MIGRATION_ACTOR = "migration"

#: The six spend buckets the HTML cost-breakdown column shows, in display
#: order. The seed lands them in this order so the admin page renders them
#: top-to-bottom as the partner recognises them from the printed map
#: (issue #95). Issue #82 decision D locked these as the non-empty default
#: — the analogue of ADR-0009's empty cafe set, except non-empty because
#: the HTML's six are known.
_SEEDED_SPEND_BUCKETS: tuple[tuple[str, str], ...] = (
    ("taps", "Taps"),
    ("kitchen", "Kitchen"),
    ("coffee", "Coffee"),
    ("bakery", "Bakery"),
    ("staff", "Staff"),
    ("rent", "Rent"),
)


@dataclass(frozen=True)
class AuditEntry:
    """One row of the ``audit_log`` table — the paper trail (Slice 5).

    ``old_value`` / ``new_value`` are whole-row snapshots (column -> stored
    value) of the changed row, taken immediately before and after the write.
    ``None`` means the row did not exist on that side: a creation has no
    ``old_value``; a deletion (a reverted creation) has no ``new_value``.
    Whole-row snapshots rather than per-field rows because "revert exactly
    that one change" means undoing a *save action* — a cost save touches
    pack price, quantity, and the VAT flag together, and they must be undone
    together. Revert diffs the two snapshots and restores exactly the fields
    the entry changed, so later edits to *other* fields of the same row
    survive (see :meth:`SqliteConfigStore.revert_entry`).

    ``reason`` is the optional why a partner typed when reverting (ADR-0003:
    the log records intent); ``None`` for ordinary edits.
    """

    entry_id: int
    table_name: str
    pk: str
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    changed_by: str
    changed_at: str
    session_id: str | None
    reason: str | None


@dataclass(frozen=True)
class CostRow:
    """One full row of the ``costs`` table, as the editor and upload see it.

    Unlike :class:`~tangerine.cost.CostBook` (the engine's net-price view),
    this carries the receipt-shaped provenance: pack price/quantity are
    ``None`` for rows migrated from YAML (the file recorded only the derived
    per-unit price), and populated once a partner saves through the editor
    or the upload path.
    """

    sku_id: str
    pack_price: Decimal | None
    pack_quantity: Decimal | None
    vat_inclusive: bool
    price_per_unit_net: Decimal
    updated_at: date
    updated_by: str


@dataclass(frozen=True)
class SpendBucket:
    """One row of the ``spend_buckets`` table (issue #95, parent #82).

    The controlled vocabulary cash-spend rows (slice #96) FK into.
    ``bucket_id`` is the stable slug a partner types or that the seed
    ships; ``name`` is the display label rendered in the picker and on the
    page. ``retired_at`` carries the soft-retire timestamp — a retired
    bucket stays in the table so historical cash-spend rows keep
    aggregating under it, but is excluded from the new-entry picker.

    A spend bucket is a **product-family / cost-category** concept, never a
    segment: "taps" means bar-product-family spend, not "the bar segment".
    Whether a bucket's cost falls against the cafe or bar segment is a
    downstream P&L computation against recipes (ADR-0007 pure-clock
    segmentation), not a fact of the purchase.
    """

    bucket_id: str
    name: str
    retired_at: str | None
    created_at: str
    created_by: str


@dataclass(frozen=True)
class LoyverseExport:
    """One row of the ``loyverse_exports`` table (issue #102, parent spec #100).

    The Loyverse cost-mirror's paper trail. Every confirmed export — including
    a zero-drift confirm — leaves one row here, so "the mirror was confirmed
    current on <date>" is a visible fact rather than something inferred from
    the absence of a later edit. The drift badge (slice 3, issue #103) reads
    this newest-first to answer "how stale is Loyverse?"; any future export-
    history surface reads the same rows.

    This is a **dedicated table**, deliberately not a ``kind`` on
    :class:`AuditEntry` / ``audit_log``. ``audit_log`` feeds the 9am "N
    changes since last review" count (``unreviewed_changes``); a Loyverse-
    bound export is a mirror action, not a config edit, and would pollute
    that count. The dedicated-vs-audit-log decision is the Q5 resolution in
    issue #70 / spec #100.

    ``drift_payload`` is the raw JSON string the table holds — an array of
    ``{sku, name, loyverse_cost, books_cost}`` for the changed rows, exactly
    the diff the prepare step (issue #101) rendered. Kept as a string so the
    read side need not re-parse to answer "when was the last export?".
    """

    id: int
    partner_id: str
    confirmed_at: str
    item_count: int
    changed_count: int
    drift_payload: str


class SqliteConfigStore:
    """Read-side view over the config tables.

    The store holds an open connection for its lifetime. ``":memory:"`` for
    tests; a filesystem path for the running tool. Like
    :class:`~tangerine.storage.sqlite_store.SqliteLoyverseStore`, the connection
    is serialised by a per-store lock because SQLite connections are not safe
    for concurrent use from multiple threads even with ``check_same_thread=False``
    — and the web app serves sync routes from a threadpool alongside the nightly
    sync cron.

    Multi-write authoring strokes (:meth:`batch`) run several audited writes in
    one lock + one SQLite transaction, so a mid-stroke failure rolls back every
    write and its audit rows together. A stroke like the sold-as-is
    quick-create (a purchasable SKU + its cost + a sold SKU + a serving recipe
    + a mapping) lands as all-or-nothing. Audit semantics are unchanged: each
    write still records its own audit row, all stamped with the same
    ``session_id`` — the only difference from sequential standalone calls is
    atomicity.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: threading.Lock | threading.RLock | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        """``lock`` lets a caller share one serialisation lock across every
        store wrapping the *same* connection — see
        :class:`~tangerine.storage.sqlite_store.SqliteLoyverseStore` for why
        two independent locks over one connection would be unsafe. Defaults
        to a private lock for standalone use (tests, the migration seam's
        own tests).

        ``now`` is the clock (returning a UTC ISO-8601 string) stamped on
        audit entries, mappings, SKU creations, and review marks. Injectable
        — like ``auth.py``'s clock and ``create_app``'s ``now_epoch`` — so
        the web app can drive the store and the "under 24 hours" banner from
        the same pinned instant in tests. Defaults to the wall clock.

        The lock may be a plain :class:`~threading.Lock` or an
        :class:`~threading.RLock`; an ``RLock`` is required when callers use
        :meth:`batch` (the write methods called inside a ``batch`` block
        re-enter the same lock the block already holds).
        :func:`~tangerine.web.app.create_app` always shares an ``RLock``
        with the Loyverse store so config authoring routes can wrap their
        multi-write strokes in ``batch()``.
        """
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()
        self._now = now if now is not None else _utc_now_iso
        # True while a :meth:`batch` block holds the lock + open transaction.
        # The write methods check this to decide whether to take the lock and
        # manage their own transaction (standalone call) or assume the caller
        # already holds both (call from inside ``batch``). Guarded by ``_lock``.
        self._in_batch = False

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Run several audited writes in one lock + one SQLite transaction.

        Inside the block, the store's write methods (``save_cost``,
        ``save_mapping``, ``create_sku``, ``save_recipe``, ``delete_recipe``,
        ``create_fixed_cost``, ``end_fixed_cost``, ``delete_fixed_cost``)
        join the open transaction instead of each opening and committing
        their own. A normal block exit commits the whole batch atomically;
        any exception propagating out rolls back every write *and* its audit
        rows — the audit log never records a partial stroke.

        The same ``session_id`` threaded into each write keeps the audit
        grouping (per-session revert, "N changes since last review")
        identical to what N sequential standalone calls would have
        produced — only the all-or-nothing durability is new.

        ``batch`` blocks may be nested; the innermost block that is not
        itself inside a ``batch`` owns the transaction (its ``with
        self._conn`` is the commit/rollback point). Inner blocks just
        re-enter the already-held lock and yield without touching the
        transaction, so a helper that wraps its own ``batch()`` is safe to
        call either standalone or from inside another batch. The lock is
        held for the outermost block's whole duration, so a long-running
        stroke serialises against concurrent writers — the same
        serialisation standalone writes already buy.
        """
        with self._lock:
            outer = not self._in_batch
            if outer:
                self._in_batch = True
            try:
                if outer:
                    with self._conn:
                        yield
                else:
                    yield
            finally:
                if outer:
                    self._in_batch = False

    def recipes(self) -> list[Recipe]:
        """All stored recipes, in sku_id order, each with its ingredient rows.

        Ingredients come back ordered by ``position`` so the same recipe
        round-trips deterministically (and so a recipe that legitimately uses
        the same SKU twice keeps its stages in order).
        """
        with self._lock:
            header_rows = self._conn.execute(
                "SELECT sku_id, name, segment, yield_qty, yield_estimated,"
                " target_gross_margin_pct, prep"
                " FROM recipes ORDER BY sku_id"
            ).fetchall()
            if not header_rows:
                return []
            ingredient_rows = self._conn.execute(
                "SELECT sku_id, ingredient_sku_id, quantity, position"
                " FROM recipe_ingredients ORDER BY sku_id, position"
            ).fetchall()
        ingredients_by_recipe: dict[str, list[RecipeIngredient]] = {}
        for recipe_sku, ing_sku, quantity, _position in ingredient_rows:
            ingredients_by_recipe.setdefault(recipe_sku, []).append(
                RecipeIngredient(sku_id=ing_sku, quantity=_parse_decimal(quantity))
            )
        return [
            Recipe(
                sku_id=sku_id,
                name=name,
                segment=Segment(segment),
                ingredients=tuple(ingredients_by_recipe.get(sku_id, [])),
                yield_qty=_parse_decimal(yield_qty),
                yield_estimated=bool(yield_estimated),
                target_gross_margin_pct=(
                    _parse_decimal(target) if target is not None else None
                ),
                prep=bool(prep),
            )
            for sku_id, name, segment, yield_qty, yield_estimated, target, prep in header_rows
        ]

    def cost_book(self) -> CostBook:
        """All stored costs as a :class:`CostBook`.

        The table holds net per-unit prices (``price_per_unit_net``); the
        book is built directly from those. Slice 3's editor will start
        capturing pack price + quantity; this read side is already net.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT sku_id, price_per_unit_net, updated_at FROM costs"
            ).fetchall()
        prices: dict[str, tuple[Decimal, date]] = {}
        for sku_id, price_net, updated_at in rows:
            prices[sku_id] = (Decimal(price_net), date.fromisoformat(updated_at))
        return CostBook(prices)

    def price_history(self) -> PriceHistory:
        """The price-as-of-date lookup, reconstructed from the audit log.

        Wave 2 slice 1 (ADR-0004 decision 2): every cost edit already
        snapshots the row's old/new ``price_per_unit_net`` in ``audit_log``,
        so a SKU's price on any past date falls out of walking those
        entries — no new capture, no new table. SKUs with no cost-edit
        history (everything pre-cutover) resolve to their current seed
        price via the cost book.

        A cost edit takes effect on the *day it was made*: the partner
        repriced that morning, so that day's sales carry the new price and
        every earlier day keeps the old one. "The day" is the partner-facing
        calendar date the save recorded in the row's ``updated_at``
        (see :func:`_change_effective_date`), not the UTC date of the audit
        timestamp — the venue runs at UTC+7, so those disagree for
        early-morning edits.
        """
        current = self.cost_book()
        changes: list[PriceChange] = []
        # audit_entries() is newest-first; the history wants chronological
        # order so same-day edits resolve to the last one saved.
        for entry in reversed(self.audit_entries()):
            if entry.table_name != "costs":
                continue
            changes.append(
                PriceChange(
                    sku_id=entry.pk,
                    changed_on=_change_effective_date(entry),
                    old_price=_snapshot_price(entry.old_value),
                    new_price=_snapshot_price(entry.new_value),
                )
            )
        return PriceHistory(current=current, changes=changes)

    def cost_rows(self) -> list[CostRow]:
        """Every ``costs`` row with its receipt-shaped provenance, by sku_id.

        The upload template and the cost editor need the pack inputs and the
        VAT flag, not just the derived net price ``cost_book()`` exposes.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT sku_id, pack_price, pack_quantity, vat_inclusive,"
                " price_per_unit_net, updated_at, updated_by"
                " FROM costs ORDER BY sku_id"
            ).fetchall()
        return [
            CostRow(
                sku_id=sku_id,
                pack_price=Decimal(pack_price) if pack_price is not None else None,
                pack_quantity=(
                    Decimal(pack_quantity) if pack_quantity is not None else None
                ),
                vat_inclusive=bool(vat_inclusive),
                price_per_unit_net=Decimal(net),
                updated_at=date.fromisoformat(updated_at),
                updated_by=updated_by,
            )
            for sku_id, pack_price, pack_quantity, vat_inclusive, net, updated_at, updated_by in rows
        ]

    def mappings(self) -> list[SkuMapping]:
        """All stored Loyverse-item -> SKU mappings."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, sku_id FROM mappings ORDER BY item_id"
            ).fetchall()
        return [SkuMapping(item_id=item_id, sku_id=sku_id) for item_id, sku_id in rows]

    def sku(self, sku_id: str) -> SkuRecord | None:
        """One SKU by id, or ``None`` — the editor routes' existence check."""
        with self._lock:
            row = self._conn.execute(
                "SELECT sku_id, name, segment, unit FROM skus WHERE sku_id = ?",
                (sku_id,),
            ).fetchone()
        if row is None:
            return None
        found_id, name, segment, unit = row
        return SkuRecord(
            sku_id=found_id,
            name=name,
            segment=Segment(segment) if segment else None,
            unit=unit,
        )

    def save_cost(
        self,
        sku_id: str,
        *,
        pack_price: Decimal,
        pack_quantity: Decimal,
        vat_inclusive: bool,
        updated_by: str,
        updated_on: date,
        session_id: str | None = None,
    ) -> Decimal:
        """Record a new cost for ``sku_id`` from its receipt-shaped inputs.

        Per ADR-0003 decision 4 (gross-input / net-stored): the partner types
        what the receipt says — pack price as charged, pack quantity — and
        the store derives and persists the net per-unit price, dividing by
        1.07 only when the purchase was VAT-inclusive. One row per SKU
        (latest wins); history lives in the audit log (Slice 5), which every
        save appends to inside the same transaction.

        Returns the derived net per-unit price so callers can surface it.

        Batch-aware: when called inside a :meth:`batch` block the write +
        audit row join that block's open transaction (so a mid-stroke
        failure rolls them back); standalone calls open and commit their
        own transaction as before.
        """
        net = net_price_per_unit(pack_price, pack_quantity, vat_inclusive)
        if self._in_batch:
            self._save_cost_impl(
                sku_id,
                net=net,
                pack_price=pack_price,
                pack_quantity=pack_quantity,
                vat_inclusive=vat_inclusive,
                updated_by=updated_by,
                updated_on=updated_on,
                session_id=session_id,
            )
        else:
            with self._lock, self._conn:
                self._save_cost_impl(
                    sku_id,
                    net=net,
                    pack_price=pack_price,
                    pack_quantity=pack_quantity,
                    vat_inclusive=vat_inclusive,
                    updated_by=updated_by,
                    updated_on=updated_on,
                    session_id=session_id,
                )
        return net

    def _save_cost_impl(
        self,
        sku_id: str,
        *,
        net: Decimal,
        pack_price: Decimal,
        pack_quantity: Decimal,
        vat_inclusive: bool,
        updated_by: str,
        updated_on: date,
        session_id: str | None,
    ) -> None:
        """The write + audit, assuming the lock + transaction are held."""
        old = self._row_snapshot("costs", "sku_id", sku_id)
        self._conn.execute(
            "INSERT OR REPLACE INTO costs"
            " (sku_id, pack_price, pack_quantity, vat_inclusive,"
            "  price_per_unit_net, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sku_id,
                str(pack_price),
                str(pack_quantity),
                1 if vat_inclusive else 0,
                str(net),
                updated_on.isoformat(),
                updated_by,
            ),
        )
        new = self._row_snapshot("costs", "sku_id", sku_id)
        self._record_audit(
            "costs",
            sku_id,
            old=old,
            new=new,
            changed_by=updated_by,
            session_id=session_id,
        )

    def audit_entries(self) -> list[AuditEntry]:
        """Every audit-log row, newest first — what ``GET /audit`` renders."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry_id, table_name, pk, old_value, new_value,"
                " changed_by, changed_at, session_id, reason"
                " FROM audit_log ORDER BY entry_id DESC"
            ).fetchall()
        return [
            AuditEntry(
                entry_id=entry_id,
                table_name=table_name,
                pk=pk,
                old_value=json.loads(old) if old is not None else None,
                new_value=json.loads(new) if new is not None else None,
                changed_by=changed_by,
                changed_at=changed_at,
                session_id=session_id,
                reason=reason,
            )
            for entry_id, table_name, pk, old, new, changed_by, changed_at, session_id, reason in rows
        ]

    def unreviewed_changes(self, assignee_id: str) -> list[AuditEntry]:
        """Audit entries ``assignee_id`` has not yet reviewed, newest first.

        "Reviewed" means: recorded before the partner last pressed the
        audit page's "Mark as reviewed" button (:meth:`mark_reviewed`). A
        partner who has never marked sees everything. This is what drives
        the 9am review's "N changes since last review" link — per partner,
        because each partner sanity-checks the diff for themselves.
        """
        with self._lock:
            mark_row = self._conn.execute(
                "SELECT reviewed_at FROM review_marks WHERE assignee_id = ?",
                (assignee_id,),
            ).fetchone()
        entries = self.audit_entries()
        if mark_row is None:
            return entries
        mark = mark_row[0]
        return [e for e in entries if e.changed_at > mark]

    def mark_reviewed(self, assignee_id: str) -> None:
        """Record that ``assignee_id`` has just reviewed the config changes."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO review_marks (assignee_id, reviewed_at)"
                " VALUES (?, ?)",
                (assignee_id, self._now()),
            )

    def revert_entry(
        self,
        entry_id: int,
        *,
        changed_by: str,
        session_id: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Undo exactly the change ``entry_id`` records.

        Surgical: only the fields that entry actually changed are set back
        to their before-values, so a later edit to a *different* field of
        the same row survives the revert. Undoing a creation (no old value)
        deletes the row. The revert is itself recorded as a new audit entry
        (from the row's *current* state to the restored one, with the
        optional ``reason`` the partner gave), so even the undo has a paper
        trail, and a revert can in turn be reverted.

        Returns ``False`` for an unknown ``entry_id``.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT table_name, pk, old_value, new_value FROM audit_log"
                " WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            if row is None:
                return False
            table_name, pk, old_json, new_json = row
            self._revert_one(
                table_name,
                pk,
                old_json,
                new_json,
                changed_by=changed_by,
                session_id=session_id,
                reason=reason,
            )
        return True

    def revert_session(
        self,
        session_id: str,
        *,
        changed_by: str,
        reverter_session_id: str | None = None,
        reason: str | None = None,
    ) -> int:
        """Undo every edit made in one browser session — the panic undo.

        Applies the single-entry revert to each of the session's entries in
        reverse chronological order, so multiple edits to the same row
        unwind cleanly back to the pre-session state. Each individual revert
        is logged (under the *reverter's* session, so the panic undo is
        itself batch-revertable; ``reason`` rides along on each).

        Returns the number of entries reverted (0 for an unknown session).
        """
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT table_name, pk, old_value, new_value FROM audit_log"
                " WHERE session_id = ? ORDER BY entry_id DESC",
                (session_id,),
            ).fetchall()
            for table_name, pk, old_json, new_json in rows:
                self._revert_one(
                    table_name,
                    pk,
                    old_json,
                    new_json,
                    changed_by=changed_by,
                    session_id=reverter_session_id,
                    reason=reason,
                )
        return len(rows)

    def _revert_one(
        self,
        table_name: str,
        pk: str,
        old_json: str | None,
        new_json: str | None,
        *,
        changed_by: str,
        session_id: str | None,
        reason: str | None,
    ) -> None:
        """Undo one entry's change against the row's *current* state, logged.

        The shared core of both revert flavours. Callers hold the lock and
        the transaction.
        """
        old = json.loads(old_json) if old_json is not None else None
        new = json.loads(new_json) if new_json is not None else None
        current = self._snapshot(table_name, pk)
        target = _revert_target(old, new, current)
        self._apply_snapshot(table_name, pk, target)
        self._record_audit(
            table_name,
            pk,
            old=current,
            new=target,
            changed_by=changed_by,
            session_id=session_id,
            reason=reason,
        )

    #: The primary-key column of each audited table — what an audit entry's
    #: ``pk`` value keys into.
    _PK_COLUMNS = {
        "costs": "sku_id",
        "mappings": "item_id",
        "skus": "sku_id",
        "recipes": "sku_id",
        "cash_spend": "id",
        "fixed_costs": "id",
        "spend_buckets": "bucket_id",
        "suppliers": "supplier_id",
    }

    def _snapshot(self, table_name: str, pk: str) -> dict[str, Any] | None:
        """The current whole-row snapshot of an audited table's row."""
        if table_name == "recipes":
            return self._recipe_snapshot(pk)
        return self._row_snapshot(table_name, self._PK_COLUMNS[table_name], pk)

    def _apply_snapshot(
        self, table_name: str, pk: str, snapshot: dict[str, Any] | None
    ) -> None:
        """Make ``table_name``'s row for ``pk`` equal ``snapshot``.

        ``None`` deletes the row (reverting a creation). The recipe shape
        spans two tables, so its ingredient rows are replaced alongside the
        header. Callers hold the lock and the transaction.
        """
        pk_column = self._PK_COLUMNS[table_name]
        if table_name == "recipes":
            self._conn.execute(
                "DELETE FROM recipe_ingredients WHERE sku_id = ?", (pk,)
            )
            self._conn.execute("DELETE FROM recipes WHERE sku_id = ?", (pk,))
            if snapshot is None:
                return
            header = {k: v for k, v in snapshot.items() if k != "ingredients"}
            self._insert_row("recipes", header)
            self._conn.executemany(
                "INSERT INTO recipe_ingredients"
                " (sku_id, ingredient_sku_id, quantity, position)"
                " VALUES (?, ?, ?, ?)",
                [
                    (pk, ingredient_sku_id, quantity, position)
                    for position, (ingredient_sku_id, quantity) in enumerate(
                        snapshot.get("ingredients", [])
                    )
                ],
            )
            return
        self._conn.execute(
            f"DELETE FROM {table_name} WHERE {pk_column} = ?", (pk,)  # noqa: S608
        )
        if snapshot is not None:
            self._insert_row(table_name, snapshot)

    def _insert_row(self, table: str, values: dict[str, Any]) -> None:
        """INSERT one row from a column -> value snapshot dict."""
        columns = list(values.keys())
        placeholders = ", ".join("?" for _ in columns)
        self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
            [values[column] for column in columns],
        )

    def _row_snapshot(
        self, table: str, pk_column: str, pk: str
    ) -> dict[str, Any] | None:
        """The full current row of ``table`` keyed by ``pk``, or ``None``.

        Column -> stored value, exactly as SQLite holds it — the shape the
        audit log's ``old_value``/``new_value`` JSON carries, and the shape
        revert writes back verbatim. Callers hold the lock.
        """
        cursor = self._conn.execute(
            f"SELECT * FROM {table} WHERE {pk_column} = ?", (pk,)  # noqa: S608 — table/pk_column are internal literals
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [description[0] for description in cursor.description]
        return dict(zip(columns, row))

    def _record_audit(
        self,
        table_name: str,
        pk: str,
        *,
        old: dict[str, Any] | None,
        new: dict[str, Any] | None,
        changed_by: str,
        session_id: str | None,
        reason: str | None = None,
    ) -> None:
        """Append one audit row for a write that just happened.

        A no-op when nothing actually changed (``old == new``) — re-saving
        identical values is not an event the partner needs to see in the
        morning diff. Callers hold the lock and the transaction; the audit
        row commits or rolls back with the write it records.
        """
        if old == new:
            return
        self._conn.execute(
            "INSERT INTO audit_log"
            " (table_name, pk, old_value, new_value,"
            "  changed_by, changed_at, session_id, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                table_name,
                pk,
                json.dumps(old) if old is not None else None,
                json.dumps(new) if new is not None else None,
                changed_by,
                self._now(),
                session_id,
                reason,
            ),
        )

    def save_mapping(
        self,
        item_id: str,
        sku_id: str,
        *,
        updated_by: str,
        session_id: str | None = None,
    ) -> None:
        """Assign a Loyverse item to a SKU (upsert — latest assignment wins).

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            self._save_mapping_impl(
                item_id, sku_id, updated_by=updated_by, session_id=session_id
            )
        else:
            with self._lock, self._conn:
                self._save_mapping_impl(
                    item_id, sku_id, updated_by=updated_by, session_id=session_id
                )

    def _save_mapping_impl(
        self,
        item_id: str,
        sku_id: str,
        *,
        updated_by: str,
        session_id: str | None,
    ) -> None:
        """The write + audit, assuming the lock + transaction are held."""
        old = self._row_snapshot("mappings", "item_id", item_id)
        self._conn.execute(
            "INSERT OR REPLACE INTO mappings"
            " (item_id, sku_id, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?)",
            (item_id, sku_id, self._now(), updated_by),
        )
        new = self._row_snapshot("mappings", "item_id", item_id)
        self._record_audit(
            "mappings",
            item_id,
            old=old,
            new=new,
            changed_by=updated_by,
            session_id=session_id,
        )

    def create_sku(
        self,
        sku_id: str,
        *,
        name: str,
        unit: str,
        created_by: str,
        session_id: str | None = None,
        segment: Segment | None = None,
    ) -> None:
        """Create a new SKU with its unit confirmed from the start.

        Unlike migrated rows (whose ``unit`` may be NULL pending partner
        confirmation), an editor-created SKU always carries its unit —
        ADR-0003 decision 3: an editor without the unit field is a
        silent-corruption machine. Segment stays NULL by default (an
        ingredient may feed both cafe and bar); the sold-as-is quick-create
        passes the Loyverse item's segment for the *sold* SKU it creates, so
        the segment-contribution-margin view attributes the sale correctly.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            self._create_sku_impl(
                sku_id,
                name=name,
                unit=unit,
                created_by=created_by,
                session_id=session_id,
                segment=segment,
            )
        else:
            with self._lock, self._conn:
                self._create_sku_impl(
                    sku_id,
                    name=name,
                    unit=unit,
                    created_by=created_by,
                    session_id=session_id,
                    segment=segment,
                )

    def _create_sku_impl(
        self,
        sku_id: str,
        *,
        name: str,
        unit: str,
        created_by: str,
        session_id: str | None,
        segment: Segment | None,
    ) -> None:
        """The write + audit, assuming the lock + transaction are held."""
        segment_value = segment.value if segment is not None else None
        self._conn.execute(
            "INSERT INTO skus"
            " (sku_id, name, segment, unit, yield_qty,"
            "  yield_estimated, target_gross_margin_pct,"
            "  created_at, created_by)"
            " VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
            (sku_id, name, segment_value, unit, self._now(), created_by),
        )
        self._record_audit(
            "skus",
            sku_id,
            old=None,
            new=self._row_snapshot("skus", "sku_id", sku_id),
            changed_by=created_by,
            session_id=session_id,
        )

    def save_recipe(
        self,
        sku_id: str,
        *,
        ingredients: list[tuple[str, Decimal]],
        yield_qty: Decimal,
        yield_estimated: bool,
        target_gross_margin_pct: Decimal | None = None,
        prep: bool = False,
        updated_by: str,
        session_id: str | None = None,
    ) -> None:
        """Replace ``sku_id``'s ingredient rows with ``ingredients``, in order.

        Each ``(ingredient_sku_id, quantity)`` pair is written at its list
        position, so the editor's row order round-trips (and the same
        ingredient may legitimately appear twice — e.g. water in two stages).
        Quantities are already in the ingredient's canonical unit; shorthand
        conversion happens at the edge (the web route), not here.

        ``yield_qty`` (issue #34) is the recipe's yield in its output SKU's
        own unit — a decimal that the engine divides the input cost by. It
        is required on every save because the editor always posts it (the
        estimated-yield recompute happens at the edge, not here, so by the
        time we are called the yield is the value the partner sees).
        ``yield_estimated`` records whether that value is the no-loss
        estimate or a measured one; the editor's badge and recompute rule
        live off it.

        ``target_gross_margin_pct`` and ``prep`` (issue #35) are the
        recipe's other whole-header editable fields: the editor posts both
        with every save (an empty input means "no target"; an unticked box
        means "not a prep"), so they are written unconditionally rather
        than preserved.

        The recipe header is created on first save — name and segment come
        from the SKU row (segment defaults to cafe for a SKU that has none;
        an ingredient-only SKU gaining a recipe must produce *something*
        saleable, and the editor lets the partner change it later) — and
        its name/segment are preserved as-is on subsequent saves.

        The audit entry snapshots the *whole recipe* (header + ingredient
        rows) as one logical row, because that is what the partner edits and
        what a revert must restore in one stroke.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            self._save_recipe_impl(
                sku_id,
                ingredients=ingredients,
                yield_qty=yield_qty,
                yield_estimated=yield_estimated,
                target_gross_margin_pct=target_gross_margin_pct,
                prep=prep,
                updated_by=updated_by,
                session_id=session_id,
            )
        else:
            with self._lock, self._conn:
                self._save_recipe_impl(
                    sku_id,
                    ingredients=ingredients,
                    yield_qty=yield_qty,
                    yield_estimated=yield_estimated,
                    target_gross_margin_pct=target_gross_margin_pct,
                    prep=prep,
                    updated_by=updated_by,
                    session_id=session_id,
                )

    def _save_recipe_impl(
        self,
        sku_id: str,
        *,
        ingredients: list[tuple[str, Decimal]],
        yield_qty: Decimal,
        yield_estimated: bool,
        target_gross_margin_pct: Decimal | None,
        prep: bool,
        updated_by: str,
        session_id: str | None,
    ) -> None:
        """The write + audit, assuming the lock + transaction are held."""
        target_str = _decimal_or_none_to_str(target_gross_margin_pct)
        yield_str = str(yield_qty)
        old = self._recipe_snapshot(sku_id)
        header = self._conn.execute(
            "SELECT sku_id FROM recipes WHERE sku_id = ?", (sku_id,)
        ).fetchone()
        if header is None:
            sku_row = self._conn.execute(
                "SELECT name, segment FROM skus WHERE sku_id = ?", (sku_id,)
            ).fetchone()
            name, segment = sku_row if sku_row else (sku_id, None)
            self._conn.execute(
                "INSERT INTO recipes"
                " (sku_id, name, segment, yield_qty, yield_estimated,"
                "  target_gross_margin_pct, prep)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sku_id,
                    name,
                    segment or Segment.CAFE.value,
                    yield_str,
                    1 if yield_estimated else 0,
                    target_str,
                    int(prep),
                ),
            )
        else:
            self._conn.execute(
                "UPDATE recipes"
                " SET yield_qty = ?, yield_estimated = ?,"
                "     target_gross_margin_pct = ?, prep = ?"
                " WHERE sku_id = ?",
                (
                    yield_str,
                    1 if yield_estimated else 0,
                    target_str,
                    int(prep),
                    sku_id,
                ),
            )
        self._conn.execute(
            "DELETE FROM recipe_ingredients WHERE sku_id = ?", (sku_id,)
        )
        self._conn.executemany(
            "INSERT INTO recipe_ingredients"
            " (sku_id, ingredient_sku_id, quantity, position)"
            " VALUES (?, ?, ?, ?)",
            [
                (sku_id, ingredient_sku_id, str(quantity), position)
                for position, (ingredient_sku_id, quantity) in enumerate(
                    ingredients
                )
            ],
        )
        new = self._recipe_snapshot(sku_id)
        self._record_audit(
            "recipes",
            sku_id,
            old=old,
            new=new,
            changed_by=updated_by,
            session_id=session_id,
        )

    def delete_recipe(
        self,
        sku_id: str,
        *,
        updated_by: str,
        session_id: str | None = None,
    ) -> None:
        """Remove ``sku_id``'s recipe, flipping the SKU back to purchasable.

        A no-op when the SKU has no recipe. The whole recipe (header +
        ingredient rows) is snapshotted before deletion and the audit entry
        records the removal (``new`` is ``None``), so a revert restores the
        recipe in one stroke — the mirror image of the create-on-first-save
        the recipe editor performs (issue #37, the role-flip demo).

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            self._delete_recipe_impl(
                sku_id, updated_by=updated_by, session_id=session_id
            )
        else:
            with self._lock, self._conn:
                self._delete_recipe_impl(
                    sku_id, updated_by=updated_by, session_id=session_id
                )

    def _delete_recipe_impl(
        self,
        sku_id: str,
        *,
        updated_by: str,
        session_id: str | None,
    ) -> None:
        """The write + audit, assuming the lock + transaction are held."""
        old = self._recipe_snapshot(sku_id)
        if old is None:
            return
        self._apply_snapshot("recipes", sku_id, None)
        self._record_audit(
            "recipes",
            sku_id,
            old=old,
            new=None,
            changed_by=updated_by,
            session_id=session_id,
        )

    def _recipe_snapshot(self, sku_id: str) -> dict[str, Any] | None:
        """One recipe — header plus ordered ingredient rows — as a single dict.

        The recipe is the one config shape spanning two tables; the audit
        log snapshots it as the logical row the partner edits, with the
        ingredient rows inlined as ``[[ingredient_sku_id, quantity], ...]``
        in position order. Callers hold the lock.
        """
        header = self._row_snapshot("recipes", "sku_id", sku_id)
        if header is None:
            return None
        rows = self._conn.execute(
            "SELECT ingredient_sku_id, quantity FROM recipe_ingredients"
            " WHERE sku_id = ? ORDER BY position",
            (sku_id,),
        ).fetchall()
        header["ingredients"] = [list(row) for row in rows]
        return header

    def fixed_costs(self) -> list[FixedCostEntry]:
        """Every stored fixed cost, oldest first (Wave 2 slice 3).

        Returned in the engine's own shape so the period review can consume
        the list directly (``build_period_review(..., fixed_costs=...)``).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, label, category, amount, kind, period, ended_at"
                " FROM fixed_costs ORDER BY id"
            ).fetchall()
        return [
            FixedCostEntry(
                entry_id=entry_id,
                label=label,
                category=category,
                amount=Decimal(amount),
                kind=kind,
                period=_parse_year_month(period),
                ended_at=date.fromisoformat(ended_at) if ended_at else None,
            )
            for entry_id, label, category, amount, kind, period, ended_at in rows
        ]

    def create_fixed_cost(
        self,
        *,
        label: str,
        category: str,
        amount: Decimal,
        kind: str,
        period: tuple[int, int],
        created_by: str,
        session_id: str | None = None,
    ) -> int:
        """Store a new fixed cost (recurring or one-off), audit-logged.

        ``period`` is the ``(year, month)`` a one-off applies to, or the
        first month a recurring row applies from. Returns the new row's id.

        Batch-aware: see :meth:`save_cost`. The returned id is only stable
        once the surrounding batch commits — a rolled-back stroke may have
        already allocated and discarded the rowid.
        """
        if self._in_batch:
            return self._create_fixed_cost_impl(
                label=label,
                category=category,
                amount=amount,
                kind=kind,
                period=period,
                created_by=created_by,
                session_id=session_id,
            )
        with self._lock, self._conn:
            return self._create_fixed_cost_impl(
                label=label,
                category=category,
                amount=amount,
                kind=kind,
                period=period,
                created_by=created_by,
                session_id=session_id,
            )

    def _create_fixed_cost_impl(
        self,
        *,
        label: str,
        category: str,
        amount: Decimal,
        kind: str,
        period: tuple[int, int],
        created_by: str,
        session_id: str | None,
    ) -> int:
        """The write + audit, assuming the lock + transaction are held."""
        cursor = self._conn.execute(
            "INSERT INTO fixed_costs"
            " (label, category, amount, kind, period, created_at, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                label,
                category,
                str(amount),
                kind,
                _format_year_month(period),
                self._now(),
                created_by,
            ),
        )
        entry_id = int(cursor.lastrowid or 0)
        self._record_audit(
            "fixed_costs",
            str(entry_id),
            old=None,
            new=self._row_snapshot("fixed_costs", "id", str(entry_id)),
            changed_by=created_by,
            session_id=session_id,
        )
        return entry_id

    def end_fixed_cost(
        self,
        entry_id: int,
        *,
        ended_on: date,
        updated_by: str,
        session_id: str | None = None,
    ) -> bool:
        """Stop a recurring fixed cost applying after ``ended_on``'s month.

        The month it is ended in still charges in full (ending rent
        mid-month does not un-charge a month already paid); later months
        charge nothing. Audit-logged like every config edit. Returns
        ``False`` for an unknown id.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            return self._end_fixed_cost_impl(
                entry_id,
                ended_on=ended_on,
                updated_by=updated_by,
                session_id=session_id,
            )
        with self._lock, self._conn:
            return self._end_fixed_cost_impl(
                entry_id,
                ended_on=ended_on,
                updated_by=updated_by,
                session_id=session_id,
            )

    def _end_fixed_cost_impl(
        self,
        entry_id: int,
        *,
        ended_on: date,
        updated_by: str,
        session_id: str | None,
    ) -> bool:
        """The write + audit, assuming the lock + transaction are held."""
        old = self._row_snapshot("fixed_costs", "id", str(entry_id))
        if old is None:
            return False
        self._conn.execute(
            "UPDATE fixed_costs SET ended_at = ? WHERE id = ?",
            (ended_on.isoformat(), entry_id),
        )
        self._record_audit(
            "fixed_costs",
            str(entry_id),
            old=old,
            new=self._row_snapshot("fixed_costs", "id", str(entry_id)),
            changed_by=updated_by,
            session_id=session_id,
        )
        return True

    def delete_fixed_cost(
        self,
        entry_id: int,
        *,
        deleted_by: str,
        session_id: str | None = None,
    ) -> bool:
        """Remove a fixed cost entirely, audit-logged (revert restores it).

        Deletion is for rows that should never have applied (a typo, a
        duplicate); a cost that genuinely stopped is *ended*, which keeps
        its history in past months. Returns ``False`` for an unknown id.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            return self._delete_fixed_cost_impl(
                entry_id, deleted_by=deleted_by, session_id=session_id
            )
        with self._lock, self._conn:
            return self._delete_fixed_cost_impl(
                entry_id, deleted_by=deleted_by, session_id=session_id
            )

    def _delete_fixed_cost_impl(
        self,
        entry_id: int,
        *,
        deleted_by: str,
        session_id: str | None,
    ) -> bool:
        """The write + audit, assuming the lock + transaction are held."""
        old = self._row_snapshot("fixed_costs", "id", str(entry_id))
        if old is None:
            return False
        self._conn.execute("DELETE FROM fixed_costs WHERE id = ?", (entry_id,))
        self._record_audit(
            "fixed_costs",
            str(entry_id),
            old=old,
            new=None,
            changed_by=deleted_by,
            session_id=session_id,
        )
        return True

    # --- Suppliers (issue #94) -------------------------------------------------
    #
    # A controlled vendor list the cash-spend entry surface (slice #96) will
    # FK into. Vendors have no lifecycle (no recurring / one-off / ended-at);
    # this is plain CRUD on a controlled list, because free-form lets "Makro"
    # / "Makro Phuket" drift and break per-vendor aggregation (decision 2a of
    # parent #82). Reuses the dormant ``Supplier(supplier_id, name)`` type in
    # place (decision E of #82).
    #
    # Every write goes through the existing ``audit_log`` machinery with
    # ``table_name='suppliers'`` (registered in ``_PK_COLUMNS``), so the
    # existing per-entry / per-session Revert works without any new revert
    # code — the ADR-0003 pattern, unchanged. The FK from ``cash_spend`` lands
    # with #96; until then the route-level ``supplier_in_use`` guard refuses a
    # delete that would break referential integrity once that FK is real.

    def suppliers(self) -> list[Supplier]:
        """Every stored supplier, in ``supplier_id`` order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT supplier_id, name FROM suppliers ORDER BY supplier_id"
            ).fetchall()
        return [Supplier(supplier_id=supplier_id, name=name) for supplier_id, name in rows]

    def get_supplier(self, supplier_id: str) -> Supplier | None:
        """One supplier by id, or ``None`` — the editor route's existence check."""
        with self._lock:
            row = self._conn.execute(
                "SELECT supplier_id, name FROM suppliers WHERE supplier_id = ?",
                (supplier_id,),
            ).fetchone()
        if row is None:
            return None
        found_id, name = row
        return Supplier(supplier_id=found_id, name=name)

    def create_supplier(
        self,
        supplier_id: str,
        *,
        name: str,
        created_by: str,
        session_id: str | None = None,
    ) -> bool:
        """Create a new supplier, audit-logged.

        ``supplier_id`` is the PK the cash-spend rows in #96 will FK to, so
        re-creating one id is refused (returns ``False``) rather than
        clobbering the existing row's name. Nothing is written or audited
        on a refused create.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            return self._create_supplier_impl(
                supplier_id, name=name, created_by=created_by, session_id=session_id
            )
        with self._lock, self._conn:
            return self._create_supplier_impl(
                supplier_id, name=name, created_by=created_by, session_id=session_id
            )

    def _create_supplier_impl(
        self,
        supplier_id: str,
        *,
        name: str,
        created_by: str,
        session_id: str | None,
    ) -> bool:
        """The write + audit, assuming the lock + transaction are held."""
        existing = self._row_snapshot("suppliers", "supplier_id", supplier_id)
        if existing is not None:
            return False
        self._conn.execute(
            "INSERT INTO suppliers (supplier_id, name, created_at, created_by)"
            " VALUES (?, ?, ?, ?)",
            (supplier_id, name, self._now(), created_by),
        )
        self._record_audit(
            "suppliers",
            supplier_id,
            old=None,
            new=self._row_snapshot("suppliers", "supplier_id", supplier_id),
            changed_by=created_by,
            session_id=session_id,
        )
        return True

    def update_supplier(
        self,
        supplier_id: str,
        *,
        name: str,
        updated_by: str,
        session_id: str | None = None,
    ) -> bool:
        """Rename a supplier (the id is immutable — it is the FK target).

        Returns ``False`` for an unknown id; nothing is written or audited
        in that case. Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            return self._update_supplier_impl(
                supplier_id, name=name, updated_by=updated_by, session_id=session_id
            )
        with self._lock, self._conn:
            return self._update_supplier_impl(
                supplier_id, name=name, updated_by=updated_by, session_id=session_id
            )

    def _update_supplier_impl(
        self,
        supplier_id: str,
        *,
        name: str,
        updated_by: str,
        session_id: str | None,
    ) -> bool:
        """The write + audit, assuming the lock + transaction are held."""
        old = self._row_snapshot("suppliers", "supplier_id", supplier_id)
        if old is None:
            return False
        self._conn.execute(
            "UPDATE suppliers SET name = ? WHERE supplier_id = ?",
            (name, supplier_id),
        )
        self._record_audit(
            "suppliers",
            supplier_id,
            old=old,
            new=self._row_snapshot("suppliers", "supplier_id", supplier_id),
            changed_by=updated_by,
            session_id=session_id,
        )
        return True

    def supplier_in_use(self, supplier_id: str) -> bool:
        """Whether any row currently references ``supplier_id``.

        Forward-looking for slice #96: once ``cash_spend`` lands with its FK
        to ``suppliers.supplier_id``, this is the check the delete route
        runs to refuse a delete that would break referential integrity.
        Today the table is empty (or absent), so this answers False; the
        guard ships now so it is in place the moment the FK is real.
        """
        with self._lock:
            if not self._conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name = 'cash_spend'"
            ).fetchone():
                return False
            row = self._conn.execute(
                "SELECT 1 FROM cash_spend WHERE supplier_id = ? LIMIT 1",
                (supplier_id,),
            ).fetchone()
        return row is not None

    def delete_supplier(
        self,
        supplier_id: str,
        *,
        deleted_by: str,
        session_id: str | None = None,
    ) -> bool:
        """Hard-delete a supplier, audit-logged (revert restores it).

        Refuses (returns ``False``) when the supplier does not exist or when
        :meth:`supplier_in_use` reports a referencing row — the partner gets
        a clear message rather than a 500 from a future FK violation. The
        FK constraint itself lands with slice #96; the guard is correct
        today against the empty table and stands ready.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            return self._delete_supplier_impl(
                supplier_id, deleted_by=deleted_by, session_id=session_id
            )
        with self._lock, self._conn:
            return self._delete_supplier_impl(
                supplier_id, deleted_by=deleted_by, session_id=session_id
            )

    def _delete_supplier_impl(
        self,
        supplier_id: str,
        *,
        deleted_by: str,
        session_id: str | None,
    ) -> bool:
        """The write + audit, assuming the lock + transaction are held."""
        old = self._row_snapshot("suppliers", "supplier_id", supplier_id)
        if old is None:
            return False
        # Re-check in-use inside the held transaction so a concurrent
        # cash-spend write cannot slip in between the route's check and the
        # delete. ``supplier_in_use`` takes the lock itself; we are already
        # inside ``self._lock`` (an RLock), so this re-enters cleanly.
        if self.supplier_in_use(supplier_id):
            return False
        self._conn.execute(
            "DELETE FROM suppliers WHERE supplier_id = ?", (supplier_id,)
        )
        self._record_audit(
            "suppliers",
            supplier_id,
            old=old,
            new=None,
            changed_by=deleted_by,
            session_id=session_id,
        )
        return True

    # --- Cash spend (issue #96, parent #82) -----------------------------------
    #
    # The row that produces the HTML's "Cost of goods — purchases (cash)"
    # line and the per-bucket breakdown. A row is one bucket's slice of a
    # vendor bill on a date (decision A of #82); a multi-bucket bill is N
    # sibling rows sharing date + supplier, differing bucket + amount, no
    # parent. The invoice total is the derived fact SUM(amount) WHERE
    # date+supplier — never stored on a parent.
    #
    # Every write goes through the existing ``audit_log`` machinery with
    # ``table_name='cash_spend'`` (registered in ``_PK_COLUMNS``), so the
    # existing per-entry / per-session Revert works without new revert
    # code — the ADR-0003 pattern, unchanged. The route-level
    # referential-integrity checks (#94's ``supplier_in_use``, #95's
    # ``spend_bucket_in_use``) already query this table; the migration's
    # REFERENCES clauses match the 0002 convention (declarative; PRAGMA
    # foreign_keys stays at its default).

    def cash_spend_rows(self) -> list[CashSpendEntry]:
        """Every cash-spend row, oldest first (issue #96).

        Returned in the engine's own ``CashSpendEntry`` shape so the
        reporting surface / P&L view can feed the list straight to
        :func:`tangerine.cash_spend.cash_spend_for_period`. ``amount`` is
        the THB amount as paid (gross when ``vat_inclusive``); the
        aggregation layer divides by 1.07 only when the flag is set — the
        same rule ADR-0003 decision 4 applied to the cost book, applied
        here at aggregation time so the raw invoice total still
        reconstructs from ``SUM(amount)``.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, date, supplier_id, description, bucket_id,"
                " amount, vat_inclusive"
                " FROM cash_spend ORDER BY id"
            ).fetchall()
        return [
            CashSpendEntry(
                row_id=row_id,
                date=date.fromisoformat(day),
                supplier_id=supplier_id,
                description=description,
                bucket_id=bucket_id,
                amount=Decimal(amount),
                vat_inclusive=bool(vat_inclusive),
            )
            for row_id, day, supplier_id, description, bucket_id, amount, vat_inclusive in rows
        ]

    def create_cash_spend(
        self,
        entry: CashSpendEntry,
        *,
        created_by: str,
        session_id: str | None = None,
    ) -> int:
        """Store a new cash-spend row, audit-logged.

        ``entry.row_id`` is ignored — the table auto-assigns the id and
        the stored row gets it. Returns the new row's id. The id is only
        stable once the surrounding batch commits (a rolled-back stroke
        may have already allocated and discarded the rowid).

        A multi-bucket bill is N independent ``create_cash_spend`` calls
        (optionally wrapped in a :meth:`batch` for atomicity); the storage
        shape has no parent row.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            return self._create_cash_spend_impl(
                entry, created_by=created_by, session_id=session_id
            )
        with self._lock, self._conn:
            return self._create_cash_spend_impl(
                entry, created_by=created_by, session_id=session_id
            )

    def _create_cash_spend_impl(
        self,
        entry: CashSpendEntry,
        *,
        created_by: str,
        session_id: str | None,
    ) -> int:
        """The write + audit, assuming the lock + transaction are held."""
        cursor = self._conn.execute(
            "INSERT INTO cash_spend"
            " (date, supplier_id, description, bucket_id, amount,"
            "  vat_inclusive, created_at, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.date.isoformat(),
                entry.supplier_id,
                entry.description,
                entry.bucket_id,
                str(entry.amount),
                1 if entry.vat_inclusive else 0,
                self._now(),
                created_by,
            ),
        )
        row_id = int(cursor.lastrowid or 0)
        self._record_audit(
            "cash_spend",
            str(row_id),
            old=None,
            new=self._row_snapshot("cash_spend", "id", str(row_id)),
            changed_by=created_by,
            session_id=session_id,
        )
        return row_id

    def update_cash_spend(
        self,
        entry: CashSpendEntry,
        *,
        updated_by: str,
        session_id: str | None = None,
    ) -> bool:
        """Replace a cash-spend row with ``entry``, audit-logged.

        ``entry.row_id`` identifies the row to replace; every other field
        is written from ``entry`` (the edit form always posts the whole
        row). Returns ``False`` for an unknown id; nothing is written or
        audited in that case. Reverts of the resulting audit entry restore
        only the fields the edit moved (the amount, typically), leaving a
        later edit to a different field intact — the surgical-revert rule.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            return self._update_cash_spend_impl(
                entry, updated_by=updated_by, session_id=session_id
            )
        with self._lock, self._conn:
            return self._update_cash_spend_impl(
                entry, updated_by=updated_by, session_id=session_id
            )

    def _update_cash_spend_impl(
        self,
        entry: CashSpendEntry,
        *,
        updated_by: str,
        session_id: str | None,
    ) -> bool:
        """The write + audit, assuming the lock + transaction are held."""
        row_id = str(entry.row_id)
        old = self._row_snapshot("cash_spend", "id", row_id)
        if old is None:
            return False
        self._conn.execute(
            "UPDATE cash_spend"
            " SET date = ?, supplier_id = ?, description = ?,"
            "     bucket_id = ?, amount = ?, vat_inclusive = ?"
            " WHERE id = ?",
            (
                entry.date.isoformat(),
                entry.supplier_id,
                entry.description,
                entry.bucket_id,
                str(entry.amount),
                1 if entry.vat_inclusive else 0,
                entry.row_id,
            ),
        )
        self._record_audit(
            "cash_spend",
            row_id,
            old=old,
            new=self._row_snapshot("cash_spend", "id", row_id),
            changed_by=updated_by,
            session_id=session_id,
        )
        return True

    def delete_cash_spend(
        self,
        row_id: int,
        *,
        deleted_by: str,
        session_id: str | None = None,
    ) -> bool:
        """Remove a cash-spend row entirely, audit-logged (revert restores).

        Deletion is for rows that should never have existed (a typo, a
        duplicate). Returns ``False`` for an unknown id. Batch-aware: see
        :meth:`save_cost`.
        """
        if self._in_batch:
            return self._delete_cash_spend_impl(
                row_id, deleted_by=deleted_by, session_id=session_id
            )
        with self._lock, self._conn:
            return self._delete_cash_spend_impl(
                row_id, deleted_by=deleted_by, session_id=session_id
            )

    def _delete_cash_spend_impl(
        self,
        row_id: int,
        *,
        deleted_by: str,
        session_id: str | None,
    ) -> bool:
        """The write + audit, assuming the lock + transaction are held."""
        pk = str(row_id)
        old = self._row_snapshot("cash_spend", "id", pk)
        if old is None:
            return False
        self._conn.execute("DELETE FROM cash_spend WHERE id = ?", (row_id,))
        self._record_audit(
            "cash_spend",
            pk,
            old=old,
            new=None,
            changed_by=deleted_by,
            session_id=session_id,
        )
        return True

    # --- Loyverse cost-mirror paper trail (issue #102, parent spec #100) ------
    #
    # The dedicated ``loyverse_exports`` table — every confirmed cost-mirror
    # export leaves one row here. Deliberately NOT routed through
    # ``_record_audit``: ``audit_log`` feeds ``unreviewed_changes`` and the
    # 9am "N config changes since last review" count, and a Loyverse-bound
    # export is a mirror action, not a config edit — including it would
    # pollute that count (the Q5 dedicated-vs-audit-log decision, issue #70
    # resolution / spec #100). Writes go straight to the dedicated table; the
    # read side (``loyverse_exports()``) is what slice 3's drift badge and any
    # future export-history surface consume.
    #
    # ``confirmed_at`` is stamped by the store's injectable clock (the same
    # ``now=`` seam ``cash_spend`` and every other audited write uses), so
    # tests pin the timestamp and the route never passes a wall-clock value.

    def loyverse_exports(self) -> list[LoyverseExport]:
        """Every confirmed Loyverse cost-mirror export, newest-first.

        Newest-first (highest id first) is the read order the drift badge
        (slice 3, issue #103) wants: "when was the most recent confirm?" is
        the first row. Empty before any confirm has happened — the null state
        slice 3 hides behind (no "stale since forever" message).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, partner_id, confirmed_at, item_count,"
                " changed_count, drift_payload"
                " FROM loyverse_exports ORDER BY id DESC"
            ).fetchall()
        return [
            LoyverseExport(
                id=row_id,
                partner_id=partner_id,
                confirmed_at=confirmed_at,
                item_count=item_count,
                changed_count=changed_count,
                drift_payload=drift_payload,
            )
            for (
                row_id,
                partner_id,
                confirmed_at,
                item_count,
                changed_count,
                drift_payload,
            ) in rows
        ]

    def record_loyverse_export(
        self,
        *,
        partner_id: str,
        item_count: int,
        changed_count: int,
        drift_payload: str,
    ) -> int:
        """Record one confirmed Loyverse cost-mirror export, return its id.

        Writes a single row to the dedicated ``loyverse_exports`` table.
        ``confirmed_at`` is stamped by the store's injectable clock — the
        caller does not pass a timestamp, mirroring ``cash_spend`` and every
        other audited write so tests pin the moment via ``now=``.

        This is **not** an audited write: it bypasses ``_record_audit`` and
        lands directly on the dedicated table, so ``unreviewed_changes`` and
        the 9am config-changes count are unaffected by construction. A zero-
        drift confirm still calls this (``changed_count = 0``,
        ``drift_payload = "[]"``) — PRD user story 9: the null-state proof
        is visible, not inferred from absence.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO loyverse_exports"
                " (partner_id, confirmed_at, item_count, changed_count,"
                "  drift_payload)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    partner_id,
                    self._now(),
                    item_count,
                    changed_count,
                    drift_payload,
                ),
            )
            return int(cursor.lastrowid or 0)

    def skus(self) -> list[SkuRecord]:
        """Every row in the ``skus`` table, in ``sku_id`` order.

        The SKU + item coverage views' (Wave 1.5, Slice 2) whole-table read:
        unlike ``recipes()``, this includes cost-only leaf SKUs that never
        produce a recipe of their own (e.g. ``almond-ground``), so the SKU
        view can show every SKU that has ever been seeded or edited.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT sku_id, name, segment, unit FROM skus ORDER BY sku_id"
            ).fetchall()
        return [
            SkuRecord(
                sku_id=sku_id,
                name=name,
                segment=Segment(segment) if segment else None,
                unit=unit,
            )
            for sku_id, name, segment, unit in rows
        ]

    def spend_buckets(self) -> list[SpendBucket]:
        """Every spend bucket, in seed-then-creation order (issue #95).

        The seeded six sort first in their HTML display order, then any
        partner-added buckets in creation order. The admin page renders
        this list verbatim — retired buckets stay visible (struck-through)
        so a partner reading the page keeps the historical context that
        ``retired_at`` preserves. Slice #96's new-entry picker filters to
        ``retired_at is None`` rows only.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT bucket_id, name, retired_at, created_at, created_by"
                " FROM spend_buckets ORDER BY"
                "  CASE bucket_id"
                "    WHEN 'taps' THEN 0"
                "    WHEN 'kitchen' THEN 1"
                "    WHEN 'coffee' THEN 2"
                "    WHEN 'bakery' THEN 3"
                "    WHEN 'staff' THEN 4"
                "    WHEN 'rent' THEN 5"
                "    ELSE 6"
                "  END,"
                "  created_at, bucket_id"
            ).fetchall()
        return [
            SpendBucket(
                bucket_id=bucket_id,
                name=name,
                retired_at=retired_at,
                created_at=created_at,
                created_by=created_by,
            )
            for bucket_id, name, retired_at, created_at, created_by in rows
        ]

    def create_spend_bucket(
        self,
        bucket_id: str,
        *,
        name: str,
        created_by: str,
        session_id: str | None = None,
    ) -> None:
        """Add a new spend bucket to the vocabulary, audit-logged (issue #95).

        ``bucket_id`` is the stable slug a partner types (and what slice
        #96's cash-spend rows will FK to); ``name`` is the display label.
        A duplicate ``bucket_id`` raises :class:`sqlite3.IntegrityError` —
        the route catches it and explains, because the controlled
        vocabulary's whole point is one canonical id per bucket.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            self._create_spend_bucket_impl(
                bucket_id, name=name, created_by=created_by, session_id=session_id
            )
        else:
            with self._lock, self._conn:
                self._create_spend_bucket_impl(
                    bucket_id, name=name, created_by=created_by, session_id=session_id
                )

    def _create_spend_bucket_impl(
        self,
        bucket_id: str,
        *,
        name: str,
        created_by: str,
        session_id: str | None,
    ) -> None:
        """The write + audit, assuming the lock + transaction are held."""
        self._conn.execute(
            "INSERT INTO spend_buckets"
            " (bucket_id, name, retired_at, created_at, created_by)"
            " VALUES (?, ?, NULL, ?, ?)",
            (bucket_id, name, self._now(), created_by),
        )
        self._record_audit(
            "spend_buckets",
            bucket_id,
            old=None,
            new=self._row_snapshot("spend_buckets", "bucket_id", bucket_id),
            changed_by=created_by,
            session_id=session_id,
        )

    def retire_spend_bucket(
        self,
        bucket_id: str,
        *,
        retired_at: str,
        updated_by: str,
        session_id: str | None = None,
    ) -> bool:
        """Soft-retire a bucket (issue #95); it stays in the table, flagged.

        Mirrors fixed-costs' ending-vs-deleting distinction (ADR-0004
        decision 3): a retired bucket stays in the table so historical
        cash-spend rows keep aggregating under it, but slice #96's
        new-entry picker excludes it. Retiring is the partner action for
        "we no longer use this bucket"; hard-delete is for typos. Returns
        ``False`` for an unknown id.

        ``retired_at`` is a partner-facing date string (the route passes
        ``app.state.today.isoformat()``); retiring is dated the day the
        partner retires it, exactly like ending a fixed cost.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            return self._retire_spend_bucket_impl(
                bucket_id,
                retired_at=retired_at,
                updated_by=updated_by,
                session_id=session_id,
            )
        with self._lock, self._conn:
            return self._retire_spend_bucket_impl(
                bucket_id,
                retired_at=retired_at,
                updated_by=updated_by,
                session_id=session_id,
            )

    def _retire_spend_bucket_impl(
        self,
        bucket_id: str,
        *,
        retired_at: str,
        updated_by: str,
        session_id: str | None,
    ) -> bool:
        """The write + audit, assuming the lock + transaction are held."""
        old = self._row_snapshot("spend_buckets", "bucket_id", bucket_id)
        if old is None:
            return False
        self._conn.execute(
            "UPDATE spend_buckets SET retired_at = ? WHERE bucket_id = ?",
            (retired_at, bucket_id),
        )
        self._record_audit(
            "spend_buckets",
            bucket_id,
            old=old,
            new=self._row_snapshot("spend_buckets", "bucket_id", bucket_id),
            changed_by=updated_by,
            session_id=session_id,
        )
        return True

    def delete_spend_bucket(
        self,
        bucket_id: str,
        *,
        deleted_by: str,
        session_id: str | None = None,
    ) -> bool:
        """Hard-delete a bucket (issue #95); audit-logged, revert restores.

        Deletion is for buckets that should never have existed (a typo, a
        duplicate); a bucket a partner merely stopped using is *retired*,
        which keeps its history. The route guards on
        :meth:`spend_bucket_in_use` before calling this so a bucket with
        referencing rows is never deleted; the FK constraint itself lands
        with slice #96. Returns ``False`` for an unknown id.

        Batch-aware: see :meth:`save_cost`.
        """
        if self._in_batch:
            return self._delete_spend_bucket_impl(
                bucket_id, deleted_by=deleted_by, session_id=session_id
            )
        with self._lock, self._conn:
            return self._delete_spend_bucket_impl(
                bucket_id, deleted_by=deleted_by, session_id=session_id
            )

    def _delete_spend_bucket_impl(
        self,
        bucket_id: str,
        *,
        deleted_by: str,
        session_id: str | None,
    ) -> bool:
        """The write + audit, assuming the lock + transaction are held."""
        old = self._row_snapshot("spend_buckets", "bucket_id", bucket_id)
        if old is None:
            return False
        self._conn.execute(
            "DELETE FROM spend_buckets WHERE bucket_id = ?", (bucket_id,)
        )
        self._record_audit(
            "spend_buckets",
            bucket_id,
            old=old,
            new=None,
            changed_by=deleted_by,
            session_id=session_id,
        )
        return True

    def spend_bucket_in_use(self, bucket_id: str) -> bool:
        """Whether any cash-spend row currently references ``bucket_id``.

        Slice #96 lands the ``cash_spend`` table and its FK to
        ``spend_buckets.bucket_id``; until then this returns ``False``
        (no referencing table exists). The route-level guard calls this
        before a hard-delete so the surface is honest from day one — the
        FK constraint that enforces it in the DB lands with #96, at which
        point this query starts returning ``True`` against real rows.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name = 'cash_spend'"
            ).fetchone()
            if row is None:
                return False
            count = self._conn.execute(
                "SELECT COUNT(*) FROM cash_spend WHERE bucket_id = ?",  # noqa: S608
                (bucket_id,),
            ).fetchone()
        return bool(count and count[0] > 0)


def _revert_target(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """What the row should look like after undoing one entry's change.

    "Exactly that one change" means: only the fields whose values the entry
    moved go back to their before-values; everything else keeps its
    *current* value, so a later edit to a different field of the same row
    survives the revert. Three whole-row cases fall out first:

    - The entry created the row (``old is None``): undo deletes it — later
      edits to the row necessarily die with it.
    - The entry deleted the row (``new is None``): undo restores the
      before-state whole.
    - The row is gone now (``current is None``): there is nothing to merge
      into, so the before-state comes back whole.
    """
    if old is None:
        return None
    if new is None or current is None:
        return dict(old)
    target = dict(current)
    for field in set(old) | set(new):
        if old.get(field) != new.get(field):
            target[field] = old.get(field)
    return target


def seed_config(
    conn: sqlite3.Connection,
    *,
    recipes_path: str | Path,
    costs_path: str | Path | None = None,
) -> None:
    """Seed the config tables from YAML on first run.

    Idempotent: a no-op when the ``skus`` table already has rows, so calling
    this on every startup is safe (and what ``create_app`` does). The seeder
    applies migrations first so a freshly-created database is immediately
    seedable.

    Costs are seeded only when ``costs_path`` is provided; tests that exercise
    recipes alone can omit it. ``create_app`` always provides both.

    The seeding writes commit as one transaction (``with conn:``) so they are
    durable and release the write lock immediately — without this, sqlite3's
    default deferred-transaction behaviour leaves the insert transaction open
    until some *other* call happens to commit, holding a write lock that
    blocks any other connection to the same file (surfaces as
    ``sqlite3.OperationalError: database is locked``).

    The estimated-yield backfill (issue #34) runs on *every* call, not only
    the first-seed path: an existing partner database predates the yield
    concept, so its preps still carry the legacy default of 1 until this
    flips them. The backfill is idempotent — it only touches a prep whose
    yield is still the untouched legacy default — so re-running it every
    startup neither churns data nor clobbers a partner-measured yield. It
    runs after seeding so ingredient units (seeded from the cost comments)
    are known, which is what lets it exclude count-unit inputs from the sum.

    The spend-bucket seed (issue #95) gates on its *own* table being empty,
    not on ``skus``: a partner upgrading an existing production DB (skus
    already populated) must still get the six HTML buckets on the next boot.
    Once a partner has added, renamed, or retired a bucket the seed is a
    no-op — never clobber a partner's edits.
    """
    apply_migrations(conn)
    if not _skus_table_has_rows(conn):
        catalog = load_recipes(recipes_path)
        now = _utc_now_iso()
        with conn:
            _seed_recipes(conn, catalog, now)
            _seed_mappings(conn, catalog, now)
            if costs_path is not None:
                _seed_costs(conn, costs_path, now)
    _seed_spend_buckets_if_empty(conn)
    _backfill_estimated_yields(conn)


def _seed_spend_buckets_if_empty(conn: sqlite3.Connection) -> None:
    """Land the HTML's six spend buckets on first boot against an empty table.

    The seed-on-empty analogue of ADR-0003 decision 1's cost/recipe seeder,
    applied to a vocabulary table instead of a YAML-backed one (issue #95).
    The six are the HTML cost-breakdown column's known set — the seed is
    non-empty where ADR-0009's cafe-category set is empty by default,
    because here the default is known.

    Once the table holds any row — a seeded bucket, a partner's addition,
    a partner's rename — this is a no-op. A partner's edits, additions, and
    retirements are never clobbered by re-running the seeder on boot. The
    seed writes commit as one transaction so the six land together or not
    at all; no audit row is written (the seed is a migration, attributed
    to ``_MIGRATION_ACTOR`` in the row's ``created_by``).
    """
    row = conn.execute("SELECT COUNT(*) FROM spend_buckets").fetchone()
    if row and row[0] > 0:
        return
    now = _utc_now_iso()
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO spend_buckets"
            " (bucket_id, name, retired_at, created_at, created_by)"
            " VALUES (?, ?, NULL, ?, ?)",
            [
                (bucket_id, name, now, _MIGRATION_ACTOR)
                for bucket_id, name in _SEEDED_SPEND_BUCKETS
            ],
        )


def _seed_recipes(conn: sqlite3.Connection, catalog: RecipeCatalog, now: str) -> None:
    """Write every recipe (header + ingredient rows) and its producing SKU.

    The SKU row carries name + segment; the unit column is left NULL here
    (ADR-0003 decision 3 — best-effort derivation happens in ``_seed_costs``
    where the pack-size comment lives; ambiguous cases stay NULL for the
    Slice 3 editor to confirm). yield_qty and target margin live on both
    the SKU (for the editor's eventual form) and the recipe (for the engine);
    the SKU's copy is left NULL where the recipe carries the default of 1.

    Issue #34: the estimated-yield backfill (a sub-recipe consumed as an
    ingredient gets an estimated yield from its input sum) is *not* run here.
    It runs from :func:`seed_config` after ``_seed_costs`` — both so it covers
    existing databases on upgrade, not just first seed, and so ingredient
    units (derived from the cost comments) are already known when it decides
    which inputs to sum. Dishes keep their yield marked measured (fixed).

    Issue #35: ``prep`` is derived from usage, not from a YAML field: a
    recipe whose output SKU is referenced as an ingredient by any other
    recipe is flagged prep on seed. That derivation needs every recipe's
    ingredient rows written first, so the seed is three passes — headers,
    ingredient rows, then an ``UPDATE`` from a usage query — rather than
    interleaving prep with the header write.
    """
    for recipe in catalog.all():
        target_str = _decimal_or_none_to_str(recipe.target_gross_margin_pct)
        # Migrated yield: write the value as-is, but defer yield_estimated to
        # the usage-based backfill below. The engine divides by this number
        # on read, so it must always be set (the loader defaults to 1).
        yield_str = str(recipe.yield_qty)
        conn.execute(
            "INSERT OR IGNORE INTO skus"
            " (sku_id, name, segment, unit, yield_qty,"
            "  yield_estimated, target_gross_margin_pct,"
            "  created_at, created_by)"
            " VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, ?)",
            (
                recipe.sku_id,
                recipe.name,
                recipe.segment.value,
                # The SKU's yield columns stay NULL where the recipe carries
                # the default of 1 — same inheritance pattern the old
                # yield_units column used.
                None if recipe.yield_qty == Decimal("1") else yield_str,
                target_str,
                now,
                _MIGRATION_ACTOR,
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO recipes"
            " (sku_id, name, segment, yield_qty, yield_estimated,"
            "  target_gross_margin_pct, prep)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                recipe.sku_id,
                recipe.name,
                recipe.segment.value,
                yield_str,
                1 if recipe.yield_estimated else 0,
                target_str,
                # The catalog flag is honoured when set, then the usage
                # derivation below ors in any prep declared only by usage.
                1 if recipe.prep else 0,
            ),
        )
        for position, ing in enumerate(recipe.ingredients):
            conn.execute(
                "INSERT INTO recipe_ingredients"
                " (sku_id, ingredient_sku_id, quantity, position)"
                " VALUES (?, ?, ?, ?)",
                (recipe.sku_id, ing.sku_id, str(ing.quantity), position),
            )

    # Issue #35: usage is the declaration. A recipe whose output SKU another
    # recipe consumes is a prep regardless of what the YAML said — this is
    # what makes the prep- naming convention stop carrying meaning. Mirrors
    # the backfill in 0007_sku_roles.sql for databases seeded before the
    # column existed; running both is idempotent (UPDATE sets the same bit).
    conn.execute(
        "UPDATE recipes SET prep = 1"
        " WHERE sku_id IN ("
        "   SELECT DISTINCT ri.ingredient_sku_id"
        "     FROM recipe_ingredients AS ri"
        "     JOIN recipes AS r ON r.sku_id = ri.ingredient_sku_id"
        " )"
    )


def _backfill_estimated_yields(conn: sqlite3.Connection) -> None:
    """Issue #34: sub-recipes used as ingredients get estimated yields.

    A recipe whose output SKU is referenced as an ingredient by another
    recipe is a prep; its yield defaults to the no-loss estimate (the sum
    of its weight/volume input quantities) and is marked estimated. The
    partner can replace the estimate with a measured value after weighing
    a batch.

    The estimate uses the input sum because that is the cheapest upper
    bound available without weighing: evaporation means a reduced sauce's
    true yield is *lower* (so its true cost-per-gram is higher) — the
    estimate is labelled to make that caveat visible (CONTEXT.md "Yield").

    Idempotent and safe to run on every startup (``seed_config`` does):
    a prep is only (re)derived while its yield is the *untouched legacy
    default* — measured (``yield_estimated = 0``) with ``yield_qty = 1``,
    the shape both a first seed and an on-disk 0006 migration leave. Once
    flipped to an estimate, or once a partner types a measured value, the
    stored yield is left alone. This is what carries existing partner
    databases — which predate the yield concept and still hold the legacy
    default — over on upgrade, not just fresh seeds.
    """
    used_as_ingredient = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT ri.ingredient_sku_id"
            "  FROM recipe_ingredients AS ri"
            "  JOIN recipes AS r ON r.sku_id = ri.ingredient_sku_id"
        ).fetchall()
    }
    unit_by_sku = {
        sku_id: unit
        for sku_id, unit in conn.execute("SELECT sku_id, unit FROM skus").fetchall()
    }
    with conn:
        for sku_id, yield_qty, yield_estimated in conn.execute(
            "SELECT sku_id, yield_qty, yield_estimated FROM recipes"
        ).fetchall():
            if sku_id not in used_as_ingredient:
                continue
            # Only touch the untouched legacy default: a partner-measured
            # yield (estimated 0, value != 1) or an already-derived estimate
            # (estimated 1) is left exactly as stored.
            if not (yield_estimated == 0 and Decimal(yield_qty) == Decimal("1")):
                continue
            rows = conn.execute(
                "SELECT ingredient_sku_id, quantity FROM recipe_ingredients"
                " WHERE sku_id = ?",
                (sku_id,),
            ).fetchall()
            input_sum = estimated_yield(
                [(ing_sku, Decimal(qty)) for ing_sku, qty in rows],
                unit_by_sku,
            )
            # Zero inputs would mean the prep has no weighable rows yet —
            # leave its yield alone rather than divide by zero in the engine.
            if input_sum <= 0:
                continue
            conn.execute(
                "UPDATE recipes"
                " SET yield_qty = ?, yield_estimated = 1"
                " WHERE sku_id = ?",
                (str(input_sum), sku_id),
            )


def _seed_mappings(conn: sqlite3.Connection, catalog: RecipeCatalog, now: str) -> None:
    """Write every Loyverse-item -> SKU mapping the loaded catalog carries.

    The loader already validated that every mapping's ``sku_id`` references a
    real recipe (``_validate_mappings_target_real_recipes``), so this is a
    straight write — no FK violation is possible for a file that passed
    ``load_recipes``.
    """
    conn.executemany(
        "INSERT OR REPLACE INTO mappings (item_id, sku_id, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?)",
        [(m.item_id, m.sku_id, now, _MIGRATION_ACTOR) for m in catalog.mappings()],
    )


def _seed_costs(
    conn: sqlite3.Connection, costs_path: str | Path, now: str
) -> None:
    """Write every SKU's net per-unit cost.

    Reads the raw YAML text *and* the parsed structure. The parsed dict gives
    the per-SKU ``price`` / ``updated_at`` (and drives ``load_costs``'s
    validation, which runs first so a malformed file still raises
    ``ConfigError``). The raw text is walked line-by-line to extract the
    trailing per-line comments — ``yaml.safe_load`` drops comments, but the
    file holds the supplier / pack-size provenance there that slice 3 needs
    for VAT detection (ADR-0003 decision 4).
    """
    # Run the validator first; on a bad file it raises ConfigError before we
    # touch the DB. We don't use the returned CostBook for iteration (it has
    # no list-all accessor) but constructing it validates the file end-to-end.
    load_costs(costs_path)

    text = Path(costs_path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return  # load_costs would have raised; defensive only.
    raw_costs = data.get("costs", {})
    if not isinstance(raw_costs, dict):
        return

    comments = _cost_comments_by_sku(text)
    rows: list[tuple[str, int, str, str, str]] = []
    for sku_id, entry in raw_costs.items():
        if not (isinstance(entry, dict) and "price" in entry and "updated_at" in entry):
            continue
        comment = comments.get(sku_id)
        vat_inclusive = _looks_vat_inclusive(comment)
        net = _net_per_unit(_parse_decimal(entry["price"]), vat_inclusive)
        rows.append(
            (sku_id, 1 if vat_inclusive else 0, str(net), entry["updated_at"], _MIGRATION_ACTOR)
        )
        unit_comment = _resolve_unit_comment(sku_id, comments)
        _ensure_sku_row(conn, sku_id, unit=_derive_unit(unit_comment), now=now)
    conn.executemany(
        "INSERT OR REPLACE INTO costs"
        " (sku_id, pack_price, pack_quantity, vat_inclusive,"
        "  price_per_unit_net, updated_at, updated_by)"
        " VALUES (?, NULL, NULL, ?, ?, ?, ?)",
        rows,
    )


def _ensure_sku_row(
    conn: sqlite3.Connection, sku_id: str, *, unit: str | None, now: str
) -> None:
    """Make sure ``sku_id`` has a ``skus`` row, backfilling ``unit`` if known.

    Most costed SKUs are pure ingredients (``almond-ground``, ``butter``, ...)
    that are never a recipe's own ``sku_id`` and so never get a row from
    ``_seed_recipes`` — this creates one, with ``segment`` left NULL (an
    ingredient may feed both cafe and bar recipes, so it has no single
    segment). For a SKU that *is* also a recipe output (a costed sub-recipe
    like a batch-brewed concentrate), a row already exists with its segment
    and name set; this only fills in ``unit`` when it is still unknown,
    never overwriting a row's ``name``/``segment``.
    """
    conn.execute(
        "INSERT INTO skus (sku_id, name, segment, unit, yield_qty,"
        " yield_estimated, target_gross_margin_pct, created_at, created_by)"
        " VALUES (?, ?, NULL, ?, NULL, NULL, NULL, ?, ?)"
        " ON CONFLICT(sku_id) DO UPDATE SET unit = COALESCE(skus.unit, excluded.unit)",
        (sku_id, sku_id, unit, now, _MIGRATION_ACTOR),
    )


# Matches a pack-size token immediately preceded by either a number (optionally
# with a decimal point and/or a space -- "500 g", "5kg", "1.6 l") or a literal
# "/" (the price-per-kilo shorthand used throughout costs.yaml for market
# meat/fish, e.g. "79/kg", "645/kg"). The trailing word boundary avoids
# matching inside longer words (the "g" in "gross", "pc" in "packs").
_WEIGHT_RE = re.compile(r"(?:\d+(?:\.\d+)?\s*|/)k?g\b", re.IGNORECASE)
_VOLUME_RE = re.compile(r"(?:\d+(?:\.\d+)?\s*|/)(?:ml|l)\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"\bpcs?\b", re.IGNORECASE)

# Matches an alias comment such as "= corn" (see ``_resolve_unit_comment``).
_ALIAS_RE = re.compile(r"^=\s*(\S+)$")


def _resolve_unit_comment(sku_id: str, comments: dict[str, str]) -> str | None:
    """Follow a ``# = other_sku`` alias comment to the unit-bearing comment.

    Some cost entries are priced identically to another SKU and say so with
    an alias comment instead of repeating the pack-size text (e.g.
    ``corn-grilled: {...}  # = corn``). Read literally, ``_derive_unit`` would
    see ``"= corn"`` — no weight/volume/count token — and give up as
    ambiguous, even though the aliased SKU (``corn``) already carries a known
    unit. This follows the alias chain to that SKU's own comment so its unit
    derives correctly too. Cycle-guarded against a malformed alias loop.
    """
    seen: set[str] = set()
    current = sku_id
    while current not in seen:
        seen.add(current)
        comment = comments.get(current)
        if comment is None:
            return None
        match = _ALIAS_RE.match(comment)
        if not match:
            return comment
        current = match.group(1)
    return None


def _derive_unit(comment: str | None) -> str | None:
    """Best-effort unit from a cost line's trailing pack-size comment.

    Per ADR-0003 decision 3: a weight token (``g``/``kg``) means the SKU's
    canonical unit is ``g``; a volume token (``ml``/``l``) means ``ml``; a
    count token (``pc``/``pcs``) means ``unit``. A comment naming more than
    one kind of token, or none at all (e.g. ``"120/30"`` for eggs, or no pack
    size at all), is genuinely ambiguous from the text alone — this returns
    ``None`` rather than guess, leaving the row queryable (``unit IS NULL``)
    for partner confirmation later, exactly like the VAT flag's conservative
    default in ``_looks_vat_inclusive``.
    """
    if not comment:
        return None
    kinds = {
        unit
        for pattern, unit in (
            (_WEIGHT_RE, "g"),
            (_VOLUME_RE, "ml"),
            (_COUNT_RE, "unit"),
        )
        if pattern.search(comment)
    }
    if len(kinds) != 1:
        return None
    return next(iter(kinds))


def net_price_per_unit(
    pack_price: Decimal, pack_quantity: Decimal, vat_inclusive: bool
) -> Decimal:
    """Derive the stored net per-unit price from receipt-shaped inputs.

    The single place the cost editor's arithmetic lives (ADR-0003 decision 4):
    ``pack_price / pack_quantity``, then ``/ 1.07`` when the purchase was
    VAT-inclusive. E.g. a 380 THB Makro receipt for a 2 kg block of butter →
    ``380 / 2000 / 1.07 = 0.177570 THB/g`` net. Quantised to 6 decimal
    places (matching the migrated rows' precision) so the same inputs always
    derive the same stored value.
    """
    per_unit = pack_price / pack_quantity
    if vat_inclusive:
        per_unit = per_unit / Decimal("1.07")
    return per_unit.quantize(Decimal("0.000001"))


def _net_per_unit(gross: Decimal, vat_inclusive: bool) -> Decimal:
    """Gross-input / net-stored: divide by 1.07 when the purchase was VAT-inclusive.

    Per ADR-0003 decision 4: today's ``costs.yaml`` stores gross (VAT-inclusive)
    prices with a comment saying "divide by 1.07 for net" that the engine never
    executes — so every margin the shipped tool has produced is understated by
    ~7% of COGS on VAT-inclusive inputs. The migrator performs that division
    once, on seed, so the engine sees net from then on.

    Quantised to 6 decimal places (ROUND_HALF_UP) — matching the existing
    per-unit price precision in ``costs.yaml`` — so the "except Makro rows"
    delta is deterministic and the comparison in the verification test is exact.
    """
    if not vat_inclusive:
        return gross
    return (gross / Decimal("1.07")).quantize(Decimal("0.000001"))


def _cost_comments_by_sku(text: str) -> dict[str, str]:
    """Map each ``costs:`` entry's sku_id to the trailing comment on its line.

    ``costs.yaml`` records supplier / pack-size provenance in ``# ...``
    comments after each entry (e.g. ``almond-ground: {...}  # ARO Almond 500 g``).
    Those comments are the only place the VAT-ness and the unit live, so the
    seeder parses them directly. Only the ``costs:`` block is walked; lines
    outside it are ignored.
    """
    comments: dict[str, str] = {}
    in_costs_block = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if not in_costs_block:
            if stripped.rstrip() == "costs:":
                in_costs_block = True
            continue
        # A line at the original indent (no leading whitespace) and not blank
        # ends the costs: block.
        if line and not line[0].isspace() and not stripped.startswith("#"):
            in_costs_block = False
            continue
        if stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        sku_part, _, rest = stripped.partition(":")
        sku_id = sku_part.strip()
        if "#" in rest:
            comments[sku_id] = rest.split("#", 1)[1].strip()
    return comments


def _looks_vat_inclusive(comment: str | None) -> bool:
    """True when a cost line's comment clearly names a VAT-registered supplier.

    Per ADR-0003 decision 4: VAT-ness is a property of the purchase, not the
    SKU. The migrator sets ``vat_inclusive`` only for costs whose comment
    clearly names Makro or ARO (case-insensitive). Everything else defaults
    to ``False`` so the migration never makes a number *worse* by guessing
    wrong — wet-market and ambiguous rows surface in the Slice 3 editor for
    partner confirmation.
    """
    if not comment:
        return False
    return any(marker in comment.lower() for marker in ("makro", "aro"))


def _skus_table_has_rows(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM skus").fetchone()
    return bool(row and row[0] > 0)


def _parse_decimal(value: str) -> Decimal:
    return Decimal(value)


def _decimal_or_none_to_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_year_month(value: str) -> tuple[int, int]:
    """``'2026-07'`` -> ``(2026, 7)`` (the stored ``period`` format)."""
    year_text, month_text = value.split("-", 1)
    return (int(year_text), int(month_text))


def _format_year_month(period: tuple[int, int]) -> str:
    """``(2026, 7)`` -> ``'2026-07'`` (the stored ``period`` format)."""
    year, month = period
    return f"{year:04d}-{month:02d}"


def _snapshot_price(snapshot: dict[str, Any] | None) -> Decimal | None:
    """The net per-unit price a whole-row audit snapshot records.

    ``None`` when the row did not exist on that side of the edit (a
    creation's before, a deletion's after) — the SKU had no price then.
    """
    if snapshot is None:
        return None
    return Decimal(str(snapshot["price_per_unit_net"]))


def _change_effective_date(entry: AuditEntry) -> date:
    """The calendar date a ``costs`` audit entry's price took effect.

    A save stamps the partner-facing date into the row's ``updated_at``
    (``save_cost``'s ``updated_on`` — the app's local *today*), while the
    audit ``changed_at`` clock is UTC. At the venue's UTC+7 those disagree
    between local midnight and ~07:00, and costing must follow the
    partner's calendar — otherwise an early-morning repricing would land
    on *yesterday* and silently re-state a day already reviewed.

    A normal save moves ``updated_at`` forward (or sets it on creation),
    so its new snapshot carries the effective date. A revert restores the
    field *backward* and a deletion has no new snapshot — neither records
    a local effective date, so both fall back to the UTC date of
    ``changed_at``: an undo takes effect the day it was performed, not the
    day of the change it undoes.
    """
    new_date = _snapshot_updated_at(entry.new_value)
    old_date = _snapshot_updated_at(entry.old_value)
    if new_date is not None and (old_date is None or new_date >= old_date):
        return new_date
    return datetime.fromisoformat(entry.changed_at).date()


def _snapshot_updated_at(snapshot: dict[str, Any] | None) -> date | None:
    """The ``updated_at`` date a whole-row cost snapshot records, if any."""
    if snapshot is None:
        return None
    value = snapshot.get("updated_at")
    if value is None:
        return None
    return date.fromisoformat(value)


__all__ = [
    "AuditEntry",
    "CostRow",
    "LoyverseExport",
    "SpendBucket",
    "SqliteConfigStore",
    "net_price_per_unit",
    "seed_config",
]

