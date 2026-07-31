"""Unit tests for the Profit Report composition module (issue #113, parent #112).

``tangerine.profit_report`` is the pure composition layer that the
``/review?mode=profit`` route sits on: it takes the two existing engines'
outputs (``build_period_review`` for the recipe-cost lens and
``cash_spend_for_period`` for the cash-basis lens), pairs them into the
4-tile summary, and computes the "month in progress" flag from pure date
logic. No I/O, no store imports — the web layer supplies the engines'
results.

These tests pin the two pieces of pure logic the spine owns: the tile
composition (cash-basis GP = revenue − cash-spend total; the other three
tiles pass through the recipe-cost lens) and the in-progress flag (True
only when the range is a calendar month containing today). Engine outputs
are constructed directly — no need to stand up the full sales pipeline to
test the composition.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal as D

from tangerine.cash_spend import CashSpendForPeriod
from tangerine.fixed_costs import FixedCostsForPeriod
from tangerine.period_review import PeriodGoal, PeriodReview
from tangerine.profit_report import (
    ProfitReport,
    ProfitTiles,
    build_profit_report,
    is_month_in_progress,
)
from tangerine.types import Money, Segment, SegmentMargin


def _empty_review(start: date, end: date, *, revenue: Money = D("0")) -> PeriodReview:
    """A minimal ``PeriodReview`` carrying only what the tiles read.

    The composition module reads ``revenue`` / ``gross_margin`` / ``net_profit``
    and passes the rest through; the segment / day / fixed-costs detail is
    irrelevant to the tile math, so the fixture fills them with empty/zero
    values. This keeps the unit tests focused on the composition, not on
    standing up a full sales pipeline (the E2E covers that).
    """
    days_in_range = (end - start).days + 1
    return PeriodReview(
        start=start,
        end=end,
        revenue=revenue,
        cogs=D("0"),
        gross_margin=revenue,  # no COGS in this fixture
        segment_margins=(
            SegmentMargin(
                segment=Segment.CAFE,
                revenue=revenue,
                variable_costs=D("0"),
                contribution_margin=revenue,
            ),
            SegmentMargin(
                segment=Segment.BAR,
                revenue=D("0"),
                variable_costs=D("0"),
                contribution_margin=D("0"),
            ),
        ),
        flagged_revenue=D("0"),
        needs_attention=(),
        days=(),
        fixed_costs=FixedCostsForPeriod(lines=(), total=D("0"), estimated=False),
        net_profit=revenue,  # no fixed costs in this fixture
        goal=PeriodGoal(
            target=D("10000") * days_in_range,
            actual=revenue,
            days_in_range=days_in_range,
        ),
    )


# --- in-progress flag: pure date logic -----------------------------------------


def test_in_progress_true_for_current_calendar_month() -> None:
    """The flag is True when [start, end] is a calendar month containing today.

    July 2026 (1–31 Jul) viewed on 15 Jul 2026 is in progress.
    """
    assert is_month_in_progress(
        start=date(2026, 7, 1), end=date(2026, 7, 31), today=date(2026, 7, 15)
    )


def test_in_progress_false_for_a_fully_past_month() -> None:
    """A closed month never carries the marker — its absence is meaningful.

    June 2026 (1–30 Jun) viewed on 15 Jul 2026 is fully past.
    """
    assert not is_month_in_progress(
        start=date(2026, 6, 1), end=date(2026, 6, 30), today=date(2026, 7, 15)
    )


def test_in_progress_false_for_a_fully_future_month() -> None:
    """A future month is not "in progress" either.

    August 2026 viewed on 15 Jul 2026 has not started.
    """
    assert not is_month_in_progress(
        start=date(2026, 8, 1), end=date(2026, 8, 31), today=date(2026, 7, 15)
    )


def test_in_progress_false_when_range_is_not_a_calendar_month() -> None:
    """A sub-month range containing today is not "in progress".

    The Profit Report is always a calendar month (default current month, or
    ``?month=YYYY-MM``), so the flag is specifically "is this the current
    calendar month" — a 7-day window overlapping today does not qualify, even
    if today falls inside it. This keeps the marker's meaning unambiguous.
    """
    assert not is_month_in_progress(
        start=date(2026, 7, 10), end=date(2026, 7, 16), today=date(2026, 7, 13)
    )


def test_in_progress_false_when_today_lands_outside_the_month() -> None:
    """A calendar month that does not contain today is not in progress.

    Guard against an off-by-one where today == end-of-next-month etc.
    """
    last_jul = calendar.monthrange(2026, 7)[1]
    assert not is_month_in_progress(
        start=date(2026, 7, 1), end=date(2026, 7, last_jul), today=date(2026, 8, 1)
    )


def test_in_progress_handles_february_in_a_leap_year() -> None:
    """Feb 2028 has 29 days; the 29th is in progress when today is the 14th."""
    assert calendar.monthrange(2028, 2)[1] == 29
    assert is_month_in_progress(
        start=date(2028, 2, 1), end=date(2028, 2, 29), today=date(2028, 2, 14)
    )


# --- tile composition: cash-basis GP beside recipe-cost GM ---------------------


def test_tiles_match_the_two_engines_over_the_same_range() -> None:
    """The 4 tiles are a direct composition of the two engines' outputs.

        revenue        = review.revenue                 (gross-sales headline)
        cash_basis_gp  = revenue - cash_spend.total     (the cash-basis lens)
        recipe_cost_gm = review.gross_margin            (the recipe-cost lens)
        net_profit     = review.net_profit              (gross_margin - fixed)

    Worked example — July 2026, 100,000 THB revenue, 30,000 THB recipe-cost
    COGS, 50,000 THB recurring rent, and 40,000 THB cash spend (net of VAT):

        revenue        = 100,000
        recipe_cost_gm = 100,000 - 30,000 = 70,000
        net_profit     = 70,000 - 50,000  = 20,000
        cash_basis_gp  = 100,000 - 40,000 = 60,000
    """
    start, end = date(2026, 7, 1), date(2026, 7, 31)
    review = PeriodReview(
        start=start,
        end=end,
        revenue=D("100000"),
        cogs=D("30000"),
        gross_margin=D("70000"),
        segment_margins=(
            SegmentMargin(
                segment=Segment.CAFE,
                revenue=D("60000"),
                variable_costs=D("18000"),
                contribution_margin=D("42000"),
            ),
            SegmentMargin(
                segment=Segment.BAR,
                revenue=D("40000"),
                variable_costs=D("12000"),
                contribution_margin=D("28000"),
            ),
        ),
        flagged_revenue=D("0"),
        needs_attention=(),
        days=(),
        fixed_costs=FixedCostsForPeriod(lines=(), total=D("50000"), estimated=False),
        net_profit=D("20000"),
        goal=PeriodGoal(
            target=D("10000") * 31, actual=D("20000"), days_in_range=31
        ),
    )
    cash_spend = CashSpendForPeriod(
        total=D("40000"),
        by_bucket={"coffee": D("15000"), "taps": D("25000")},
    )

    report = build_profit_report(
        review=review, cash_spend=cash_spend, today=date(2026, 7, 15)
    )

    assert isinstance(report, ProfitReport)
    assert isinstance(report.tiles, ProfitTiles)
    assert report.tiles.revenue == D("100000")
    assert report.tiles.cash_basis_gp == D("60000")  # 100,000 - 40,000
    assert report.tiles.recipe_cost_gm == D("70000")
    assert report.tiles.net_profit == D("20000")
    # The two lenses are carried through for the template and later tickets
    # (two-lens P&L, charts, bestsellers extend this module).
    assert report.review is review
    assert report.cash_spend is cash_spend
    assert report.in_progress is True


def test_cash_basis_gp_is_revenue_minus_cash_spend_even_when_negative() -> None:
    """A month where cash spend exceeds revenue shows a negative cash-basis GP.

    The two lenses can disagree by design — a month with thin revenue and a
    big Makro restock shows a healthy recipe-cost GM (the beans will be sold
    later) but a negative cash-basis GP (the cash went out the door now).
    The tile reports the honest negative number, not zero.
    """
    start, end = date(2026, 7, 1), date(2026, 7, 31)
    review = _empty_review(start, end, revenue=D("10000"))
    cash_spend = CashSpendForPeriod(
        total=D("25000"), by_bucket={"coffee": D("25000")}
    )

    report = build_profit_report(
        review=review, cash_spend=cash_spend, today=date(2026, 8, 15)
    )

    assert report.tiles.cash_basis_gp == D("-15000")  # 10,000 - 25,000
    assert report.in_progress is False  # August viewed from a July range


def test_zero_cash_spend_makes_cash_basis_gp_equal_revenue() -> None:
    """A month with no cash-spend rows: cash-basis GP equals revenue.

    The cash-basis lens has nothing to subtract, so the tile collapses to
    the revenue figure rather than rendering an empty/zero state.
    """
    start, end = date(2026, 7, 1), date(2026, 7, 31)
    review = _empty_review(start, end, revenue=D("5000"))
    cash_spend = CashSpendForPeriod(total=D("0"), by_bucket={})

    report = build_profit_report(
        review=review, cash_spend=cash_spend, today=date(2026, 7, 31)
    )

    assert report.tiles.cash_basis_gp == D("5000")
    assert report.tiles.revenue == D("5000")


def test_in_progress_on_the_last_day_of_the_current_month() -> None:
    """Today landing on the month's last day still counts as in progress."""
    start, end = date(2026, 7, 1), date(2026, 7, 31)
    review = _empty_review(start, end)
    cash_spend = CashSpendForPeriod(total=D("0"), by_bucket={})

    report = build_profit_report(
        review=review, cash_spend=cash_spend, today=date(2026, 7, 31)
    )

    assert report.in_progress is True
