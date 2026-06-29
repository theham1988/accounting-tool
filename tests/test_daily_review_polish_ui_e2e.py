"""End-to-end UI seam for the daily-review polish (Wave 1, Slice 5).

Slice 5 adds the small UX interactions that make the daily-review surface
pleasant for two weeks of dogfooding, and turns silent failures into readable
error states:

  - **Day navigation**: a date input HTMX-GETs ``/review?day=YYYY-MM-DD`` and
    swaps the review body. Future / out-of-range dates surface a readable
    "no data for that day" state, not a broken page.
  - **Empty-store state**: a fresh store (never synced) shows a friendly empty
    state with a prominent "Sync now" button.
  - **Stale-data banner**: when the last successful sync is more than 24 hours
    old, a banner at the top offers "Sync now".
  - **Last-sync indicator**: the most recent successful sync's timestamp is
    visible so a partner can tell at a glance whether the data is fresh.
  - **Actionable unmapped wording**: the needs-attention section's wording makes
    clear it is something to act on.

Per the PRD testing rules these tests parse the rendered HTML and assert on the
partner-visible content (text, the wired-up control, the banner) — never on
implementation details. The genuine boundary is HTTP via FastAPI's
``TestClient``; sales and the last-sync marker are seeded straight into the
SQLite store, exactly as Slice 3's sync writes them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tangerine.loyverse.store import MenuSnapshot, SaleRecord
from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.types import Money, Sale

# --- config + auth helpers (mirror the slice-2/3 UI seam helpers) ------------

_TEST_PASSPHRASE = "slice5-test-passphrase"
_TEST_SIGNING_SECRET = "slice5-test-signing-secret"


def _seeded_recipes_yaml() -> str:
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


def _seeded_costs_yaml() -> str:
    return """
costs:
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }
  beans-arabica: { price: "2", updated_at: "2026-06-01" }
  milk-fresh: { price: "0.025", updated_at: "2026-06-01" }
"""


def _seeded_assignees_yaml() -> str:
    return """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
"""


def _write_config(tmp_path: Path, *, recipes_yaml: str | None = None) -> tuple[str, str, str]:
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(recipes_yaml or _seeded_recipes_yaml(), encoding="utf-8")
    costs.write_text(_seeded_costs_yaml(), encoding="utf-8")
    assignees.write_text(_seeded_assignees_yaml(), encoding="utf-8")
    return str(recipes), str(costs), str(assignees)


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


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _authed_client(app):  # type: ignore[no-untyped-def]
    """A ``TestClient`` already logged in as ``daniel`` (slice-4 gate)."""
    from tangerine.web.auth import SESSION_COOKIE

    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies, "test login did not set a session cookie"
    return client


def _build_app(
    tmp_path: Path,
    *,
    today: date,
    sales: list[SaleRecord] | None = None,
    last_sync_at: datetime | None = None,
    now_epoch: int | None = None,
    recipes_yaml: str | None = None,
):  # type: ignore[no-untyped-def]
    """Build the app over a SQLite DB pre-seeded with ``sales`` and an optional
    last-sync marker.

    ``last_sync_at`` is seeded by recording an (empty) menu snapshot at that
    instant — the same write a real sync performs — so the store's
    ``last_sync_at()`` returns it. ``now_epoch`` pins "now" for the staleness
    check (and for the auth clock, kept consistent with login).
    """
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_config(
        tmp_path, recipes_yaml=recipes_yaml
    )
    store = SqliteLoyverseStore.connect(db_path)
    if sales:
        store.record_sales(sales)
    if last_sync_at is not None:
        store.record_menu_snapshot(MenuSnapshot(items=()), at=last_sync_at)
    store.close()
    return create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        now_epoch=now_epoch,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )


def _section(html: str, anchor: str) -> str:
    start = f"<!--section:{anchor}-->"
    end = f"<!--/section:{anchor}-->"
    i = html.find(start)
    j = html.find(end)
    assert i != -1 and j != -1, f"section {anchor!r} not found in HTML"
    return html[i : j + len(end)]


@pytest.fixture
def yesterday() -> date:
    return date(2026, 6, 24)


@pytest.fixture
def today(yesterday: date) -> date:
    return yesterday + timedelta(days=1)


# --- AC: day-navigation control is wired to /review and swaps the body --------


def test_review_page_has_day_nav_control_wired_to_review_swapping_the_body(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The review page exposes a date input that HTMX-GETs ``/review`` and
    swaps a dedicated review-body region.

    Slice 5 AC: "Selecting a different date in the date input HTMX-swaps the
    review body to that day's review." A ``TestClient`` cannot run the HTMX JS,
    so this pins the wiring a browser needs: a ``type="date"`` input named
    ``day`` that ``hx-get``s ``/review`` and targets a ``#review-body`` element
    that exists on the page (the swap destination). Its value defaults to the
    day currently shown (yesterday), so the control reflects where the partner
    is.
    """
    sales = [
        _sale_record(
            receipt_number="5-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    nav = _section(html, "day-nav")
    # A date input the partner picks a day from.
    assert 'type="date"' in nav
    assert 'name="day"' in nav
    # Wired to GET /review via HTMX.
    assert "hx-get" in nav and "/review" in nav
    # The swap destination exists on the page and is what the control targets.
    assert 'id="review-body"' in html
    assert "review-body" in nav  # hx-target / hx-select reference the body
    # The control reflects the day currently shown (yesterday).
    assert yesterday.isoformat() in nav


def test_review_route_response_carries_the_day_in_the_review_body(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """``GET /review?day=`` returns that day's numbers inside ``#review-body``.

    The day-nav control extracts ``#review-body`` from the ``/review`` response
    and swaps it in, so the per-day content a partner sees after picking a date
    must live inside that region. Worked example: two days ago had revenue 240
    (chang + latte); the ``/review`` body for that day carries 240.00.
    """
    two_days_ago = yesterday - timedelta(days=1)
    sales = [
        _sale_record(
            receipt_number="5-a",
            item_id="chang-draft-500",
            day=two_days_ago,
            price="120",
        ),
        _sale_record(
            receipt_number="5-b",
            item_id="espresso-latte",
            day=two_days_ago,
            price="120",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/review", params={"day": two_days_ago.isoformat()})

    assert response.status_code == 200
    html = response.text
    assert 'id="review-body"' in html
    body = _review_body(html)
    assert two_days_ago.isoformat() in body
    assert "240.00" in body


# --- AC (enabler): the HTMX library is actually loaded ------------------------


def test_review_page_loads_the_htmx_library(tmp_path: Path, today: date) -> None:
    """The review page loads the HTMX library so its ``hx-*`` interactions work.

    Day navigation and the Sync-now button are both HTMX behaviours; without the
    library loaded they are inert markup. This pins that a ``<script>`` actually
    pulls HTMX in, with a Subresource-Integrity hash so the CDN asset is
    tamper-checked.
    """
    import re

    app = _build_app(tmp_path, today=today, sales=None)
    client = _authed_client(app)

    html = client.get("/").text

    match = re.search(r'<script[^>]+src=["\'][^"\']*htmx[^"\']*["\'][^>]*>', html)
    assert match is not None, "review page must load the htmx library"
    # The CDN script is integrity-checked.
    assert "integrity=" in match.group(0)


# --- AC: the unmapped / needs-attention wording is actionable ------------------


def test_needs_attention_wording_is_actionable(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The needs-attention section tells the partner what to *do*, not just that
    something is wrong.

    Slice 5 AC: items sold without a recipe mapping are "already surfaced as
    unmapped_items, but the section's wording should make clear it's
    actionable." An item with no recipe is revenue the tool cannot cost; the
    fix is to map it to a recipe in config. The section names the item and tells
    the partner to map it.

    Worked example. Yesterday a mapped chang and an unmapped "mystery-mocktail"
    were sold. The unmapped item surfaces in needs-attention, and the wording
    points the partner at mapping it to a recipe.
    """
    sales = [
        _sale_record(
            receipt_number="5-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        ),
        _sale_record(
            receipt_number="5-2",
            item_id="mystery-mocktail",
            day=yesterday,
            price="200",
        ),
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    needs = _section(response.text, "needs-attention").lower()
    assert "mystery-mocktail" in needs
    # Actionable: an explicit imperative telling the partner the corrective
    # action (map them to a recipe in config) — not merely that the items are
    # uncostable. The phrase is distinctive so the assertion cannot pass on the
    # incidental "unmapped" / "no recipe mapping" substrings already present.
    assert "map them to a recipe" in needs


# --- AC: the last successful sync timestamp is visible -------------------------


def test_last_sync_timestamp_is_visible_on_the_page(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """The most recent successful sync's timestamp is shown on the review page.

    Slice 5 AC: "The last successful sync's timestamp is visible somewhere on
    the review page." A partner scanning the page at 9am can tell at a glance
    whether they are looking at fresh data.

    Worked example. The last sync ran 2026-06-24 22:30 UTC and "now" is the
    next morning (within 24h, so no stale banner) — the page shows that
    timestamp.
    """
    now = datetime(2026, 6, 25, 9, 0, tzinfo=timezone.utc)
    last_sync = datetime(2026, 6, 24, 22, 30, tzinfo=timezone.utc)
    sales = [
        _sale_record(
            receipt_number="5-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        )
    ]
    app = _build_app(
        tmp_path,
        today=today,
        sales=sales,
        last_sync_at=last_sync,
        now_epoch=_epoch(now),
    )
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    indicator = _section(response.text, "last-sync")
    # The synced date is shown (the exact time format is incidental).
    assert "2026-06-24" in indicator


def test_last_sync_indicator_reads_never_when_no_sync_has_run(
    tmp_path: Path, today: date
) -> None:
    """When nothing has been synced, the indicator reads "never" rather than
    rendering a blank or a broken date.

    A fresh store has no last-sync timestamp; the indicator must still render a
    sensible value so the partner understands why the page is empty.
    """
    app = _build_app(tmp_path, today=today, sales=None)  # empty / never synced
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    indicator = _section(response.text, "last-sync").lower()
    assert "never" in indicator


# --- AC: stale-data banner when the last sync is more than 24h old ------------


def test_stale_data_banner_appears_when_last_sync_is_old(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """A banner appears at the top when the last sync is more than 24h old.

    Slice 5 AC: "A stale-data banner appears at the top of the review when the
    last sync was more than 24 hours ago, with a 'Sync now' affordance." This
    is the 9:01am recovery cue — the partner opens the tool, sees the data is
    stale (the cron must have failed overnight), and can force a sync without
    hunting for the button.

    Worked example. "Now" is 2026-06-26 09:00 UTC; the last successful sync was
    2026-06-23 22:30 UTC — about 2.5 days ago. The banner shows, names how long
    ago, and carries a Sync-now affordance.
    """
    now = datetime(2026, 6, 26, 9, 0, tzinfo=timezone.utc)
    last_sync = datetime(2026, 6, 23, 22, 30, tzinfo=timezone.utc)
    sales = [
        _sale_record(
            receipt_number="5-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        )
    ]
    app = _build_app(
        tmp_path,
        today=today,
        sales=sales,
        last_sync_at=last_sync,
        now_epoch=_epoch(now),
    )
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    banner = _section(response.text, "stale-banner")
    # It is recognisably a "data is old" message with a day count.
    assert "last sync" in banner.lower()
    assert "2" in banner and "day" in banner.lower()
    # It carries a Sync-now affordance that POSTs to /sync.
    assert "hx-post" in banner and "/sync" in banner
    assert "sync now" in banner.lower()


def test_no_stale_banner_when_last_sync_is_recent(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """No stale banner when the last sync is within the last 24 hours.

    The banner is an exception state, not chrome — when the nightly sync ran on
    schedule the partner should not be nagged. "Now" is 2026-06-25 09:00 UTC
    and the last sync was 2 hours earlier, so no banner.
    """
    now = datetime(2026, 6, 25, 9, 0, tzinfo=timezone.utc)
    last_sync = now - timedelta(hours=2)
    sales = [
        _sale_record(
            receipt_number="5-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        )
    ]
    app = _build_app(
        tmp_path,
        today=today,
        sales=sales,
        last_sync_at=last_sync,
        now_epoch=_epoch(now),
    )
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "<!--section:stale-banner-->" not in response.text


# --- AC: an empty store shows a friendly first-run state ----------------------


def test_empty_store_shows_friendly_state_with_prominent_sync_button(
    tmp_path: Path, today: date
) -> None:
    """A store that has never been synced shows a friendly empty state with a
    prominent "Sync now" button.

    Slice 5 AC: "The empty-store state shows a friendly message and a prominent
    'Sync now' button." On a fresh install there is nothing to review; rather
    than a page of zeros that looks broken, the partner gets a clear first-run
    call to action that POSTs to ``/sync``.
    """
    app = _build_app(tmp_path, today=today, sales=None)  # empty store
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    empty = _section(html, "empty-store")
    # A friendly message (not a stack trace, not silence).
    assert "sync" in empty.lower()
    # A prominent button that POSTs to /sync.
    assert "hx-post" in empty and "/sync" in empty
    assert "sync now" in empty.lower()
    # This is the whole-store empty state, so the per-day "no data" note must
    # NOT also be shown (they are mutually exclusive).
    assert "<!--section:no-day-data-->" not in html


# --- AC: a day with no data shows a readable state, not a broken page ---------


def test_navigating_to_an_empty_day_shows_readable_no_data_state(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """A day the store has no sales for renders a readable "no data" state.

    Slice 5 AC: "A date with no data shows a readable empty state, not a broken
    page." The store has yesterday's sales, but the partner navigates back to a
    day with nothing recorded. The page returns 200 and surfaces a clear
    per-day "no data" note inside the review body — it is NOT the whole-store
    empty state (the store does have data, just not for this day).
    """
    empty_day = yesterday - timedelta(days=10)
    sales = [
        _sale_record(
            receipt_number="5-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/review", params={"day": empty_day.isoformat()})

    assert response.status_code == 200
    html = response.text
    # A readable per-day "no data" state is present.
    no_data = _section(html, "no-day-data").lower()
    assert "no data" in no_data
    # The page is intact, not broken.
    assert "Daily 9am review" in html
    # This is NOT the whole-store empty state — the store has data elsewhere.
    assert "<!--section:empty-store-->" not in html


def test_future_date_shows_readable_no_data_state(
    tmp_path: Path, yesterday: date, today: date
) -> None:
    """A future date surfaces the same readable "no data" state, not a 500.

    Slice 5 AC: "Future and out-of-range dates surface a readable 'no data for
    that day' state rather than an empty page." A partner can fat-finger a
    future date in the picker; the tool must answer with a calm "nothing here"
    rather than an error.
    """
    future_day = today + timedelta(days=5)
    sales = [
        _sale_record(
            receipt_number="5-1",
            item_id="chang-draft-500",
            day=yesterday,
            price="120",
        )
    ]
    app = _build_app(tmp_path, today=today, sales=sales)
    client = _authed_client(app)

    response = client.get("/review", params={"day": future_day.isoformat()})

    assert response.status_code == 200
    no_data = _section(response.text, "no-day-data").lower()
    assert "no data" in no_data


def _review_body(html: str) -> str:
    """Slice out the ``#review-body`` element so assertions pin per-day content.

    The day-nav control swaps this element; pinning it keeps the test honest
    that the per-day content (and not just somewhere on the page) carries the
    right numbers.
    """
    i = html.find('id="review-body"')
    assert i != -1, "no #review-body element in response"
    start = html.rfind("<", 0, i)
    # Balanced-div scan from the opening tag.
    depth = 0
    pos = start
    while pos < len(html):
        nxt_open = html.find("<div", pos)
        nxt_close = html.find("</div>", pos)
        if nxt_close == -1:
            break
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            pos = nxt_open + 4
        else:
            depth -= 1
            pos = nxt_close + 6
            if depth == 0:
                return html[start:pos]
    return html[start:]
