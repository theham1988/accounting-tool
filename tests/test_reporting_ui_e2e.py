"""E2E: the mode-switched report page (Wave 2 slice 2, issue #29).

Extends the Wave 1 UI seam: through FastAPI's ``TestClient`` over the real
SQLite stores, the one report page renders in Day / Period / Month modes,
switched by a top control, every state a deep-linkable URL. The daily review
stays the home — ``GET /`` lands on yesterday's Day mode (Wave 1 user story
19 preserved, now via the redirect ADR-0004 decision 4 specifies).

Per the PRD's testing rules these tests parse the rendered HTML and assert
on partner-visible numbers and controls, never on implementation details.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tangerine.loyverse.store import SaleRecord
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Money, Sale

D = Decimal

_TEST_PASSPHRASE = "slice2-test-passphrase"
_TEST_SIGNING_SECRET = "slice2-test-signing-secret"


def _recipes_yaml() -> str:
    """One cafe recipe (latte 45 THB cost) and one bar recipe (chang 35 THB)."""
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
    segment=None,  # type: ignore[no-untyped-def]
) -> SaleRecord:
    return SaleRecord(
        sale=Sale(
            item_id=item_id,
            timestamp=day,
            sell_price=Money(price),
            segment=segment,
        ),
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
    return date(2026, 7, 15)


@pytest.fixture
def today(yesterday: date) -> date:
    return yesterday + timedelta(days=1)


# --- AC: opening the tool still lands on yesterday's daily review ---------------


def test_root_redirects_to_yesterdays_day_mode_review(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``GET /`` redirects to ``/review?mode=day&day=<yesterday>``.

    The 9am ritual is unchanged — no extra navigation — and the landing
    state is now a deep-linkable URL like every other mode (ADR-0004
    decision 4).
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

    bare = client.get("/", follow_redirects=False)
    assert bare.status_code in (302, 303, 307)
    assert bare.headers["location"] == f"/review?mode=day&day={yesterday.isoformat()}"

    landed = client.get("/")
    assert landed.status_code == 200
    assert "Daily 9am review" in landed.text
    assert yesterday.isoformat() in landed.text
    assert "120.00" in landed.text  # yesterday's revenue, on the landing page


# --- AC: a mode control switches the report page; every state deep-linkable -----


def test_day_mode_page_carries_the_mode_switcher_with_deep_links(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The report page renders a Day / Period / Month mode control.

    Each mode's control is an ordinary link to a deep-linkable URL (mode +
    date/range as query params) — the back button and shared links work, and
    switching modes never needs JavaScript. From a day-mode page for day D,
    Period offers the 7 days ending at D and Month offers D's month.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    html = client.get(f"/review?mode=day&day={yesterday.isoformat()}").text

    switcher = html.split("<!--section:mode-switcher-->")[1].split(
        "<!--/section:mode-switcher-->"
    )[0]
    assert f"/review?mode=day&amp;day={yesterday.isoformat()}" in switcher
    week_start = (yesterday - timedelta(days=6)).isoformat()
    assert (
        f"/review?mode=period&amp;start={week_start}&amp;end={yesterday.isoformat()}"
        in switcher
    )
    assert "/review?mode=month&amp;month=2026-07" in switcher


def test_day_picker_re_anchors_the_mode_switcher(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Picking a new review date re-anchors the Day/Period/Month links.

    The day picker swaps ``#review-body`` in place, but the mode switcher
    sits above it — so the picker must also swap the switcher out-of-band,
    or its links would keep pointing at the previous day's week and month.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    html = client.get(f"/review?mode=day&day={yesterday.isoformat()}").text

    # The switcher is addressable and the picker refreshes it out-of-band.
    assert 'id="mode-switcher"' in html
    nav = html.split("<!--section:day-nav-->")[1].split("<!--/section:day-nav-->")[0]
    assert 'hx-select-oob="#mode-switcher"' in nav

    # The response for the newly picked day carries switcher links anchored
    # on that day, so the out-of-band swap lands the right targets.
    earlier = yesterday - timedelta(days=10)
    repicked = client.get(f"/review?mode=day&day={earlier.isoformat()}").text
    switcher = repicked.split("<!--section:mode-switcher-->")[1].split(
        "<!--/section:mode-switcher-->"
    )[0]
    week_start = (earlier - timedelta(days=6)).isoformat()
    assert (
        f"/review?mode=period&amp;start={week_start}&amp;end={earlier.isoformat()}"
        in switcher
    )


def test_period_mode_renders_the_ranges_totals_and_segment_cm(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``?mode=period&start=...&end=...`` shows the week's recipe-cost numbers.

    Worked example: a latte (120 THB, cost 45) sells on Monday and a Chang
    draft (120 THB, cost 35) on Thursday. The 7-day period page shows
    revenue 240.00, COGS 80.00, gross margin 160.00, and the per-segment
    CM rows (cafe 75.00, bar 85.00).
    """
    start = yesterday - timedelta(days=6)
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=start,
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="chang-draft-500",
            day=start + timedelta(days=3),
            price="120",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get(
        f"/review?mode=period&start={start.isoformat()}&end={yesterday.isoformat()}"
    )

    assert response.status_code == 200
    html = response.text
    assert "240.00" in html  # period revenue
    assert "80.00" in html   # period COGS
    assert "160.00" in html  # period gross margin
    # Both segment CM rows, with their period numbers.
    assert "75.00" in html   # cafe CM (120 - 45)
    assert "85.00" in html   # bar CM (120 - 35)
    # The range itself is named on the page.
    assert start.isoformat() in html
    assert yesterday.isoformat() in html


def test_month_mode_compares_against_10k_per_day_times_days_in_month(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``?mode=month&month=YYYY-MM`` is the period engine over the month.

    July has 31 days, so the target line reads 310,000.00 THB. From slice 3
    the comparison basis is net profit (with no fixed costs entered it
    equals the gross margin, and the page says what it is comparing).
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 5),
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="espresso-latte",
            day=date(2026, 7, 20),
            price="120",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/review?mode=month&month=2026-07")

    assert response.status_code == 200
    html = response.text
    # The whole calendar month is the range (both ends named on the page).
    assert "2026-07-01" in html
    assert "2026-07-31" in html
    # Month totals: two lattes, revenue 240, COGS 90, GM 150.
    assert "240.00" in html
    assert "150.00" in html
    # The 10K x 31 target, and the honest basis label.
    assert "310000.00" in html
    assert "net profit" in html.lower()

    assert client.get("/review?mode=month&month=2026-13").status_code == 400
    assert client.get("/review?mode=month").status_code == 400


def test_month_mode_shows_exact_net_profit_after_fixed_costs(
    tmp_path: Path, today: date
) -> None:
    """Issue #30 AC: Month mode ends in a net-profit line vs the target.

    Rent (50,000/month, recurring) entered once in Admin; July sells two
    lattes (150.00 gross margin). The Month view shows the rent on a fixed-
    costs line, net profit 150 − 50,000 = −49,850.00 against the 310,000
    target — and, this being a calendar month, no "estimate" label anywhere
    (issue #30 AC: "a calendar-month view never shows the estimate label").
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 5),
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="espresso-latte",
            day=date(2026, 7, 20),
            price="120",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)
    client.post(
        "/admin/fixed-costs",
        data={
            "label": "Rent",
            "category": "rent",
            "amount": "50000",
            "kind": "recurring",
            "period": "2026-07",
        },
    )

    html = client.get("/review?mode=month&month=2026-07").text

    fixed = html.split("<!--section:fixed-costs-->")[1].split(
        "<!--/section:fixed-costs-->"
    )[0]
    assert "Rent" in fixed
    assert "50000.00" in fixed
    assert "-49850.00" in html  # net profit = 150 − 50,000
    assert "net profit" in html.lower()
    assert "estimate" not in html.lower()  # exact for a calendar month
    # Never allocated to a segment: the segment rows stay pure CM.
    segment = html.split("<!--section:segment-cm-->")[1].split(
        "<!--/section:segment-cm-->"
    )[0]
    assert "50000" not in segment
    assert "49850" not in segment


def test_period_mode_excludes_unmapped_revenue_and_surfaces_it(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The daily view's unmapped rule holds in Period mode (issue #29 AC).

    A week with one latte (120 THB) and two sales of an unmapped seasonal
    special (150 THB each): the headline shows only the latte's 120.00
    revenue; the special's 300.00 sits in a needs-attention section with its
    fix-it link into item coverage, not silently in the totals.
    """
    start = yesterday - timedelta(days=6)
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=start,
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="i-special",
            day=start + timedelta(days=1),
            price="150",
        ),
        _sale_record(
            receipt_number="r-3",
            item_id="i-special",
            day=start + timedelta(days=2),
            price="150",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get(
        f"/review?mode=period&start={start.isoformat()}&end={yesterday.isoformat()}"
    ).text

    # Headline: latte only.
    assert "120.00" in html
    assert "420.00" not in html  # 120 + 300 must NOT be a headline number

    attention = html.split("<!--section:needs-attention-->")[1].split(
        "<!--/section:needs-attention-->"
    )[0]
    assert "i-special" in attention
    assert "300.00" in attention  # both days' revenue, aggregated
    assert "unmapped" in attention
    assert "/items?item=i-special" in attention  # the existing fix-it deep link


def test_period_and_month_pages_carry_the_switcher_anchored_on_their_range(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Reports tabs switch Period / Month / Trends via deep-linkable URLs.

    On a period page ending at E, Month offers E's month and Trends is
    reachable — moving between report modes feels like one screen with tabs
    (Wave 3 #47), and every step is a URL (ADR-0004).
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)
    start = yesterday - timedelta(days=6)

    period_html = client.get(
        f"/review?mode=period&start={start.isoformat()}&end={yesterday.isoformat()}"
    ).text
    switcher = period_html.split("<!--section:mode-switcher-->")[1].split(
        "<!--/section:mode-switcher-->"
    )[0]
    assert "Period" in switcher
    assert "Month" in switcher
    assert "Trends" in switcher
    assert "mode-switcher__link--active" in switcher
    assert "/review?mode=month&amp;month=2026-07" in switcher
    assert "/review?mode=trends" in switcher
    # Day is the Today surface now — not a Reports tab.
    assert "mode=day" not in switcher

    month_html = client.get("/review?mode=month&month=2026-07").text
    month_switcher = month_html.split("<!--section:mode-switcher-->")[1].split(
        "<!--/section:mode-switcher-->"
    )[0]
    week_start = (date(2026, 7, 31) - timedelta(days=6)).isoformat()
    assert (
        f"/review?mode=period&amp;start={week_start}&amp;end=2026-07-31"
        in month_switcher
    )
    assert "mode-switcher__link--active" in month_switcher


def test_period_mode_flags_a_segment_with_negative_cm_red(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The existing CM < 0 red flag renders in Period mode (issue #29 AC).

    A week of Chang drafts sold at 20 THB against a 35 THB pour cost puts
    the bar's period CM at -15/unit; the segment row carries the RED flag.
    """
    start = yesterday - timedelta(days=6)
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="chang-draft-500",
            day=start,
            price="20",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get(
        f"/review?mode=period&start={start.isoformat()}&end={yesterday.isoformat()}"
    ).text

    assert "segment-cm__row--red" in html
    assert "RED" in html


# --- AC: the Admin destination gathers the config surfaces ----------------------


def test_admin_destination_gathers_the_config_surfaces(
    tmp_path: Path, today: date
) -> None:
    """``GET /admin`` links every editing surface under one umbrella.

    Issue #30 AC: Admin is the app's second top-level destination, gathering
    the Wave 1.5 surfaces (SKUs, items, upload, audit) plus the new
    fixed-cost entry. The existing paths themselves stay unchanged so the
    daily review's needs-attention deep links keep resolving.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    response = client.get("/admin")

    assert response.status_code == 200
    html = response.text
    for path in ("/skus", "/items", "/upload", "/audit", "/admin/fixed-costs"):
        assert f'href="{path}"' in html
    # The gathered surfaces still resolve at their unchanged paths.
    assert client.get("/skus").status_code == 200
    assert client.get("/items").status_code == 200


def test_review_pages_link_to_the_admin_destination(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Today keeps the Admin entry; Reports jumps into Fixed Costs.

    Wave 3 moves Admin off the Reports chrome — the Fixed Costs row-link
    card is the way into entity-level costs from where the partner notices
    them (#47). Day mode still offers Admin via the mode switcher.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    day_html = client.get(f"/review?mode=day&day={yesterday.isoformat()}").text
    month_html = client.get("/review?mode=month&month=2026-07").text

    assert 'href="/admin"' in day_html
    assert 'href="/admin/fixed-costs"' in month_html


# --- AC: fixed-cost entry (create / end / delete), audit-logged -----------------


def test_fixed_cost_form_creates_a_recurring_cost_and_logs_it(
    tmp_path: Path, today: date
) -> None:
    """Issue #30's end-to-end start: "Rent, 50,000/month, recurring", once.

    The Admin fixed-costs page posts the entry, the list shows it, and the
    edit lands in the same audit log as recipe/cost/mapping edits — the
    Wave 1.5 safety net extends to fixed costs.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    page = client.get("/admin/fixed-costs")
    assert page.status_code == 200

    response = client.post(
        "/admin/fixed-costs",
        data={
            "label": "Rent",
            "category": "rent",
            "amount": "50000",
            "kind": "recurring",
            "period": "2026-07",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    listing = response.text.split("<!--section:fixed-cost-list-->")[1].split(
        "<!--/section:fixed-cost-list-->"
    )[0]
    assert "Rent" in listing
    assert "50000.00" in listing
    assert "recurring" in listing

    audit = client.get("/audit").text
    assert "fixed_costs" in audit
    assert "Rent" in audit


def test_sub_month_period_shows_the_apportioned_estimate_labelled(
    tmp_path: Path, today: date
) -> None:
    """Issue #30 AC: a sub-month period labels its fixed costs an estimate.

    Rent 50,000/month recurring; the last 7 days of July show
    (7/31) × 50,000 = 11,290.32 on a line explicitly labelled as an
    apportioned estimate, and the net profit labelled likewise — "a
    net-profit number for any window without being lied to about its
    precision" (PRD user story 16).
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 28),
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)
    client.post(
        "/admin/fixed-costs",
        data={
            "label": "Rent",
            "category": "rent",
            "amount": "50000",
            "kind": "recurring",
            "period": "2026-07",
        },
    )

    html = client.get(
        "/review?mode=period&start=2026-07-25&end=2026-07-31"
    ).text

    fixed = html.split("<!--section:fixed-costs-->")[1].split(
        "<!--/section:fixed-costs-->"
    )[0]
    assert "11290.32" in fixed
    assert "apportioned" in fixed.lower()
    lowered = html.lower()
    assert "fixed (est · apportioned)" in lowered
    assert "net profit (estimate)" in lowered
    # Net profit estimate: latte GM 75 − 11,290.32.
    assert "-11215.32" in html


def test_ending_a_recurring_cost_stops_it_after_its_end_month(
    tmp_path: Path,
) -> None:
    """Issue #30 AC: a partner can end a recurring fixed cost.

    Rent runs from June; the partner ends it in July (today). July's month
    view still charges it in full — the month was already owed — but
    August's charges nothing. The list shows the row as ended, not gone.
    """
    app = _build_app(tmp_path, today=date(2026, 7, 16), sales=[])
    client = _authed_client(app)
    client.post(
        "/admin/fixed-costs",
        data={
            "label": "Rent",
            "category": "rent",
            "amount": "50000",
            "kind": "recurring",
            "period": "2026-06",
        },
    )

    response = client.post(
        "/admin/fixed-costs/1/end", follow_redirects=True
    )

    assert response.status_code == 200
    listing = response.text.split("<!--section:fixed-cost-list-->")[1].split(
        "<!--/section:fixed-cost-list-->"
    )[0]
    # The redesigned status label (issue #50): "ENDED <iso-date>".
    assert "ENDED 2026-07-16" in listing

    july = client.get("/review?mode=month&month=2026-07").text
    assert "50000.00" in july.split("<!--section:fixed-costs-->")[1]
    august = client.get("/review?mode=month&month=2026-08").text
    fixed_august = august.split("<!--section:fixed-costs-->")[1].split(
        "<!--/section:fixed-costs-->"
    )[0]
    assert "50000.00" not in fixed_august


def test_deleting_a_fixed_cost_removes_it_from_every_month(
    tmp_path: Path, today: date
) -> None:
    """Issue #30 AC: a partner can delete a fixed cost (typo/duplicate).

    Unlike ending, deletion removes the row from every month — the July
    view stops charging it and the list no longer shows it.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)
    client.post(
        "/admin/fixed-costs",
        data={
            "label": "Rentt typo",
            "category": "rent",
            "amount": "99999",
            "kind": "recurring",
            "period": "2026-06",
        },
    )
    assert "99999.00" in client.get("/review?mode=month&month=2026-07").text

    response = client.post(
        "/admin/fixed-costs/1/delete", follow_redirects=True
    )

    assert response.status_code == 200
    listing = response.text.split("<!--section:fixed-cost-list-->")[1].split(
        "<!--/section:fixed-cost-list-->"
    )[0]
    assert "Rentt typo" not in listing
    assert "99999.00" not in client.get("/review?mode=month&month=2026-07").text
    assert client.post("/admin/fixed-costs/1/delete").status_code == 404


# --- AC: fixed-costs admin redesign (issue #50) --------------------------------


def test_fixed_costs_page_is_a_reports_sub_page_with_admin_tag_and_intro(
    tmp_path: Path, today: date
) -> None:
    """Issue #50 AC: the page is a Reports sub-page, bottom nav present.

    The header sub-row links back into Reports, names the page "FIXED COSTS",
    and carries a right-aligned ADMIN tag; the bottom nav renders with REPORTS
    active; and the intro sentence states these are whole-venue costs never
    split across cafe/taps.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    html = client.get("/admin/fixed-costs").text

    # Header sub-row: back-to-Reports link, the title, and the ADMIN tag.
    header = html.split("<!--section:fixed-cost-header-->")[1].split(
        "<!--/section:fixed-cost-header-->"
    )[0]
    assert "/review?mode=month" in header  # back-to-Reports target (nav_urls.reports)
    assert "Reports" in header
    assert "FIXED COSTS" in header
    assert "ADMIN" in header

    # The intro sentence: whole-venue, never split across cafe/taps.
    assert "whole-venue" in html.lower()
    assert "never split" in html.lower()

    # Bottom nav present with REPORTS marked active.
    nav = html.split("<!--section:bottom-nav-->")[1].split(
        "<!--/section:bottom-nav-->"
    )[0]
    assert "tb-bottomnav__cell--active" in nav
    assert "Reports" in nav
    assert 'aria-current="page"' in nav


def test_add_a_cost_card_has_every_field_and_saves(
    tmp_path: Path, today: date
) -> None:
    """Issue #50 AC: the ADD A COST card carries label, category, amount, kind,
    from-month, and a SAVE; a valid submit stores the entry."""
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    form_html = client.get("/admin/fixed-costs").text.split(
        "<!--section:fixed-cost-form-->"
    )[1].split("<!--/section:fixed-cost-form-->")[0]
    assert "ADD A COST" in form_html
    for field in ('name="label"', 'name="category"', 'name="amount"',
                  'name="kind"', 'name="period"', 'type="submit"'):
        assert field in form_html
    assert "LABEL" in form_html
    assert "CATEGORY" in form_html
    assert "AMOUNT" in form_html
    assert "KIND" in form_html
    assert "FROM MONTH" in form_html
    assert "SAVE" in form_html
    # The two kind values the route accepts.
    assert 'value="recurring"' in form_html
    assert 'value="oneoff"' in form_html

    response = client.post(
        "/admin/fixed-costs",
        data={
            "label": "Rent",
            "category": "rent",
            "amount": "50000",
            "kind": "recurring",
            "period": "2026-07",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    listing = response.text.split("<!--section:fixed-cost-list-->")[1].split(
        "<!--/section:fixed-cost-list-->"
    )[0]
    assert "Rent" in listing
    assert "50000.00" in listing


def test_invalid_submit_shows_inline_error_and_saves_nothing(
    tmp_path: Path, today: date
) -> None:
    """Issue #50 AC: an invalid submit shows the inline error in place and
    nothing is saved.

    A POST missing both label and amount re-renders the page (200, not a bare
    400) with the canonical "Needs a label and an amount — nothing was saved."
    message inside the ADD A COST card; no row appears in CURRENT and the
    audit log gains no fixed_costs entry.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    response = client.post(
        "/admin/fixed-costs",
        data={"label": "", "amount": "", "kind": "recurring", "period": "2026-07"},
        follow_redirects=False,
    )

    # Re-rendered in place — 200, not a 400 error page.
    assert response.status_code == 200
    form = response.text.split("<!--section:fixed-cost-form-->")[1].split(
        "<!--/section:fixed-cost-form-->"
    )[0]
    assert "Needs a label and an amount — nothing was saved." in form
    assert "fixed-cost-form__error" in form

    # Nothing was saved: CURRENT is empty, and the audit log has no entry.
    listing = response.text.split("<!--section:fixed-cost-list-->")[1].split(
        "<!--/section:fixed-cost-list-->"
    )[0]
    assert "fixed-cost-list__row" not in listing
    assert "fixed_costs" not in client.get("/audit").text


def test_current_list_shows_recurring_total_meta_and_statuses(
    tmp_path: Path, today: date
) -> None:
    """Issue #50 AC: CURRENT · N shows the recurring monthly total, per-row
    meta (category · kind · from month) and status (ACTIVE / ENDED / ONE
    MONTH ONLY), with END on recurring+active rows and DEL on every row.

    Three costs: a recurring rent (50,000, active), a recurring utilities
    cost (5,000, ended) and a one-off repair (8,000). The recurring monthly
    total counts only the active recurring cost → 50,000.00.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)
    for data in (
        {"label": "Rent", "category": "rent", "amount": "50000",
         "kind": "recurring", "period": "2026-06"},
        {"label": "Utilities", "category": "utilities", "amount": "5000",
         "kind": "recurring", "period": "2026-06"},
        {"label": "Espresso machine repair", "category": "other",
         "amount": "8000", "kind": "oneoff", "period": "2026-07"},
    ):
        client.post("/admin/fixed-costs", data=data, follow_redirects=True)

    # End the Utilities recurring cost (entry_id 2).
    client.post("/admin/fixed-costs/2/end", follow_redirects=True)

    listing = client.get("/admin/fixed-costs").text.split(
        "<!--section:fixed-cost-list-->"
    )[1].split("<!--/section:fixed-cost-list-->")[0]

    assert "CURRENT · 3" in listing

    # The recurring monthly total counts only the active recurring cost
    # (Rent 50,000 — not the ended 5,000 nor the one-off 8,000). Isolate the
    # header total so the per-row amounts don't muddy the assertion.
    total_line = listing.split("THB/mo recurring")[0].rsplit(">", 1)[-1]
    assert "50000.00" in total_line
    assert "5000.00" not in total_line
    assert "8000.00" not in total_line

    # Each row carries its meta line (category · kind · from month) and a
    # status. The three statuses the design names.
    assert "ACTIVE" in listing
    assert "ENDED" in listing
    assert "ONE MONTH ONLY" in listing
    assert "rent · recurring · from 2026-06" in listing
    assert "utilities · recurring · from 2026-06" in listing
    assert "other · one-off · from 2026-07" in listing
    assert "/mo" in listing
    assert "once" in listing

    # END exists only for the active recurring row (Rent, entry_id 1);
    # DEL exists for every row. The ended recurring (entry 2) and the one-off
    # (entry 3) carry no END button.
    assert 'action="/admin/fixed-costs/1/end"' in listing
    assert 'action="/admin/fixed-costs/2/end"' not in listing
    assert 'action="/admin/fixed-costs/3/end"' not in listing

    # DEL is on every row.
    for entry_id in (1, 2, 3):
        assert f'action="/admin/fixed-costs/{entry_id}/delete"' in listing

    # The footer explains END vs DEL and that both are audit-logged/revertible.
    assert "END" in listing
    assert "DEL" in listing
    assert "audit log" in listing.lower()
    assert "reverted" in listing.lower()


def test_reverting_a_fixed_cost_creation_removes_it_like_any_config_edit(
    tmp_path: Path, today: date
) -> None:
    """Issue #30 AC: fixed-cost edits are revertible like config edits.

    The rent entry lands in the audit log; reverting that entry (a
    creation) deletes the row — the list and the Month view stop showing
    it, and the revert itself is logged. The Wave 1.5 safety net, extended.
    """
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)
    client.post(
        "/admin/fixed-costs",
        data={
            "label": "Rent",
            "category": "rent",
            "amount": "50000",
            "kind": "recurring",
            "period": "2026-07",
        },
    )
    assert "50000.00" in client.get("/review?mode=month&month=2026-07").text

    audit_html = client.get("/audit").text
    match = re.search(r'action="/audit/(\d+)/revert"', audit_html)
    assert match is not None
    response = client.post(
        f"/audit/{match.group(1)}/revert", follow_redirects=False
    )

    assert response.status_code == 303
    listing = client.get("/admin/fixed-costs").text
    assert "Rent" not in listing.split("<!--section:fixed-cost-list-->")[1]
    assert "50000.00" not in client.get("/review?mode=month&month=2026-07").text
    # The revert is itself on the trail (creation + revert = two rows).
    assert client.get("/audit").text.count('<tr class="audit-row') == 2


def test_issue_30_end_to_end_recurring_rent_plus_oneoff(
    tmp_path: Path, today: date
) -> None:
    """Issue #30's E2E: recurring rent + a one-off; month exact, week estimated.

    Rent 50,000/month recurring (from June) and an 8,000 one-off repair in
    July; one July latte (75.00 gross margin).

    - July month view: fixed costs 58,000 exact; net profit 75 − 58,000 =
      −57,925.00, no estimate label.
    - Last-7-days view: rent 11,290.32 + repair 1,806.45 (both 7/31
      apportioned) = 13,096.77 estimated; net profit −13,021.77, labelled.
    - Both entries are on the audit log; reverting the repair's creation
      removes it, and the month view settles to rent alone (−49,925.00).
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=date(2026, 7, 28),
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)
    client.post(
        "/admin/fixed-costs",
        data={
            "label": "Rent",
            "category": "rent",
            "amount": "50000",
            "kind": "recurring",
            "period": "2026-06",
        },
    )
    client.post(
        "/admin/fixed-costs",
        data={
            "label": "Espresso machine repair",
            "category": "other",
            "amount": "8000",
            "kind": "oneoff",
            "period": "2026-07",
        },
    )

    # Exact monthly net profit, no estimate label.
    month = client.get("/review?mode=month&month=2026-07").text
    assert "58000.00" in month
    assert "-57925.00" in month
    assert "estimate" not in month.lower()

    # Apportioned 7-day estimate, labelled.
    week = client.get(
        "/review?mode=period&start=2026-07-25&end=2026-07-31"
    ).text
    fixed = week.split("<!--section:fixed-costs-->")[1].split(
        "<!--/section:fixed-costs-->"
    )[0]
    assert "11290.32" in fixed
    assert "1806.45" in fixed
    assert "13096.77" in week
    assert "-13021.77" in week
    assert "fixed (est · apportioned)" in week.lower()
    assert "net profit (estimate)" in week.lower()

    # Both edits are on the trail; reverting the repair's creation undoes it.
    audit_html = client.get("/audit").text
    assert audit_html.count("fixed_costs") >= 2
    entry_ids = re.findall(r'action="/audit/(\d+)/revert"', audit_html)
    repair_entry = next(
        eid
        for eid in entry_ids
        if "repair" in _audit_row_for(audit_html, eid).lower()
    )
    client.post(f"/audit/{repair_entry}/revert")

    settled = client.get("/review?mode=month&month=2026-07").text
    assert "50000.00" in settled
    assert "-49925.00" in settled
    assert "8000" not in settled


def _audit_row_for(audit_html: str, entry_id: str) -> str:
    """The audit table row (as text) whose revert form targets ``entry_id``."""
    rows = audit_html.split('<tr class="audit-row')
    return next(
        row for row in rows if f'action="/audit/{entry_id}/revert"' in row
    )
def test_period_day_rows_link_to_that_days_day_mode_review(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Issue #31 AC: day rows in Period/Month mode link to Day mode.

    The period page lists every day in the range with its headline numbers;
    each row is an ordinary link to ``/review?mode=day&day=<that date>`` —
    "what drove this week" is one click to the day, deep-linkable, back
    button returns to the period.
    """
    start = yesterday - timedelta(days=6)
    sale_day = start + timedelta(days=2)
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=sale_day,
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get(
        f"/review?mode=period&start={start.isoformat()}&end={yesterday.isoformat()}"
    ).text

    days = html.split("<!--section:period-days-->")[1].split(
        "<!--/section:period-days-->"
    )[0]
    # Every day in the range is a row linking into its Day-mode review.
    for offset in range(7):
        day = (start + timedelta(days=offset)).isoformat()
        assert f'href="/review?mode=day&amp;day={day}"' in days
    # The sale day's numbers ride on its row (120 revenue, 75 margin).
    assert "120.00" in days
    assert "75.00" in days

    # Month mode renders the same drill (the same engine, same template).
    month_html = client.get("/review?mode=month&month=2026-07").text
    month_days = month_html.split("<!--section:period-days-->")[1].split(
        "<!--/section:period-days-->"
    )[0]
    assert f'href="/review?mode=day&amp;day={sale_day.isoformat()}"' in month_days
    assert 'href="/review?mode=day&amp;day=2026-07-01"' in month_days


def test_item_mode_shows_the_items_period_performance_and_edit_recipe_link(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Issue #31 AC: the item-performance view, with its Admin escape hatch.

    ``?mode=item&item=...&start=...&end=...`` shows the latte's week: 2 units,
    240 revenue, 90 recipe-cost COGS, 150 margin (62.50%), one row per day it
    sold — and a distinct "edit recipe" link to its SKU page in Admin, so the
    config fix is one click away while the report itself stays read-only.
    """
    start = yesterday - timedelta(days=6)
    day_one = start
    day_two = start + timedelta(days=3)
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=day_one,
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="espresso-latte",
            day=day_two,
            price="120",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get(
        f"/review?mode=item&item=espresso-latte"
        f"&start={start.isoformat()}&end={yesterday.isoformat()}"
    )

    assert response.status_code == 200
    html = response.text
    assert "Espresso Latte" in html

    # The period totals: units, revenue, COGS, margin and %.
    assert "240.00" in html  # revenue (2 x 120)
    assert "90.00" in html   # COGS (2 x 45)
    assert "150.00" in html  # gross margin
    assert "62.50" in html   # margin %

    # Day-by-day: one row per day it sold, each with that day's numbers.
    days = html.split("<!--section:item-days-->")[1].split(
        "<!--/section:item-days-->"
    )[0]
    assert day_one.isoformat() in days
    assert day_two.isoformat() in days
    assert "75.00" in days  # each day's margin (120 - 45)

    # The distinct edit-recipe affordance, pointing at the SKU in Admin.
    assert 'href="/skus/espresso-latte"' in html
    assert "edit recipe" in html.lower()


def test_item_mode_refuses_unmapped_items_and_malformed_params(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Unmapped items have no recipe-cost to show — no fabricated drill.

    An unmapped item's performance URL answers 404 (its fix path stays the
    needs-attention link); missing or malformed params are client errors.
    """
    start = yesterday - timedelta(days=6)
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="i-special",
            day=start,
            price="150",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    assert (
        client.get(
            f"/review?mode=item&item=i-special"
            f"&start={start.isoformat()}&end={yesterday.isoformat()}"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/review?mode=item&start={start.isoformat()}"
            f"&end={yesterday.isoformat()}"
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/review?mode=item&item=espresso-latte&start=nope&end=2026-07-15"
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/review?mode=item&item=espresso-latte"
            "&start=2026-07-15&end=2026-07-01"
        ).status_code
        == 400
    )


def test_day_mode_links_mapped_items_to_their_performance_but_not_unmapped(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Issue #31 AC: mapped items in Day mode link to the performance drill.

    The latte's rows in the day's rankings link to
    ``?mode=item&item=...&start=<day>&end=<day>`` (the PRD's worked
    interaction). The unmapped special offers no performance drill — there
    is no recipe cost to show — and keeps its existing needs-attention fix
    path into item coverage.
    """
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=yesterday,
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="i-special",
            day=yesterday,
            price="150",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    html = client.get(f"/review?mode=day&day={yesterday.isoformat()}").text

    day = yesterday.isoformat()
    item_url = (
        f"/review?mode=item&amp;item=espresso-latte&amp;start={day}&amp;end={day}"
    )
    rankings = html.split("<!--section:top-by-margin-->")[1].split(
        "<!--/section:bottom-by-volume-->"
    )[0]
    assert f'href="{item_url}"' in rankings

    # The unmapped special: no performance drill anywhere on the page...
    assert "mode=item&amp;item=i-special" not in html
    # ...but its existing fix path survives.
    attention = html.split("<!--section:needs-attention-->")[1].split(
        "<!--/section:needs-attention-->"
    )[0]
    assert "/items?item=i-special" in attention


def test_breadcrumb_reflects_the_zoom_path_and_each_crumb_navigates(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Issue #31 AC: a breadcrumb shows the path; each crumb navigates up.

    The item page drilled from 15 Jul reads Review › Jul 2026 › 15 Jul ›
    Espresso Latte — Review links home, the month crumb to Month mode, the
    day crumb to that day's review; the current step is named but not a
    link. Day and Month mode carry the same trail, one level shorter.
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
    day = yesterday.isoformat()

    item_html = client.get(
        f"/review?mode=item&item=espresso-latte&start={day}&end={day}"
    ).text
    crumbs = item_html.split("<!--section:breadcrumb-->")[1].split(
        "<!--/section:breadcrumb-->"
    )[0]
    assert 'href="/"' in crumbs  # Review, the home crumb
    assert 'href="/review?mode=month&amp;month=2026-07"' in crumbs
    assert f'href="/review?mode=day&amp;day={day}"' in crumbs
    assert "Jul 2026" in crumbs
    assert "15 Jul" in crumbs
    assert "Espresso Latte" in crumbs  # the current step, named

    day_html = client.get(f"/review?mode=day&day={day}").text
    day_crumbs = day_html.split("<!--section:breadcrumb-->")[1].split(
        "<!--/section:breadcrumb-->"
    )[0]
    assert 'href="/"' in day_crumbs
    assert 'href="/review?mode=month&amp;month=2026-07"' in day_crumbs
    assert "15 Jul" in day_crumbs

    month_html = client.get("/review?mode=month&month=2026-07").text
    month_crumbs = month_html.split("<!--section:breadcrumb-->")[1].split(
        "<!--/section:breadcrumb-->"
    )[0]
    assert 'href="/"' in month_crumbs
    assert "Jul 2026" in month_crumbs

    # A multi-day item drill steps back to its period, not to a single day.
    start = yesterday - timedelta(days=6)
    range_html = client.get(
        f"/review?mode=item&item=espresso-latte"
        f"&start={start.isoformat()}&end={day}"
    ).text
    range_crumbs = range_html.split("<!--section:breadcrumb-->")[1].split(
        "<!--/section:breadcrumb-->"
    )[0]
    assert (
        f'href="/review?mode=period&amp;start={start.isoformat()}&amp;end={day}"'
        in range_crumbs
    )


def test_partner_drills_month_to_day_to_item_via_the_pages_own_links(
    tmp_path: Path, today: date
) -> None:
    """Issue #31's end-to-end walk: month → worst day → the item behind it.

    Every step follows a link the previous page rendered (never a hand-built
    URL), so the test proves the zoom is navigable: the July month view
    links to 10 Jul's day review, whose bottom-by-margin list links to the
    loss-making Chang draft's performance view — breadcrumb, period numbers,
    and the edit-recipe link to its SKU in Admin.
    """
    bad_day = date(2026, 7, 10)
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="chang-draft-500",
            day=bad_day,
            price="20",  # sold below its 35 THB pour cost all day
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="chang-draft-500",
            day=bad_day,
            price="20",
            line_id="li-2",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    # Zoom 1: the month view carries a link to the bad day.
    month_html = client.get("/review?mode=month&month=2026-07").text
    day_url = f"/review?mode=day&day={bad_day.isoformat()}"
    assert f'href="{day_url.replace("&", "&amp;")}"' in month_html

    # Zoom 2: the day view's rankings link to the item's performance.
    day_html = client.get(day_url).text
    item_url = (
        f"/review?mode=item&item=chang-draft-500"
        f"&start={bad_day.isoformat()}&end={bad_day.isoformat()}"
    )
    assert f'href="{item_url.replace("&", "&amp;")}"' in day_html

    # Zoom 3: the item view — breadcrumb, period numbers, edit-recipe link.
    item_html = client.get(item_url).text
    crumbs = item_html.split("<!--section:breadcrumb-->")[1].split(
        "<!--/section:breadcrumb-->"
    )[0]
    assert 'href="/"' in crumbs
    assert 'href="/review?mode=month&amp;month=2026-07"' in crumbs
    assert f'href="{day_url.replace("&", "&amp;")}"' in crumbs
    assert "Chang Draft 500ml" in crumbs

    perf = item_html.split("<!--section:item-performance-->")[1].split(
        "<!--/section:item-performance-->"
    )[0]
    assert "40.00" in perf   # revenue: 2 x 20
    assert "70.00" in perf   # COGS: 2 x 35
    assert "-30.00" in perf  # gross margin: underwater
    assert "-75.00" in perf  # margin %

    assert 'href="/skus/chang-draft-500"' in item_html
    assert "edit recipe" in item_html.lower()


def test_period_mode_rejects_a_malformed_or_backwards_range(
    tmp_path: Path, today: date
) -> None:
    """Bad ranges are client errors, not misleading zero-filled reports."""
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)

    assert client.get("/review?mode=period&start=nope&end=2026-07-15").status_code == 400
    assert client.get("/review?mode=period&start=2026-07-15").status_code == 400
    assert (
        client.get(
            "/review?mode=period&start=2026-07-15&end=2026-07-01"
        ).status_code
        == 400
    )


def test_period_range_nav_dims_at_the_bounds_of_synced_sales(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Period prev/next arrows dim when the range hits the synced bounds.

    Earliest sale day and yesterday (latest reviewable) are the ends — the
    same bound rule the Today day-nav uses (#45), applied to a sliding
    period window (#47). A mid-range window offers live arrows both ways.
    """
    earliest = date(2026, 6, 1)
    sales = [
        _sale_record(
            receipt_number="r-1",
            item_id="espresso-latte",
            day=earliest,
            price="120",
        ),
        _sale_record(
            receipt_number="r-2",
            item_id="espresso-latte",
            day=yesterday,
            price="120",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    # Mid-range 7-day window: both arrows are live links that slide by 7 days.
    mid_end = yesterday - timedelta(days=7)
    mid_start = mid_end - timedelta(days=6)
    mid = client.get(
        f"/review?mode=period&start={mid_start.isoformat()}&end={mid_end.isoformat()}"
    ).text
    mid_nav = mid.split("<!--section:range-nav-->")[1].split(
        "<!--/section:range-nav-->"
    )[0]
    assert "range-nav__arrow--dimmed" not in mid_nav
    prev_start = (mid_start - timedelta(days=7)).isoformat()
    prev_end = (mid_end - timedelta(days=7)).isoformat()
    next_start = (mid_start + timedelta(days=7)).isoformat()
    next_end = (mid_end + timedelta(days=7)).isoformat()
    assert (
        f'href="/review?mode=period&amp;start={prev_start}&amp;end={prev_end}"'
        in mid_nav
    )
    assert (
        f'href="/review?mode=period&amp;start={next_start}&amp;end={next_end}"'
        in mid_nav
    )

    # Window ending on yesterday: next dims (cannot step into today).
    at_end = client.get(
        f"/review?mode=period"
        f"&start={(yesterday - timedelta(days=6)).isoformat()}"
        f"&end={yesterday.isoformat()}"
    ).text
    end_nav = at_end.split("<!--section:range-nav-->")[1].split(
        "<!--/section:range-nav-->"
    )[0]
    assert "range-nav__arrow--next range-nav__arrow--dimmed" in end_nav or (
        "range-nav__arrow--dimmed" in end_nav
        and 'aria-disabled="true"' in end_nav
    )


def test_reports_pages_mark_bottom_nav_reports_active(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """Period / Month / Trends light the REPORTS bottom-nav cell (#47)."""
    app = _build_app(tmp_path, today=today, sales=[])
    client = _authed_client(app)
    start = yesterday - timedelta(days=6)

    for url in (
        f"/review?mode=period&start={start.isoformat()}&end={yesterday.isoformat()}",
        "/review?mode=month&month=2026-07",
        "/review?mode=trends",
    ):
        html = client.get(url).text
        nav = html.split("<!--section:bottom-nav-->")[1].split(
            "<!--/section:bottom-nav-->"
        )[0]
        assert "tb-bottomnav__cell--active" in nav
        assert "Reports" in nav
        # The active cell is the Reports one (tangerine), not Today.
        reports_cell = [
            cell for cell in nav.split("<a ") if "Reports" in cell
        ][0]
        assert "tb-bottomnav__cell--active" in reports_cell
