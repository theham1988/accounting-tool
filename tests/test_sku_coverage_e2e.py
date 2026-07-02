"""SKU + item coverage engine (Wave 1.5, Slice 2).

Per ADR-0003 and the Slice 2 issue, the partner's biggest pain is
invisibility: there is no way to see which SKUs are mapped, recipe-complete,
or priced without scanning YAML by eye. These tests read as worked examples
over the pure ``coverage`` engine — the same "feed synthetic data through the
real engine" style ``margin.py``'s tests use — before any web-layer or
storage wiring is exercised.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tangerine.cost import CostBook
from tangerine.coverage import build_item_coverage, build_sku_coverage, classify_sku, sku_health
from tangerine.loyverse.parser import parse_items_snapshot
from tangerine.loyverse.store import MenuItem
from tangerine.types import (
    Recipe,
    RecipeIngredient,
    Segment,
    SkuClassification,
    SkuHealth,
    SkuMapping,
    SkuRecord,
)

D = Decimal
_DAY = date(2026, 6, 1)


def _recipe(sku_id: str, *ingredient_sku_ids: str) -> Recipe:
    return Recipe(
        sku_id=sku_id,
        name=sku_id,
        segment=Segment.CAFE,
        ingredients=tuple(
            RecipeIngredient(sku_id=ing, quantity=D("1")) for ing in ingredient_sku_ids
        ),
    )


# --- AC: SKU view distinguishes active / prep-internal / dangling SKUs ------


def test_sku_with_a_mapping_is_active() -> None:
    """A SKU that at least one sold Loyverse item maps to is ``ACTIVE``.

    Worked example. ``espresso-latte`` is the SKU a real Loyverse item
    (``i-latte``) maps to — the ordinary "this SKU is on the menu" case.
    """
    recipes = [_recipe("espresso-latte", "beans-arabica", "milk-fresh")]
    mappings = [SkuMapping(item_id="i-latte", sku_id="espresso-latte")]

    assert (
        classify_sku("espresso-latte", recipes=recipes, mappings=mappings)
        == SkuClassification.ACTIVE
    )


def test_sku_used_only_as_an_ingredient_is_prep_internal() -> None:
    """A SKU no item is mapped to, but that another recipe consumes, is
    ``PREP_INTERNAL``.

    Worked example. ``beans-arabica`` is never sold directly (no mapping
    targets it) but ``espresso-latte``'s recipe consumes it as an ingredient
    — this is the "existing prep-* sub-recipes" case the issue names, and
    also covers a plain raw-material leaf SKU the same way.
    """
    recipes = [_recipe("espresso-latte", "beans-arabica", "milk-fresh")]
    mappings = [SkuMapping(item_id="i-latte", sku_id="espresso-latte")]

    assert (
        classify_sku("beans-arabica", recipes=recipes, mappings=mappings)
        == SkuClassification.PREP_INTERNAL
    )


def test_sku_neither_sold_nor_used_is_dangling() -> None:
    """A SKU with no mapping and no recipe references it at all is
    ``DANGLING`` — likely a mistake (per the issue).

    Worked example. ``orphan-sku`` exists in the ``skus`` table (perhaps a
    discontinued item's leftover row) but nothing sells it and nothing's
    recipe consumes it.
    """
    recipes = [_recipe("espresso-latte", "beans-arabica", "milk-fresh")]
    mappings = [SkuMapping(item_id="i-latte", sku_id="espresso-latte")]

    assert (
        classify_sku("orphan-sku", recipes=recipes, mappings=mappings)
        == SkuClassification.DANGLING
    )


def test_active_wins_over_prep_internal_when_both_apply() -> None:
    """A SKU that is both mapped AND used as another recipe's ingredient
    (e.g. a sub-recipe also sold as a standalone item) classifies ``ACTIVE``
    — being sold is the stronger signal for the partner's workspace.
    """
    recipes = [
        _recipe("latte-concentrate", "coffee-beans-house"),
        _recipe("iced-latte", "latte-concentrate", "milk-fresh"),
    ]
    mappings = [
        SkuMapping(item_id="i-concentrate-cup", sku_id="latte-concentrate"),
        SkuMapping(item_id="i-iced-latte", sku_id="iced-latte"),
    ]

    assert (
        classify_sku("latte-concentrate", recipes=recipes, mappings=mappings)
        == SkuClassification.ACTIVE
    )


# --- AC: health indicators (green/yellow/red) distinguish complete/partial/broken SKUs


def test_dangling_sku_is_always_red() -> None:
    """A dangling SKU is red regardless of what its (absent) recipe/cost say."""
    assert (
        sku_health(
            "orphan-sku",
            recipe=None,
            cost=CostBook(),
            classification=SkuClassification.DANGLING,
        )
        == SkuHealth.RED
    )


def test_recipe_with_every_ingredient_priced_is_green() -> None:
    """A recipe SKU whose every ingredient has a cost entry is green.

    Worked example. ``espresso-latte`` needs beans + milk; both are priced.
    """
    recipe = _recipe("espresso-latte", "beans-arabica", "milk-fresh")
    cost = CostBook({"beans-arabica": (D("2"), _DAY), "milk-fresh": (D("0.025"), _DAY)})

    assert (
        sku_health(
            "espresso-latte", recipe=recipe, cost=cost, classification=SkuClassification.ACTIVE
        )
        == SkuHealth.GREEN
    )


def test_recipe_with_one_unpriced_ingredient_is_yellow() -> None:
    """A recipe SKU with at least one (but not all) unpriced ingredients is
    yellow — partial, not broken.
    """
    recipe = _recipe("espresso-latte", "beans-arabica", "milk-fresh")
    cost = CostBook({"beans-arabica": (D("2"), _DAY)})  # milk-fresh unpriced

    assert (
        sku_health(
            "espresso-latte", recipe=recipe, cost=cost, classification=SkuClassification.ACTIVE
        )
        == SkuHealth.YELLOW
    )


def test_recipe_with_no_ingredient_priced_is_yellow_not_red() -> None:
    """A recipe that exists but has no priced ingredient at all is still
    yellow, per the issue's own example ("recipe exists but no ingredient
    has a cost") — the recipe itself is not broken, just entirely unpriced.
    """
    recipe = _recipe("espresso-latte", "beans-arabica", "milk-fresh")

    assert (
        sku_health(
            "espresso-latte", recipe=recipe, cost=CostBook(), classification=SkuClassification.ACTIVE
        )
        == SkuHealth.YELLOW
    )


def test_recipe_with_zero_ingredients_is_red() -> None:
    """A recipe SKU with no ingredients at all is broken — red, not yellow."""
    recipe = _recipe("mystery-item")  # no ingredients

    assert (
        sku_health(
            "mystery-item", recipe=recipe, cost=CostBook(), classification=SkuClassification.ACTIVE
        )
        == SkuHealth.RED
    )


def test_leaf_ingredient_with_a_price_is_green() -> None:
    """A raw-material SKU with no recipe of its own is green when priced.

    Worked example. ``beans-arabica`` is never a recipe's own output — it is
    green as soon as it has a cost entry.
    """
    cost = CostBook({"beans-arabica": (D("2"), _DAY)})

    assert (
        sku_health(
            "beans-arabica", recipe=None, cost=cost, classification=SkuClassification.PREP_INTERNAL
        )
        == SkuHealth.GREEN
    )


def test_leaf_ingredient_with_no_price_is_red() -> None:
    """A raw-material SKU with no recipe and no cost entry is broken (red) —
    any recipe consuming it cannot be honestly costed.
    """
    assert (
        sku_health(
            "unpriced-leaf", recipe=None, cost=CostBook(), classification=SkuClassification.PREP_INTERNAL
        )
        == SkuHealth.RED
    )


def test_active_sku_with_no_recipe_at_all_is_red() -> None:
    """A SKU that is sold (mapped) but has no recipe defined is broken — the
    partner's "unmapped or broken" red case from the issue.
    """
    assert (
        sku_health(
            "phantom-sku", recipe=None, cost=CostBook(), classification=SkuClassification.ACTIVE
        )
        == SkuHealth.RED
    )


# --- AC: `GET /skus` renders one row per SKU with the full coverage picture -


def test_build_sku_coverage_returns_one_row_per_sku_with_full_picture() -> None:
    """``build_sku_coverage`` returns one row per SKU, each carrying identity,
    classification, health, mapping count, recipe completeness, and derived
    cost — the exact columns the SKU view table needs.

    Worked example. ``espresso-latte`` is sold (one mapping), fully recipe'd
    and priced -> active/green with a derived cost. ``beans-arabica`` is a
    priced leaf ingredient the latte consumes -> prep-internal/green.
    ``orphan-sku`` sits in the table unused -> dangling/red.
    """
    skus = [
        SkuRecord(sku_id="espresso-latte", name="Espresso Latte", segment=Segment.CAFE, unit=None),
        SkuRecord(sku_id="beans-arabica", name="beans-arabica", segment=None, unit="g"),
        SkuRecord(sku_id="milk-fresh", name="milk-fresh", segment=None, unit="ml"),
        SkuRecord(sku_id="orphan-sku", name="Orphan", segment=None, unit=None),
    ]
    recipes = [_recipe("espresso-latte", "beans-arabica", "milk-fresh")]
    mappings = [SkuMapping(item_id="i-latte", sku_id="espresso-latte")]
    cost = CostBook({"beans-arabica": (D("2"), _DAY), "milk-fresh": (D("0.025"), _DAY)})

    rows = build_sku_coverage(skus=skus, recipes=recipes, mappings=mappings, cost=cost)

    by_id = {r.sku_id: r for r in rows}
    assert set(by_id) == {"espresso-latte", "beans-arabica", "milk-fresh", "orphan-sku"}

    latte = by_id["espresso-latte"]
    assert latte.classification == SkuClassification.ACTIVE
    assert latte.health == SkuHealth.GREEN
    assert latte.mapped_item_count == 1
    assert latte.has_recipe is True
    assert latte.ingredient_count == 2
    assert latte.priced_ingredient_count == 2
    # 20g beans (unused here; ingredients are quantity 1 in this fixture) — the
    # important assertion is that a per-unit cost was actually derived.
    assert latte.cost_per_unit == D("2") + D("0.025")

    beans = by_id["beans-arabica"]
    assert beans.classification == SkuClassification.PREP_INTERNAL
    assert beans.health == SkuHealth.GREEN
    assert beans.mapped_item_count == 0
    assert beans.has_recipe is False
    assert beans.cost_per_unit == D("2")

    orphan = by_id["orphan-sku"]
    assert orphan.classification == SkuClassification.DANGLING
    assert orphan.health == SkuHealth.RED
    assert orphan.cost_per_unit is None


def test_build_sku_coverage_is_sorted_by_sku_id() -> None:
    """Rows come back sorted by ``sku_id`` for deterministic rendering."""
    skus = [
        SkuRecord(sku_id="zebra", name="zebra", segment=None, unit=None),
        SkuRecord(sku_id="apple", name="apple", segment=None, unit=None),
    ]

    rows = build_sku_coverage(skus=skus, recipes=[], mappings=[], cost=CostBook())

    assert [r.sku_id for r in rows] == ["apple", "zebra"]


# --- AC: `GET /items` renders one row per Loyverse item, unmapped bubble to top


def _menu_item(item_id: str, name: str, price: str, segment: Segment = Segment.CAFE) -> MenuItem:
    return MenuItem(item_id=item_id, name=name, sell_price=D(price), segment=segment)


def test_mapped_item_carries_its_skus_health_and_derived_margin() -> None:
    """A mapped item's row shows the SKU it resolves to, that SKU's health,
    and a derived gross margin (sell price minus the SKU's per-unit cost).

    Worked example. ``i-latte`` sells at 120 and maps to ``espresso-latte``,
    fully priced at 2.025 THB/unit (beans + milk here both quantity 1) ->
    margin 117.975.
    """
    skus = [SkuRecord(sku_id="espresso-latte", name="Espresso Latte", segment=Segment.CAFE, unit=None)]
    recipes = [_recipe("espresso-latte", "beans-arabica", "milk-fresh")]
    mappings = [SkuMapping(item_id="i-latte", sku_id="espresso-latte")]
    cost = CostBook({"beans-arabica": (D("2"), _DAY), "milk-fresh": (D("0.025"), _DAY)})
    menu = {"i-latte": _menu_item("i-latte", "Espresso Latte", "120")}

    rows = build_item_coverage(menu=menu, skus=skus, recipes=recipes, mappings=mappings, cost=cost)

    assert len(rows) == 1
    row = rows[0]
    assert row.item_id == "i-latte"
    assert row.mapped_sku_id == "espresso-latte"
    assert row.sku_health == SkuHealth.GREEN
    assert row.cost_per_unit == D("2.025")
    assert row.gross_margin == D("117.975")


def test_unmapped_item_has_no_sku_health_or_margin() -> None:
    """An unmapped item's row has no SKU, health, cost, or margin — just its
    Loyverse identity and price, visually distinct from a mapped row.
    """
    menu = {"i-mystery": _menu_item("i-mystery", "Mystery Soda", "60")}

    rows = build_item_coverage(menu=menu, skus=[], recipes=[], mappings=[], cost=CostBook())

    assert len(rows) == 1
    row = rows[0]
    assert row.mapped_sku_id is None
    assert row.sku_health is None
    assert row.cost_per_unit is None
    assert row.gross_margin is None


def test_synced_menu_rows_join_to_variant_sku_mappings() -> None:
    """A menu synced from a real-shaped Loyverse payload joins to mappings.

    Regression guard. Loyverse ``/items`` entries are keyed by an item UUID,
    but receipt lines — and therefore ``config/recipes.yaml`` mappings — carry
    the *variant SKU* (e.g. ``10042``). The parser must key menu rows by that
    SKU, or ``build_item_coverage`` marks every genuinely mapped item as
    unmapped and the daily review's ``/items?item=<sku>`` deep links land on
    an empty table.
    """
    snapshot = parse_items_snapshot(
        {
            "items": [
                {
                    "id": "d5fe0da6-44b3-4633-9915-e9dc5118cbfc",
                    "item_name": "Espresso",
                    "category_id": "cat-cafe",
                    "variants": [
                        {"variant_id": "v-1", "option1_value": "Espresso", "sku": "10042", "default_price": 70}
                    ],
                }
            ]
        }
    )
    menu = {mi.item_id: mi for mi in snapshot.items}
    skus = [SkuRecord(sku_id="espresso", name="Espresso", segment=Segment.CAFE, unit=None)]
    recipes = [_recipe("espresso", "beans-arabica")]
    mappings = [SkuMapping(item_id="10042", sku_id="espresso")]
    cost = CostBook({"beans-arabica": (D("2"), _DAY)})

    rows = build_item_coverage(menu=menu, skus=skus, recipes=recipes, mappings=mappings, cost=cost)

    assert len(rows) == 1
    row = rows[0]
    assert row.item_id == "10042"
    assert row.mapped_sku_id == "espresso"
    assert row.sku_health == SkuHealth.GREEN


def test_unmapped_and_broken_items_bubble_to_the_top() -> None:
    """Rows sort worst-first: unmapped, then red, then yellow, then green —
    so the partner's "map everything" audit starts at the top of the page.

    Worked example. Four items, one of each health tier, given in
    best-to-worst insertion order; the rendered order must be reversed.
    """
    skus = [
        SkuRecord(sku_id="green-sku", name="Green", segment=Segment.CAFE, unit=None),
        SkuRecord(sku_id="yellow-sku", name="Yellow", segment=Segment.CAFE, unit=None),
        SkuRecord(sku_id="red-sku", name="Red", segment=Segment.CAFE, unit=None),
    ]
    recipes = [
        _recipe("green-sku", "priced-ingredient"),
        _recipe("yellow-sku", "unpriced-ingredient"),
        # red-sku deliberately has no recipe at all -> broken/red.
    ]
    mappings = [
        SkuMapping(item_id="i-green", sku_id="green-sku"),
        SkuMapping(item_id="i-yellow", sku_id="yellow-sku"),
        SkuMapping(item_id="i-red", sku_id="red-sku"),
    ]
    cost = CostBook({"priced-ingredient": (D("1"), _DAY)})
    menu = {
        "i-green": _menu_item("i-green", "Green Item", "100"),
        "i-yellow": _menu_item("i-yellow", "Yellow Item", "100"),
        "i-red": _menu_item("i-red", "Red Item", "100"),
        "i-unmapped": _menu_item("i-unmapped", "Unmapped Item", "100"),
    }

    rows = build_item_coverage(menu=menu, skus=skus, recipes=recipes, mappings=mappings, cost=cost)

    assert [r.item_id for r in rows] == ["i-unmapped", "i-red", "i-yellow", "i-green"]
