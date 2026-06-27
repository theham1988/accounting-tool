"""Fixed costs + monthly accrual P&L (slice 08).

The monthly reconciliation view the PRD calls for (PRD user story 23): a full
entity-level P&L built on proper accrual-basis COGS, with segment contribution
margins, entity-level fixed costs, net profit, and a comparison to the
10,000 THB/day target.

Per the PRD segmentation model and issue 08:

    segment_accrual_cm(segment, month) =
        revenue(segment, month) - accrual_cogs(segment, month)
    entity_net_profit(month) =
        sum_over_segments(segment_accrual_cm) - fixed_costs(entity, month)

The bar's accrual COGS comes from slice 05 (keg weigh-ins: beginning volume −
ending volume × cost per ml); the cafe's from slice 06 (cafe stock counts:
beginning + purchases − ending, priced at the latest approved price). This
module calls both engines internally so a caller passes raw inventory inputs
and gets a single monthly result.

Fixed costs are entity-level only — never allocated to a segment (PRD user
story 20). Revenue is from Loyverse sales, recognised by transaction timestamp
(PRD "COGS recognition"). A separate cash-flow view reports payables by invoice
date (PRD user story 24), so the accounting view (COGS by consumption) and the
cash-flow view (when bills are due) are both available.
"""

from __future__ import annotations

import calendar
from datetime import date

from .cafe_stock import compute_cafe_consumed_cogs
from .cost import CostBook
from .keg_inventory import compute_keg_inventory
from .recipes import RecipeCatalog
from .segments import segment_of_sale
from .types import (
    CafeItem,
    CafeStockCount,
    CashFlowEntry,
    CashFlowView,
    DAILY_PROFIT_TARGET_THB,
    FixedCost,
    GoalStatus,
    KegBrand,
    KegWeighIn,
    Money,
    MonthlyPnl,
    Purchase,
    Sale,
    Segment,
    SegmentAccrualPnl,
    YearMonth,
)

# Canonical display order for segment roll-ups: cafe first, then bar. Mirrors
# the slice-07 order key so the monthly view matches the daily view's layout.
_SEGMENT_ORDER: dict[Segment, int] = {Segment.CAFE: 0, Segment.BAR: 1}


def compute_monthly_pnl(
    *,
    month: YearMonth,
    sales: list[Sale],
    recipes: RecipeCatalog,
    cost: CostBook,
    brands: list[KegBrand],
    weigh_ins: list[KegWeighIn],
    cafe_items: list[CafeItem],
    cafe_beginning: list[CafeStockCount],
    cafe_ending: list[CafeStockCount],
    purchases: list[Purchase],
    fixed_costs: list[FixedCost],
) -> MonthlyPnl:
    """Compute the full monthly accrual P&L for the entity.

    All inputs are raw (sales, inventory, purchases, fixed costs); the engine
    resolves per-segment accrual COGS by calling slices 05 (kegs → bar) and
    06 (cafe stock → cafe) internally, sums per-segment accrual contribution
    margin, subtracts entity-level fixed costs, and compares the result to the
    10K THB/day × days-in-month target. A separate cash-flow view reports
    payables recognised by invoice date.

    Month is a ``(year, month)`` tuple; revenue is restricted to sales whose
    transaction timestamp falls in that month, and fixed costs to entries
    whose ``period`` matches it.
    """
    accrual_cogs_by_segment = _accrual_cogs_by_segment(
        month=month,
        sales=sales,
        recipes=recipes,
        cost=cost,
        brands=brands,
        weigh_ins=weigh_ins,
        cafe_items=cafe_items,
        cafe_beginning=cafe_beginning,
        cafe_ending=cafe_ending,
        purchases=purchases,
    )
    revenue_by_segment = _revenue_by_segment(
        sales=sales, recipes=recipes, month=month
    )

    segment_pnl = tuple(
        SegmentAccrualPnl(
            segment=seg,
            revenue=revenue_by_segment[seg],
            accrual_cogs=accrual_cogs_by_segment[seg],
            contribution_margin=revenue_by_segment[seg]
            - accrual_cogs_by_segment[seg],
        )
        for seg in sorted(Segment, key=lambda s: _SEGMENT_ORDER[s])
    )

    month_fixed = [fc for fc in fixed_costs if fc.period == month]
    total_fixed = sum((fc.amount for fc in month_fixed), Money("0"))
    total_cm = sum((sp.contribution_margin for sp in segment_pnl), Money("0"))
    net_profit = total_cm - total_fixed

    days_in_month = calendar.monthrange(month[0], month[1])[1]
    target = Money(DAILY_PROFIT_TARGET_THB * days_in_month)

    cash_flow = _cash_flow_view(month=month, purchases=purchases)

    return MonthlyPnl(
        month=month,
        segment_pnl=segment_pnl,
        fixed_costs=tuple(month_fixed),
        total_fixed_costs=total_fixed,
        entity_net_profit=net_profit,
        goal=GoalStatus(
            target=target,
            actual=net_profit,
            days_in_month=days_in_month,
        ),
        cash_flow=cash_flow,
    )


def _accrual_cogs_by_segment(
    *,
    month: YearMonth,
    sales: list[Sale],
    recipes: RecipeCatalog,
    cost: CostBook,
    brands: list[KegBrand],
    weigh_ins: list[KegWeighIn],
    cafe_items: list[CafeItem],
    cafe_beginning: list[CafeStockCount],
    cafe_ending: list[CafeStockCount],
    purchases: list[Purchase],
) -> dict[Segment, Money]:
    """Resolve each segment's accrual COGS for the month.

    Bar: keg inventory (slice 05). Cafe: stock counts (slice 06).

    Issue 08's AC for accrual COGS is the literal formula
    ``beginning inventory value + purchases − ending inventory value``. The two
    segments map onto it differently because their inventory units differ:

      - Cafe (slice 06): the literal formula, in quantity terms. Beginning +
        purchases (deliveries in the SKU's count window) − ending, priced at
        the latest approved price. A mid-month milk delivery is a real
        ``purchases`` term here because milk is bought and consumed in the same
        unit (ml).
      - Bar (slice 05): the formula in volume terms. ``purchases`` is implicit
        in the ending weigh — a keg refill increases ending volume, and
        beginning_volume − ending_volume already nets it out. Kegs are bought
        whole and ARE inventory, so a separate keg-purchase term would double-
        count. A genuine mid-month refill without its own weigh surfaces as
        negative consumption (slice 05 flags it); reconciling that against a
        keg-purchase ledger is deferred slice-05 work, not introduced here.

    Both engines are called with the month's end as the period close.
    """
    month_end_date = _last_day(month)

    bar_cogs = Money("0")
    if brands:
        keg_report = compute_keg_inventory(
            brands=brands,
            weigh_ins=weigh_ins,
            sales=sales,
            recipes=recipes,
            cost=cost,
            period_end=month_end_date,
        )
        bar_cogs = keg_report.total_accrual_cogs

    cafe_cogs = Money("0")
    if cafe_items:
        cafe_rows = compute_cafe_consumed_cogs(
            items=cafe_items,
            beginning=cafe_beginning,
            ending=cafe_ending,
            purchases=purchases,
            cost=cost,
        )
        cafe_cogs = sum((row.cogs for row in cafe_rows), Money("0"))

    return {Segment.BAR: bar_cogs, Segment.CAFE: cafe_cogs}


def _revenue_by_segment(
    *, sales: list[Sale], recipes: RecipeCatalog, month: YearMonth
) -> dict[Segment, Money]:
    """All-sale revenue per segment for the month, by sale timestamp.

    Revenue is restricted to sales whose ``timestamp`` falls in the month.
    A mapped sale takes its segment from the recipe (slice-07 rule). An
    unmapped sale takes its segment from the shift-stamped ``sale.segment``
    (the slice-07 fallback the Loyverse parser resolved at the sync boundary
    from the transaction timestamp: 8am–5pm cafe, else bar).

    Unlike the daily recipe-margin engine, the monthly view INCLUDES unmapped
    sales' revenue. The daily engine excludes them because their COGS is
    recipe-derived and unknown; in the monthly view COGS is consumption-derived
    (accrual, from inventory), and the inventory engines capture consumption of
    ALL stock regardless of which sale used it. Dropping unmapped revenue here
    would therefore under-state segment CM — the consumed stock is costed but
    the sale that consumed it is invisible. Including unmapped revenue (via the
    shift fallback segment) keeps revenue and accrual COGS symmetric.
    """
    start, end = _month_bounds(month)
    buckets: dict[Segment, Money] = {seg: Money("0") for seg in Segment}
    for sale in sales:
        if not (start <= sale.timestamp <= end):
            continue
        recipe = recipes.for_item(sale.item_id)
        seg = segment_of_sale(sale, recipe)
        buckets[seg] += sale.sell_price * sale.quantity
    return buckets


def _cash_flow_view(
    *, month: YearMonth, purchases: list[Purchase]
) -> CashFlowView:
    """Payables recognised by invoice date for the month (the cash-flow view).

    Per PRD user story 24, cash-basis payables are tracked by invoice date —
    the cash the business owes for goods received that month — separately from
    accrual COGS (which is by consumption). A purchase contributes its total
    when its invoice date falls in the month.
    """
    start, end = _month_bounds(month)
    entries: list[CashFlowEntry] = []
    for purchase in purchases:
        if not (start <= purchase.invoice_date <= end):
            continue
        entries.append(
            CashFlowEntry(
                supplier_id=purchase.supplier_id,
                invoice_date=purchase.invoice_date,
                total=purchase.total,
            )
        )
    entries.sort(key=lambda e: (e.invoice_date, e.supplier_id))
    total = sum((e.total for e in entries), Money("0"))
    return CashFlowView(month=month, total_payables=total, entries=tuple(entries))


def _month_bounds(month: YearMonth) -> tuple[date, date]:
    """The inclusive ``[first day, last day]`` of the given ``(year, month)``."""
    year, mon = month
    return date(year, mon, 1), _last_day(month)


def _last_day(month: YearMonth) -> date:
    """The last calendar day of the given ``(year, month)``."""
    year, mon = month
    return date(year, mon, calendar.monthrange(year, mon)[1])
