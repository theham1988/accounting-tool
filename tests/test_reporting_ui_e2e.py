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

    July has 31 days, so the target line reads 310,000.00 THB. Until fixed
    costs land (slice 3) the comparison is honestly labelled gross-margin-
    based — the page says so and never claims net profit (issue #29 AC).
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
    assert "gross margin" in html.lower()
    assert "net-profit" in html or "net profit" in html  # ...named as NOT that

    assert client.get("/review?mode=month&month=2026-13").status_code == 400
    assert client.get("/review?mode=month").status_code == 400


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
    """Switching modes from a period/month page keeps the partner's context.

    On a period page ending at E, the switcher's Day link goes to E's day
    review and Month to E's month — moving between modes feels like zooming
    the same report (PRD user story 5), and every step is a URL.
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
    assert f"/review?mode=day&amp;day={yesterday.isoformat()}" in switcher
    assert "/review?mode=month&amp;month=2026-07" in switcher

    month_html = client.get("/review?mode=month&month=2026-07").text
    month_switcher = month_html.split("<!--section:mode-switcher-->")[1].split(
        "<!--/section:mode-switcher-->"
    )[0]
    assert "/review?mode=day&amp;day=2026-07-31" in month_switcher


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
