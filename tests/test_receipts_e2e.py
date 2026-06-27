"""End-to-end receipt ingestion test seam (slice 03).

These tests are the contract for the receipt pipeline. Per the PRD testing
rules they are readable as worked examples — "given a receipt whose lines
plus VAT do not reconcile to the total, the pipeline auto-rejects it" —
and they do not mock internal modules. The only mocked boundary is the
OCR/LLM provider, which is a genuine external boundary (the extractor).

Flow under test (docs/issues/03-receipt-ingestion-pipeline.md):

    upload -> OCR -> sum-check -> price-check/SKU-check -> queue -> decision
        -> stored purchase + last_known_price update
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tangerine.approvals import ApprovalBook, apply_decision
from tangerine.receipts import check_receipt
from tangerine.seeded_receipts import SeededReceiptSource
from tangerine.types import (
    ExtractedReceipt,
    ExtractedReceiptLine,
    LastKnownPrice,
    LineFlag,
    Purchase,
    PurchaseLine,
    ReceiptDecision,
    ReceiptState,
    Sku,
    Supplier,
)

D = Decimal


# --- shared fixtures --------------------------------------------------------


@pytest.fixture
def day() -> date:
    return date(2026, 6, 24)


@pytest.fixture
def chang_sku() -> Sku:
    return Sku(sku_id="chang-keg", name="Chang draught beer", unit="ml")


@pytest.fixture
def beer_supplier() -> Supplier:
    return Supplier(supplier_id="phuket-beverages", name="Phuket Beverages Co.")


def _receipt(
    supplier_id: str,
    invoice_date: date,
    lines: tuple[ExtractedReceiptLine, ...],
    vat: Decimal,
    total: Decimal,
) -> ExtractedReceipt:
    return ExtractedReceipt(
        supplier_id=supplier_id,
        invoice_date=invoice_date,
        lines=lines,
        vat=vat,
        total=total,
    )


def _approve() -> ReceiptDecision:
    return ReceiptDecision(decision=ReceiptState.APPROVED)


def _correct(lines: tuple[ExtractedReceiptLine, ...]) -> ReceiptDecision:
    return ReceiptDecision(decision=ReceiptState.APPROVED, corrected_lines=lines)


def _reject() -> ReceiptDecision:
    return ReceiptDecision(decision=ReceiptState.REJECTED)


# --- 1. sum-check -----------------------------------------------------------


def test_sum_check_auto_rejects_when_lines_plus_vat_differ_from_total(
    day: date, beer_supplier: Supplier
) -> None:
    """Lines sum to 4000, VAT is 280 -> 4280 expected; stated total is 4300.

    Outside the 1.00 THB tolerance, so the receipt must auto-reject and must
    NOT reach the approval queue.
    """
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="Chang draught keg 30L",
                quantity=D("1"),
                unit_price=D("4000"),
                sku_id="chang-keg",
            ),
        ),
        vat=D("280"),
        total=D("4300"),
    )

    result = check_receipt(receipt, skus={}, reference_prices={})

    assert result.state == ReceiptState.AUTO_REJECTED
    assert result.rejection_reason is not None
    assert result.lines == ()


def test_sum_check_passes_within_tolerance(day: date, beer_supplier: Supplier) -> None:
    """Lines sum to 4000 + VAT 280 = 4280; stated total 4280.50 is within 1 THB."""
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="Chang draught keg 30L",
                quantity=D("1"),
                unit_price=D("4000"),
                sku_id="chang-keg",
            ),
        ),
        vat=D("280"),
        total=D("4280.50"),
    )

    result = check_receipt(receipt, skus={}, reference_prices={})

    assert result.state == ReceiptState.QUEUED


# --- 2. reference-price check ----------------------------------------------


def test_price_deviation_over_5_percent_flags_for_review(
    day: date, beer_supplier: Supplier, chang_sku: Sku
) -> None:
    """Last known price for (chang-keg, supplier) is 0.07/ml. Extracted unit
    price is 0.10/ml -> ~43% deviation, well over the 5% threshold, so the line
    is flagged with PRICE_DEVIATION and the receipt is queued (not auto-rejected).
    """
    skus = {chang_sku.sku_id: chang_sku}
    reference = {
        (chang_sku.sku_id, beer_supplier.supplier_id): LastKnownPrice(
            price=D("0.07"), updated_at=date(2026, 6, 1)
        )
    }
    # 1 ml line item priced at 0.10 — artificial to keep the sum check trivial.
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="1 ml sample",
                quantity=D("1"),
                unit_price=D("0.10"),
                sku_id=chang_sku.sku_id,
            ),
        ),
        vat=D("0"),
        total=D("0.10"),
    )

    result = check_receipt(receipt, skus=skus, reference_prices=reference)

    assert result.state == ReceiptState.QUEUED
    assert result.lines[0].flags == (LineFlag.PRICE_DEVIATION,)


def test_price_within_5_percent_does_not_flag(
    day: date, beer_supplier: Supplier, chang_sku: Sku
) -> None:
    """Last known 0.07; extracted 0.072 -> ~2.9% deviation, under threshold."""
    skus = {chang_sku.sku_id: chang_sku}
    reference = {
        (chang_sku.sku_id, beer_supplier.supplier_id): LastKnownPrice(
            price=D("0.07"), updated_at=date(2026, 6, 1)
        )
    }
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="1 ml sample",
                quantity=D("1"),
                unit_price=D("0.072"),
                sku_id=chang_sku.sku_id,
            ),
        ),
        vat=D("0"),
        total=D("0.072"),
    )

    result = check_receipt(receipt, skus=skus, reference_prices=reference)

    assert result.state == ReceiptState.QUEUED
    assert result.lines[0].flags == ()


def test_line_with_no_reference_price_does_not_flag(
    day: date, beer_supplier: Supplier, chang_sku: Sku
) -> None:
    """First-time purchase: no last_known_price for this (SKU, supplier).

    Nothing to deviate from, so the line is not flagged on price grounds.
    (It may still queue for other reasons, e.g. missing SKU mapping.)
    """
    skus = {chang_sku.sku_id: chang_sku}
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="1 ml sample",
                quantity=D("1"),
                unit_price=D("0.07"),
                sku_id=chang_sku.sku_id,
            ),
        ),
        vat=D("0"),
        total=D("0.07"),
    )

    result = check_receipt(receipt, skus=skus, reference_prices={})

    assert result.state == ReceiptState.QUEUED
    assert result.lines[0].flags == ()


# --- 3. SKU mapping ---------------------------------------------------------


def test_line_without_sku_mapping_is_always_queued(
    day: date, beer_supplier: Supplier
) -> None:
    """A line the OCR could not map to any SKU queues for review regardless of
    its price (there is no reference price to check against)."""
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="Unknown mystery item",
                quantity=D("2"),
                unit_price=D("50"),
                sku_id=None,
            ),
        ),
        vat=D("0"),
        total=D("100"),
    )

    result = check_receipt(receipt, skus={}, reference_prices={})

    assert result.state == ReceiptState.QUEUED
    assert result.lines[0].flags == (LineFlag.UNMAPPED_SKU,)


def test_mapped_and_unmapped_lines_in_same_receipt_each_flag_independently(
    day: date, beer_supplier: Supplier, chang_sku: Sku
) -> None:
    """A receipt with one mapped, in-price line and one unmapped line is queued
    with flags only on the unmapped line."""
    skus = {chang_sku.sku_id: chang_sku}
    reference = {
        (chang_sku.sku_id, beer_supplier.supplier_id): LastKnownPrice(
            price=D("0.07"), updated_at=date(2026, 6, 1)
        )
    }
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="1 ml sample",
                quantity=D("1"),
                unit_price=D("0.07"),
                sku_id=chang_sku.sku_id,
            ),
            ExtractedReceiptLine(
                description="Mystery item",
                quantity=D("1"),
                unit_price=D("0.05"),
                sku_id=None,
            ),
        ),
        vat=D("0"),
        total=D("0.12"),
    )

    result = check_receipt(receipt, skus=skus, reference_prices=reference)

    assert result.state == ReceiptState.QUEUED
    assert result.lines[0].flags == ()
    assert result.lines[1].flags == (LineFlag.UNMAPPED_SKU,)


# --- 4. approval queue: approve / correct / reject --------------------------


def test_approve_decision_stores_purchase_and_updates_last_known_price(
    day: date, beer_supplier: Supplier, chang_sku: Sku
) -> None:
    """Worked example: a clean receipt (no flags) is approved.

    The approved receipt becomes a Purchase, and approving it writes/updates
    last_known_price for every mapped line's (SKU, supplier).
    """
    skus = {chang_sku.sku_id: chang_sku}
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="Chang draught keg 30L",
                quantity=D("1"),
                unit_price=D("4000"),
                sku_id=chang_sku.sku_id,
            ),
        ),
        vat=D("280"),
        total=D("4280"),
    )
    checked = check_receipt(receipt, skus=skus, reference_prices={})
    book = ApprovalBook()

    decision = apply_decision(checked, _approve(), book)

    assert decision.purchase == Purchase(
        supplier_id=beer_supplier.supplier_id,
        invoice_date=day,
        lines=(
            PurchaseLine(
                sku_id=chang_sku.sku_id,
                description="Chang draught keg 30L",
                quantity=D("1"),
                unit_price=D("4000"),
            ),
        ),
        vat=D("280"),
        total=D("4280"),
    )
    # last_known_price updated for the one mapped line.
    assert book.last_known_price(chang_sku.sku_id, beer_supplier.supplier_id) == (
        D("4000"),
        day,
    )


def test_correct_decision_overrides_extracted_fields_before_storing(
    day: date, beer_supplier: Supplier, chang_sku: Sku
) -> None:
    """A partner corrects an OCR mistake (unit price was read as 40000, should
    be 4000), then approves. The stored purchase and last_known_price reflect
    the CORRECTED values, not the extracted ones.
    """
    skus = {chang_sku.sku_id: chang_sku}
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="Chang draught keg 30L",
                quantity=D("1"),
                unit_price=D("40000"),  # OCR error
                sku_id=chang_sku.sku_id,
            ),
        ),
        vat=D("280"),
        total=D("40280"),
    )
    checked = check_receipt(receipt, skus=skus, reference_prices={})
    book = ApprovalBook()

    corrected = (
        ExtractedReceiptLine(
            description="Chang draught keg 30L",
            quantity=D("1"),
            unit_price=D("4000"),  # corrected
            sku_id=chang_sku.sku_id,
        ),
    )
    decision = apply_decision(checked, _correct(corrected), book)

    assert decision.purchase is not None
    assert decision.purchase.lines[0].unit_price == D("4000")
    assert book.last_known_price(chang_sku.sku_id, beer_supplier.supplier_id) == (
        D("4000"),
        day,
    )


def test_reject_decision_stores_no_purchase(
    day: date, beer_supplier: Supplier, chang_sku: Sku
) -> None:
    """A partner rejects a queued receipt. Nothing is stored, no price update."""
    skus = {chang_sku.sku_id: chang_sku}
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="1 ml sample",
                quantity=D("1"),
                unit_price=D("0.07"),
                sku_id=chang_sku.sku_id,
            ),
        ),
        vat=D("0"),
        total=D("0.07"),
    )
    checked = check_receipt(receipt, skus=skus, reference_prices={})
    book = ApprovalBook()

    decision = apply_decision(checked, _reject(), book)

    assert decision.purchase is None
    assert book.last_known_price(chang_sku.sku_id, beer_supplier.supplier_id) is None


def test_auto_rejected_receipt_cannot_be_approved(
    day: date, beer_supplier: Supplier
) -> None:
    """A receipt that failed the sum-check is auto-rejected; partners cannot
    'approve' it — they must re-upload. Attempting to apply a decision raises.
    """
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="x",
                quantity=D("1"),
                unit_price=D("100"),
                sku_id=None,
            ),
        ),
        vat=D("0"),
        total=D("999"),  # does not reconcile
    )
    checked = check_receipt(receipt, skus={}, reference_prices={})
    assert checked.state == ReceiptState.AUTO_REJECTED

    with pytest.raises(Exception):
        apply_decision(checked, _approve(), ApprovalBook())


# --- 5. unmapped lines do not update last_known_price on approval -----------


def test_approval_with_unmapped_line_stores_purchase_but_skips_price_update_for_it(
    day: date, beer_supplier: Supplier, chang_sku: Sku
) -> None:
    """A receipt with one mapped line and one unmapped line is approved.

    The mapped line updates last_known_price; the unmapped line (no SKU) is
    stored on the purchase but contributes nothing to the price table.
    """
    skus = {chang_sku.sku_id: chang_sku}
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="1 ml sample",
                quantity=D("1"),
                unit_price=D("0.07"),
                sku_id=chang_sku.sku_id,
            ),
            ExtractedReceiptLine(
                description="Mystery item",
                quantity=D("1"),
                unit_price=D("0.05"),
                sku_id=None,
            ),
        ),
        vat=D("0"),
        total=D("0.12"),
    )
    checked = check_receipt(receipt, skus=skus, reference_prices={})
    book = ApprovalBook()

    decision = apply_decision(checked, _approve(), book)

    assert decision.purchase is not None
    assert len(decision.purchase.lines) == 2
    # Only the mapped line's (SKU, supplier) gets a price entry.
    assert book.last_known_price(chang_sku.sku_id, beer_supplier.supplier_id) == (
        D("0.07"),
        day,
    )
    # Nothing else in the book (the unmapped line has no key to store under).
    assert book.price_keys() == {(chang_sku.sku_id, beer_supplier.supplier_id)}


# --- 6. end-to-end through the seeded source --------------------------------


def test_seeded_source_yields_extracted_receipts(
    day: date, beer_supplier: Supplier, chang_sku: Sku
) -> None:
    """The seeded source is the in-repo stand-in for the real upload + OCR
    flow. It satisfies the same `ReceiptSource` protocol a future Google Drive
    import + real OCR provider will satisfy.
    """
    receipt = _receipt(
        beer_supplier.supplier_id,
        day,
        lines=(
            ExtractedReceiptLine(
                description="Chang draught keg 30L",
                quantity=D("1"),
                unit_price=D("4000"),
                sku_id=chang_sku.sku_id,
            ),
        ),
        vat=D("280"),
        total=D("4280"),
    )
    source = SeededReceiptSource(receipts=[receipt])

    assert source.receipts() == [receipt]
