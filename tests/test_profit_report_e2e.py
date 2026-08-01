"""E2E: the Profit Report screen (issue #113, parent spec #112).

Mirrors ``test_period_review_e2e.py``'s seam: ``seed_config`` +
``SqliteConfigStore`` + ``StoreSource`` + ``InMemoryLoyverseStore`` +
FastAPI ``TestClient``, against the public interfaces (the
``/review?mode=profit`` route, the two composed engines, the store). No
reaching into internals.

This slice lands the spine only — the 4th Reports tab, the route, the
default-to-current-calendar-month behaviour, the range navigator, the
report chrome (Tangerine Taps header + banner), the 4-tile summary
(Revenue / Cash-basis GP / Recipe-cost GM / Net profit), and the
"month in progress" marker. The two-lens P&L table, the charts, and the
bestseller lists land in later tickets and extend the composition module
landed alongside this test.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

from fastapi.testclient import TestClient

from tangerine.cash_spend import CashSpendEntry, cash_spend_for_period
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.store import InMemoryLoyverseStore, SaleRecord
from tangerine.period_review import build_period_review
from tangerine.profit_report import build_profit_report
from tangerine.storage.config_store import SqliteConfigStore, seed_config
from tangerine.types import Sale, Segment
from tangerine.web.app import create_app
from tangerine.web.auth import SESSION_COOKIE

_TEST_PASSPHRASE = "profit-report-test-passphrase"
_TEST_SIGNING_SECRET = "profit-report-test-signing-secret"


# One cafe recipe (latte) + one bar recipe (chang), each mapped to the Loyverse
# item the seeded sales use, so both lenses have something real to aggregate.
# Cash-spend rows land against the seeded six buckets (the seed ships
# taps/kitchen/coffee/bakery/staff/rent), so this config exercises both lenses
# and lets the GMs be asserted against the worked example in ``_july_sales``.
def _recipes_yaml() -> str:
    return """
recipes:
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
mappings:
  - { item_id: i-latte, sku_id: espresso-latte }
  - { item_id: i-chang, sku_id: chang-draft-500 }
"""


def _costs_yaml() -> str:
    return """
costs:
  beans-arabica: { price: "2", updated_at: "2026-06-01" }
  milk-fresh: { price: "0.025", updated_at: "2026-06-01" }
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }
"""


def _assignees_yaml() -> str:
    return """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
"""


def _sale_record(
    *,
    receipt_number: str,
    item_id: str,
    day: date,
    price: str,
    line_id: str,
    segment: Segment,
) -> SaleRecord:
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=day, sell_price=D(price), segment=segment),
        receipt_number=receipt_number,
        line_id=line_id,
    )


def _write_seed_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs.write_text(_costs_yaml(), encoding="utf-8")
    assignees.write_text(_assignees_yaml(), encoding="utf-8")
    return recipes, costs, assignees


def _build_app(
    tmp_path: Path,
    *,
    today: date,
    sales: list[SaleRecord] | None = None,
    cash_spend: list[CashSpendEntry] | None = None,
):  # type: ignore[no-untyped-def]
    """App factory over a seeded SQLite DB (the Wave 1 UI-seam pattern).

    Seeds Loyverse sales into the store and cash-spend rows into the config
    store so both lenses have something to aggregate. ``today`` is the app's
    injected clock — it drives the in-progress marker and the default-
    calendar-month resolution.
    """
    recipes, costs, assignees = _write_seed_files(tmp_path)
    db_path = str(tmp_path / "tangerine.db")
    # Open + close once to materialise the file the app will reopen.
    from tangerine.storage.sqlite_store import SqliteLoyverseStore

    store = SqliteLoyverseStore.connect(db_path)
    if sales:
        store.record_sales(sales)
    store.close()
    app = create_app(
        db_path=db_path,
        recipes_path=str(recipes),
        costs_path=str(costs),
        assignees_path=str(assignees),
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )
    cfg: SqliteConfigStore = app.state.config_store
    # Seed the two suppliers cash-spend rows FK into (the migration seeds the
    # six buckets; suppliers are partner-created).
    cfg.create_supplier("makro", name="Makro Phuket", created_by="migration")
    cfg.create_supplier(
        "wet-market", name="Local wet market", created_by="migration"
    )
    for entry in cash_spend or []:
        cfg.create_cash_spend(entry, created_by="migration")
    # Fixed costs land directly as stored entries (no create route needed).
    return app


def _authed_client(app) -> TestClient:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies, "test login did not set a session cookie"
    return client


def _july_sales() -> list[SaleRecord]:
    """A week of lattes (cafe) + changs (bar) in July 2026.

        7 lattes @ 80 THB: revenue 560, COGS 7 x (20g beans x 2 + 200ml milk x 0.025)
                          = 7 x (40 + 5) = 7 x 45 = 315
        7 changs @ 90 THB: revenue 630, COGS 7 x (500ml x 0.07) = 7 x 35 = 245

        July headline (this slice of it):
          revenue        = 560 + 630         = 1190
          cogs           = 315 + 245         = 560
          gross_margin   = 1190 - 560        = 630
    """
    sales: list[SaleRecord] = []
    for i in range(7):
        day = date(2026, 7, 5) + timedelta(days=i)  # 5–11 Jul
        sales.append(
            _sale_record(
                receipt_number=f"r-latte-{i}",
                item_id="i-latte",
                day=day,
                price="80",
                line_id=f"li-l-{i}",
                segment=Segment.CAFE,
            )
        )
        sales.append(
            _sale_record(
                receipt_number=f"r-chang-{i}",
                item_id="i-chang",
                day=day,
                price="90",
                line_id=f"li-c-{i}",
                segment=Segment.BAR,
            )
        )
    return sales


def _july_cash_spend() -> list[CashSpendEntry]:
    """Two July cash-spend rows (net of VAT where flagged).

    A 1,200 THB VAT-inclusive Makro coffee purchase and a 350 THB non-VAT
    wet-market kitchen purchase, both on 10 Jul:

        coffee net = 1200 / 1.07 = 1121.50 (2dp)
        kitchen    = 350          (no division)
        total      = 1471.50
    """
    return [
        CashSpendEntry(
            row_id=0,
            date=date(2026, 7, 10),
            supplier_id="makro",
            description="HoD beans",
            bucket_id="coffee",
            amount=D("1200"),
            vat_inclusive=True,
        ),
        CashSpendEntry(
            row_id=0,
            date=date(2026, 7, 10),
            supplier_id="wet-market",
            description="veg run",
            bucket_id="kitchen",
            amount=D("350"),
            vat_inclusive=False,
        ),
    ]


# --- auth: unauthenticated GET redirects to login ------------------------------


def test_profit_report_requires_auth(tmp_path: Path) -> None:
    """AC: unauthenticated ``GET /review?mode=profit`` redirects to ``/login``.

    Same gate every other Admin/Review route uses — the Profit Report
    carries the venue's profit numbers, so it requires a signed-in partner.
    """
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = TestClient(app)  # no login

    response = client.get("/review?mode=profit", follow_redirects=False)

    assert response.status_code in (302, 303)
    assert "/login" in response.headers.get("location", "")


# --- default range: no month param renders the current calendar month ---------


def test_default_range_renders_current_calendar_month(
    tmp_path: Path,
) -> None:
    """AC: ``GET /review?mode=profit`` (no month) renders the current month.

    ``today`` is injected to 15 Jul 2026, so the default page renders July
    2026. The range label carries 2026-07-01 to 2026-07-31, and the jump-to-
    month input is pre-filled with 2026-07.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit").text

    assert "2026-07-01" in html
    assert "2026-07-31" in html
    # The month jump input is pre-filled with the current month.
    jump = html.split("<!--section:range-nav-->")[1].split("<!--/section:range-nav-->")[0]
    assert 'value="2026-07"' in jump


# --- ?month=YYYY-MM selects any month + range nav ------------------------------


def test_month_param_selects_any_month(tmp_path: Path) -> None:
    """AC: ``?month=YYYY-MM`` resolves and renders that calendar month.

    June 2026 (no sales, no cash spend) renders cleanly — the tiles show
    zeros rather than erroring on an empty month.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-06").text

    assert "2026-06-01" in html
    assert "2026-06-30" in html


def test_invalid_month_is_a_client_error(tmp_path: Path) -> None:
    """A malformed ``month`` is a 400, not a misleading zero-filled report."""
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    response = client.get("/review?mode=profit&month=not-a-month")

    assert response.status_code == 400


def test_range_nav_prev_next_step_calendar_months(tmp_path: Path) -> None:
    """AC: prev/next arrows step whole calendar months and are deep-linkable.

    The prev arrow points at ``?month=YYYY-MM`` for the previous calendar
    month and is a live ``href`` when that month overlaps the synced range.
    A June sale is seeded so July's prev arrow (→ June) stays live. The
    next arrow dims at ``today − 1`` (the latest reviewable day) — the
    same Month-mode bound rule — so on a July page with ``today=2026-07-15``
    the next arrow (→ August) is correctly dimmed. Reaching August is the
    jump-to-month input's job (the next test).
    """
    june_sale = _sale_record(
        receipt_number="r-june-seed",
        item_id="i-latte",
        day=date(2026, 6, 15),
        price="80",
        line_id="li-june",
        segment=Segment.CAFE,
    )
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales() + [june_sale],
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    nav = html.split("<!--section:range-nav-->")[1].split("<!--/section:range-nav-->")[0]

    # Prev arrow is a live link to June.
    assert "mode=profit&amp;month=2026-06" in nav
    # Next arrow targets August but dims at the today−1 bound (Month-mode rule).
    assert "range-nav__arrow--next" in nav
    assert "range-nav__arrow--dimmed" in nav


def test_range_nav_jump_to_month_reaches_any_month(tmp_path: Path) -> None:
    """AC: the jump-to-month input reaches months the arrows dim past.

    The prev/next arrows dim at the synced bounds, but the jump-to-month
    input is unconstrained — a partner can type any month (including a
    future one the next arrow dims past) and land on its report.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    # August is past the next-arrow bound, but the jump input reaches it.
    august_html = client.get("/review?mode=profit&month=2026-08").text
    assert "2026-08-01" in august_html
    assert "2026-08-31" in august_html


def test_range_nav_prev_arrow_dims_when_target_month_precedes_all_sales(
    tmp_path: Path,
) -> None:
    """The prev arrow dims when the target month is before the synced range.

    Same Month-mode rule: July sales only, viewed in July — June's prev
    arrow dims (its end, 2026-06-30, is before the earliest sale on
    2026-07-05). The arrow is ``aria-disabled`` with no ``href``.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    nav = html.split("<!--section:range-nav-->")[1].split("<!--/section:range-nav-->")[0]

    assert "range-nav__arrow--prev" in nav
    assert "range-nav__arrow--dimmed" in nav
    assert 'aria-disabled="true"' in nav


def test_range_nav_jump_to_month_input_submits_to_profit_mode(
    tmp_path: Path,
) -> None:
    """AC: the jump-to-month input carries ``mode=profit`` so it stays on tab.

    The Period/Month jump input carries ``mode=month`` / ``mode=period``;
    the Profit one must carry ``mode=profit`` or jumping would silently
    switch tabs.
    """
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    nav = html.split("<!--section:range-nav-->")[1].split("<!--/section:range-nav-->")[0]

    assert 'name="mode" value="profit"' in nav
    assert 'name="month"' in nav


# --- 4th tab wiring ------------------------------------------------------------


def test_profit_tab_present_in_reports_tabs(tmp_path: Path) -> None:
    """AC: a 4th "Profit" tab appears beside Period / Month / Trends.

    The Profit tab links to ``/review?mode=profit``; on a profit page it
    carries the ``--active`` modifier.
    """
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    switcher = html.split("<!--section:mode-switcher-->")[1].split(
        "<!--/section:mode-switcher-->"
    )[0]

    assert "Profit" in switcher
    assert "/review?mode=profit" in switcher
    # The other three tabs remain.
    assert "Period" in switcher
    assert "Month" in switcher
    assert "Trends" in switcher
    # The active tab is Profit.
    assert "mode-switcher__link--active" in switcher


def test_profit_tab_links_to_the_viewed_month_from_other_tabs(
    tmp_path: Path,
) -> None:
    """From Period/Month, the Profit tab links to that view's month.

    The Profit tab is month-anchored like Month mode: from a June Month page,
    the Profit tab points at June's Profit Report (so a partner reading June
    jumps straight to June's profit, not back to the current month). The
    anchor follows the same rule every other Reports tab follows — the
    target is anchored on the view's day, per ``_mode_switcher_urls``.
    """
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    # From a Month page (June), the Profit tab points at June too.
    html = client.get("/review?mode=month&month=2026-06").text
    switcher = html.split("<!--section:mode-switcher-->")[1].split(
        "<!--/section:mode-switcher-->"
    )[0]
    assert "/review?mode=profit&amp;month=2026-06" in switcher


# --- report chrome: Tangerine Taps header + banner ----------------------------


def test_report_header_renders_tangerine_taps_chrome(tmp_path: Path) -> None:
    """AC: the Tangerine Taps report header + banner render on the page.

    The header carries the "Tangerine Taps" wordmark with "Taps" in the
    tango colour, and the "— Profit Report" suffix in the body face. The
    CSS classes (``.report-header`` / ``.report-banner``) were reserved for
    this screen in #87–#90; this is the first template to wear them.
    """
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    header = html.split("<!--section:report-header-->")[1].split(
        "<!--/section:report-header-->"
    )[0]

    assert "Tangerine" in header
    assert "<b>Taps</b>" in header  # the tango-coloured word
    assert "Profit Report" in header


def test_report_banner_renders_on_every_month(tmp_path: Path) -> None:
    """AC #4 / user story #6: the banner is standing chrome, not just the marker.

    The Tangerine Taps brand banner renders on every Profit Report month —
    past, current, and future. The in-progress marker is *additional*
    content inside the banner when the month is current; its absence on a
    closed month must not also remove the brand chrome.
    """
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    for month in ("2026-06", "2026-07", "2026-08"):
        html = client.get(f"/review?mode=profit&month={month}").text
        assert "<!--section:report-banner-->" in html
        banner = html.split("<!--section:report-banner-->")[1].split(
            "<!--/section:report-banner-->"
        )[0]
        assert "report-banner" in banner  # the CSS class is present


# --- 4 tiles: match build_period_review + cash_spend_for_period ----------------


def test_four_tiles_match_the_two_engines_over_the_same_range(
    tmp_path: Path,
) -> None:
    """AC: the 4 tiles render and match the two engines over the same range.

        Revenue / Recipe-cost GM / Net profit come from ``build_period_review``;
        Cash-basis GP = revenue − ``cash_spend_for_period.total``. Each tile's
        partner-visible number renders inside a ``<!--section:tiles-->`` block
        so the seam is stable against incidental markup changes.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    # Re-run the two engines directly for the assertion baseline.
    source: StoreSource = app.state.source
    cfg: SqliteConfigStore = app.state.config_store
    review = build_period_review(
        source=source,
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        fixed_costs=cfg.fixed_costs(),
    )
    cash = cash_spend_for_period(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        entries=cfg.cash_spend_rows(),
    )
    report = build_profit_report(
        review=review, cash_spend=cash, today=date(2026, 7, 15)
    )

    html = client.get("/review?mode=profit&month=2026-07").text
    tiles = html.split("<!--section:tiles-->")[1].split("<!--/section:tiles-->")[0]

    # Each tile's value renders (money filter drops trailing zeros).
    assert "Revenue" in tiles
    assert f"{report.tiles.revenue:.2f}" in html or str(report.tiles.revenue) in html
    assert "Cash-basis GP" in tiles
    assert "Recipe-cost GM" in tiles
    assert "Net profit" in tiles

    # The cash-basis GP is revenue minus the seeded cash spend:
    #   1190 - (1121.50 + 350) = 1190 - 1471.50 = -281.50
    assert report.tiles.cash_basis_gp == D("-281.50")
    assert "-281.50" in tiles or "-281.5" in tiles


def test_tiles_agree_with_period_view_recipe_cost_numbers(
    tmp_path: Path,
) -> None:
    """AC: the recipe-cost GM on Profit Report equals Period/Month for the range.

    The two screens must never disagree on the number they share. Profit
    Report's Recipe-cost GM tile equals the Period view's gross-margin hero
    for the same ``[start, end]`` (shared as-of-date pricing, by
    construction).
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    profit_html = client.get("/review?mode=profit&month=2026-07").text
    period_html = client.get(
        "/review?mode=period&start=2026-07-01&end=2026-07-31"
    ).text

    # The Period hero value is what the Profit Report's recipe-cost tile
    # must equal. Extract the period hero value and confirm it appears in
    # the profit report's tiles section.
    period_hero = period_html.split('class="headline__hero-value">')[1].split("<")[0]
    profit_tiles = profit_html.split("<!--section:tiles-->")[1].split(
        "<!--/section:tiles-->"
    )[0]
    assert period_hero in profit_tiles


# --- in-progress marker: current month yes, past month no ---------------------


def test_in_progress_marker_visible_on_current_month(tmp_path: Path) -> None:
    """AC: the "month in progress" marker shows when the range contains today."""
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    banner = html.split("<!--section:report-banner-->")[1].split(
        "<!--/section:report-banner-->"
    )[0]

    assert "Month in progress" in banner


def test_in_progress_marker_absent_on_a_fully_past_month(
    tmp_path: Path,
) -> None:
    """AC: a fully-past month does *not* show the marker — absence is meaningful.

    The banner chrome still renders (it is standing brand chrome), but the
    in-progress kicker is absent.
    """
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    # View June from a July "today": June is fully past.
    html = client.get("/review?mode=profit&month=2026-06").text
    banner = html.split("<!--section:report-banner-->")[1].split(
        "<!--/section:report-banner-->"
    )[0]

    assert "Month in progress" not in banner


def test_in_progress_marker_absent_on_a_fully_future_month(
    tmp_path: Path,
) -> None:
    """AC: a fully-future month also does not show the marker."""
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-08").text
    banner = html.split("<!--section:report-banner-->")[1].split(
        "<!--/section:report-banner-->"
    )[0]

    assert "Month in progress" not in banner


# --- two-lens P&L panel (#114): cash-basis GP beside recipe-cost GM ------------


def test_two_lens_pnl_panel_renders_with_both_rows_and_percentages(
    tmp_path: Path,
) -> None:
    """AC #114: the ``.pnl`` table renders two adjacent rows on Profit Report.

        Cash-basis GP    = revenue − cash_spend_for_period.total
        Recipe-cost GM   = review.gross_margin (= revenue − cogs)

    Both percentages are computed against the same ``revenue`` (apples-to-
    apples), and both absolute THB values render inside the panel. Worked
    example over July 2026 (seeded sales + cash spend):

        revenue         = 1190.00
        cogs            = 560.00
        gross_margin    = 630.00    →  630 / 1190 × 100 = 52.94%
        cash_spend      = 1471.50
        cash_basis_gp   = -281.50   →  -281.50 / 1190 × 100 = -23.66%

    The two lenses disagree by design — that is the whole point of the
    screen — and the panel carries both honestly rather than one hiding
    the other.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    assert "<!--section:pnl-->" in html
    pnl = html.split("<!--section:pnl-->")[1].split("<!--/section:pnl-->")[0]

    # Both lens rows are present and labelled.
    assert "Cash-basis GP" in pnl
    assert "Recipe-cost GM" in pnl

    # Both absolute THB values render (money filter → 2dp).
    assert "-281.50" in pnl  # cash-basis GP
    assert "630.00" in pnl  # recipe-cost GM

    # Both percentages render against the same revenue base, to 2dp.
    # Cash-basis: -281.50 / 1190 × 100 = -23.66%; Recipe-cost: 52.94%.
    assert "-23.66%" in pnl
    assert "52.94%" in pnl

    # Revenue is the shared base — stated once in the panel so the
    # apples-to-apples comparison is explicit, not implicit.
    assert "1190.00" in pnl


def test_pnl_percentages_are_absent_when_revenue_is_zero(tmp_path: Path) -> None:
    """AC #114: a month with no sales shows no ``%`` on either lens.

    ``gross_margin_pct`` returns None when revenue is zero (no division by
    zero, no misleading "0%"). The panel renders a placeholder for both
    rows rather than fabricating a percentage.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),  # sales in July
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    # August has no sales in the seed.
    html = client.get("/review?mode=profit&month=2026-08").text
    pnl = html.split("<!--section:pnl-->")[1].split("<!--/section:pnl-->")[0]

    # The rows still render (both lenses present, all amounts 0.00); every
    # % cell — both lens rows *and* the revenue ghost row — is the em-dash
    # placeholder, not a fabricated percentage. No lens says "0.00%", no
    # ghost row says "100.00%" of zero revenue. The only rendered "%" in
    # the panel is the column header "% of revenue" (standing chrome).
    assert "Cash-basis GP" in pnl
    assert "Recipe-cost GM" in pnl

    # Three em-dash % cells: two lens rows + the revenue ghost row.
    em_dash_cells = pnl.count('<td class="num">\u2014</td>')
    assert em_dash_cells == 3
    # The only "%" token left is the column header — no fabricated
    # percentage anywhere in the panel body.
    assert pnl.count("%") == 1


# --- honesty callout (#114): mirror #71 / ADR-0008 on flagged revenue ----------


def test_honesty_callout_when_flagged_revenue_present(tmp_path: Path) -> None:
    """AC #114: a callout shows when ``review.flagged_revenue > 0``.

    Mirrors the period_review.html ``headline__uncosted-note`` pattern (#71,
    ADR-0008): "Revenue includes N THB of sales whose cost the tool cannot
    compute". The recipe-cost lens implicitly zero-costs the uncosted
    portion; the callout is the honest labelling for that — it links the
    partner to the fix path.
    """
    # An unmapped sale (no recipe) lands in flagged_revenue.
    unmapped_sale = _sale_record(
        receipt_number="r-mystery",
        item_id="i-mystery",  # no recipe maps this
        day=date(2026, 7, 12),
        price="100",
        line_id="li-mystery",
        segment=Segment.CAFE,
    )
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales() + [unmapped_sale],
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    assert "<!--section:pnl-flagged-->" in html
    callout = html.split("<!--section:pnl-flagged-->")[1].split(
        "<!--/section:pnl-flagged-->"
    )[0]

    # The callout names the uncosted revenue and points at the fix path,
    # same labelling the period headline carries.
    assert "100.00" in callout  # the flagged revenue amount
    assert "cannot compute" in callout.lower() or "uncosted" in callout.lower()


def test_honesty_callout_absent_when_no_flagged_revenue(tmp_path: Path) -> None:
    """AC #114: the callout section does not render when nothing is flagged.

    Every seeded sale in ``_july_sales()`` maps to a recipe, so July has no
    flagged revenue — the honesty callout is absent (not rendered empty).
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text

    assert "<!--section:pnl-flagged-->" not in html


def test_callout_needs_attention_link_resolves_to_a_rendered_section(
    tmp_path: Path,
) -> None:
    """ADR-0008 pair: the callout's ``#needs-attention`` link must resolve.

    The honesty callout mirrors the period/daily pattern (#71 / ADR-0008):
    a callout *plus* a ``id="needs-attention"`` card one click apart. The
    Profit Report must render the card too — otherwise the callout promises
    a destination that isn't there (a dead anchor on a screen whose entire
    job is honesty about the numbers). ``review.needs_attention`` is already
    on the composition module's review object; this test pins that the
    card is rendered whenever the callout is.
    """
    unmapped_sale = _sale_record(
        receipt_number="r-mystery",
        item_id="i-mystery",
        day=date(2026, 7, 12),
        price="100",
        line_id="li-mystery",
        segment=Segment.CAFE,
    )
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales() + [unmapped_sale],
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text

    # The anchor target renders on the same page as the callout.
    assert 'id="needs-attention"' in html
    # And the card lists the uncosted item by id (deep-link target of the row).
    assert "i-mystery" in html


def test_needs_attention_section_absent_when_nothing_is_flagged(
    tmp_path: Path,
) -> None:
    """The ``id="needs-attention"`` card renders only when there's a fix path.

    Mirror of the period view: the section is gated on
    ``review.needs_attention`` being non-empty. A clean month (every sale
    mapped) renders neither the callout nor the card.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text

    assert 'id="needs-attention"' not in html


# --- regression guard: Period/Month stay recipe-cost-only (#114 territory) -----


def test_period_view_does_not_render_two_lens_or_cash_basis_tile(
    tmp_path: Path,
) -> None:
    """Period/Month stays recipe-cost-only — no two-lens panel, no cash-basis tile.

    The Profit Report is the *only* surface that carries the cash-basis lens.
    This is the regression guard for the "Period/Month stays recipe-cost-only"
    decision (#114 owns the full guard; this assertion pins that the new
    cash-basis vocabulary has not leaked into the Period template).
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    period_html = client.get(
        "/review?mode=period&start=2026-07-01&end=2026-07-31"
    ).text

    # The Period template renders neither the tiles block, the .pnl two-lens
    # panel, nor the cash-basis vocabulary — all Profit-Report-only.
    assert "<!--section:tiles-->" not in period_html
    assert "<!--section:pnl-->" not in period_html
    assert "Cash-basis GP" not in period_html


def test_month_view_does_not_render_two_lens_pnl_panel(
    tmp_path: Path,
) -> None:
    """AC #114: Month mode also stays recipe-cost-only — no two-lens panel.

    The Period/Month-recipe-cost-only decision applies to *both* Period and
    Month templates (they share ``period_review.html``). Pinning Month
    separately guards against a future template split that could regress
    one without the other.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    month_html = client.get("/review?mode=month&month=2026-07").text

    assert "<!--section:pnl-->" not in month_html
    assert "Cash-basis GP" not in month_html


def test_recipe_cost_gm_on_profit_equals_period_view_for_same_range(
    tmp_path: Path,
) -> None:
    """AC #114: the recipe-cost GM on Profit Report equals Period/Month's GM.

    The two screens must never disagree on the number they share. The
    Profit Report's recipe-cost GM row carries the same value the Period
    and Month views' headline hero shows for the same ``[start, end]``
    (shared as-of-date pricing, by construction — both Period and Month
    route through ``_render_period_review`` → ``build_period_review``).
    The cross-screen agreement is pinned at the rendered-string level so
    a future drift surfaces here. Both Period and Month are asserted
    because the AC says "Period/Month" and a future template split could
    regress one without the other.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    period_html = client.get(
        "/review?mode=period&start=2026-07-01&end=2026-07-31"
    ).text
    month_html = client.get("/review?mode=month&month=2026-07").text
    profit_html = client.get("/review?mode=profit&month=2026-07").text

    # Both Period and Month wear the same hero (they share period_review.html);
    # extract once and confirm the Month page agrees with the Period page too.
    period_hero = period_html.split('class="headline__hero-value">')[1].split("<")[0]
    month_hero = month_html.split('class="headline__hero-value">')[1].split("<")[0]
    assert period_hero == month_hero  # Period ↔ Month

    profit_pnl = profit_html.split("<!--section:pnl-->")[1].split(
        "<!--/section:pnl-->"
    )[0]

    # The hero value (630.00) appears verbatim in the Profit Report's
    # recipe-cost GM row — all three surfaces agree on the shared number.
    assert period_hero in profit_pnl  # Profit ↔ Period
    assert month_hero in profit_pnl  # Profit ↔ Month


# --- deep-linkable: every state is a URL --------------------------------------


def test_deep_link_to_a_specific_month_resolves(tmp_path: Path) -> None:
    """AC: ``/review?mode=profit&month=YYYY-MM`` is a shareable, bookmarkable URL."""
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    response = client.get("/review?mode=profit&month=2026-07", follow_redirects=False)

    assert response.status_code == 200
    assert "2026-07-01" in response.text


def test_profit_tab_lights_reports_bottom_nav(tmp_path: Path) -> None:
    """The Profit Report wears the REPORTS bottom-nav cell active."""
    app = _build_app(tmp_path, today=date(2026, 7, 15), sales=_july_sales())
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    nav = html.split("<!--section:bottom-nav-->")[1].split(
        "<!--/section:bottom-nav-->"
    )[0]

    assert "reports" in nav.lower()
    assert "is-active" in nav or "--active" in nav


# --- daily-revenue chart (#115): one bar per day, deep-link to that day ---------
#
# AC: a daily-revenue chart renders one bar per day in the selected month,
# each bar a deep-link into that day's review (``/review?mode=day&day=...``).
# The chart uses the existing ``bar_row`` / ``ChartPoint`` vocabulary (ADR-0002
# unchanged — no new SVG geometry, no client JavaScript). The month's days
# are sourced from ``review.days`` (one entry per day in range, zero-revenue
# days included as zeros rather than omitted, so the chart reads as a
# calendar not a sparse list).


def test_daily_revenue_chart_renders_one_bar_per_day_in_the_month(
    tmp_path: Path,
) -> None:
    """AC: the daily-revenue chart has one bar per calendar day in July (31).

    July 2026 has 31 days. The chart section renders one ``trend-bar`` per
    day, sourced from ``review.days`` — including the 24 days with no sales
    (zero-revenue days stay in the chart so it reads as a calendar, not a
    sparse list of trading days).
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    assert "<!--section:daily-revenue-chart-->" in html
    chart = html.split("<!--section:daily-revenue-chart-->")[1].split(
        "<!--/section:daily-revenue-chart-->"
    )[0]

    # 31 bars, one per calendar day in July. ``bar_row`` emits one
    # ``class="trend-bar"`` outer element per point (the per-bar class is
    # distinct from ``trend-bar__fill`` / ``trend-bar__value`` /
    # ``trend-bar__label`` which carry the inner spans).
    assert chart.count('class="trend-bar"') == 31


def test_daily_revenue_chart_bar_values_come_from_review_days(
    tmp_path: Path,
) -> None:
    """AC: each bar's value is sourced from ``review.days[i].revenue``.

    Worked example: each trading day (5–11 Jul) carries one latte @ 80 THB +
    one chang @ 90 THB = 170 THB revenue. The bar's displayed value reads
    170.00 on those days and 0.00 on the quiet days (rendered, not omitted).
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    chart = html.split("<!--section:daily-revenue-chart-->")[1].split(
        "<!--/section:daily-revenue-chart-->"
    )[0]

    # Seven trading days carry 170 THB each; the value renders to 2dp.
    assert chart.count("170.00") == 7


def test_daily_revenue_chart_each_bar_deep_links_to_that_days_review(
    tmp_path: Path,
) -> None:
    """AC: each daily-revenue bar is a link to ``/review?mode=day&day=...``.

    Every bar in the daily-revenue chart is a drill-in: a partner reading
    the month's revenue shape can jump to any day's review in one click.
    The bar's href carries the day's ISO date, so 2026-07-05 through
    2026-07-11 are all present (the seeded trading days).
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    chart = html.split("<!--section:daily-revenue-chart-->")[1].split(
        "<!--/section:daily-revenue-chart-->"
    )[0]

    # Every bar is a link: ``bar_row`` emits ``<a class="trend-bar">`` when
    # the ChartPoint has an href, ``<span class="trend-bar">`` otherwise.
    # All 31 daily bars carry a deep-link, so the anchor form must appear 31
    # times — never the non-clickable span form.
    assert chart.count('<a class="trend-bar"') == 31
    assert chart.count('<span class="trend-bar"') == 0
    # Every calendar day in July is deep-linked (zero-revenue days too —
    # the partner can still drill into a quiet day's review).
    for day in (5, 11, 1, 31):  # trading days + the month's bookends
        assert f"mode=day&amp;day=2026-07-{day:02d}" in chart
    # The bar's label is the day-of-month (1..31), not the full ISO date —
    # so the chart reads as a compact calendar, one number per bar.
    assert '<span class="trend-bar__label">5</span>' in chart
    assert '<span class="trend-bar__label">11</span>' in chart
    assert '<span class="trend-bar__label">31</span>' in chart


def test_daily_revenue_chart_renders_for_a_month_with_no_sales(
    tmp_path: Path,
) -> None:
    """AC: the chart renders gracefully on an empty month — no broken chart.

    August 2026 has no seeded sales. The chart still renders its 31 bars
    (all zero-revenue) rather than erroring or rendering an empty section.
    This is the empty-period case (no sales) the AC calls out.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-08").text
    assert "<!--section:daily-revenue-chart-->" in html
    chart = html.split("<!--section:daily-revenue-chart-->")[1].split(
        "<!--/section:daily-revenue-chart-->"
    )[0]

    # August has 31 days; every bar is a zero-revenue day.
    assert chart.count('class="trend-bar"') == 31


# --- spend-by-category chart (#115): one bar per non-zero bucket ---------------
#
# AC: a spend-by-category chart renders one bar per bucket with non-zero
# spend in the period, labelled with the bucket's *display name* from the
# spend-bucket vocabulary (#95) — not the raw ``bucket_id`` slug. Zero-spend
# buckets are absent (the chart isn't cluttered with empty categories).


def test_spend_by_category_chart_renders_one_bar_per_non_zero_bucket(
    tmp_path: Path,
) -> None:
    """AC: one bar per bucket with non-zero spend; zero-spend buckets absent.

    The seeded July cash-spend lands on two buckets — coffee (1,121.50 net)
    and kitchen (350) — so the chart has two bars. The other four seeded
    buckets (taps / bakery / staff / rent) carry no spend in July and so
    must not render a bar (AC #14: zero-spend buckets are visibly absent).
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    assert "<!--section:spend-by-category-chart-->" in html
    chart = html.split("<!--section:spend-by-category-chart-->")[1].split(
        "<!--/section:spend-by-category-chart-->"
    )[0]

    # Two bars — coffee + kitchen — no zero-spend bucket.
    assert chart.count('class="trend-bar"') == 2


def test_spend_by_category_chart_bars_carry_display_names(
    tmp_path: Path,
) -> None:
    """AC: bars are labelled with the bucket's display name, not ``bucket_id``.

    The spend-bucket vocabulary (#95) ships display names alongside the
    slug: ``coffee`` → ``Coffee``, ``kitchen`` → ``Kitchen``. The chart's
    bars render the partner-facing name, never the slug — a partner reads
    ``Coffee`` on the chart, not the controlled-vocabulary id.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    chart = html.split("<!--section:spend-by-category-chart-->")[1].split(
        "<!--/section:spend-by-category-chart-->"
    )[0]

    # The display names render; the raw slugs do not (display name is what
    # the partner reads on the chart, the slug is the FK never surfaced).
    assert "Coffee" in chart
    assert "Kitchen" in chart
    # The raw bucket_id slugs must NOT appear as labels — they are an
    # implementation detail of the controlled vocabulary, not a partner-
    # facing label. ``bar_row`` wraps each bar's label in a
    # ``trend-bar__label`` span; check that span's content carries the
    # display name (capitalised), never the lowercase slug.
    assert '<span class="trend-bar__label">Coffee</span>' in chart
    assert '<span class="trend-bar__label">Kitchen</span>' in chart
    assert '<span class="trend-bar__label">coffee</span>' not in chart
    assert '<span class="trend-bar__label">kitchen</span>' not in chart


def test_spend_by_category_chart_bar_values_match_cash_spend_engine(
    tmp_path: Path,
) -> None:
    """AC: each bucket's bar carries its net-of-VAT spend from the engine.

    Worked example (from ``_july_cash_spend``):
      coffee  = 1200 / 1.07 = 1121.50 (2dp, VAT-inclusive)
      kitchen = 350          (no division)

    The chart's bars read those values verbatim, tying the chart to the
    cash-spend admin page for the same range (parent #112 user story #22).
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    chart = html.split("<!--section:spend-by-category-chart-->")[1].split(
        "<!--/section:spend-by-category-chart-->"
    )[0]

    assert "1121.50" in chart  # coffee net-of-VAT
    assert "350.00" in chart  # kitchen


def test_spend_by_category_chart_renders_gracefully_with_no_spend(
    tmp_path: Path,
) -> None:
    """AC: an empty-period case (no cash spend) renders gracefully.

    August 2026 has no cash-spend rows. The chart section still renders
    (so the page layout is stable) but carries no bars and a clear "no
    spend this month" message rather than a broken or missing chart.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-08").text
    assert "<!--section:spend-by-category-chart-->" in html
    chart = html.split("<!--section:spend-by-category-chart-->")[1].split(
        "<!--/section:spend-by-category-chart-->"
    )[0]

    # No bars (zero non-zero buckets), and an honest empty-state message.
    assert chart.count("trend-bar") == 0
    assert "no spend" in chart.lower() or "no cash spend" in chart.lower()


def test_charts_use_existing_bar_row_vocabulary_no_new_svg_or_js(
    tmp_path: Path,
) -> None:
    """AC: both charts use existing ``bar_row`` / ``ChartPoint``.

    ADR-0002 unchanged: the charts render through the same server-side
    ``bar_row`` CSS-bar vocabulary the Trends page uses — no new SVG
    geometry, no client JavaScript. The page ships the single HTMX script
    the base layout already loads (no page-local JS added).
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text

    # The shared ``bar_row`` vocabulary is present on both charts.
    daily = html.split("<!--section:daily-revenue-chart-->")[1].split(
        "<!--/section:daily-revenue-chart-->"
    )[0]
    spend = html.split("<!--section:spend-by-category-chart-->")[1].split(
        "<!--/section:spend-by-category-chart-->"
    )[0]
    assert "trend-bar" in daily
    assert "trend-bar" in spend
    # No new client JavaScript: the page still ships only the base HTMX.
    assert html.count("<script") == 1
    assert "htmx.org" in html
    assert "onclick" not in html


# --- regression: charts absent on Period/Month (Profit-only surface) -----------
#
# Parent #112 IA decision: the Profit Report is the *only* two-lens surface.
# The spend-by-category chart is a cash-basis-lens artifact, so it must not
# leak into Period/Month (regression guard, mirrors the #114 pnl-panel guard).


def test_period_and_month_views_do_not_render_the_profit_charts(
    tmp_path: Path,
) -> None:
    """The new charts are Profit-Report-only — Period/Month stay clean.

    The daily-revenue and spend-by-category chart sections are artifacts
    of the two-lens composition; Period/Month stay recipe-cost-only. This
    pins that the new chart sections have not leaked into the shared
    period_review.html template.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    period_html = client.get(
        "/review?mode=period&start=2026-07-01&end=2026-07-31"
    ).text
    month_html = client.get("/review?mode=month&month=2026-07").text

    for html in (period_html, month_html):
        assert "<!--section:daily-revenue-chart-->" not in html
        assert "<!--section:spend-by-category-chart-->" not in html


# --- bestseller rankings (#116): two sales-side lists ---------------------------
#
# AC: two rankings on the Profit Report — top-N by total sales volume (THB) and
# top-N by total items (unit count). Both share one per-item aggregation over
# the same per-day ``ItemMargin`` rows the recipe-cost lens consumes
# (``margins_over_range``), so they are sourced from the same sales the period
# engine already costed. Each list is sorted descending with ties broken by
# ``item_id``; fewer-than-N renders what exists. Unmapped items appear in both
# rankings (their revenue counts) with a "CM unknown" marker. A visible note
# explains the ranking is sales-side, not contribution-margin.


def test_bestsellers_section_renders_two_lists(tmp_path: Path) -> None:
    """AC: the bestsellers section renders both the by-revenue and by-units lists.

    The section carries two list panes — one headed "By sales volume" (THB),
    one headed "By items sold" (units) — so a partner sees both views at once.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    assert "<!--section:bestsellers-->" in html
    section = html.split("<!--section:bestsellers-->")[1].split(
        "<!--/section:bestsellers-->"
    )[0]

    # Both list panes render, headed with the two metric labels.
    assert "By sales volume" in section
    assert "By items sold" in section
    assert 'class="bestsellers__list bestsellers__list--revenue"' in section
    assert 'class="bestsellers__list bestsellers__list--units"' in section


def test_bestsellers_by_revenue_sorted_descending(tmp_path: Path) -> None:
    """AC: the by-revenue list is sorted high-to-low by total sales volume.

    Seeded July sales: 7 changs @ 90 = 630 THB, 7 lattes @ 80 = 560 THB.
    By revenue, chang (630) outranks latte (560), so the list reads
    chang then latte.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    section = html.split("<!--section:bestsellers-->")[1].split(
        "<!--/section:bestsellers-->"
    )[0]
    # Isolate the revenue pane (it precedes the units pane).
    rev_list = section.split('bestsellers__list--revenue"', 1)[1].split(
        'bestsellers__list--units"', 1
    )[0]

    # Chang (630) precedes latte (560) in the by-revenue list.
    chang_pos = rev_list.find("Chang Draft 500ml")
    latte_pos = rev_list.find("Espresso Latte")
    assert chang_pos != -1 and latte_pos != -1
    assert chang_pos < latte_pos
    # Both absolute THB values render in their revenue cells.
    assert rev_list.count('bestsellers__value--revenue">630.00 THB</span>') == 1
    assert rev_list.count('bestsellers__value--revenue">560.00 THB</span>') == 1


def test_bestsellers_by_units_sorted_descending_with_tie_break(
    tmp_path: Path,
) -> None:
    """AC: the by-units list is sorted high-to-low by unit count; ties by item_id.

    Seeded July sales: 7 changs and 7 lattes — equal units (7 each), so the
    deterministic tie-break by ``item_id`` ascending decides: ``i-chang``
    precedes ``i-latte``. In the by-units list the unit count is the main
    value; revenue is the sub.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    section = html.split("<!--section:bestsellers-->")[1].split(
        "<!--/section:bestsellers-->"
    )[0]
    # The units pane is the second/last list, so splitting on its marker
    # isolates it; the revenue pane precedes it.
    units_list = section.split('bestsellers__list--units"', 1)[1]

    chang_pos = units_list.find("Chang Draft 500ml")
    latte_pos = units_list.find("Espresso Latte")
    assert chang_pos != -1 and latte_pos != -1
    assert chang_pos < latte_pos  # tie-break: i-chang < i-latte
    # The unit count is the main value in the by-units list: each item
    # renders its count in a ``--units`` value span.
    assert units_list.count('bestsellers__value--units">7</span>') == 2


def test_bestsellers_show_fewer_than_n_when_few_items_sold(
    tmp_path: Path,
) -> None:
    """AC: fewer-than-N items renders what exists, not padded.

    The seed has only two distinct items (latte + chang). The default limit
    (10) is larger than that, so each list carries exactly two ranked rows —
    no zero-fill padding.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    section = html.split("<!--section:bestsellers-->")[1].split(
        "<!--/section:bestsellers-->"
    )[0]
    # Isolate each pane: revenue (first) up to the units marker, then units
    # (last). Count the ``<li class="bestsellers__item`` rows precisely —
    # ``class="bestsellers__items"`` (the <ol> container) shares the prefix.
    rev_list = section.split('bestsellers__list--revenue"', 1)[1].split(
        'bestsellers__list--units"', 1
    )[0]
    units_list = section.split('bestsellers__list--units"', 1)[1]

    assert rev_list.count('<li class="bestsellers__item') == 2
    assert units_list.count('<li class="bestsellers__item') == 2


def test_bestsellers_unmapped_item_appears_with_cm_unknown_marker(
    tmp_path: Path,
) -> None:
    """AC: unmapped items appear in both rankings with a "CM unknown" marker.

    An unmapped item's revenue counts toward the ranking (it sold), but its
    cost is unknown so its margin is not shown — the marker is the visible
    labelling for that, not an exclusion. The seeded unmapped item sells 3
    units @ 100 = 300 THB, so it ranks in both lists.

    By revenue: chang 630 > unmapped 300 > latte 560? No — 560 > 300, so the
    revenue order is chang (630), latte (560), unmapped (300). The marker
    must appear next to the unmapped item in *both* lists.
    """
    unmapped_sales = [
        _sale_record(
            receipt_number=f"r-mystery-{i}",
            item_id="i-mystery",  # no recipe maps this
            day=date(2026, 7, 5) + timedelta(days=i),
            price="100",
            line_id=f"li-mystery-{i}",
            segment=Segment.CAFE,
        )
        for i in range(3)  # 3 units @ 100 = 300 THB
    ]
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales() + unmapped_sales,
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    section = html.split("<!--section:bestsellers-->")[1].split(
        "<!--/section:bestsellers-->"
    )[0]
    # Isolate each pane precisely.
    rev_list = section.split('bestsellers__list--revenue"', 1)[1].split(
        'bestsellers__list--units"', 1
    )[0]
    units_list = section.split('bestsellers__list--units"', 1)[1]

    # The unmapped item appears in both lists (its revenue counts).
    assert "i-mystery" in rev_list or "mystery" in rev_list.lower()
    assert "i-mystery" in units_list or "mystery" in units_list.lower()

    # The "CM unknown" marker renders next to the unmapped item in both lists.
    assert rev_list.count("CM unknown") == 1
    assert units_list.count("CM unknown") == 1


def test_bestsellers_sales_side_note_renders(tmp_path: Path) -> None:
    """AC: a visible note explains the ranking is sales-side, not contribution-margin.

    The note records that the rankings are by revenue/units, not by profit —
    so a future reader doesn't mistake "top seller by revenue" for "top earner
    by profit" and "fix" the lists toward contribution-margin.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-07").text
    section = html.split("<!--section:bestsellers-->")[1].split(
        "<!--/section:bestsellers-->"
    )[0]

    note = section.split('class="bestsellers__note"', 1)[1]
    assert "sales-side" in note.lower() or "sales side" in note.lower()
    assert "contribution-margin" in note.lower() or "contribution margin" in note.lower()


def test_bestsellers_render_gracefully_with_no_sales(tmp_path: Path) -> None:
    """AC: an empty month renders the lists' empty state, not a broken section.

    August 2026 has no seeded sales. The section still renders (so the page
    layout is stable), each list shows an honest "No sales this month" empty
    state, and the heading + note still render.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    html = client.get("/review?mode=profit&month=2026-08").text
    assert "<!--section:bestsellers-->" in html
    section = html.split("<!--section:bestsellers-->")[1].split(
        "<!--/section:bestsellers-->"
    )[0]

    # No ranked items, but each list carries the empty-state message.
    assert section.count('<li class="bestsellers__item') == 0
    assert section.lower().count("no sales this month") == 2


# --- regression: bestsellers are Profit-Report-only -----------------------------


def test_period_and_month_views_do_not_render_bestsellers(tmp_path: Path) -> None:
    """The bestsellers section is Profit-Report-only — Period/Month stay clean.

    Mirrors the #114 pnl-panel and #115 chart regression guards: the
    bestsellers are an artifact of the two-lens composition, so they must
    not leak into the shared period_review.html template.
    """
    app = _build_app(
        tmp_path,
        today=date(2026, 7, 15),
        sales=_july_sales(),
        cash_spend=_july_cash_spend(),
    )
    client = _authed_client(app)

    period_html = client.get(
        "/review?mode=period&start=2026-07-01&end=2026-07-31"
    ).text
    month_html = client.get("/review?mode=month&month=2026-07").text

    for html in (period_html, month_html):
        assert "<!--section:bestsellers-->" not in html
        assert "bestsellers" not in html.lower()
