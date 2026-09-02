"""Unit seam for the receipt-grain parser and the pure five-number
aggregation (issue #147; semantics locked in the #142 resolution).

These tests pin the derivation's hard rules as worked examples:

- payments route through the channel map; unknown id = ``LoyverseParseError``
- the per-receipt integrity assert: ``Σ payments == total_money`` or error
- ``receipt_type`` outside {SALE, REFUND} = error
- refunds contribute negatives on their own local day and channel (P-11)
- discount = Σ ``total_discounts`` (never signed by refunds)
- trading day = any-receipt local day; zeros inside; no row for receiptless
  days (aggregation level)
- idempotent storage on ``receipt_number``
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from tangerine.loyverse.config import PaymentChannelMap
from tangerine.loyverse.derive import derive_five_numbers
from tangerine.loyverse.parser import LoyverseParseError, parse_receipt_facts
from tangerine.loyverse.store import InMemoryLoyverseStore, ReceiptFact

CHANNELS = PaymentChannelMap(
    channels={"pay-cash": "cash", "pay-transfer": "qr", "pay-card": "card"}
)


def _receipt(
    *,
    receipt_number: str = "4-10000",
    receipt_type: str = "SALE",
    created_at: str = "2026-07-01T06:30:00.000Z",  # 13:30 Bangkok
    total_money: float = 120.0,
    payments: list[dict] | None = None,
    total_discounts: list[dict] | None = None,
) -> dict:
    if payments is None:
        payments = [{"payment_type_id": "pay-cash", "money_amount": 120.0}]
    return {
        "receipt_number": receipt_number,
        "receipt_type": receipt_type,
        "created_at": created_at,
        "total_money": total_money,
        "payments": payments,
        "total_discounts": total_discounts or [],
    }


def test_split_tender_routes_by_payment_type() -> None:
    facts = parse_receipt_facts(
        {
            "receipts": [
                _receipt(
                    total_money=350.0,
                    payments=[
                        {"payment_type_id": "pay-cash", "money_amount": 150.0},
                        {"payment_type_id": "pay-card", "money_amount": 200.0},
                    ],
                )
            ]
        },
        CHANNELS,
    )
    assert len(facts) == 1
    f = facts[0]
    assert (f.cash, f.qr, f.card) == (D("150"), D("0"), D("200"))
    assert f.local_date == date(2026, 7, 1)


def test_till_qr_is_the_transfer_tender() -> None:
    facts = parse_receipt_facts(
        {"receipts": [_receipt(
            total_money=90.0,
            payments=[{"payment_type_id": "pay-transfer", "money_amount": 90.0}],
        )]},
        CHANNELS,
    )
    assert facts[0].qr == D("90")


def test_unmapped_payment_type_is_hard_error() -> None:
    with pytest.raises(LoyverseParseError, match="not mapped to a channel"):
        parse_receipt_facts(
            {"receipts": [_receipt(
                total_money=120.0,
                payments=[{"payment_type_id": "uuid-unknown", "money_amount": 120.0}],
            )]},
            CHANNELS,
        )


def test_empty_channel_map_routes_nothing() -> None:
    """The configured-but-empty map makes every payment a hard error."""
    with pytest.raises(LoyverseParseError):
        parse_receipt_facts(
            {"receipts": [_receipt()]},
            PaymentChannelMap(channels={}),
        )


def test_payments_total_mismatch_is_hard_error() -> None:
    with pytest.raises(LoyverseParseError, match="payments sum"):
        parse_receipt_facts(
            {"receipts": [_receipt(
                total_money=130.0,
                payments=[{"payment_type_id": "pay-cash", "money_amount": 120.0}],
            )]},
            CHANNELS,
        )


def test_unknown_receipt_type_is_hard_error() -> None:
    with pytest.raises(LoyverseParseError, match="unknown receipt_type"):
        parse_receipt_facts(
            {"receipts": [_receipt(receipt_type="VOID")]},
            CHANNELS,
        )


def test_refund_contributes_negatives_own_day_own_channel() -> None:
    """P-11: a refund nets negative on its own local day and channel."""
    facts = parse_receipt_facts(
        {
            "receipts": [
                _receipt(
                    receipt_number="4-10340",
                    receipt_type="REFUND",
                    created_at="2026-07-26T04:46:00.000Z",  # 11:46 Bangkok
                    total_money=-180.0,
                    payments=[{"payment_type_id": "pay-cash", "money_amount": -180.0}],
                )
            ]
        },
        CHANNELS,
    )
    f = facts[0]
    assert f.receipt_type == "REFUND"
    assert f.cash == D("-180")
    assert f.discount == D("0")


def test_discount_sums_total_discounts_all_scopes() -> None:
    """Both scopes live in ``total_discounts`` — one family, one sum."""
    facts = parse_receipt_facts(
        {"receipts": [_receipt(
            total_money=70.0,
            payments=[{"payment_type_id": "pay-cash", "money_amount": 70.0}],
            total_discounts=[
                {"scope": "RECEIPT", "money_amount": 20.0},
                {"scope": "LINE_ITEM", "money_amount": 30.0},
            ],
        )]},
        CHANNELS,
    )
    assert facts[0].discount == D("50")


def test_local_day_boundary_uses_bangkok_calendar() -> None:
    """A 19:05 UTC receipt (02:05 next-day Bangkok) buckets to the next day."""
    facts = parse_receipt_facts(
        {"receipts": [_receipt(created_at="2026-07-01T19:05:00.000Z")]},
        CHANNELS,
    )
    assert facts[0].local_date == date(2026, 7, 2)


# --- the pure aggregation ----------------------------------------------------


def _fact(**overrides: object) -> ReceiptFact:
    defaults: dict = {
        "receipt_number": "r-1",
        "receipt_type": "SALE",
        "local_date": date(2026, 7, 1),
        "cash": D("100"),
        "qr": D("0"),
        "card": D("0"),
        "discount": D("0"),
        "total_money": D("100"),
    }
    defaults.update(overrides)
    return ReceiptFact(**defaults)


def test_derivation_emits_five_tuple_with_zeros() -> None:
    days = derive_five_numbers([_fact(cash=D("100"))])
    assert len(days) == 1
    d = days[0]
    assert (d.date, d.cash, d.qr, d.card, d.discount) == (
        "2026-07-01",
        D("100"),
        D("0"),
        D("0"),
        D("0"),
    )


def test_derivation_omits_receiptless_days() -> None:
    """A closed day with zero receipts emits nothing — not zeros."""
    days = derive_five_numbers([
        _fact(receipt_number="r-1", local_date=date(2026, 7, 1)),
        _fact(receipt_number="r-2", local_date=date(2026, 7, 3)),
    ])
    assert [d.date for d in days] == ["2026-07-01", "2026-07-03"]


def test_derivation_refund_only_day_can_go_negative() -> None:
    days = derive_five_numbers([
        _fact(
            receipt_number="r-ref",
            receipt_type="REFUND",
            cash=D("-180"),
            total_money=D("-180"),
        )
    ])
    assert days[0].cash == D("-180")


def test_derivation_orders_days_ascending() -> None:
    days = derive_five_numbers([
        _fact(receipt_number="r-2", local_date=date(2026, 7, 3)),
        _fact(receipt_number="r-1", local_date=date(2026, 7, 1)),
    ])
    assert [d.date for d in days] == ["2026-07-01", "2026-07-03"]


def test_store_is_idempotent_on_receipt_number() -> None:
    store = InMemoryLoyverseStore()
    fact = _fact()
    store.record_receipt_facts([fact])
    store.record_receipt_facts([fact])
    assert len(store.receipt_facts()) == 1
