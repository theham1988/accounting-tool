"""The serving-recipe setup domain stroke (extracted from the sold-as-is route).

A serving recipe is CONTEXT.md's one-line recipe expressing how much of a
purchasable SKU one sold unit consumes (a bottled Chang sale = 330 ml of the
``beer-chang`` SKU). Creating one from an unmapped Loyverse item creates five
facts in one atomic stroke:

  1. the purchasable SKU (receipt-priced),
  2. its cost (from the receipt),
  3. the produced sold SKU the recipe outputs (named ``<sku>:served``),
  4. the serving recipe itself (one ingredient line, yield 1),
  5. the Loyverse item -> sold-SKU mapping.

"sold-as-is" is the Books UI label for this stroke (a quick-create on an
unmapped item's row); it is not a domain concept. This module is the domain
half of that stroke; the route is a thin adapter that turns the form post
into a call here.

These tests exercise the stroke *without HTTP* — a real
:class:`~tangerine.storage.config_store.SqliteConfigStore` over an in-memory
SQLite connection, driven directly. The atomicity guarantee is the store's
(``batch()``); this module is the policy that knows which five writes make
the stroke and which inputs are invalid.

The partner-facing (HTTP) behaviour is covered by the existing sold-as-is e2e
tests in ``test_config_authoring_ui_e2e.py``; those keep passing unchanged
once the route is rewired to call this module.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tangerine.serving_recipe import (
    ServingRecipeSetup,
    SkuAuthoringError,
    create_serving_recipe_setup,
)
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.types import Segment

D = Decimal

_ACTOR = "daniel"
_SESSION_ID = "stroke-session-1"
_TODAY = date(2026, 7, 16)


def _empty_recipes_path() -> Path:
    """A throwaway path to an empty ``recipes: []`` YAML file.

    ``seed_config`` needs *some* recipes file to bring the schema up; the
    stroke then builds its own rows through the store's write methods.
    """
    path = Path(tempfile.mkdtemp()) / "recipes.yaml"
    path.write_text("recipes: []\n", encoding="utf-8")
    return path


def _seeded_store(
    now_iso: str = "2026-07-16T00:00:00+00:00",
) -> tuple[sqlite3.Connection, SqliteConfigStore]:
    """A store with the schema up and no rows, ready for a stroke."""
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=_empty_recipes_path())
    return conn, SqliteConfigStore(conn, now=lambda: now_iso)


def _chang_setup(
    *,
    sku_id: str = "beer-chang",
    sold_segment: Segment | None = Segment.BAR,
) -> ServingRecipeSetup:
    """The worked example mirroring the e2e fixture: a bottled Chang.

    A 6-pack of 330 ml bottles bought at 360 THB VAT-inclusive, served as
    one 330 ml bottle per sale. The sold SKU is ``beer-chang:served``.

    The numeric fields are strings — the dataclass carries the partner-typed
    form values and the stroke parses them, exactly as the route will pass
    the raw ``Form(...)`` strings through.
    """
    return ServingRecipeSetup(
        sku_id=sku_id,
        name="Chang Bottle",
        unit="ml",
        pack_price="360",
        pack_quantity="1980",  # 6 × 330 ml
        vat_inclusive=True,
        serving_qty="330",
        sold_segment=sold_segment,
    )


# --- AC: a successful stroke creates all five facts --------------------------


def test_successful_stroke_creates_the_purchasable_sku_receipt_priced() -> None:
    """The purchasable SKU lands, priced from the receipt inputs.

    360 / 1980 / 1.07 = 0.169923 THB/ml net — the same derived net price
    the cost editor would store for those inputs.
    """
    _conn, store = _seeded_store()

    create_serving_recipe_setup(
        store,
        item_id="i-chang",
        setup=_chang_setup(),
        actor=_ACTOR,
        session_id=_SESSION_ID,
        today=_TODAY,
    )

    skus = {s.sku_id: s for s in store.skus()}
    assert skus["beer-chang"].name == "Chang Bottle"
    assert skus["beer-chang"].unit == "ml"
    # The purchasable carries no segment — it may feed both cafe and bar.
    assert skus["beer-chang"].segment is None

    costs = {c.sku_id: c for c in store.cost_rows()}
    assert costs["beer-chang"].price_per_unit_net == D("0.169924")
    assert costs["beer-chang"].vat_inclusive is True


def test_successful_stroke_creates_the_sold_sku_inheriting_the_item_segment() -> None:
    """The produced sold SKU is ``<sku>:served``, unit ``unit``, inheriting
    the Loyverse item's segment so the segment-CM view attributes the sale
    correctly. A bar item yields a bar sold SKU.
    """
    _conn, store = _seeded_store()

    create_serving_recipe_setup(
        store,
        item_id="i-chang",
        setup=_chang_setup(),
        actor=_ACTOR,
        session_id=_SESSION_ID,
        today=_TODAY,
    )

    skus = {s.sku_id: s for s in store.skus()}
    sold = skus["beer-chang:served"]
    assert sold.name == "Chang Bottle (serving)"
    assert sold.unit == "unit"
    assert sold.segment is Segment.BAR


def test_successful_stroke_creates_the_serving_recipe_yield_one_not_prep() -> None:
    """The serving recipe is one ingredient line at the serving quantity,
    yield 1 in the sold SKU's unit, marked measured (not estimated), and
    not a prep — the sold SKU is a dish, not an ingredient for others.
    """
    _conn, store = _seeded_store()

    create_serving_recipe_setup(
        store,
        item_id="i-chang",
        setup=_chang_setup(),
        actor=_ACTOR,
        session_id=_SESSION_ID,
        today=_TODAY,
    )

    recipes = {r.sku_id: r for r in store.recipes()}
    recipe = recipes["beer-chang:served"]
    assert len(recipe.ingredients) == 1
    assert recipe.ingredients[0].sku_id == "beer-chang"
    assert recipe.ingredients[0].quantity == D("330")
    assert recipe.yield_qty == D("1")
    assert recipe.yield_estimated is False
    assert recipe.prep is False


def test_successful_stroke_maps_the_item_to_the_sold_sku() -> None:
    """The Loyverse item now resolves to the produced sold SKU — the
    unmapped item that prompted the stroke is mapped.
    """
    _conn, store = _seeded_store()

    create_serving_recipe_setup(
        store,
        item_id="i-chang",
        setup=_chang_setup(),
        actor=_ACTOR,
        session_id=_SESSION_ID,
        today=_TODAY,
    )

    mappings = {m.item_id: m for m in store.mappings()}
    assert mappings["i-chang"].sku_id == "beer-chang:served"


def test_successful_stroke_with_no_item_segment_leaves_sold_sku_unsegmented() -> None:
    """A Loyverse item with no segment (the menu did not tag it) leaves the
    sold SKU unsegmented too — the module does not invent a segment. The
    purchasable is segment-NULL either way.
    """
    _conn, store = _seeded_store()

    create_serving_recipe_setup(
        store,
        item_id="i-mystery",
        setup=_chang_setup(sold_segment=None),
        actor=_ACTOR,
        session_id=_SESSION_ID,
        today=_TODAY,
    )

    skus = {s.sku_id: s for s in store.skus()}
    assert skus["beer-chang:served"].segment is None
    assert skus["beer-chang"].segment is None


# --- AC: every write is audited, attributed to the actor + session -----------
# (the atomicity guarantee itself — mid-stroke rollback — is the store's,
# covered by test_config_store_batch_e2e.py. Here we assert the stroke lands
# the expected audit trail.)


def test_successful_stroke_records_five_audited_changes_one_session() -> None:
    """The stroke is five writes across four tables (skus, costs, recipes,
    mappings), each landing its own audit row, all attributed to the actor
    and the one session id — the same trail the route produced before
    extraction, so per-session revert and "N changes since last review"
    behave identically.
    """
    _conn, store = _seeded_store()

    create_serving_recipe_setup(
        store,
        item_id="i-chang",
        setup=_chang_setup(),
        actor=_ACTOR,
        session_id=_SESSION_ID,
        today=_TODAY,
    )

    entries = store.audit_entries()  # newest first
    assert len(entries) == 5
    assert {e.session_id for e in entries} == {_SESSION_ID}
    assert {e.changed_by for e in entries} == {_ACTOR}
    assert {e.table_name for e in entries} == {"skus", "costs", "recipes", "mappings"}


# --- AC: invalid inputs raise SkuAuthoringError with a partner-facing msg -----


@pytest.mark.parametrize(
    "setup_overrides, expected_fragment",
    [
        ({"sku_id": "", "name": ""}, "sku_id and name are required"),
        ({"sku_id": "beer-chang", "name": "   "}, "sku_id and name are required"),
        ({"unit": "kg"}, "Unit must be one of: g, ml, unit"),
        ({"unit": ""}, "Unit must be one of: g, ml, unit"),
        ({"pack_price": "abc"}, "Pack price and pack quantity must be numbers"),
        ({"pack_quantity": ""}, "Pack price and pack quantity must be numbers"),
        ({"pack_price": "-1"}, "Pack price must be"),
        ({"pack_quantity": "0"}, "pack quantity must be > 0"),
        ({"serving_qty": "abc"}, "Serving size must be a number"),
        ({"serving_qty": "0"}, "Serving size must be > 0"),
    ],
)
def test_invalid_inputs_raise_sku_authoring_error(
    setup_overrides: dict[str, object], expected_fragment: str
) -> None:
    """Each validation rule raises :class:`SkuAuthoringError` with a message
    naming the problem in partner-readable terms. The route maps these to
    HTTP 400; the module itself knows nothing about HTTP.
    """
    _conn, store = _seeded_store()
    base = _chang_setup()
    fields: dict[str, object] = {
        "sku_id": base.sku_id,
        "name": base.name,
        "unit": base.unit,
        "pack_price": base.pack_price,
        "pack_quantity": base.pack_quantity,
        "vat_inclusive": base.vat_inclusive,
        "serving_qty": base.serving_qty,
        "sold_segment": base.sold_segment,
    }
    fields.update(setup_overrides)

    with pytest.raises(SkuAuthoringError, match=expected_fragment):
        create_serving_recipe_setup(
            store,
            item_id="i-chang",
            setup=ServingRecipeSetup(**fields),  # type: ignore[arg-type]
            actor=_ACTOR,
            session_id=_SESSION_ID,
            today=_TODAY,
        )

    # Nothing landed — the stroke rejected before any write.
    assert store.skus() == []
    assert store.audit_entries() == []


def test_existing_purchasable_sku_id_is_rejected() -> None:
    """A sku_id that already exists cannot be re-created — the stroke
    rejects with a partner-facing message rather than letting the store's
    INSERT fail mid-stroke (which would surface a raw integrity error).
    """
    _conn, store = _seeded_store()
    store.create_sku(
        "beer-chang",
        name="Existing",
        unit="ml",
        created_by=_ACTOR,
        session_id=_SESSION_ID,
    )

    with pytest.raises(SkuAuthoringError, match="beer-chang already exists"):
        create_serving_recipe_setup(
            store,
            item_id="i-chang",
            setup=_chang_setup(),
            actor=_ACTOR,
            session_id=_SESSION_ID,
            today=_TODAY,
        )


def test_existing_served_sku_id_is_rejected() -> None:
    """The derived ``<sku>:served`` id must be free too — a leftover served
    SKU from a prior mapping (or a hand-created one) rejects the stroke
    before any write, so the failure message names the real conflict.
    """
    _conn, store = _seeded_store()
    store.create_sku(
        "beer-chang:served",
        name="Existing served",
        unit="unit",
        created_by=_ACTOR,
        session_id=_SESSION_ID,
    )

    with pytest.raises(SkuAuthoringError, match="beer-chang:served already exists"):
        create_serving_recipe_setup(
            store,
            item_id="i-chang",
            setup=_chang_setup(),
            actor=_ACTOR,
            session_id=_SESSION_ID,
            today=_TODAY,
        )


# --- AC: a stroke does not half-land (the store's batch backs it) -------------


def test_failed_stroke_leaves_nothing_behind() -> None:
    """If anything inside the stroke raises, nothing lands — neither the
    data writes nor their audit rows. The module relies on the store's
    ``batch()`` for atomicity; this test proves the module actually uses it
    (a stroke that raised after the first write would otherwise leave the
    purchasable SKU behind).
    """
    _conn, store = _seeded_store()

    # Sabotage the cost write by pre-creating the purchasable SKU *without*
    # the guard seeing it: inject a fault by passing a serving quantity the
    # engine accepts but the recipe write rejects is hard to engineer, so
    # instead drive a real second SKU-id collision mid-stroke by pre-seeding
    # the served id *after* validation but conceptually the cleanest path is
    # to assert the no-half-stroke property via the store's own contract.
    #
    # Concretely: a stroke whose sold sku id already exists is rejected up
    # front (see test_existing_served_sku_id_is_rejected), so the only way a
    # mid-stroke failure can happen here is a store-level fault. We simulate
    # one by closing the connection's transaction forcibly: drop the table
    # the recipe write needs, so the third write fails after two writes.
    _conn.execute("DROP TABLE recipe_ingredients")
    _conn.execute("DROP TABLE recipes")
    _conn.commit()

    with pytest.raises(Exception):
        create_serving_recipe_setup(
            store,
            item_id="i-chang",
            setup=_chang_setup(),
            actor=_ACTOR,
            session_id=_SESSION_ID,
            today=_TODAY,
        )

    # The purchasable SKU + cost the stroke wrote before the recipe write
    # failed must NOT be visible — the batch rolled them back. The skus/
    # costs tables still exist (the stroke created rows in them inside the
    # transaction); the reads just see nothing because the transaction
    # rolled back.
    assert {s.sku_id for s in store.skus()} == set()
    assert {c.sku_id for c in store.cost_rows()} == set()
    assert store.audit_entries() == []


# --- AC: a vat-exclusive purchase stores gross-as-net ------------------------


def test_vat_exclusive_purchase_skips_the_vat_division() -> None:
    """A wet-market purchase (no VAT) stores the gross price as the net
    price — 360 / 1980 = 0.181818 THB/ml, no 1.07 division. Same rule as
    the cost editor.
    """
    _conn, store = _seeded_store()

    create_serving_recipe_setup(
        store,
        item_id="i-chang",
        setup=ServingRecipeSetup(
            sku_id="beer-chang",
            name="Chang Bottle",
            unit="ml",
            pack_price="360",
            pack_quantity="1980",
            vat_inclusive=False,
            serving_qty="330",
            sold_segment=Segment.BAR,
        ),
        actor=_ACTOR,
        session_id=_SESSION_ID,
        today=_TODAY,
    )

    costs = {c.sku_id: c for c in store.cost_rows()}
    assert costs["beer-chang"].price_per_unit_net == D("0.181818")
    assert costs["beer-chang"].vat_inclusive is False
