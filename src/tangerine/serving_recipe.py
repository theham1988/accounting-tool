"""The serving-recipe setup stroke (CONTEXT.md "Serving recipe").

A serving recipe is a one-line recipe expressing how much of a purchasable
SKU one sold unit consumes: a bottled Chang sale = 330 ml of the
``beer-chang`` SKU, a draught pint = 473 ml of the keg SKU. Serving recipes
exist so directly-sold purchasables (beer, wine, soft drinks) cost through
the same item → SKU → recipe path as dishes — there is no second costing
path. "Beers don't have recipes" is therefore false in this model; they
have *uninteresting* ones.

Creating one from an unmapped Loyverse item creates five facts in one atomic
stroke:

  1. the purchasable SKU (receipt-priced),
  2. its cost (derived from the receipt inputs),
  3. the produced sold SKU the recipe outputs (named ``<sku>:served``),
  4. the serving recipe itself (one ingredient line, yield 1),
  5. the Loyverse item → sold-SKU mapping.

The sold SKU inherits the Loyverse item's segment so the segment-
contribution-margin view attributes the sale correctly; the purchasable
stays segment-NULL (an ingredient may feed both cafe and bar). The sold
SKU's unit is ``unit`` (one sale = one bottle / one pint); the recipe's
yield is 1, marked measured (a bottle is a bottle), and ``prep`` is False
(the sold SKU is a dish, not an ingredient for other recipes).

This module is the *domain* half of the stroke — the sibling of
:mod:`tangerine.sku_authoring` (the create-SKU stroke), reusing its
:class:`~tangerine.sku_authoring.SkuAuthoringError` as the partner-facing
error type exactly as that module's docstring anticipated. It owns the
policy — which five writes make a serving-recipe setup, which inputs are
invalid, what the sold SKU is named — and the atomicity guarantee is the
store's (:meth:`~tangerine.storage.config_store.SqliteConfigStore.batch`):
every write runs inside one ``batch()`` block, so a mid-stroke failure rolls
back every write *and* its audit row. The web route
(``POST /items/{item_id}/sold-as-is``) is a thin adapter: form → this
module → redirect on success / HTTP 400 on :class:`SkuAuthoringError`.

"sold-as-is" is the Books UI label for this stroke (a quick-create on an
unmapped item's row); it is not a domain concept, which is why this module
bears the domain name and the route keeps the UI name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from .sku_authoring import SkuAuthoringError
from .storage.config_store import SqliteConfigStore
from .types import Segment

#: Re-exported so the route and tests can import the error type alongside
#: the stroke it is raised from, without reaching into :mod:`tangerine.sku_authoring`
#: for one symbol. The error itself is defined once, in the create-SKU module
#: this stroke siblings — sharing it is exactly what that module anticipated.
__all__ = [
    "ServingRecipeSetup",
    "SkuAuthoringError",
    "create_serving_recipe_setup",
]

#: The only units a partner may declare for a purchasable SKU (CONTEXT.md
#: "Cost unit convention"): per-ml for liquids, per-g for solids, per-unit
#: for countables. The cost editor and :mod:`tangerine.sku_authoring`
#: enforce the same set; a serving-recipe setup does too, so a purchasable
#: created here is interchangeable with one created through either editor.
_ALLOWED_UNITS = frozenset({"g", "ml", "unit"})

#: The suffix that turns a purchasable sku_id into its produced sold SKU.
#: ``beer-chang`` (per-ml purchasable) → ``beer-chang:served`` (per-sale
#: sold SKU). One scheme everywhere; the recipe editor and the coverage
#: views treat the served SKU as an ordinary produced SKU.
_SERVED_SUFFIX = ":served"

#: The unit of every serving recipe's sold output — one sale consumes one
#: bottle / one pint / one can, so the sold SKU is countable.
_SOLD_UNIT = "unit"

#: One sale consumes one sold unit, so every serving recipe yields 1.
#: Measured, not estimated (a bottle is a bottle); not a prep (a sold dish,
#: not an ingredient for other recipes).
_SOLD_YIELD_QTY = Decimal("1")


@dataclass(frozen=True)
class ServingRecipeSetup:
    """The partner-typed inputs to a serving-recipe setup stroke.

    The shape of the sold-as-is quick-create form, minus the item id (which
    lives in the URL) and the audit attribution (actor / session, which the
    route pulls off the request). Frozen so the stroke cannot be mutated
    between validation and the writes that depend on it.

    The numeric fields are the raw partner-typed strings, parsed and
    range-checked by :func:`create_serving_recipe_setup` — matching the
    convention :mod:`tangerine.sku_authoring` establishes (the route hands
    the module unparsed form text; the module owns the value checks). The
    identity fields (``sku_id``, ``name``) are whitespace-stripped on
    construction so a stray space cannot defeat the duplicate-sku_id check
    or write a ``"  beer-chang  "`` row.

    - ``sku_id``           the purchasable SKU's id (e.g. ``beer-chang``).
                           Must not already exist; the produced sold SKU is
                           derived as ``f"{sku_id}:served"`` and must not
                           exist either.
    - ``name``             the purchasable SKU's human name (e.g.
                           "Chang Bottle"). Also seeds the sold SKU's name
                           as ``f"{name} (serving)"``.
    - ``unit``             the purchasable's canonical unit — one of
                           ``g``, ``ml``, ``unit`` (CONTEXT.md "Cost unit
                           convention").
    - ``pack_price`` /
      ``pack_quantity`` /
      ``vat_inclusive``    the receipt inputs the cost is derived from, per
                           ADR-0003 decision 4 (gross-input / net-stored).
    - ``serving_qty``      how much of the purchasable one sold unit
                           consumes, in the purchasable's unit (330 ml of
                           beer per bottle).
    - ``sold_segment``     the Loyverse item's segment, which the produced
                           sold SKU inherits. ``None`` when the menu did
                           not tag the item — the module does not invent a
                           segment. The purchasable is segment-NULL either
                           way (an ingredient carries no segment of its
                           own).
    """

    sku_id: str
    name: str
    unit: str
    pack_price: str
    pack_quantity: str
    vat_inclusive: bool
    serving_qty: str
    sold_segment: Segment | None

    def __post_init__(self) -> None:
        # Frozen dataclass — bypass the freeze to normalise the string fields,
        # mirroring :class:`~tangerine.sku_authoring.SkuAuthoringInput`.
        object.__setattr__(self, "sku_id", self.sku_id.strip())
        object.__setattr__(self, "name", self.name.strip())


def create_serving_recipe_setup(
    store: SqliteConfigStore,
    *,
    item_id: str,
    setup: ServingRecipeSetup,
    actor: str,
    session_id: str | None,
    today: date,
) -> str:
    """Create a serving-recipe setup for ``item_id`` in one atomic stroke.

    Validates the inputs (raising :class:`SkuAuthoringError` on any
    partner-facing problem), then runs the five writes — purchasable SKU,
    its cost, sold SKU, serving recipe, mapping — inside one
    :meth:`~tangerine.storage.config_store.SqliteConfigStore.batch`, so a
    mid-stroke failure rolls back every write and its audit row. Returns
    the produced sold SKU's id (``f"{sku_id}:served"``) so the caller can
    redirect to its editor page.

    The audit semantics are unchanged from N sequential standalone writes:
    each write still records its own audit row, all stamped with
    ``session_id``. Only the all-or-nothing durability is new — per-session
    revert and "N changes since last review" behave exactly as they would
    have for five sequential saves.
    """
    _validate_identity(setup)
    pack_price, pack_quantity = _parse_pack(setup)
    serving_qty = _parse_serving_qty(setup)

    sold_sku_id = f"{setup.sku_id}{_SERVED_SUFFIX}"
    _require_sku_free(store, setup.sku_id)
    _require_sku_free(store, sold_sku_id)

    with store.batch():
        # 1. The purchasable SKU — receipt-priced. Segment stays NULL: an
        #    ingredient may feed both cafe and bar, so it carries no segment
        #    of its own.
        store.create_sku(
            setup.sku_id,
            name=setup.name,
            unit=setup.unit,
            created_by=actor,
            session_id=session_id,
        )
        store.save_cost(
            setup.sku_id,
            pack_price=pack_price,
            pack_quantity=pack_quantity,
            vat_inclusive=setup.vat_inclusive,
            updated_by=actor,
            updated_on=today,
            session_id=session_id,
        )
        # 2. The produced sold SKU the serving recipe outputs (one per
        #    sale). It inherits the item's segment so the segment-CM view
        #    attributes the sale correctly.
        store.create_sku(
            sold_sku_id,
            name=f"{setup.name} (serving)",
            unit=_SOLD_UNIT,
            segment=setup.sold_segment,
            created_by=actor,
            session_id=session_id,
        )
        # 3. The serving recipe — one ingredient line, yield 1 sold unit.
        #    Measured (a bottle is a bottle), not a prep (a sold dish, not
        #    an ingredient for others).
        store.save_recipe(
            sold_sku_id,
            ingredients=[(setup.sku_id, serving_qty)],
            yield_qty=_SOLD_YIELD_QTY,
            yield_estimated=False,
            prep=False,
            updated_by=actor,
            session_id=session_id,
        )
        # 4. The mapping — the Loyverse item now resolves to the sold SKU.
        store.save_mapping(
            item_id, sold_sku_id, updated_by=actor, session_id=session_id
        )

    return sold_sku_id


def _validate_identity(setup: ServingRecipeSetup) -> None:
    """Non-empty id/name (post-strip) and an allowed unit, else raise."""
    if not setup.sku_id or not setup.name:
        raise SkuAuthoringError("sku_id and name are required.")
    if setup.unit not in _ALLOWED_UNITS:
        raise SkuAuthoringError("Unit must be one of: g, ml, unit.")


def _parse_pack(
    setup: ServingRecipeSetup,
) -> tuple[Decimal, Decimal]:
    """The receipt inputs, parsed and range-checked."""
    try:
        pack_price = Decimal(setup.pack_price.strip())
        pack_quantity = Decimal(setup.pack_quantity.strip())
    except InvalidOperation:
        raise SkuAuthoringError(
            "Pack price and pack quantity must be numbers."
        ) from None
    if pack_price < 0 or pack_quantity <= 0:
        raise SkuAuthoringError(
            "Pack price must be \u2265 0 and pack quantity must be > 0."
        )
    return pack_price, pack_quantity


def _parse_serving_qty(setup: ServingRecipeSetup) -> Decimal:
    """The serving quantity, parsed and range-checked."""
    try:
        serving_qty = Decimal(setup.serving_qty.strip())
    except InvalidOperation:
        raise SkuAuthoringError("Serving size must be a number.") from None
    if serving_qty <= 0:
        raise SkuAuthoringError("Serving size must be > 0.")
    return serving_qty


def _require_sku_free(store: SqliteConfigStore, sku_id: str) -> None:
    """Raise if ``sku_id`` already exists, naming it in the message."""
    if store.sku(sku_id) is not None:
        raise SkuAuthoringError(f"SKU {sku_id} already exists.")
