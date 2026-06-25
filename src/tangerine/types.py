"""Schema-level types for the accounting domain.

These are the shapes that flow across the ingestion boundary and through the
margin engine. They are deliberately plain dataclasses: later slices may add
persistence, but the in-memory contract stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum


class Segment(str, Enum):
    """Business segment. Per PRD, every transaction/recipe/item is tagged."""

    CAFE = "cafe"
    BAR = "bar"


# Money is represented as Decimal throughout to avoid float rounding in THB.
Money = Decimal


@dataclass(frozen=True)
class Ingredient:
    """One input into a recipe.

    `purchase_price` is the current per-`unit` cost of this input (e.g. cost
    per 1 ml of beer, per 1 g of beans). Quantity in the recipe is expressed in
    the same `unit`.
    """

    sku_id: str
    name: str
    unit: str
    quantity: Decimal
    purchase_price: Money


@dataclass(frozen=True)
class Recipe:
    """Maps a Loyverse item to its inputs and yield.

    For slice 01 we model a recipe as a list of ingredients, each already
    carrying its current purchase price. The margin engine sums
    `quantity * purchase_price` across ingredients to get COGS.
    """

    item_id: str
    name: str
    segment: Segment
    ingredients: tuple[Ingredient, ...]


@dataclass(frozen=True)
class Sale:
    """One unit of one item sold at a point in time.

    Slice 01 is single-unit: one Sale == one sold unit. Quantity is carried on
    the sale (defaulting to 1) so later slices can extend without reshaping.
    """

    item_id: str
    timestamp: date
    sell_price: Money
    quantity: int = 1


@dataclass(frozen=True)
class ItemMargin:
    """Per-item margin for a single day.

    `revenue = sell_price * quantity_sold`; `cogs` is the recipe cost summed
    across units sold; `gross_margin = revenue - cogs`.
    """

    item_id: str
    name: str
    segment: Segment
    day: date
    units_sold: int
    revenue: Money
    cogs: Money
    gross_margin: Money


@dataclass(frozen=True)
class DailyMargin:
    """Roll-up of all item margins for a single day, split by segment."""

    day: date
    item_margins: tuple[ItemMargin, ...]
    total_revenue: Money
    total_cogs: Money
    total_gross_margin: Money
