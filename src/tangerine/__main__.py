"""CLI entrypoint: print the daily 9am review against seeded data.

    python -m tangerine

This exists so a human can see the pipeline produce a number end-to-end without
writing a test. Real sources (Loyverse, receipts) plug in here later.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .cost import CostBook
from .daily_review import DailyReview, build_daily_review
from .seeded import SeededSource
from .types import Recipe, RecipeIngredient, Sale, Segment


def _seeded_source() -> SeededSource:
    # One bar sale (Chang draft) and one cafe sale (espresso latte) per day for
    # the last 7 days, so the rolling-average goal has data to work with.
    #   Chang:  500 ml beer @ 0.07 THB/ml -> 35 cost, 120 sell -> 85 bar CM
    #   Latte:  20 g beans @ 2 THB/g + 200 ml milk @ 0.025 THB/ml -> 45 cost,
    #           120 sell -> 75 cafe CM  (daily GM = 160)
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
    sales: list[Sale] = []
    for i in range(7):
        d = day - timedelta(days=i)
        sales.append(Sale(item_id="chang-draft-500", timestamp=d, sell_price=Decimal("120")))
        sales.append(Sale(item_id="espresso-latte", timestamp=d, sell_price=Decimal("120")))
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


def _print_review(review: DailyReview) -> None:
    print(f"Daily 9am review for {review.day}:")
    print(
        f"  revenue:       {_money(review.revenue)} THB   "
        f"COGS: {_money(review.cogs)} THB   "
        f"gross margin: {_money(review.gross_margin)} THB"
    )
    print("  segment contribution margin:")
    for sm in review.segment_margins:
        flag = "  [RED]" if sm.is_red else ""
        print(
            f"    [{sm.segment.value}] CM={_money(sm.contribution_margin)} THB"
            f"  (revenue={_money(sm.revenue)}, variable_costs={_money(sm.variable_costs)}){flag}"
        )
    print(f"  top by margin:   {[(im.name, _money(im.gross_margin)) for im in review.top_by_margin.items]}")
    print(f"  bottom by margin:{[(im.name, _money(im.gross_margin)) for im in review.bottom_by_margin.items]}")
    print(f"  top by volume:   {[(im.name, im.units_sold) for im in review.top_by_volume.items]}")
    print(f"  bottom by volume:{[(im.name, im.units_sold) for im in review.bottom_by_volume.items]}")
    if review.below_target_items:
        print(f"  below target:    {[im.name for im in review.below_target_items]}")
    if review.unmapped_items:
        print(f"  unmapped:        {[im.item_id for im in review.unmapped_items]}")
    if review.anomaly_flags:
        print("  anomaly flags:")
        for f in review.anomaly_flags:
            print(f"    [{f.kind.value}] {f.detail}")
    goal = review.goal
    progress = "MET" if goal.met else "MISSING"
    print(
        f"  goal: {progress}  7-day avg {_money(goal.rolling_average)} THB/day "
        f"vs target {_money(goal.target)} (surplus {_money(goal.surplus)} THB; "
        f"{goal.days_in_window} days)"
    )


def main() -> None:
    source = _seeded_source()
    day = source.sales()[0].timestamp
    review = build_daily_review(source=source, review_date=day)
    _print_review(review)


if __name__ == "__main__":
    main()
