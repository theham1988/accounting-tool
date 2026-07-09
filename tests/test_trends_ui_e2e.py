"""E2E: Trends mode with server-rendered SVG sparklines (Wave 2 slice 5, issue #32).

Extends the Wave 1 UI seam: through FastAPI's ``TestClient`` over the real
SQLite stores, the report page's fourth mode renders revenue / COGS / gross
margin / segment CM over weekly and monthly buckets as inline SVG sparklines
and clickable CSS bars — all emitted by the server, no client JavaScript
(ADR-0004 decision 5, ADR-0002 unchanged).

Trend buckets are computed by the same period engine the Period/Month modes
render, so a bucket's number always equals what drilling into that bucket
shows (issue #32 AC: "trend bucket totals equal the same bucket rendered
directly in Period/Month mode").

Per the PRD's testing rules these tests parse the rendered HTML and assert on
partner-visible numbers and links, never on implementation details.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tangerine.loyverse.store import SaleRecord
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Money, Sale

_TEST_PASSPHRASE = "slice5-test-passphrase"
_TEST_SIGNING_SECRET = "slice5-test-signing-secret"


def _recipes_yaml() -> str:
    """One cafe recipe (latte, 45 THB cost) and one bar recipe (chang, 35 THB)."""
    return """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
  - sku_id: espresso-latte
    name: Espresso Latte
    segment: cafe
    ingredients:
      - { sku_id: beans-arabica, quantity: "20" }
      - { sku_id: milk-fresh, quantity: "200" }
"""


def _costs_yaml() -> str:
    return """
costs:
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }
  beans-arabica: { price: "2", updated_at: "2026-06-01" }
  milk-fresh: { price: "0.025", updated_at: "2026-06-01" }
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
    line_id: str = "li-1",
) -> SaleRecord:
    return SaleRecord(
        sale=Sale(item_id=item_id, timestamp=day, sell_price=Money(price)),
        receipt_number=receipt_number,
        line_id=line_id,
    )


def _build_app(
    tmp_path: Path, *, today: date, sales: list[SaleRecord]
):  # type: ignore[no-untyped-def]
    """App factory over a seeded SQLite DB (the Wave 1 UI-seam pattern)."""
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs.write_text(_costs_yaml(), encoding="utf-8")
    assignees.write_text(_assignees_yaml(), encoding="utf-8")
    store = SqliteLoyverseStore.connect(db_path)
    if sales:
        store.record_sales(sales)
    store.close()
    return create_app(
        db_path=db_path,
        recipes_path=str(recipes),
        costs_path=str(costs),
        assignees_path=str(assignees),
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )


def _authed_client(app):  # type: ignore[no-untyped-def]
    from tangerine.web.auth import SESSION_COOKIE

    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies, "test login did not set a session cookie"
    return client


@pytest.fixture
def yesterday() -> date:
    # A Wednesday, so the week containing it is Mon 13 Jul – Wed 15 Jul.
    return date(2026, 7, 15)


@pytest.fixture
def today(yesterday: date) -> date:
    return yesterday + timedelta(days=1)


# --- AC: Trends joins the mode switcher; the page renders server-side SVG -------


def test_trends_mode_renders_an_inline_svg_sparkline_and_joins_the_switcher(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``GET /review?mode=trends`` renders the fourth mode.

    The page carries the mode switcher with Trends active, and the trend
    chart is an inline ``<svg>`` sparkline emitted by the server — present in
    the raw HTML response, no JavaScript needed to draw it (ADR-0004
    decision 5).
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=yesterday,
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/review?mode=trends")

    assert response.status_code == 200
    html = response.text
    # The chart arrives as inline SVG in the HTML itself.
    assert "<svg" in html
    assert "<polyline" in html
    # Trends is a switcher tab, active on this page; the other three modes
    # are reachable from it.
    switcher = html.split("<!--section:mode-switcher-->")[1].split(
        "<!--/section:mode-switcher-->"
    )[0]
    assert "Trends" in switcher
    assert "mode-switcher__link--active" in switcher
    assert "/review?mode=day" in switcher
    assert "/review?mode=month" in switcher

    # And Trends is reachable from the other modes' switcher too.
    day_html = client.get(f"/review?mode=day&day={yesterday.isoformat()}").text
    day_switcher = day_html.split("<!--section:mode-switcher-->")[1].split(
        "<!--/section:mode-switcher-->"
    )[0]
    assert "/review?mode=trends" in day_switcher


# --- AC: bucket totals match the period engine; bars drill into Period mode -----


def test_weekly_buckets_match_period_mode_and_bars_link_into_it(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """A week bucket shows exactly what drilling into that week shows.

    Worked example: one latte (120 THB, cost 45 -> GM 75) sells on Tue 7 Jul
    (the Mon 6 Jul week); two Changs (120 THB, cost 35 -> GM 85 each) sell on
    Tue 14 Jul (the Mon 13 Jul week, truncated at the 15 Jul anchor). The
    trend's bars carry those weeks' gross margins, each bar links to that
    week's Period-mode URL, and fetching the link renders the identical
    number — same engine, same as-of-date prices (issue #32 AC).
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 7),
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="chang-draft-500",
            day=date(2026, 7, 14),
            price="120",
        ),
        _sale_record(
            receipt_number="r-3",
            item_id="chang-draft-500",
            day=date(2026, 7, 14),
            price="120",
            line_id="li-2",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get("/review?mode=trends&metric=gross_margin&span=weeks").text

    # The full week of 6 Jul: one bar, GM 75.00, linking to that exact range.
    week1_href = "/review?mode=period&amp;start=2026-07-06&amp;end=2026-07-12"
    assert week1_href in html
    assert "75.00" in html

    # The anchor's own week is truncated at the anchor (13-15 Jul, not a
    # claimed-but-empty full week), and carries both Changs' margin.
    week2_href = "/review?mode=period&amp;start=2026-07-13&amp;end=2026-07-15"
    assert week2_href in html
    assert "170.00" in html

    # Drilling in shows the identical number: the bar's target renders the
    # same gross margin the bucket displayed.
    drilled = client.get(
        "/review?mode=period&start=2026-07-06&end=2026-07-12"
    ).text
    assert "75.00" in drilled

    # Twelve weekly buckets end at the anchor's week; the oldest is the week
    # of 27 Apr (11 weeks before 13 Jul).
    assert "/review?mode=period&amp;start=2026-04-27&amp;end=2026-05-03" in html
    # No bucket reaches past the anchor.
    assert "2026-07-16" not in html


def test_monthly_buckets_link_into_month_mode_and_match_it(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``span=months`` renders month-over-month buckets that drill into
    Month mode.

    A latte sells in June and two in July. The monthly trend's June bar
    carries GM 75.00 and links to ``mode=month&month=2026-06`` — the same
    calendar-month range Month mode renders, so the drilled-in page shows
    the identical number (issue #32 AC: bucket totals equal the bucket
    rendered directly).
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 6, 10),
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="espresso-latte",
            day=date(2026, 7, 3),
            price="120",
        ),
        _sale_record(
            receipt_number="r-3",
            item_id="espresso-latte",
            day=date(2026, 7, 10),
            price="120",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get("/review?mode=trends&metric=gross_margin&span=months").text

    # Month bars drill into Month mode, not into an ad-hoc period range.
    assert "/review?mode=month&amp;month=2026-06" in html
    assert "/review?mode=month&amp;month=2026-07" in html
    # June's GM (75.00) and July's (150.00) are the bars' numbers.
    assert "75.00" in html
    assert "150.00" in html
    # Twelve monthly buckets ending with the anchor's month: Aug 2025 is the
    # oldest, and months are labelled as months.
    assert "/review?mode=month&amp;month=2025-08" in html
    assert "Jun 2026" in html

    # Drilling into June renders the identical number.
    drilled = client.get("/review?mode=month&month=2026-06").text
    assert "75.00" in drilled


# --- AC: metric + span are deep-linkable params; bad values are client errors ---


def test_metric_param_selects_revenue_or_cogs_and_the_page_offers_both(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``?metric=revenue`` and ``?metric=cogs`` plot those numbers.

    One latte on 14 Jul: the revenue trend's anchor-week bar reads 120.00,
    the COGS trend's reads 45.00. The page carries a metric control of
    ordinary links (metric as a query param, span preserved) so every chart
    is a shareable URL — same pattern as the mode switcher.
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 14),
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    revenue_html = client.get("/review?mode=trends&metric=revenue&span=weeks").text
    assert "120.00" in revenue_html
    assert "Revenue" in revenue_html

    cogs_html = client.get("/review?mode=trends&metric=cogs&span=months").text
    assert "45.00" in cogs_html

    # The metric control: links carrying metric + span params.
    picker = revenue_html.split("<!--section:metric-switcher-->")[1].split(
        "<!--/section:metric-switcher-->"
    )[0]
    assert "metric=revenue" in picker
    assert "metric=cogs" in picker
    assert "metric=gross_margin" in picker
    assert "span=weeks" in picker
    # And a span control preserving the metric.
    span_picker = revenue_html.split("<!--section:span-switcher-->")[1].split(
        "<!--/section:span-switcher-->"
    )[0]
    assert "span=weeks" in span_picker
    assert "span=months" in span_picker
    assert "metric=revenue" in span_picker


def test_trends_rejects_an_unknown_metric_or_span(
    tmp_path: Path, today: date
) -> None:
    """Bad params are client errors, not silently-defaulted charts."""
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    assert client.get("/review?mode=trends&metric=nope").status_code == 400
    assert client.get("/review?mode=trends&span=fortnights").status_code == 400


# --- AC: per-segment contribution margin trends -----------------------------------


def test_segment_cm_metric_renders_a_chart_per_segment(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``?metric=segment_cm`` plots cafe and bar CM as separate series.

    A latte (CM 75) and a Chang (CM 85) both sell on 14 Jul. The segment-CM
    trend shows a cafe chart whose anchor-week bar reads 75.00 and a bar
    chart reading 85.00, each bar still a drill-in link to that week's
    Period view.
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 14),
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="chang-draft-500",
            day=date(2026, 7, 14),
            price="120",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get("/review?mode=trends&metric=segment_cm&span=weeks").text

    cafe = html.split("<!--section:trend-chart-segment_cm-cafe-->")[1].split(
        "<!--/section:trend-chart-segment_cm-cafe-->"
    )[0]
    bar = html.split("<!--section:trend-chart-segment_cm-bar-->")[1].split(
        "<!--/section:trend-chart-segment_cm-bar-->"
    )[0]
    assert "75.00" in cafe
    assert "85.00" in bar
    # Both segments' charts are sparklines with drill-in bars.
    assert "<svg" in cafe and "<svg" in bar
    week_href = "/review?mode=period&amp;start=2026-07-13&amp;end=2026-07-15"
    assert week_href in cafe and week_href in bar
    # The metric is offered from the metric control.
    assert "metric=segment_cm" in html


# --- AC: day-of-week breakdown across the selected span --------------------------


def test_day_of_week_breakdown_compares_weekdays_across_the_span(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Mondays vs Tuesdays across the whole span, as per-weekday averages.

    The 12-week span (27 Apr – 15 Jul) contains twelve Mondays and twelve
    Tuesdays. Two lattes sell on Mon 13 Jul (GM 150) and one on Tue 14 Jul
    (GM 75); every other day is quiet. The breakdown shows the average gross
    margin per weekday — Monday 12.50, Tuesday 6.25 — quiet weekdays
    included as zeros, so a structural pattern reads as a pattern, not as a
    single lucky day (PRD user story 19).
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 13),
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="espresso-latte",
            day=date(2026, 7, 13),
            price="120",
            line_id="li-2",
        ),
        _sale_record(
            receipt_number="r-3",
            item_id="espresso-latte",
            day=date(2026, 7, 14),
            price="120",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get("/review?mode=trends&metric=gross_margin&span=weeks").text

    breakdown = html.split("<!--section:weekday-breakdown-->")[1].split(
        "<!--/section:weekday-breakdown-->"
    )[0]
    # All seven weekdays render, Monday first.
    for label in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        assert label in breakdown
    assert breakdown.index("Mon") < breakdown.index("Tue") < breakdown.index("Sun")
    # The averages: 150 over 12 Mondays, 75 over 12 Tuesdays.
    assert "12.50" in breakdown
    assert "6.25" in breakdown


def test_monthly_weekday_breakdown_excludes_future_days_in_anchors_month(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """A monthly span's weekday averages count only days up to the anchor.

    ``span=months`` buckets are full calendar months, but the anchor's month
    has not finished yet — days past the anchor carry zero sales (they have
    not happened), so counting them dilutes the per-weekday average with
    future zeros. The breakdown must clip the anchor's month at the anchor,
    matching the weekly span's truncation rule (regression test for a
    Bugbot finding on issue #32: weekday counts were inflated by future
    days in monthly trends).

    Anchor is Wed 15 Jul 2026. One latte (GM 75) sells on Fri 10 Jul. The
    12-month span (Aug 2025 – Jul 2026) contains 53 Fridays in total, but
    3 of July's Fridays (17, 24, 31) fall after the anchor and must not
    count — so the average Friday gross margin is 75 / 50 = 1.50, not
    75 / 53 ≈ 1.42.
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 10),
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get("/review?mode=trends&metric=gross_margin&span=months").text

    breakdown = html.split("<!--section:weekday-breakdown-->")[1].split(
        "<!--/section:weekday-breakdown-->"
    )[0]
    # Friday's average is 75 over the 50 Fridays that have actually happened
    # across the 12-month span (53 total minus July's 3 future Fridays).
    assert "1.50" in breakdown
    # The buggy denominator (all 53 Fridays, including July's future ones)
    # produced ~1.42 — that value must not appear.
    assert "1.42" not in breakdown


# --- AC: the 10K THB/day goal tracked over weeks/months ---------------------------


def test_goal_metric_tracks_attainment_of_10k_per_day_per_bucket(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``?metric=goal`` plots each bucket's progress against 10K THB/day.

    One latte (GM 75) sells on 14 Jul. The anchor's truncated week spans
    3 days (13–15 Jul), so its target is 30,000 THB and its attainment
    0.25%; quiet full weeks read 0.00% of their 70,000 target. Attainment
    is a percentage so short and full buckets compare on one scale, each
    bar still drills into its period — and the basis is honestly labelled
    gross margin, not net profit (fixed costs are not entered yet).
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 14),
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get("/review?mode=trends&metric=goal&span=weeks").text

    # The anchor week's attainment (75 / 30000) and a quiet week's zero.
    assert "0.25%" in html
    assert "0.00%" in html
    # Bars still drill into the bucket's period view.
    assert "/review?mode=period&amp;start=2026-07-13&amp;end=2026-07-15" in html
    # The goal and its honest basis are named on the page.
    assert "10,000 THB/day" in html
    assert "gross margin" in html.lower()
    assert "net" in html.lower()  # ...explicitly named as NOT net profit
    # Goal is offered from the metric control.
    assert "metric=goal" in html


# --- AC: no client JavaScript — the page draws with JS disabled -------------------


def test_trends_page_ships_no_javascript(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The trends page carries no ``<script>`` and no JS event handlers.

    ADR-0004 decision 5 / ADR-0002: charts are inline SVG and CSS bars
    computed server-side; interactivity is plain links. A browser with
    JavaScript disabled renders and navigates the whole trend surface.
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 14),
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    for url in (
        "/review?mode=trends",
        "/review?mode=trends&metric=segment_cm&span=months",
        "/review?mode=trends&metric=goal&span=weeks",
    ):
        html = client.get(url).text
        # The base layout loads HTMX from its CDN (ADR-0002, unchanged); the
        # trend charts themselves remain server-rendered with no page-local JS.
        assert html.count("<script") == 1
        assert "htmx.org" in html
        assert "onclick" not in html
        # The chart is drawn in the markup itself: an SVG polyline and CSS
        # bars are already present in the raw response.
        assert "<polyline" in html
        assert "trend-bar" in html
