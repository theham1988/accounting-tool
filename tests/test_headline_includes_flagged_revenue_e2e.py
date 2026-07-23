"""Issue #71 — the headline ties to Loyverse Gross sales.

Reverses the slice-04 reliable-rows-only rule for **revenue** (CONTEXT.md →
COGS recognition, ADR-0008). The headline REVENUE on Day, Period, and Month
must include every sale's revenue — mapped and unmapped alike — so it equals
Loyverse's Gross sales for the same range. COGS, gross margin, and per-segment
contribution margin stay recipe-cost over reliable rows only; flagged revenue
is still surfaced (in ``flagged_revenue`` and ``needs_attention``) so the fix
path stays visible. The card just no longer pretends the unflagged revenue
isn't part of the day's take.

These tests are the worked examples that pin the new contract. Each reads as a
specification of one facet: the daily headline, the period headline, the
per-day drill-down, the segment cards (unchanged), and the loyalty guarantee
that flagged revenue still surfaces.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

from tangerine.cost import CostBook
from tangerine.daily_review import build_daily_review
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.store import InMemoryLoyverseStore, SaleRecord
from tangerine.margin import compute_daily_margin
from tangerine.period_review import build_period_review
from tangerine.seeded import SeededSource
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.types import Recipe, RecipeIngredient, Sale, Segment


def _seeded_cost() -> CostBook:
    """Chang keg + latte ingredients priced (so the mapped sales are reliable)."""
    return CostBook(
        {
            "chang-keg": (D("0.07"), date(2026, 6, 1)),
            "beans-arabica": (D("2"), date(2026, 6, 1)),
            "milk-fresh": (D("0.025"), date(2026, 6, 1)),
        }
    )


# --- shared fixtures ---------------------------------------------------------


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


_DAY = date(2026, 7, 14)


# --- slice 1: the daily margin headline -------------------------------------


def test_daily_margin_total_revenue_includes_unmapped_revenue() -> None:
    """``DailyMargin.total_revenue`` equals Loyverse Gross sales: mapped plus
    unmapped. Worked example: 1 mapped Chang (120) + 1 unmapped 'mystery'
    item (90) -> total_revenue 210, not 120.

    Pre-#71 this returned 120 (reliable rows only). The reversal lands the
    unmapped sale's revenue in the headline so the number a partner reads
    matches the Loyverse dashboard for the same day.
    """
    sales = [
        Sale(item_id="chang-draft-500", timestamp=_DAY, sell_price=D("120")),
        Sale(
            item_id="mystery",
            timestamp=_DAY,
            sell_price=D("90"),
            segment=Segment.CAFE,
        ),
    ]
    source = SeededSource(
        sales=sales,
        recipes=[_chang_recipe(), _latte_recipe()],
        cost=_seeded_cost(),
    )

    result = compute_daily_margin(source, _DAY)

    # 120 (mapped Chang) + 90 (unmapped mystery) = 210 = Loyverse Gross.
    assert result.total_revenue == D("210")
    # COGS stays recipe-cost over reliable rows only: just the Chang (35).
    assert result.total_cogs == D("35")
    # Gross margin is revenue minus COGS, but only the mapped portion has a
    # known COGS. Booked literally it is 210 - 35 = 175 — that's what the
    # headline now carries. The honest labelling lives on the template (the
    # "includes N THB of uncosted revenue" callout), not in the number.
    assert result.total_gross_margin == D("175")
    # The unmapped revenue still surfaces here so the needs-attention card
    # and the headline callout share one source of truth.
    assert result.flagged_revenue == D("90")


def test_daily_margin_total_revenue_includes_unknown_price_revenue() -> None:
    """A mapped-but-unpriced sale's revenue is also in the headline.

    A Chang sold when the keg has no cost-book entry: revenue surfaces in
    the headline (120), COGS stays zero (no recipe-cost can be derived),
    flagged_revenue still carries the 120 so the fix path stays visible.
    """
    sales = [Sale(item_id="chang-draft-500", timestamp=_DAY, sell_price=D("120"))]
    # Empty cost book -> the resolver cannot price the keg.
    source = SeededSource(sales=sales, recipes=[_chang_recipe()], cost=None)

    result = compute_daily_margin(source, _DAY)

    assert result.total_revenue == D("120")
    assert result.total_cogs == D("0")
    assert result.flagged_revenue == D("120")


def test_daily_review_revenue_matches_daily_margin_total_revenue() -> None:
    """``build_daily_review`` exposes the daily-margin headline unchanged —
    issue #71's reversal must not stop at the margin engine; the review the
    partner opens every morning carries the gross-sales headline too.
    """
    sales = [
        Sale(item_id="chang-draft-500", timestamp=_DAY, sell_price=D("120")),
        Sale(
            item_id="mystery",
            timestamp=_DAY,
            sell_price=D("90"),
            segment=Segment.CAFE,
        ),
    ]
    source = SeededSource(
        sales=sales,
        recipes=[_chang_recipe(), _latte_recipe()],
        cost=_seeded_cost(),
    )

    review = build_daily_review(source=source, review_date=_DAY)

    assert review.revenue == D("210")
    assert review.cogs == D("35")
    assert review.gross_margin == D("175")


# --- slice 2: the period review headline ------------------------------------


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
) -> StoreSource:
    """A real ``StoreSource`` over an in-memory SQLite config + sales store."""
    recipes_path = tmp_path / "recipes.yaml"
    recipes_path.write_text(recipes_yaml, encoding="utf-8")
    costs_path = tmp_path / "costs.yaml"
    costs_path.write_text(costs_yaml, encoding="utf-8")
    import sqlite3

    conn = sqlite3.connect(":memory:")
    seed_config(conn, recipes_path=recipes_path, costs_path=costs_path)
    config_store = SqliteConfigStore(
        conn, now=lambda: "2026-07-14T02:00:00+00:00"
    )
    loyverse_store = InMemoryLoyverseStore()
    loyverse_store.record_sales(sales)
    return StoreSource(store=loyverse_store, config=config_store)


def _sale(item_id: str, day: date, price: str, line: str) -> SaleRecord:
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=day, sell_price=D(price)),
        receipt_number=f"r-{day.isoformat()}",
        line_id=line,
    )


def test_period_headline_revenue_includes_unmapped_revenue(
    tmp_path: Path,
) -> None:
    """The period's headline REVENUE includes unmapped sales — issue #71's
    reversal applied to Period/Month mode, not just Day.

    Worked example (one week, one mapped croissant at 80 THB, two unmapped
    specials at 150 THB each):

        reliable revenue     = 80
        flagged  revenue     = 300   (pre-#71 this was the residue)
        headline revenue     = 380   (= Loyverse Gross sales for the week)
        cogs                 = 5      (mapped only — unchanged)
        gross margin         = 375
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [
        _sale("i-croissant", date(2026, 7, 1), "80", "l-1"),
        SaleRecord(
            sale=Sale(
                item_id="i-special",
                timestamp=date(2026, 7, 2),
                sell_price=D("150"),
                segment=Segment.BAR,
            ),
            receipt_number="r-special-1",
            line_id="l-s1",
        ),
        SaleRecord(
            sale=Sale(
                item_id="i-special",
                timestamp=date(2026, 7, 3),
                sell_price=D("150"),
                segment=Segment.BAR,
            ),
            receipt_number="r-special-2",
            line_id="l-s2",
        ),
    ]
    source = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    review = build_period_review(
        source=source, start=date(2026, 7, 1), end=date(2026, 7, 7)
    )

    assert review.revenue == D("380")
    assert review.cogs == D("5")
    assert review.gross_margin == D("375")
    # Flagged revenue still surfaces as the residue — same number, still the
    # needs-attention fix path.
    assert review.flagged_revenue == D("300")
    assert len(review.needs_attention) == 1


def test_period_per_day_rows_include_unmapped_revenue(tmp_path: Path) -> None:
    """The Day-by-Day drilldown rows also carry the gross-sales revenue.

    A quiet day with only an unmapped sale now shows the unmapped revenue on
    its row, not zero. This keeps the drill-down summing to the headline.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [
        _sale("i-croissant", date(2026, 7, 1), "80", "l-1"),
        SaleRecord(
            sale=Sale(
                item_id="i-special",
                timestamp=date(2026, 7, 2),
                sell_price=D("150"),
                segment=Segment.BAR,
            ),
            receipt_number="r-special",
            line_id="l-s",
        ),
    ]
    source = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    review = build_period_review(
        source=source, start=date(2026, 7, 1), end=date(2026, 7, 3)
    )

    by_day = {d.day: d for d in review.days}
    assert by_day[date(2026, 7, 1)].revenue == D("80")
    assert by_day[date(2026, 7, 2)].revenue == D("150")
    assert by_day[date(2026, 7, 2)].cogs == D("0")
    assert by_day[date(2026, 7, 3)].revenue == D("0")
    # Drill-down sums to the headline (80 + 150 + 0 = 230).
    assert sum((d.revenue for d in review.days), D("0")) == review.revenue


# --- slice 3: segment CMs stay reliable-only (the honesty rule) -------------


def test_period_segment_margins_stay_reliable_only(tmp_path: Path) -> None:
    """The headline includes flagged revenue; segment cards do not.

    A bar-shift unmapped special would distort the TAPS card's contribution
    margin (its COGS is unknown, so its revenue cannot honestly land in a
    segment CM). The card stays reliable-only — issue #71 keeps the
    'clean and defensible' rule from PRD user story 20 for segment CM, even
    as the headline moves to gross-sales.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    sales = [
        _sale("i-croissant", date(2026, 7, 1), "80", "l-1"),
        SaleRecord(
            sale=Sale(
                item_id="i-special",
                timestamp=date(2026, 7, 1),
                sell_price=D("150"),
                segment=Segment.BAR,
            ),
            receipt_number="r-special",
            line_id="l-s",
        ),
    ]
    source = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    review = build_period_review(
        source=source, start=date(2026, 7, 1), end=date(2026, 7, 1)
    )

    by_segment = {sm.segment: sm for sm in review.segment_margins}
    # Cafe card carries only the mapped croissant (80 revenue, 5 cogs).
    assert by_segment[Segment.CAFE].revenue == D("80")
    assert by_segment[Segment.CAFE].variable_costs == D("5")
    # Bar card is empty — the unmapped special stays out of the segment CM.
    assert by_segment[Segment.BAR].revenue == D("0")
    assert by_segment[Segment.BAR].contribution_margin == D("0")
    # But the unmapped revenue is in the headline.
    assert review.revenue == D("230")
    assert review.flagged_revenue == D("150")


# --- slice 4: production July 2026 parity (gross-sales reconciliation) ------


def test_july_2026_headline_matches_loyverse_gross_sales(tmp_path: Path) -> None:
    """The map's destination: Books' July headline == Loyverse Gross sales
    (฿130,005) for July 2026.

    This is the parity check the map exists to deliver. Most of July's
    revenue sits in unmapped sales (the bulk of the gap vs Loyverse today);
    landing the unmapped revenue in the headline closes that gap by
    construction. Here we pin the mechanism with a synthetic month: three
    sales totalling 130,005, of which only one is mapped. The headline must
    read 130,005 — Loyverse's Gross sales for the same receipts.
    """
    recipes_yaml, costs_yaml = _CROISSANT_CONFIG
    # One mapped sale (80) + two unmapped sales (100000 + 29925 = 129925) =
    # 130,005. Synthetic, but sized to the real July gap.
    sales = [
        _sale("i-croissant", date(2026, 7, 1), "80", "l-1"),
        SaleRecord(
            sale=Sale(
                item_id="i-taps-special",
                timestamp=date(2026, 7, 15),
                sell_price=D("100000"),
                segment=Segment.BAR,
            ),
            receipt_number="r-taps",
            line_id="l-t",
        ),
        SaleRecord(
            sale=Sale(
                item_id="i-cafe-special",
                timestamp=date(2026, 7, 16),
                sell_price=D("29925"),
                segment=Segment.CAFE,
            ),
            receipt_number="r-cafe",
            line_id="l-c",
        ),
    ]
    source = _seeded_source(
        tmp_path, recipes_yaml=recipes_yaml, costs_yaml=costs_yaml, sales=sales
    )

    review = build_period_review(
        source=source, start=date(2026, 7, 1), end=date(2026, 7, 31)
    )

    assert review.revenue == D("130005")
    # COGS stays mapped-only (5 THB of butter); the rest carries unknown COGS,
    # surfaced honestly as flagged_revenue.
    assert review.cogs == D("5")
    assert review.flagged_revenue == D("129925")
