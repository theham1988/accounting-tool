"""Cafe stock counts → accrual COGS engine (slice 06).

Computes the cafe segment's consumption-based COGS for a period from
partner-entered stock counts and approved purchases. Per the PRD and
issue 06, the accrual-COGS primitive is::

    consumed = beginning + purchases − ending

Priced at the SKU's latest approved purchase price, that becomes the
period's cafe COGS — the number the monthly P&L (slice 08) books. The
daily 9am view keeps using the recipe-based margin engine (slice 04);
this module does not touch it.

This mirrors the keg-inventory approach planned for slice 05 but is
self-contained: there is no shared inventory abstraction yet, and
per-keg yield / density math is keg-specific and stays out of here. A
later slice may factor a shared ``periodic-inventory → accrual COGS``
helper once both shapes exist.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .cost import CostBook
from .types import (
    CafeConsumedCogs,
    CafeItem,
    CafeStockCount,
    Money,
    Purchase,
)


def consumed_quantity(
    *, beginning: Decimal, purchased: Decimal, ending: Decimal
) -> Decimal:
    """Accrual-COGS consumption: ``beginning + purchased − ending``.

    The pure formula. Pricing and period-window filtering live in
    ``compute_cafe_consumed_cogs``; this helper exists so the arithmetic
    can be unit-checked in isolation.

    The result is **not** clamped to zero. A negative consumed quantity
    (ending > beginning + purchases) means stock appeared from nowhere —
    a count error or an unrecorded purchase — and is surfaced so a later
    slice can flag it rather than silently hiding the discrepancy.
    """
    return beginning + purchased - ending


def _sku_purchase_window(
    sku_id: str,
    beginning: list[CafeStockCount],
    ending: list[CafeStockCount],
) -> tuple[date | None, date | None]:
    """The purchase-inclusion window for one SKU, bounded by ITS OWN counts.

    Each cafe SKU's window is independent: count cadence is per-item by
    shelf life (issue 06 — milk daily, beans weekly), so counts for
    different SKUs land on different days. Bounding a SKU's window by a
    global earliest/latest across all SKUs would mis-attribute purchases
    that fall inside the global window but outside that SKU's actual
    count dates.

    The window is ``(open, close]``: a purchase on the opening-count day
    belongs to the prior period; one on the closing-count day belongs to
    this period. If a SKU has no opening (or no closing) count, that side
    of the window is unbounded (``None``).
    """
    open_date: date | None = None
    close_date: date | None = None
    for c in beginning:
        if c.sku_id == sku_id and (open_date is None or c.timestamp < open_date):
            open_date = c.timestamp
    for c in ending:
        if c.sku_id == sku_id and (close_date is None or c.timestamp > close_date):
            close_date = c.timestamp
    return open_date, close_date


def _in_window(
    invoice_date: date, open_date: date | None, close_date: date | None
) -> bool:
    """True when ``open_date < invoice_date <= close_date`` (None = unbounded)."""
    if open_date is not None and not (invoice_date > open_date):
        return False
    if close_date is not None and not (invoice_date <= close_date):
        return False
    return True


def _purchased_quantity_for(
    sku_id: str,
    purchases: list[Purchase],
    open_date: date | None,
    close_date: date | None,
) -> Decimal:
    """Sum the purchased quantity for ``sku_id`` from in-window purchases.

    A purchase line for the SKU contributes its ``quantity`` (in the SKU's
    own unit) only if the purchase's invoice date falls inside the SKU's
    period window. Lines with a ``None`` ``sku_id`` (unmapped receipt lines)
    never match and are skipped.
    """
    total = Decimal("0")
    for purchase in purchases:
        if not _in_window(purchase.invoice_date, open_date, close_date):
            continue
        for line in purchase.lines:
            if line.sku_id == sku_id:
                total += line.quantity
    return total


def compute_cafe_consumed_cogs(
    *,
    items: list[CafeItem],
    beginning: list[CafeStockCount],
    ending: list[CafeStockCount],
    purchases: list[Purchase],
    cost: CostBook,
) -> list[CafeConsumedCogs]:
    """Consumed quantity and COGS contribution per cafe SKU for the period.

    For each cafe item, the opening and closing counts are looked up by
    ``sku_id``. The purchased quantity for the period is the sum of all
    purchase lines for that SKU whose invoice date falls inside the SKU's
    own ``(opening, closing]`` window (see ``_sku_purchase_window`` — the
    window is per-SKU because count cadence is per-item by shelf life).
    Consumed quantity is then ``beginning + purchased − ending``, priced at
    the SKU's latest approved price from ``cost``.

    A SKU with no opening count is treated as beginning at zero; a SKU with
    no closing count is treated as ending at zero. (A later slice that
    wants to flag missing counts can do so; this slice keeps the arithmetic
    pure and total.)

    A SKU with no approved price is flagged ``unpriced``: its consumed
    quantity is still computed and surfaced, but COGS is reported as zero
    rather than silently booked — booking zero COGS on a real consumption
    would under-report cost and over-state period margin. This matches the
    slice-04 margin engine's ``unknown_price`` convention.

    Output is one ``CafeConsumedCogs`` per item in ``items``, in input order,
    so the caller controls the presentation order.
    """
    beginning_by_sku = {c.sku_id: c.quantity for c in beginning}
    ending_by_sku = {c.sku_id: c.quantity for c in ending}

    results: list[CafeConsumedCogs] = []
    for item in items:
        beginning_qty = beginning_by_sku.get(item.sku_id, Decimal("0"))
        ending_qty = ending_by_sku.get(item.sku_id, Decimal("0"))
        open_date, close_date = _sku_purchase_window(item.sku_id, beginning, ending)
        purchased_qty = _purchased_quantity_for(
            item.sku_id, purchases, open_date, close_date
        )
        consumed = consumed_quantity(
            beginning=beginning_qty,
            purchased=purchased_qty,
            ending=ending_qty,
        )

        price_entry = cost.price(item.sku_id)
        if price_entry is None:
            results.append(
                CafeConsumedCogs(
                    sku_id=item.sku_id,
                    name=item.name,
                    unit=item.unit,
                    cadence=item.cadence,
                    beginning_quantity=beginning_qty,
                    purchased_quantity=purchased_qty,
                    ending_quantity=ending_qty,
                    consumed_quantity=consumed,
                    unit_cost=Money("0"),
                    cogs=Money("0"),
                    unpriced=True,
                )
            )
            continue

        unit_cost = price_entry.price
        cogs = Money(consumed * unit_cost)
        results.append(
            CafeConsumedCogs(
                sku_id=item.sku_id,
                name=item.name,
                unit=item.unit,
                cadence=item.cadence,
                beginning_quantity=beginning_qty,
                purchased_quantity=purchased_qty,
                ending_quantity=ending_qty,
                consumed_quantity=consumed,
                unit_cost=unit_cost,
                cogs=cogs,
                unpriced=False,
            )
        )

    return results
