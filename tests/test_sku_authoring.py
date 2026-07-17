"""Unit tests for the create-SKU authoring stroke (prefactor for issue #33).

These exercise the domain module directly — no HTTP, no FastAPI app — so the
create-SKU stroke is testable at its real seam. The genuine external boundary
the module touches is the SQLite connection (``:memory:``); the audit trail,
the cost row, the mapping, and the SKU row are read back the same way the
coverage / margin engines read them, confirming the stroke lands the same
facts the inline route did.

The HTTP translation (``SkuAuthoringError`` → 400) and the unchanged UI /
HTMX behaviour are covered by the existing e2e tests in
``test_config_authoring_ui_e2e.py``; these tests pin the domain contract the
route leans on.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tangerine.sku_authoring import (
    SkuAuthoringError,
    SkuAuthoringInput,
    create_sku,
    parse_price,
)
from tangerine.storage.config_store import SqliteConfigStore, seed_config

D = Decimal

_ACTOR = "daniel"
_EFFECTIVE_ON = date(2026, 7, 16)
_SESSION_ID = "create-sku-session-1"
_NOW_ISO = "2026-07-16T08:00:00+00:00"


def _empty_recipes_path() -> Path:
    """A throwaway path to an empty ``recipes: []`` YAML file.

    ``seed_config`` needs *some* recipes file to bring the schema up; the
    strokes then build their own rows through the module under test.
    """
    path = Path(tempfile.mkdtemp()) / "recipes.yaml"
    path.write_text("recipes: []\n", encoding="utf-8")
    return path


def _store() -> SqliteConfigStore:
    """A fresh, schema-initialised store against an in-memory connection."""
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=_empty_recipes_path())
    return SqliteConfigStore(conn, now=lambda: _NOW_ISO)


def _conn(store: SqliteConfigStore) -> sqlite3.Connection:
    """The underlying connection — used to read raw rows back for assertions."""
    return store._conn  # type: ignore[attr-defined]


# --- parse_price ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["0.09", "  0.09  ", "0", "10", "1_000"],  # underscores tolerated by Decimal
)
def test_parse_price_accepts_non_negative_numbers(text: str) -> None:
    assert parse_price(text) >= 0


def test_parse_price_rejects_empty() -> None:
    with pytest.raises(SkuAuthoringError, match="Price must be a number"):
        parse_price("   ")


def test_parse_price_rejects_non_numeric() -> None:
    with pytest.raises(SkuAuthoringError, match="Price must be a number"):
        parse_price("cheap")


def test_parse_price_rejects_negative() -> None:
    with pytest.raises(SkuAuthoringError, match="Price must be"):
        parse_price("-1.50")


# --- SKU only (single write) ------------------------------------------------


def test_create_sku_writes_the_sku_row_when_no_price_or_mapping() -> None:
    """A bare SKU create (no price, no item) lands the SKU and nothing else."""
    store = _store()

    create_sku(
        store,
        SkuAuthoringInput(sku_id="oat-milk", name="Oat Milk", unit="ml"),
        created_by=_ACTOR,
        effective_on=_EFFECTIVE_ON,
        session_id=_SESSION_ID,
    )

    assert store.sku("oat-milk") is not None
    # No cost row, no mapping — those are optional and were not requested.
    assert store.cost_rows() == []
    assert store.mappings() == []


def test_create_sku_strips_whitespace_from_identity_fields() -> None:
    store = _store()

    create_sku(
        store,
        SkuAuthoringInput(sku_id="  oat-milk  ", name="  Oat Milk  ", unit="ml"),
        created_by=_ACTOR,
        effective_on=_EFFECTIVE_ON,
    )

    sku = store.sku("oat-milk")
    assert sku is not None
    assert sku.sku_id == "oat-milk"
    assert sku.name == "Oat Milk"


@pytest.mark.parametrize("unit", ["g", "ml", "unit"])
def test_create_sku_accepts_every_allowed_unit(unit: str) -> None:
    store = _store()
    create_sku(
        store,
        SkuAuthoringInput(sku_id=f"sku-{unit}", name=f"SKU {unit}", unit=unit),
        created_by=_ACTOR,
        effective_on=_EFFECTIVE_ON,
    )
    assert store.sku(f"sku-{unit}") is not None


# --- SKU + optional cost ----------------------------------------------------


def test_create_sku_records_the_price_as_pack_qty_1_net_no_vat() -> None:
    """The optional per-unit price is stored exactly as the inline route did:
    pack quantity 1, ``vat_inclusive=False``, so the stored net per-unit
    price equals the typed number (no VAT division).
    """
    store = _store()

    create_sku(
        store,
        SkuAuthoringInput(
            sku_id="oat-milk", name="Oat Milk", unit="ml", price_per_unit=D("0.09")
        ),
        created_by=_ACTOR,
        effective_on=_EFFECTIVE_ON,
        session_id=_SESSION_ID,
    )

    rows = store.cost_rows()
    assert len(rows) == 1
    [row] = rows
    assert row.sku_id == "oat-milk"
    assert row.pack_price == D("0.09")
    assert row.pack_quantity == D("1")
    assert row.vat_inclusive is False
    # net = pack_price / pack_quantity / (1.07 if vat else 1) → 0.09 exactly.
    assert row.price_per_unit_net == D("0.090000")
    assert row.updated_by == _ACTOR


def test_create_sku_with_zero_price_writes_a_zero_cost_row() -> None:
    """A price of exactly 0 is valid (a free sample, or a placeholder) and
    writes a cost row — it is not the same as omitting the price."""
    store = _store()

    create_sku(
        store,
        SkuAuthoringInput(
            sku_id="sample", name="Sample", unit="g", price_per_unit=D("0")
        ),
        created_by=_ACTOR,
        effective_on=_EFFECTIVE_ON,
    )

    [row] = store.cost_rows()
    assert row.pack_price == D("0")
    assert row.price_per_unit_net == D("0.000000")


# --- SKU + optional mapping -------------------------------------------------


def test_create_sku_maps_the_item_when_one_is_carried_along() -> None:
    """The item-coverage entry point: the new SKU exists to cost this unmapped
    item, so the mapping lands in the same stroke."""
    store = _store()

    create_sku(
        store,
        SkuAuthoringInput(
            sku_id="soda", name="House Soda", unit="ml", item_id="i-mystery"
        ),
        created_by=_ACTOR,
        effective_on=_EFFECTIVE_ON,
        session_id=_SESSION_ID,
    )

    [mapping] = store.mappings()
    assert mapping.item_id == "i-mystery"
    assert mapping.sku_id == "soda"
    # No cost row was requested.
    assert store.cost_rows() == []


def test_create_sku_with_price_and_mapping_writes_all_three() -> None:
    """The full stroke: SKU + cost + mapping, the multi-write path."""
    store = _store()

    create_sku(
        store,
        SkuAuthoringInput(
            sku_id="soda",
            name="House Soda",
            unit="ml",
            price_per_unit=D("0.03"),
            item_id="i-mystery",
        ),
        created_by=_ACTOR,
        effective_on=_EFFECTIVE_ON,
        session_id=_SESSION_ID,
    )

    assert store.sku("soda") is not None
    assert store.cost_rows()[0].sku_id == "soda"
    [mapping] = store.mappings()
    assert mapping.item_id == "i-mystery"


# --- error cases ------------------------------------------------------------


def test_create_sku_requires_sku_id_and_name() -> None:
    store = _store()
    with pytest.raises(SkuAuthoringError, match="sku_id and name are required"):
        create_sku(
            store,
            SkuAuthoringInput(sku_id="", name="Oat Milk", unit="ml"),
            created_by=_ACTOR,
            effective_on=_EFFECTIVE_ON,
        )


def test_create_sku_requires_name_even_when_sku_id_is_present() -> None:
    store = _store()
    with pytest.raises(SkuAuthoringError, match="sku_id and name are required"):
        create_sku(
            store,
            SkuAuthoringInput(sku_id="oat-milk", name="   ", unit="ml"),
            created_by=_ACTOR,
            effective_on=_EFFECTIVE_ON,
        )


@pytest.mark.parametrize("bad_unit", ["kg", "", "grams", "ML", "units"])
def test_create_sku_rejects_an_unallowed_unit(bad_unit: str) -> None:
    store = _store()
    with pytest.raises(SkuAuthoringError, match="Unit must be g, ml, or unit"):
        create_sku(
            store,
            SkuAuthoringInput(sku_id="x", name="X", unit=bad_unit),
            created_by=_ACTOR,
            effective_on=_EFFECTIVE_ON,
        )


def test_create_sku_rejects_a_duplicate_sku_id() -> None:
    store = _store()
    create_sku(
        store,
        SkuAuthoringInput(sku_id="oat-milk", name="Oat Milk", unit="ml"),
        created_by=_ACTOR,
        effective_on=_EFFECTIVE_ON,
    )
    with pytest.raises(SkuAuthoringError, match="already exists"):
        create_sku(
            store,
            SkuAuthoringInput(sku_id="oat-milk", name="Oat Milk 2", unit="ml"),
            created_by=_ACTOR,
            effective_on=_EFFECTIVE_ON,
        )


def test_create_sku_error_is_a_value_error() -> None:
    """The error subclasses ``ValueError`` to match the project's other domain
    errors (QuantityError, etc.) — the route relies on this only by name, but
    the type relationship matters for any caller catching ``ValueError``."""
    assert issubclass(SkuAuthoringError, ValueError)


# --- atomicity: a mid-stroke failure rolls back the whole stroke ------------


def test_a_mid_stroke_failure_rolls_back_sku_cost_and_mapping() -> None:
    """When the stroke is multi-write, a failure partway through must leave the
    store untouched — the audit trail never records a half-stroke. We simulate
    a mid-stroke failure by monkeypatching ``save_mapping`` (the last write of
    a full stroke) to raise, after the SKU and cost have already been issued
    inside the batch block.
    """
    store = _store()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated mid-stroke failure")

    store.save_mapping = boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated mid-stroke failure"):
        create_sku(
            store,
            SkuAuthoringInput(
                sku_id="soda",
                name="House Soda",
                unit="ml",
                price_per_unit=D("0.03"),
                item_id="i-mystery",
            ),
            created_by=_ACTOR,
            effective_on=_EFFECTIVE_ON,
            session_id=_SESSION_ID,
        )

    # Nothing landed — the SKU, the cost row, and (obviously) the mapping are
    # all absent, and the audit log has no entry for the stroke.
    assert store.sku("soda") is None
    assert store.cost_rows() == []
    assert store.mappings() == []
    assert [e for e in store.audit_entries() if e.table_name == "skus"] == []
    assert [e for e in store.audit_entries() if e.table_name == "costs"] == []


# --- audit semantics: each write still records its own row ------------------


def test_create_sku_with_price_and_mapping_writes_three_audit_rows() -> None:
    """Audit semantics are unchanged by batching: each write records its own
    audit row, all stamped with the same ``session_id`` — exactly what N
    sequential standalone saves would have produced. Only the atomicity is
    new."""
    store = _store()

    create_sku(
        store,
        SkuAuthoringInput(
            sku_id="soda",
            name="House Soda",
            unit="ml",
            price_per_unit=D("0.03"),
            item_id="i-mystery",
        ),
        created_by=_ACTOR,
        effective_on=_EFFECTIVE_ON,
        session_id=_SESSION_ID,
    )

    entries = store.audit_entries()
    table_names = {e.table_name for e in entries}
    assert table_names == {"skus", "costs", "mappings"}
    assert all(e.session_id == _SESSION_ID for e in entries)
    assert all(e.changed_by == _ACTOR for e in entries)
