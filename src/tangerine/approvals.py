"""Receipt approval actions and the last-known-price book (slice 03).

Partners act on QUEUED receipts (the output of `receipts.check_receipt`) via a
`ReceiptDecision`. `apply_decision` enforces the lifecycle and, for approvals,
promotes the receipt to a stored `Purchase` and updates the `PriceBook`'s
last-known-price table for each mapped line.

`PriceBook` is the source of truth for `last_known_price`. It is mutable on
approval because prices genuinely change over time; the receipt checks
themselves remain pure functions over the book's current snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .types import (
    CheckedReceipt,
    ExtractedReceiptLine,
    LastKnownPrice,
    Purchase,
    PurchaseLine,
    ReceiptDecision,
    ReceiptState,
)


class DecisionError(Exception):
    """Raised when a decision cannot be applied to a receipt in its state.

    Most commonly: trying to approve/correct/reject a receipt that already
    failed the sum-check (AUTO_REJECTED) — those must be re-uploaded, not
    approved, so a broken extraction can never reach the books.
    """


@dataclass(frozen=True)
class ApprovalResult:
    """Outcome of applying a `ReceiptDecision` to a checked receipt.

    `purchase` is the stored purchase for APPROVED/CORRECTED decisions, or
    None for REJECTED. `final_state` distinguishes a plain approval
    (APPROVED) from a corrected-then-approved one (CORRECTED), per the
    issue's three acceptance-criteria actions.
    """

    purchase: Purchase | None
    final_state: ReceiptState


@dataclass
class PriceBook:
    """Mutable store of last-known prices per (SKU, supplier).

    Despite living in this approvals module (it is updated on approval), the
    name reflects what it stores — prices — not the queue of receipts. The
    queue itself is the caller's responsibility until a persistence slice
    introduces a stored approval workflow.

    Updated only when a receipt is approved (or corrected-then-approved).
    `price_snapshot` is the view `receipts.check_receipt` consumes.
    """

    _prices: dict[tuple[str, str], LastKnownPrice] = field(default_factory=dict)

    def last_known_price(
        self, sku_id: str, supplier_id: str
    ) -> tuple[Decimal, date] | None:
        entry = self._prices.get((sku_id, supplier_id))
        if entry is None:
            return None
        return (entry.price, entry.updated_at)

    def price_keys(self) -> set[tuple[str, str]]:
        return set(self._prices.keys())

    def price_snapshot(self) -> dict[tuple[str, str], LastKnownPrice]:
        """The (key -> LastKnownPrice) view the receipt checker consumes."""
        return dict(self._prices)

    def record(
        self, sku_id: str, supplier_id: str, price: Decimal, on: date
    ) -> None:
        """Write/replace the last-known price for a (SKU, supplier) pair.

        Public because approvals (an engine) update the book (a store) across
        a boundary; reaching into a private `_record` from outside the class
        would cross that boundary improperly.
        """
        self._prices[(sku_id, supplier_id)] = LastKnownPrice(
            price=price, updated_at=on
        )


def _lines_to_store(
    checked: CheckedReceipt, corrected: tuple[ExtractedReceiptLine, ...] | None
) -> tuple[ExtractedReceiptLine, ...]:
    """The lines that will be stored as a purchase.

    A correction overrides the extracted fields wholesale (a partner re-enters
    the lines they want on the books). Without a correction, the checked
    receipt's lines are stored as-is.
    """
    if corrected is not None:
        return corrected
    return tuple(
        ExtractedReceiptLine(
            description=cl.description,
            quantity=cl.quantity,
            unit_price=cl.unit_price,
            sku_id=cl.sku_id,
        )
        for cl in checked.lines
    )


def _lines_total(lines: tuple[ExtractedReceiptLine, ...]) -> Decimal:
    """Sum of (quantity * unit_price) across the given lines.

    Mirrors `receipts._lines_total` but operates on the lines being stored
    (which may be corrected), so a corrected purchase can recompute its total
    from the corrected line values rather than reuse the extracted total.
    """
    return Decimal(
        sum((line.quantity * line.unit_price) for line in lines) or Decimal("0")
    )


def _to_purchase(
    checked: CheckedReceipt,
    lines: tuple[ExtractedReceiptLine, ...],
    is_correction: bool,
) -> Purchase:
    """Build the stored purchase from the (possibly corrected) lines.

    For a plain approve, the lines are unchanged from the extraction and the
    original `total`/`vat` already reconcile (the sum-check guaranteed it), so
    they're carried through as-is.

    For a correction, the line totals may have changed. To keep the sum-check
    invariant intact on the stored record (`lines + vat == total`), the total
    is recomputed as `sum(corrected lines) + vat`. VAT is a receipt-level
    field the partner does not edit through the line correction UI, so the
    extracted VAT is retained.
    """
    if is_correction:
        total = _lines_total(lines) + checked.vat
    else:
        total = checked.total
    return Purchase(
        supplier_id=checked.supplier_id,
        invoice_date=checked.invoice_date,
        lines=tuple(
            PurchaseLine(
                sku_id=line.sku_id,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
            )
            for line in lines
        ),
        vat=checked.vat,
        total=total,
    )


def apply_decision(
    checked: CheckedReceipt,
    decision: ReceiptDecision,
    book: PriceBook,
) -> ApprovalResult:
    """Apply a partner's decision to a QUEUED receipt.

    - APPROVED (no correction): store the checked lines as a purchase, update
      last_known_price for every mapped line, final state APPROVED.
    - APPROVED with `corrected_lines`: store the corrected lines instead,
      update last_known_price from the corrected prices, final state CORRECTED.
    - REJECTED: store nothing, update nothing, final state REJECTED.

    Raises `DecisionError` if the receipt is not QUEUED (e.g. it was
    AUTO_REJECTED by the sum-check and must be re-uploaded rather than
    approved).
    """
    if checked.state != ReceiptState.QUEUED:
        raise DecisionError(
            f"cannot apply decision to receipt in state {checked.state.value}; "
            "only QUEUED receipts are eligible"
        )

    if decision.decision == ReceiptState.REJECTED:
        return ApprovalResult(purchase=None, final_state=ReceiptState.REJECTED)

    is_correction = decision.corrected_lines is not None
    lines = _lines_to_store(checked, decision.corrected_lines)
    purchase = _to_purchase(checked, lines, is_correction=is_correction)

    for line in lines:
        if line.sku_id is None:
            continue
        book.record(
            line.sku_id,
            checked.supplier_id,
            line.unit_price,
            checked.invoice_date,
        )

    final_state = ReceiptState.CORRECTED if is_correction else ReceiptState.APPROVED
    return ApprovalResult(purchase=purchase, final_state=final_state)
