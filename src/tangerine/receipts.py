"""Receipt checking engine (slice 03).

Pure function over an extracted receipt plus two reference tables:

- `skus`:               known SKU master items (for the SKU-mapping check)
- `reference_prices`:   last-known unit price per (SKU, supplier)

Three checks run in order, matching docs/issues/03:

    1. Sum-check:     sum(lines) + VAT must equal the stated total within
                      `SUM_TOLERANCE`. Failure -> AUTO_REJECTED.
    2. Price-check:   per line, |unit_price - last_known_price| / last_known
                      > PRICE_DEVIATION_THRESHOLD (5%) -> flag PRICE_DEVIATION.
    3. SKU-mapping:   a line whose `sku_id` is None (extractor could not map
                      it) is always flagged UNMAPPED_SKU.

A receipt that passes the sum-check is QUEUED with per-line flags. The
approval queue (see `approvals.py`) acts on queued receipts; the books are
only touched on approval.
"""

from __future__ import annotations

from decimal import Decimal

from .types import (
    CheckedLine,
    CheckedReceipt,
    ExtractedReceipt,
    LastKnownPrice,
    LineFlag,
    ReceiptState,
    Sku,
)

# Absolute tolerance for the sum-check, in THB. Decimal money; anything below
# a satang is OCR noise.
SUM_TOLERANCE: Decimal = Decimal("1.00")

# A unit price deviating more than this fraction from the last-known price for
# the same (SKU, supplier) flags the line for human review. Per PRD user story 4.
PRICE_DEVIATION_THRESHOLD: Decimal = Decimal("0.05")  # 5%


def _lines_total(extracted: ExtractedReceipt) -> Decimal:
    """Sum of (quantity * unit_price) across extracted lines."""
    return Decimal(
        sum((line.quantity * line.unit_price) for line in extracted.lines) or Decimal("0")
    )


def _passes_sum_check(extracted: ExtractedReceipt) -> bool:
    expected = _lines_total(extracted) + extracted.vat
    return abs(expected - extracted.total) <= SUM_TOLERANCE


def _price_flags(
    line_unit_price: Decimal,
    reference: LastKnownPrice | None,
) -> tuple[LineFlag, ...]:
    """Return `(PRICE_DEVIATION,)` if the price deviates beyond the threshold.

    No reference price (first-time purchase) -> no flag; there is nothing to
    deviate from.
    """
    if reference is None or reference.price == 0:
        return ()
    deviation = abs(line_unit_price - reference.price) / reference.price
    if deviation > PRICE_DEVIATION_THRESHOLD:
        return (LineFlag.PRICE_DEVIATION,)
    return ()


def check_line(
    extracted_unit_price: Decimal,
    sku_id: str | None,
    reference_price: LastKnownPrice | None,
) -> tuple[LineFlag, ...]:
    """Flags for a single line: SKU mapping first, then price deviation.

    Exposed for unit-level clarity; `check_receipt` is the normal entry point.
    """
    flags: list[LineFlag] = []
    if sku_id is None:
        flags.append(LineFlag.UNMAPPED_SKU)
        # No SKU -> no key to look up a reference price under.
        return tuple(flags)
    flags.extend(_price_flags(extracted_unit_price, reference_price))
    return tuple(flags)


def check_receipt(
    extracted: ExtractedReceipt,
    skus: dict[str, Sku],
    reference_prices: dict[tuple[str, str], LastKnownPrice],
) -> CheckedReceipt:
    """Run the three checks against an extracted receipt.

    `reference_prices` is keyed by (sku_id, supplier_id) and maps to the
    current `LastKnownPrice` for that pair (see PRD schema sketch). The
    `ApprovalBook` carries this table and hands a snapshot to the checker.

    Returns a `CheckedReceipt` whose `state` is AUTO_REJECTED (sum-check
    failed, no lines populated) or QUEUED (sum-check passed, per-line flags
    populated). `check_receipt` never approves — that is a human action.
    """
    if not _passes_sum_check(extracted):
        lines_total = _lines_total(extracted)
        return CheckedReceipt(
            supplier_id=extracted.supplier_id,
            invoice_date=extracted.invoice_date,
            vat=extracted.vat,
            total=extracted.total,
            state=ReceiptState.AUTO_REJECTED,
            lines=(),
            rejection_reason=(
                f"sum-check failed: lines({lines_total}) + vat("
                f"{extracted.vat}) != total({extracted.total}) "
                f"outside {SUM_TOLERANCE} tolerance"
            ),
        )

    checked_lines: list[CheckedLine] = []
    for line in extracted.lines:
        reference = (
            reference_prices.get((line.sku_id, extracted.supplier_id))
            if line.sku_id is not None
            else None
        )
        # A sku_id that isn't in the master table is treated as unmapped: the
        # extractor proposed an id, but it is not a known SKU.
        effective_sku = line.sku_id if line.sku_id in skus else None
        flags = check_line(line.unit_price, effective_sku, reference)
        checked_lines.append(
            CheckedLine(
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                sku_id=effective_sku,
                flags=flags,
            )
        )

    return CheckedReceipt(
        supplier_id=extracted.supplier_id,
        invoice_date=extracted.invoice_date,
        vat=extracted.vat,
        total=extracted.total,
        state=ReceiptState.QUEUED,
        lines=tuple(checked_lines),
        rejection_reason=None,
    )
