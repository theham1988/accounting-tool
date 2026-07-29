"""Cash-spend: cash-basis supplier purchases, aggregated net-of-VAT per bucket.

Issue #96 (parent #82: "model cash-basis supplier spend"). This is the row
that produces the HTML's "Cost of goods — purchases (cash)" line and the
per-bucket breakdown (taps / kitchen / coffee / bakery / staff / rent). A
cash-spend row is one bucket's slice of a vendor bill on a date; a
multi-bucket bill (the Makro tax invoice crossing coffee beans + taps
glassware) is entered as **N sibling rows** sharing date + supplier but
differing bucket + amount (decision A). The invoice total is a derived
fact — ``SUM(amount) WHERE date=X AND supplier_id=Y`` — never stored.

Net of VAT where ``vat_inclusive=True`` (divide by 1.07, per ADR-0003
decision 4: "default false so the migration never makes a number worse by
guessing wrong"); gross otherwise. VAT-ness is a property of the *row*,
not the supplier or the bucket — the same SKU bought from a VAT-registered
supplier (Makro, ARO) on one occasion and a wet-market stall on another
carries a different flag each time.

**No day-apportionment.** This is the key difference from
:mod:`tangerine.fixed_costs`: a fixed cost belongs to a *month* (so a
sub-month range takes a day-fraction of it), but a cash purchase belongs
to its own *date* — a 4,200 THB Makro bill is 4,200 THB whether the period
is one day or seven, and a row outside ``[start, end]`` is excluded
entirely (decision C reason 1). That single difference is why a sibling
table exists rather than a new ``fixed_costs.kind``.

Pure engine — no I/O, no storage imports. The store
(:class:`~tangerine.storage.config_store.SqliteConfigStore`) supplies
``CashSpendEntry`` lists via ``cash_spend_rows()``; the reporting surface
(P&L, the admin page) feeds those lists here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .types import Money

#: The Thai VAT rate. ADR-0003 decision 4: when a cost was VAT-inclusive the
#: stored net value is ``gross / 1.07``. Cash-spend rows carry the same
#: per-row flag and apply the same division at aggregation time.
_VAT_RATE = Money("1.07")

#: Two-decimal quantisation for displayed THB amounts, matching
#: :mod:`tangerine.fixed_costs`' apportionment precision. Per-bucket and
#: period totals are quantised after the division so the partner's
#: displayed number is a real currency value, not a 6-dp derived one.
_CENTS = Money("0.01")


@dataclass(frozen=True)
class CashSpendEntry:
    """One stored cash-spend row — one bucket's slice of a vendor bill.

    The shape mirrors the ``cash_spend`` table (issue #96) and the admin
    entry form. A multi-bucket bill is N of these sharing ``date`` +
    ``supplier_id`` but differing ``bucket_id`` + ``amount`` + typically
    ``description`` (decision A); the invoice total is the sum of those
    sibling rows' amounts, derived, never stored on a parent.

    ``amount`` is the THB amount **as paid** (gross when
    ``vat_inclusive``, net otherwise). The aggregation layer divides by
    1.07 only when ``vat_inclusive`` is set — VAT-ness is a property of
    the *purchase*, not the supplier or the bucket (ADR-0003 decision 4).

    There is deliberately **no segment field** (decision D + ADR-0007):
    a bucket is a product-family / cost-category, not a segment. Which
    segment a bucket's cost falls against is a downstream P&L computation
    against recipes, not a fact of the purchase.
    """

    row_id: int
    date: date
    supplier_id: str
    description: str
    bucket_id: str
    amount: Money
    vat_inclusive: bool


@dataclass(frozen=True)
class CashSpendForPeriod:
    """Cash spend summed over an inclusive ``[start, end]`` range.

    ``total`` is the net-of-VAT THB total for the period (the COGS-side
    number). ``by_bucket`` is the per-bucket breakdown keyed by
    ``bucket_id`` — the shape the HTML cost-breakdown column renders.
    Only buckets with non-zero spend appear; a bucket with no rows in
    the period is absent (callers that need every bucket present merge
    against the spend-bucket vocabulary).
    """

    total: Money
    by_bucket: dict[str, Money]


def cash_spend_for_period(
    *, start: date, end: date, entries: list[CashSpendEntry]
) -> CashSpendForPeriod:
    """Aggregate ``entries`` over the inclusive ``[start, end]`` range.

    Each row whose ``date`` falls inside ``[start, end]`` (inclusive)
    contributes its amount to its bucket; rows outside the window are
    excluded entirely — no day-apportionment (decision C). Within a
    bucket, the contribution is ``amount / 1.07`` when ``vat_inclusive``
    is set (ADR-0003 decision 4), else ``amount`` as-is. Per-bucket and
    period totals are quantised to two decimals after the division.
    """
    by_bucket: dict[str, Money] = {}
    for entry in entries:
        if not (start <= entry.date <= end):
            continue
        contribution = _net_of_vat(entry)
        accumulated = by_bucket.get(entry.bucket_id, Money("0")) + contribution
        by_bucket[entry.bucket_id] = accumulated.quantize(_CENTS)
    total = sum(by_bucket.values(), Money("0")).quantize(_CENTS)
    return CashSpendForPeriod(total=total, by_bucket=by_bucket)


def _net_of_vat(entry: CashSpendEntry) -> Money:
    """One row's THB contribution to its bucket: gross, or net of VAT.

    The single place the cash-spend engine applies the VAT rule. Mirrors
    :func:`tangerine.storage.config_store._net_per_unit`: the same rule,
    applied at aggregation time (the raw amount is stored as-paid so the
    invoice total reconstructs from ``SUM(amount)`` — decision A).
    """
    if not entry.vat_inclusive:
        return entry.amount
    return entry.amount / _VAT_RATE


__all__ = [
    "CashSpendEntry",
    "CashSpendForPeriod",
    "cash_spend_for_period",
]
