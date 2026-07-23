"""Issue #66 — Loyverse ``created_at`` is UTC; bucket and shift-stamp locally.

The venue is in Phuket (Asia/Bangkok, UTC+7). Cafe is 8am–5pm local, bar is
5pm–10pm local. The parser must convert the UTC ``created_at`` to Asia/Bangkok
before taking ``.date()`` (for the sale's day bucket) and before
``segment_for_timestamp`` (for the shift fallback), otherwise:

- a sale at local 02:00 on the 14th (the small hours of the 13th's bar shift)
  buckets to the 13th, not the 14th;
- a sale at local 18:00 (the heart of the bar shift) is 11:00 UTC, which falls
  inside ``[8, 17)`` and is shift-stamped *cafe* — exactly backwards for half
  the day.

These tests pin the local-time semantics. They live in their own module so the
#66 fix is legible as a unit; the existing slice-02 sync tests stay valid
because their worked examples (09:15 UTC → 16:15 local) land on the same date
and segment either way.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from tangerine.loyverse.parser import parse_receipts_to_sales
from tangerine.types import Segment


def _receipt(
    *,
    receipt_number: str,
    created_at: str,
    line_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "receipt_number": receipt_number,
        "receipt_type": "SALE",
        "refund_for": None,
        "created_at": created_at,
        "receipt_date": created_at,
        "total_money": 120,
        "total_tax": 0,
        "line_items": line_items
        or [
            {
                "id": "li-1",
                "item_id": "i-1",
                "variant_id": "v-1",
                "sku": "chang-draft-500",
                "quantity": 1,
                "price": 120,
            }
        ],
    }


# --- shift-stamp: UTC hour that lands inside cafe, but local hour is bar -------


def test_evening_bar_sale_is_not_shift_stamped_as_cafe() -> None:
    """Local 18:00 (the heart of the bar shift) is 11:00 UTC.

    ``[8, 17)`` in UTC hours would stamp this *cafe* — exactly the bug #66
    flags. After the fix the conversion happens first, so the local 18:00 hour
    falls outside ``[8, 17)`` and the sale stamps *bar*.
    """
    payload = {"receipts": [_receipt(receipt_number="66-1", created_at="2026-07-14T11:00:00.000Z")]}

    records = parse_receipts_to_sales(payload)

    assert len(records) == 1
    assert records[0].sale.segment is Segment.BAR


def test_morning_cafe_sale_is_not_shift_stamped_as_bar() -> None:
    """Local 09:00 (mid-morning cafe) is 02:00 UTC.

    ``[8, 17)`` in UTC hours would stamp this *bar* (2 < 8). After the fix the
    conversion happens first, so the local 09:00 hour falls inside ``[8, 17)``
    and the sale stamps *cafe*.
    """
    payload = {"receipts": [_receipt(receipt_number="66-2", created_at="2026-07-14T02:00:00.000Z")]}

    records = parse_receipts_to_sales(payload)

    assert len(records) == 1
    assert records[0].sale.segment is Segment.CAFE


# --- day bucket: a local-time sale that crosses the UTC date boundary ---------


def test_late_evening_sale_buckets_to_the_local_day_not_the_prior_utc_day() -> None:
    """Local 23:30 on the 14th is 16:30 UTC the same day.

    The UTC date and the local date agree here, so this is the easy case: the
    bucket is the 14th under both readings. Pinned anyway to make sure the
    fix does not regress the obvious case while fixing the boundary one.
    """
    payload = {"receipts": [_receipt(receipt_number="66-3", created_at="2026-07-14T16:30:00.000Z")]}

    records = parse_receipts_to_sales(payload)

    assert records[0].sale.timestamp == date(2026, 7, 14)


def test_early_morning_sale_buckets_to_the_local_day_not_the_prior_utc_day() -> None:
    """Local 02:00 on the 14th is 19:00 UTC on the 13th.

    This is the boundary case that motivated the ticket. Under the old code the
    sale bucketed to the 13th (the UTC date); the bar shift that closed at
    22:00 local on the 13th *appeared* to spill a 02:00 nightcap into the next
    day's books. After the fix the conversion happens first, so the bucket is
    the local 14th.
    """
    payload = {"receipts": [_receipt(receipt_number="66-4", created_at="2026-07-13T19:00:00.000Z")]}

    records = parse_receipts_to_sales(payload)

    assert records[0].sale.timestamp == date(2026, 7, 14)


def test_early_morning_sale_shift_stamps_bar_under_local_time() -> None:
    """Local 02:00 on the 14th (19:00 UTC on the 13th) is out-of-hours → bar.

    Combines the two halves of the fix on one receipt: the bucket is the local
    14th (above) and the segment is *bar* (02:00 is outside ``[8, 17)`` local).
    Under the old code this sale bucketed to the 13th *and* stamped *bar*
    (19 > 17 in UTC) — wrong on date, right on segment, by coincidence.
    """
    payload = {"receipts": [_receipt(receipt_number="66-5", created_at="2026-07-13T19:00:00.000Z")]}

    records = parse_receipts_to_sales(payload)

    assert records[0].sale.timestamp == date(2026, 7, 14)
    assert records[0].sale.segment is Segment.BAR


# --- cross-month boundary -----------------------------------------------------


def test_sale_crossing_a_month_boundary_buckets_to_the_local_month() -> None:
    """Local 02:00 on 1 August is 19:00 UTC on 31 July.

    Under the old code this sale bucketed to 31 July — the bar shift that
    closed at 22:00 local on 31 July spilled its late nightcap into the wrong
    month. After the fix the bucket is the local 1 August.
    """
    payload = {"receipts": [_receipt(receipt_number="66-6", created_at="2026-07-31T19:00:00.000Z")]}

    records = parse_receipts_to_sales(payload)

    assert records[0].sale.timestamp == date(2026, 8, 1)
