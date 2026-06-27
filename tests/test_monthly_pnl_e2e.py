"""End-to-end fixed costs + monthly accrual P&L test seam (slice 08).

Per the PRD testing rules these tests read as worked examples: "given a month
with 240 THB of bar revenue, 5 L of beer consumed (accrual COGS 350 THB), 120
THB of cafe revenue against 45 THB of consumed cafe stock, and 30,000 THB of
rent, the entity net profit is X and the 10K THB/day target is met/missed by Y."

They feed synthetic inventory (keg weigh-ins + cafe stock counts), approved
purchases, and fixed-cost entries through a single monthly engine and assert:

  - per-segment accrual COGS (beginning inventory + purchases − ending inventory)
  - per-segment accrual contribution margin (revenue − accrual COGS)
  - entity net profit (sum of segment CM − fixed costs)
  - the 10K THB/day × days-in-month goal comparison
  - a separate cash-flow view (payables by invoice date)

Scope (issue 08):
  - Fixed costs are entity-level only; never allocated to a segment (PRD US 20).
  - Monthly CM uses accrual COGS (PRD US 22), distinct from the daily
    recipe-based CM (slice 04/07).
  - Cash-basis payables (PRD US 24) are tracked by invoice date, separately.
  - The bar's accrual COGS comes from slice 05 (keg weigh-ins); the cafe's from
    slice 06 (cafe stock counts). The monthly engine calls both internally.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tangerine.cost import CostBook
from tangerine.monthly_pnl import compute_monthly_pnl
from tangerine.recipes import RecipeCatalog
from tangerine.types import (
    CafeCountCadence,
    CafeItem,
    CafeStockCount,
    DAILY_PROFIT_TARGET_THB,
    FixedCost,
    FixedCostCategory,
    KegBrand,
    KegWeighIn,
    Purchase,
    PurchaseLine,
    Recipe,
    RecipeIngredient,
    Sale,
    Segment,
)

D = Decimal


# --- shared fixtures --------------------------------------------------------
#
# A single coherent month is used across most tests: June 2026 (30 days).
# Both segments have inventory + sales so the worked examples exercise the
# full accrual P&L. Numbers are chosen so the arithmetic is easy to follow.


@pytest.fixture
def month() -> tuple[int, int]:
    return (2026, 6)


@pytest.fixture
def month_start(month: tuple[int, int]) -> date:
    return date(month[0], month[1], 1)


@pytest.fixture
def month_end(month: tuple[int, int]) -> date:
    # Last calendar day of the month. June has 30 days.
    return date(month[0], month[1], 30)


@pytest.fixture
def chang_brand() -> KegBrand:
    """A 20L Chang keg: tare 5000g, water density."""
    return KegBrand(
        brand_id="chang",
        name="Chang Draught",
        beer_sku_id="chang-keg",
        tare_weight_g=D("5000"),
    )


@pytest.fixture
def chang_recipe() -> Recipe:
    """500ml Chang pour, bar segment."""
    return Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )


@pytest.fixture
def latte_recipe() -> Recipe:
    """Espresso latte, cafe segment."""
    return Recipe(
        sku_id="espresso-latte",
        name="Espresso Latte",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="beans-arabica", quantity=D("20")),
            RecipeIngredient(sku_id="milk-fresh", quantity=D("200")),
        ),
    )


@pytest.fixture
def milk_item() -> CafeItem:
    return CafeItem(
        sku_id="milk-fresh",
        name="Fresh milk",
        unit="ml",
        cadence=CafeCountCadence.DAILY,
    )


@pytest.fixture
def cost() -> CostBook:
    """The per-unit prices the accrual engines price consumption at."""
    return CostBook(
        {
            "chang-keg": (D("0.07"), date(2026, 6, 1)),
            "beans-arabica": (D("2"), date(2026, 6, 1)),
            "milk-fresh": (D("0.025"), date(2026, 6, 1)),
        }
    )


def _purchase_line(
    sku_id: str, description: str, quantity: Decimal, unit_price: Decimal
) -> PurchaseLine:
    return PurchaseLine(
        sku_id=sku_id,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
    )


# --- AC 1: fixed cost entries exist (amount, category, period) --------------


def test_fixed_cost_entry_carries_amount_category_period() -> None:
    """A fixed cost entry records how much, what kind, and which month.

    Issue 08 AC: 'Fixed cost entries exist: amount, category, period.' These
    are entity-level (never segment) monthly recurring costs. The category is
    a closed enum with an OTHER catch-all (the issue lists 'rent, utilities,
    shared staff salaries, insurance, etc.').
    """
    rent = FixedCost(
        amount=D("30000"),
        category=FixedCostCategory.RENT,
        period=(2026, 6),
    )
    salaries = FixedCost(
        amount=D("45000"),
        category=FixedCostCategory.STAFF_SALARIES,
        period=(2026, 6),
    )
    other = FixedCost(
        amount=D("1200"),
        category=FixedCostCategory.OTHER,
        period=(2026, 6),
    )

    assert rent.amount == D("30000")
    assert rent.category == FixedCostCategory.RENT
    assert rent.period == (2026, 6)

    assert salaries.category == FixedCostCategory.STAFF_SALARIES
    assert other.category == FixedCostCategory.OTHER

    # The known categories round-trip through their string form so a
    # persistence/config layer can read/write them as plain strings.
    for cat in (
        FixedCostCategory.RENT,
        FixedCostCategory.UTILITIES,
        FixedCostCategory.STAFF_SALARIES,
        FixedCostCategory.INSURANCE,
        FixedCostCategory.OTHER,
    ):
        assert FixedCostCategory(cat.value) is cat


# --- AC 2: monthly view computes accrual COGS (bar via keg weigh-ins, 05) ----


def test_monthly_bar_accrual_cogs_from_keg_weigh_ins(
    month: tuple[int, int],
    month_start: date,
    month_end: date,
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost: CostBook,
) -> None:
    """The bar segment's monthly accrual COGS comes from keg weigh-ins (slice 05).

    Worked example: a 20L Chang keg weighed 25000g on Jun 1 (net 20000g ->
    20000ml) and 20000g on Jun 30 (net 15000g -> 15000ml). Consumed volume is
    5000ml, priced at 0.07 THB/ml -> 350 THB of bar accrual COGS for June.

    Beginning inventory value + purchases − ending inventory value is the keg
    primitive in volume terms (beginning_volume − ending_volume) × cost per ml;
    purchases here are zero so the consumed volume is pure inventory drawdown.
    """
    weigh_ins = [
        KegWeighIn(brand_id="chang", weighed_on=month_start, gross_weight_g=D("25000")),
        KegWeighIn(brand_id="chang", weighed_on=month_end, gross_weight_g=D("20000")),
    ]
    # One Chang sale in June so revenue is non-zero (asserted in a later test).
    sales = [
        Sale(item_id="chang-draft-500", timestamp=month_start, sell_price=D("120")),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=sales,
        recipes=RecipeCatalog([chang_recipe]),
        cost=cost,
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        cafe_items=[],
        cafe_beginning=[],
        cafe_ending=[],
        purchases=[],
        fixed_costs=[],
    )

    by_seg = {sp.segment: sp for sp in pnl.segment_pnl}
    bar = by_seg[Segment.BAR]
    assert bar.accrual_cogs == D("350")  # 5000ml consumed × 0.07 THB/ml


# --- AC 2 (cont): monthly cafe accrual COGS (cafe via stock counts, 06) -----


def test_monthly_cafe_accrual_cogs_from_stock_counts(
    month: tuple[int, int],
    month_start: date,
    month_end: date,
    milk_item: CafeItem,
    latte_recipe: Recipe,
    cost: CostBook,
) -> None:
    """The cafe segment's monthly accrual COGS comes from stock counts (slice 06).

    Worked example: fresh milk opening 5000ml (Jun 1), a 10000ml delivery on
    Jun 15 at 0.025 THB/ml, closing 3000ml (Jun 30). Consumed = 5000 + 10000
    − 3000 = 12000ml -> 300 THB of cafe accrual COGS for June. This is the
    ``beginning + purchases − ending`` accrual primitive the PRD calls for.
    """
    purchases = [
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 6, 15),
            lines=(
                _purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),
            ),
            vat=D("0"),
            total=D("250"),
        ),
    ]
    cafe_beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=month_start),
    ]
    cafe_ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("3000"), timestamp=month_end),
    ]
    # One cafe sale in June so the cafe segment exists (revenue asserted later).
    sales = [
        Sale(item_id="espresso-latte", timestamp=month_start, sell_price=D("120")),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=sales,
        recipes=RecipeCatalog([latte_recipe]),
        cost=cost,
        brands=[],
        weigh_ins=[],
        cafe_items=[milk_item],
        cafe_beginning=cafe_beginning,
        cafe_ending=cafe_ending,
        purchases=purchases,
        fixed_costs=[],
    )

    by_seg = {sp.segment: sp for sp in pnl.segment_pnl}
    cafe = by_seg[Segment.CAFE]
    assert cafe.accrual_cogs == D("300")  # 12000ml consumed × 0.025 THB/ml


# --- AC: monthly revenue per segment, recognised by sale timestamp ----------


def test_monthly_revenue_per_segment_recognised_by_timestamp(
    month: tuple[int, int],
    month_start: date,
    month_end: date,
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    latte_recipe: Recipe,
    cost: CostBook,
) -> None:
    """Monthly revenue is summed per segment from Loyverse sales in the month.

    Worked example over June 2026: 2x Chang (bar) @ 120 = 240 bar revenue;
    1x Latte (cafe) @ 120 = 120 cafe revenue. A sale dated May 31 and one
    dated July 1 are excluded (revenue is recognised by transaction timestamp
    per the PRD COGS-recognition note). Segment comes from the recipe (slice 07
    rule).
    """
    weigh_ins = [
        KegWeighIn(brand_id="chang", weighed_on=month_start, gross_weight_g=D("25000")),
        KegWeighIn(brand_id="chang", weighed_on=month_end, gross_weight_g=D("25000")),
    ]
    sales = [
        Sale(item_id="chang-draft-500", timestamp=date(2026, 6, 10), sell_price=D("120")),
        Sale(item_id="chang-draft-500", timestamp=date(2026, 6, 20), sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=date(2026, 6, 15), sell_price=D("120")),
        # Outside the month -> excluded from June revenue.
        Sale(item_id="chang-draft-500", timestamp=date(2026, 5, 31), sell_price=D("120")),
        Sale(item_id="chang-draft-500", timestamp=date(2026, 7, 1), sell_price=D("120")),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=sales,
        recipes=RecipeCatalog([chang_recipe, latte_recipe]),
        cost=cost,
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        cafe_items=[],
        cafe_beginning=[],
        cafe_ending=[],
        purchases=[],
        fixed_costs=[],
    )

    by_seg = {sp.segment: sp for sp in pnl.segment_pnl}
    assert by_seg[Segment.BAR].revenue == D("240")
    assert by_seg[Segment.CAFE].revenue == D("120")


def test_monthly_revenue_excludes_unmapped_sales(
    month: tuple[int, int],
    cost: CostBook,
) -> None:
    """An unmapped sale's revenue is excluded from monthly segment revenue.

    Its COGS is unknown (no recipe), so booking its revenue against accrual
    COGS would not be apples-to-apples. This mirrors the daily engine's
    reliable-rows-only convention (slice 04/07). Both segments still appear,
    carrying zero when nothing reliable was sold in them.
    """
    sales = [
        Sale(
            item_id="mystery",
            timestamp=date(2026, 6, 10),
            sell_price=D("90"),
            segment=Segment.CAFE,  # stamped via shift fallback, but unmapped
        ),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=sales,
        recipes=RecipeCatalog([]),
        cost=cost,
        brands=[],
        weigh_ins=[],
        cafe_items=[],
        cafe_beginning=[],
        cafe_ending=[],
        purchases=[],
        fixed_costs=[],
    )

    by_seg = {sp.segment: sp for sp in pnl.segment_pnl}
    assert by_seg[Segment.CAFE].revenue == D("0")
    assert by_seg[Segment.BAR].revenue == D("0")


def test_monthly_revenue_includes_mapped_sale_with_unpriced_ingredient(
    month: tuple[int, int],
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    month_start: date,
    month_end: date,
) -> None:
    """A mapped sale whose recipe ingredient has no price IS in monthly revenue.

    Unlike the daily recipe-margin engine (which excludes ``unknown_price``
    rows because their COGS is recipe-derived and unknown), the monthly view
    costs via accrual inventory consumption — so the sale's revenue is real
    and its cost is captured independently. The sale is therefore included
    in monthly segment revenue even though its recipe ingredient (``chang-keg``)
    has no approved price in the (empty) cost book.
    """
    weigh_ins = [
        KegWeighIn(brand_id="chang", weighed_on=month_start, gross_weight_g=D("25000")),
        KegWeighIn(brand_id="chang", weighed_on=month_end, gross_weight_g=D("25000")),
    ]
    empty_cost = CostBook({})  # chang-keg has no approved price
    sales = [
        Sale(item_id="chang-draft-500", timestamp=date(2026, 6, 10), sell_price=D("120")),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=sales,
        recipes=RecipeCatalog([chang_recipe]),
        cost=empty_cost,
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        cafe_items=[],
        cafe_beginning=[],
        cafe_ending=[],
        purchases=[],
        fixed_costs=[],
    )

    by_seg = {sp.segment: sp for sp in pnl.segment_pnl}
    assert by_seg[Segment.BAR].revenue == D("120")  # included, not excluded


# --- AC 3: monthly segment CM = revenue − accrual COGS, with red flag -------


def test_monthly_segment_cm_is_revenue_minus_accrual_cogs(
    month: tuple[int, int],
    month_start: date,
    month_end: date,
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    latte_recipe: Recipe,
    milk_item: CafeItem,
    cost: CostBook,
) -> None:
    """Monthly segment CM = revenue − accrual COGS (the accrual view).

    Worked example over June 2026:
      - Bar:  2x Chang @ 120 = 240 revenue; 5000ml beer consumed @ 0.07 = 350
              accrual COGS -> CM = -110 (red: bar sold below accrual cost).
      - Cafe: 1x Latte @ 120 = 120 revenue; 12000ml milk consumed @ 0.025 =
              300 accrual COGS -> CM = -180 (also red this month).

    Both segments appear; each CM is the difference of its own revenue and
    accrual COGS (no fixed-cost allocation here — fixed costs land at entity
    level). The ``is_red`` flag mirrors the slice-07 daily flag on the monthly
    accrual number.
    """
    weigh_ins = [
        KegWeighIn(brand_id="chang", weighed_on=month_start, gross_weight_g=D("25000")),
        KegWeighIn(brand_id="chang", weighed_on=month_end, gross_weight_g=D("20000")),
    ]
    cafe_beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=month_start),
    ]
    cafe_ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("3000"), timestamp=month_end),
    ]
    sales = [
        Sale(item_id="chang-draft-500", timestamp=date(2026, 6, 10), sell_price=D("120")),
        Sale(item_id="chang-draft-500", timestamp=date(2026, 6, 20), sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=date(2026, 6, 15), sell_price=D("120")),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=sales,
        recipes=RecipeCatalog([chang_recipe, latte_recipe]),
        cost=cost,
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        cafe_items=[milk_item],
        cafe_beginning=cafe_beginning,
        cafe_ending=cafe_ending,
        purchases=[],  # milk consumption = beginning − ending with no delivery
        fixed_costs=[],
    )

    by_seg = {sp.segment: sp for sp in pnl.segment_pnl}
    bar = by_seg[Segment.BAR]
    cafe = by_seg[Segment.CAFE]

    assert bar.revenue == D("240")
    assert bar.accrual_cogs == D("350")
    assert bar.contribution_margin == D("-110")
    assert bar.is_red is True

    assert cafe.revenue == D("120")
    assert cafe.accrual_cogs == D("50")  # 2000ml consumed × 0.025 (no purchase)
    assert cafe.contribution_margin == D("70")
    assert cafe.is_red is False

    # Segment order is the canonical cafe-then-bar (matches the daily view).
    assert [sp.segment for sp in pnl.segment_pnl] == [Segment.CAFE, Segment.BAR]


# --- AC: entity net profit = sum of segment CM − fixed costs -----------------


def test_entity_net_profit_is_segment_cm_sum_minus_fixed_costs(
    month: tuple[int, int],
    month_start: date,
    month_end: date,
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    latte_recipe: Recipe,
    cost: CostBook,
) -> None:
    """Entity net profit = sum of segment CM − entity-level fixed costs.

    Worked example over June 2026 (no inventory drawdown, so accrual COGS is
    zero and segment CM == segment revenue):
      - Bar revenue 240, cafe revenue 120 -> total CM 360.
      - Fixed costs: rent 30000 + staff salaries 45000 = 75000 for June.
      - Entity net profit = 360 − 75000 = -74640.

    Fixed costs are subtracted at entity level only — never allocated to a
    segment (PRD user story 20), so the segment CMs above are unaffected by
    them.
    """
    weigh_ins = [
        KegWeighIn(brand_id="chang", weighed_on=month_start, gross_weight_g=D("25000")),
        KegWeighIn(brand_id="chang", weighed_on=month_end, gross_weight_g=D("25000")),
    ]
    sales = [
        Sale(item_id="chang-draft-500", timestamp=date(2026, 6, 10), sell_price=D("120")),
        Sale(item_id="chang-draft-500", timestamp=date(2026, 6, 20), sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=date(2026, 6, 15), sell_price=D("120")),
    ]
    fixed_costs = [
        FixedCost(amount=D("30000"), category=FixedCostCategory.RENT, period=(2026, 6)),
        FixedCost(
            amount=D("45000"), category=FixedCostCategory.STAFF_SALARIES, period=(2026, 6)
        ),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=sales,
        recipes=RecipeCatalog([chang_recipe, latte_recipe]),
        cost=cost,
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        cafe_items=[],
        cafe_beginning=[],
        cafe_ending=[],
        purchases=[],
        fixed_costs=fixed_costs,
    )

    total_cm = sum((sp.contribution_margin for sp in pnl.segment_pnl), D("0"))
    assert total_cm == D("360")
    assert pnl.total_fixed_costs == D("75000")
    assert pnl.entity_net_profit == D("-74640")


def test_fixed_costs_from_other_months_excluded(
    month: tuple[int, int],
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost: CostBook,
    month_start: date,
    month_end: date,
) -> None:
    """Only fixed costs whose ``period`` matches the P&L month are recognised.

    A rent entry for July and one for May must NOT land on the June P&L, even
    though both are passed in. The month filter is on ``period`` equality, not
    on a date range, because fixed costs are monthly recurring (their period
    is a (year, month) the cost applies to).
    """
    weigh_ins = [
        KegWeighIn(brand_id="chang", weighed_on=month_start, gross_weight_g=D("25000")),
        KegWeighIn(brand_id="chang", weighed_on=month_end, gross_weight_g=D("25000")),
    ]
    fixed_costs = [
        FixedCost(amount=D("30000"), category=FixedCostCategory.RENT, period=(2026, 6)),
        FixedCost(amount=D("30000"), category=FixedCostCategory.RENT, period=(2026, 5)),
        FixedCost(amount=D("30000"), category=FixedCostCategory.RENT, period=(2026, 7)),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=[],
        recipes=RecipeCatalog([chang_recipe]),
        cost=cost,
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        cafe_items=[],
        cafe_beginning=[],
        cafe_ending=[],
        purchases=[],
        fixed_costs=fixed_costs,
    )

    # Only the June entry is recognised; May and July are dropped.
    assert len(pnl.fixed_costs) == 1
    assert pnl.total_fixed_costs == D("30000")


# --- AC 4: entity net profit compared to 10K THB/day × days in month --------


def test_goal_target_scales_with_days_in_month(
    month: tuple[int, int],
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost: CostBook,
    month_start: date,
    month_end: date,
) -> None:
    """The monthly target is 10,000 THB/day × days in the month.

    June 2026 has 30 days -> target = 300,000 THB. ``days_in_month`` is
    reported alongside so the comparison is auditable. ``met`` is False and
    ``surplus`` is negative when the venue falls short (the common case until
    the daily target is consistently hit).
    """
    weigh_ins = [
        KegWeighIn(brand_id="chang", weighed_on=month_start, gross_weight_g=D("25000")),
        KegWeighIn(brand_id="chang", weighed_on=month_end, gross_weight_g=D("25000")),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=[],
        recipes=RecipeCatalog([chang_recipe]),
        cost=cost,
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        cafe_items=[],
        cafe_beginning=[],
        cafe_ending=[],
        purchases=[],
        fixed_costs=[],
    )

    assert pnl.goal.days_in_month == 30
    assert pnl.goal.target == D("300000")  # 10000 × 30
    assert pnl.goal.actual == D("0")  # no sales, no fixed costs
    assert pnl.goal.met is False
    assert pnl.goal.surplus == D("-300000")


def test_goal_met_when_net_profit_at_or_above_target() -> None:
    """A month whose net profit hits the target marks ``met`` True.

    Worked example over February 2026 (28 days -> target 280,000): segment CM
    sums to 400,000, fixed costs 100,000 -> net profit 300,000 >= 280,000.
    ``surplus`` is +20,000.
    """
    feb = (2026, 2)
    sales = [
        Sale(item_id="chang-draft-500", timestamp=date(2026, 2, 10), sell_price=D("400000")),
    ]
    chang_recipe = Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 2, 1))})
    # Identical weighs -> zero bar accrual COGS, so bar CM == revenue.
    feb_start = date(2026, 2, 1)
    feb_end = date(2026, 2, 28)
    weigh_ins = [
        KegWeighIn(brand_id="chang", weighed_on=feb_start, gross_weight_g=D("25000")),
        KegWeighIn(brand_id="chang", weighed_on=feb_end, gross_weight_g=D("25000")),
    ]
    brand = KegBrand(
        brand_id="chang", name="Chang", beer_sku_id="chang-keg", tare_weight_g=D("5000")
    )
    fixed_costs = [
        FixedCost(amount=D("100000"), category=FixedCostCategory.RENT, period=feb),
    ]

    pnl = compute_monthly_pnl(
        month=feb,
        sales=sales,
        recipes=RecipeCatalog([chang_recipe]),
        cost=cost,
        brands=[brand],
        weigh_ins=weigh_ins,
        cafe_items=[],
        cafe_beginning=[],
        cafe_ending=[],
        purchases=[],
        fixed_costs=fixed_costs,
    )

    assert pnl.goal.days_in_month == 28
    assert pnl.goal.target == D("280000")
    assert pnl.entity_net_profit == D("300000")
    assert pnl.goal.met is True
    assert pnl.goal.surplus == D("20000")


# --- AC 5: cash-flow view (payables by invoice date), separate from accrual --


def test_cash_flow_view_reports_payables_by_invoice_date(
    month: tuple[int, int],
    milk_item: CafeItem,
    month_start: date,
    month_end: date,
    latte_recipe: Recipe,
    cost: CostBook,
) -> None:
    """The cash-flow view reports payables recognised by invoice date.

    Worked example: a 250 THB milk delivery invoiced Jun 15 and a 10000 THB
    beans delivery invoiced Jun 20 both fall in June -> total payables 10250
    THB, with one entry per invoice. A delivery invoiced May 31 (prior month)
    and one invoiced Jul 1 (next month) are excluded — payables are cash-basis
    by invoice date, NOT by consumption.

    The accrual COGS for the month is a DIFFERENT number (it is driven by
    what was consumed, per cafe stock counts); the cash-flow view answers
    "what bills landed this month?" independently.
    """
    purchases = [
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 6, 15),
            lines=(_purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),),
            vat=D("0"),
            total=D("250"),
        ),
        Purchase(
            supplier_id="phuket-coffee",
            invoice_date=date(2026, 6, 20),
            lines=(_purchase_line("beans-arabica", "Arabica 1kg", D("5000"), D("2")),),
            vat=D("0"),
            total=D("10000"),
        ),
        # Outside June -> excluded from the cash-flow view.
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 5, 31),
            lines=(_purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),),
            vat=D("0"),
            total=D("250"),
        ),
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 7, 1),
            lines=(_purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),),
            vat=D("0"),
            total=D("250"),
        ),
    ]
    cafe_beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=month_start),
    ]
    cafe_ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=month_end),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=[],
        recipes=RecipeCatalog([latte_recipe]),
        cost=cost,
        brands=[],
        weigh_ins=[],
        cafe_items=[milk_item],
        cafe_beginning=cafe_beginning,
        cafe_ending=cafe_ending,
        purchases=purchases,
        fixed_costs=[],
    )

    cf = pnl.cash_flow
    assert cf.month == month
    assert cf.total_payables == D("10250")  # 250 + 10000 only
    assert len(cf.entries) == 2
    # Entries sorted by (invoice_date, supplier_id) for determinism.
    assert [(e.invoice_date, e.supplier_id, e.total) for e in cf.entries] == [
        (date(2026, 6, 15), "phuket-dairy", D("250")),
        (date(2026, 6, 20), "phuket-coffee", D("10000")),
    ]


def test_cash_flow_differs_from_accrual_cogs(
    month: tuple[int, int],
    milk_item: CafeItem,
    month_start: date,
    month_end: date,
    cost: CostBook,
) -> None:
    """Cash-flow payables and accrual COGS are genuinely different numbers.

    This is the whole point of carrying both views (PRD user story 24): a big
    delivery invoiced late in June that mostly sits in inventory shows up
    fully in the cash-flow view (the bill is owed) but only partially in
    accrual COGS (little was consumed).

    Worked example: opening 5000ml (Jun 1), a 50000ml delivery on Jun 28 at
    0.025 THB/ml (1250 THB payable), closing 51000ml (Jun 30). Consumed =
    5000 + 50000 − 51000 = 4000ml -> accrual COGS = 4000 × 0.025 = 100 THB.
    Cash-flow payables = 1250 THB (the full invoice). The two differ because
    consumption and invoicing answer different questions.
    """
    purchases = [
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 6, 28),
            lines=(_purchase_line("milk-fresh", "Fresh milk 1L", D("50000"), D("0.025")),),
            vat=D("0"),
            total=D("1250"),
        ),
    ]
    cafe_beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=month_start),
    ]
    cafe_ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("51000"), timestamp=month_end),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=[],
        recipes=RecipeCatalog([]),
        cost=cost,
        brands=[],
        weigh_ins=[],
        cafe_items=[milk_item],
        cafe_beginning=cafe_beginning,
        cafe_ending=cafe_ending,
        purchases=purchases,
        fixed_costs=[],
    )

    by_seg = {sp.segment: sp for sp in pnl.segment_pnl}
    assert by_seg[Segment.CAFE].accrual_cogs == D("100")  # consumed basis
    assert pnl.cash_flow.total_payables == D("1250")  # invoiced basis


# --- AC 6: end-to-end monthly P&L (synthetic inventory + purchases + fixed) --


def test_end_to_end_monthly_pnl_full_reconciliation(
    month: tuple[int, int],
    month_start: date,
    month_end: date,
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    latte_recipe: Recipe,
    milk_item: CafeItem,
    cost: CostBook,
) -> None:
    """Full slice-08 seam: synthetic inventory + purchases + fixed costs in;
    the complete monthly accrual P&L out.

    Worked example over June 2026 (30 days, target 10,000 × 30 = 300,000 THB):

      Bar (kegs):
        Chang: Jun 1 gross 25000g (net 20000g -> 20000ml), Jun 30 gross 15000g
        (net 10000g -> 10000ml). Consumed 10000ml × 0.07 = 700 THB accrual COGS.
        Sales: 100 × Chang @ 120 = 12,000 THB -> bar CM = 11,300.

      Cafe (stock counts):
        Milk:  5000ml (Jun 1) + 10000ml delivered Jun 15 @ 0.025 − 6000ml
               (Jun 30) = 9000ml consumed -> 225 THB.
        Beans: 2000g (Jun 1) + 5000g delivered Jun 10 @ 2 − 1000g (Jun 30) =
               6000g consumed -> 12,000 THB.
        Cafe accrual COGS = 225 + 12,000 = 12,225 THB.
        Sales: 80 × Latte @ 120 = 9,600 THB -> cafe CM = -2,625 (red).

      Fixed costs (June): rent 25,000 + utilities 8,000 + staff salaries
      60,000 = 93,000 THB.

      Entity net profit = (11,300 + (-2,625)) − 93,000 = -84,325.
      Goal: target 300,000, actual -84,325, met False, surplus -384,325.
      Cash flow: payables = 250 (milk) + 10,000 (beans) = 10,250 THB.
    """
    weigh_ins = [
        KegWeighIn(brand_id="chang", weighed_on=month_start, gross_weight_g=D("25000")),
        KegWeighIn(brand_id="chang", weighed_on=month_end, gross_weight_g=D("15000")),
    ]
    cafe_beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=month_start),
        CafeStockCount(sku_id="beans-arabica", quantity=D("2000"), timestamp=month_start),
    ]
    cafe_ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("6000"), timestamp=month_end),
        CafeStockCount(sku_id="beans-arabica", quantity=D("1000"), timestamp=month_end),
    ]
    beans_item = CafeItem(
        sku_id="beans-arabica",
        name="Arabica beans",
        unit="g",
        cadence=CafeCountCadence.WEEKLY,
    )
    # Add the beans price to the cost book (milk already there).
    full_cost = CostBook(
        {
            "chang-keg": (D("0.07"), date(2026, 6, 1)),
            "beans-arabica": (D("2"), date(2026, 6, 1)),
            "milk-fresh": (D("0.025"), date(2026, 6, 1)),
        }
    )
    purchases = [
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 6, 15),
            lines=(_purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),),
            vat=D("0"),
            total=D("250"),
        ),
        Purchase(
            supplier_id="phuket-coffee",
            invoice_date=date(2026, 6, 10),
            lines=(_purchase_line("beans-arabica", "Arabica 1kg", D("5000"), D("2")),),
            vat=D("0"),
            total=D("10000"),
        ),
    ]
    # 100 Chang sales + 80 Latte sales, spread across June days.
    sales = [
        Sale(item_id="chang-draft-500", timestamp=date(2026, 6, d), sell_price=D("120"))
        for d in ((list(range(1, 31)) * 4)[:100])  # 100 sales across the month
    ]
    sales += [
        Sale(item_id="espresso-latte", timestamp=date(2026, 6, d), sell_price=D("120"))
        for d in ((list(range(1, 31)) * 3)[:80])  # 80 sales across the month
    ]
    fixed_costs = [
        FixedCost(amount=D("25000"), category=FixedCostCategory.RENT, period=month),
        FixedCost(amount=D("8000"), category=FixedCostCategory.UTILITIES, period=month),
        FixedCost(
            amount=D("60000"), category=FixedCostCategory.STAFF_SALARIES, period=month
        ),
    ]

    pnl = compute_monthly_pnl(
        month=month,
        sales=sales,
        recipes=RecipeCatalog([chang_recipe, latte_recipe]),
        cost=full_cost,
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        cafe_items=[milk_item, beans_item],
        cafe_beginning=cafe_beginning,
        cafe_ending=cafe_ending,
        purchases=purchases,
        fixed_costs=fixed_costs,
    )

    # --- per-segment accrual P&L ---
    by_seg = {sp.segment: sp for sp in pnl.segment_pnl}
    bar = by_seg[Segment.BAR]
    cafe = by_seg[Segment.CAFE]

    assert bar.revenue == D("12000")
    assert bar.accrual_cogs == D("700")
    assert bar.contribution_margin == D("11300")
    assert bar.is_red is False

    assert cafe.revenue == D("9600")
    assert cafe.accrual_cogs == D("12225")
    assert cafe.contribution_margin == D("-2625")
    assert cafe.is_red is True

    # --- entity net profit ---
    assert pnl.total_fixed_costs == D("93000")
    assert pnl.entity_net_profit == D("-84325")
    assert len(pnl.fixed_costs) == 3

    # --- goal comparison ---
    assert pnl.goal.days_in_month == 30
    assert pnl.goal.target == D("300000")
    assert pnl.goal.actual == D("-84325")
    assert pnl.goal.met is False
    assert pnl.goal.surplus == D("-384325")

    # --- cash-flow view (separate from accrual) ---
    assert pnl.cash_flow.total_payables == D("10250")
    assert len(pnl.cash_flow.entries) == 2
