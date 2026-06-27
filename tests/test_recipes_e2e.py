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
    compute_item_margins,
    recipe_cost,
    recipe_cost_per_unit,
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


def test_recipe_has_yield_and_is_defined_per_sku(day: date) -> None:
    """A recipe is defined per SKU (the master item it produces) and carries
    a yield (how many saleable units one execution produces).

    Spec (issue 04 AC): "Recipe schema exists with inputs (SKU + qty) and
    yield". A 1L Chang pitcher recipe takes 1000ml of beer and yields 2
    units (two 500ml pours), so cost-per-unit = 1000 * 0.07 / 2 = 35 THB.
    """
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipe = Recipe(
        sku_id="chang-pitcher-1l",
        name="Chang Pitcher 1L",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("1000")),),
        yield_units=2,
    )
    recipes = RecipeCatalog([recipe])

    # 1000ml @ 0.07 = 70 THB input cost, yields 2 units -> 35 THB per unit.
    assert recipe_cost(recipe, cost) == D("70")
    assert recipe_cost_per_unit(recipe, cost) == D("35")


def test_recipe_default_yield_is_one(day: date) -> None:
    """A single-pour recipe (the common case) implicitly yields 1 unit."""
    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    recipe = Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )
    assert recipe.yield_units == 1
    assert recipe_cost_per_unit(recipe, cost) == D("35")


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

    assert recipe_cost(recipe, cost) == D("35")


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


def test_flagged_rows_excluded_from_daily_rollup(day: date) -> None:
    """Unmapped/unknown-price rows do not inflate the daily margin totals.

    One mapped Chang (120 revenue, 85 margin) and one unmapped mystery item
    (90 revenue). The daily roll-up totals only the mapped row: 120 revenue,
    85 margin. The unmapped item's 90 revenue is surfaced separately as
    ``flagged_revenue`` so it is visible, not silently booked as margin.
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

    assert result.total_revenue == D("120")  # only the mapped item
    assert result.total_gross_margin == D("85")
    assert result.flagged_revenue == D("90")  # unmapped item's revenue, surfaced


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
    sales = [Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120"))]

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
