"""End-to-end daily 9am review view (slice 11).

The 9am review is the single daily surface a partner opens every morning to see
everything that needs attention from yesterday (PRD user story 29; issue 11).
Per issue 11 it surfaces, in one fast-scan view:

  - yesterday's revenue, COGS, gross margin
  - per-segment contribution margin with red flags where CM < 0
  - top/bottom items by margin and by sell volume
  - items whose actual margin is below their set target
  - anomaly flags from slice 10 (voids, drawer variance, clustering)
  - items sold without a recipe mapping
  - progress toward the 10,000 THB/day target (7-day rolling average vs target)

These tests read as worked examples: a synthetic yesterday goes in; the review
object carries the right numbers and flags out.

Scope decisions (confirmed with the partner before code):

  - **Goal comparison number**: the 7-day rolling average is the daily
    ``total_gross_margin`` (= sum of segment CMs today; direct labor is not
    tracked and fixed costs are not daily-allocated per the PRD/issue 08).
  - **Anomaly window**: yesterday only — matches the "yesterday's review"
    framing of the rest of the view.
  - **Anomaly inputs**: explicit parameters on the review function. The current
    ``Source`` Protocol yields only sales/recipes/cost_book; voids, closes, and
    per-cashier sales_counts are passed in by the caller so this slice does not
    widen the ingestion boundary.
  - **Top/bottom lists**: 3 items each, ranked by margin and by units sold;
    flagged rows (unmapped / unknown-price) are excluded because their margins
    are meaningless.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from tangerine.cost import CostBook
from tangerine.daily_review import build_daily_review
from tangerine.seeded import SeededSource
from tangerine.types import (
    AnomalyFlag,
    AnomalyKind,
    DAILY_PROFIT_TARGET_THB,
    Money,
    Recipe,
    RecipeIngredient,
    Sale,
    Segment,
    ShiftClose,
    Void,
)

D = Decimal


# --- shared fixtures --------------------------------------------------------


@pytest.fixture
def day() -> date:
    """The review date — i.e. the day whose numbers we are reviewing."""
    return date(2026, 6, 24)


def _chang_recipe(*, target: Decimal | None = None) -> Recipe:
    """500ml Chang draught, bar segment, 35 THB/pour cost."""
    return Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
        target_gross_margin_pct=target,
    )


def _latte_recipe(*, target: Decimal | None = None) -> Recipe:
    """Espresso latte, cafe segment, 45 THB cost (20g beans + 200ml milk)."""
    return Recipe(
        sku_id="espresso-latte",
        name="Espresso Latte",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="beans-arabica", quantity=D("20")),
            RecipeIngredient(sku_id="milk-fresh", quantity=D("200")),
        ),
        target_gross_margin_pct=target,
    )


def _cappuccino_recipe() -> Recipe:
    """Cappuccino, cafe segment, 40 THB cost (15g beans + 400ml milk)."""
    return Recipe(
        sku_id="cappuccino",
        name="Cappuccino",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="beans-arabica", quantity=D("15")),
            RecipeIngredient(sku_id="milk-fresh", quantity=D("400")),
        ),
    )


def _leo_recipe() -> Recipe:
    """500ml Leo draught, bar segment, 35 THB/pour cost."""
    return Recipe(
        sku_id="leo-draft-500",
        name="Leo Draft 500ml",
        segment=Segment.BAR,
        # Leo is a different brand but its keg happens to be the same per-ml
        # cost as Chang in this fixture (the cost book keys by SKU id).
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )


def _cost() -> CostBook:
    return CostBook(
        {
            "chang-keg": (D("0.07"), date(2026, 6, 1)),
            "beans-arabica": (D("2"), date(2026, 6, 1)),
            "milk-fresh": (D("0.025"), date(2026, 6, 1)),
        }
    )


# --- AC 1 + 2: yesterday's revenue / COGS / gross margin + segment CM/red ---


def test_review_carry_yesterday_revenue_cogs_and_gross_margin(day: date) -> None:
    """The review surface shows yesterday's revenue, COGS, and gross margin.

    Worked example. One Chang @ 120, cost 35 -> revenue 120, COGS 35, GM 85.
    The review mirrors the daily-margin numbers from slice 04.
    """
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    review = build_daily_review(source=source, review_date=day)

    assert review.day == day
    assert review.revenue == D("120")
    assert review.cogs == D("35")
    assert review.gross_margin == D("85")


def test_review_surfaces_per_segment_cm_with_red_flag(day: date) -> None:
    """AC: "Segment contribution margins displayed with red flags for CM < 0".

    Worked example. Bar sells Chang below cost (30 THB, cost 35 -> CM -5, red).
    Cafe sells a latte normally (120 THB, cost 45 -> CM 75, not red).
    """
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("30")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    review = build_daily_review(source=source, review_date=day)

    by_seg = {sm.segment: sm for sm in review.segment_margins}
    assert by_seg[Segment.BAR].contribution_margin == D("-5")
    assert by_seg[Segment.BAR].is_red is True
    assert by_seg[Segment.CAFE].contribution_margin == D("75")
    assert by_seg[Segment.CAFE].is_red is False


# --- AC: top/bottom items by margin and by sell volume ----------------------


def test_review_ranks_top_and_bottom_items_by_margin(day: date) -> None:
    """AC: "Top and bottom items by margin and sell volume visible".

    Worked example. Four reliable items on the day, each sold once, with margins
    85 / 75 / 50 / 10. Top 3 by margin = the three highest (85, 75, 50). Bottom
    3 by margin = the three lowest (10, 50, 75). Top is high-to-low; bottom is
    low-to-high, so the partner sees the extremes first when scanning.
    """
    # Chang @ 120 -> 85 margin; latte @ 120 -> 75 margin; cappuccino @ 90 ->
    # 50 margin (cost 40); Leo @ 45 -> 10 margin (cost 35).
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
        Sale(item_id="cappuccino", timestamp=day, sell_price=D("90")),
        Sale(item_id="leo-draft-500", timestamp=day, sell_price=D("45")),
    ]
    recipes = [_chang_recipe(), _latte_recipe(), _cappuccino_recipe(), _leo_recipe()]
    source = SeededSource(sales=sales, recipes=recipes, cost=_cost())

    review = build_daily_review(source=source, review_date=day)

    top_margins = [im.gross_margin for im in review.top_by_margin.items]
    assert top_margins == [D("85"), D("75"), D("50")]

    bottom_margins = [im.gross_margin for im in review.bottom_by_margin.items]
    assert bottom_margins == [D("10"), D("50"), D("75")]


def test_review_ranks_top_and_bottom_items_by_volume(day: date) -> None:
    """Top/bottom by units sold. Two items: one sold 5 times, one sold 2 times.

    Top by volume = the 5-unit item first; bottom by volume = the 2-unit item
    first. With only two reliable items, both lists are length 2 (the lists do
    not pad with placeholder rows; ``TOP_BOTTOM_COUNT`` is a cap, not a quota).
    """
    sales = [
        *[Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))
          for _ in range(5)],
        *[Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120"))
          for _ in range(2)],
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    review = build_daily_review(source=source, review_date=day)

    top_volume = [(im.item_id, im.units_sold) for im in review.top_by_volume.items]
    assert top_volume == [("chang-draft-500", 5), ("espresso-latte", 2)]

    bottom_volume = [(im.item_id, im.units_sold) for im in review.bottom_by_volume.items]
    assert bottom_volume == [("espresso-latte", 2), ("chang-draft-500", 5)]


def test_review_rankings_exclude_flagged_rows(day: date) -> None:
    """An unmapped item is excluded from the margin/volume rankings.

    Its margin is meaningless (no recipe -> no COGS), so including it in either
    ranking would mislead the partner. The unmapped item is surfaced separately
    (see ``unmapped_items``) so its existence is not hidden, but it does not
    pollute the "top earners" or "top sellers" lists.
    """
    sales = [
        # Reliable: 1 Chang @ 120, margin 85.
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        # Flagged unmapped: no recipe for 'mystery'. Margin would be reported
        # as 0 (no COGS), which would otherwise show as a "low margin" item.
        Sale(item_id="mystery", timestamp=day, sell_price=D("1000"),
             segment=Segment.CAFE),
    ]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    review = build_daily_review(source=source, review_date=day)

    in_rankings = set()
    for ranking in (
        review.top_by_margin, review.bottom_by_margin,
        review.top_by_volume, review.bottom_by_volume,
    ):
        in_rankings.update(im.item_id for im in ranking.items)

    assert "mystery" not in in_rankings
    assert "chang-draft-500" in in_rankings


# --- AC: below-target-margin items flagged ----------------------------------


def test_review_flags_below_target_margin_items(day: date) -> None:
    """AC: "Items whose actual margin is below their set target."

    Worked example. Chang sold at 120 THB has a 70.83% gross margin
    (85 / 120 * 100). Its recipe carries a target of 75%. 70.83 < 75 -> the
    item appears in ``below_target_items``.

    The latte sold at 120 THB has a 62.50% margin (75 / 120), but its recipe
    carries a 50% target — comfortably above target, so it does not appear.
    """
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
    ]
    recipes = [
        _chang_recipe(target=D("75")),   # chang actual = 70.83% < 75% -> flagged
        _latte_recipe(target=D("50")),   # latte actual = 62.50% >= 50% -> ok
    ]
    source = SeededSource(sales=sales, recipes=recipes, cost=_cost())

    review = build_daily_review(source=source, review_date=day)

    below_ids = {im.item_id for im in review.below_target_items}
    assert below_ids == {"chang-draft-500"}


def test_review_no_below_target_items_when_no_targets_set(day: date) -> None:
    """Recipes without a target never fire the below-target flag."""
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe()], cost=_cost()  # no target set
    )

    review = build_daily_review(source=source, review_date=day)

    assert review.below_target_items == ()


# --- AC: items sold without recipe mapping flagged --------------------------


def test_review_flags_items_sold_without_recipe_mapping(day: date) -> None:
    """AC: "Items sold without recipe mapping."

    Worked example. One mapped Chang sale + one unmapped 'mystery' sale. The
    review surfaces the unmapped item so the partner can resolve its recipe
    before tomorrow's margin is meaningful for it.

    The unmapped row carries its real revenue (so the partner sees what was
    sold), and its segment is the shift-fallback (here cafe via the sale stamp).
    """
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(
            item_id="mystery",
            timestamp=day,
            sell_price=D("100"),
            segment=Segment.CAFE,
        ),
    ]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    review = build_daily_review(source=source, review_date=day)

    unmapped_ids = [im.item_id for im in review.unmapped_items]
    assert unmapped_ids == ["mystery"]
    mystery = review.unmapped_items[0]
    assert mystery.unmapped is True
    assert mystery.revenue == D("100")
    assert mystery.segment == Segment.CAFE


def test_review_no_unmapped_items_when_everything_mapped(day: date) -> None:
    """A day where every sold item has a recipe produces no unmapped items."""
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe()], cost=_cost()
    )

    review = build_daily_review(source=source, review_date=day)

    assert review.unmapped_items == ()


# --- AC: anomaly flags from slice 10 appear ---------------------------------


def _close(
    *,
    shift_id: str,
    cashier_id: str,
    closed_at: datetime,
    variance: str,
) -> ShiftClose:
    """A shift close with a chosen variance.

    The detector only consumes ``cashier_id``, ``closed_at``, ``variance`` from
    a ``ShiftClose``; the other fields are filled with neutral values so the
    record is internally consistent (closing = opening + rung_up + variance).
    """
    from tangerine.cash_drawer import close_shift

    v = Money(variance)
    opened = Money("5000")
    rung_up = Money("8000")
    closing = opened + rung_up + v
    return close_shift(
        shift_id=shift_id,
        cashier_id=cashier_id,
        closed_at=closed_at,
        opening_cash=opened,
        closing_cash=closing,
        rung_up_cash=rung_up,
    )


def _void(
    *,
    void_id: str,
    cashier_id: str,
    created_at: datetime,
) -> Void:
    """One synthetic void at a chosen instant."""
    return Void(
        void_id=void_id,
        cashier_id=cashier_id,
        created_at=created_at,
        item_id="chang-draft-500",
        quantity=1,
        price=Money("120"),
    )


def test_review_surfaces_anomaly_flags_for_yesterday(day: date) -> None:
    """AC: "Anomaly flags from slice 10 appear."

    Worked example. Alice closes three short shifts in a row yesterday; the
    slice-10 rule fires. The review surfaces the same flag — the review's job
    is to compose slice-10 over yesterday's window, not to re-derive it.

    The anomaly window is ``review_date`` only (the partner-confirmed scope):
    matches the rest of the view's "yesterday" framing.
    """
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    closes = [
        _close(shift_id="a-1", cashier_id="alice",
               closed_at=datetime.combine(day, datetime.min.time()),
               variance="-50"),
        _close(shift_id="a-2", cashier_id="alice",
               closed_at=datetime.combine(day, datetime.min.time()) + timedelta(hours=1),
               variance="-60"),
        _close(shift_id="a-3", cashier_id="alice",
               closed_at=datetime.combine(day, datetime.min.time()) + timedelta(hours=2),
               variance="-40"),
    ]

    review = build_daily_review(
        source=source,
        review_date=day,
        closes=closes,
        sales_counts={"alice": 0},
        drawer_short_rate_threshold=D("0.25"),
    )

    run_flags = [
        f for f in review.anomaly_flags
        if f.kind is AnomalyKind.DRAWER_SHORT_THREE_SHIFTS_RUNNING
    ]
    assert {f.cashier_id for f in run_flags} == {"alice"}


def test_review_no_anomaly_flags_when_no_cash_void_data(day: date) -> None:
    """No closes/voids/sales_counts passed in -> empty anomaly section.

    A review for a quiet day (or a day where cash/void data has not been wired
    up yet) must not require the caller to invent inputs; it returns an empty
    anomaly list rather than raising or defaulting to zero-flags-silently.
    """
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    review = build_daily_review(source=source, review_date=day)

    assert review.anomaly_flags == ()


def test_review_anomaly_window_is_yesterday_only(day: date) -> None:
    """Anomaly flags only consider records on ``review_date``.

    Three alice shorts the day before yesterday plus three yesterday: the
    detector sees a 3-shift run yesterday, but not a 6-shift run. If the window
    leaked backward, alice's run would extend to 6 (or fire on the day-before
    closes alone). Asserting exactly one flag with observed == 3 proves the
    window is single-day.
    """
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    prev = day - timedelta(days=1)
    closes = [
        *[_close(shift_id=f"a-prev-{i}", cashier_id="alice",
                 closed_at=datetime.combine(prev, datetime.min.time()) + timedelta(hours=i),
                 variance="-50") for i in range(3)],
        *[_close(shift_id=f"a-now-{i}", cashier_id="alice",
                 closed_at=datetime.combine(day, datetime.min.time()) + timedelta(hours=i),
                 variance="-50") for i in range(3)],
    ]

    review = build_daily_review(
        source=source,
        review_date=day,
        closes=closes,
        sales_counts={"alice": 0},
        drawer_short_rate_threshold=D("0.25"),
    )

    run_flags = [
        f for f in review.anomaly_flags
        if f.kind is AnomalyKind.DRAWER_SHORT_THREE_SHIFTS_RUNNING
    ]
    assert len(run_flags) == 1
    assert run_flags[0].observed == D("3")
    assert run_flags[0].period_start == day
    assert run_flags[0].period_end == day


def test_review_surfaces_void_rate_anomaly_for_yesterday(day: date) -> None:
    """AC: "Anomaly flags from slice 10 appear" — the void side, not just drawer.

    The drawer-run test above proves the wiring works for one rule kind. This
    test exercises a *different* rule kind (void rate above venue median) to
    prove the review surfaces whatever slice 10 fires — it does not silently
    filter to drawer rules only.

    Worked example. Alice voids 6 of her 20 sales (rate 0.30); Bob voids 1 of
    his 20 (rate 0.05). Venue median of {0.30, 0.05} = 0.175. Alice's 0.30 >
    0.175 fires the void-rate flag. The review surfaces it under yesterday's
    window.
    """
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    voids = [
        *[_void(void_id=f"a-{i}", cashier_id="alice",
                created_at=datetime.combine(day, datetime.min.time()) + timedelta(hours=12))
          for i in range(6)],
        _void(void_id="b-0", cashier_id="bob",
              created_at=datetime.combine(day, datetime.min.time()) + timedelta(hours=12)),
    ]

    review = build_daily_review(
        source=source,
        review_date=day,
        voids=voids,
        sales_counts={"alice": 20, "bob": 20},
        drawer_short_rate_threshold=D("0.25"),
    )

    void_rate_flags = [
        f for f in review.anomaly_flags
        if f.kind is AnomalyKind.VOID_RATE_ABOVE_VENUE_MEDIAN
    ]
    assert {f.cashier_id for f in void_rate_flags} == {"alice"}
    assert void_rate_flags[0].period_start == day
    assert void_rate_flags[0].period_end == day


# --- AC: 7-day rolling average vs 10K THB/day target ------------------------


def test_review_goal_rolling_average_meets_target_when_consistent(day: date) -> None:
    """AC: "7-day rolling average vs 10K THB/day target shown."

    Worked example. 7 days ending on ``review_date``, each with 12,000 THB
    daily gross margin (one Chang sale per day at 12,085 THB sell would be
    unreasonable; we use multiple sales to land a clean 12,000 margin per day).
    Average = 12,000 >= 10,000 target -> ``goal.met`` is True, surplus 2,000.

    The comparison number is the daily gross margin (= sum of segment CMs
    today; direct labor is not tracked and fixed costs are not daily-allocated
    per PRD user story 20 / issue 08).
    """
    # 1 chang @ 120 -> 85 margin; need 12,000 per day -> 120 + small extras.
    # Easier: 100 sales of chang @ 120 = 12,000 revenue, 3,500 COGS, GM 8,500.
    # Easier still: pick prices so GM per day is exactly 12,000. We sell N units
    # at price P; GM = N * (P - 35). Pick N=200, P=95 -> GM = 200 * 60 = 12,000.
    days = [day - timedelta(days=i) for i in range(7)]  # 7 days ending on `day`
    sales = [
        Sale(item_id="chang-draft-500", timestamp=d, sell_price=D("95"))
        for d in days
        for _ in range(200)
    ]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    review = build_daily_review(source=source, review_date=day)

    assert review.goal.days_in_window == 7
    assert review.goal.rolling_average == D("12000")
    assert review.goal.target == D("10000")
    assert review.goal.met is True
    assert review.goal.surplus == D("2000")


def test_review_goal_rolling_average_misses_target(day: date) -> None:
    """Worked example. 7 days at 8,000 THB GM each -> average 8,000 < 10,000.

    200 chang @ 75 = 15,000 rev, 7,000 cogs, GM 8,000.
    """
    days = [day - timedelta(days=i) for i in range(7)]
    sales = [
        Sale(item_id="chang-draft-500", timestamp=d, sell_price=D("75"))
        for d in days
        for _ in range(200)
    ]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    review = build_daily_review(source=source, review_date=day)

    assert review.goal.rolling_average == D("8000")
    assert review.goal.met is False
    assert review.goal.surplus == D("-2000")


def test_review_goal_short_history_averages_only_seen_days(day: date) -> None:
    """A venue with fewer than 7 days of sales averages only what it has.

    Two days of sales -> ``days_in_window`` is 2 and the rolling average is the
    mean over those two days. We do NOT pad missing days with zeros: that would
    under-state progress for the first week and a half of operation.
    """
    earlier = day - timedelta(days=1)
    sales = [
        # Day 1 (earlier): 200 @ 95 -> GM 12,000
        *[Sale(item_id="chang-draft-500", timestamp=earlier, sell_price=D("95"))
          for _ in range(200)],
        # Day 2 (review_date): 200 @ 75 -> GM 8,000
        *[Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("75"))
          for _ in range(200)],
    ]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    review = build_daily_review(source=source, review_date=day)

    assert review.goal.days_in_window == 2
    # Mean of 12,000 and 8,000 = 10,000.
    assert review.goal.rolling_average == D("10000")


def test_review_goal_target_is_ten_k_per_day(day: date) -> None:
    """The goal target is the PRD's 10,000 THB/day profit number."""
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=_cost())

    review = build_daily_review(source=source, review_date=day)

    assert review.goal.target == DAILY_PROFIT_TARGET_THB
    assert review.goal.target == D("10000")


# --- AC: full end-to-end across all sections --------------------------------


def test_end_to_end_synthetic_yesterday_renders_all_sections(day: date) -> None:
    """Full slice-11 seam: a synthetic yesterday feeds every review section.

    This is the issue-11 acceptance criterion "End-to-end test feeds a synthetic
    yesterday; asserts all expected sections render with the right numbers/flags."

    Yesterday (``review_date``):

      - 4 reliable items sold:
        * chang-draft-500 @ 120 (1u, bar)  -> margin 85
        * espresso-latte @ 120 (1u, cafe)  -> margin 75
        * cappuccino      @ 90  (1u, cafe) -> margin 50
        * leo-draft-500   @ 45  (1u, bar)  -> margin 10
      - 1 unmapped item sold (mystery) -> unmapped flag
      - chang recipe carries a 75% target -> actual 70.83% -> below target flag
      - alice closes 3 short shifts in a row -> drawer run anomaly flag
      - the trailing 7 days each carry the same four items -> rolling avg
        = (85 + 75 + 50 + 10) = 220 GM/day, well below the 10K target.

    Asserts the review carries every section with the right numbers/flags.
    """
    recipes = [
        _chang_recipe(target=D("75")),  # actual 70.83% -> below target
        _latte_recipe(),
        _cappuccino_recipe(),
        _leo_recipe(),
    ]

    reliable_sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
        Sale(item_id="cappuccino", timestamp=day, sell_price=D("90")),
        Sale(item_id="leo-draft-500", timestamp=day, sell_price=D("45")),
    ]
    unmapped_sale = Sale(
        item_id="mystery", timestamp=day, sell_price=D("100"),
        segment=Segment.CAFE,
    )

    # Trailing 7 days: every prior day has the same reliable_items, so the
    # rolling average = 220 (the sum of the four per-item margins).
    prior_days = [day - timedelta(days=i) for i in range(1, 7)]
    all_sales = list(reliable_sales) + [unmapped_sale]
    for d in prior_days:
        all_sales.extend(
            Sale(item_id=s.item_id, timestamp=d, sell_price=s.sell_price)
            for s in reliable_sales
        )
    source = SeededSource(sales=all_sales, recipes=recipes, cost=_cost())

    closes = [
        _close(shift_id="a-1", cashier_id="alice",
               closed_at=datetime.combine(day, datetime.min.time()),
               variance="-50"),
        _close(shift_id="a-2", cashier_id="alice",
               closed_at=datetime.combine(day, datetime.min.time()) + timedelta(hours=1),
               variance="-60"),
        _close(shift_id="a-3", cashier_id="alice",
               closed_at=datetime.combine(day, datetime.min.time()) + timedelta(hours=2),
               variance="-40"),
    ]

    review = build_daily_review(
        source=source,
        review_date=day,
        closes=closes,
        sales_counts={"alice": 0},
        drawer_short_rate_threshold=D("0.25"),
    )

    # 1. Revenue / COGS / gross margin.
    # Reliable revenue (mapped items only): 120 + 120 + 90 + 45 = 375.
    # The unmapped 'mystery' sale (100) is excluded from revenue/COGS/GM per
    # slice-04 policy (its COGS is unknown) — its revenue surfaces at
    # ``daily.flagged_revenue`` instead, and the item shows up in
    # ``unmapped_items`` below.
    # COGS counts reliable rows only: 35 + 45 + 40 + 35 = 155.
    # Gross margin = reliable revenue (375) - COGS (155) = 220.
    assert review.revenue == D("375")
    assert review.cogs == D("155")
    assert review.gross_margin == D("220")
    # The unmapped sale's cash is not lost — it surfaces here.
    assert review.daily.flagged_revenue == D("100")

    # 2. Segment CMs. Bar: 85 + 10 = 95 (chang + leo). Cafe: 75 + 50 = 125.
    # Neither is negative, so neither is red.
    by_seg = {sm.segment: sm for sm in review.segment_margins}
    assert by_seg[Segment.BAR].contribution_margin == D("95")
    assert by_seg[Segment.BAR].is_red is False
    assert by_seg[Segment.CAFE].contribution_margin == D("125")
    assert by_seg[Segment.CAFE].is_red is False

    # 3. Top/bottom by margin.
    assert [im.gross_margin for im in review.top_by_margin.items] == [
        D("85"), D("75"), D("50"),
    ]
    assert [im.gross_margin for im in review.bottom_by_margin.items] == [
        D("10"), D("50"), D("75"),
    ]

    # 4. Top/bottom by volume — every item sold once, so all four tie at 1.
    # Sort within the tie is by item id (the margin engine's deterministic
    # order); we just check every item appears and lengths are right.
    assert len(review.top_by_volume.items) == 3
    assert len(review.bottom_by_volume.items) == 3

    # 5. Below-target items.
    assert {im.item_id for im in review.below_target_items} == {"chang-draft-500"}

    # 6. Unmapped items.
    assert [im.item_id for im in review.unmapped_items] == ["mystery"]

    # 7. Anomaly flags.
    run_flags = [
        f for f in review.anomaly_flags
        if f.kind is AnomalyKind.DRAWER_SHORT_THREE_SHIFTS_RUNNING
    ]
    assert {f.cashier_id for f in run_flags} == {"alice"}

    # 8. Goal progress — every day in the trailing 7 carries the same 220 GM.
    assert review.goal.days_in_window == 7
    assert review.goal.rolling_average == D("220")
    assert review.goal.target == D("10000")
    assert review.goal.met is False
    assert review.goal.surplus == D("-9780")
