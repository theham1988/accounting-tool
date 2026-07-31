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
report shape the template renders. The spine lands here: the 4-tile
summary + the in-progress flag. The two-lens ``.pnl`` table, the daily-
revenue chart, the spend-by-category chart, and the bestseller lists land
in later tickets and extend this module's outputs.

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

from .cash_spend import CashSpendForPeriod
from .period_review import PeriodReview
from .types import Money


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
    "ProfitReport",
    "ProfitTiles",
    "build_profit_report",
    "is_month_in_progress",
]
