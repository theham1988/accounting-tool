"""Profit Report composition module (issue #113, parent spec #112).

The composition layer that sits between the two existing P&L engines and
the ``/review?mode=profit`` route. The Profit Report screen puts two lenses
on the **same month** side by side:

- **Recipe-cost lens** — :func:`tangerine.period_review.build_period_review`:
  revenue / COGS / gross margin / net profit, each sale costed at the net
  price in effect on its own date. *Theoretical* — it costs what *should*
  have been consumed, with no inventory measurement.
- **Cash-basis lens** — :func:`tangerine.cash_spend.cash_spend_for_period`:
  what the venue *paid* vendors in the window, by bucket, net of VAT.
  *Actual* — it costs what went out the door, period-matched to the cash.

The two lenses disagree by design (a big Makro restock shows a healthy
recipe-cost GM but a thin cash-basis GP — the beans will sell later, but
the cash has already left). Surfacing both honestly is the whole point of
the screen; the spec (parent #112) records the "Profit Report is the only
two-lens surface" decision (Period/Month stay recipe-cost-only).

This module is **pure composition** — it owns no I/O, no storage imports.
The web layer supplies the two engines' already-computed results plus
``today`` (for the in-progress flag); this module pairs them into the
report shape the template renders. The spine landed the 4-tile summary
+ the in-progress flag; later tickets added the two-lens ``.pnl`` table
(#114), the daily-revenue and spend-by-category charts (#115), and the
bestseller rankings (#116) — each extending this module's outputs.

The two-lens honesty model (parent #112 "Two-lens honesty model"):

- Cash-basis GP = ``revenue − cash_spend_for_period.total``.
- Recipe-cost GM = ``review.gross_margin`` (= ``revenue − cogs``).

Both percentages will be computed against the same ``revenue`` on the
template once the two-lens ``.pnl`` table lands (a later ticket); the spine
lands only the four absolute-THB tiles plus the lenses' full outputs for
later tickets to extend.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .cash_spend import CashSpendForPeriod
from .period_review import PeriodReview
from .types import ItemMargin, Money


@dataclass(frozen=True)
class ProfitTiles:
    """The 4-tile summary at the top of the Profit Report.

    The partner's four headline answers, each sourced from the lens that
    answers it honestly:

    - ``revenue``        — the gross-sales headline (every sale, mapped or
                           not, so Books ties to Loyverse Gross for the
                           range; ADR-0008). Both lenses share this base.
    - ``cash_basis_gp``  — the cash-basis lens: ``revenue − cash_spend``.
                           What the venue actually kept after paying vendors.
    - ``recipe_cost_gm`` — the recipe-cost lens: ``review.gross_margin``
                           (= ``revenue − cogs``). The theoretical margin
                           at as-of-date prices.
    - ``net_profit``     — ``review.net_profit`` (gross margin minus entity
                           fixed costs). The bottom line the Period/Month
                           goal compares against 10K THB/day.

    A negative ``cash_basis_gp`` is the honest signal a big restock month
    sends — the cash went out the door, the beans will sell later. The tile
    carries the negative number rather than zero.
    """

    revenue: Money
    cash_basis_gp: Money
    recipe_cost_gm: Money
    net_profit: Money


@dataclass(frozen=True)
class ProfitReport:
    """The full report shape for one calendar month's Profit Report.

    Carries the 4-tile summary plus the two lenses' full outputs, so the
    template can render the tiles now and later tickets can extend the
    two-lens panel / charts / bestsellers off the same object without
    re-running the engines.

    ``in_progress`` is True only when ``[start, end]`` is the current
    calendar month (contains ``today``). A fully-past or fully-future month
    is False — the absence is meaningful (a partner must not mistake a
    half-month's numbers for a closed month's).
    """

    review: PeriodReview
    cash_spend: CashSpendForPeriod
    tiles: ProfitTiles
    in_progress: bool


def is_month_in_progress(*, start: date, end: date, today: date) -> bool:
    """True when ``[start, end]`` is the calendar month containing ``today``.

    Pure date logic — no I/O, no clock injection beyond the ``today`` arg
    the caller supplies (the web layer passes ``app.state.today``, which is
    injectable in tests and reads the wall clock in production).

    Two conditions must hold:

    1. ``today`` falls inside ``[start, end]`` (inclusive).
    2. ``[start, end]`` is exactly one calendar month — ``start`` is the
       first day of a month and ``end`` is its last day.

    The second condition is what makes the marker unambiguous: the Profit
    Report is always a calendar month (default current month, or
    ``?month=YYYY-MM``), so a sub-month range containing today never shows
    the marker even though today is "in range". A future month is not in
    progress either — it has not started.
    """
    if not (start <= today <= end):
        return False
    if start.day != 1:
        return False
    last_day_of_start_month = calendar.monthrange(start.year, start.month)[1]
    if end.day != last_day_of_start_month:
        return False
    # ``start`` and ``end`` must be the same calendar month. ``end.day`` is
    # already the last day of ``start``'s month, so checking ``end``'s year
    # + month agree with ``start``'s is sufficient (handles February etc.).
    return (end.year, end.month) == (start.year, start.month)


@dataclass(frozen=True)
class ItemSalesTotal:
    """One item's sales totals across the selected month (issue #116).

    The shared per-item aggregation the two bestseller rankings sort. Both
    rankings (total sales volume / total items) are two sorts of the same
    list, so the aggregation is computed once and sorted twice.

    - ``item_id``       the Loyverse item id (the aggregation key)
    - ``name``          the item's display name. For a mapped item this is
                        the recipe name; for an unmapped one it falls back
                        to the item id (how ``compute_item_margins`` names
                        an unmapped row) so the rankings always carry a
                        readable label.
    - ``units_sold``    total units sold in the month, summed across days
    - ``revenue``       total revenue (THB) in the month, summed across days
    - ``cm_unknown``    True when the item's contribution margin cannot be
                        honestly derived — either ``unmapped`` (no recipe) or
                        ``unknown_price`` (a recipe ingredient is unpriced).
                        The item still appears in the rankings (its revenue
                        counts); the marker is the visible "CM unknown"
                        labelling the spec requires, *not* an exclusion.
    """

    item_id: str
    name: str
    units_sold: int
    revenue: Money
    cm_unknown: bool


#: The bestseller metric to rank by. Two values, one per ranking (issue #116):
#: total revenue (THB) and total units (item count). Both sorts share one
#: aggregation; the metric selects the descending key.
_BestsellerMetric = str  # "revenue" | "units"


def aggregate_item_sales(rows: list[ItemMargin]) -> list[ItemSalesTotal]:
    """Collapse per-day ``ItemMargin`` rows into one ``ItemSalesTotal`` per item.

    Pure aggregation over the same per-day rows the recipe-cost lens consumes
    (``margins_over_range`` → ``DayMargins.item_margins``): ``revenue`` and
    ``units_sold`` sum across the days the item sold; the flags collapse to
    a single ``cm_unknown`` (True when *any* of the item's rows is flagged
    ``unmapped`` or ``unknown_price``).

    Rows for items that sold nothing in the month are not produced by the
    margin engine (it emits a row per ``(item, segment)`` that *sold* that
    day), so this function does not filter zero rows either — the ranking
    function owns that guard (see :func:`rank_bestsellers`).

    Output order is ``item_id`` ascending (deterministic), so the two rankings
    start from a stable base before applying their own metric sort. ``name``
    is taken from the first row seen for an item; it is the recipe name for a
    mapped item and the item id for an unmapped one (matching how
    ``compute_item_margins`` names an unmapped row).
    """
    by_item: dict[str, list[ItemMargin]] = {}
    for im in rows:
        by_item.setdefault(im.item_id, []).append(im)
    totals: list[ItemSalesTotal] = []
    for item_id in sorted(by_item):
        item_rows = by_item[item_id]
        totals.append(
            ItemSalesTotal(
                item_id=item_id,
                name=item_rows[0].name,
                units_sold=sum(im.units_sold for im in item_rows),
                revenue=sum((im.revenue for im in item_rows), Money("0")),
                cm_unknown=any(
                    im.unmapped or im.unknown_price for im in item_rows
                ),
            )
        )
    return totals


def rank_bestsellers(
    items: list[ItemSalesTotal],
    *,
    metric: _BestsellerMetric,
    limit: int,
) -> list[ItemSalesTotal]:
    """Rank items into a descending top-N list (issue #116).

    Two sales-side rankings share one aggregation — call
    :func:`aggregate_item_sales` once, then sort its result twice (once per
    metric) so the per-item rows are computed a single time:

    - ``metric="revenue"`` — top-N by total sales volume (THB)
    - ``metric="units"``   — top-N by total items (unit count)

    Sorting is descending by the chosen metric; ties are broken
    deterministically by ``item_id`` ascending (so the same aggregation
    always produces the same ranking). Fewer-than-N items renders what
    exists — no padding. Items that never sold (zero units *and* zero
    revenue) are dropped so a stale or future drift cannot pollute the
    ranking with empty rows; an item that moved units but earned zero
    revenue (a giveaway) still ranks.

    ``cm_unknown`` is carried through from the aggregation so the template
    can render the "CM unknown" marker on either ranking — the item is not
    excluded, only labelled.
    """
    ranked_input = [
        t
        for t in items
        if not (t.units_sold == 0 and t.revenue == 0)
    ]
    # Descending by metric, ascending by ``item_id`` as the deterministic
    # tie-break. Negate the numeric metric for the descending sort; keep
    # ``item_id`` positive so the tie-break is ascending. Both metrics
    # promote to Decimal (int converts cleanly), so the sign flip is
    # well-typed regardless of which metric was chosen.
    ranked = sorted(
        ranked_input, key=lambda t: (-_metric_value(t, metric), t.item_id)
    )
    return ranked[:limit]


def _metric_value(total: ItemSalesTotal, metric: _BestsellerMetric) -> Decimal:
    """The sort key for a ranking — the metric value as a ``Decimal``.

    Validates ``metric`` up front (the only two legal values are
    ``"revenue"`` and ``"units"``) and returns a ``Decimal`` so the caller's
    sign-flip for the descending sort is well-typed. ``units_sold`` (int)
    converts to ``Decimal`` cleanly; ``revenue`` is already ``Money``
    (``Decimal``).
    """
    if metric == "revenue":
        return total.revenue
    if metric == "units":
        return Decimal(total.units_sold)
    raise ValueError(
        f"unknown bestseller metric {metric!r}; expected 'revenue' or 'units'"
    )


def build_profit_report(
    *,
    review: PeriodReview,
    cash_spend: CashSpendForPeriod,
    today: date,
) -> ProfitReport:
    """Pair the two engines' outputs into the Profit Report shape.

    Engines are passed in (the web layer runs them and supplies the
    results); this module owns only the composition. The 4-tile summary
    is computed here; the two-lens panel, charts, and bestsellers (later
    tickets) extend off ``review`` / ``cash_spend`` without re-running
    either engine.

    ``today`` drives the in-progress flag (see :func:`is_month_in_progress`).
    """
    revenue = review.revenue
    tiles = ProfitTiles(
        revenue=revenue,
        cash_basis_gp=revenue - cash_spend.total,
        recipe_cost_gm=review.gross_margin,
        net_profit=review.net_profit,
    )
    return ProfitReport(
        review=review,
        cash_spend=cash_spend,
        tiles=tiles,
        in_progress=is_month_in_progress(
            start=review.start, end=review.end, today=today
        ),
    )


__all__ = [
    "ItemSalesTotal",
    "ProfitReport",
    "ProfitTiles",
    "aggregate_item_sales",
    "build_profit_report",
    "is_month_in_progress",
    "rank_bestsellers",
]
