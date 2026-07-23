"""Issue #73 — pure-clock segmentation (ADR-0007).

Reverses slice 07's rule for **revenue splitting**. The slice-07 rule was
"recipe's segment wins when a sale is mapped; the shift-stamped segment is
the fallback only for unmapped sales". Issue #65 locked in a pure-clock
rule instead: a sale's segment for revenue splitting is decided **entirely
by its local timestamp** (``[8, 17)`` = cafe, else bar; half-open at 17:00).

Two consequences, both pinned here:

  1. ``recipe.segment`` **no longer drives** revenue segmentation. A beer
     (a bar recipe) sold at 2pm is *cafe* by the clock; a cappuccino (a
     cafe recipe) sold at 7pm is *bar*. ``recipe.segment`` is now a menu-
     shape fact only — it still tags the recipe row for ``/items``,
     ``/skus``, and ``coverage.py``, but it does not move revenue.
  2. Per-segment contribution margin is **mapped revenue − mapped COGS
     only**; unmapped revenue still flows into a segment card by clock,
     but it shows as a flagged line and is excluded from the card's CM
     (the slice-07 "clean and defensible" rule, now per-card).

These are pure tests over the public interfaces
(``segment_of_sale``, ``compute_daily_margin``, ``build_period_review``).
The clock-stamped ``sale.segment`` is the parser's output (post-#66, in
local time); tests construct it directly to pin the rule.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from tangerine.cost import CostBook
from tangerine.margin import compute_item_margins
from tangerine.recipes import RecipeCatalog
from tangerine.segments import segment_of_sale
from tangerine.seeded import SeededSource
from tangerine.types import Recipe, RecipeIngredient, Sale, Segment

from tangerine.margin import compute_daily_margin


# --- shared fixtures --------------------------------------------------------


def _chang_recipe() -> Recipe:
    """500 ml Chang draught, *bar* segment by menu shape. Sold at ฿120."""
    return Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )


def _latte_recipe() -> Recipe:
    """Espresso latte, *cafe* segment by menu shape. Sold at ฿80."""
    return Recipe(
        sku_id="espresso-latte",
        name="Espresso Latte",
        segment=Segment.CAFE,
        ingredients=(
            RecipeIngredient(sku_id="beans-arabica", quantity=D("20")),
            RecipeIngredient(sku_id="milk-fresh", quantity=D("200")),
        ),
    )


def _cost() -> CostBook:
    return CostBook(
        {
            "chang-keg": (D("0.07"), date(2026, 6, 1)),
            "beans-arabica": (D("2"), date(2026, 6, 1)),
            "milk-fresh": (D("0.025"), date(2026, 6, 1)),
        }
    )


# --- AC 1: the clock wins over the recipe -----------------------------------


def test_beer_sold_in_cafe_hour_is_cafe_by_clock() -> None:
    """A *bar* recipe's sale, made in the cafe window, is cafe for revenue.

    This is the headline reversal of slice 07. Pre-#73 the recipe's
    ``segment=bar`` won and the sale rolled into the bar/Taps card. Under
    pure-clock the 14:00 local timestamp decides — the sale is cafe.
    """
    day = date(2026, 6, 24)
    sale = Sale(
        item_id="chang-draft-500",
        timestamp=day,
        sell_price=D("120"),
        segment=Segment.CAFE,  # clock-stamped by the parser: 14:00 local
    )

    assert segment_of_sale(sale, recipe=_chang_recipe()) == Segment.CAFE


def test_cappuccino_sold_in_bar_hour_is_bar_by_clock() -> None:
    """A *cafe* recipe's sale, made in the bar window, is bar for revenue.

    The mirror of the beer test. Pre-#73 the recipe's ``segment=cafe``
    won. Under pure-clock the 19:00 local timestamp decides — the sale is
    bar, so the cappuccino's revenue lands on the Taps card.
    """
    day = date(2026, 6, 24)
    sale = Sale(
        item_id="espresso-latte",
        timestamp=day,
        sell_price=D("80"),
        segment=Segment.BAR,  # clock-stamped by the parser: 19:00 local
    )

    assert segment_of_sale(sale, recipe=_latte_recipe()) == Segment.BAR


# --- AC 2: an item sold in both shifts splits into two ItemMargin rows ------


def test_item_sold_in_both_shifts_produces_two_per_segment_rows() -> None:
    """An item that sells in both the cafe and bar windows on the same day
    produces **two** ``ItemMargin`` rows — one per segment — each carrying
    only the units/revenue sold in its window.

    Worked example: 2x Chang draught at 14:00 (cafe) + 1x Chang at 19:00
    (bar), all at ฿120. Pre-#73 the engine aggregated by ``item_id`` alone,
    so the first sale's recipe-segment (bar) won for all three units and
    the cafe card saw none of this item's revenue. Under pure-clock the
    cafe card sees 2 units / ฿240, the bar card sees 1 unit / ฿120.
    """
    day = date(2026, 6, 24)
    sales = [
        Sale(
            item_id="chang-draft-500",
            timestamp=day,
            sell_price=D("120"),
            quantity=2,
            segment=Segment.CAFE,  # 14:00 local — cafe window
        ),
        Sale(
            item_id="chang-draft-500",
            timestamp=day,
            sell_price=D("120"),
            quantity=1,
            segment=Segment.BAR,  # 19:00 local — bar window
        ),
    ]

    rows = compute_item_margins(
        sales=sales,
        recipes=RecipeCatalog([_chang_recipe()]),
        cost=_cost(),
        day=day,
    )

    by_segment = {im.segment: im for im in rows}
    assert set(by_segment) == {Segment.CAFE, Segment.BAR}
    # Same recipe/price in both branches, so cost_per_unit and sell_price
    # match; only units and revenue split.
    cafe_row = by_segment[Segment.CAFE]
    assert cafe_row.units_sold == 2
    assert cafe_row.revenue == D("240")
    bar_row = by_segment[Segment.BAR]
    assert bar_row.units_sold == 1
    assert bar_row.revenue == D("120")


def test_daily_segment_margins_split_revenue_purely_by_clock() -> None:
    """A mapped sale's revenue lands in its **clock** segment, not its
    recipe's segment, on the daily segment cards.

    Worked example: 1x Chang (bar recipe) sold at 14:00 local (cafe window)
    for ฿120. Pre-#73 this rolled into the bar card via ``recipe.segment``.
    Under pure-clock the ฿120 lands on the cafe card; the bar card carries
    zero revenue for the day. COGS follows the revenue (cost ฿35 stays with
    the sale's cafe bucket), so the cafe CM is 85 and the bar CM is 0.
    """
    day = date(2026, 6, 24)
    sales = [
        Sale(
            item_id="chang-draft-500",
            timestamp=day,
            sell_price=D("120"),
            segment=Segment.CAFE,  # 14:00 local — cafe window, despite bar recipe
        ),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    by_seg = {sm.segment: sm for sm in result.segment_margins}
    assert by_seg[Segment.CAFE].revenue == D("120")
    assert by_seg[Segment.CAFE].variable_costs == D("35")
    assert by_seg[Segment.CAFE].contribution_margin == D("85")
    assert by_seg[Segment.BAR].revenue == D("0")
    assert by_seg[Segment.BAR].contribution_margin == D("0")


# --- AC 3: per-segment flagged revenue (unmapped, excluded from CM) --------


def test_unmapped_revenue_lands_in_its_clock_segment_as_flagged_revenue() -> None:
    """An unmapped sale's revenue lands in its **clock** segment's
    ``flagged_revenue``, not in the segment's CM revenue.

    The honest-labelling principle from #64 applied to the card shape (issue
    #73): every sale flows into a segment card by clock (mapped or not), but
    unmapped revenue shows as a **flagged line** on that card, excluded from
    the card's CM. Pre-#73 unmapped revenue sat only in the daily-level
    ``flagged_revenue`` and no per-segment attribution existed.

    Worked example: 1 mapped Chang at ฿120 + 1 unmapped 'mystery' at ฿90
    sold at 10am (cafe window). The cafe card shows ฿90 flagged (excluded
    from CM) plus ฿0 of reliable revenue (the Chang was sold at 19:00 so
    it is bar). The bar card shows ฿120 reliable revenue, ฿85 CM, and ฿0
    flagged.
    """
    day = date(2026, 6, 24)
    sales = [
        Sale(
            item_id="chang-draft-500",
            timestamp=day,
            sell_price=D("120"),
            segment=Segment.BAR,  # 19:00 local — bar window
        ),
        Sale(
            item_id="mystery",
            timestamp=day,
            sell_price=D("90"),
            segment=Segment.CAFE,  # 10:00 local — cafe window, unmapped
        ),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    by_seg = {sm.segment: sm for sm in result.segment_margins}
    # The cafe card carries the unmapped sale as flagged revenue, excluded
    # from the card's CM (which stays clean: 0 reliable revenue, 0 CM).
    assert by_seg[Segment.CAFE].flagged_revenue == D("90")
    assert by_seg[Segment.CAFE].revenue == D("0")
    assert by_seg[Segment.CAFE].contribution_margin == D("0")
    # The bar card has the mapped Chang — no flagged revenue on this card.
    assert by_seg[Segment.BAR].flagged_revenue == D("0")
    assert by_seg[Segment.BAR].revenue == D("120")
    assert by_seg[Segment.BAR].contribution_margin == D("85")
    # The daily-level flagged revenue total is unchanged: ฿90.
    assert result.flagged_revenue == D("90")
