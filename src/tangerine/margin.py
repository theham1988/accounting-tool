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

from datetime import date
from decimal import Decimal

from .cost import CostBook, cost_per_unit
from .ingestion import Source
from .recipes import RecipeCatalog
from .types import (
    DailyMargin,
    ItemMargin,
    Money,
    Recipe,
    Sale,
    Segment,
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
    day: date,
    units: int,
    sell_price: Money,
    revenue: Money,
    segment: Segment = Segment.BAR,
    unknown_price: bool = False,
) -> ItemMargin:
    """Build a flagged margin row (unmapped or unknown-price).

    Flagged rows carry the real revenue (so it can be surfaced) but zero
    COGS and a None margin %: their cost is unknown, so any margin number
    would be misleading. ``excluded_from_totals`` is True on them.

    ``segment`` defaults to BAR; slice 07 will tag by Loyverse category /
    shift timestamp. For a flagged row the flag (not the segment) is what
    surfaces it for review.
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
    )
