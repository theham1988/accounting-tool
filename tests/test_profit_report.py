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
    aggregate_item_sales,
    build_profit_report,
    is_month_in_progress,
    rank_bestsellers,
)
from tangerine.types import ItemMargin, Money, Segment, SegmentMargin


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


# --- bestseller aggregation + ranking (#116, parent #112) -----------------------
#
# AC #116: two sales-side rankings on the Profit Report — top-N by total sales
# volume (THB) and top-N by total items (unit count). Both rankings share one
# per-item aggregation; the two lists are two sorts of the same aggregation.
# Sorting is descending by the chosen metric; ties are broken deterministically
# by ``item_id``; fewer-than-N items renders what exists. Unmapped items appear
# (their revenue counts toward the ranking) with a ``cm_unknown`` marker.
#
# The pure aggregation is fed per-day ``ItemMargin`` rows — the *same rows the
# period engine already produces* (``margins_over_range`` → ``DayMargins.item_
# margins``), so the rankings are sourced from the same sales the recipe-cost
# lens consumes. ``cm_unknown`` is True for any item that is ``unmapped`` OR
# ``unknown_price`` on its rows (a contribution-margin number cannot be honestly
# derived for either); the marker is the visible labelling for that, per the
# "sales-side, not contribution-margin" note the spec requires.


def _im(
    *,
    item_id: str,
    name: str | None = None,
    day: date | None = None,
    units_sold: int = 1,
    revenue: str = "0",
    unmapped: bool = False,
    unknown_price: bool = False,
) -> ItemMargin:
    """A minimal ``ItemMargin`` carrying only what the aggregation reads.

    ``revenue`` and ``units_sold`` are what the ranking sums; ``unmapped`` /
    ``unknown_price`` drive ``cm_unknown``; ``name`` falls back to ``item_id``
    (matching how the margin engine names an unmapped item — see
    ``compute_item_margins``'s ``name=item_id`` fallback). The other margin
    fields are irrelevant to the aggregation.
    """
    return ItemMargin(
        item_id=item_id,
        name=name if name is not None else item_id,
        segment=Segment.CAFE,
        day=day if day is not None else date(2026, 7, 1),
        units_sold=units_sold,
        sell_price=D("0"),
        cost_per_unit=D("0"),
        revenue=D(revenue),
        cogs=D("0"),
        gross_margin=D("0"),
        gross_margin_pct=None,
        unmapped=unmapped,
        unknown_price=unknown_price,
    )


def test_aggregate_groups_per_day_rows_by_item_id() -> None:
    """The aggregation collapses multi-day per-item rows into one row per item.

    ``margins_over_range`` emits one ``ItemMargin`` per day per item; the
    aggregation sums ``revenue`` and ``units_sold`` across those days. Worked
    example — latte sold on two days, chang on one:

        latte day 1: 1 unit, 80 THB
        latte day 2: 2 units, 160 THB   -> latte total: 3 units, 240 THB
        chang day 1: 1 unit, 90 THB     -> chang total: 1 unit,  90 THB

    The aggregated rows are keyed by ``item_id`` so the two rankings share
    one source list.
    """
    rows = [
        _im(item_id="i-latte", day=date(2026, 7, 1), units_sold=1, revenue="80"),
        _im(item_id="i-chang", day=date(2026, 7, 1), units_sold=1, revenue="90"),
        _im(
            item_id="i-latte", day=date(2026, 7, 2), units_sold=2, revenue="160"
        ),
    ]

    aggregated = aggregate_item_sales(rows)

    by_id = {a.item_id: a for a in aggregated}
    assert by_id["i-latte"].units_sold == 3
    assert by_id["i-latte"].revenue == D("240")
    assert by_id["i-chang"].units_sold == 1
    assert by_id["i-chang"].revenue == D("90")


def test_aggregate_marks_an_unmapped_item_cm_unknown() -> None:
    """An unmapped item appears in the aggregation with ``cm_unknown=True``.

    Unmapped items (no recipe) cannot have a contribution margin derived —
    they appear in the rankings (their revenue counts) with a ``cm_unknown``
    marker so the template can label them. The marker is the visible
    labelling the spec requires ("CM unknown"), not an exclusion.
    """
    rows = [
        _im(item_id="i-latte", revenue="80"),
        _im(item_id="i-mystery", revenue="100", unmapped=True),
    ]

    aggregated = aggregate_item_sales(rows)

    by_id = {a.item_id: a for a in aggregated}
    assert by_id["i-latte"].cm_unknown is False
    assert by_id["i-mystery"].cm_unknown is True


def test_aggregate_marks_an_unknown_price_item_cm_unknown() -> None:
    """A mapped-but-unpriced item is also ``cm_unknown``.

    A mapped item with an unpriced ingredient has unknown COGS, so its
    contribution margin cannot be honestly derived either — same marker,
    same treatment (it appears; its revenue counts).
    """
    rows = [
        _im(item_id="i-latte", revenue="80"),
        _im(item_id="i-stockout", revenue="100", unknown_price=True),
    ]

    by_id = {a.item_id: a for a in aggregate_item_sales(rows)}
    assert by_id["i-stockout"].cm_unknown is True


# --- ranking: two orderings, ties, fewer-than-N, zero-unit items ---------------


def test_rank_by_revenue_sorts_descending() -> None:
    """Top-N by total sales volume (THB) is sorted high-to-low by revenue.

        chang: 630 THB   -> rank 1
        latte: 240 THB   -> rank 2

    ``rank_bestsellers`` sorts the shared aggregation (computed once, fed
    into both rankings); the test mirrors that contract by aggregating
    then ranking.
    """
    aggregated = aggregate_item_sales(
        [
            _im(item_id="i-latte", revenue="240"),
            _im(item_id="i-chang", revenue="630"),
        ]
    )

    ranked = rank_bestsellers(aggregated, metric="revenue", limit=10)

    assert [r.item_id for r in ranked] == ["i-chang", "i-latte"]


def test_rank_by_units_sorts_descending() -> None:
    """Top-N by total items (unit count) is a *separate* sort of the same data.

        latte:  3 units   -> rank 1
        chang:  1 unit    -> rank 2

    The revenue ranking above (chang > latte) and the units ranking here
    (latte > chang) disagree — that is the whole point of two rankings: a
    partner sees both "what brings in the money" and "what we move a lot of".
    Both sorts consume the *same* aggregation (the spec's "shared
    aggregation" contract).
    """
    aggregated = aggregate_item_sales(
        [
            _im(item_id="i-latte", units_sold=3, revenue="240"),
            _im(item_id="i-chang", units_sold=1, revenue="630"),
        ]
    )

    ranked = rank_bestsellers(aggregated, metric="units", limit=10)

    assert [r.item_id for r in ranked] == ["i-latte", "i-chang"]


def test_rank_breaks_ties_deterministically_by_item_id() -> None:
    """Equal metric values tie-break by ``item_id`` (ascending).

    Two items with identical revenue: ``i-aaa`` and ``i-bbb`` both at 100 THB.
    The tie-break is ``item_id`` ascending, so ``i-aaa`` precedes ``i-bbb``
    regardless of input order. Determinism matters: the same aggregation
    must produce the same ranking every time.
    """
    aggregated = aggregate_item_sales(
        [
            _im(item_id="i-bbb", revenue="100"),
            _im(item_id="i-aaa", revenue="100"),
            _im(item_id="i-ccc", revenue="500"),  # unambiguous rank 1
        ]
    )

    ranked = rank_bestsellers(aggregated, metric="revenue", limit=10)

    assert [r.item_id for r in ranked] == ["i-ccc", "i-aaa", "i-bbb"]


def test_rank_limits_to_top_n() -> None:
    """``limit=N`` caps the list at N rows after sorting + tie-breaking."""
    aggregated = aggregate_item_sales(
        [
            _im(item_id="i-c", revenue="300"),
            _im(item_id="i-a", revenue="100"),
            _im(item_id="i-b", revenue="200"),
        ]
    )

    ranked = rank_bestsellers(aggregated, metric="revenue", limit=2)

    assert [r.item_id for r in ranked] == ["i-c", "i-b"]


def test_rank_renders_what_exists_when_fewer_than_n_items() -> None:
    """Fewer items than ``limit`` renders every item, not padding.

    Two items, ``limit=10`` — the list carries both, in order, with no
    zero-fill rows.
    """
    aggregated = aggregate_item_sales(
        [
            _im(item_id="i-latte", revenue="240"),
            _im(item_id="i-chang", revenue="630"),
        ]
    )

    ranked = rank_bestsellers(aggregated, metric="revenue", limit=10)

    assert len(ranked) == 2
    assert [r.item_id for r in ranked] == ["i-chang", "i-latte"]


def test_rank_excludes_items_with_no_sales_this_month() -> None:
    """Items with zero units AND zero revenue never sold — they don't rank.

    A zero-row would be a row from ``margins_over_range`` for an item that
    sold nothing (there shouldn't be any — the engine emits a row per
    (item, segment) that *sold* that day — but guarding against it keeps
    a stale or future drift from polluting the ranking with empty rows).
    An item that sold but earned zero revenue is different and *does*
    rank (it moved units), so the guard is ``units == 0 and revenue == 0``.
    """
    aggregated = aggregate_item_sales(
        [
            _im(item_id="i-silent", units_sold=0, revenue="0"),  # never sold
            _im(item_id="i-freebie", units_sold=2, revenue="0"),  # given away
            _im(item_id="i-latte", units_sold=3, revenue="240"),
        ]
    )

    ranked_by_rev = rank_bestsellers(aggregated, metric="revenue", limit=10)
    ranked_by_units = rank_bestsellers(aggregated, metric="units", limit=10)

    # The silent item is gone; the freebie (moved 2 units) still ranks.
    ids_by_rev = [r.item_id for r in ranked_by_rev]
    ids_by_units = [r.item_id for r in ranked_by_units]
    assert "i-silent" not in ids_by_rev
    assert "i-silent" not in ids_by_units
    assert "i-freebie" in ids_by_units


def test_rank_preserves_cm_unknown_marker_through_both_orderings() -> None:
    """An unmapped item's marker survives into both ranked lists.

    The marker is set during aggregation and carried through ranking so
    the template can render the "CM unknown" label next to the item in
    *both* lists (revenue and units) — not just one.
    """
    aggregated = aggregate_item_sales(
        [
            _im(item_id="i-latte", units_sold=3, revenue="240"),
            _im(
                item_id="i-mystery",
                units_sold=5,
                revenue="500",
                unmapped=True,
            ),
        ]
    )

    by_rev = rank_bestsellers(aggregated, metric="revenue", limit=10)
    by_units = rank_bestsellers(aggregated, metric="units", limit=10)

    mystery_rev = next(r for r in by_rev if r.item_id == "i-mystery")
    mystery_units = next(r for r in by_units if r.item_id == "i-mystery")
    assert mystery_rev.cm_unknown is True
    assert mystery_units.cm_unknown is True


def test_rank_empty_input_returns_empty_list() -> None:
    """A month with no sales aggregates to nothing — both lists empty."""
    assert rank_bestsellers([], metric="revenue", limit=10) == []
    assert rank_bestsellers([], metric="units", limit=10) == []
