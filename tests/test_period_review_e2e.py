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

import calendar
import sqlite3
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

from tangerine.daily_review import build_daily_review
from tangerine.fixed_costs import FixedCostEntry, fixed_costs_for_period
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.store import InMemoryLoyverseStore, SaleRecord
from tangerine.period_review import build_item_performance, build_period_review
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


def _sale(
    item_id: str,
    day: date,
    price: str,
    line: str,
    segment: Segment | None = None,
) -> SaleRecord:
    """Build a ``SaleRecord`` for a synthetic sale.

    ``segment`` is the **clock-stamped** segment (ADR-0007 pure-clock rule).
    Tests written before #73 left it unset; pre-#73 the recipe's segment
    filled in, but under pure-clock an unset segment defaults to bar
    (``segment_of_sale``'s out-of-hours default), which would silently
    mis-split revenue. Tests that care which segment their sale lands in
    pass it explicitly.
    """
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=day, sell_price=D(price), segment=segment),
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
        # ADR-0007: clock-stamped segments — the recipe's segment is menu-shape only.
        _sale("i-croissant", date(2026, 7, 1), "80", "l-1", Segment.CAFE),
        _sale("i-croissant", date(2026, 7, 2), "80", "l-2", Segment.CAFE),
        _sale("i-chang", date(2026, 7, 1), "20", "l-3", Segment.BAR),
        _sale("i-chang", date(2026, 7, 2), "20", "l-4", Segment.BAR),
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


# --- unmapped items: in the headline (gross-sales), surfaced in needs_attention --


def test_unmapped_revenue_is_in_headline_and_surfaced(
    tmp_path: Path,
) -> None:
    """Issue #71 / ADR-0008: the headline is gross-sales, so unmapped revenue
    lands in it. The segment CMs still exclude it (PRD user story 20 — segment
    CM must stay "clean and defensible": a flagged row's COGS is unknown, so
    its revenue cannot honestly land in a segment's CM).

    A seasonal special sells twice in the week with no recipe mapping, once
    stamped with the bar shift fallback. Its 300 THB of revenue is in the
    headline (so the partner reads Loyverse Gross sales), stays out of the
    segment CMs, and surfaces as one aggregated needs-attention row carrying
    the shift-stamped segment.
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

    # Headline is gross-sales: mapped croissant (80) + unmapped specials (300).
    assert review.revenue == D("380")
    assert review.cogs == D("5")  # COGS stays mapped-only
    assert review.gross_margin == D("375")  # 380 - 5
    # Segment CMs still exclude the flagged revenue — the cafe card carries
    # the croissant only; the bar card is empty.
    for sm in review.segment_margins:
        assert sm.revenue in (D("80"), D("0"))

    # Flagged revenue still surfaces as the needs-attention residue.
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


def test_goal_compares_net_profit_against_10k_per_day_times_days_in_range(
    tmp_path: Path,
) -> None:
    """Issue #29/#30 AC: Month/Period compare against 10K THB/day x days.

    A 7-day period targets 70,000 THB. One 80 THB croissant sale (75 THB
    gross margin) misses it by 69,925. From slice 3 the comparison basis is
    net profit (PRD: net-profit-based for Period/Month, gross-margin-based
    only for the daily view); with no fixed costs entered the two coincide
    numerically but the basis label says what is being compared.
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
    assert review.goal.actual == review.net_profit == D("75")
    assert not review.goal.met
    assert review.goal.surplus == D("-69925")
    assert review.goal.basis == "net_profit"


# --- fixed costs: recurring + one-off, exact for calendar months ---------------


def test_recurring_fixed_cost_is_exact_over_a_calendar_month() -> None:
    """Issue #30's worked example: "Rent, 50,000/month, recurring", July view.

    A recurring cost defined once (from June) applies to July in full. A
    whole-calendar-month range is exact — no apportioning, no estimate flag
    (ADR-0004 decision 3: "calendar-month numbers stay exact").
    """
    rent = FixedCostEntry(
        entry_id=1,
        label="Rent",
        category="rent",
        amount=D("50000"),
        kind="recurring",
        period=(2026, 6),
    )

    result = fixed_costs_for_period(
        start=date(2026, 7, 1), end=date(2026, 7, 31), entries=[rent]
    )

    assert not result.estimated
    assert result.total == D("50000")
    (line,) = result.lines
    assert line.label == "Rent"
    assert line.amount == D("50000")
    assert not line.apportioned


def test_sub_month_range_apportions_by_days_and_flags_the_estimate() -> None:
    """The PRD's estimate example: last 7 days of July against 50K rent.

    ``(7 / 31) × 50,000 = 11,290.32`` on an apportioned line, and the whole
    result carries ``estimated=True`` — the label the ADR requires so a
    sub-month net profit is never presented as exact.
    """
    rent = FixedCostEntry(
        entry_id=1,
        label="Rent",
        category="rent",
        amount=D("50000"),
        kind="recurring",
        period=(2026, 6),
    )

    result = fixed_costs_for_period(
        start=date(2026, 7, 25), end=date(2026, 7, 31), entries=[rent]
    )

    assert result.estimated
    (line,) = result.lines
    assert line.apportioned
    assert line.monthly_amount == D("50000")
    assert line.amount == D("11290.32")
    assert result.total == D("11290.32")


def test_oneoff_applies_only_in_its_month() -> None:
    """A one-off repair entered for July lands in July and nowhere else."""
    repair = FixedCostEntry(
        entry_id=1,
        label="Espresso machine repair",
        category="other",
        amount=D("8000"),
        kind="oneoff",
        period=(2026, 7),
    )

    july = fixed_costs_for_period(
        start=date(2026, 7, 1), end=date(2026, 7, 31), entries=[repair]
    )
    august = fixed_costs_for_period(
        start=date(2026, 8, 1), end=date(2026, 8, 31), entries=[repair]
    )

    assert july.total == D("8000")
    assert not july.estimated
    assert august.total == D("0")
    assert august.lines == ()


def test_recurring_respects_its_first_month_and_its_ended_month() -> None:
    """A recurring cost runs from its first month to its end month, inclusive.

    Utilities defined from July and ended on 15 September: June charges
    nothing (before the first month), July and September charge in full
    (ending a cost mid-month does not un-charge the month it was ended in),
    October charges nothing.
    """
    utilities = FixedCostEntry(
        entry_id=1,
        label="Utilities",
        category="utilities",
        amount=D("12000"),
        kind="recurring",
        period=(2026, 7),
        ended_at=date(2026, 9, 15),
    )

    def month_total(year: int, month: int) -> D:
        last = calendar.monthrange(year, month)[1]
        return fixed_costs_for_period(
            start=date(year, month, 1),
            end=date(year, month, last),
            entries=[utilities],
        ).total

    assert month_total(2026, 6) == D("0")
    assert month_total(2026, 7) == D("12000")
    assert month_total(2026, 9) == D("12000")
    assert month_total(2026, 10) == D("0")


# --- net profit: the period review consumes fixed costs ------------------------


def test_month_review_shows_exact_net_profit_and_goal_moves_to_net_basis(
    tmp_path: Path,
) -> None:
    """Issue #30 AC: Month mode's net profit = segment CM − entity fixed costs.

    A July of daily croissant sales (31 × 75 = 2,325 gross margin) against
    50K recurring rent: net profit = 2,325 − 50,000 = −47,675, exact (no
    estimate flag for a calendar month), and the goal now compares *net
    profit* — not gross margin — against 10K × 31.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [
        _sale("i-croissant", date(2026, 7, 1) + timedelta(days=i), "80", f"l-{i}")
        for i in range(31)
    ]
    source, _ = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )
    rent = FixedCostEntry(
        entry_id=1,
        label="Rent",
        category="rent",
        amount=D("50000"),
        kind="recurring",
        period=(2026, 6),
    )

    review = build_period_review(
        source=source,
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        fixed_costs=[rent],
    )

    assert review.gross_margin == D("2325")
    assert review.fixed_costs.total == D("50000")
    assert not review.fixed_costs.estimated
    assert review.net_profit == D("-47675")
    assert review.goal.basis == "net_profit"
    assert review.goal.actual == D("-47675")
    assert review.goal.target == D("310000")
    assert not review.goal.met


def test_sub_month_review_carries_the_apportioned_estimate(
    tmp_path: Path,
) -> None:
    """Issue #30 AC: a 7-day period shows apportioned costs as an estimate.

    Last 7 days of July against 50K rent: the review's fixed costs carry
    ``estimated=True`` and 11,290.32 (7/31 of the month), and net profit is
    gross margin minus that estimate — the number the template must label.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [_sale("i-croissant", date(2026, 7, 28), "80", "l-1")]
    source, _ = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )
    rent = FixedCostEntry(
        entry_id=1,
        label="Rent",
        category="rent",
        amount=D("50000"),
        kind="recurring",
        period=(2026, 6),
    )

    review = build_period_review(
        source=source,
        start=date(2026, 7, 25),
        end=date(2026, 7, 31),
        fixed_costs=[rent],
    )

    assert review.fixed_costs.estimated
    assert review.fixed_costs.total == D("11290.32")
    assert review.net_profit == D("75") - D("11290.32")
    assert review.goal.actual == review.net_profit


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


# --- item performance (the drill-down's last zoom step, issue #31) --------------


def test_item_performance_aggregates_one_item_at_as_of_date_prices(
    tmp_path: Path,
) -> None:
    """Issue #31: a mapped item's performance over a period.

    The croissant sells on 3, 15, and 16 Jul at 80 THB; butter is repriced
    0.50 -> 1.00 THB/g on 15 Jul. Its item-performance view over 1–16 Jul
    shows:

        units    = 3
        revenue  = 3 x 80              = 240
        cogs     = 5 + 10 + 10         = 25   (each day at its own price)
        gm       = 240 - 25            = 215
        gm %     = 215 / 240           = 89.58

    plus one row per day it sold (quiet days omitted — the day-by-day answers
    "when did it sell", not "render a calendar"), and the SKU behind it (the
    "edit recipe" link's target).
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [
        _sale("i-croissant", date(2026, 7, 3), "80", "l-1"),
        _sale("i-croissant", date(2026, 7, 15), "80", "l-2"),
        _sale("i-croissant", date(2026, 7, 16), "80", "l-3"),
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

    perf = build_item_performance(
        source=source,
        item_id="i-croissant",
        start=date(2026, 7, 1),
        end=date(2026, 7, 16),
    )

    assert perf is not None
    assert perf.item_id == "i-croissant"
    assert perf.name == "Butter Croissant"
    assert perf.sku_id == "croissant"
    assert perf.units_sold == 3
    assert perf.revenue == D("240")
    assert perf.cogs == D("25")
    assert perf.gross_margin == D("215")
    assert perf.gross_margin_pct == D("89.58")

    assert [(d.day, d.units_sold, d.cogs) for d in perf.days] == [
        (date(2026, 7, 3), 1, D("5")),
        (date(2026, 7, 15), 1, D("10")),
        (date(2026, 7, 16), 1, D("10")),
    ]


def test_item_performance_is_none_for_an_unmapped_item(tmp_path: Path) -> None:
    """Issue #31 AC: unmapped items do not offer the performance drill.

    A seasonal special with no recipe mapping has no recipe-cost to show —
    its numbers would be fabricated. The engine answers None; the item's fix
    path stays the needs-attention link into item coverage.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [_sale("i-special", date(2026, 7, 2), "150", "l-1")]
    source, _ = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    perf = build_item_performance(
        source=source,
        item_id="i-special",
        start=date(2026, 7, 1),
        end=date(2026, 7, 7),
    )

    assert perf is None


def test_item_performance_carries_the_target_margin_flag(tmp_path: Path) -> None:
    """Issue #31 AC: the item view shows its target-margin flag over the period.

    A croissant with a 95% target margin sells at 80 THB against 5 THB COGS
    (93.75% actual): the period view carries the target and the below-target
    flag, so "was it underwater all week" is answered on the drill itself.
    """
    recipes_yaml = """
recipes:
  - sku_id: croissant
    name: Butter Croissant
    segment: cafe
    target_gross_margin_pct: "95"
    ingredients:
      - { sku_id: butter, quantity: "10" }

mappings:
  - { item_id: i-croissant, sku_id: croissant }
"""
    _, costs_yaml = _CROISSANT_CONFIG
    sales = [_sale("i-croissant", date(2026, 7, 2), "80", "l-1")]
    source, _ = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    perf = build_item_performance(
        source=source,
        item_id="i-croissant",
        start=date(2026, 7, 1),
        end=date(2026, 7, 7),
    )

    assert perf is not None
    assert perf.gross_margin_pct == D("93.75")
    assert perf.target_gross_margin_pct == D("95")
    assert perf.below_target


def test_item_performance_flag_reflects_period_margin_not_per_day_or(
    tmp_path: Path,
) -> None:
    """The below_target flag matches the displayed period margin, not a per-day OR.

    Bug regression: ``below_target`` used to OR each day's per-day flag. With
    as-of-date pricing (or just different sell prices per day), one day's
    margin % can sit below target while the period aggregate — the number
    shown next to the flag — sits above it. The per-day OR would then answer
    True (one day was below) and contradict the displayed period %, e.g.
    "Gross margin 73% / Target 60% / BELOW TARGET". The flag now compares
    the period-level margin % against the target, matching the number beside
    it.

    Worked example — a latte with a 60% target, recipe 10 g beans at
    5.00 THB/g = 50 THB COGS per unit:

      * Day 1 (low-price day): 1 unit at 55 THB -> margin 5/55 = 9.09%
        (below the 60% target). Low volume, so it barely moves the period.
      * Day 2: 10 units at 200 THB -> revenue 2000, COGS 500, margin 1500
        = 75% (above the 60% target). High volume dominates the period.

    Period: revenue 2055, COGS 550, margin 1505/2055 = 73.24% — above the
    60% target. The old per-day OR answered True (day 1 tripped it); the
    period-level comparison answers False, matching "73.24% / target 60%".
    """
    recipes_yaml = """
recipes:
  - sku_id: latte
    name: Latte
    segment: cafe
    target_gross_margin_pct: "60"
    ingredients:
      - { sku_id: beans, quantity: "10" }

mappings:
  - { item_id: i-latte, sku_id: latte }
"""
    costs_yaml = """
costs:
  beans: { price: "5.00", updated_at: "2026-06-01" }
"""
    # Day 2 is 10 units at 200 THB. _sale() builds a single-unit SaleRecord;
    # record ten of them on the same day to get the high-margin volume.
    day2_sales = [
        _sale("i-latte", date(2026, 7, 2), "200", f"l-2-{i}") for i in range(10)
    ]
    sales = [_sale("i-latte", date(2026, 7, 1), "55", "l-1"), *day2_sales]
    source, _ = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    perf = build_item_performance(
        source=source,
        item_id="i-latte",
        start=date(2026, 7, 1),
        end=date(2026, 7, 2),
    )

    assert perf is not None
    assert perf.revenue == D("2055")  # 55 + 10 x 200
    assert perf.cogs == D("550")  # 11 x 50
    assert perf.gross_margin == D("1505")
    assert perf.gross_margin_pct == D("73.24")
    assert perf.target_gross_margin_pct == D("60")
    assert not perf.below_target  # 73.24% > 60%, even though day 1 was below.
