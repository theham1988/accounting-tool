"""E2E: the as-of range margin pass (Wave 2 slice — "as-of range pass powers daily margin").

The reporting surfaces used to rebuild a ``RecipeCatalog`` + loop days
independently (the daily review, ``build_period_review``). This slice
introduced a single ``margins_over_range(source, start, end)`` pass —
catalog built once, each day costed at that day's ``cost_book_as_of`` — and
the period/item/goal surfaces now project over its slices. The successor
slice ("Period, item drill, and goal use the range pass") removed the old
``compute_period_segment_margins`` helper entirely, leaving one as-of loop
owning all multi-day costing. Daily numbers must not change.

Each test is a worked example over the public interfaces (``SeededSource``,
``StoreSource``, ``SqliteConfigStore``) — no reaching into internals.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

from collections.abc import Callable

import pytest

from tangerine.cost import CostBook
from tangerine.daily_review import build_daily_review
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.store import InMemoryLoyverseStore, SaleRecord
from tangerine.margin import (
    DayMargins,
    compute_daily_margin,
    margins_over_range,
)
from tangerine.seeded import SeededSource
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.types import (
    DailyMargin,
    ItemMargin,
    Recipe,
    RecipeIngredient,
    Sale,
    Segment,
)


# --- shared seeded fixtures (recipes constant across days) -------------------


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


def _seeded_cost() -> CostBook:
    return CostBook(
        {
            "chang-keg": (D("0.07"), date(2026, 6, 1)),
            "beans-arabica": (D("2"), date(2026, 6, 1)),
            "milk-fresh": (D("0.025"), date(2026, 6, 1)),
        }
    )


# --- AC: the range pass returns one slice per day, in order -------------------


def test_range_pass_returns_one_slice_per_day_in_order() -> None:
    """A 3-day range yields 3 ``DayMargins`` slices, in calendar order.

    Each slice carries that day's ``ItemMargin`` rows only — the pass does
    not aggregate across days. The slice's ``day`` field matches its rows'
    ``day`` fields, and quiet days surface as an empty tuple rather than
    being dropped (the same totality rule the period review's per-day
    drilldown rows follow).
    """
    day1 = date(2026, 6, 24)
    day2 = date(2026, 6, 25)
    day3 = date(2026, 6, 26)
    # Chang on day1, latte on day3, nothing on day2.
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day1, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day3, sell_price=D("120")),
    ]
    source = SeededSource(
        sales=sales,
        recipes=[_chang_recipe(), _latte_recipe()],
        cost=_seeded_cost(),
    )

    slices = margins_over_range(source, start=day1, end=day3)

    assert [s.day for s in slices] == [day1, day2, day3]
    assert all(isinstance(s, DayMargins) for s in slices)
    # Day1: one Chang row; day2: quiet (empty); day3: one latte row.
    assert [im.item_id for im in slices[0].item_margins] == ["chang-draft-500"]
    assert slices[1].item_margins == ()
    assert [im.item_id for im in slices[2].item_margins] == ["espresso-latte"]
    # The slice's day matches each row's day.
    for s in slices:
        for im in s.item_margins:
            assert im.day == s.day


def test_range_pass_single_day_range_returns_one_slice() -> None:
    """A one-day range is the degenerate case ``compute_daily_margin`` projects.

    It must still return a 1-tuple of ``DayMargins`` (not a bare row), so the
    projection ``margins_over_range(source, day, day)[0]`` is uniform across
    one-day and multi-day callers.
    """
    day = date(2026, 6, 24)
    sales = [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe()], cost=_seeded_cost()
    )

    slices = margins_over_range(source, start=day, end=day)

    assert len(slices) == 1
    assert slices[0].day == day
    assert [im.item_id for im in slices[0].item_margins] == ["chang-draft-500"]


# --- AC: the multi-day pass matches per-day results --------------------------


def test_range_pass_rows_match_per_day_compute_daily_margin() -> None:
    """The pass's per-day rows match ``compute_daily_margin``'s for every day.

    Worked example. Three days, each with both a Chang (bar) and a latte
    (cafe). The pass and the per-day roll-up must answer *identical*
    ``ItemMargin`` rows for each day — same revenue, COGS, gross margin, %,
    flags, and segment. This is the "daily margin is a projection over the
    range pass" property: the projection cannot invent or reorder the rows.
    """
    day1 = date(2026, 6, 24)
    day2 = date(2026, 6, 25)
    day3 = date(2026, 6, 26)
    sales: list[Sale] = []
    for d in (day1, day2, day3):
        sales.append(Sale(item_id="chang-draft-500", timestamp=d, sell_price=D("120")))
        sales.append(Sale(item_id="espresso-latte", timestamp=d, sell_price=D("120")))
    source = SeededSource(
        sales=sales,
        recipes=[_chang_recipe(), _latte_recipe()],
        cost=_seeded_cost(),
    )

    slices = margins_over_range(source, start=day1, end=day3)

    for d, slice_ in zip((day1, day2, day3), slices):
        daily = compute_daily_margin(source, d)
        assert slice_.item_margins == daily.item_margins


def test_range_pass_keeps_flagged_rows_per_day() -> None:
    """An unmapped sale surfaces in its day's slice exactly as the daily view shows.

    Flagged rows (unmapped / unknown-price) are returned by the pass — so a
    caller can surface them — but excluded from totals by the projection.
    Their revenue is carried on the row, so the projection's
    ``flagged_revenue`` recovers it.
    """
    day1 = date(2026, 6, 24)
    day2 = date(2026, 6, 25)
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day1, sell_price=D("120")),
        Sale(item_id="mystery", timestamp=day2, sell_price=D("100"),
             segment=Segment.CAFE),
    ]
    source = SeededSource(
        sales=sales, recipes=[_chang_recipe()], cost=_seeded_cost()
    )

    slices = margins_over_range(source, start=day1, end=day2)

    # Day1: one reliable Chang; day2: one unmapped 'mystery'.
    (chang,) = slices[0].item_margins
    assert chang.item_id == "chang-draft-500" and not chang.excluded_from_totals
    (mystery,) = slices[1].item_margins
    assert mystery.item_id == "mystery" and mystery.unmapped

    # The day2 projection recovers the unmapped revenue as flagged_revenue.
    daily_day2 = compute_daily_margin(source, day2)
    assert daily_day2.flagged_revenue == D("100")
    assert daily_day2.item_margins == slices[1].item_margins


# --- AC: daily margin is a thin projection (shape / totals / flagged / segments)


@pytest.mark.parametrize(
    "sales_factory",
    [
        # Single reliable item.
        lambda day: [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120"))],
        # Two reliable items across segments.
        lambda day: [
            Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
            Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
        ],
        # An unmapped sale mixed in (flagged, excluded from totals).
        lambda day: [
            Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
            Sale(item_id="mystery", timestamp=day, sell_price=D("90"),
                 segment=Segment.CAFE),
        ],
        # A loss-leader (negative segment CM, red flag).
        lambda day: [Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("30"))],
    ],
)
def test_daily_margin_projection_unchanged_across_shapes(
    sales_factory: Callable[[date], list[Sale]],
    day: date = date(2026, 6, 24),
) -> None:
    """``compute_daily_margin`` projects the one-day range slice verbatim.

    The projection cannot change Day behaviour: the ``DailyMargin`` shape,
    headline totals (revenue/COGS/gross margin), ``flagged_revenue``, and
    the per-segment CM tuples must match what the per-day roll-up built. The
    four parametrised shapes cover a single item, a split-segment day, a
    flagged-unmapped mix, and a negative-CM segment.
    """
    sales = sales_factory(day)
    source = SeededSource(
        sales=sales,
        recipes=[_chang_recipe(), _latte_recipe()],
        cost=_seeded_cost(),
    )

    daily = compute_daily_margin(source, day)

    # Shape: a DailyMargin for the requested day.
    assert isinstance(daily, DailyMargin)
    assert daily.day == day
    # The rows are exactly the one-day range pass's rows.
    expected_rows = margins_over_range(source, day, day)[0].item_margins
    assert daily.item_margins == expected_rows
    # Headline follows the gross-sales rule (issue #71, ADR-0008): revenue
    # sums every row; COGS sums reliable rows only; gross margin is
    # ``revenue - cogs`` by construction. Flagged revenue surfaces
    # separately so the needs-attention card and headline callout share
    # one source of truth.
    counted = [im for im in expected_rows if not im.excluded_from_totals]
    flagged = [im for im in expected_rows if im.excluded_from_totals]
    assert daily.total_revenue == sum((im.revenue for im in expected_rows), D("0"))
    assert daily.total_cogs == sum((im.cogs for im in counted), D("0"))
    assert daily.total_gross_margin == daily.total_revenue - daily.total_cogs
    assert daily.flagged_revenue == sum((im.revenue for im in flagged), D("0"))
    # Both segments always present, cafe-then-bar canonical order.
    assert [sm.segment for sm in daily.segment_margins] == [Segment.CAFE, Segment.BAR]


def test_daily_margin_unchanged_for_split_segments_with_red_flag() -> None:
    """Pinned slice-11 numbers: cafe positive, bar negative -> bar is red.

    This is the same worked example as the segment-CM end-to-end test, run
    through the rewired ``compute_daily_margin``. Pinning the numbers here
    guards against the projection accidentally shifting a figure when the
    range pass is the new path.
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
        sales=sales,
        recipes=[_chang_recipe(), _latte_recipe()],
        cost=_seeded_cost(),
    )

    daily = compute_daily_margin(source, day)

    assert daily.total_revenue == D("330")
    assert daily.total_cogs == D("195")
    assert daily.total_gross_margin == D("135")
    by_seg = {sm.segment: sm for sm in daily.segment_margins}
    assert by_seg[Segment.BAR].is_red is True
    assert by_seg[Segment.BAR].contribution_margin == D("-15")
    assert by_seg[Segment.CAFE].contribution_margin == D("150")


# --- AC: the range pass costs each day at that day's as-of price --------------


def _sale(item_id: str, day: date, price: str, line: str) -> SaleRecord:
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=day, sell_price=D(price)),
        receipt_number=f"r-{day.isoformat()}",
        line_id=line,
    )


def _seeded_store_source(
    tmp_path: Path,
    *,
    recipes_yaml: str,
    costs_yaml: str,
    sales: list[SaleRecord],
    clock: dict[str, str],
) -> tuple[StoreSource, SqliteConfigStore]:
    """A real ``StoreSource`` over an in-memory SQLite config + sales store."""
    recipes_path = tmp_path / "recipes.yaml"
    recipes_path.write_text(recipes_yaml, encoding="utf-8")
    costs_path = tmp_path / "costs.yaml"
    costs_path.write_text(costs_yaml, encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes_path, costs_path=costs_path)
    config_store = SqliteConfigStore(conn, now=lambda: clock["now"])

    loyverse_store = InMemoryLoyverseStore()
    loyverse_store.record_sales(sales)
    return StoreSource(store=loyverse_store, config=config_store), config_store


def test_range_pass_costs_each_day_at_its_own_days_price(
    tmp_path: Path,
) -> None:
    """As-of-date pricing flows through the range pass, day by day.

    Worked example (mirrors the period review's price-change test): a
    croissant (10 g butter) sells on 3 Jul and 16 Jul; butter's net price
    doubles on 15 Jul. The pass's day slices cost the 3rd at the old price
    (5 THB COGS) and the 16th at the new price (10 THB COGS), reconstructed
    from the audit log — never the render-time price.
    """
    recipes_yaml = """
recipes:
  - sku_id: croissant
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "10" }

mappings:
  - { item_id: i-croissant, sku_id: croissant }
"""
    costs_yaml = """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
"""
    sales = [
        _sale("i-croissant", date(2026, 7, 3), "80", "l-1"),
        _sale("i-croissant", date(2026, 7, 16), "80", "l-2"),
    ]
    clock = {"now": "2026-07-15T02:00:00+00:00"}
    source, config_store = _seeded_store_source(
        tmp_path,
        recipes_yaml=recipes_yaml,
        costs_yaml=costs_yaml,
        sales=sales,
        clock=clock,
    )
    # 15 Jul: butter now 1000 THB per kg -> net 1.00 THB/g.
    config_store.save_cost(
        "butter",
        pack_price=D("1000"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 15),
    )

    slices = margins_over_range(
        source, start=date(2026, 7, 3), end=date(2026, 7, 16)
    )

    # One slice per day in the range; the two sale days land their rows.
    by_day = {s.day: s for s in slices}
    assert by_day[date(2026, 7, 3)].item_margins[0].cogs == D("5")     # 10 g x 0.50
    assert by_day[date(2026, 7, 16)].item_margins[0].cogs == D("10")  # 10 g x 1.00
    # The quiet days between are present and empty.
    for d in (date(2026, 7, 4), date(2026, 7, 15)):
        assert by_day[d].item_margins == ()


# --- AC: validation — reject end < start -------------------------------------


def test_range_pass_rejects_end_before_start() -> None:
    """``end < start`` raises ``ValueError`` — the range is inclusive.

    Mirrors ``build_period_review`` so every reporting caller that takes an
    inclusive range fails the same way on an inverted window.
    """
    start = date(2026, 6, 25)
    end = date(2026, 6, 24)
    source = SeededSource(
        sales=[],
        recipes=[_chang_recipe()],
        cost=_seeded_cost(),
    )

    with pytest.raises(ValueError, match="precedes start"):
        margins_over_range(source, start=start, end=end)


# --- AC: the daily review (slice 11) still works through the new path ---------


def test_daily_review_unchanged_through_range_pass_projection() -> None:
    """The slice-11 daily review composes ``compute_daily_margin`` unchanged.

    ``build_daily_review`` calls ``compute_daily_margin``, which now projects
    over the range pass. A review for a split-segment day must still carry
    the same headline revenue / COGS / gross margin, segment CMs, and
    flagged revenue — proving the rewired daily margin did not change the
    review's outward shape.
    """
    day = date(2026, 6, 24)
    sales = [
        Sale(item_id="chang-draft-500", timestamp=day, sell_price=D("120")),
        Sale(item_id="espresso-latte", timestamp=day, sell_price=D("120")),
    ]
    source = SeededSource(
        sales=sales,
        recipes=[_chang_recipe(), _latte_recipe()],
        cost=_seeded_cost(),
    )

    review = build_daily_review(source=source, review_date=day)

    assert review.revenue == D("240")  # 120 + 120
    assert review.cogs == D("80")      # 35 + 45
    assert review.gross_margin == D("160")
    by_seg = {sm.segment: sm for sm in review.segment_margins}
    assert by_seg[Segment.BAR].contribution_margin == D("85")    # 120 - 35
    assert by_seg[Segment.CAFE].contribution_margin == D("75")   # 120 - 45
    assert review.daily.flagged_revenue == D("0")
