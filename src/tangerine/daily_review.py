"""Daily 9am review view (slice 11).

The single daily surface a partner opens every morning (PRD user story 29).
Issue 11: it surfaces, in one fast-scan view, everything that needs attention
from yesterday:

  - yesterday's revenue, COGS, gross margin
  - per-segment contribution margin with red flags where CM < 0
  - top/bottom items by margin and by sell volume
  - items whose actual margin is below their set target
  - anomaly flags from slice 10 (voids, drawer variance, clustering)
  - items sold without a recipe mapping
  - progress toward the 10,000 THB/day target (7-day rolling average vs target)

The review is a pure function over its inputs. It composes the slice-04 daily
margin engine (for the financial numbers + per-segment CM) with the slice-10
anomaly detector (for cash/void flags); both halves are already tested in their
own E2E seams, so this slice's job is to wire them together and surface the
fields a fast morning scan needs.

Scope decisions (confirmed before code):

  - **Goal comparison number**: 7-day rolling average of the daily gross margin
    (= sum of segment CMs today; direct labor is not tracked, and fixed costs
    are not daily-allocated per PRD user story 20 / issue 08).
  - **Anomaly window**: yesterday only (the review's review_date). Matches the
    rest of the view's "yesterday" framing.
  - **Anomaly inputs**: explicit parameters. The ``Source`` Protocol yields only
    sales/recipes/cost_book; voids, closes, and per-cashier sales_counts are
    passed in by the caller so this slice does not widen the ingestion boundary.
  - **Top/bottom lists**: 3 items each, ranked by margin and by units sold;
    flagged rows (unmapped / unknown-price) are excluded because their margins
    are meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .anomaly import AnomalyConfig, detect_anomalies
from .ingestion import Source
from .margin import compute_daily_margin, margins_over_range
from .types import (
    AnomalyFlag,
    DailyMargin,
    DAILY_PROFIT_TARGET_THB,
    ItemMargin,
    Money,
    SegmentMargin,
    ShiftClose,
    Void,
)


#: How many items each "top"/"bottom" list carries (issue 11: "top and bottom
#: items by margin and sell volume"). Three is enough for a fast morning scan
#: without overwhelming the surface; surfaced as a module constant so a later
#: slice can relax it without re-architecting.
TOP_BOTTOM_COUNT: int = 3

#: How wide the goal-tracking rolling average is. PRD user story 18: "running
#: 7-day average vs target." Surfaced as a module constant so the window is
#: named in one place.
ROLLING_AVERAGE_DAYS: int = 7


@dataclass(frozen=True)
class ItemRanking:
    """Top- or bottom-N ranking of items by a single metric.

    Carries the ranked ``ItemMargin`` rows (already filtered to reliable rows
    only — flagged unmapped/unknown-price rows are excluded because their
    margins are meaningless). The metric the list is ranked on lives on each
    row's ``gross_margin`` (for the margin ranking) or ``units_sold`` (for the
    volume ranking).
    """

    items: tuple[ItemMargin, ...]


@dataclass(frozen=True)
class GoalProgress:
    """7-day rolling-average daily gross margin vs the 10,000 THB/day target.

    Per PRD user story 18 the daily review shows "progress toward the 10K
    THB/day goal" as a 7-day rolling average. The comparison number is the
    daily ``total_gross_margin`` (revenue − COGS, = sum of segment CMs today);
    direct labor is not tracked and fixed costs are deliberately not
    daily-allocated (PRD user story 20 / issue 08 — fixed costs land at entity
    level on the monthly view only).

    - ``rolling_average``  mean daily gross margin over the trailing
                          ``ROLLING_AVERAGE_DAYS`` days (counting only days
                          inside the source's sales range, so a brand-new
                          venue with one day of sales reports that one day)
    - ``target``          the daily 10K THB target (``DAILY_PROFIT_TARGET_THB``)
    - ``days_in_window``  how many distinct days were averaged
    """

    rolling_average: Money
    target: Money
    days_in_window: int

    @property
    def met(self) -> bool:
        """True when the rolling average meets or exceeds the daily target."""
        return self.rolling_average >= self.target

    @property
    def surplus(self) -> Money:
        """``rolling_average − target`` (negative when the target is missed)."""
        return self.rolling_average - self.target


@dataclass(frozen=True)
class DailyReview:
    """The 9am review object — one fast-scan surface for one day.

    Mirrors the daily-margin numbers (revenue, COGS, gross margin) plus the
    per-segment contribution margins, plus the "needs attention" signals the
    partner scans for: top/bottom items by margin and volume, below-target
    items, unmapped items, anomaly flags, and goal progress.

    The underlying ``daily`` is the slice-04 ``DailyMargin``; this object adds
    the things slice 04 does not surface (rankings, anomaly flags, goal
    progress) so the review surface has one shape.
    """

    day: date
    revenue: Money
    cogs: Money
    gross_margin: Money
    segment_margins: tuple[SegmentMargin, ...]
    daily: DailyMargin
    top_by_margin: ItemRanking
    bottom_by_margin: ItemRanking
    top_by_volume: ItemRanking
    bottom_by_volume: ItemRanking
    below_target_items: tuple[ItemMargin, ...]
    unmapped_items: tuple[ItemMargin, ...]
    anomaly_flags: tuple[AnomalyFlag, ...]
    goal: GoalProgress


def build_daily_review(
    *,
    source: Source,
    review_date: date,
    voids: list[Void] | None = None,
    closes: list[ShiftClose] | None = None,
    sales_counts: dict[str, int] | None = None,
    drawer_short_rate_threshold: Decimal | None = None,
) -> DailyReview:
    """Build the daily 9am review for ``review_date``.

    The review composes:

      - the slice-04 daily-margin engine (revenue, COGS, GM, per-segment CM,
        item margins with unmapped/unknown-price/below-target flags)
      - the slice-10 anomaly detector, run over ``review_date`` only (yesterday)

    Anomaly inputs (``voids`` / ``closes`` / ``sales_counts``) are passed in by
    the caller. The ``drawer_short_rate_threshold`` mirrors
    ``AnomalyConfig.drawer_short_rate_threshold`` — required when any closes are
    present, surfaced here so the caller does not have to import ``AnomalyConfig``
    just to wire the review. A reviewer who passes no cash/void data gets an
    empty anomaly section (yesterday's review for a day with no closes).

    The rolling-average goal uses the trailing ``ROLLING_AVERAGE_DAYS`` days,
    counting only days that fall inside the source's sales range.
    """
    daily = compute_daily_margin(source, review_date)

    reliable = [im for im in daily.item_margins if not im.excluded_from_totals]

    anomaly_flags = _run_anomaly_detection(
        review_date=review_date,
        voids=voids or [],
        closes=closes or [],
        sales_counts=sales_counts or {},
        drawer_short_rate_threshold=drawer_short_rate_threshold,
    )

    goal = _compute_goal_progress(source, review_date)

    return DailyReview(
        day=review_date,
        revenue=daily.total_revenue,
        cogs=daily.total_cogs,
        gross_margin=daily.total_gross_margin,
        segment_margins=daily.segment_margins,
        daily=daily,
        top_by_margin=_rank_top_by_margin(reliable),
        bottom_by_margin=_rank_bottom_by_margin(reliable),
        top_by_volume=_rank_top_by_volume(reliable),
        bottom_by_volume=_rank_bottom_by_volume(reliable),
        below_target_items=tuple(
            im for im in reliable if im.below_target
        ),
        unmapped_items=tuple(
            im for im in daily.item_margins if im.unmapped
        ),
        anomaly_flags=tuple(anomaly_flags),
        goal=goal,
    )


# --- rankings ----------------------------------------------------------------


def _rank_top_by_margin(rows: list[ItemMargin]) -> ItemRanking:
    """Top-N items by gross margin (highest first)."""
    ranked = sorted(rows, key=lambda im: im.gross_margin, reverse=True)
    return ItemRanking(items=tuple(ranked[:TOP_BOTTOM_COUNT]))


def _rank_bottom_by_margin(rows: list[ItemMargin]) -> ItemRanking:
    """Bottom-N items by gross margin (lowest first)."""
    ranked = sorted(rows, key=lambda im: im.gross_margin)
    return ItemRanking(items=tuple(ranked[:TOP_BOTTOM_COUNT]))


def _rank_top_by_volume(rows: list[ItemMargin]) -> ItemRanking:
    """Top-N items by units sold (highest first)."""
    ranked = sorted(rows, key=lambda im: im.units_sold, reverse=True)
    return ItemRanking(items=tuple(ranked[:TOP_BOTTOM_COUNT]))


def _rank_bottom_by_volume(rows: list[ItemMargin]) -> ItemRanking:
    """Bottom-N items by units sold (lowest first)."""
    ranked = sorted(rows, key=lambda im: im.units_sold)
    return ItemRanking(items=tuple(ranked[:TOP_BOTTOM_COUNT]))


# --- anomaly detection (yesterday window) -----------------------------------


def _run_anomaly_detection(
    *,
    review_date: date,
    voids: list[Void],
    closes: list[ShiftClose],
    sales_counts: dict[str, int],
    drawer_short_rate_threshold: Decimal | None,
) -> list[AnomalyFlag]:
    """Run slice-10 anomaly detection over yesterday's window.

    The window is ``review_date`` only (issue 11: "anomaly flags from slice 10
    appear" — the rest of the view is also "yesterday"). An empty window (no
    closes, no voids, no sales) returns no flags without raising, so a review
    for a quiet day does not require the caller to invent inputs.

    ``drawer_short_rate_threshold`` is required only when any closes are
    present (mirrors ``AnomalyConfig``'s policy); passing ``None`` with closes
    raises ``ValueError`` rather than silently defaulting.
    """
    if not voids and not closes and not sales_counts:
        return []

    config = AnomalyConfig(
        start=review_date,
        end=review_date,
        drawer_short_rate_threshold=drawer_short_rate_threshold,
    )
    return detect_anomalies(
        config=config,
        voids=voids,
        closes=closes,
        sales_counts=sales_counts,
    )


# --- goal progress (7-day rolling average) ----------------------------------


def _compute_goal_progress(source: Source, review_date: date) -> GoalProgress:
    """7-day rolling-average daily gross margin vs the 10K THB/day target.

    Counts only days that fall inside the source's sales range. A brand-new
    venue with one day of sales reports that one day's gross margin as its
    rolling average; this is the honest number, not a fabricated six zeros
    that would under-state progress for the first week.

    Projects over the single as-of range pass (``margins_over_range``): one
    loop owns the trailing window's costing, the same path the daily view
    and period review take, so the goal agrees with the per-day gross
    margins by construction.
    """
    sales = source.sales()
    if not sales:
        return GoalProgress(
            rolling_average=Money("0"),
            target=DAILY_PROFIT_TARGET_THB,
            days_in_window=0,
        )

    earliest = min(s.timestamp for s in sales)
    window_start = max(earliest, review_date - timedelta(days=ROLLING_AVERAGE_DAYS - 1))

    # ``review_date`` may sit before the earliest sale (the "empty day" a
    # partner navigates to): the window is empty, the old per-day loop
    # simply never iterated. Guard the range pass the same way rather than
    # calling it with ``end < start``.
    if window_start > review_date:
        return GoalProgress(
            rolling_average=Money("0"),
            target=DAILY_PROFIT_TARGET_THB,
            days_in_window=0,
        )

    slices = margins_over_range(source, window_start, review_date)
    total = Money("0")
    days_seen = 0
    for slice_ in slices:
        rows = slice_.item_margins
        counted = [im for im in rows if not im.excluded_from_totals]
        total += sum((im.gross_margin for im in counted), Money("0"))
        days_seen += 1

    average = (
        Money(total / Decimal(days_seen))
        if days_seen > 0
        else Money("0")
    )
    return GoalProgress(
        rolling_average=average,
        target=DAILY_PROFIT_TARGET_THB,
        days_in_window=days_seen,
    )


__all__ = [
    "DailyReview",
    "GoalProgress",
    "ItemRanking",
    "ROLLING_AVERAGE_DAYS",
    "TOP_BOTTOM_COUNT",
    "build_daily_review",
]
