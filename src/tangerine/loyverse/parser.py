"""Pure parsers from raw Loyverse JSON to domain types (slice 02).

These functions are pure: raw payload in, ``Sale`` / ``MenuSnapshot`` out. They
are the single place money crosses from ``float`` (Loyverse JSON numbers) to
``Decimal`` (the rest of the codebase), so float drift is confined to this
boundary.

Conventions mirrored from the Loyverse API:

- A receipt's ``created_at`` is the transaction timestamp in UTC; sales carry
  the ``date`` portion of it **converted to the venue's local timezone**
  (Asia/Bangkok, UTC+7 — issue #66). The PRD's "stored with their Loyverse
  transaction timestamp" is honoured at local granularity: a 02:00 local
  nightcap (19:00 UTC the prior day) belongs to the local calendar day it
  happened on, not the UTC one. The shift fallback (cafe ``[8, 17)`` local,
  else bar) is stamped from the same local timestamp.
- A line item's identity is its ``sku`` (falling back to ``item_id``) — that is
  the value recipes map onto in slice 04.
- REFUND receipts are excluded from sales for now (refund handling is a later
  slice); polling must not count a refund as fresh revenue.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..types import Money, Sale, Segment
from ..segments import segment_for_timestamp
from .payloads import LoyverseItem, LoyverseLineItem, LoyverseVariant
from .store import DEFAULT_CAFE_CATEGORY_IDS, MenuItem, MenuSnapshot, SaleRecord

#: The venue's local timezone. Loyverse ``created_at`` is always UTC; every
#: venue-facing decision (which day a sale belongs to, which shift it falls in)
#: is local. Bangkok has no DST, so this is a fixed +07:00 offset that holds
#: forever — there is no clock-change edge case to track.
VENUE_TIMEZONE = ZoneInfo("Asia/Bangkok")


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
        created_dt = _parse_created_at(receipt["created_at"])
        # Issue #66: bucket the sale date and shift-stamp the segment in venue
        # local time, not UTC. Loyverse ``created_at`` is UTC; the venue is in
        # Phuket (UTC+7). Without this conversion, an 18:00 local bar sale
        # (11:00 UTC) stamps *cafe* under the ``[8, 17)`` cafe window, and a
        # 02:00 local nightcap (19:00 UTC the prior day) buckets to the wrong
        # calendar day and across month boundaries. Bangkok has no DST so the
        # conversion is a fixed +07:00 forever.
        local_dt = created_dt.astimezone(VENUE_TIMEZONE)
        created = local_dt.date()
        # Shift-timestamp fallback (slice 07): stamp the segment from the
        # transaction time so an unmapped sale (no recipe -> no category
        # segment) can still be tagged cafe/bar. A mapped sale's recipe
        # segment overrides this at margin time.
        shift_segment = segment_for_timestamp(local_dt)
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
                        segment=shift_segment,
                    ),
                    receipt_number=receipt_number,
                    line_id=line_id,
                    # The raw UTC timestamp, kept so a future fix can re-derive
                    # ``Sale.timestamp``/``segment`` from it without re-fetching
                    # the receipt (issue #66's migration story).
                    created_at_utc=created_dt,
                )
            )
    return records


def variant_price(variant: LoyverseVariant, *, store_id: str | None = None) -> Decimal | None:
    """Resolve one Loyverse variant's price, or ``None`` if it has none on record.

    Loyverse has no flat ``price`` field on a variant: a ``FIXED``-priced
    variant carries it in ``default_price``; a per-store-priced variant
    (this venue's actual configuration) carries it in ``stores``, one entry
    per store. Falls back to the first ``stores`` entry when ``store_id``
    is unset — this venue has a single store, so that is always correct
    here (see ``LoyverseVariant``'s docstring for how this was confirmed).

    Shared by the sync parser (:func:`parse_items_snapshot`) and the
    recipe-authoring worksheet (``scripts/dump_loyverse_items.py``) so the
    two callers cannot read a variant's price differently from each other.
    """
    default = variant.get("default_price")
    if default is not None:
        return _money(default)
    for store in variant.get("stores") or []:
        if store_id is None or store.get("store_id") == store_id:
            price = store.get("price")
            if price is not None:
                return _money(price)
    return None


def parse_items_snapshot(
    payload: dict[str, Any],
    *,
    store_id: str | None = None,
    cafe_category_ids: frozenset[str] = DEFAULT_CAFE_CATEGORY_IDS,
) -> MenuSnapshot:
    """Turn an ``/items`` response into a ``MenuSnapshot`` (current menu).

    One ``MenuItem`` per item *variant*, keyed by the same identity a receipt
    line carries: the variant's ``sku``, falling back to the Loyverse item id
    (mirroring :func:`_line_item_id`). Loyverse sells variants, not items —
    recipe mappings key on variant SKUs and sales are stored under them — so
    menu rows must share that identity or nothing downstream (item coverage,
    the daily review's deep links) can join a menu row to a mapping or a
    sale. A multi-variant item (e.g. two sizes with their own SKUs and
    prices) yields one row per variant; an item with no variants still
    yields one row, keyed by the item id and priced at zero.

    Segment is cafe when the item's ``category_id`` is in ``cafe_category_ids``
    (the configured set of Loyverse cafe category UUIDs), else bar. The
    default empty set tags every item bar — the honest restatement of the
    slice-02 placeholder bug (ADR-0009). Under pure-clock segmentation
    (#65 / ADR-0007) this segment no longer drives revenue splitting, but it
    still feeds menu-shape views (``/items``, ``/skus``) and the sold-as-is
    quick-create (which inherits it onto the sold SKU), so correctness here
    matters even after the clock won the revenue-side call.

    ``store_id`` disambiguates a variant's per-store price (see
    :func:`variant_price`); the sync orchestrator passes the configured
    credentials' store id.
    """
    raw_items = payload.get("items", [])
    menu_items: list[MenuItem] = []
    for raw in raw_items:
        item_id = raw.get("id", "")
        name = raw.get("item_name", "")
        segment = (
            Segment.CAFE
            if raw.get("category_id") in cafe_category_ids
            else Segment.BAR
        )
        variants = raw.get("variants") or []
        if not variants:
            menu_items.append(
                MenuItem(
                    item_id=item_id,
                    name=name,
                    sell_price=Decimal("0"),
                    segment=segment,
                )
            )
            continue
        for variant in variants:
            price = variant_price(variant, store_id=store_id)
            menu_items.append(
                MenuItem(
                    item_id=variant.get("sku") or item_id,
                    name=name or variant.get("option1_value", ""),
                    sell_price=price if price is not None else Decimal("0"),
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
    "variant_price",
    "receipts_cursor",
    "items_cursor",
]
