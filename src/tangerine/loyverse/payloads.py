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


class LoyverseReceipt(TypedDict, total=False):
    receipt_number: str
    receipt_type: str  # "SALE" | "REFUND"
    refund_for: str | None
    created_at: str    # ISO 8601, e.g. "2026-06-24T09:15:00.000Z"
    receipt_date: str
    total_money: float
    total_tax: float
    line_items: list[LoyverseLineItem]


class ReceiptsResponse(TypedDict, total=False):
    receipts: list[LoyverseReceipt]
    cursor: str | None


class LoyverseVariant(TypedDict, total=False):
    id: str
    name: str
    sku: str | None
    price: float


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
