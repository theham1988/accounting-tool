"""Pure parsers from raw Loyverse JSON to domain types (slice 02).

These functions are pure: raw payload in, ``Sale`` / ``MenuSnapshot`` out. They
are the single place money crosses from ``float`` (Loyverse JSON numbers) to
``Decimal`` (the rest of the codebase), so float drift is confined to this
boundary.

Conventions mirrored from the Loyverse API:

- A receipt's ``created_at`` is the transaction timestamp; sales carry the
  ``date`` portion of it (the PRD says "stored with their Loyverse transaction
  timestamp"; the margin engine keys on ``date``).
- A line item's identity is its ``sku`` (falling back to ``item_id``) — that is
  the value recipes map onto in slice 04.
- REFUND receipts are excluded from sales for now (refund handling is a later
  slice); polling must not count a refund as fresh revenue.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..types import Money, Sale, Segment
from .payloads import LoyverseItem, LoyverseLineItem
from .store import CAFE_CATEGORY_ID, MenuItem, MenuSnapshot, SaleRecord


def _money(v: Any) -> Money:
    """Convert a Loyverse JSON number to ``Decimal`` via ``str`` to avoid drift.

    ``Decimal(0.1)`` is not ``Decimal("0.1")``; going through ``str`` gives the
    intended value. Loyverse money is THB with at most two decimals.
    """
    return Money(str(v))


def _parse_created_at(raw: str) -> datetime:
    """Parse a Loyverse ISO-8601 ``created_at`` (always UTC, trailing ``Z``)."""
    # ``datetime.fromisoformat`` (3.11+) accepts the trailing ``Z``.
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _line_item_id(line: LoyverseLineItem) -> str:
    """The id recipes will map onto: sku if present, else item_id."""
    sku = line.get("sku")
    if sku:
        return sku
    return line.get("item_id", "")


def parse_receipts_to_sales(payload: dict[str, Any]) -> list[SaleRecord]:
    """Turn a ``/receipts`` response into ``SaleRecord``s.

    REFUND receipts are skipped. Each remaining SALE line becomes one record
    carrying its Loyverse ``(receipt_number, line_id)`` identity so the store
    can dedupe idempotently even when two different sales collide on value
    (same SKU/day/price/qty). Quantity is taken from the line (defaults to 1).
    Records are returned in payload order.
    """
    receipts = payload.get("receipts", [])
    records: list[SaleRecord] = []
    for receipt in receipts:
        if receipt.get("receipt_type", "SALE") == "REFUND":
            continue
        receipt_number = receipt.get("receipt_number", "")
        created = _parse_created_at(receipt["created_at"]).date()
        for line in receipt.get("line_items", []):
            quantity = line.get("quantity", 1)
            # Loyverse quantity may be fractional (e.g. weight items); the
            # margin engine uses int quantity. We floor to whole units for
            # beer/coffee which are always integer-quantity sales at this venue.
            qty = int(quantity) if isinstance(quantity, (int, float)) else 1
            line_id = line.get("id", "")
            records.append(
                SaleRecord(
                    sale=Sale(
                        item_id=_line_item_id(line),
                        timestamp=created,
                        sell_price=_money(line.get("price", 0)),
                        quantity=qty,
                    ),
                    receipt_number=receipt_number,
                    line_id=line_id,
                )
            )
    return records


def _first_variant_price(item: LoyverseItem) -> Decimal:
    variants = item.get("variants") or []
    if not variants:
        return Decimal("0")
    return _money(variants[0].get("price", 0))


def _item_name(item: LoyverseItem) -> str:
    name = item.get("item_name")
    if name:
        return name
    variants = item.get("variants") or []
    if variants:
        return variants[0].get("name", "")
    return ""


def parse_items_snapshot(payload: dict[str, Any]) -> MenuSnapshot:
    """Turn an ``/items`` response into a ``MenuSnapshot`` (current menu).

    One ``MenuItem`` per Loyverse item, keyed by Loyverse item id. Segment is
    cafe when the item's category is the cafe category, else bar.
    """
    raw_items = payload.get("items", [])
    menu_items: list[MenuItem] = []
    for raw in raw_items:
        item_id = raw.get("id", "")
        segment = (
            Segment.CAFE
            if raw.get("category_id") == CAFE_CATEGORY_ID
            else Segment.BAR
        )
        menu_items.append(
            MenuItem(
                item_id=item_id,
                name=_item_name(raw),
                sell_price=_first_variant_price(raw),
                segment=segment,
            )
        )
    menu_items.sort(key=lambda mi: mi.item_id)
    return MenuSnapshot(items=tuple(menu_items))


def receipts_cursor(payload: dict[str, Any]) -> str | None:
    """Extract the pagination cursor from a receipts response (None if last)."""
    cur = payload.get("cursor")
    return cur or None


def items_cursor(payload: dict[str, Any]) -> str | None:
    cur = payload.get("cursor")
    return cur or None


# Re-exported for callers that build payloads by hand in tests.
__all__ = [
    "parse_receipts_to_sales",
    "parse_items_snapshot",
    "receipts_cursor",
    "items_cursor",
]
