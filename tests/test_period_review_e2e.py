"""E2E: recipe-cost period engine (Wave 2 slice 2, issue #29).

ADR-0004 decision 1: the period/month reporting surfaces run on recipe-cost
COGS — the daily review's math, aggregated over an arbitrary ``[start, end]``
range. Every sale is costed at the as-of-sale-date price (slice 1's lookup),
so the daily, period, and monthly views agree by construction.

Each test is a worked example over the public interfaces
(``build_period_review``, ``SqliteConfigStore``, ``StoreSource``) — no
reaching into internals.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

from tangerine.daily_review import build_daily_review
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.store import InMemoryLoyverseStore, SaleRecord
from tangerine.period_review import build_period_review
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.types import Sale, Segment


_CROISSANT_CONFIG = (
    """
recipes:
  - sku_id: croissant
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "10" }

mappings:
  - { item_id: i-croissant, sku_id: croissant }
""",
    """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }  # wet market butter
""",
)


def _seeded_source(
    tmp_path: Path,
    *,
    recipes_yaml: str,
    costs_yaml: str,
    sales: list[SaleRecord],
    clock: dict[str, str] | None = None,
) -> tuple[StoreSource, SqliteConfigStore]:
    """A real ``StoreSource`` over an in-memory SQLite config + sales store.

    ``clock["now"]`` is the injectable audit timestamp — mutate it between
    saves to simulate cost edits on different days.
    """
    recipes_path = tmp_path / "recipes.yaml"
    recipes_path.write_text(recipes_yaml, encoding="utf-8")
    costs_path = tmp_path / "costs.yaml"
    costs_path.write_text(costs_yaml, encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes_path, costs_path=costs_path)
    the_clock = clock if clock is not None else {"now": "2026-07-01T02:00:00+00:00"}
    config_store = SqliteConfigStore(conn, now=lambda: the_clock["now"])

    loyverse_store = InMemoryLoyverseStore()
    loyverse_store.record_sales(sales)
    return StoreSource(store=loyverse_store, config=config_store), config_store


def _sale(item_id: str, day: date, price: str, line: str) -> SaleRecord:
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=day, sell_price=D(price)),
        receipt_number=f"r-{day.isoformat()}",
        line_id=line,
    )


# --- period totals at as-of-sale-date prices ----------------------------------


def test_period_totals_cost_each_day_at_its_own_days_price(
    tmp_path: Path,
) -> None:
    """Issue #29's E2E worked example: a month with a mid-month price change.

    A croissant (recipe: 10 g butter) sells once a day for all of July at
    80 THB. On 15 Jul butter's net price doubles (0.50 -> 1.00 THB/g). The
    July period costs the 1st-14th at 5 THB COGS each and the 15th-31st at
    10 THB each — as-of-date, not the render-time price:

        revenue = 31 x 80            = 2480
        cogs    = 14 x 5 + 17 x 10   = 240
        gm      = 2480 - 240         = 2240
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [
        _sale("i-croissant", date(2026, 7, 1) + timedelta(days=i), "80", f"l-{i}")
        for i in range(31)
    ]
    clock = {"now": "2026-07-15T02:00:00+00:00"}
    source, config_store = _seeded_source(
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

    review = build_period_review(
        source=source, start=date(2026, 7, 1), end=date(2026, 7, 31)
    )

    assert review.revenue == D("2480")
    assert review.cogs == D("240")
    assert review.gross_margin == D("2240")


# --- segment contribution margin ----------------------------------------------


def test_segment_cm_splits_by_recipe_segment_with_red_flag_when_negative(
    tmp_path: Path,
) -> None:
    """Mapped sales land in their recipe's segment; a losing segment is red.

    Two days of sales: croissants (cafe, 80 THB vs 5 THB COGS) and a
    loss-leader draft beer (bar, 20 THB vs 35 THB COGS — a segment CM below
    zero). The period's cafe CM is positive; the bar CM is negative and
    carries the existing ``is_red`` flag (issue #29 AC: "the existing red
    flag for CM < 0").
    """
    recipes_yaml = """
recipes:
  - sku_id: croissant
    name: Butter Croissant
    segment: cafe
    ingredients:
      - { sku_id: butter, quantity: "10" }
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }

mappings:
  - { item_id: i-croissant, sku_id: croissant }
  - { item_id: i-chang, sku_id: chang-draft-500 }
"""
    costs_yaml = """
costs:
  butter: { price: "0.50", updated_at: "2026-06-01" }
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }
"""
    sales = [
        _sale("i-croissant", date(2026, 7, 1), "80", "l-1"),
        _sale("i-croissant", date(2026, 7, 2), "80", "l-2"),
        _sale("i-chang", date(2026, 7, 1), "20", "l-3"),
        _sale("i-chang", date(2026, 7, 2), "20", "l-4"),
    ]
    source, _ = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    review = build_period_review(
        source=source, start=date(2026, 7, 1), end=date(2026, 7, 2)
    )

    by_segment = {sm.segment.value: sm for sm in review.segment_margins}
    cafe = by_segment["cafe"]
    assert cafe.revenue == D("160")  # 2 x 80
    assert cafe.variable_costs == D("10")  # 2 x 10 g x 0.50
    assert cafe.contribution_margin == D("150")
    assert not cafe.is_red

    bar = by_segment["bar"]
    assert bar.revenue == D("40")  # 2 x 20
    assert bar.variable_costs == D("70")  # 2 x 500 ml x 0.07
    assert bar.contribution_margin == D("-30")
    assert bar.is_red


# --- unmapped items: excluded from headline, surfaced in needs_attention -------


def test_unmapped_revenue_is_excluded_from_headline_and_surfaced(
    tmp_path: Path,
) -> None:
    """The daily view's unmapped rule, applied to the period (ADR-0004 dec 1).

    A seasonal special sells twice in the week with no recipe mapping, once
    stamped with the bar shift fallback. Its 300 THB of revenue stays out of
    the headline and the segment CMs (recipe-cost COGS is unknown for it) and
    surfaces as one aggregated needs-attention row carrying the shift-stamped
    segment.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    special = SaleRecord(
        sale=Sale(
            item_id="i-special",
            timestamp=date(2026, 7, 2),
            sell_price=D("150"),
            segment=Segment.BAR,
        ),
        receipt_number="r-special-1",
        line_id="l-s1",
    )
    special_again = SaleRecord(
        sale=Sale(
            item_id="i-special",
            timestamp=date(2026, 7, 3),
            sell_price=D("150"),
            segment=Segment.BAR,
        ),
        receipt_number="r-special-2",
        line_id="l-s2",
    )
    sales = [
        _sale("i-croissant", date(2026, 7, 1), "80", "l-1"),
        special,
        special_again,
    ]
    source, _ = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    review = build_period_review(
        source=source, start=date(2026, 7, 1), end=date(2026, 7, 7)
    )

    # Headline counts only the mapped croissant sale.
    assert review.revenue == D("80")
    assert review.cogs == D("5")
    for sm in review.segment_margins:
        assert sm.revenue in (D("80"), D("0"))

    assert review.flagged_revenue == D("300")
    assert len(review.needs_attention) == 1
    row = review.needs_attention[0]
    assert row.item_id == "i-special"
    assert row.unmapped
    assert row.units_sold == 2
    assert row.revenue == D("300")
    assert row.segment is Segment.BAR  # the shift-stamp fallback


# --- a one-day period and the day view agree (daily ⊂ period) -------------------


def test_one_day_period_agrees_exactly_with_the_daily_review(
    tmp_path: Path,
) -> None:
    """Issue #29 AC: "a one-day period agrees with the Day view for that day."

    Same worked example as the mid-month price change: for a day after the
    butter edit, the one-day period review and ``build_daily_review`` answer
    identical revenue / COGS / gross margin / segment CMs — shared as-of-date
    lookup, shared recipe-cost core, agreement by construction.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [
        _sale("i-croissant", date(2026, 7, 3), "80", "l-1"),
        _sale("i-croissant", date(2026, 7, 16), "80", "l-2"),
    ]
    clock = {"now": "2026-07-15T02:00:00+00:00"}
    source, config_store = _seeded_source(
        tmp_path,
        recipes_yaml=recipes_yaml,
        costs_yaml=costs_yaml,
        sales=sales,
        clock=clock,
    )
    config_store.save_cost(
        "butter",
        pack_price=D("1000"),
        pack_quantity=D("1000"),
        vat_inclusive=False,
        updated_by="daniel",
        updated_on=date(2026, 7, 15),
    )

    for day in (date(2026, 7, 3), date(2026, 7, 16)):
        daily = build_daily_review(source=source, review_date=day)
        period = build_period_review(source=source, start=day, end=day)

        assert period.revenue == daily.revenue
        assert period.cogs == daily.cogs
        assert period.gross_margin == daily.gross_margin
        assert period.segment_margins == daily.segment_margins


# --- goal status: 10K THB/day x days in range ----------------------------------


def test_goal_compares_gross_margin_against_10k_per_day_times_days_in_range(
    tmp_path: Path,
) -> None:
    """Issue #29 AC: Month/Period compare against 10K THB/day x days.

    A 7-day period targets 70,000 THB. One 80 THB croissant sale (75 THB
    gross margin) misses it by 69,925. Until slice 3 lands fixed costs the
    comparison basis is gross margin, and the goal says so — no silent
    net-profit claim.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [_sale("i-croissant", date(2026, 7, 1), "80", "l-1")]
    source, _ = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    review = build_period_review(
        source=source, start=date(2026, 7, 1), end=date(2026, 7, 7)
    )

    assert review.goal.days_in_range == 7
    assert review.goal.target == D("70000")
    assert review.goal.actual == review.gross_margin == D("75")
    assert not review.goal.met
    assert review.goal.surplus == D("-69925")
    assert review.goal.basis == "gross_margin"


# --- per-day rows (drill-down foundation) --------------------------------------


def test_per_day_rows_carry_each_days_headline(tmp_path: Path) -> None:
    """One row per day in the range, quiet days included with zeros.

    The PRD's ``period_review`` contract returns per-day rows so a period
    total can be drilled into. A 3-day period with sales on the 1st and 3rd
    carries three rows; the quiet 2nd shows zeros, not a gap.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [
        _sale("i-croissant", date(2026, 7, 1), "80", "l-1"),
        _sale("i-croissant", date(2026, 7, 3), "80", "l-2"),
    ]
    source, _ = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    review = build_period_review(
        source=source, start=date(2026, 7, 1), end=date(2026, 7, 3)
    )

    assert [d.day for d in review.days] == [
        date(2026, 7, 1),
        date(2026, 7, 2),
        date(2026, 7, 3),
    ]
    first, quiet, third = review.days
    assert first.revenue == D("80") and first.gross_margin == D("75")
    assert quiet.revenue == D("0") and quiet.cogs == D("0")
    assert third.gross_margin == D("75")
