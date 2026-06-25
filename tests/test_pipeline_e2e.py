"""End-to-end pipeline test seam (slice 01).

These tests are the contract for the accounting pipeline: seeded inputs in,
gross margin numbers out. They are deliberately readable as worked examples
rather than white-box assertions against internals.

Adding a new item to the seam requires only appending to the seeded sales and
recipes — no pipeline code changes. That is the seam-reuse acceptance criterion.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tangerine.pipeline import run
from tangerine.seeded import SeededSource
from tangerine.types import Ingredient, Recipe, Sale, Segment

D = Decimal


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def day() -> date:
    return date(2026, 6, 24)


def chang_recipe() -> Recipe:
    """500 ml of Chang draught at 0.07 THB/ml -> 35 THB cost per pour."""
    return Recipe(
        item_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(
            Ingredient(
                sku_id="chang-keg",
                name="Chang draught beer",
                unit="ml",
                quantity=D("500"),
                purchase_price=D("0.07"),
            ),
        ),
    )


def chang_sale(day: date) -> Sale:
    return Sale(
        item_id="chang-draft-500",
        timestamp=day,
        sell_price=D("120"),
    )


# --- the core seam test -----------------------------------------------------


def test_single_item_gross_margin(day: date) -> None:
    """Worked example: one Chang draft at 120 THB, cost 35 THB -> margin 85 THB."""
    source = SeededSource(sales=[chang_sale(day)], recipes=[chang_recipe()])

    result = run(source, day)

    # Daily roll-up
    assert result.day == day
    assert result.total_revenue == D("120")
    assert result.total_cogs == D("35")
    assert result.total_gross_margin == D("85")

    # Per-item line
    assert len(result.item_margins) == 1
    item = result.item_margins[0]
    assert item.item_id == "chang-draft-500"
    assert item.units_sold == 1
    assert item.revenue == D("120")
    assert item.cogs == D("35")
    assert item.gross_margin == D("85")
    assert item.segment == Segment.BAR


# --- seam reuse: a second item without touching pipeline code ----------------


def test_two_items_reuse_seam(day: date) -> None:
    """Adding a cafe item to the seam must not require any pipeline change.

    A 120 THB espresso latte (cost: 20 g beans @ 2 THB/g + 200 ml milk @
    0.025 THB/ml = 45 THB) sold the same day alongside the Chang draft.
    """
    latte_recipe = Recipe(
        item_id="espresso-latte",
        name="Espresso Latte",
        segment=Segment.CAFE,
        ingredients=(
            Ingredient(
                sku_id="beans-arabica",
                name="Arabica beans",
                unit="g",
                quantity=D("20"),
                purchase_price=D("2"),
            ),
            Ingredient(
                sku_id="milk-fresh",
                name="Fresh milk",
                unit="ml",
                quantity=D("200"),
                purchase_price=D("0.025"),
            ),
        ),
    )
    latte_sale = Sale(
        item_id="espresso-latte",
        timestamp=day,
        sell_price=D("120"),
    )

    source = SeededSource(
        sales=[chang_sale(day), latte_sale],
        recipes=[chang_recipe(), latte_recipe],
    )

    result = run(source, day)

    # Daily roll-up: 240 revenue, 35 + (40 + 5) = 80 cogs, 160 gross margin
    assert result.total_revenue == D("240")
    assert result.total_cogs == D("80")
    assert result.total_gross_margin == D("160")

    by_item = {im.item_id: im for im in result.item_margins}
    assert by_item["chang-draft-500"].gross_margin == D("85")
    assert by_item["espresso-latte"].gross_margin == D("75")
    assert by_item["espresso-latte"].segment == Segment.CAFE


# --- multi-unit + day filtering --------------------------------------------


def test_multi_unit_sale_aggregates_per_item(day: date) -> None:
    """Three Chang pours in a day roll up into one item margin line."""
    sales = [chang_sale(day) for _ in range(3)]
    source = SeededSource(sales=sales, recipes=[chang_recipe()])

    result = run(source, day)

    assert len(result.item_margins) == 1
    item = result.item_margins[0]
    assert item.units_sold == 3
    assert item.revenue == D("360")
    assert item.cogs == D("105")
    assert item.gross_margin == D("255")


def test_sales_on_other_days_are_excluded(day: date) -> None:
    """A sale timestamped another day must not appear in today's margin."""
    other_day = date(2026, 6, 23)
    source = SeededSource(
        sales=[chang_sale(other_day)],
        recipes=[chang_recipe()],
    )

    result = run(source, day)

    assert result.item_margins == ()
    assert result.total_revenue == D("0")
    assert result.total_gross_margin == D("0")


# --- unmapped item surfaces immediately (PRD user story 12) -----------------


def test_unmapped_item_raises(day: date) -> None:
    source = SeededSource(sales=[chang_sale(day)], recipes=[])

    with pytest.raises(Exception) as exc:
        run(source, day)

    assert "chang-draft-500" in str(exc.value)
