"""End-to-end recipe and per-item cost engine test seam (slice 04).

Per the PRD testing rules these tests read as worked examples:
"given a keg of Chang costing X, sold as Y pours, the 500ml margin is Z."
They feed synthetic recipes, sales, and approved purchases through the
real margin engine and assert the per-item margin numbers.

The cost-per-unit is derived from the latest approved purchase price held
in the ``ApprovalBook`` (populated by slice 03). Recipes carry only the
SKU + quantity of each input; the engine resolves current cost from the
book, so a re-pricing after the next receipt approval flows straight into
tomorrow's margin without touching the recipe.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tangerine.approvals import ApprovalBook, apply_decision
from tangerine.cost import CostBook, cost_per_unit
from tangerine.margin import (
    CostResolver,
    compute_item_margins,
)
from tangerine.receipts import check_receipt
from tangerine.recipes import RecipeCatalog
from tangerine.types import (
    ExtractedReceipt,
    ExtractedReceiptLine,
    ReceiptDecision,
    ReceiptState,
    Recipe,
    RecipeIngredient,
    Sale,
    Segment,
    Sku,
    SkuMapping,
    Supplier,
)

D = Decimal


# --- shared fixtures --------------------------------------------------------


@pytest.fixture
def day() -> date:
    return date(2026, 6, 24)


@pytest.fixture
def chang_sku() -> Sku:
    return Sku(sku_id="chang-keg", name="Chang draught beer", unit="ml")


@pytest.fixture
def beer_supplier() -> Supplier:
    return Supplier(supplier_id="phuket-beverages", name="Phuket Beverages Co.")


# --- helpers ----------------------------------------------------------------


def _approve_purchase(
    *,
    supplier_id: str,
    on: date,
    sku_id: str,
    per_unit_qty: Decimal,
    unit_price: Decimal,
    skus: dict[str, Sku],
    book: ApprovalBook,
) -> None:
    """Approve a single-line purchase at a given per-unit price.

    A quantity of ``per_unit_qty`` units at ``unit_price`` THB each, plus 7%
    VAT, reconciles through the sum-check. On approval the line's
    ``(sku_id, supplier_id)`` price is recorded in the book, which the cost
    engine then resolves as the SKU's current cost per unit.
    """
    line_total = (per_unit_qty * unit_price).quantize(D("0.01"))
    receipt = ExtractedReceipt(
        supplier_id=supplier_id,
        invoice_date=on,
        lines=(
            ExtractedReceiptLine(
                description=f"{sku_id} purchase",
                quantity=per_unit_qty,
                unit_price=unit_price,
                sku_id=sku_id,
            ),
        ),
        vat=(line_total * D("0.07")).quantize(D("0.01")),
        total=(line_total * D("1.07")).quantize(D("0.01")),
    )
    checked = check_receipt(receipt, skus=skus, reference_prices={})
    apply_decision(
        checked, ReceiptDecision(decision=ReceiptState.APPROVED), book
    )


# --- recipe schema: inputs (SKU + qty) and yield ----------------------------
#
# Yield is a decimal quantity denominated in the output SKU's own unit
# (issue #34 / CONTEXT.md "Yield"): an ahi-sauce batch yields ~61 g; a 1L
# pitcher yields 2 (units — two pours). One formula everywhere:
# cost-per-unit = input cost / yield_qty. A yield carries an
# estimated/measured marker: estimated yields are recomputed from the sum of
# input quantities when rows change; measured yields are fixed.


def test_recipe_costs_per_unit_against_a_fractional_weight_yield(
    day: date,
) -> None:
    """Worked example: an ahi-sauce batch costs 12.20 THB of inputs and
    yields 61 g, so one gram of sauce costs 0.20 THB.

    This is the recipe shape that motivated issue #34's unified yield: a
    weight-denominated output whose batch yield is fractional. The old
    integer ``yield_units`` could not express "yields 61 g" — every prep
    was pinned to ``yield_units=1`` and cost the *whole batch* per gram.
    """
    cost = CostBook({
        "soy sauce": (D("0.05"), date(2026, 6, 1)),
        "mirin": (D("0.30"), date(2026, 6, 1)),
    })
    # Inputs: 100 g soy + 24 g mirin = 5.00 + 7.20 = 12.20 THB.
    recipe = Recipe(
        sku_id="sauce-ahi",
        name="Ahi Sauce",
        segment=Segment.BAR,
        ingredients=(
            RecipeIngredient(sku_id="soy sauce", quantity=D("100")),
            RecipeIngredient(sku_id="mirin", quantity=D("24")),
        ),
        yield_qty=D("61"),
    )
    resolver = CostResolver(RecipeCatalog([recipe]), cost)

    # Input cost 12.20 THB spread over a 61 g batch = 0.20 THB per gram.
    assert resolver.cost_per_unit(recipe) * recipe.yield_qty == D("12.20")
    assert resolver.cost_per_unit(recipe) == D("0.20")


def test_recipe_yield_defaults_to_one_and_is_estimated() -> None:
    """A recipe constructed without an explicit yield defaults to producing
    one unit of its output SKU, marked as an estimate.

    This is the "new recipe in the editor" case (issue #34 AC: the yield
    defaults to the sum of input quantities, marked estimated, until a
    partner enters a measured value). A single-input cafe recipe built with
    no yield fields at all carries ``yield_qty=1`` and ``yield_estimated=True``.
    """
    recipe = Recipe(
        sku_id="espresso",
        name="Espresso",
        segment=Segment.CAFE,
        ingredients=(RecipeIngredient(sku_id="beans-arabica", quantity=D("18")),),
    )
    assert recipe.yield_qty == D("1")
    assert recipe.yield_estimated is True


def test_recipe_with_unit_denominated_yield_costs_per_pour(day: date) -> None:
    """A 1L Chang pitcher recipe takes 1000 ml of beer and yields 2 units
    (two 500 ml pours), so cost-per-unit = 1000 × 0.07 / 2 = 35 THB.

    The pitcher is the case where the old integer ``yield_units`` and the
    new decimal ``yield_qty`` agree: a ``unit``-denominated output whose
    yield is a small whole number. The unified formula must still produce
    the same per-pour cost the old engine did.
    """
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipe = Recipe(
        sku_id="chang-pitcher-1l",
        name="Chang Pitcher 1L",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("1000")),),
        yield_qty=D("2"),
    )
    recipes = RecipeCatalog([recipe])
    resolver = CostResolver(recipes, cost)

    # 1000 ml @ 0.07 = 70 THB input cost, yields 2 units -> 35 THB per unit.
    assert resolver.cost_per_unit(recipe) * recipe.yield_qty == D("70")
    assert resolver.cost_per_unit(recipe) == D("35")


def test_cost_per_unit_uses_latest_approved_price(
    day: date, chang_sku: Sku, beer_supplier: Supplier
) -> None:
    """Worked example: a 30L keg approved at 0.07 THB/ml makes 1 ml cost 0.07.

    The cost engine resolves the SKU's current price from the approval book,
    supplier-agnostic (latest across all suppliers). No recipe needed yet —
    this is the unit-cost primitive the recipe engine multiplies.
    """
    book = ApprovalBook()
    _approve_purchase(
        supplier_id=beer_supplier.supplier_id,
        on=date(2026, 6, 1),
        sku_id="chang-keg",
        per_unit_qty=D("30000"),
        unit_price=D("0.07"),
        skus={chang_sku.sku_id: chang_sku},
        book=book,
    )

    cost = CostBook.from_book(book)

    assert cost_per_unit(cost, "chang-keg") == D("0.07")


# --- recipe cost: sum of (ingredient qty * current cost per unit) ------------


def test_recipe_cost_sums_ingredients_at_current_price(
    day: date, chang_sku: Sku, beer_supplier: Supplier
) -> None:
    """Worked example: a 500ml Chang pour at 0.07/ml -> recipe cost 35 THB.

    The recipe carries only the SKU + quantity of each input. The cost is
    looked up from the CostBook, so it tracks the latest approved purchase
    price rather than a stale number baked into the recipe.
    """
    book = ApprovalBook()
    _approve_purchase(
        supplier_id=beer_supplier.supplier_id,
        on=date(2026, 6, 1),
        sku_id="chang-keg",
        per_unit_qty=D("30000"),
        unit_price=D("0.07"),
        skus={chang_sku.sku_id: chang_sku},
        book=book,
    )
    cost = CostBook.from_book(book)

    recipe = Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(
            RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
        ),
    )
    resolver = CostResolver(RecipeCatalog([recipe]), cost)

    # Single ingredient, yield defaults to 1 -> input cost == per-unit cost.
    assert resolver.cost_per_unit(recipe) == D("35")


# --- per-item margin table: cost, margin, margin %, sell volume --------------


def test_item_margin_for_single_unit(day: date) -> None:
    """Worked example: one Chang draft at 120 THB, cost 35 THB.

    Margin 85 THB (70.83%). Sell volume 1 unit for the day. The per-item row
    carries every number the daily review table needs: cost per unit, sell
    price, revenue, COGS, gross margin, gross-margin %.
    """
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
            )
        ]
    )
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
    ]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    by_item = {m.item_id: m for m in margins}
    m = by_item["chang-draft-500"]
    assert m.units_sold == 1
    assert m.cost_per_unit == D("35")
    assert m.revenue == D("120")
    assert m.cogs == D("35")
    assert m.gross_margin == D("85")
    assert m.gross_margin_pct == D("70.83")
    assert m.sell_price == D("120")


def test_item_margin_aggregates_multi_unit_sales(day: date) -> None:
    """Three pours of the same item in a day roll up into one margin line."""
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
            )
        ]
    )
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))
        for _ in range(3)
    ]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    assert len(margins) == 1
    m = margins[0]
    assert m.units_sold == 3
    assert m.revenue == D("360")
    assert m.cogs == D("105")
    assert m.gross_margin == D("255")


# --- unmapped sold item flagged, not raised (PRD user story 12) -------------


def test_unmapped_sold_item_is_flagged_not_raised(day: date) -> None:
    """A sold item with no recipe surfaces as a flagged row, not an exception.

    PRD user story 12 requires unmapped sales to surface immediately. Slice
    04 reports them in the margin table with ``unmapped=True`` so one unmapped
    item does not abort the whole day's margin run. The row carries the real
    revenue (so it is visible) but zero COGS and a zero/None margin: its cost
    is unknown, so booking full revenue as margin would over-state
    profitability. The row is excluded from the daily margin totals.
    """
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipes = RecipeCatalog([])  # no recipes -> everything sold is unmapped
    sales = [Sale(item_id="mystery-item", timestamp=day, sell_price=D("90"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    assert len(margins) == 1
    m = margins[0]
    assert m.item_id == "mystery-item"
    assert m.units_sold == 1
    assert m.unmapped is True
    assert m.revenue == D("90")  # real revenue, surfaced
    assert m.cost_per_unit == D("0")  # no recipe -> no cost
    assert m.cogs == D("0")
    assert m.gross_margin == D("0")  # unknown cost -> no margin booked
    assert m.gross_margin_pct is None
    assert m.excluded_from_totals is True


def test_unmapped_and_mapped_items_coexist_in_same_run(day: date) -> None:
    """A day with one mapped and one unmapped item reports both rows.

    The mapped item gets a normal margin; the unmapped item gets a flagged
    row. Neither aborts the other.
    """
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
            )
        ]
    )
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="mystery-item", timestamp=day, sell_price=D("90")),
    ]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    by_item = {m.item_id: m for m in margins}
    assert by_item["chang-draft-500"].unmapped is False
    assert by_item["chang-draft-500"].gross_margin == D("85")
    assert by_item["mystery-item"].unmapped is True


def test_mapped_item_with_unpriced_ingredient_is_flagged_unknown_price(
    day: date,
) -> None:
    """A mapped item whose recipe references an unpriced SKU is flagged.

    The recipe exists, but the keg SKU has no approved purchase in the cost
    book, so the per-unit cost is unknown. The row is flagged
    ``unknown_price`` (not ``unmapped``) and excluded from totals — silently
    zero-costing it would book full revenue as margin and over-state profit.
    """
    cost = CostBook({})  # no prices at all
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
            )
        ]
    )
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    m = margins[0]
    assert m.unmapped is False  # it IS mapped
    assert m.unknown_price is True  # but an ingredient has no price
    assert m.excluded_from_totals is True
    assert m.gross_margin == D("0")  # no margin booked on unknown cost


def test_flagged_rows_revenue_in_headline_but_cogs_reliable_only(day: date) -> None:
    """Unmapped/unknown-price rows inflate revenue but not COGS (issue #71).

    Reverses the slice-04 reliable-rows-only rule for revenue (ADR-0008):
    the headline ties to Loyverse Gross sales, so every sale's revenue lands
    in ``total_revenue`` — mapped or not. COGS stays recipe-cost over
    reliable rows only (a flagged row's cost is unknown), and
    ``total_gross_margin = total_revenue - total_cogs`` follows.

    Worked example. One mapped Chang (120 revenue, 35 COGS) and one unmapped
    mystery item (90 revenue). The daily roll-up:

      total_revenue       = 120 + 90 = 210  (gross-sales headline)
      total_cogs          = 35            (mapped only)
      total_gross_margin  = 210 - 35 = 175 (revenue − cogs, by construction)
      flagged_revenue     = 90            (still surfaces the residue)

    The implicit assumption (the unmapped revenue carries zero COGS)
    overstates the margin on the uncosted portion — honest labelling lives
    on the template's "includes N THB of uncosted revenue" callout, and the
    needs-attention card still carries the unmapped item.
    """
    from tangerine.margin import compute_daily_margin
    from tangerine.seeded import SeededSource

    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipes = [
        Recipe(
            sku_id="chang-draft-500",
            name="Chang Draft 500ml",
            segment=Segment.BAR,
            ingredients=(
                RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
            ),
        )
    ]
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="mystery-item", timestamp=day, sell_price=D("90")),
    ]
    source = SeededSource(sales=sales, recipes=recipes, cost=cost)

    result = compute_daily_margin(source, day)

    assert result.total_revenue == D("210")  # gross-sales: mapped + unmapped
    assert result.total_cogs == D("35")  # COGS stays mapped-only
    assert result.total_gross_margin == D("175")  # 210 - 35
    assert result.flagged_revenue == D("90")  # unmapped revenue still surfaces


# --- Loyverse items map to recipes via SkuMapping ----------------------------


def test_loverse_item_maps_to_recipe_via_sku_mapping(day: date) -> None:
    """A Loyverse item id resolves to a recipe through a SKU mapping.

    Per issue 04: recipes are defined against SKUs, and Loyverse items map to
    SKUs. Here the Loyverse item ``chang-draft-500`` maps to the master SKU
    ``chang-draft``, whose recipe is 500ml of chang-keg. This decouples the
    recipe (a formula keyed by SKU) from the Loyverse item id (a menu
    identity), so multiple menu items can share one SKU/recipe.
    """
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipe = Recipe(
        sku_id="chang-draft",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )
    recipes = RecipeCatalog(
        [recipe],
        mappings=[SkuMapping(item_id="chang-draft-500", sku_id="chang-draft")],
    )
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    m = margins[0]
    assert m.cost_per_unit == D("35")
    assert m.gross_margin == D("85")
    assert m.unmapped is False


def test_item_with_no_mapping_and_no_matching_recipe_is_unmapped(
    day: date,
) -> None:
    """An item id with no SKU mapping (and no recipe keyed by that id) is unmapped."""
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipe = Recipe(
        sku_id="chang-draft",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )
    recipes = RecipeCatalog([recipe], mappings=[])  # no mapping for the sold id
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    assert margins[0].unmapped is True


def test_compute_daily_margin_honours_source_mappings(day: date) -> None:
    """A Source's ``mappings()`` must reach the margin engine, not just
    ``recipes()``.

    Regression test for a bug where ``compute_daily_margin`` /
    ``build_period_review`` rebuilt a ``RecipeCatalog`` from only
    ``source.recipes()``, silently dropping ``source.mappings()`` — every
    Loyverse item -> SKU mapping in ``config/recipes.yaml`` was therefore
    never consulted in production (regardless of whether it was correct),
    because ``compute_item_margins``-level tests exercise ``RecipeCatalog``
    directly and never go through a ``Source``.     Here the sold Loyverse item
    id (``10042``, a real variant SKU shape) differs from the recipe's own
    ``sku_id`` (``espresso``) and only resolves through the mapping — this
    must reach the daily rollup, not just flag as unmapped.
    """
    from tangerine.margin import compute_daily_margin
    from tangerine.seeded import SeededSource

    cost = CostBook({"beans-arabica": (D("2"), date(2026, 6, 1))})
    recipe = Recipe(
        sku_id="espresso",
        name="Espresso",
        segment=Segment.CAFE,
        ingredients=(RecipeIngredient(sku_id="beans-arabica", quantity=D("10")),),
    )
    sales = [Sale(item_id="10042", timestamp=day, sell_price=D("70"))]
    source = SeededSource(
        sales=sales,
        recipes=[recipe],
        cost=cost,
        mappings=[SkuMapping(item_id="10042", sku_id="espresso")],
    )

    result = compute_daily_margin(source, day)

    assert result.total_revenue == D("70")
    assert result.total_gross_margin == D("50")  # 70 - (10 * 2)
    assert result.flagged_revenue == D("0")
    assert result.item_margins[0].unmapped is False


# --- target-margin violations flagged (PRD user story 13) -------------------


def test_item_below_target_margin_is_flagged(day: date) -> None:
    """An item with a target gross-margin % set is flagged when actual < target.

    Chang sold at 120, cost 35 -> 70.83% margin. Target 75% -> flagged.
    """
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
                target_gross_margin_pct=D("75"),
            )
        ]
    )
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    assert margins[0].below_target is True


def test_item_meeting_target_margin_is_not_flagged(day: date) -> None:
    """Same item, target 70% -> 70.83% meets it -> not flagged."""
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
                target_gross_margin_pct=D("70"),
            )
        ]
    )
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    assert margins[0].below_target is False


def test_item_without_target_margin_is_never_flagged(day: date) -> None:
    """No target set -> never flagged, even at a thin margin."""
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
                # no target_gross_margin_pct
            )
        ]
    )
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    assert margins[0].below_target is False


# --- multi-input recipe: cafe latte -----------------------------------------


def test_multi_input_recipe_costs_each_ingredient(day: date) -> None:
    """Latte: 20g beans @ 2 THB/g + 200ml milk @ 0.025 THB/ml = 45 THB cost.

    Sold at 120 THB -> margin 75 THB (62.50%). Each ingredient is costed
    against its own SKU's current price in the cost book.
    """
    cost = CostBook(
        {
            "beans-arabica": (D("2"), date(2026, 6, 1)),
            "milk-fresh": (D("0.025"), date(2026, 6, 1)),
        }
    )
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="espresso-latte",
                name="Espresso Latte",
                segment=Segment.CAFE,
                ingredients=(
                    RecipeIngredient(sku_id="beans-arabica", quantity=D("20")),
                    RecipeIngredient(sku_id="milk-fresh", quantity=D("200")),
                ),
            )
        ]
    )
    # ADR-0007: clock-stamped CAFE — the recipe's segment is menu-shape only.
    sales = [
        Sale(
            item_id="espresso-latte",
            timestamp=day,
            sell_price=D("120"),
            segment=Segment.CAFE,
        )
    ]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    m = margins[0]
    assert m.cost_per_unit == D("45")
    assert m.gross_margin == D("75")
    assert m.gross_margin_pct == D("62.50")
    assert m.segment == Segment.CAFE


# --- keg-based recipe shape (ml of beer; yield math is slice 05) -------------


def test_keg_recipe_shape_supports_beer_input_in_ml(day: date) -> None:
    """A keg-based recipe expresses its beer input in ml of beer.

    The recipe shape must support referencing a keg as input with conversion
    to ml per item (acceptance criterion). The actual yield-vs-weighed math
    is slice 05; here we only require that a recipe can express "500 ml of
    chang-keg beer" and the engine costs it from the per-ml keg price.
    """
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
            )
        ]
    )
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    # Same numbers as the worked example: 500ml @ 0.07 = 35 cost, 85 margin.
    assert margins[0].cost_per_unit == D("35")


# --- full end-to-end: recipes + sales + approved purchases -------------------


def test_end_to_end_approved_purchases_drive_margins(
    day: date, chang_sku: Sku, beer_supplier: Supplier
) -> None:
    """Full slice-04 seam.

    Given:
      - Approved keg purchase (drives last_known_price for chang-keg).
      - Approved cafe purchases (beans, milk) driving their prices.
      - Recipes: chang-draft-500 -> 500ml chang-keg; espresso-latte ->
        20g beans + 200ml milk.
      - Sales: 1x chang + 2x latte on the day.

    The cost book is built from the approval book via ``CostBook.from_book``
    (no seeding). The margin engine then produces a per-item table whose
    numbers reconcile:

      chang: 120 - 35 = 85 margin  (70.83%), 1 unit
      latte: 240 - 90 = 150 margin (62.50%), 2 units
    """
    book = ApprovalBook()
    skus = {
        "chang-keg": chang_sku,
        "beans-arabica": Sku(sku_id="beans-arabica", name="Arabica beans", unit="g"),
        "milk-fresh": Sku(sku_id="milk-fresh", name="Fresh milk", unit="ml"),
    }
    # Three approved purchases drive three SKU prices through the receipt
    # pipeline (sum-check + approve -> last_known_price).
    _approve_purchase(
        supplier_id=beer_supplier.supplier_id,
        on=date(2026, 6, 1),
        sku_id="chang-keg",
        per_unit_qty=D("30000"),
        unit_price=D("0.07"),
        skus=skus,
        book=book,
    )
    _approve_purchase(
        supplier_id="phuket-coffee",
        on=date(2026, 6, 1),
        sku_id="beans-arabica",
        per_unit_qty=D("1000"),
        unit_price=D("2"),
        skus=skus,
        book=book,
    )
    _approve_purchase(
        supplier_id="phuket-dairy",
        on=date(2026, 6, 1),
        sku_id="milk-fresh",
        per_unit_qty=D("1000"),
        unit_price=D("0.025"),
        skus=skus,
        book=book,
    )

    cost = CostBook.from_book(book)
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
            ),
            Recipe(
                sku_id="espresso-latte",
                name="Espresso Latte",
                segment=Segment.CAFE,
                ingredients=(
                    RecipeIngredient(sku_id="beans-arabica", quantity=D("20")),
                    RecipeIngredient(sku_id="milk-fresh", quantity=D("200")),
                ),
            ),
        ]
    )
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
    ]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    by_item = {m.item_id: m for m in margins}
    assert by_item["chang-draft-500"].units_sold == 1
    assert by_item["chang-draft-500"].cost_per_unit == D("35")
    assert by_item["chang-draft-500"].gross_margin == D("85")
    assert by_item["chang-draft-500"].gross_margin_pct == D("70.83")
    assert by_item["espresso-latte"].units_sold == 2
    assert by_item["espresso-latte"].cost_per_unit == D("45")
    assert by_item["espresso-latte"].revenue == D("240")
    assert by_item["espresso-latte"].cogs == D("90")
    assert by_item["espresso-latte"].gross_margin == D("150")
    assert by_item["espresso-latte"].gross_margin_pct == D("62.50")


def test_reprice_flows_into_margin_without_recipe_edit(
    day: date, chang_sku: Sku, beer_supplier: Supplier
) -> None:
    """A re-approved keg price changes tomorrow's margin with no recipe edit.

    Same recipe (500ml chang-keg). June keg at 0.07/ml -> 35 cost, 85 margin.
    July keg at 0.08/ml -> 40 cost, 80 margin. The recipe never changes; only
    the approved purchase price does.
    """
    recipes = RecipeCatalog(
        [
            Recipe(
                sku_id="chang-draft-500",
                name="Chang Draft 500ml",
                segment=Segment.BAR,
                ingredients=(
                    RecipeIngredient(sku_id="chang-keg", quantity=D("500")),
                ),
            )
        ]
    )
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]

    # June price.
    book_june = ApprovalBook()
    _approve_purchase(
        supplier_id=beer_supplier.supplier_id,
        on=date(2026, 6, 1),
        sku_id="chang-keg",
        per_unit_qty=D("30000"),
        unit_price=D("0.07"),
        skus={chang_sku.sku_id: chang_sku},
        book=book_june,
    )
    margins_june = compute_item_margins(
        sales=sales, recipes=recipes, cost=CostBook.from_book(book_june), day=day
    )

    # July reprice.
    book_july = ApprovalBook()
    _approve_purchase(
        supplier_id=beer_supplier.supplier_id,
        on=date(2026, 7, 1),
        sku_id="chang-keg",
        per_unit_qty=D("30000"),
        unit_price=D("0.08"),
        skus={chang_sku.sku_id: chang_sku},
        book=book_july,
    )
    margins_july = compute_item_margins(
        sales=sales, recipes=recipes, cost=CostBook.from_book(book_july), day=day
    )

    assert margins_june[0].cost_per_unit == D("35")
    assert margins_june[0].gross_margin == D("85")
    assert margins_july[0].cost_per_unit == D("40")
    assert margins_july[0].gross_margin == D("80")


# --- derived costing: produced SKUs costed from their recipe (issue #36) -------
#
# Reversal of two Wave 1 decisions (recorded as ADR-0005): the engine now
# recurses into prep recipes, and a produced SKU's cost is *always* derived
# from its recipe — there is no leaf-price-wins branch. A prep with an
# unpriced purchasable leaf propagates ``unknown_price`` to every dish using
# it, exactly as a direct missing price does. As-of-date pricing composes by
# construction because the resolver runs against the as-of-date cost book.


def test_dish_containing_prep_is_costed_from_prep_recipe(day: date) -> None:
    """A dish whose recipe uses a prep is costed by recursing into the prep's
    own recipe down to purchasables — not by leaving the prep unpriced.

    Worked example (the ahi-sauce poke bowl the seed data exists to express).
    Ahi sauce: 100 g soy (0.05/g) + 24 g mirin (0.30/g) = 12.20 THB of inputs,
    yielding 61 g. Sauce per-gram cost = 12.20 / 61 = 0.20 THB/g. The bowl
    uses 25 g of sauce -> 25 * 0.20 = 5.00 THB of sauce in the dish.

    Before #36, the bowl was flagged ``unknown_price`` because the sauce SKU
    has no direct cost-book entry (the 0-of-13 evidence the ADR cites). The
    resolver recurses instead.
    """
    cost = CostBook(
        {
            "soy-sauce": (D("0.05"), date(2026, 6, 1)),
            "mirin": (D("0.30"), date(2026, 6, 1)),
        }
    )
    ahi_sauce = Recipe(
        sku_id="sauce-ahi",
        name="Ahi Sauce",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="soy-sauce", quantity=D("100")),
            RecipeIngredient(sku_id="mirin", quantity=D("24")),
        ),
        yield_qty=D("61"),
        prep=True,
    )
    poke_bowl = Recipe(
        sku_id="poke-bowl",
        name="Poke Bowl",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="sauce-ahi", quantity=D("25")),
        ),
    )
    recipes = RecipeCatalog([ahi_sauce, poke_bowl])
    sales = [Sale(item_id="poke-bowl", timestamp=day, sell_price=D("200"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    m = margins[0]
    assert m.unmapped is False
    assert m.unknown_price is False
    assert m.cost_per_unit == D("5")
    assert m.gross_margin == D("195")


def test_prep_cost_divides_by_yield_at_each_recipe_level(day: date) -> None:
    """A dish using 25 g of a sauce whose 61 g batch costs 12.20 THB carries
    (25 / 61) × 12.20 = 5.00 THB of sauce — not the whole batch cost.

    Issue #36 AC: "Each recursion level divides by the prep's yield in its
    own unit." This is the arithmetic that #34's unified yield exists to
    express and #36's recursion makes use of; before #34 every prep had
    ``yield_units: 1`` and 25 g of sauce was costed at 25 whole batches.
    """
    cost = CostBook(
        {
            "soy-sauce": (D("0.05"), date(2026, 6, 1)),
            "mirin": (D("0.30"), date(2026, 6, 1)),
        }
    )
    ahi_sauce = Recipe(
        sku_id="sauce-ahi",
        name="Ahi Sauce",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="soy-sauce", quantity=D("100")),
            RecipeIngredient(sku_id="mirin", quantity=D("24")),
        ),
        yield_qty=D("61"),  # 100 g + 24 g inputs, ~61 g reduced sauce batch
        prep=True,
    )
    # Two dishes use the same sauce at different quantities: each carries
    # its own fraction of the batch cost, not the whole batch.
    poke_bowl = Recipe(
        sku_id="poke-bowl",
        name="Poke Bowl (25 g sauce)",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="sauce-ahi", quantity=D("25")),
        ),
    )
    tacos = Recipe(
        sku_id="tacos",
        name="Tacos (10 g sauce)",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="sauce-ahi", quantity=D("10")),
        ),
    )
    recipes = RecipeCatalog([ahi_sauce, poke_bowl, tacos])
    sales = [
        Sale(item_id="poke-bowl", timestamp=day, sell_price=D("200")),
        Sale(item_id="tacos", timestamp=day, sell_price=D("160")),
    ]

    by_item = {
        m.item_id: m
        for m in compute_item_margins(sales=sales, recipes=recipes, cost=cost, day=day)
    }

    # 25/61 × 12.20 = 5.00 THB of sauce in the bowl.
    assert by_item["poke-bowl"].cost_per_unit == D("5")
    # 10/61 × 12.20 ≈ 2.00 THB of sauce in the tacos (exact: 1220/61 = 20).
    # Decimal: 10 * 12.20 / 61 = 122 / 61 = 2 exactly.
    assert by_item["tacos"].cost_per_unit == D("2")


def test_two_level_prep_nesting_costs_each_level(day: date) -> None:
    """A dish → prep A → prep B → purchasables chain costs through two
    recipe edges, each dividing by its own yield.

    Worked example (the loco-moco "sauce made of sauces" shape). A base
    mayo prep: 100 g oil (0.10/g) + 20 g egg (0.50/g) = 10 + 10 = 20 THB,
    yields 100 g -> 0.20/g. A spicy mayo prep uses 50 g of the base mayo
    (50 × 0.20 = 10 THB) + 5 g chili paste (4.00/g = 20 THB) = 30 THB,
    yields 50 g -> 0.60/g. The loco moco dish uses 30 g of the spicy mayo:
    30 × 0.60 = 18 THB of sauce in the dish.

    Each recursion level divides by its own yield in its own unit, so the
    sauce-on-sauce chain costs honestly per gram used rather than charging
    the dish for two whole batches (issue #36 AC).
    """
    cost = CostBook(
        {
            "oil": (D("0.10"), date(2026, 6, 1)),
            "egg": (D("0.50"), date(2026, 6, 1)),
            "chili-paste": (D("4.00"), date(2026, 6, 1)),
        }
    )
    base_mayo = Recipe(
        sku_id="prep-mayo-base",
        name="Base Mayo",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="oil", quantity=D("100")),
            RecipeIngredient(sku_id="egg", quantity=D("20")),
        ),
        yield_qty=D("100"),
        prep=True,
    )
    spicy_mayo = Recipe(
        sku_id="prep-spicy-mayo",
        name="Spicy Mayo",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="prep-mayo-base", quantity=D("50")),
            RecipeIngredient(sku_id="chili-paste", quantity=D("5")),
        ),
        yield_qty=D("50"),
        prep=True,
    )
    loco_moco = Recipe(
        sku_id="loco-moco",
        name="Loco Moco",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="prep-spicy-mayo", quantity=D("30")),
        ),
    )
    recipes = RecipeCatalog([base_mayo, spicy_mayo, loco_moco])
    sales = [Sale(item_id="loco-moco", timestamp=day, sell_price=D("220"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    m = margins[0]
    assert m.unmapped is False
    assert m.unknown_price is False
    assert m.cost_per_unit == D("18")


def test_produced_sku_with_stale_direct_price_uses_recipe(day: date) -> None:
    """A produced SKU's cost is *always* derived from its recipe — never the
    cost book — even if a direct price entry exists for it.

    Issue #36 reversal of the leaf-price-wins rule: the spreadsheet's
    prototype resolver honoured a direct price over a recipe (so a partner
    could price a sauce-as-bought even when a recipe existed). That branch
    is deliberately *not* carried over. The seed migration removes any
    pre-existing direct cost rows on produced SKUs; the cost editor rejects
    new ones. A stale direct price reaching the resolver is silently
    ignored rather than honoured — the recipe is the one source of truth.

    Worked example. Ahi sauce has a recipe (12.20 THB / 61 g = 0.20/g).
    A stale direct cost-book entry of 0.99/g exists for ``sauce-ahi`` from
    before #36. A dish using 25 g of sauce costs 25 × 0.20 = 5.00 THB
    (derived), not 25 × 0.99 = 24.75 THB (direct).
    """
    cost = CostBook(
        {
            "soy-sauce": (D("0.05"), date(2026, 6, 1)),
            "mirin": (D("0.30"), date(2026, 6, 1)),
            # Stale direct entry on a produced SKU — must be ignored.
            "sauce-ahi": (D("0.99"), date(2026, 6, 1)),
        }
    )
    ahi_sauce = Recipe(
        sku_id="sauce-ahi",
        name="Ahi Sauce",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="soy-sauce", quantity=D("100")),
            RecipeIngredient(sku_id="mirin", quantity=D("24")),
        ),
        yield_qty=D("61"),
        prep=True,
    )
    poke_bowl = Recipe(
        sku_id="poke-bowl",
        name="Poke Bowl",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="sauce-ahi", quantity=D("25")),
        ),
    )
    recipes = RecipeCatalog([ahi_sauce, poke_bowl])
    sales = [Sale(item_id="poke-bowl", timestamp=day, sell_price=D("200"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    # 25 × 0.20 = 5.00 (derived). A leaf-price-wins resolver would have
    # returned 25 × 0.99 = 24.75 instead.
    assert margins[0].cost_per_unit == D("5")


def test_unknown_price_propagates_through_prep_to_dish(day: date) -> None:
    """A prep whose recipe contains an unpriced purchasable makes every dish
    using it flag ``unknown_price`` — revenue stays surfaced, totals stay
    clean (issue #36 AC).

    Before #36, only the dish's *direct* ingredients were checked; a prep
    could hide an unpriced leaf inside its own recipe and the dish would
    silently zero-cost the prep line. The honesty rule now applies
    recursively: if any leaf needed to derive a prep's cost is missing, the
    prep itself is unpriceable, and so is any dish that uses it.

    Worked example. Ahi sauce has a recipe, but mirin has no price in the
    cost book. A poke bowl using 25 g of ahi sauce is flagged
    ``unknown_price`` (excluded from totals); a second dish whose recipe
    uses only purchasables is costed normally and counts in the totals.
    """
    cost = CostBook(
        {
            "soy-sauce": (D("0.05"), date(2026, 6, 1)),
            # mirin deliberately missing — the ahi-sauce prep's unpriced leaf
        }
    )
    ahi_sauce = Recipe(
        sku_id="sauce-ahi",
        name="Ahi Sauce",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="soy-sauce", quantity=D("100")),
            RecipeIngredient(sku_id="mirin", quantity=D("24")),
        ),
        yield_qty=D("61"),
        prep=True,
    )
    poke_bowl = Recipe(
        sku_id="poke-bowl",
        name="Poke Bowl",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="sauce-ahi", quantity=D("25")),
        ),
    )
    # A reliable dish that uses only purchasables directly — counts in totals.
    plain_rice = Recipe(
        sku_id="plain-rice",
        name="Plain Rice",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="rice", quantity=D("200")),
        ),
    )
    cost = CostBook(
        {
            "soy-sauce": (D("0.05"), date(2026, 6, 1)),
            "rice": (D("0.03"), date(2026, 6, 1)),
        }
    )
    recipes = RecipeCatalog([ahi_sauce, poke_bowl, plain_rice])
    sales = [
        Sale(item_id="poke-bowl", timestamp=day, sell_price=D("200")),
        Sale(item_id="plain-rice", timestamp=day, sell_price=D("50")),
    ]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )
    by_item = {m.item_id: m for m in margins}

    # The bowl is mapped but its prep's recipe contains an unpriced leaf.
    bowl = by_item["poke-bowl"]
    assert bowl.unmapped is False
    assert bowl.unknown_price is True
    assert bowl.excluded_from_totals is True
    # Revenue still surfaced (not silently dropped).
    assert bowl.revenue == D("200")

    # The reliable dish counts normally.
    assert by_item["plain-rice"].unknown_price is False
    assert by_item["plain-rice"].excluded_from_totals is False


def test_runtime_recipe_cycle_resolves_unpriceable(day: date) -> None:
    """A cycle in the recipe graph — however it arose — resolves as
    unpriceable rather than looping the resolver.

    Issue #36 AC: "A cycle encountered at costing time resolves as
    unpriceable rather than looping (defense in depth behind the save-time
    rejection)." The save-time guard (``find_recipe_cycle``, issue #35)
    should prevent cycles from entering the store; this is the engine-side
    fallback for a cycle that slipped past (a bad migration, a hand-edit
    to the YAML seed, a future import path).

    The test deliberately constructs a cycle the save-time guard would
    reject: A contains B and B contains A. The resolver must terminate and
    flag the dish ``unknown_price`` — never ``RecursionError``.

    Memoisation must not mask the cycle: the ``seen`` set is the per-path
    guard, and the memo is only written on the way back up (``seen`` is
    non-empty while a SKU is on its own stack). A buggy memo-first
    resolver would loop forever.
    """
    cost = CostBook(
        {
            "oil": (D("0.10"), date(2026, 6, 1)),
        }
    )
    sauce_a = Recipe(
        sku_id="sauce-a",
        name="Sauce A",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="oil", quantity=D("10")),
            RecipeIngredient(sku_id="sauce-b", quantity=D("5")),  # cycle!
        ),
        yield_qty=D("15"),
        prep=True,
    )
    sauce_b = Recipe(
        sku_id="sauce-b",
        name="Sauce B",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="sauce-a", quantity=D("5")),  # cycle!
        ),
        yield_qty=D("5"),
        prep=True,
    )
    dish = Recipe(
        sku_id="dish",
        name="Dish",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="sauce-a", quantity=D("10")),
        ),
    )
    recipes = RecipeCatalog([sauce_a, sauce_b, dish])
    sales = [Sale(item_id="dish", timestamp=day, sell_price=D("100"))]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )

    m = margins[0]
    assert m.unknown_price is True
    assert m.excluded_from_totals is True
    assert m.revenue == D("100")  # surfaced, not silently dropped


def test_prep_cost_consistent_across_many_dishes_using_it(day: date) -> None:
    """A prep used as an ingredient by several dishes costs the same per
    gram in each — and the same as it costs standalone.

    Issue #36 AC: "resolution is memoised per costing pass and cycle-safe."
    The user-observable property of memoisation is consistency: the prep's
    derived per-unit cost is computed once for the pass and reused, so
    every dish that uses it sees the same number. (An incorrectly
    memoised resolver — say, one that cached across costing passes with
    different cost books — would make these numbers disagree.)

    Worked example. A shared chili sauce prep (yield 50 g, batch 25 THB ->
    0.50/g) is used by three dishes at three different quantities. Each
    dish's sauce line is ``qty × 0.50``; the engine does not re-derive
    differently per dish.
    """
    cost = CostBook(
        {
            "chili": (D("0.50"), date(2026, 6, 1)),
        }
    )
    # Chili sauce: 50 g chili at 0.50/g = 25 THB batch, yields 50 g -> 0.50/g.
    chili_sauce = Recipe(
        sku_id="prep-chili-sauce",
        name="Chili Sauce",
        segment=Segment.CAFE,
        ingredients=(RecipeIngredient(sku_id="chili", quantity=D("50")),),
        yield_qty=D("50"),
        prep=True,
    )
    # Three dishes each use the sauce at a different quantity.
    dishes = [
        Recipe(
            sku_id=f"dish-{i}",
            name=f"Dish {i}",
            segment=Segment.CAFE,
            ingredients=(
                RecipeIngredient(
                    sku_id="prep-chili-sauce", quantity=D(qty)
                ),
            ),
        )
        for i, qty in enumerate((10, 15, 30), start=1)
    ]
    recipes = RecipeCatalog([chili_sauce, *dishes])
    sales = [
        Sale(item_id=f"dish-{i}", timestamp=day, sell_price=D("100"))
        for i in (1, 2, 3)
    ]

    margins = compute_item_margins(
        sales=sales, recipes=recipes, cost=cost, day=day
    )
    by_item = {m.item_id: m for m in margins}

    # 0.50/g in every dish: 10×0.50=5, 15×0.50=7.5, 30×0.50=15.
    assert by_item["dish-1"].cost_per_unit == D("5")
    assert by_item["dish-2"].cost_per_unit == D("7.5")
    assert by_item["dish-3"].cost_per_unit == D("15")
