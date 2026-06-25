"""Margin engine.

Given sales and recipes from a source, compute per-item and daily gross margin.
Pure function over inputs — no I/O, no mutation. The PRD defines:

    gross_margin = revenue - cogs
    cogs(item)   = sum over recipe ingredients of (quantity * purchase_price)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .ingestion import Source
from .types import DailyMargin, ItemMargin, Money, Recipe


class UnmappedItemError(Exception):
    """Raised when a sold item has no recipe mapping.

    Per PRD user story 12, unmapped sales must surface immediately rather than
    silently produce a zero-COGS margin.
    """


def recipe_cost(recipe: Recipe) -> Money:
    """Cost of producing one unit of the recipe's item."""
    return Money(sum(
        (ing.quantity * ing.purchase_price) for ing in recipe.ingredients
    ) or Decimal("0"))


def compute_daily_margin(source: Source, day: date) -> DailyMargin:
    """Compute item-level and rolled-up gross margin for a single day.

    Sales for other days are ignored. Every sold item must have a recipe; an
    unmapped item raises `UnmappedItemError`.
    """
    recipes_by_item = {recipe.item_id: recipe for recipe in source.recipes()}

    # Bucket sales for the day by item so we emit one ItemMargin per item, not
    # one per sale. This keeps the test seam shape stable as multi-unit sales
    # arrive in later slices.
    units_by_item: dict[str, int] = {}
    revenue_by_item: dict[str, Money] = {}

    for sale in source.sales():
        if sale.timestamp != day:
            continue
        if sale.item_id not in recipes_by_item:
            raise UnmappedItemError(
                f"Sold item {sale.item_id!r} has no recipe mapping"
            )
        units_by_item[sale.item_id] = (
            units_by_item.get(sale.item_id, 0) + sale.quantity
        )
        revenue_by_item[sale.item_id] = (
            revenue_by_item.get(sale.item_id, Money("0"))
            + sale.sell_price * sale.quantity
        )

    item_margins: list[ItemMargin] = []
    for item_id, units in sorted(units_by_item.items()):
        recipe = recipes_by_item[item_id]
        revenue = revenue_by_item[item_id]
        cogs = recipe_cost(recipe) * units
        item_margins.append(
            ItemMargin(
                item_id=item_id,
                name=recipe.name,
                segment=recipe.segment,
                day=day,
                units_sold=units,
                revenue=revenue,
                cogs=cogs,
                gross_margin=revenue - cogs,
            )
        )

    return DailyMargin(
        day=day,
        item_margins=tuple(item_margins),
        total_revenue=sum((im.revenue for im in item_margins), Money("0")),
        total_cogs=sum((im.cogs for im in item_margins), Money("0")),
        total_gross_margin=sum(
            (im.gross_margin for im in item_margins), Money("0")
        ),
    )
