"""CLI entrypoint: run the pipeline against seeded data and print the margin.

    python -m tangerine

This exists so a human can see the pipeline produce a number end-to-end without
writing a test. Real sources (Loyverse, receipts) plug in here later.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .pipeline import run
from .seeded import SeededSource
from .types import Ingredient, Recipe, Sale, Segment


def _seeded_source() -> SeededSource:
    # One Chang draft sold for 120 THB. Recipe is 500 ml of beer at 0.07 THB/ml:
    # a ~5000 THB keg yields ~70 litres of billable pour, so a 500 ml pour
    # costs ~35 THB and the gross margin is 85 THB.
    chang_recipe = Recipe(
        item_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(
            Ingredient(
                sku_id="chang-keg",
                name="Chang draught beer",
                unit="ml",
                quantity=Decimal("500"),
                purchase_price=Decimal("0.07"),
            ),
        ),
    )
    sale = Sale(
        item_id="chang-draft-500",
        timestamp=date(2026, 6, 24),
        sell_price=Decimal("120"),
    )
    return SeededSource(sales=[sale], recipes=[chang_recipe])


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
    for im in result.item_margins:
        print(
            f"  - [{im.segment.value}] {im.name}: "
            f"{im.units_sold}u  revenue={_money(im.revenue)}  "
            f"cogs={_money(im.cogs)}  margin={_money(im.gross_margin)} THB"
        )


if __name__ == "__main__":
    main()
