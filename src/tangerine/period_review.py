"""Recipe-cost period engine (Wave 2 slice 2, ADR-0004 decision 1).

Aggregates the daily review's recipe-cost math over an arbitrary inclusive
``[start, end]`` range: every sale is costed at the net price in effect on
its own date (slice 1's as-of-date lookup, via ``source.cost_book_as_of``),
so a one-day period agrees with the daily view by construction and a cost
edit never re-states a past period.

Pure engine over the same ``Source`` boundary the daily review consumes —
no I/O, no storage imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .ingestion import Source
from .margin import compute_item_margins, segment_margins_from_items
from .recipes import RecipeCatalog
from .types import (
    DAILY_PROFIT_TARGET_THB,
    ItemMargin,
    Money,
    Segment,
    SegmentMargin,
)


@dataclass(frozen=True)
class PeriodDay:
    """One day's headline inside a period — the drill-down row.

    Reliable rows only, same rule as the period headline. Quiet days carry
    zeros rather than being omitted, so a rendered period always shows every
    day in the range.
    """

    day: date
    revenue: Money
    cogs: Money
    gross_margin: Money


@dataclass(frozen=True)
class PeriodGoal:
    """The period's gross margin vs 10K THB/day x days in the range.

    ``basis`` names what ``actual`` is measured on. Until fixed costs land
    (Wave 2 slice 3) the only honest basis is ``"gross_margin"`` — the label
    is carried on the result so no surface can silently present it as net
    profit (issue #29: "honestly labelled — no net-profit line yet").
    """

    target: Money
    actual: Money
    days_in_range: int
    basis: str = "gross_margin"

    @property
    def met(self) -> bool:
        """True when the period's actual meets or exceeds the target."""
        return self.actual >= self.target

    @property
    def surplus(self) -> Money:
        """``actual − target`` (negative when the target is missed)."""
        return self.actual - self.target


@dataclass(frozen=True)
class FlaggedPeriodItem:
    """One item whose revenue the period could not honestly cost.

    The period's needs-attention row: an unmapped item (no recipe mapping)
    or a mapped item with an unpriced ingredient, aggregated over every day
    it sold in the range. ``segment`` is the resolved segment the daily view
    would show — the recipe's for a mapped-but-unpriced row, the sale's
    shift-stamp fallback for an unmapped one.
    """

    item_id: str
    name: str
    segment: Segment
    units_sold: int
    revenue: Money
    unmapped: bool
    unknown_price: bool


@dataclass(frozen=True)
class PeriodReview:
    """The report shape for an inclusive ``[start, end]`` range.

    Headline totals (``revenue`` / ``cogs`` / ``gross_margin``) sum only
    reliable rows — unmapped and unknown-price rows are excluded, as the
    daily view excludes them.

    ``segment_margins`` is the period's per-segment contribution margin,
    rolled up through the same path the daily view uses (mapped sale →
    recipe's segment; both segments always present, cafe-then-bar; a losing
    segment carries ``is_red``).

    The revenue sitting in flagged rows is surfaced as ``flagged_revenue``
    plus one aggregated ``needs_attention`` row per flagged item, so it is
    visible, not silently dropped (the daily view's rule, per the COGS
    recognition entry in ``CONTEXT.md``).
    """

    start: date
    end: date
    revenue: Money
    cogs: Money
    gross_margin: Money
    segment_margins: tuple[SegmentMargin, ...]
    flagged_revenue: Money
    needs_attention: tuple[FlaggedPeriodItem, ...]
    days: tuple[PeriodDay, ...]
    goal: PeriodGoal


def build_period_review(*, source: Source, start: date, end: date) -> PeriodReview:
    """Build the period review for the inclusive ``[start, end]`` range.

    Runs the per-item margin engine one day at a time, each day costed at
    that day's prices (``cost_book_as_of``, ADR-0004 decision 2), and sums
    the reliable rows into the period headline.
    """
    if end < start:
        raise ValueError(
            f"period end {end} precedes start {start}; range must be inclusive"
        )

    sales = source.sales()
    recipes = RecipeCatalog(list(source.recipes()), list(source.mappings()))

    counted_rows: list[ItemMargin] = []
    flagged_rows: list[ItemMargin] = []
    days: list[PeriodDay] = []
    current = start
    while current <= end:
        rows = compute_item_margins(
            sales=sales,
            recipes=recipes,
            cost=source.cost_book_as_of(current),
            day=current,
        )
        counted = [im for im in rows if not im.excluded_from_totals]
        counted_rows.extend(counted)
        flagged_rows.extend(im for im in rows if im.excluded_from_totals)
        day_revenue = sum((im.revenue for im in counted), Money("0"))
        day_cogs = sum((im.cogs for im in counted), Money("0"))
        days.append(
            PeriodDay(
                day=current,
                revenue=day_revenue,
                cogs=day_cogs,
                gross_margin=day_revenue - day_cogs,
            )
        )
        current += timedelta(days=1)

    revenue = sum((im.revenue for im in counted_rows), Money("0"))
    cogs = sum((im.cogs for im in counted_rows), Money("0"))
    gross_margin = revenue - cogs
    days_in_range = (end - start).days + 1
    return PeriodReview(
        start=start,
        end=end,
        revenue=revenue,
        cogs=cogs,
        gross_margin=gross_margin,
        segment_margins=segment_margins_from_items(counted_rows),
        flagged_revenue=sum((im.revenue for im in flagged_rows), Money("0")),
        needs_attention=_aggregate_flagged(flagged_rows),
        days=tuple(days),
        goal=PeriodGoal(
            target=DAILY_PROFIT_TARGET_THB * days_in_range,
            actual=gross_margin,
            days_in_range=days_in_range,
        ),
    )


def _aggregate_flagged(rows: list[ItemMargin]) -> tuple[FlaggedPeriodItem, ...]:
    """Roll per-day flagged rows into one needs-attention row per item.

    Units and revenue sum over the days the item sold; the flags and the
    resolved segment are per-item facts and are taken from the first day's
    row. Sorted by item id for determinism, matching the margin engine.
    """
    by_item: dict[str, list[ItemMargin]] = {}
    for im in rows:
        by_item.setdefault(im.item_id, []).append(im)
    return tuple(
        FlaggedPeriodItem(
            item_id=item_id,
            name=days[0].name,
            segment=days[0].segment,
            units_sold=sum(im.units_sold for im in days),
            revenue=sum((im.revenue for im in days), Money("0")),
            unmapped=days[0].unmapped,
            unknown_price=days[0].unknown_price,
        )
        for item_id, days in sorted(by_item.items())
    )


__all__ = [
    "FlaggedPeriodItem",
    "PeriodDay",
    "PeriodGoal",
    "PeriodReview",
    "build_period_review",
]
