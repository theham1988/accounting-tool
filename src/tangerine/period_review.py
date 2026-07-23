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
from datetime import date
from decimal import Decimal

from .fixed_costs import (
    FixedCostEntry,
    FixedCostsForPeriod,
    fixed_costs_for_period,
)
from .ingestion import Source
from .margin import (
    gross_margin_pct,
    margins_over_range,
    segment_margins_from_items,
)
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
    """The period's net profit vs 10K THB/day x days in the range.

    ``basis`` names what ``actual`` is measured on. From Wave 2 slice 3 the
    Period/Month basis is ``"net_profit"`` (segment CM minus entity fixed
    costs — exact for a calendar month, a labelled apportioned estimate for
    a sub-month range); the daily view stays gross-margin-based (fixed costs
    are not daily). The label is carried on the result so the two bases stay
    visibly different, not silently interchangeable (PRD: "Goal comparison
    basis").
    """

    target: Money
    actual: Money
    days_in_range: int
    basis: str = "net_profit"

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

    ``fixed_costs`` is the entity-level fixed-cost block for the range
    (never allocated to a segment — the segment rows above stay pure
    contribution margin) and ``net_profit`` is ``gross_margin`` minus its
    total. When ``fixed_costs.estimated`` is set the net profit is a
    labelled estimate (sub-month apportionment, ADR-0004 decision 3).
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
    fixed_costs: FixedCostsForPeriod
    net_profit: Money
    goal: PeriodGoal


def build_period_review(
    *,
    source: Source,
    start: date,
    end: date,
    fixed_costs: list[FixedCostEntry] | None = None,
) -> PeriodReview:
    """Build the period review for the inclusive ``[start, end]`` range.

    Projects over the single as-of range pass (``margins_over_range``): one
    loop owns the multi-day costing — the catalog is built once and each day
    is costed at that day's prices (``cost_book_as_of``, ADR-0004 decision
    2). The per-day ``DayMargins`` slices are rolled up here into the
    period's reliable-rows headline and per-day drilldown rows.
    ``fixed_costs`` are the stored entity-level entries (Wave 2 slice 3);
    the ones applying to the range turn the gross margin into ``net_profit``,
    which is what the goal compares against 10K THB/day × days.
    """
    if end < start:
        raise ValueError(
            f"period end {end} precedes start {start}; range must be inclusive"
        )

    slices = margins_over_range(source, start, end)

    counted_rows: list[ItemMargin] = []
    flagged_rows: list[ItemMargin] = []
    days: list[PeriodDay] = []
    for slice_ in slices:
        rows = slice_.item_margins
        counted = [im for im in rows if not im.excluded_from_totals]
        counted_rows.extend(counted)
        flagged_rows.extend(im for im in rows if im.excluded_from_totals)
        day_revenue = sum((im.revenue for im in counted), Money("0"))
        day_cogs = sum((im.cogs for im in counted), Money("0"))
        days.append(
            PeriodDay(
                day=slice_.day,
                revenue=day_revenue,
                cogs=day_cogs,
                gross_margin=day_revenue - day_cogs,
            )
        )

    revenue = sum((im.revenue for im in counted_rows), Money("0"))
    cogs = sum((im.cogs for im in counted_rows), Money("0"))
    gross_margin = revenue - cogs
    days_in_range = (end - start).days + 1
    period_fixed = fixed_costs_for_period(
        start=start, end=end, entries=list(fixed_costs or [])
    )
    net_profit = gross_margin - period_fixed.total
    return PeriodReview(
        start=start,
        end=end,
        revenue=revenue,
        cogs=cogs,
        gross_margin=gross_margin,
        segment_margins=segment_margins_from_items(counted_rows, flagged_rows),
        flagged_revenue=sum((im.revenue for im in flagged_rows), Money("0")),
        needs_attention=_aggregate_flagged(flagged_rows),
        days=tuple(days),
        fixed_costs=period_fixed,
        net_profit=net_profit,
        goal=PeriodGoal(
            target=DAILY_PROFIT_TARGET_THB * days_in_range,
            actual=net_profit,
            days_in_range=days_in_range,
        ),
    )


@dataclass(frozen=True)
class ItemDay:
    """One day an item sold inside its performance view (issue #31).

    Days with no sales of the item are omitted — the day-by-day answers
    "when did it sell and at what margin", not "render a calendar".
    """

    day: date
    units_sold: int
    revenue: Money
    cogs: Money
    gross_margin: Money


@dataclass(frozen=True)
class ItemPerformance:
    """One mapped item's performance over ``[start, end]`` (issue #31).

    The drill-down's last zoom step: units, revenue, recipe-cost COGS (each
    day costed at its own day's prices), gross margin and %, day-by-day rows,
    and the target-margin flag over the whole period. ``sku_id`` is the SKU
    the item maps to — the "edit recipe" link's target in Admin.
    """

    item_id: str
    name: str
    sku_id: str
    start: date
    end: date
    units_sold: int
    revenue: Money
    cogs: Money
    gross_margin: Money
    gross_margin_pct: Decimal | None
    target_gross_margin_pct: Decimal | None
    below_target: bool
    days: tuple[ItemDay, ...]


def build_item_performance(
    *, source: Source, item_id: str, start: date, end: date
) -> ItemPerformance | None:
    """Build one item's performance view, or None when it cannot be costed.

    Projects over the single as-of range pass (``margins_over_range``), the
    same pass ``build_period_review`` uses — so the item's period numbers
    agree with the period and day views by construction (shared as-of-date
    pricing) — and keeps only ``item_id``'s reliable rows. Returns None for
    an unmapped item (no recipe, so no recipe-cost to show; its fix path is
    the needs-attention link, per issue #31).
    """
    if end < start:
        raise ValueError(
            f"period end {end} precedes start {start}; range must be inclusive"
        )

    recipes = RecipeCatalog(list(source.recipes()), list(source.mappings()))
    recipe = recipes.for_item(item_id)
    if recipe is None:
        return None

    day_rows: list[ItemDay] = []
    for slice_ in margins_over_range(source, start, end):
        row = next(
            (im for im in slice_.item_margins if im.item_id == item_id), None
        )
        if row is None or row.excluded_from_totals:
            continue
        day_rows.append(
            ItemDay(
                day=slice_.day,
                units_sold=row.units_sold,
                revenue=row.revenue,
                cogs=row.cogs,
                gross_margin=row.gross_margin,
            )
        )

    revenue = sum((d.revenue for d in day_rows), Money("0"))
    cogs = sum((d.cogs for d in day_rows), Money("0"))
    gross_margin = revenue - cogs
    gross_margin_pct_value = gross_margin_pct(gross_margin, revenue)
    # Compare the period-level margin % against the target — the same number
    # the view shows next to this flag. OR-ing per-day flags can contradict
    # the displayed period %, because as-of-date pricing lets an item dip
    # below target on a low-volume day yet land above it for the period.
    below_target = (
        recipe.target_gross_margin_pct is not None
        and gross_margin_pct_value is not None
        and gross_margin_pct_value < recipe.target_gross_margin_pct
    )
    return ItemPerformance(
        item_id=item_id,
        name=recipe.name,
        sku_id=recipes.sku_for_item(item_id) or recipe.sku_id,
        start=start,
        end=end,
        units_sold=sum(d.units_sold for d in day_rows),
        revenue=revenue,
        cogs=cogs,
        gross_margin=gross_margin,
        gross_margin_pct=gross_margin_pct_value,
        target_gross_margin_pct=recipe.target_gross_margin_pct,
        below_target=below_target,
        days=tuple(day_rows),
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
    "ItemDay",
    "ItemPerformance",
    "PeriodDay",
    "PeriodGoal",
    "PeriodReview",
    "build_item_performance",
    "build_period_review",
]
