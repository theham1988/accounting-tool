"""Receipt approval queue (slice 03).

Partners act on QUEUED receipts (the output of `receipts.check_receipt`) via a
`ReceiptDecision`. `apply_decision` enforces the lifecycle and, for approvals,
promotes the receipt to a stored `Purchase` and updates the `ApprovalBook`'s
last-known-price table for each mapped line.

The book is the source of truth for `last_known_price`. It is mutable on
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
    None for REJECTED. `final_state` is the resulting receipt state
    (APPROVED or REJECTED).
    """

    purchase: Purchase | None
    final_state: ReceiptState


@dataclass
class ApprovalBook:
    """Mutable store of last-known prices per (SKU, supplier).

    Updated only when a receipt is approved (or corrected-then-approved).
    Reading the current price is what `receipts.check_receipt` consumes via
    the `reference_prices` snapshot callers build from `price_snapshot`.
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

    def _record(self, sku_id: str, supplier_id: str, price: Decimal, on: date) -> None:
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


def _to_purchase(
    checked: CheckedReceipt, lines: tuple[ExtractedReceiptLine, ...]
) -> Purchase:
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
        total=checked.total,
    )


def apply_decision(
    checked: CheckedReceipt,
    decision: ReceiptDecision,
    book: ApprovalBook,
) -> ApprovalResult:
    """Apply a partner's decision to a QUEUED receipt.

    - APPROVED (no correction): store the checked lines as a purchase and
      update last_known_price for every mapped line.
    - APPROVED with `corrected_lines`: store the corrected lines instead, and
      update last_known_price from the corrected prices.
    - REJECTED: store nothing, update nothing.

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

    # Both plain approve and correct-then-approve land here.
    lines = _lines_to_store(checked, decision.corrected_lines)
    purchase = _to_purchase(checked, lines)

    for line in lines:
        if line.sku_id is None:
            continue
        book._record(
            line.sku_id,
            checked.supplier_id,
            line.unit_price,
            checked.invoice_date,
        )

    return ApprovalResult(purchase=purchase, final_state=ReceiptState.APPROVED)
