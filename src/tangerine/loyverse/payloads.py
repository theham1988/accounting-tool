"""Raw Loyverse API JSON shapes, as ``TypedDict``s (slice 02).

These are deliberately total-but-minimal: only the fields the sync consumes.
They mirror the Loyverse v1.0 API response documented at
``https://api.loyverse.com/v1.0/{receipts,items}``. Because Loyverse responses
include many fields we do not read, each line/receipt/item dict uses
``total=False`` so missing keys are not flagged — we only read keys we model.

Money in Loyverse payloads is a JSON number (THB, two decimals). The parser
converts every money value to ``Decimal`` at the boundary so the rest of the
codebase never sees a float.
"""

from __future__ import annotations

from typing import Any, TypedDict


class LoyverseLineItem(TypedDict, total=False):
    id: str
    item_id: str
    variant_id: str
    item_name: str
    variant_name: str | None
    sku: str | None
    quantity: float
    price: float
    gross_total_money: float
    total_money: float
    cost: float
    cost_total: float


class LoyversePayment(TypedDict, total=False):
    """One tender on a receipt (``payments[]``, issue #147).

    ``payment_type_id`` is an opaque account UUID (like the cafe category
    ids, ADR-0009) — Loyverse's built-ins ("Cash", "Card") and the venue's
    custom types (the till-QR tender named "Transfer") all surface as
    UUIDs, and the id→channel routing lives in env configuration, never
    the repo. ``name`` is documentation only; the derivation never routes
    money by it. ``money_amount`` is signed: negative on REFUND receipts.
    """

    payment_type_id: str
    name: str
    money_amount: float


class LoyverseDiscount(TypedDict, total=False):
    """One discount applied to a receipt (``total_discounts[]``, #147).

    Loyverse folds both scopes (RECEIPT and LINE_ITEM) into
    ``total_discounts``; summing ``line_discounts`` too would
    double-count, so this is the only discount family the derivation
    reads. Amounts are positive as exported; REFUND receipts never carry
    discounts (Loyverse refunds the discounted amount actually paid).
    """

    id: str
    name: str
    scope: str  # "RECEIPT" | "LINE_ITEM"
    money_amount: float


class LoyverseReceipt(TypedDict, total=False):
    receipt_number: str
    receipt_type: str  # "SALE" | "REFUND"
    refund_for: str | None
    created_at: str    # ISO 8601, e.g. "2026-06-24T09:15:00.000Z"
    receipt_date: str
    total_money: float
    total_tax: float
    line_items: list[LoyverseLineItem]
    payments: list[LoyversePayment]
    total_discounts: list[LoyverseDiscount]


class ReceiptsResponse(TypedDict, total=False):
    receipts: list[LoyverseReceipt]
    cursor: str | None


class LoyverseVariantStore(TypedDict, total=False):
    store_id: str
    price: float


class LoyverseVariant(TypedDict, total=False):
    """A Loyverse item variant, as returned by ``/items`` and ``/variants``.

    There is no flat ``price`` field: a ``FIXED``-priced variant carries its
    price in ``default_price``; a per-store-priced variant (this venue's
    actual configuration — every real variant has ``default_price`` ``None``)
    carries it in ``stores``, one entry per store. Confirmed against this
    venue's real Loyverse account via ``scripts/dump_loyverse_items.py``.
    There is also no variant-level ``name``; ``option1_value`` is the closest
    analogue (the value of the item's first configured option, e.g. a size).
    """

    variant_id: str
    option1_value: str
    sku: str | None
    default_price: float | None
    stores: list[LoyverseVariantStore]


class LoyverseItem(TypedDict, total=False):
    id: str
    item_name: str
    category_id: str
    sku: str | None
    variants: list[LoyverseVariant]


class ItemsResponse(TypedDict, total=False):
    items: list[LoyverseItem]
    cursor: str | None


# The raw envelope returned by any list endpoint is either {"receipts": ...} or
# {"items": ...}; callers index by the known key. This alias makes the parser
# signature honest about the shape it accepts.
RawPayload = dict[str, Any]
