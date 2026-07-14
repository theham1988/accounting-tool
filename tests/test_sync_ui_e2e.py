"""End-to-end UI seam for the Loyverse sync (Wave 1, Slice 3).

Exercises the ``POST /sync`` route and the ``python -m tangerine.sync`` script
through the same genuine external boundary as slice 02's Loyverse seam — the
HTTP ``urlopen`` callable, injected via the existing ``StubHttp`` pattern. The
SQLite store and config loader run for real; only Loyverse's HTTPS endpoint is
stubbed.

Per the PRD testing rules these tests read like worked examples and assert on
partner-visible artefacts — the persisted sales, the rendered result fragment,
the headline numbers refreshed out-of-band — never on implementation details
(how the route is wired, which function computed a count).

Scope (slice 3 only):
  - ``POST /sync`` triggers a real Loyverse sync against a stubbed endpoint and
    writes results into the SQLite store from slice 1.
  - The response is an HTML fragment describing rows ingested, menu changes,
    and any errors.
  - First sync backfills the last 30 days; subsequent syncs do not.
  - Re-running a sync does not double-count (idempotent on
    ``(receipt_number, line_id)``).
  - A Loyverse auth error surfaces a readable message in the fragment rather
    than crashing the app.
  - ``python -m tangerine.sync`` runs the same sync from the command line.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tangerine.storage.sqlite_store import SqliteLoyverseStore


# --- helpers: build synthetic Loyverse payloads -----------------------------
# Mirrors the helpers in ``tests/test_loyverse_sync_e2e.py``. The slice-02 file
# owns the canonical copies; these local mirrors keep this slice's tests
# self-contained and readable as worked examples without coupling two test
# files at the import level.


def _receipt_json(
    *,
    receipt_number: str,
    created_at: str,
    receipt_type: str = "SALE",
    line_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One minimal Loyverse receipt payload (shape from the API docs)."""
    item_id = "d5fe0da6-44b3-4633-9915-e9dc5118cbfc"
    return {
        "receipt_number": receipt_number,
        "receipt_type": receipt_type,
        "refund_for": None,
        "created_at": created_at,
        "receipt_date": created_at,
        "total_money": 120,
        "total_tax": 0,
        "line_items": line_items
        or [
            {
                "id": "li-1",
                "item_id": item_id,
                "variant_id": "v-1",
                "item_name": "Chang Draft 500ml",
                "sku": "chang-draft-500",
                "quantity": 1,
                "price": 120,
                "total_money": 120,
            }
        ],
    }


def _item_json(
    *,
    item_id: str,
    name: str,
    sku: str,
    price: float,
    category_id: str = "cat-bar",
) -> dict[str, Any]:
    """Real field names, not guessed ones — mirrors test_loyverse_sync_e2e.py."""
    return {
        "id": item_id,
        "item_name": name,
        "category_id": category_id,
        "sku": sku,
        "variants": [
            {
                "variant_id": f"{item_id}-v1",
                "option1_value": name,
                "sku": sku,
                "default_price": price,
            }
        ],
    }


def _envelope(items: list[dict[str, Any]], cursor: str | None = None) -> bytes:
    return json.dumps({"items": items, "cursor": cursor}).encode("utf-8")


def _receipts_envelope(
    receipts: list[dict[str, Any]], cursor: str | None = None
) -> bytes:
    return json.dumps({"receipts": receipts, "cursor": cursor}).encode("utf-8")


class StubResponse:
    """Minimal stand-in for an HTTPResponse for the urlopen seam."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._buf = io.BytesIO(body)
        self.status = status

    @property
    def status_code(self) -> int:  # pragma: no cover - trivial
        return self.status

    def read(self, amt: int = -1) -> bytes:
        return self._buf.read(-1 if amt is None else amt)

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self._buf.close()


class StubHttp:
    """Records the requests the client made and serves canned pages by path.

    Mirrors slice 02's class so the Loyverse HTTP boundary is the only seam
    stubbed here; everything beyond ``urlopen`` runs for real.
    """

    def __init__(self, routes: dict[str, list[bytes]]) -> None:
        # routes: path -> list of response bodies (pages), popped in order.
        self._routes = {k: list(v) for k, v in routes.items()}
        self.requests: list[tuple[str, dict[str, str] | None, dict[str, Any]]] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> StubResponse:
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        self.requests.append((url, headers, dict(params or {})))
        pages = self._routes.get(path)
        if pages is None:
            raise AssertionError(f"unexpected request to {url!r}")
        if not pages:
            raise AssertionError(f"ran out of pages for {path!r}")
        return StubResponse(pages.pop(0))


# --- shared app / config helpers --------------------------------------------


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
    """The Wave 1 default partners (mirrors ``config/assignees.yaml``)."""
    return """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
"""


def _write_config(tmp_path: Path) -> tuple[str, str, str]:
    """Write recipes + costs + assignees YAML; return their paths.

    Slice 4 added the auth gate (the assignees file backs the role selector),
    so the slice-3 builders thread the assignees path through and the sync
    tests run unchanged against the gate.
    """
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(_seeded_recipes_yaml(), encoding="utf-8")
    costs.write_text(_seeded_costs_yaml(), encoding="utf-8")
    assignees.write_text(_seeded_assignees_yaml(), encoding="utf-8")
    return str(recipes), str(costs), str(assignees)


#: Stable passphrase + signing secret for the slice-3 suite. Slice 4 added the
#: auth gate; rather than rewriting every test, the builders inject these
#: explicitly so no test mutates the process environment.
_TEST_PASSPHRASE = "slice3-test-passphrase"
_TEST_SIGNING_SECRET = "slice3-test-signing-secret"


def _authed_client(app):  # type: ignore[no-untyped-def]
    """A ``TestClient`` that has already logged in as ``daniel``.

    Slice 4 gates ``/`` and ``/sync`` behind a signed-cookie session. The
    slice-3 tests assert on the sync result fragment and the persisted sales —
    they do not care about auth themselves, so this helper performs the login
    dance once and hands back a ready-to-use authenticated client.
    """
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
def today() -> date:
    """Fixed "today" so first-sync backfill math is deterministic in tests."""
    return date(2026, 6, 25)


def _build_app(
    tmp_path: Path,
    *,
    today: date,
    urlopen: Any,
    loyverse_token: str = "tok-secret",
    loyverse_store_id: str | None = "store-1",
) -> Any:
    """Build the FastAPI app with the Loyverse HTTP boundary stubbed.

    ``urlopen`` is wired through the app factory so the stubbed Loyverse
    endpoint is the only external boundary replaced; the SQLite store, config
    loader, parser, orchestrator, and template rendering all run for real.
    """
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_config(tmp_path)
    SqliteLoyverseStore.connect(db_path).close()
    return create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        loyverse_urlopen=urlopen,
        loyverse_access_token=loyverse_token,
        loyverse_store_id=loyverse_store_id,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )


def _section(html: str, anchor: str) -> str:
    """Return the HTML slice for a section delimited by anchor comments.

    Same convention as the slice-02 UI test — anchors are HTML comments so the
    tests don't depend on any particular tag structure or CSS class.
    """
    start = f"<!--section:{anchor}-->"
    end = f"<!--/section:{anchor}-->"
    i = html.find(start)
    j = html.find(end)
    assert i != -1 and j != -1, f"section {anchor!r} not found in HTML"
    return html[i : j + len(end)]


def _store_sales_count(db_path: str) -> int:
    """Read the persisted sales count straight from the SQLite DB file.

    The store is the genuine persistence layer; reading it back after a sync is
    the most direct way to assert what the sync wrote, without going through
    the route again.
    """
    store = SqliteLoyverseStore.connect(db_path)
    try:
        return len(store.sales())
    finally:
        store.close()


# --- AC: POST /sync runs a real Loyverse sync and writes into the store ------


def test_post_sync_writes_one_sale_into_store_and_fragment_reports_rows_ingested(
    tmp_path: Path, today: date
) -> None:
    """``POST /sync`` runs the orchestrator against the stubbed Loyverse
    endpoint, persists one sale into the SQLite store, and the response
    fragment reports ``1`` rows ingested.

    Worked example. The Loyverse endpoint returns one SALE receipt (Chang Draft
    500ml @ 120 THB). The sync writes it into the store; the result fragment
    surfaces ``1`` for the rows-ingested count so the partner can see the sync
    did something.
    """
    yesterday = today - timedelta(days=1)
    created_at = f"{yesterday.isoformat()}T12:00:00.000Z"
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [
                _receipts_envelope(
                    [_receipt_json(receipt_number="s3-1", created_at=created_at)],
                    cursor=None,
                )
            ],
            "/v1.0/items": [_envelope([], cursor=None)],
        }
    )

    db_path = str(tmp_path / "tangerine.db")
    app = _build_app(tmp_path, today=today, urlopen=stub)
    client = _authed_client(app)

    response = client.post("/sync")

    assert response.status_code == 200
    # The sale was persisted into the SQLite store.
    assert _store_sales_count(db_path) == 1
    # The result fragment surfaces the rows-ingested count.
    assert "1" in _section(response.text, "sync-result")


# --- AC: the result fragment reports menu changes -----------------------------


def test_post_sync_fragment_reports_menu_changes(
    tmp_path: Path, today: date
) -> None:
    """The result fragment reports how many menu changes the sync recorded.

    Worked example. The Loyverse ``/items`` endpoint returns one new item
    (Chang Draft). Because the store starts empty, the first snapshot records
    an ``ADDED`` change for that item -> exactly one menu change. The fragment
    surfaces ``1`` for the menu-changes count so a partner can see the menu
    history move alongside the sales count.
    """
    yesterday = today - timedelta(days=1)
    created_at = f"{yesterday.isoformat()}T12:00:00.000Z"
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [
                _receipts_envelope(
                    [_receipt_json(receipt_number="s3-mc-1", created_at=created_at)],
                    cursor=None,
                )
            ],
            "/v1.0/items": [
                _envelope(
                    [_item_json(item_id="i-1", name="Chang Draft", sku="chang-draft-500", price=120)],
                    cursor=None,
                )
            ],
        }
    )

    app = _build_app(tmp_path, today=today, urlopen=stub)
    client = _authed_client(app)

    response = client.post("/sync")

    assert response.status_code == 200
    fragment = _section(response.text, "sync-result")
    # One item added on the first snapshot -> one menu change recorded.
    assert "1" in fragment
    # The label is named so a reader can tell which count is which.
    assert "menu changes" in fragment.lower()


# --- AC: first sync backfills the last 30 days; subsequent syncs do not -------


def test_first_sync_passes_created_at_min_around_30_days_back(
    tmp_path: Path, today: date
) -> None:
    """The first sync (empty sales table) passes ``created_at_min`` to the
    receipts endpoint, set to roughly 30 days before today.

    PRD user story 9: "the first sync backfills the last 30 days of sales, so
    that the 7-day rolling average has data immediately rather than reporting
    zeros for the first week."

    Worked example. Today is 2026-06-25; the store is empty (no prior sync).
    The first ``POST /sync`` calls Loyverse's ``/receipts`` with a
    ``created_at_min`` query param around ``2026-05-26`` (today minus 30 days).
    The stub records every request the client made, so we assert the param
    was sent.
    """
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [_receipts_envelope([], cursor=None)],
            "/v1.0/items": [_envelope([], cursor=None)],
        }
    )

    app = _build_app(tmp_path, today=today, urlopen=stub)
    client = _authed_client(app)

    client.post("/sync")

    # The receipts request carried a created_at_min query param, dated around
    # today minus 30 days (the exact day is a config constant, so we only
    # assert it's an ISO timestamp in the right month window).
    receipts_requests = [
        r for r in stub.requests if "/v1.0/receipts" in r[0]
    ]
    assert receipts_requests, "sync never called /receipts"
    params = receipts_requests[0][2]
    assert "created_at_min" in params, (
        "first sync (empty store) must pass created_at_min to backfill"
    )
    # The value is an ISO-8601 timestamp ~30 days before today.
    value = params["created_at_min"]
    backfill_day = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    expected = today - timedelta(days=30)
    assert backfill_day == expected, (
        f"first sync backfill window is {backfill_day}, expected {expected}"
    )


def test_subsequent_sync_does_not_pass_created_at_min(
    tmp_path: Path, today: date
) -> None:
    """Once the store has sales, subsequent syncs pull everything (no date
    filter). Idempotency at the store handles overlap.

    Without this rule a nightly cron would only ever pull the last 30 days,
    losing any older data; with it, the cron is a full pull and the store's
    primary key dedupes the replayed receipts.

    The "first sync" signal is an empty sales table, so this test seeds one
    sale on the first sync (making the table non-empty) and then asserts the
    second sync sends no ``created_at_min``.
    """
    yesterday = today - timedelta(days=1)
    created_at = f"{yesterday.isoformat()}T12:00:00.000Z"
    stub = StubHttp(
        routes={
            # First sync: one receipt (so the store becomes non-empty).
            # Second sync: empty page (the cron's normal "nothing new" case).
            "/v1.0/receipts": [
                _receipts_envelope(
                    [_receipt_json(receipt_number="s3-bs-1", created_at=created_at)],
                    cursor=None,
                ),
                _receipts_envelope([], cursor=None),
            ],
            "/v1.0/items": [_envelope([], cursor=None), _envelope([], cursor=None)],
        }
    )

    app = _build_app(tmp_path, today=today, urlopen=stub)
    client = _authed_client(app)

    # First sync: empty store -> backfill window sent; one sale ingested.
    client.post("/sync")
    first_params = [
        r for r in stub.requests if "/v1.0/receipts" in r[0]
    ][0][2]
    assert "created_at_min" in first_params

    # Second sync: store now non-empty -> no date filter.
    client.post("/sync")
    receipts_requests = [
        r for r in stub.requests if "/v1.0/receipts" in r[0]
    ]
    second_params = receipts_requests[1][2]
    assert "created_at_min" not in second_params, (
        "subsequent syncs must not pass created_at_min "
        "(idempotency handles the overlap without a date filter)"
    )


# --- AC: re-running a sync does not duplicate sales ---------------------------


def test_replayed_sync_does_not_double_count_sales(
    tmp_path: Path, today: date
) -> None:
    """Re-running a sync (manual press after cron, or overlapping page ranges)
    never double-counts a sale.

    PRD user story 10: "sync runs are idempotent." The store dedupes on
    ``(receipt_number, line_id)`` via its primary key (slice 1), so replaying
    the same receipts page produces no new sales.

    Worked example. Loyverse returns the same single receipt on both syncs.
    After the first sync the store has 1 sale; after the second sync it still
    has 1 sale, and the second sync's fragment reports 0 rows ingested (the
    dedup is visible to the partner, not silent).
    """
    yesterday = today - timedelta(days=1)
    created_at = f"{yesterday.isoformat()}T12:00:00.000Z"
    page = _receipts_envelope(
        [_receipt_json(receipt_number="s3-idem-1", created_at=created_at)],
        cursor=None,
    )
    empty_items = _envelope([], cursor=None)
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [page, page],
            "/v1.0/items": [empty_items, empty_items],
        }
    )

    db_path = str(tmp_path / "tangerine.db")
    app = _build_app(tmp_path, today=today, urlopen=stub)
    client = _authed_client(app)

    first = client.post("/sync")
    second = client.post("/sync")

    # Store has exactly one sale after both syncs — not two.
    assert _store_sales_count(db_path) == 1
    # First sync ingested 1; second sync ingested 0 (dedup is surfaced, not silent).
    assert "1" in _section(first.text, "sync-result")
    second_fragment = _section(second.text, "sync-result")
    # The rows-ingested count on the second sync is 0 (already-seen receipt deduped).
    assert (
        f'<dd class="sync-result__rows">0</dd>' in second_fragment
    ), "replayed sync must report 0 rows ingested (dedup visible to partner)"


# --- AC: a Loyverse auth error surfaces a readable message, not a crash -------


def test_post_sync_surfaces_auth_error_in_fragment_without_crashing(
    tmp_path: Path, today: date
) -> None:
    """A Loyverse auth failure (HTTP 401) surfaces as a readable error in the
    result fragment rather than crashing the app.

    PRD user story 7 + slice 03 AC: "A sync that hits a Loyverse auth error
    surfaces a readable error in the result fragment rather than crashing the
    app." The 9:01am recovery path must keep working even when the token has
    expired.

    The route returns 200 (not 500) with the error rendered in the fragment,
    the store is unchanged (no partial writes leaked through), and the
    fragment's rows-ingested count is 0.
    """
    from urllib.error import HTTPError

    def urlopen_401(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> StubResponse:
        raise HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"bad token"))

    db_path = str(tmp_path / "tangerine.db")
    app = _build_app(tmp_path, today=today, urlopen=urlopen_401)
    client = _authed_client(app)

    response = client.post("/sync")

    # 200, not 500 — the page must not crash on a Loyverse failure.
    assert response.status_code == 200
    fragment = _section(response.text, "sync-result")
    # The fragment flags that there were errors.
    assert 'data-sync-errors="true"' in fragment
    # The error message is human-readable and present in the fragment.
    assert "401" in fragment or "auth" in fragment.lower() or "token" in fragment.lower()
    # No sales leaked into the store.
    assert _store_sales_count(db_path) == 0


# --- AC: the response refreshes yesterday's headline numbers out-of-band ------


def test_post_sync_refreshes_yesterday_headline_numbers_out_of_band(
    tmp_path: Path, today: date
) -> None:
    """The ``/sync`` response carries an out-of-band element that HTMX swaps
    into the page to refresh yesterday's headline numbers, so the partner sees
    fresh revenue / COGS / gross-margin without a manual reload.

    Worked example. Yesterday one Chang Draft @ 120 was sold (cost 35). After
    the sync writes it into the store, the response's out-of-band element
    carries the refreshed headline numbers: revenue 120.00, gross-margin 85.00.

    The PRD's "Sync recovery" interaction says "the page is reloaded with
    fresh data"; the out-of-band swap is how that happens without a full
    navigation.
    """
    yesterday = today - timedelta(days=1)
    created_at = f"{yesterday.isoformat()}T12:00:00.000Z"
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [
                _receipts_envelope(
                    [_receipt_json(receipt_number="s3-oob-1", created_at=created_at)],
                    cursor=None,
                )
            ],
            "/v1.0/items": [_envelope([], cursor=None)],
        }
    )

    app = _build_app(tmp_path, today=today, urlopen=stub)
    client = _authed_client(app)

    response = client.post("/sync")

    assert response.status_code == 200
    html = response.text
    # The out-of-band element is present and marked for HTMX to swap.
    assert "hx-swap-oob" in html
    assert 'id="headline-oob"' in html
    # The refreshed headline numbers appear inside the OOB element. Yesterday's
    # sale was one Chang @ 120 (cost 35) -> revenue 120.00, gross margin 85.00.
    oob = _oob_block(html)
    assert "120.00" in oob  # revenue
    assert "85.00" in oob   # gross margin


def _oob_block(html: str) -> str:
    """Slice the out-of-band headline-refresh element out of the response."""
    i = html.find('id="headline-oob"')
    assert i != -1, "no #headline-oob out-of-band element in response"
    # Find the enclosing <div ...> ... </div>.
    start = html.rfind("<div", 0, i)
    end = html.find("</div>", i)
    assert start != -1 and end != -1, "malformed OOB element"
    return html[start : end + len("</div>")]


# --- AC: the "Sync now" button renders on the review page --------------------


def test_get_root_renders_sync_now_button_posting_to_sync(
    tmp_path: Path, today: date
) -> None:
    """The review page carries a "Sync now" button that POSTs to ``/sync`` and
    shows a "Syncing..." indicator while in flight.

    PRD user story 7 + slice 03 AC: "The button swaps to 'Syncing...' while in
    flight (HTMX indicator)." The button is the 9:01am recovery path — a
    partner notices stale numbers and forces a sync without leaving the page.
    """
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [_receipts_envelope([], cursor=None)],
            "/v1.0/items": [_envelope([], cursor=None)],
        }
    )
    app = _build_app(tmp_path, today=today, urlopen=stub)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    button = _section(html, "sync-control")
    # The button POSTs to /sync (HTMX).
    assert "hx-post" in button and "/sync" in button
    # It has an in-flight indicator that HTMX toggles while the request runs.
    assert "syncing" in button.lower() or "hx-indicator" in button
    # The result container the fragment swaps into exists on the page.
    assert "sync-result" in html.lower() or "sync-result" in button


def test_sync_now_footer_wires_to_sync_result_target(
    tmp_path: Path, today: date
) -> None:
    """Issue #45: the SYNC NOW footer POSTs to ``/sync`` and targets ``#sync-result``."""
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [_receipts_envelope([], cursor=None)],
            "/v1.0/items": [_envelope([], cursor=None)],
        }
    )
    app = _build_app(tmp_path, today=today, urlopen=stub)
    client = _authed_client(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    footer = _section(html, "sync-control")
    assert "sync-control--footer" in footer
    assert 'hx-target="#sync-result"' in footer
    assert html.count('id="sync-result"') == 1


# --- AC: python -m tangerine.sync runs the same sync from the command line ----


def test_sync_script_main_persists_sales_and_prints_summary(
    tmp_path: Path, today: date, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m tangerine.sync`` runs the same sync function as the route,
    persists sales into the SQLite store, and prints a one-line summary.

    Worked example. Loyverse returns one SALE receipt. The script writes it
    into the store and prints a summary naming the rows-ingested count, so a
    partner reading cron output (or running it by hand) can see what happened.

    The script's ``main()`` takes explicit ``db_path`` / paths / credentials
    / ``urlopen`` parameters (mirroring ``tangerine.__main__``) so this test
    drives it in-process without env mutation or subprocess overhead. The real
    ``python -m tangerine.sync`` entrypoint just calls ``main()`` with env
    defaults.
    """
    from tangerine.sync import main as sync_main

    yesterday = today - timedelta(days=1)
    created_at = f"{yesterday.isoformat()}T12:00:00.000Z"
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [
                _receipts_envelope(
                    [_receipt_json(receipt_number="s3-cli-1", created_at=created_at)],
                    cursor=None,
                )
            ],
            "/v1.0/items": [_envelope([], cursor=None)],
        }
    )

    db_path = str(tmp_path / "tangerine.db")
    # Touch the DB so the file exists (matches the route's setup).
    SqliteLoyverseStore.connect(db_path).close()

    sync_main(
        db_path=db_path,
        access_token="tok-secret",
        store_id="store-1",
        urlopen=stub,
        today=today,
    )

    # The sale was persisted into the SQLite store.
    assert _store_sales_count(db_path) == 1
    # A summary line was printed naming the rows-ingested count.
    out = capsys.readouterr().out
    assert "1" in out
    assert "sync" in out.lower()


def test_sync_script_main_surfaces_auth_error_without_crashing(
    tmp_path: Path, today: date, capsys: pytest.CaptureFixture[str]
) -> None:
    """The script surfaces a Loyverse auth error in its summary output rather
    than raising a traceback.

    Cron emails its owner the script's output; a raw traceback is useless at
    9:01am. The script catches the error and prints a readable line so the
    partner sees "auth failed" (or similar) and knows to refresh the token.
    """
    from urllib.error import HTTPError

    from tangerine.sync import main as sync_main

    def urlopen_401(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> StubResponse:
        raise HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"bad token"))

    db_path = str(tmp_path / "tangerine.db")
    SqliteLoyverseStore.connect(db_path).close()

    # Must not raise.
    sync_main(
        db_path=db_path,
        access_token="tok-secret",
        store_id="store-1",
        urlopen=urlopen_401,
        today=today,
    )

    out = capsys.readouterr().out
    # The summary surfaces the auth failure readably.
    assert "401" in out or "auth" in out.lower() or "token" in out.lower()
    # No sales leaked into the store.
    assert _store_sales_count(db_path) == 0


# --- AC: Loyverse credentials come from environment variables -----------------


def test_route_reads_loyverse_token_from_env(
    tmp_path: Path, today: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/sync`` route reads the Loyverse access token from
    ``$LOYVERSE_ACCESS_TOKEN`` (and the store id from ``$LOYVERSE_STORE_ID``)
    when no explicit credentials are passed.

    PRD user story 11: "Loyverse credentials live in an environment variable
    on the server, so that they are never in the database or the repo." The
    stub records the ``Authorization`` header the client sent; asserting it
    carries the env-sourced token proves the wiring.
    """
    yesterday = today - timedelta(days=1)
    created_at = f"{yesterday.isoformat()}T12:00:00.000Z"
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [
                _receipts_envelope(
                    [_receipt_json(receipt_number="s3-env-1", created_at=created_at)],
                    cursor=None,
                )
            ],
            "/v1.0/items": [_envelope([], cursor=None)],
        }
    )

    monkeypatch.setenv("LOYVERSE_ACCESS_TOKEN", "env-token-xyz")
    monkeypatch.setenv("LOYVERSE_STORE_ID", "env-store-9")

    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_config(tmp_path)
    SqliteLoyverseStore.connect(db_path).close()
    # No explicit token / store_id / urlopen passed except the stub boundary.
    app = create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        loyverse_urlopen=stub,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )
    client = _authed_client(app)

    client.post("/sync")

    # The client authenticated with the env-sourced token.
    receipts_requests = [r for r in stub.requests if "/v1.0/receipts" in r[0]]
    assert receipts_requests, "sync never called /receipts"
    headers = receipts_requests[0][1]
    assert headers is not None
    assert headers.get("Authorization") == "Bearer env-token-xyz"
    # And the request was scoped to the env-sourced store id.
    assert receipts_requests[0][2].get("store_id") == "env-store-9"


def test_route_without_credentials_surfaces_readable_error(
    tmp_path: Path, today: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no Loyverse token is configured (env unset, none passed), the
    route returns a readable error in the fragment rather than crashing.

    A misconfigured deploy (forgotten env var) must not produce a 500; the
    partner sees a message naming the missing env var so they know what to
    set. This is the "fail loudly" rule from the PRD, applied to runtime
    config as well as startup config.
    """
    monkeypatch.delenv("LOYVERSE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LOYVERSE_STORE_ID", raising=False)

    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_config(tmp_path)
    SqliteLoyverseStore.connect(db_path).close()
    app = create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today,
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )
    client = _authed_client(app)

    response = client.post("/sync")

    assert response.status_code == 200  # not 500
    fragment = _section(response.text, "sync-result")
    assert 'data-sync-errors="true"' in fragment
    # The error names the missing env var so the partner knows what to set.
    assert "LOYVERSE_ACCESS_TOKEN" in fragment
