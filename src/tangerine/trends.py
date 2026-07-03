"""Trend buckets over the recipe-cost period engine (Wave 2 slice 5, issue #32).

A trend is the period engine run once per bucket: each weekly or monthly
bucket is a ``build_period_review`` over that bucket's ``[start, end]`` range,
so a trend bucket's totals equal the same bucket rendered directly in
Period/Month mode by construction — same engine, same as-of-date prices
(ADR-0004 decisions 1 and 2).

Pure engine over the same ``Source`` boundary the daily and period reviews
consume — no I/O, no storage imports. Rendering (SVG geometry) lives in
``web/sparkline.py``, not here.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from .ingestion import Source
from .period_review import PeriodReview, build_period_review
from .types import Money

#: How many buckets a trend spans. ~12 weeks / 12 months is the scanning
#: volume ADR-0004 decision 5 sized the no-JS rendering for.
TREND_BUCKETS: int = 12

_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class TrendBucket:
    """One week or month inside a trend.

    ``review`` is the full period review for the bucket's range — the same
    object Period/Month mode renders for that range, so every metric a trend
    can plot (revenue, COGS, gross margin, segment CM, goal) is read off it
    rather than recomputed.
    """

    start: date
    end: date
    label: str
    review: PeriodReview


@dataclass(frozen=True)
class WeekdayAggregate:
    """One weekday's totals across the whole span (the Mondays-vs-Saturdays
    breakdown, PRD user story 19).

    ``day_count`` is how many of that weekday fall inside the span — quiet
    days count, so an average over it reads as a structural pattern rather
    than a lucky-day artifact. Averages are left to the caller
    (``total / day_count``) so one aggregate serves every metric.
    """

    weekday: int
    label: str
    day_count: int
    revenue: Money
    cogs: Money
    gross_margin: Money


@dataclass(frozen=True)
class TrendReport:
    """The trend shape for one span: buckets in chronological order, plus
    the per-weekday breakdown across the span's full range."""

    span: str
    anchor: date
    buckets: tuple[TrendBucket, ...]
    weekdays: tuple[WeekdayAggregate, ...]


def build_trends(
    *, source: Source, anchor: date, span: str = "weeks", buckets: int = TREND_BUCKETS
) -> TrendReport:
    """Build the trend report ending at ``anchor`` (usually yesterday).

    ``span="weeks"``: the last ``buckets`` calendar weeks (Monday–Sunday),
    ending with the week containing ``anchor``; that final, possibly partial
    week is truncated at ``anchor`` so a bucket never claims days that have
    not happened yet.

    ``span="months"``: the last ``buckets`` calendar months, ending with
    ``anchor``'s month. Months are always full calendar months — the range
    Month mode renders — so drilling into a month bucket shows the identical
    number.
    """
    if span == "weeks":
        ranges = _week_ranges(anchor, buckets)
    elif span == "months":
        ranges = _month_ranges(anchor, buckets)
    else:
        raise ValueError(f"unknown trend span {span!r}; expected 'weeks' or 'months'")

    built = tuple(
        TrendBucket(
            start=start,
            end=end,
            label=label,
            review=build_period_review(source=source, start=start, end=end),
        )
        for start, end, label in ranges
    )
    return TrendReport(
        span=span,
        anchor=anchor,
        buckets=built,
        weekdays=_weekday_aggregates(built, anchor=anchor),
    )


def _weekday_aggregates(
    buckets: tuple[TrendBucket, ...], *, anchor: date
) -> tuple[WeekdayAggregate, ...]:
    """Roll the buckets' per-day rows up by weekday, Monday first.

    Month buckets are full calendar months and can reach past the trend's
    anchor (the month the anchor lives in has days that have not happened
    yet). Those future days carry zeros in the period engine's per-day
    rows, so counting them would dilute the averages with future zero-days
    (worst early in the month). They are dropped here by clipping at the
    anchor — the same rule the weekly span applies via ``_week_ranges``.
    Bucket ranges never overlap, so after the clip every day is counted
    exactly once.
    """
    days = [
        day
        for bucket in buckets
        for day in bucket.review.days
        if day.day <= anchor
    ]
    aggregates: list[WeekdayAggregate] = []
    for weekday, label in enumerate(_WEEKDAY_LABELS):
        matching = [d for d in days if d.day.weekday() == weekday]
        aggregates.append(
            WeekdayAggregate(
                weekday=weekday,
                label=label,
                day_count=len(matching),
                revenue=sum((d.revenue for d in matching), Money("0")),
                cogs=sum((d.cogs for d in matching), Money("0")),
                gross_margin=sum((d.gross_margin for d in matching), Money("0")),
            )
        )
    return tuple(aggregates)


def _week_ranges(anchor: date, buckets: int) -> list[tuple[date, date, str]]:
    """Monday-to-Sunday weeks ending with the (truncated) week of ``anchor``."""
    anchor_monday = anchor - timedelta(days=anchor.weekday())
    ranges: list[tuple[date, date, str]] = []
    for i in range(buckets - 1, -1, -1):
        monday = anchor_monday - timedelta(weeks=i)
        sunday = monday + timedelta(days=6)
        end = min(sunday, anchor)
        ranges.append((monday, end, monday.strftime("%d %b")))
    return ranges


def _month_ranges(anchor: date, buckets: int) -> list[tuple[date, date, str]]:
    """Full calendar months ending with ``anchor``'s month."""
    year, month = anchor.year, anchor.month
    months: list[tuple[int, int]] = []
    for _ in range(buckets):
        months.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    ranges: list[tuple[date, date, str]] = []
    for y, m in reversed(months):
        first = date(y, m, 1)
        last = first.replace(day=calendar.monthrange(y, m)[1])
        ranges.append((first, last, first.strftime("%b %Y")))
    return ranges


__all__ = [
    "TREND_BUCKETS",
    "TrendBucket",
    "TrendReport",
    "WeekdayAggregate",
    "build_trends",
]
