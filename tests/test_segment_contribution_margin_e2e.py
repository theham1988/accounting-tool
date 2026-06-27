"""End-to-end segment tagging + contribution margin test seam (slice 07).

Per the PRD testing rules these tests read as worked examples: synthetic sales,
recipes, and costs split across cafe and bar segments go in; per-segment
contribution margin numbers and red flags come out.

Segment rules (docs/issues/07-segment-tagging-and-contribution-margin.md +
PRD "Segmentation"):

- Every recipe carries a ``cafe`` or ``bar`` segment (the default source: the
  Loyverse category). Sale segment is the recipe's segment, with shift-timestamp
  as fallback for unmapped sales (8am–5pm cafe, 5pm–10pm bar).
- Per segment, per period: revenue, variable costs (= COGS today; direct labor
  is "if tracked" and not tracked yet), contribution margin = revenue −
  variable costs.
- Fixed costs are NOT allocated to segments (entity-level only, slice 08).
  The segment CM numbers therefore never include fixed costs.
- A segment whose CM < 0 is flagged red.

Flagged rows (unmapped / unknown-price) are excluded from segment CM for the
same reason they are excluded from the daily roll-up: their COGS is unknown, so
booking their revenue as CM would over-state the segment's profitability.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tangerine.cost import CostBook
from tangerine.margin import compute_daily_margin, compute_period_segment_margins
from tangerine.seeded import SeededSource
from tangerine.segments import segment_for_timestamp, segment_of_sale
from tangerine.types import Recipe, RecipeIngredient, Sale, Segment
from tangerine.loyverse.parser import (
    parse_items_snapshot,
    parse_receipts_to_sales,
)
from tangerine.loyverse.store import CAFE_CATEGORY_ID

D = Decimal


# --- shared fixtures --------------------------------------------------------


@pytest.fixture
def day() -> date:
    return date(2026, 6, 24)


def _chang_recipe() -> Recipe:
    """500 ml Chang draught, bar segment, cost 35 THB/pour at 0.07 THB/ml."""
    return Recipe(
        sku_id="chang-draft-500",
        name="Chang Draft 500ml",
        segment=Segment.BAR,
        ingredients=(RecipeIngredient(sku_id="chang-keg", quantity=D("500")),),
    )


def _latte_recipe() -> Recipe:
    """Espresso latte, cafe segment, cost 45 THB (20g beans + 200ml milk)."""
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


# --- AC 1: items, recipes, and transactions carry a segment tag -------------


def test_recipe_carries_segment_tag() -> None:
    """A recipe carries an explicit ``cafe`` or ``bar`` segment (PRD: 'each
    transaction, recipe, and item is tagged')."""
    assert _chang_recipe().segment == Segment.BAR
    assert _latte_recipe().segment == Segment.CAFE


def test_item_margin_row_carries_recipe_segment(day: date) -> None:
    """A sold item's margin row inherits the recipe's segment (default source
    is the Loyverse category, carried via the recipe)."""
    from tangerine.cost import CostBook
    from tangerine.margin import compute_item_margins
    from tangerine.recipes import RecipeCatalog

    cost = CostBook({"chang-keg": (D("0.07"), date(2026, 6, 1))})
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]

    margins = compute_item_margins(
        sales=sales, recipes=RecipeCatalog([_chang_recipe()]), cost=cost, day=day
    )

    assert margins[0].segment == Segment.BAR


def test_loyverse_item_segment_tagged_from_category() -> None:
    """The Loyverse menu parser tags each item cafe/bar from its category
    (default tagging source per issue 07)."""
    snapshot = parse_items_snapshot(
        {
            "items": [
                {
                    "id": "i-bar",
                    "item_name": "Chang Draft",
                    "category_id": "cat-bar",
                    "variants": [{"id": "v", "name": "Chang", "sku": "c", "price": 120}],
                },
                {
                    "id": "i-cafe",
                    "item_name": "Latte",
                    "category_id": CAFE_CATEGORY_ID,
                    "variants": [{"id": "v", "name": "Latte", "sku": "l", "price": 80}],
                },
            ]
        }
    )

    by_id = {mi.item_id: mi for mi in snapshot.items}
    assert by_id["i-bar"].segment == Segment.BAR
    assert by_id["i-cafe"].segment == Segment.CAFE


# --- AC 2: shift-timestamp fallback ----------------------------------------


def test_segment_for_timestamp_cafe_window() -> None:
    """The shift-timestamp fallback tags 8am–5pm (exclusive) as cafe."""
    for hour in (8, 12, 16):
        ts = datetime(2026, 6, 24, hour, 0, tzinfo=timezone.utc)
        assert segment_for_timestamp(ts) == Segment.CAFE, hour


def test_segment_for_timestamp_bar_window() -> None:
    """The shift-timestamp fallback tags 5pm–10pm as bar."""
    for hour in (17, 18, 21):
        ts = datetime(2026, 6, 24, hour, 0, tzinfo=timezone.utc)
        assert segment_for_timestamp(ts) == Segment.BAR, hour


def test_segment_for_timestamp_outside_hours_defaults_bar() -> None:
    """Outside both shift windows (e.g. 23:00, 02:00, 06:00) defaults to bar.

    The bar is the late shift; anything outside the cafe window is treated as
    bar rather than dropped, so an after-hours sale is never lost. This is a
    documented default, not a third segment.
    """
    for hour in (23, 2, 6):
        ts = datetime(2026, 6, 24, hour, 0, tzinfo=timezone.utc)
        assert segment_for_timestamp(ts) == Segment.BAR, hour


def test_segment_of_unmapped_sale_uses_shift_fallback(day: date) -> None:
    """An unmapped sale (no recipe → no category-derived segment) is tagged
    from its shift-stamped segment, which the Loyverse parser resolves at the
    sync boundary from the transaction timestamp (8am–5pm cafe, else bar).

    A pre-resolved ``sale.segment`` (the parser's output) is the fallback the
    margin engine uses when no recipe is mapped. Here an unmapped cafe-hour
    sale is stamped cafe, and a bar-hour sale is stamped bar.
    """
    cafe_sale = Sale(
        item_id="mystery",
        timestamp=day,
        sell_price=D("50"),
        segment=Segment.CAFE,
    )
    bar_sale = Sale(
        item_id="mystery",
        timestamp=day,
        sell_price=D("50"),
        segment=Segment.BAR,
    )

    assert segment_of_sale(cafe_sale, recipe=None) == Segment.CAFE
    assert segment_of_sale(bar_sale, recipe=None) == Segment.BAR


def test_segment_of_mapped_sale_uses_recipe_segment(day: date) -> None:
    """A mapped sale takes its segment from the recipe (the category default),
    ignoring any shift-derived stamp on the sale — so a keg beer sold during
    the cafe hour is still bar."""
    sale = Sale(
        item_id="chang-draft-500",
        timestamp=day,
        sell_price=D("120"),
        segment=Segment.CAFE,  # would be wrong for a mapped bar item
    )
    assert segment_of_sale(sale, recipe=_chang_recipe()) == Segment.BAR


# --- AC 3 + 4: per-segment revenue, variable costs, CM; no fixed allocation -


def test_daily_segment_margins_split_revenue_and_cogs(day: date) -> None:
    """Worked example: 2x Chang (bar) + 1x Latte (cafe) on the same day.

    Bar:  2 * 120 = 240 revenue, 2 * 35  = 70 COGS, CM = 170
    Cafe: 1 * 120 = 120 revenue, 1 * 45  = 45 COGS, CM = 75
    """
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    by_seg = {sm.segment: sm for sm in result.segment_margins}
    bar = by_seg[Segment.BAR]
    cafe = by_seg[Segment.CAFE]

    assert bar.revenue == D("240")
    assert bar.variable_costs == D("70")
    assert bar.contribution_margin == D("170")

    assert cafe.revenue == D("120")
    assert cafe.variable_costs == D("45")
    assert cafe.contribution_margin == D("75")


def test_segment_margins_sum_to_daily_totals(day: date) -> None:
    """Segment revenue and COGS sum to the daily totals; segment CM equals the
    daily gross margin (today variable costs == COGS)."""
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    seg_rev = sum((sm.revenue for sm in result.segment_margins), D("0"))
    seg_cogs = sum((sm.variable_costs for sm in result.segment_margins), D("0"))
    seg_cm = sum((sm.contribution_margin for sm in result.segment_margins), D("0"))

    assert seg_rev == result.total_revenue
    assert seg_cogs == result.total_cogs
    assert seg_cm == result.total_gross_margin


def test_both_segments_present_even_when_one_is_empty(day: date) -> None:
    """A day with only bar sales still reports both segments (cafe at zero) so
    the daily review always shows both halves of the business."""
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    by_seg = {sm.segment: sm for sm in result.segment_margins}
    assert set(by_seg) == {Segment.CAFE, Segment.BAR}
    assert by_seg[Segment.CAFE].revenue == D("0")
    assert by_seg[Segment.CAFE].contribution_margin == D("0")


# --- AC 5: a segment whose CM < 0 is flagged red ---------------------------


def test_negative_cm_segment_flagged_red(day: date) -> None:
    """A segment whose contribution margin is negative is flagged red.

    Worked example: a bar item sold below cost. 1 unit sold at 30 THB, recipe
    cost 35 THB -> bar CM = -5 -> red. Cafe has a normal positive CM.
    """
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("30")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    by_seg = {sm.segment: sm for sm in result.segment_margins}
    assert by_seg[Segment.BAR].contribution_margin == D("-5")
    assert by_seg[Segment.BAR].is_red is True
    assert by_seg[Segment.CAFE].contribution_margin == D("75")
    assert by_seg[Segment.CAFE].is_red is False


def test_zero_cm_segment_not_flagged_red(day: date) -> None:
    """The threshold is strict: CM < 0 is red, CM == 0 is not (issue 07:
    'CM ≥ 0' is the failing threshold boundary)."""
    # Chang at exactly cost: 35 revenue, 35 cogs -> CM 0 -> not red.
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("35"))]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    by_seg = {sm.segment: sm for sm in result.segment_margins}
    assert by_seg[Segment.BAR].contribution_margin == D("0")
    assert by_seg[Segment.BAR].is_red is False


def test_segment_margins_ordered_cafe_then_bar(day: date) -> None:
    """Segment margins are returned in canonical cafe-then-bar order.

    ``Segment`` lists BAR before CAFE and ``Segment.value`` is alphabetical
    ("bar" < "cafe"), so neither enum order nor a ``.value`` sort gives
    cafe-first — the roll-up uses an explicit order key. This pins that order
    so a future "sort by .value" regression does not silently flip the daily
    review's segment display.
    """
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    assert [sm.segment for sm in result.segment_margins] == [
        Segment.CAFE,
        Segment.BAR,
    ]


# --- AC: fixed costs are NOT allocated to segments -------------------------


def test_segment_variable_costs_equal_cogs_no_fixed_allocation(day: date) -> None:
    """Per PRD user story 20 / issue 07: fixed costs are entity-level only.

    The segment's ``variable_costs`` must equal its COGS — there is no field on
    ``SegmentMargin`` to carry fixed costs, and the CM is revenue − variable
    costs only. (Fixed costs land in slice 08 at entity level.)
    """
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    for sm in result.segment_margins:
        # No fixed-cost field exists; CM = revenue − variable_costs exactly.
        assert sm.contribution_margin == sm.revenue - sm.variable_costs


# --- Flagged rows excluded from segment CM (clean and defensible) ----------


def test_unmapped_sale_excluded_from_segment_cm_but_counted_for_tagging(
    day: date,
) -> None:
    """An unmapped sale's revenue is NOT booked into a segment's CM (its COGS
    is unknown), but the sale is still tagged via the shift fallback so the
    daily review can show *which* segment the unmapped revenue sits in.

    Worked example: 1 mapped Chang (bar, 120 rev, 85 margin) + 1 unmapped
    'mystery' item sold at 10am (cafe hour, 90 rev). Bar CM = 85 from the
    Chang only; the cafe segment carries 0 CM (its only sale is unmapped).
    """
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(
            item_id="mystery",
            timestamp=day,
            sell_price=D("90"),
            segment=Segment.CAFE,  # cafe-hour sale, stamped via shift fallback
        ),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    by_seg = {sm.segment: sm for sm in result.segment_margins}
    assert by_seg[Segment.BAR].contribution_margin == D("85")
    assert by_seg[Segment.BAR].revenue == D("120")
    # The cafe sale was unmapped -> excluded from cafe CM.
    assert by_seg[Segment.CAFE].revenue == D("0")
    assert by_seg[Segment.CAFE].contribution_margin == D("0")
    # Its revenue is still surfaced at the daily level (flagged_revenue).
    assert result.flagged_revenue == D("90")


# --- AC 3 (period): per-period segment margins over a date range -----------


def test_period_segment_margins_span_multiple_days() -> None:
    """Per-segment revenue/CM over a multi-day period (issue 07: 'for any
    period').

    Two days, each with 1 Chang (bar) + 1 Latte (cafe). Over the period:
    Bar CM = 85 * 2 = 170, Cafe CM = 75 * 2 = 150.
    """
    day1 = date(2026, 6, 24)
    day2 = date(2026, 6, 25)
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day1, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day1, sell_price=D("120")),
        Sale(item_id="chang-draft-500", timestamp=day2, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day2, sell_price=D("120")),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    margins = compute_period_segment_margins(source, start=day1, end=day2)

    by_seg = {sm.segment: sm for sm in margins}
    assert by_seg[Segment.BAR].revenue == D("240")
    assert by_seg[Segment.BAR].variable_costs == D("70")
    assert by_seg[Segment.BAR].contribution_margin == D("170")
    assert by_seg[Segment.CAFE].revenue == D("240")
    assert by_seg[Segment.CAFE].contribution_margin == D("150")


def test_period_segment_margins_exclude_outside_days() -> None:
    """A sale outside the [start, end] window is excluded from the period."""
    day1 = date(2026, 6, 24)
    day2 = date(2026, 6, 25)
    day3 = date(2026, 6, 26)
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day1, sell_price=D("120")),
        Sale(item_id="chang-draft-500", timestamp=day3, sell_price=D("120")),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe()], cost=_cost()
    )

    margins = compute_period_segment_margins(source, start=day1, end=day2)

    by_seg = {sm.segment: sm for sm in margins}
    assert by_seg[Segment.BAR].revenue == D("120")  # only day1, day3 excluded


# --- AC 6: end-to-end across segments with a red flag ----------------------


def test_end_to_end_split_sales_assert_per_segment_cm_and_red_flag() -> None:
    """Full slice-07 seam: synthetic sales + costs split across segments;
    assert per-segment CM and that the loss-making segment is red.

    Setup:
      - Cafe: 2x latte @ 120, cost 45 -> 240 rev, 90 cogs, CM 150 (green)
      - Bar:  3x chang @ 30 (below cost), cost 35 -> 90 rev, 105 cogs, CM -15 (red)

    Daily totals reconcile to the sum of the segments; the bar segment is red.
    """
    day = date(2026, 6, 27)
    sales = [
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("30")),
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("30")),
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("30")),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe(), _latte_recipe()], cost=_cost()
    )

    result = compute_daily_margin(source, day)

    by_seg = {sm.segment: sm for sm in result.segment_margins}
    cafe = by_seg[Segment.CAFE]
    bar = by_seg[Segment.BAR]

    # Per-segment numbers
    assert cafe.revenue == D("240")
    assert cafe.variable_costs == D("90")
    assert cafe.contribution_margin == D("150")
    assert cafe.is_red is False

    assert bar.revenue == D("90")
    assert bar.variable_costs == D("105")
    assert bar.contribution_margin == D("-15")
    assert bar.is_red is True

    # Daily totals reconcile to the segment sum.
    assert result.total_revenue == D("330")
    assert result.total_cogs == D("195")
    assert result.total_gross_margin == D("135")


# --- Shift fallback at the Loyverse parser boundary ------------------------
#
# Issue 07's fallback is "shift timestamp": the Loyverse receipt's created_at
# (a full datetime) is the only place the time-of-day lives, so the parser
# resolves the shift-derived segment there and stamps it on the Sale. These
# tests pin that boundary behaviour.


def _receipts_payload(created_at: str, *, sku: str = "mystery") -> dict[str, object]:
    """One minimal SALE receipt with one unmapped line at ``created_at``."""
    return {
        "receipts": [
            {
                "receipt_number": "r-1",
                "receipt_type": "SALE",
                "created_at": created_at,
                "line_items": [
                    {
                        "id": "li-1",
                        "item_id": "i-1",
                        "sku": sku,
                        "quantity": 1,
                        "price": 50,
                    }
                ],
            }
        ]
    }


def test_parser_stamps_cafe_segment_for_daytime_unmapped_sale() -> None:
    """An unmapped sale created at 10:15 UTC is stamped cafe by the parser."""
    records = parse_receipts_to_sales(
        _receipts_payload("2026-06-24T10:15:00.000Z")
    )

    assert len(records) == 1
    assert records[0].sale.segment == Segment.CAFE


def test_parser_stamps_bar_segment_for_evening_unmapped_sale() -> None:
    """An unmapped sale created at 19:00 UTC is stamped bar by the parser."""
    records = parse_receipts_to_sales(
        _receipts_payload("2026-06-24T19:00:00.000Z")
    )

    assert len(records) == 1
    assert records[0].sale.segment == Segment.BAR
