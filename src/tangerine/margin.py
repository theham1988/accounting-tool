"""Margin engine.

Given sales, recipes, and a cost book, compute per-item and daily gross
margin. Pure functions over inputs — no I/O, no mutation. The PRD defines:

    gross_margin = revenue - cogs
    cogs(item)   = (sum over recipe ingredients of quantity * current_unit_cost)
                   / yield_units

Per slice 04, the current unit cost of each ingredient SKU is looked up from
the ``CostBook`` (which tracks the latest approved purchase price), so a
recipe is a formula and a re-pricing flows straight into margin without the
recipe changing.

Rows whose margin cannot be trusted — unmapped items (no recipe) or items
where an ingredient SKU has no approved price — are flagged and excluded
from the daily totals: their COGS is unknown, so booking their revenue as
margin would over-state profitability. Their revenue is surfaced separately
on the ``DailyMargin`` so it stays visible.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .cost import CostBook, cost_per_unit
from .ingestion import Source
from .recipes import RecipeCatalog
from .segments import segment_of_sale
from .types import (
    DailyMargin,
    ItemMargin,
    Money,
    Recipe,
    Sale,
    Segment,
    SegmentMargin,
)

# Gross margin % is reported to two decimal places (a THB cent of precision
# on a ratio). Items with no revenue, or flagged rows whose ratio is
# meaningless, carry None instead.
_MARGIN_PCT_QUANT = Decimal("0.01")


def recipe_input_cost(recipe: Recipe, cost: CostBook) -> Money:
    """Cost of executing the recipe once (before dividing by yield).

    Sums each ingredient's ``quantity`` × its SKU's current cost per unit.
    """
    return Money(
        sum(
            (ing.quantity * cost_per_unit(cost, ing.sku_id))
            for ing in recipe.ingredients
        )
        or Decimal("0")
    )


def recipe_cost(recipe: Recipe, cost: CostBook) -> Money:
    """Cost of executing the recipe once (alias of ``recipe_input_cost``).

    Retained for the slice-04 worked-example vocabulary ("recipe cost = sum
    of inputs"). For the per-saleable-unit cost use
    ``recipe_cost_per_unit``, which divides by yield.
    """
    return recipe_input_cost(recipe, cost)


def recipe_cost_per_unit(recipe: Recipe, cost: CostBook) -> Money:
    """Cost of one saleable unit produced by the recipe.

    ``recipe_input_cost / yield_units``. A single-pour recipe (yield 1) has
    per-unit cost equal to its input cost; a 1L pitcher recipe yielding two
    500ml pours halves it.
    """
    if recipe.yield_units <= 1:
        return recipe_input_cost(recipe, cost)
    return Money(recipe_input_cost(recipe, cost) / recipe.yield_units)


def has_unknown_price(recipe: Recipe, cost: CostBook) -> bool:
    """True if any ingredient SKU has no approved price in the cost book.

    Such a recipe cannot be costed honestly; its margin row is flagged and
    excluded from the daily totals rather than silently zero-costed.
    """
    return any(
        cost.price(ing.sku_id) is None for ing in recipe.ingredients
    )


def gross_margin_pct(gross_margin: Money, revenue: Money) -> Decimal | None:
    """Gross margin as a percentage of revenue, to 2 dp. None when no revenue."""
    if revenue == 0:
        return None
    return (gross_margin / revenue * Decimal("100")).quantize(_MARGIN_PCT_QUANT)


def compute_item_margins(
    *,
    sales: list[Sale],
    recipes: RecipeCatalog,
    cost: CostBook,
    day: date,
) -> list[ItemMargin]:
    """Per-item margin table for a single day.

    Sales on other days are ignored. Each sold item resolves to a recipe via
    the catalog (item -> SKU -> recipe). Three outcomes per item:

      - mapped and fully priced     -> normal margin row, included in totals
      - mapped but a SKU unpriced   -> flagged ``unknown_price``, excluded
      - unmapped (no SKU/recipe)    -> flagged ``unmapped``, excluded

    Flagged rows are returned (so the daily review surfaces them) but
    ``excluded_from_totals`` is True on them, so ``compute_daily_margin``
    sums only reliable rows.

    Output is one ``ItemMargin`` per distinct item id sold that day, sorted by
    item id for determinism.
    """
    units_by_item: dict[str, int] = {}
    revenue_by_item: dict[str, Money] = {}
    sell_price_by_item: dict[str, Money] = {}
    # Resolved segment per item, for flagged rows (unmapped items take their
    # segment from the shift fallback on the sale). For mapped items the
    # recipe's segment is used directly, so this is only consulted on the
    # unmapped branch; we still compute it for every item for simplicity.
    segment_by_item: dict[str, Segment] = {}

    for sale in sales:
        if sale.timestamp != day:
            continue
        units_by_item[sale.item_id] = (
            units_by_item.get(sale.item_id, 0) + sale.quantity
        )
        revenue_by_item[sale.item_id] = (
            revenue_by_item.get(sale.item_id, Money("0"))
            + sale.sell_price * sale.quantity
        )
        # Per-unit sell price: take the first sale's price (Loyverse sell price
        # is the menu price; intra-day repricing between syncs is accepted as
        # stale per the PRD sync note).
        sell_price_by_item.setdefault(sale.item_id, sale.sell_price)
        # First sale seen for the item wins the shift-fallback segment. Used
        # only when the item is unmapped (no recipe); a later recipe hit
        # overrides it via recipe.segment.
        segment_by_item.setdefault(sale.item_id, segment_of_sale(sale, recipe=None))

    rows: list[ItemMargin] = []
    for item_id in sorted(units_by_item):
        recipe = recipes.for_item(item_id)
        units = units_by_item[item_id]
        revenue = revenue_by_item[item_id]
        sell_price = sell_price_by_item[item_id]

        if recipe is None:
            rows.append(_flagged_row(
                item_id=item_id,
                name=item_id,
                segment=segment_by_item[item_id],
                day=day,
                units=units,
                sell_price=sell_price,
                revenue=revenue,
            ))
            continue

        unpriced = has_unknown_price(recipe, cost)
        if unpriced:
            # Mapped, but at least one ingredient has no approved price.
            # Surface the row (with the recipe's name/segment) but exclude it
            # from totals — its COGS is unknown.
            rows.append(_flagged_row(
                item_id=item_id,
                name=recipe.name,
                segment=recipe.segment,
                day=day,
                units=units,
                sell_price=sell_price,
                revenue=revenue,
                unknown_price=True,
            ))
            continue

        cpu = recipe_cost_per_unit(recipe, cost)
        cogs = cpu * units
        gm = revenue - cogs
        pct = gross_margin_pct(gm, revenue)
        below = (
            recipe.target_gross_margin_pct is not None
            and pct is not None
            and pct < recipe.target_gross_margin_pct
        )
        rows.append(
            ItemMargin(
                item_id=item_id,
                name=recipe.name,
                segment=recipe.segment,
                day=day,
                units_sold=units,
                sell_price=sell_price,
                cost_per_unit=cpu,
                revenue=revenue,
                cogs=cogs,
                gross_margin=gm,
                gross_margin_pct=pct,
                unmapped=False,
                unknown_price=False,
                below_target=below,
            )
        )
    return rows


def _flagged_row(
    *,
    item_id: str,
    name: str,
    segment: Segment,
    day: date,
    units: int,
    sell_price: Money,
    revenue: Money,
    unknown_price: bool = False,
) -> ItemMargin:
    """Build a flagged margin row (unmapped or unknown-price).

    Flagged rows carry the real revenue (so it can be surfaced) but zero
    COGS and a None margin %: their cost is unknown, so any margin number
    would be misleading. ``excluded_from_totals`` is True on them.

    ``segment`` is the resolved segment for the row: the recipe's segment when
    the row is mapped-but-unpriced, or the shift-fallback segment (from the
    sale) when the row is unmapped. Slice 07 tags every row.
    """
    return ItemMargin(
        item_id=item_id,
        name=name,
        segment=segment,
        day=day,
        units_sold=units,
        sell_price=sell_price,
        cost_per_unit=Money("0"),
        revenue=revenue,
        cogs=Money("0"),
        gross_margin=Money("0"),
        gross_margin_pct=None,
        unmapped=not unknown_price,
        unknown_price=unknown_price,
        below_target=False,
    )


def compute_daily_margin(source: Source, day: date) -> DailyMargin:
    """Compute item-level and rolled-up gross margin for a single day.

    Recipes come from ``source.recipes()``; their ingredient costs are looked
    up from ``source.cost_book()``. Rows flagged ``unmapped`` or
    ``unknown_price`` are excluded from the totals (their COGS is unknown);
    their revenue is summed into ``flagged_revenue`` so it stays visible.

    Per-segment contribution margins (slice 07) are populated from the
    reliable rows only: flagged rows have unknown COGS, so booking their
    revenue into a segment's CM would over-state it. Both segments are always
    present; a segment with no reliable sales carries zeros.
    """
    recipes = RecipeCatalog(list(source.recipes()))
    cost = source.cost_book()
    rows = compute_item_margins(
        sales=source.sales(), recipes=recipes, cost=cost, day=day
    )
    counted = [im for im in rows if not im.excluded_from_totals]
    flagged = [im for im in rows if im.excluded_from_totals]
    return DailyMargin(
        day=day,
        item_margins=tuple(rows),
        total_revenue=sum((im.revenue for im in counted), Money("0")),
        total_cogs=sum((im.cogs for im in counted), Money("0")),
        total_gross_margin=sum((im.gross_margin for im in counted), Money("0")),
        flagged_revenue=sum((im.revenue for im in flagged), Money("0")),
        segment_margins=segment_margins_from_items(counted),
    )


def segment_margins_from_items(rows: list[ItemMargin]) -> tuple[SegmentMargin, ...]:
    """Roll reliable item-margin rows up into per-segment contribution margin.

    Only reliable rows (``excluded_from_totals`` False) are summed: a flagged
    row's COGS is unknown, so its revenue cannot honestly contribute to a
    segment's CM (PRD user story 20: segment CM must stay "clean and
    defensible"). Both segments are always returned, in canonical order
    (see ``_SEGMENT_ORDER``); a segment with no reliable rows carries zeros.

    Today variable costs == COGS (direct labor is "if tracked" per issue 07
    and not tracked yet), so ``variable_costs`` is the sum of each row's COGS.
    """
    by_segment = _empty_segment_buckets()
    for im in rows:
        bucket = by_segment[im.segment]
        bucket["revenue"] += im.revenue
        bucket["cogs"] += im.cogs
    return _build_segment_margins(by_segment)


def compute_period_segment_margins(
    source: Source, start: date, end: date
) -> tuple[SegmentMargin, ...]:
    """Per-segment contribution margin over an inclusive ``[start, end]`` range.

    Issue 07 requires per-segment CM "for any period", not just one day. This
    runs the per-item margin engine for each day in the range, rolls each day's
    reliable rows into segment CMs via ``segment_margins_from_items`` (the
    single honest path), and sums the per-day segment CMs into the period
    total. Both segments are always returned.
    """
    if end < start:
        raise ValueError(
            f"period end {end} precedes start {start}; range must be inclusive"
        )
    sales = source.sales()
    recipes = RecipeCatalog(list(source.recipes()))
    cost = source.cost_book()

    accumulated = _empty_segment_buckets()
    current = start
    while current <= end:
        rows = compute_item_margins(
            sales=sales, recipes=recipes, cost=cost, day=current
        )
        counted = [im for im in rows if not im.excluded_from_totals]
        for sm in segment_margins_from_items(counted):
            bucket = accumulated[sm.segment]
            bucket["revenue"] += sm.revenue
            bucket["cogs"] += sm.variable_costs
        current = current + timedelta(days=1)

    return _build_segment_margins(accumulated)


# Canonical display order for segment roll-ups: cafe first, then bar. The
# ``Segment`` enum lists BAR before CAFE and ``Segment.value`` is alphabetical
# ("bar" < "cafe"), so neither enum order nor ``.value`` sort gives cafe-first
# — hence this explicit key.
_SEGMENT_ORDER: dict[Segment, int] = {Segment.CAFE: 0, Segment.BAR: 1}


def _empty_segment_buckets() -> dict[Segment, dict[str, Money]]:
    """A fresh ``{segment: {revenue, cogs}}`` accumulator over all segments."""
    return {
        seg: {"revenue": Money("0"), "cogs": Money("0")} for seg in Segment
    }


def _build_segment_margins(
    by_segment: dict[Segment, dict[str, Money]]
) -> tuple[SegmentMargin, ...]:
    """Turn a ``{segment: {revenue, cogs}}`` accumulator into SegmentMargins.

    One ``SegmentMargin`` per segment in canonical order (cafe-then-bar).
    Today variable costs == COGS, so ``contribution_margin = revenue - cogs``.
    """
    ordered = sorted(
        by_segment.items(), key=lambda kv: _SEGMENT_ORDER[kv[0]]
    )
    return tuple(
        SegmentMargin(
            segment=seg,
            revenue=bucket["revenue"],
            variable_costs=bucket["cogs"],
            contribution_margin=bucket["revenue"] - bucket["cogs"],
        )
        for seg, bucket in ordered
    )
