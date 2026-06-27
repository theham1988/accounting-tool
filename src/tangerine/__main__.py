"""CLI entrypoint: run the pipeline against seeded data and print the margin.

    python -m tangerine

This exists so a human can see the pipeline produce a number end-to-end without
writing a test. Real sources (Loyverse, receipts) plug in here later.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .cost import CostBook
from .pipeline import run
from .seeded import SeededSource
from .types import Recipe, RecipeIngredient, Sale, Segment


def _seeded_source() -> SeededSource:
    # One bar sale (Chang draft) and one cafe sale (espresso latte) to
    # illustrate per-segment contribution margin (slice 07).
    #   Chang:  500 ml beer @ 0.07 THB/ml -> 35 cost, 120 sell -> 85 bar CM
    #   Latte:  20 g beans @ 2 THB/g + 200 ml milk @ 0.025 THB/ml -> 45 cost,
    #           120 sell -> 75 cafe CM
    chang_recipe = Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(
            RecipeIngredient(sku_id="chang-keg", quantity=Decimal("500")),
        ),
    )
    latte_recipe = Recipe(
        sku_id="espresso-latte",
        name="Espresso Latte",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="beans-arabica", quantity=Decimal("20")),
            RecipeIngredient(sku_id="milk-fresh", quantity=Decimal("200")),
        ),
    )
    day = date(2026, 6, 24)
    sales = [
        Sale(
            item_id="chang-draft-500",
            timestamp=day,
            sell_price=Decimal("120"),
        ),
        Sale(
            item_id="espresso-latte",
            timestamp=day,
            sell_price=Decimal("120"),
        ),
    ]
    cost = CostBook(
        {
            "chang-keg": (Decimal("0.07"), date(2026, 6, 1)),
            "beans-arabica": (Decimal("2"), date(2026, 6, 1)),
            "milk-fresh": (Decimal("0.025"), date(2026, 6, 1)),
        }
    )
    return SeededSource(sales=sales, recipes=[chang_recipe, latte_recipe], cost=cost)


def _money(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"))


def main() -> None:
    source = _seeded_source()
    day = source.sales()[0].timestamp
    result = run(source, day)
    print(f"Daily margin for {result.day}:")
    print(f"  total revenue:       {_money(result.total_revenue)} THB")
    print(f"  total COGS:          {_money(result.total_cogs)} THB")
    print(f"  total gross margin:  {_money(result.total_gross_margin)} THB")
    print("  segment contribution margin:")
    for sm in result.segment_margins:
        flag = "  [RED]" if sm.is_red else ""
        print(
            f"    [{sm.segment.value}] revenue={_money(sm.revenue)}  "
            f"variable_costs={_money(sm.variable_costs)}  "
            f"CM={_money(sm.contribution_margin)} THB{flag}"
        )
    for im in result.item_margins:
        print(
            f"  - [{im.segment.value}] {im.name}: "
            f"{im.units_sold}u  revenue={_money(im.revenue)}  "
            f"cogs={_money(im.cogs)}  margin={_money(im.gross_margin)} THB"
        )


if __name__ == "__main__":
    main()
