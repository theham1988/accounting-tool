"""Atomic multi-write authoring stroke (Wave 1.5 prefactor).

``SqliteConfigStore.batch()`` lets a multi-write authoring stroke run in one
lock + one SQLite transaction, so a mid-stroke failure rolls back every
write *and* its audit row together — the audit log never records a partial
stroke. The shape this unlocks: the sold-as-is quick-create (a purchasable
SKU + its cost + a sold SKU + a serving recipe + a mapping) lands as
all-or-nothing.

This ticket is a prefactor: the capability ships, the partner-facing routes
are *not* rewired to use it yet. The tests here prove the store contract
directly so the next ticket can lean on it without re-litigating the
atomicity guarantee.

The genuine boundary is the SQLite connection (``:memory:`` for tests).
These tests read as worked examples mirroring the strokes that will land in
the routes — a sold-as-is-shaped stroke (4 writes spanning 4 tables) and a
serving-recipe-shaped stroke.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.types import Segment

D = Decimal


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def _empty_recipes_path() -> Path:
    """A throwaway path to an empty ``recipes: []`` YAML file.

    ``seed_config`` needs *some* recipes file to bring the schema up; the
    strokes then build their own rows through the store's write methods.
    Lives in a temp dir so the file outlives the seed call (seed_config
    reads it lazily for the YAML parse).
    """
    path = Path(tempfile.mkdtemp()) / "recipes.yaml"
    path.write_text("recipes: []\n", encoding="utf-8")
    return path


def _seeded_store(now_iso: str = "2026-07-16T00:00:00+00:00") -> tuple[sqlite3.Connection, SqliteConfigStore]:
    """A store holding one purchasable ingredient SKU, ready for a stroke.

    The fixture mirrors the precondition of a sold-as-is stroke: a partner
    has just typed a receipt for a purchasable SKU (a Chang bottle) and is
    about to also create the sold SKU, a serving recipe, and the Loyverse
    mapping in one atomic stroke.

    ``now_iso`` pins the audit/mapping clock so two stores running the same
    stroke produce identical timestamps — the audit-semantics test compares
    them field-for-field.
    """
    conn = _connect()
    seed_config(conn, recipes_path=_empty_recipes_path())
    return conn, SqliteConfigStore(conn, now=lambda: now_iso)


_SESSION_ID = "stroke-session-1"
_ACTOR = "daniel"


def _sold_as_is_stroke(
    store: SqliteConfigStore,
    *,
    item_id: str = "i-chang-bottle",
    purchasable_sku_id: str = "chang-bottle",
    sold_sku_id: str = "chang-bottle:served",
    fail_after: int | None = None,
) -> None:
    """Drive a sold-as-is-shaped stroke through ``store.batch()``.

    Four writes across four tables, mirroring what
    ``POST /items/{item_id}/sold-as-is`` will do once rewired:

    1. create the purchasable SKU (receipt-priced)
    2. save its cost from the receipt
    3. create the produced sold SKU the serving recipe outputs
    4. save the serving recipe (one ingredient line, yield 1 sold unit)
    5. save the Loyverse item -> sold-SKU mapping

    ``fail_after`` raises a synthetic exception after the Nth write, so the
    rollback test can simulate a mid-stroke failure at any point without
    coupling to a particular write's internals.
    """
    writes_done = 0

    def _checkpoint(n: int) -> None:
        nonlocal writes_done
        writes_done = n
        if fail_after is not None and writes_done >= fail_after:
            raise RuntimeError(f"synthetic mid-stroke failure after write {n}")

    with store.batch():
        store.create_sku(
            purchasable_sku_id,
            name="Chang Bottle 330ml",
            unit="ml",
            created_by=_ACTOR,
            session_id=_SESSION_ID,
        )
        _checkpoint(1)
        store.save_cost(
            purchasable_sku_id,
            pack_price=D("120"),
            pack_quantity=D("330"),
            vat_inclusive=True,
            updated_by=_ACTOR,
            updated_on=date(2026, 7, 16),
            session_id=_SESSION_ID,
        )
        _checkpoint(2)
        store.create_sku(
            sold_sku_id,
            name="Chang Bottle 330ml (serving)",
            unit="ml",
            segment=Segment.BAR,
            created_by=_ACTOR,
            session_id=_SESSION_ID,
        )
        _checkpoint(3)
        store.save_recipe(
            sold_sku_id,
            ingredients=[(purchasable_sku_id, D("330"))],
            yield_qty=D("1"),
            yield_estimated=False,
            updated_by=_ACTOR,
            session_id=_SESSION_ID,
        )
        _checkpoint(4)
        store.save_mapping(
            item_id, sold_sku_id, updated_by=_ACTOR, session_id=_SESSION_ID
        )
        _checkpoint(5)


# --- AC: successful stroke persists all writes + one audit row per change ---


def test_successful_stroke_persists_every_write() -> None:
    """A batch that exits normally commits every write to its table.

    Worked example. A sold-as-is stroke runs five writes across four
    tables (skus x2, costs x1, recipes x1, mappings x1). After the batch
    exits normally, each of those rows is readable through the store's
    read side — proving the writes were committed together rather than
    dropped by some rollback on the way out.
    """
    _conn, store = _seeded_store()

    _sold_as_is_stroke(store)

    skus = {s.sku_id: s for s in store.skus()}
    assert "chang-bottle" in skus
    assert skus["chang-bottle"].unit == "ml"
    assert "chang-bottle:served" in skus
    assert skus["chang-bottle:served"].segment is Segment.BAR

    costs = {c.sku_id: c for c in store.cost_rows()}
    # 120 / 330 / 1.07 = 0.339847... net per ml.
    assert costs["chang-bottle"].price_per_unit_net == D("0.339847")
    assert costs["chang-bottle"].vat_inclusive is True

    recipes = {r.sku_id: r for r in store.recipes()}
    assert recipes["chang-bottle:served"].ingredients[0].sku_id == "chang-bottle"
    assert recipes["chang-bottle:served"].ingredients[0].quantity == D("330")

    mappings = {m.item_id: m for m in store.mappings()}
    assert mappings["i-chang-bottle"].sku_id == "chang-bottle:served"


def test_successful_stroke_records_one_audit_row_per_change_same_session() -> None:
    """A batch writes one audit row per table change, all stamped with the
    same ``session_id`` — the same audit trail N sequential standalone
    writes would have produced.

    Worked example. The five-write sold-as-is stroke touches five distinct
    rows (one of them twice — the purchasable SKU is created and then
    costed, which are different rows in different tables). Each lands its
    own audit entry; every entry carries the stroke's ``session_id``. This
    is what makes per-session revert and the "N changes since last review"
    link behave identically to today's sequential-stroke behaviour — only
    the all-or-nothing durability is new.
    """
    _conn, store = _seeded_store()

    _sold_as_is_stroke(store)

    entries = store.audit_entries()  # newest first
    # Five writes -> five audit rows (skus, costs, skus, recipes, mappings).
    assert len(entries) == 5
    # Every entry carries the stroke's session_id.
    assert {e.session_id for e in entries} == {_SESSION_ID}
    # Every entry is attributable to the actor who ran the stroke.
    assert {e.changed_by for e in entries} == {_ACTOR}
    # The five entries cover the four tables the stroke wrote.
    tables_written = {e.table_name for e in entries}
    assert tables_written == {"skus", "costs", "recipes", "mappings"}


# --- AC: mid-stroke failure rolls back every write + every audit row --------


@pytest.mark.parametrize("fail_after", [1, 2, 3, 4])
def test_mid_stroke_failure_rolls_back_every_write(fail_after: int) -> None:
    """An exception propagating out of a batch leaves no write behind.

    Worked example. The sold-as-is stroke fails synthetically after the
    Nth write (parametrised across every position in the stroke). After
    the exception propagates, *none* of the writes the stroke made are
    visible — not the cost, not the SKUs, not the recipe, not the mapping.
    The stroke is all-or-nothing by construction; whichever write failed,
    the ones before it die with it.
    """
    _conn, store = _seeded_store()

    with pytest.raises(RuntimeError, match="synthetic mid-stroke failure"):
        _sold_as_is_stroke(store, fail_after=fail_after)

    skus = {s.sku_id for s in store.skus()}
    assert "chang-bottle" not in skus
    assert "chang-bottle:served" not in skus

    costs = {c.sku_id for c in store.cost_rows()}
    assert "chang-bottle" not in costs

    recipes = {r.sku_id for r in store.recipes()}
    assert "chang-bottle:served" not in recipes

    mappings = {m.item_id for m in store.mappings()}
    assert "i-chang-bottle" not in mappings


def test_mid_stroke_failure_leaves_no_audit_row() -> None:
    """A rolled-back stroke writes nothing to the audit log.

    The atomicity guarantee cuts both ways: not only are the data writes
    rolled back, so are the audit rows they recorded. A partner scanning
    ``/audit`` after a failed stroke sees no half-stroke — no "created
    SKU, then it vanished" trail to confuse tomorrow morning's diff. The
    audit log records *committed* strokes only.

    Worked example. The stroke fails after the third write. The audit log
    stays empty — even though three audit rows were inserted inside the
    transaction, the rollback dropped them along with the writes.
    """
    _conn, store = _seeded_store()

    with pytest.raises(RuntimeError):
        _sold_as_is_stroke(store, fail_after=3)

    assert store.audit_entries() == []


def test_a_stroke_after_a_failed_stroke_is_clean() -> None:
    """A failed stroke leaves the store usable; the next stroke works.

    A rolled-back transaction must not poison the connection or leave the
    store in a stuck "still in batch" state. The partner will retry the
    stroke (or move on to a different one); the store must serve that
    next attempt as if nothing had happened.

    Worked example. The first stroke fails after two writes. A second
    stroke — same shape — runs to completion and lands every write + audit
    row, proving the connection and the ``_in_batch`` flag both cleaned up
    on the way out.
    """
    _conn, store = _seeded_store()

    with pytest.raises(RuntimeError):
        _sold_as_is_stroke(store, fail_after=2)

    _sold_as_is_stroke(store)

    # The successful stroke landed all five writes.
    skus = {s.sku_id for s in store.skus()}
    assert {"chang-bottle", "chang-bottle:served"} <= skus
    # And all five audit rows — no leftover from the failed stroke.
    assert len(store.audit_entries()) == 5


# --- AC: audit rows from a batch == audit rows from sequential standalone ---


def test_batch_audit_rows_match_sequential_standalone_audit_rows() -> None:
    """The audit trail a batch produces is identical to what the same writes
    made as N standalone calls would produce — same row count, same per-row
    table_name and pk, same session_id, same old/new snapshots.

    This is the "audit semantics unchanged" guarantee. A partner running
    tomorrow morning's diff cannot tell whether yesterday's five edits
    were one batch or five sequential saves — and that is the point. The
    only behavioural difference ``batch()`` adds is atomicity; the paper
    trail is the same one Slice 5 already produced.
    """
    batch_conn = _connect()
    sequential_conn = _connect()
    empty = _empty_recipes_path()
    seed_config(batch_conn, recipes_path=empty)
    seed_config(sequential_conn, recipes_path=empty)

    # Pin the audit/mapping clock to the same instant on both stores, so the
    # only difference between the two stroke trails can be the all-or-nothing
    # durability — not "which stroke ran a few microseconds later".
    pinned_now = "2026-07-16T09:00:00+00:00"
    batch_store = SqliteConfigStore(batch_conn, now=lambda: pinned_now)
    sequential_store = SqliteConfigStore(sequential_conn, now=lambda: pinned_now)

    # Run the same stroke both ways.
    _sold_as_is_stroke(batch_store)
    _sold_as_is_stroke_sequential(sequential_store)

    batch_entries = batch_store.audit_entries()
    sequential_entries = sequential_store.audit_entries()

    assert len(batch_entries) == len(sequential_entries) == 5
    # The entries are returned newest-first; compare them pairwise on every
    # field that defines audit identity. With the clock pinned the snapshots
    # (which carry updated_at / changed_at) are bit-for-bit identical too —
    # the strongest statement that "audit semantics are unchanged".
    for batch_entry, sequential_entry in zip(batch_entries, sequential_entries):
        assert batch_entry == sequential_entry


def _sold_as_is_stroke_sequential(store: SqliteConfigStore) -> None:
    """The same stroke as :func:`_sold_as_is_stroke`, run as standalone calls.

    No ``batch()`` — each write opens and commits its own transaction, the
    way every authoring route does today. The point is to produce the
    control group the batch's audit trail must match.
    """
    store.create_sku(
        "chang-bottle",
        name="Chang Bottle 330ml",
        unit="ml",
        created_by=_ACTOR,
        session_id=_SESSION_ID,
    )
    store.save_cost(
        "chang-bottle",
        pack_price=D("120"),
        pack_quantity=D("330"),
        vat_inclusive=True,
        updated_by=_ACTOR,
        updated_on=date(2026, 7, 16),
        session_id=_SESSION_ID,
    )
    store.create_sku(
        "chang-bottle:served",
        name="Chang Bottle 330ml (serving)",
        unit="ml",
        segment=Segment.BAR,
        created_by=_ACTOR,
        session_id=_SESSION_ID,
    )
    store.save_recipe(
        "chang-bottle:served",
        ingredients=[("chang-bottle", D("330"))],
        yield_qty=D("1"),
        yield_estimated=False,
        updated_by=_ACTOR,
        session_id=_SESSION_ID,
    )
    store.save_mapping(
        "i-chang-bottle",
        "chang-bottle:served",
        updated_by=_ACTOR,
        session_id=_SESSION_ID,
    )


# --- AC: nested batches reuse the outer transaction --------------------------


def test_nested_batches_do_not_commit_until_the_outer_block_exits() -> None:
    """A ``batch()`` opened inside another ``batch()`` reuses the outer
    transaction; the outer block's exit is what commits.

    A helper that wraps its own ``batch()`` must be safe to call from inside
    another batch — that's how the routes will compose strokes. The contract
    is: the inner block does not commit (if it did, a failure after it
    returned could not roll back the writes the helper made).

    Worked example. An inner batch writes one SKU; the outer batch then
    writes a second SKU and raises. Both writes must roll back — proving the
    inner batch did not commit early.
    """
    _conn, store = _seeded_store()

    def helper_that_batches_internally() -> None:
        with store.batch():
            store.create_sku(
                "inner-sku",
                name="Inner",
                unit="g",
                created_by=_ACTOR,
                session_id=_SESSION_ID,
            )

    with pytest.raises(RuntimeError, match="outer failure"):
        with store.batch():
            helper_that_batches_internally()
            store.create_sku(
                "outer-sku",
                name="Outer",
                unit="g",
                created_by=_ACTOR,
                session_id=_SESSION_ID,
            )
            raise RuntimeError("outer failure")

    skus = {s.sku_id for s in store.skus()}
    assert "inner-sku" not in skus
    assert "outer-sku" not in skus
    assert store.audit_entries() == []


# --- AC: standalone write behaviour is unchanged (regression) ----------------


def test_standalone_save_still_works_without_batch() -> None:
    """A write called outside any ``batch()`` opens and commits its own
    transaction, exactly as before this prefactor — today's authoring routes
    are unchanged and must keep working until they are explicitly rewired.

    Worked example. ``save_cost`` called with no surrounding ``batch()``
    lands its write + audit row, immediately visible to the next read.
    """
    _conn, store = _seeded_store()
    store.create_sku(
        "standalone-sku",
        name="Standalone",
        unit="g",
        created_by=_ACTOR,
        session_id=_SESSION_ID,
    )
    store.save_cost(
        "standalone-sku",
        pack_price=D("100"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by=_ACTOR,
        updated_on=date(2026, 7, 16),
        session_id=_SESSION_ID,
    )

    costs = {c.sku_id: c for c in store.cost_rows()}
    assert costs["standalone-sku"].price_per_unit_net == D("0.100000")
    # Two standalone writes -> two audit rows, same as before this change.
    assert len(store.audit_entries()) == 2
