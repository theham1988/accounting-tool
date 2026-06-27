"""End-to-end keg inventory test seam (slice 05).

Per the PRD testing rules these tests read as worked examples:
"given a 20L keg that weighed 25kg full and 20kg at next week's weigh, with
9 x 500ml pours rung up, the consumed volume is 5L and the loss is 10%."

They feed synthetic weigh-ins + sales through the real keg-inventory engine
and assert:
  - beer volume computed from (gross - tare) / density
  - actual vs theoretical yield and loss %
  - consumed volume x current cost per ml = accrual COGS contribution
  - the density-approximation tolerance is surfaced, not silently absorbed

The cost-per-ml lookup reuses the slice-04 ``CostBook``; the rung-up pours are
resolved through the slice-04 ``RecipeCatalog`` (a recipe ingredient whose
sku_id matches the brand's beer_sku_id contributes its ml). This keeps slice 05
focused on the weigh -> volume -> COGS path while reusing the existing
vocabulary for "what was sold".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tangerine.cost import CostBook
from tangerine.keg_inventory import (
    DENSITY_TOLERANCE_NOTE,
    beer_volume_ml,
    compute_keg_inventory,
    rung_up_pours_ml,
)
from tangerine.recipes import RecipeCatalog
from tangerine.types import (
    KegBrand,
    KegWeighIn,
    Recipe,
    RecipeIngredient,
    Sale,
    Segment,
    SkuMapping,
)

D = Decimal


# --- shared fixtures --------------------------------------------------------


@pytest.fixture
def chang_brand() -> KegBrand:
    """A 20L Chang keg: tare 5000g, water density, 20000ml nominal capacity."""
    return KegBrand(
        brand_id="chang",
        name="Chang Draught",
        beer_sku_id="chang-keg",
        tare_weight_g=D("5000"),
    )


@pytest.fixture
def chang_recipe() -> Recipe:
    """A 500ml Chang pour recipe (slice-04 shape: beer input in ml)."""
    return Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )


@pytest.fixture
def week1() -> date:
    return date(2026, 6, 1)


@pytest.fixture
def week2() -> date:
    return date(2026, 6, 8)


@pytest.fixture
def cost_book(chang_brand: KegBrand) -> CostBook:
    """Cost book where chang-keg costs 0.07 THB per ml."""
    return CostBook({chang_brand.beer_sku_id: (D("0.07"), date(2026, 6, 1))})


# --- 1. beer volume from gross weight ----------------------------------------


def test_beer_volume_is_gross_minus_tare_over_density(
    chang_brand: KegBrand, week1: date
) -> None:
    """Worked example: a full 20L Chang keg.

    Gross weight 25000g, tare 5000g -> net 20000g. At water density (1.0 g/ml)
    that is 20000 ml of beer.
    """
    weigh = KegWeighIn(
        brand_id=chang_brand.brand_id,
        weighed_on=week1,
        gross_weight_g=D("25000"),
    )

    assert beer_volume_ml(chang_brand, weigh) == D("20000")


def test_beer_volume_uses_per_brand_density(chang_brand: KegBrand) -> None:
    """A brand with a non-default density converts net weight on that basis.

    Same gross/tare (net 20000g) at density 1.01 g/ml -> 20000 / 1.01 ml.
    Per the PRD, density defaults to water; a brand may override when better
    data exists. The result carries the documented ~0.5-1.5% tolerance, surfaced
    separately on the report rather than silently absorbed.
    """
    heavy = KegBrand(
        brand_id=chang_brand.brand_id,
        name=chang_brand.name,
        beer_sku_id=chang_brand.beer_sku_id,
        tare_weight_g=chang_brand.tare_weight_g,
        density_g_per_ml=D("1.01"),
    )
    weigh = KegWeighIn(
        brand_id=heavy.brand_id,
        weighed_on=date(2026, 6, 1),
        gross_weight_g=D("25000"),
    )

    assert beer_volume_ml(heavy, weigh) == D("20000") / D("1.01")


def test_beer_volume_zero_for_empty_keg(
    chang_brand: KegBrand, week1: date
) -> None:
    """Gross == tare -> net 0 -> 0 ml (an empty keg)."""
    weigh = KegWeighIn(
        brand_id=chang_brand.brand_id,
        weighed_on=week1,
        gross_weight_g=chang_brand.tare_weight_g,
    )

    assert beer_volume_ml(chang_brand, weigh) == D("0")


# --- 2. rung-up pours resolved through the recipe catalog --------------------


def test_rung_up_pours_sum_recipe_beer_ml(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    week1: date,
    week2: date,
) -> None:
    """Worked example: 9 pours of 500ml Chang -> 4500ml rung up.

    Each sale resolves to the recipe; the ingredient matching the brand's
    beer_sku_id contributes its quantity (500ml) per unit sold.
    """
    recipes = RecipeCatalog([chang_recipe])
    sales = [
        Sale(item_id="chang-draft-500", timestamp=week1, sell_price=D("120"))
        for _ in range(9)
    ]

    total = rung_up_pours_ml(sales=sales, recipes=recipes, brand=chang_brand)

    assert total == D("4500")  # 9 * 500


def test_rung_up_pours_ignore_sales_of_other_brands(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    week1: date,
) -> None:
    """Sales whose recipe references a different beer SKU contribute nothing.

    A Leo pour (sku_id "leo-keg") is not the Chang brand; it is excluded from
    Chang's rung-up total even though it shares the menu.
    """
    leo_recipe = Recipe(
        sku_id="leo-draft-500",
        name="Leo Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="leo-keg", quantity=D("500")),),
    )
    recipes = RecipeCatalog([chang_recipe, leo_recipe])
    sales = [
        Sale(item_id="chang-draft-500", timestamp=week1, sell_price=D("120")),
        Sale(item_id="leo-draft-500", timestamp=week1, sell_price=D("110")),
    ]

    chang_total = rung_up_pours_ml(sales=sales, recipes=recipes, brand=chang_brand)

    assert chang_total == D("500")  # only the Chang pour


def test_rung_up_pours_zero_when_no_recipe(
    chang_brand: KegBrand, week1: date
) -> None:
    """A sold item with no recipe mapping contributes 0 ml to the brand.

    Mirrors slice 04's unmapped handling: unmapped sales surface elsewhere; for
    keg yield they simply are not counted as rung-up pours of any brand.
    """
    recipes = RecipeCatalog([])  # nothing maps
    sales = [Sale(item_id="mystery", timestamp=week1, sell_price=D("90"))]

    assert rung_up_pours_ml(sales=sales, recipes=recipes, brand=chang_brand) == D("0")


def test_rung_up_pours_resolve_via_sku_mapping(
    chang_brand: KegBrand, chang_recipe: Recipe, week1: date
) -> None:
    """A Loyverse item maps to a recipe through a SkuMapping (slice-04 path).

    The sold id "chang-pint-pos" has no recipe keyed by it directly, but its
    SkuMapping resolves it to "chang-draft-500" -> recipe -> 500ml of beer.
    """
    recipes = RecipeCatalog(
        [chang_recipe],
        mappings=[SkuMapping(item_id="chang-pint-pos", sku_id="chang-draft-500")],
    )
    sales = [Sale(item_id="chang-pint-pos", timestamp=week1, sell_price=D("120"))]

    assert rung_up_pours_ml(sales=sales, recipes=recipes, brand=chang_brand) == D("500")


# --- 3. period accrual COGS = consumed volume x cost per ml -------------------


def test_period_accrual_cogs_uses_consumed_volume_at_current_cost(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week1: date,
    week2: date,
) -> None:
    """Worked example: beginning 20L, ending 15L -> 5L consumed at 0.07/ml.

    Beginning weigh 25000g -> 20000ml. Ending weigh 20000g -> 15000ml.
    Consumed 5000ml x 0.07 THB/ml = 350 THB accrual COGS for the period.
    No sales are needed for the COGS number itself (it is physical: weigh-based).
    """
    weigh_ins = [
        KegWeighIn(chang_brand.brand_id, week1, D("25000")),
        KegWeighIn(chang_brand.brand_id, week2, D("20000")),
    ]
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=[],
        recipes=recipes,
        cost=cost_book,
        period_end=week2,
    )

    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.brand_id == "chang"
    assert row.beginning_volume_ml == D("20000")
    assert row.ending_volume_ml == D("15000")
    assert row.volume_consumed_ml == D("5000")
    assert row.accrual_cogs == D("350")  # 5000 * 0.07
    assert report.total_accrual_cogs == D("350")


def test_accrual_cogs_zero_when_brand_has_no_approved_price(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    week1: date,
    week2: date,
) -> None:
    """A brand whose beer SKU has no approved price accrues 0 COGS.

    Mirrors slice 04's unknown-price convention: ``cost_per_unit`` is 0 for an
    unknown SKU. The volume math still runs; only the money side is zero. (A
    richer flag will land with slice 08's P&L; this slice just produces the
    number.)
    """
    weigh_ins = [
        KegWeighIn(chang_brand.brand_id, week1, D("25000")),
        KegWeighIn(chang_brand.brand_id, week2, D("20000")),
    ]
    empty_cost = CostBook({})  # no prices
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=[],
        recipes=recipes,
        cost=empty_cost,
        period_end=week2,
    )

    row = report.rows[0]
    assert row.volume_consumed_ml == D("5000")
    assert row.accrual_cogs == D("0")


# --- 4. actual vs theoretical yield -> loss % --------------------------------


def test_loss_pct_is_one_minus_actual_over_theoretical(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week1: date,
    week2: date,
) -> None:
    """Worked example: 5L consumed, 4.5L rung up -> 10% loss.

    Theoretical yield is the consumed volume (5L). Actual yield is the rung-up
    beer ml resolved from sales (9 x 500ml = 4.5L). Loss = 1 - 4.5/5 = 10%.
    Per the PRD the variance is surfaced but not attributed to individual kegs.
    """
    weigh_ins = [
        KegWeighIn(chang_brand.brand_id, week1, D("25000")),
        KegWeighIn(chang_brand.brand_id, week2, D("20000")),
    ]
    # 9 pours of 500ml = 4500ml rung up, all dated inside the period.
    sales = [
        Sale(item_id="chang-draft-500", timestamp=week2, sell_price=D("120"))
        for _ in range(9)
    ]
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=sales,
        recipes=recipes,
        cost=cost_book,
        period_end=week2,
    )

    row = report.rows[0]
    assert row.rung_up_pours_ml == D("4500")
    assert row.volume_consumed_ml == D("5000")
    assert row.theoretical_yield_ml == D("5000")  # consumed volume, same basis
    assert row.loss_pct == D("0.10")  # 10%


def test_loss_pct_none_when_consumed_volume_is_zero(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week1: date,
    week2: date,
) -> None:
    """Identical weighs -> 0 ml consumed -> loss % is None (meaningless ratio).

    A brand that sold nothing, or was weighed identically, has no consumed
    volume to take a ratio against. Surfacing 0% would imply perfect yield;
    None is honest.
    """
    weigh_ins = [
        KegWeighIn(chang_brand.brand_id, week1, D("25000")),
        KegWeighIn(chang_brand.brand_id, week2, D("25000")),  # no change
    ]
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=[],
        recipes=recipes,
        cost=cost_book,
        period_end=week2,
    )

    row = report.rows[0]
    assert row.volume_consumed_ml == D("0")
    assert row.loss_pct is None
    assert row.accrual_cogs == D("0")  # nothing consumed


def test_loss_pct_can_be_negative_when_over_pour(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week1: date,
    week2: date,
) -> None:
    """Rung-up exceeds consumed volume -> negative loss (over-pour / under-weigh).

    5L consumed, 5.5L rung up -> loss = 1 - 5.5/5 = -0.10 (-10%). A negative
    loss is a real signal (over-pouring, under-weighing, or unmapped free-pour
    stock) and is surfaced as-is rather than clamped.
    """
    weigh_ins = [
        KegWeighIn(chang_brand.brand_id, week1, D("25000")),
        KegWeighIn(chang_brand.brand_id, week2, D("20000")),
    ]
    sales = [
        Sale(item_id="chang-draft-500", timestamp=week2, sell_price=D("120"))
        for _ in range(11)  # 11 * 500 = 5500ml rung up > 5000ml consumed
    ]
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=sales,
        recipes=recipes,
        cost=cost_book,
        period_end=week2,
    )

    assert report.rows[0].loss_pct == D("-0.10")


def test_mid_period_refill_surfaces_negative_consumption(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week1: date,
    week2: date,
) -> None:
    """A gross-weight increase between weighs (refill with no separate weigh)
    surfaces as negative consumption, negative COGS, negative loss.

    Per docs/issues/05: variance is "surfaced but not attributed to individual
    kegs." Silently clamping the negative to zero would mask the refill
    signal; instead the row carries negative volume_consumed_ml, negative
    accrual_cogs, and a negative loss_pct so the review sees it.
    """
    weigh_ins = [
        KegWeighIn(chang_brand.brand_id, week1, D("20000")),  # 15L
        KegWeighIn(chang_brand.brand_id, week2, D("25000")),  # 20L (refilled)
    ]
    # 3 pours rung up over the period = 1500ml.
    sales = [
        Sale(item_id="chang-draft-500", timestamp=week2, sell_price=D("120"))
        for _ in range(3)
    ]
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=sales,
        recipes=recipes,
        cost=cost_book,
        period_end=week2,
    )

    row = report.rows[0]
    assert row.volume_consumed_ml == D("-5000")  # 15000 - 20000
    assert row.accrual_cogs == D("-350")  # -5000 * 0.07
    # 1 - 1500/(-5000) = 1 + 0.3 = 1.30
    assert row.loss_pct == D("1.30")


# --- 5. first weigh has no prior -> brand is "unstarted" ---------------------


def test_brand_with_only_one_weigh_is_unstarted(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week1: date,
) -> None:
    """A brand whose only weigh is the very first one has no period yet.

    Its volume becomes the beginning inventory for the next period; this period
    has no consumed volume to cost, so the brand appears in
    ``unstarted_brand_ids`` and produces no row.
    """
    weigh_ins = [KegWeighIn(chang_brand.brand_id, week1, D("25000"))]
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=[],
        recipes=recipes,
        cost=cost_book,
        period_end=week1,
    )

    assert report.rows == ()
    assert report.unstarted_brand_ids == ("chang",)
    assert report.total_accrual_cogs == D("0")


def test_brand_with_no_weigh_at_all_is_unstarted(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week2: date,
) -> None:
    """A brand with no weigh-in on or before period_end is unstarted."""
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=[],
        sales=[],
        recipes=recipes,
        cost=cost_book,
        period_end=week2,
    )

    assert report.rows == ()
    assert report.unstarted_brand_ids == ("chang",)


def test_period_uses_two_most_recent_weighs_before_period_end(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
) -> None:
    """With three weighs, the period ending at the latest uses the two newest.

    This is the "weekly cadence" path: the most recent weigh is the ending
    inventory, the one immediately before it is the beginning inventory. Older
    weighs are ignored.
    """
    week0 = date(2026, 5, 25)
    week1 = date(2026, 6, 1)
    week2 = date(2026, 6, 8)
    weigh_ins = [
        KegWeighIn(chang_brand.brand_id, week0, D("28000")),  # older, ignored
        KegWeighIn(chang_brand.brand_id, week1, D("25000")),  # beginning
        KegWeighIn(chang_brand.brand_id, week2, D("20000")),  # ending
    ]
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=[],
        recipes=recipes,
        cost=cost_book,
        period_end=week2,
    )

    row = report.rows[0]
    assert row.beginning_weighed_on == week1
    assert row.ending_weighed_on == week2
    assert row.beginning_volume_ml == D("20000")
    assert row.ending_volume_ml == D("15000")


# --- 6. density tolerance is documented on every row -------------------------


def test_density_tolerance_note_is_surfaced_on_every_row(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week1: date,
    week2: date,
) -> None:
    """The water-density approximation (~0.5-1.5% volume error) is surfaced, not
    silently absorbed. Every row carries the note so a reader of the report
    cannot mistake the volume for exact.
    """
    weigh_ins = [
        KegWeighIn(chang_brand.brand_id, week1, D("25000")),
        KegWeighIn(chang_brand.brand_id, week2, D("20000")),
    ]
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=[],
        recipes=recipes,
        cost=cost_book,
        period_end=week2,
    )

    row = report.rows[0]
    assert row.density_g_per_ml == D("1.0")
    assert row.density_tolerance_note == DENSITY_TOLERANCE_NOTE
    assert "0.5" in row.density_tolerance_note  # mentions the tolerance band
    assert "1.5" in row.density_tolerance_note


# --- 7. multi-brand end-to-end ----------------------------------------------


def test_multi_brand_report_aggregates_accrual_cogs(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week1: date,
    week2: date,
) -> None:
    """Two brands (Chang + Leo) in one period: rows per brand, total COGS summed.

    Chang: 5L consumed @ 0.07 = 350 THB.
    Leo:   4L consumed @ 0.06 = 240 THB.
    Total accrual COGS for the period = 590 THB.
    """
    leo_brand = KegBrand(
        brand_id="leo",
        name="Leo Draught",
        beer_sku_id="leo-keg",
        tare_weight_g=D("5000"),
    )
    leo_recipe = Recipe(
        sku_id="leo-draft-500",
        name="Leo Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="leo-keg", quantity=D("500")),),
    )
    cost = CostBook(
        {
            "chang-keg": (D("0.07"), date(2026, 6, 1)),
            "leo-keg": (D("0.06"), date(2026, 6, 1)),
        }
    )
    # Chang 25->20kg (5L consumed); Leo 25->21kg (4L consumed).
    weigh_ins = [
        KegWeighIn("chang", week1, D("25000")),
        KegWeighIn("chang", week2, D("20000")),
        KegWeighIn("leo", week1, D("25000")),
        KegWeighIn("leo", week2, D("21000")),
    ]
    recipes = RecipeCatalog([chang_recipe, leo_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand, leo_brand],
        weigh_ins=weigh_ins,
        sales=[],
        recipes=recipes,
        cost=cost,
        period_end=week2,
    )

    by_brand = {r.brand_id: r for r in report.rows}
    assert by_brand["chang"].accrual_cogs == D("350")
    assert by_brand["leo"].accrual_cogs == D("240")
    assert report.total_accrual_cogs == D("590")
    assert report.period_start == week1
    assert report.period_end == week2


def test_period_dates_in_report_match_the_ending_weigh(
    chang_brand: KegBrand,
    chang_recipe: Recipe,
    cost_book: CostBook,
    week1: date,
    week2: date,
) -> None:
    """The report's period_start/end are the weigh dates that bound it."""
    weigh_ins = [
        KegWeighIn(chang_brand.brand_id, week1, D("25000")),
        KegWeighIn(chang_brand.brand_id, week2, D("20000")),
    ]
    recipes = RecipeCatalog([chang_recipe])

    report = compute_keg_inventory(
        brands=[chang_brand],
        weigh_ins=weigh_ins,
        sales=[],
        recipes=recipes,
        cost=cost_book,
        period_end=week2,
    )

    assert report.period_start == week1
    assert report.period_end == week2
