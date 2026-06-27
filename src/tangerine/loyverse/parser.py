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


class LoyverseParseError(Exception):
    """Raised when a Loyverse payload can't be turned into a clean Sale.

    Used for values the sync would otherwise silently mangle — e.g. a
    fractional or non-positive line quantity, which the margin engine (integer
    quantities) cannot represent honestly. Surfacing these as an error keeps
    bad data out of the books; the daily review can show them for a partner.
    """


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


def _line_quantity(raw: Any, receipt_number: str, line_id: str) -> int:
    """Validate a Loyverse line quantity, returning it as a positive int.

    The margin engine represents quantities as ``int``. Loyverse quantities are
    integers for the items this venue sells (beer pours, coffees); a fractional
    or non-positive quantity means either unexpected data (weight items we don't
    carry) or a malformed payload. Either way we refuse to truncate silently —
    ``int(2.9)`` would lose revenue, and ``int(0.5)`` would store a zero-
    quantity sale. Instead raise so the bad line surfaces for review.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise LoyverseParseError(
            f"receipt {receipt_number!r} line {line_id!r}: quantity {raw!r} "
            "is not a number"
        )
    if raw <= 0:
        raise LoyverseParseError(
            f"receipt {receipt_number!r} line {line_id!r}: quantity {raw!r} "
            "must be positive"
        )
    if isinstance(raw, float) and not raw.is_integer():
        raise LoyverseParseError(
            f"receipt {receipt_number!r} line {line_id!r}: fractional quantity "
            f"{raw!r} cannot be represented as an integer sale unit"
        )
    return int(raw)


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
            line_id = line.get("id", "")
            qty = _line_quantity(
                line.get("quantity", 1), receipt_number, line_id
            )
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
