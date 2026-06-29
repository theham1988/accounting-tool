"""End-to-end UI seam for the deploy-hardening slice (Wave 1, Slice 6).

This slice turns Wave 1 from "runs on a laptop" into "both partners open it
from home at 9am". Most of the slice is operations configuration (systemd,
nginx, certbot, cron, nightly snapshots) that lives in the repo under
``deploy/`` and ``DEPLOY.md``; that is exercised by deploying, not by pytest.

The two pieces that are *code* — and therefore tested here through the same
genuine HTTP boundary as the other UI seams (FastAPI's ``TestClient``) — are:

  - ``GET /admin/db-snapshot``: a login-gated route that downloads the current
    SQLite database for an out-of-band backup before risky maintenance
    (PRD user story 28).
  - **Login rate-limiting**: the shared passphrase cannot be brute-forced, so
    excess login attempts from one client return HTTP 429 (PRD user story 32).

Per the PRD's testing rules these tests assert on partner-visible behaviour —
status codes, the downloaded bytes, the ``Content-Disposition`` header — not on
implementation details (which limiter algorithm, which middleware class).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from tangerine.storage.sqlite_store import SqliteLoyverseStore
from tangerine.web.auth import SESSION_COOKIE

# ---------------------------------------------------------------------------
# Shared fixtures and helpers (mirror the other UI seams so each test file is
# a self-contained worked example without import-coupling between slices).
# ---------------------------------------------------------------------------

TEST_PASSPHRASE = "slice6-test-passphrase"
TEST_SIGNING_SECRET = "slice6-test-signing-secret"


def _recipes_yaml() -> str:
    return """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
"""


def _costs_yaml() -> str:
    return """
costs:
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


def _write_configs(tmp_path: Path) -> tuple[str, str, str]:
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs.write_text(_costs_yaml(), encoding="utf-8")
    assignees.write_text(_assignees_yaml(), encoding="utf-8")
    return str(recipes), str(costs), str(assignees)


def _build_app(
    tmp_path: Path,
    *,
    today: date | None = None,
    login_rate_limit: int | None = None,
    login_rate_window_seconds: int | None = None,
    now_epoch: int | None = None,
):  # type: ignore[no-untyped-def]
    """Build the app against a fresh SQLite DB, with auth wired in.

    ``login_rate_limit`` / ``login_rate_window_seconds`` pin the rate-limit
    behaviour each test exercises; ``now_epoch`` pins the limiter's clock so a
    test can advance time deterministically by mutating ``app.state.now_epoch``.
    """
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    SqliteLoyverseStore.connect(db_path).close()
    recipes_path, costs_path, assignees_path = _write_configs(tmp_path)
    return create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today or date(2026, 6, 25),
        passphrase=TEST_PASSPHRASE,
        signing_secret=TEST_SIGNING_SECRET,
        login_rate_limit=login_rate_limit,
        login_rate_window_seconds=login_rate_window_seconds,
        now_epoch=now_epoch,
    )


def _login(client: TestClient, *, assignee_id: str = "daniel") -> None:
    """Log a client in so it carries a valid session cookie."""
    client.post(
        "/login",
        data={"passphrase": TEST_PASSPHRASE, "assignee_id": assignee_id},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies, "test login did not set a cookie"


# ---------------------------------------------------------------------------
# AC: GET /admin/db-snapshot downloads the current SQLite file (gated, authed)
# ---------------------------------------------------------------------------


def test_admin_db_snapshot_downloads_sqlite_file_when_authenticated(
    tmp_path: Path,
) -> None:
    """An authenticated ``GET /admin/db-snapshot`` returns the SQLite database
    as a file download.

    Tracer bullet for the admin backup route (PRD user story 28: "download a
    snapshot of the database from an admin route, so that I can take an
    out-of-band backup before risky maintenance"). It proves the route exists,
    is reachable once authenticated, and serves the database as an attachment
    rather than rendering a page.

    Asserts on partner-visible artefacts: a 200, a ``Content-Disposition:
    attachment`` header with a ``.db`` filename, and a body that begins with
    SQLite's file magic header.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)
    _login(client)

    response = client.get("/admin/db-snapshot")

    assert response.status_code == 200
    disposition = response.headers.get("content-disposition", "")
    assert "attachment" in disposition.lower()
    assert ".db" in disposition.lower()
    # The body is a real SQLite database file (magic header on every DB file).
    assert response.content.startswith(b"SQLite format 3\x00")


def test_admin_db_snapshot_redirects_to_login_when_unauthenticated(
    tmp_path: Path,
) -> None:
    """An unauthenticated ``GET /admin/db-snapshot`` is redirected to ``/login``.

    The backup route exposes the entire database, so it must be behind the same
    auth gate as every other non-public route. The test proves the gate covers
    the admin path (it is not accidentally listed as public) and that the
    database bytes do not leak to an anonymous caller.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)

    response = client.get("/admin/db-snapshot", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"
    assert not response.content.startswith(b"SQLite format 3\x00")


def test_admin_db_snapshot_reflects_live_store_contents(tmp_path: Path) -> None:
    """The downloaded snapshot contains the sales the live store holds.

    Worked example. A sale is persisted into the database before the app reads
    it; the authenticated snapshot download is itself a valid SQLite database
    that, when reopened, reports that same sale. This proves the route serves
    the *current* database (an out-of-band backup is only useful if it is the
    real data), not an empty or stale file.
    """
    from datetime import date as _date

    from tangerine.loyverse.store import SaleRecord
    from tangerine.types import Money, Sale, Segment

    db_path = str(tmp_path / "tangerine.db")
    seeded = SqliteLoyverseStore.connect(db_path)
    seeded.record_sales(
        [
            SaleRecord(
                sale=Sale(
                    item_id="chang-draft-500",
                    timestamp=_date(2026, 6, 24),
                    sell_price=Money("120"),
                    quantity=1,
                    segment=Segment.BAR,
                ),
                receipt_number="s6-1",
                line_id="li-1",
            )
        ]
    )
    seeded.close()

    recipes_path, costs_path, assignees_path = _write_configs(tmp_path)
    from tangerine.web.app import create_app

    app = create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=date(2026, 6, 25),
        passphrase=TEST_PASSPHRASE,
        signing_secret=TEST_SIGNING_SECRET,
    )
    client = TestClient(app)
    _login(client)

    response = client.get("/admin/db-snapshot")
    assert response.status_code == 200

    # Reopen the downloaded bytes as a SQLite database and confirm the sale.
    downloaded = tmp_path / "downloaded.db"
    downloaded.write_bytes(response.content)
    reopened = SqliteLoyverseStore.connect(str(downloaded))
    try:
        sales = reopened.sales()
    finally:
        reopened.close()
    assert len(sales) == 1
    assert sales[0].item_id == "chang-draft-500"


# ---------------------------------------------------------------------------
# AC: the login route is rate-limited; excess attempts from one client get 429
# ---------------------------------------------------------------------------


def test_login_rate_limit_returns_429_after_excess_attempts(
    tmp_path: Path,
) -> None:
    """Once a client exceeds the per-window login budget, further attempts get
    HTTP 429.

    PRD user story 32 / slice-6 issue: "request rate-limiting on the login
    route, so that the shared passphrase cannot be brute-forced." With a budget
    of 5 attempts per minute, the first five POSTs to ``/login`` are processed
    (here they fail the passphrase check and re-render, status 200) and the
    sixth is rejected with 429 before the passphrase is even checked.

    The clock is pinned (``now_epoch``) so all six attempts land in the same
    one-minute window; this test does not advance time.
    """
    app = _build_app(
        tmp_path,
        login_rate_limit=5,
        login_rate_window_seconds=60,
        now_epoch=1_700_000_000,
    )
    client = TestClient(app)

    statuses = [
        client.post(
            "/login",
            data={"passphrase": "wrong", "assignee_id": "daniel"},
            follow_redirects=False,
        ).status_code
        for _ in range(6)
    ]

    # The first five attempts are processed (wrong passphrase -> 200 re-render).
    assert statuses[:5] == [200, 200, 200, 200, 200]
    # The sixth attempt is rejected for exceeding the rate limit.
    assert statuses[5] == 429


def test_login_rate_limit_is_per_client_ip(tmp_path: Path) -> None:
    """One client exhausting its budget does not lock out a different client.

    The limit is "per IP" — a partner at home must not be blocked because
    someone else (or an attacker on another address) is hammering login. The
    test drives two distinct client addresses via ``X-Forwarded-For`` (the
    header nginx sets in production); the first exhausts its budget and gets
    429, while the second is still served on its first attempt.
    """
    app = _build_app(
        tmp_path,
        login_rate_limit=5,
        login_rate_window_seconds=60,
        now_epoch=1_700_000_000,
    )
    client = TestClient(app)

    attacker = {"X-Forwarded-For": "203.0.113.7"}
    partner = {"X-Forwarded-For": "198.51.100.4"}

    # Attacker burns through the budget and is then blocked.
    attacker_statuses = [
        client.post(
            "/login",
            data={"passphrase": "wrong", "assignee_id": "daniel"},
            headers=attacker,
            follow_redirects=False,
        ).status_code
        for _ in range(6)
    ]
    assert attacker_statuses[5] == 429

    # A different client is unaffected — its first attempt is processed.
    partner_resp = client.post(
        "/login",
        data={"passphrase": "wrong", "assignee_id": "daniel"},
        headers=partner,
        follow_redirects=False,
    )
    assert partner_resp.status_code == 200


def test_login_rate_limit_window_resets_after_the_window_elapses(
    tmp_path: Path,
) -> None:
    """After the window elapses, a previously-blocked client may try again.

    The limit is a transient lockout, not a permanent ban. The test exhausts
    the budget at a pinned "now", confirms the next attempt is 429, then
    advances the limiter's clock past the window and confirms the client is
    served again. Time is advanced by mutating ``app.state.now_epoch`` — the
    same per-request clock seam the auth middleware uses — so the test never
    sleeps.
    """
    start = 1_700_000_000
    app = _build_app(
        tmp_path,
        login_rate_limit=5,
        login_rate_window_seconds=60,
        now_epoch=start,
    )
    client = TestClient(app)

    def attempt() -> int:
        return client.post(
            "/login",
            data={"passphrase": "wrong", "assignee_id": "daniel"},
            follow_redirects=False,
        ).status_code

    for _ in range(5):
        assert attempt() == 200
    assert attempt() == 429  # budget spent within the window

    # Advance past the window; the budget resets and the client is served.
    app.state.now_epoch = start + 61
    assert attempt() == 200


def test_get_login_is_not_rate_limited(tmp_path: Path) -> None:
    """Rendering the login form (``GET /login``) is never throttled.

    Only the login POST is the brute-force surface; a partner reloading the
    page, or a browser re-fetching after a redirect, must always get the form.
    The test fetches the form more times than the POST budget and every
    response is a 200.
    """
    app = _build_app(
        tmp_path,
        login_rate_limit=5,
        login_rate_window_seconds=60,
        now_epoch=1_700_000_000,
    )
    client = TestClient(app)

    statuses = [client.get("/login").status_code for _ in range(10)]
    assert statuses == [200] * 10


def test_successful_login_within_budget_is_not_blocked(tmp_path: Path) -> None:
    """A correct login within the attempt budget succeeds normally.

    Rate-limiting must not get in the way of the happy path: a partner who
    signs in correctly on their first try lands on the app. The test logs in
    with the right passphrase and asserts the usual 303 redirect to ``/`` and a
    session cookie — proving the limiter passes legitimate traffic through.
    """
    app = _build_app(
        tmp_path,
        login_rate_limit=5,
        login_rate_window_seconds=60,
        now_epoch=1_700_000_000,
    )
    client = TestClient(app)

    response = client.post(
        "/login",
        data={"passphrase": TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert SESSION_COOKIE in response.cookies
