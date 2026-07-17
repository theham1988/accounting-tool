"""Domain module for the create-SKU authoring stroke (issue #33 / prefactor).

A "create SKU" stroke is one of the config authoring surfaces that the
Wave 1.5 audit-and-revert safety net (ADR-0003) has to land atomically.
From the partner's side it is a single act — type the SKU id, name, unit,
maybe a per-unit price, maybe map an unmapped Loyverse item in the same
breath — but underneath it can be up to three writes: the SKU itself, an
optional cost row, and an optional item mapping. This module owns that
stroke so the route (``POST /skus``) is a thin adapter: it parses the
form, calls :func:`create_sku`, and translates :class:`SkuAuthoringError`
into HTTP 400. The route holds no business logic of its own — exactly the
seam the recipe editor and the rest of the authoring surface already use.

The semantic this module pins (matching the inline code it replaces):

- the SKU is always created with its unit confirmed (``g`` / ``ml`` /
  ``unit``) — ADR-0003 decision 3;
- the optional per-unit ``price`` is stored as a net cost with pack
  quantity 1 and ``vat_inclusive=False`` (a receipt-shaped cost can
  replace it any time through the cost editor);
- the optional mapping, when an item id is carried along, lands in the
  same stroke — the SKU exists to cost that item.

When the stroke is more than one write, it runs inside the store's
:meth:`~tangerine.storage.config_store.SqliteConfigStore.batch` block —
one lock, one SQLite transaction — so a mid-stroke failure rolls back
every write *and* its audit row. The audit trail therefore never records
a half-stroke to confuse tomorrow morning's diff. Audit semantics are
otherwise unchanged: each write still records its own audit row, all
stamped with the same ``session_id``.

The sibling pattern this establishes — a domain function that takes the
store + the parsed inputs and raises one error type the route maps to
HTTP 400 — is the shape the serving-recipe setup (issue #38) will reuse,
sharing this error type if it needs a common one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from .storage.config_store import SqliteConfigStore

#: The only units a SKU may be measured in (ADR-0003 decision 3). An editor
#: without this strict vocabulary is a silent-corruption machine — "1 tbsp"
#: of milk means 15 ml, "1 tbsp" of flour means 15 g, and a free-text unit
#: column would let the two drift apart.
_ALLOWED_UNITS = ("g", "ml", "unit")

#: The pack quantity the optional per-unit price is recorded against. The
#: create form collects a price already denominated per unit, so the
#: receipt-shaped cost row behind it is "1 unit at the typed price", no VAT
#: (the partner typed a number, not a receipt). The cost editor's
#: receipt-shaped entry can replace it any time.
_PER_UNIT_PACK_QUANTITY = Decimal("1")


class SkuAuthoringError(ValueError):
    """A create-SKU stroke the module will not persist.

    Subclasses :class:`ValueError` to match the project's other domain
    errors (e.g. :class:`~tangerine.quantity.QuantityError`) — a bad input
    is a value problem, and the route maps it to HTTP 400. The message is
    safe to render to the partner verbatim (the route returns it as the
    response body, as the inline code did before extraction).
    """


@dataclass(frozen=True)
class SkuAuthoringInput:
    """The parsed, ready-to-persist inputs to a create-SKU stroke.

    The route builds this from form fields and hands it to
    :func:`create_sku`; a unit test builds it directly. Identity fields
    (``sku_id``, ``name``, ``item_id``) are whitespace-stripped on
    construction so the contract is self-enforcing regardless of caller —
    a stray space must not defeat the duplicate-sku_id check or write a
    ``"  oat-milk  "`` row to the database. Keeping the parsed shape
    separate from the form means :func:`create_sku` never re-parses
    strings; the only check left to it is the one that needs the store (a
    duplicate sku_id).

    - ``sku_id`` / ``name``    the required identity fields.
    - ``unit``                 one of ``g`` / ``ml`` / ``unit``.
    - ``price_per_unit``       optional net per-unit price in THB; ``None``
                               means "no cost row, derive the SKU's cost
                               later".
    - ``item_id``              optional Loyverse item to map to the new SKU
                               in the same stroke (the item-coverage entry
                               point); ``None`` / empty means "no mapping".
    """

    sku_id: str
    name: str
    unit: str
    price_per_unit: Decimal | None = None
    item_id: str | None = None

    def __post_init__(self) -> None:
        # Frozen dataclass — bypass the freeze to normalise the string fields.
        object.__setattr__(self, "sku_id", self.sku_id.strip())
        object.__setattr__(self, "name", self.name.strip())
        raw_item = self.item_id.strip() if self.item_id is not None else ""
        object.__setattr__(self, "item_id", raw_item or None)


def parse_price(text: str) -> Decimal:
    """Turn a per-unit price string from the create form into a ``Decimal``.

    Raises :class:`SkuAuthoringError` when the text is not a finite,
    non-negative number — the same checks the inline route performed. Kept
    as a free function (rather than baked into :func:`create_sku`) so the
    route can validate the price before building the input and so a unit
    test can exercise it without a store.
    """
    cleaned = text.strip()
    if not cleaned:
        raise SkuAuthoringError("Price must be a number.")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise SkuAuthoringError("Price must be a number.")
    if not value.is_finite() or value < 0:
        raise SkuAuthoringError("Price must be \u2265 0.")
    return value


def create_sku(
    store: SqliteConfigStore,
    inputs: SkuAuthoringInput,
    *,
    created_by: str,
    effective_on: date,
    session_id: str | None = None,
) -> None:
    """Persist a create-SKU stroke (SKU + optional cost + optional mapping).

    Validates the value-level inputs (non-empty id/name, allowed unit,
    non-duplicate sku_id) and writes the stroke: the SKU row always, the
    cost row when ``inputs.price_per_unit`` is set, and the mapping when
    ``inputs.item_id`` is set. When the stroke is more than one write the
    whole thing runs inside one :meth:`store.batch
    <tangerine.storage.config_store.SqliteConfigStore.batch>` block so a
    mid-stroke failure rolls back every write and its audit row.

    Raises :class:`SkuAuthoringError` for any input the module refuses to
    persist; the route maps that to HTTP 400. Store-level failures (a
    constraint violation the validation did not anticipate) propagate as
    whatever the store raises, consistent with the rest of the authoring
    surface.
    """
    if not inputs.sku_id or not inputs.name:
        raise SkuAuthoringError("sku_id and name are required.")
    if inputs.unit not in _ALLOWED_UNITS:
        raise SkuAuthoringError("Unit must be g, ml, or unit.")
    if store.sku(inputs.sku_id) is not None:
        raise SkuAuthoringError(f"SKU {inputs.sku_id} already exists.")

    has_cost = inputs.price_per_unit is not None
    has_mapping = inputs.item_id is not None
    multi_write = has_cost or has_mapping

    def _stroke() -> None:
        store.create_sku(
            inputs.sku_id,
            name=inputs.name,
            unit=inputs.unit,
            created_by=created_by,
            session_id=session_id,
        )
        if has_cost:
            store.save_cost(
                inputs.sku_id,
                pack_price=inputs.price_per_unit,  # type: ignore[arg-type]
                pack_quantity=_PER_UNIT_PACK_QUANTITY,
                vat_inclusive=False,
                updated_by=created_by,
                updated_on=effective_on,
                session_id=session_id,
            )
        if has_mapping:
            store.save_mapping(
                inputs.item_id,  # type: ignore[arg-type]
                inputs.sku_id,
                updated_by=created_by,
                session_id=session_id,
            )

    if multi_write:
        with store.batch():
            _stroke()
    else:
        _stroke()


__all__ = [
    "SkuAuthoringError",
    "SkuAuthoringInput",
    "create_sku",
    "parse_price",
]
