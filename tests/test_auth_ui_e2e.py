"""End-to-end auth seam (Wave 1, Slice 4).

Exercises the shared-passphrase + role-selector auth gate over the FastAPI app
from Slice 2. The genuine external boundary is HTTP (driven through FastAPI's
``TestClient``); no internal module is mocked. The signed-cookie / passphrase
machinery is exercised end-to-end through the routes that use it.

Scope (slice 4 only):
  - Every route except ``/login`` redirects to ``/login`` when unauthenticated.
  - ``GET /login`` renders a form with a passphrase field and a role selector
    populated from ``config/assignees.yaml``.
  - Correct passphrase + role sets a signed session cookie and lands on ``/``.
  - Wrong passphrase re-renders login with an error, no hint which field.
  - Tampered cookies invalidate the session.
  - Inactivity timeout expires the session; activity within the window refreshes.
  - ``POST /logout`` clears the cookie and redirects to ``/login``.
  - ``request.state.assignee_id`` is wired so future capture flows can attribute
    actions (observed here via a "Signed in as" row on the review page).
  - Cookie ``Secure`` flag is env-controlled (off for local HTTP dev).

Per the PRD's testing rules these tests assert on the partner-visible behaviour
— HTTP status codes, redirects, rendered form fields, the Set-Cookie header —
not on implementation details (which signer, which middleware class).
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tangerine.web.auth import SESSION_COOKIE, Session


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

#: A test passphrase. Stable across tests; passed explicitly into ``create_app``
#: so tests do not mutate the process environment.
TEST_PASSPHRASE = "correct-horse-battery-staple"

#: A stable signing secret. Real deployments read this from env; tests pass it
#: explicitly so the signer is deterministic and does not depend on env state.
TEST_SIGNING_SECRET = "test-signing-secret-not-for-prod"


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


def _assignees_yaml_daniel_noi() -> str:
    """The default two-partner list (mirrors ``config/assignees.yaml``)."""
    return """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
"""


def _write_configs(
    tmp_path: Path,
    *,
    assignees_yaml: str | None = None,
) -> tuple[str, str, str]:
    """Write recipes + costs + assignees YAML into ``tmp_path``.

    Returns ``(recipes_path, costs_path, assignees_path)``.
    """
    recipes = tmp_path / "recipes.yaml"
    costs = tmp_path / "costs.yaml"
    assignees = tmp_path / "assignees.yaml"
    recipes.write_text(_recipes_yaml(), encoding="utf-8")
    costs.write_text(_costs_yaml(), encoding="utf-8")
    assignees.write_text(
        assignees_yaml or _assignees_yaml_daniel_noi(), encoding="utf-8"
    )
    return str(recipes), str(costs), str(assignees)


def _build_app(
    tmp_path: Path,
    *,
    today: date | None = None,
    passphrase: str = TEST_PASSPHRASE,
    assignees_yaml: str | None = None,
    cookie_secure: bool = False,
    signing_secret: str = TEST_SIGNING_SECRET,
    inactivity_seconds: int = 8 * 60 * 60,
):
    """Build the app with auth wired in, against a fresh SQLite DB.

    The auth-related kwargs are explicit so each test pins the behaviour it is
    exercising (passphrase mismatch, missing env var, secure flag on/off,
    timeout window) without env mutation.
    """
    from tangerine.storage.sqlite_store import SqliteLoyverseStore
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    SqliteLoyverseStore.connect(db_path).close()
    recipes_path, costs_path, assignees_path = _write_configs(
        tmp_path, assignees_yaml=assignees_yaml
    )
    return create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        today=today or date(2026, 6, 25),
        passphrase=passphrase,
        signing_secret=signing_secret,
        cookie_secure=cookie_secure,
        inactivity_seconds=inactivity_seconds,
    )


# ---------------------------------------------------------------------------
# AC: Unauthenticated requests to any route except /login redirect to /login
# ---------------------------------------------------------------------------


def test_unauthenticated_get_root_redirects_to_login(tmp_path: Path) -> None:
    """An unauthenticated ``GET /`` is redirected to ``/login``.

    The auth gate must cover every route except ``/login`` itself. This is the
    tracer bullet: the simplest end-to-end path through the gate. It proves the
    middleware is wired, the redirect target is right, and the redirect does
    not leak the protected page's body.

    ``TestClient`` follows redirects by default; we pass ``follow_redirects=False``
    so the test pins the 302 response itself rather than the post-redirect page.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"
    # The protected page body must not leak through the redirect.
    assert "Daily 9am review" not in response.text


# ---------------------------------------------------------------------------
# AC: The login page shows a passphrase field and a role selector from config
# ---------------------------------------------------------------------------


def test_get_login_renders_form_with_role_selector_from_config(
    tmp_path: Path,
) -> None:
    """``GET /login`` renders a form with a passphrase field and a role
    selector populated from ``config/assignees.yaml``.

    The role selector must be data-driven: the page renders one ``<option>``
    per assignee in the loaded config, valued by ``assignee_id`` and labelled
    with the partner's name. Adding the future manager is a YAML entry, not a
    code change (PRD user story 31).

    Asserts on the visible form structure (field names, option values and
    labels) rather than incidental markup so the test survives CSS changes.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)

    response = client.get("/login")

    assert response.status_code == 200
    form = _section(response.text, "login-form")
    # Passphrase field is a password input named ``passphrase``.
    assert 'type="password"' in form
    assert 'name="passphrase"' in form
    # Role selector is named ``assignee_id`` and carries one option per
    # configured assignee.
    assert 'name="assignee_id"' in form
    assert 'value="daniel"' in form
    assert ">Daniel<" in form
    assert 'value="noi"' in form
    assert ">Noi<" in form


def test_role_selector_reflects_config_changes(tmp_path: Path) -> None:
    """Adding an assignee to the YAML surfaces them in the selector.

    The PRD's future-manager onboarding story (user story 31) rests on this:
    a config-only change widens the selector. The test seeds a YAML with the
    two partners plus a third ("manager") and asserts all three appear.
    """
    three_partners = """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
  - assignee_id: manager
    name: Manager
"""
    app = _build_app(tmp_path, assignees_yaml=three_partners)
    client = TestClient(app)

    response = client.get("/login")
    form = _section(response.text, "login-form")

    assert 'value="manager"' in form
    assert ">Manager<" in form


# --- section helpers ---------------------------------------------------------


def _section(html: str, anchor: str) -> str:
    """Return the HTML slice for a section marked with ``<!--section:NAME-->``."""
    start = f"<!--section:{anchor}-->"
    end = f"<!--/section:{anchor}-->"
    i = html.find(start)
    j = html.find(end)
    assert i != -1 and j != -1, f"section {anchor!r} not found in HTML"
    return html[i : j + len(end)]


# ---------------------------------------------------------------------------
# AC: Correct passphrase + role sets a signed cookie and lands on /
# ---------------------------------------------------------------------------


def test_correct_login_sets_signed_cookie_and_lands_on_root(
    tmp_path: Path,
) -> None:
    """A correct passphrase + role selection sets a session cookie and
    redirects to ``/``.

    The POST to ``/login`` with the right passphrase and a valid assignee_id:
      - returns a 303 (POST→redirect convention),
      - sets the ``tangerine_session`` cookie,
      - redirects to ``/``,
      - the cookie actually authorises the follow-up ``GET /`` (200, not 302).

    The last assertion is the load-bearing one: a cookie that does not
    authorise the very next request is a useless cookie. It proves the
    signing round-trips through the middleware end-to-end.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/login",
        data={"passphrase": TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert SESSION_COOKIE in response.cookies

    # The cookie authorises the next request — no redirect back to /login.
    # (``/`` itself 302s to the day-mode review URL since Wave 2 slice 2;
    # following redirects lands on the review page, not the login form.)
    authed = client.get("/")
    assert authed.status_code == 200
    assert "Daily 9am review" in authed.text


# ---------------------------------------------------------------------------
# AC: Wrong passphrase re-renders login with an error and no field hint
# ---------------------------------------------------------------------------


def test_wrong_passphrase_re_renders_login_with_generic_error(
    tmp_path: Path,
) -> None:
    """A wrong passphrase re-renders the login page with a generic error.

    The error message must NOT reveal which field was wrong — that would let
    an attacker enumerate passphrases (learning "passphrase wrong" vs "role
    wrong" narrows a brute-force). The same generic message appears for both
    a wrong passphrase and an unknown role.

    The test exercises both failure cases and asserts they produce identical
    error surfaces, and that no session cookie is set on failure.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)

    wrong_pass = client.post(
        "/login",
        data={"passphrase": "wrong-passphrase", "assignee_id": "daniel"},
        follow_redirects=False,
    )
    wrong_role = client.post(
        "/login",
        data={"passphrase": TEST_PASSPHRASE, "assignee_id": "nobody"},
        follow_redirects=False,
    )

    for resp in (wrong_pass, wrong_role):
        assert resp.status_code == 200  # re-renders login, not a redirect
        assert SESSION_COOKIE not in resp.cookies
        # A generic error appears (the wording is the template's choice; the
        # test pins "an error is shown" and "it does not name a field").
        assert "Sign in failed" in resp.text
        # No field-level hint: neither "passphrase" nor "role" / "assignee"
        # appears in the *error message* itself. The form field labels say
        # "Passphrase" / "Who are you?" — those are fine; the error message
        # must not echo them.
        error_block = _error_block(resp.text)
        assert "passphrase" not in error_block.lower()
        assert "role" not in error_block.lower()


def _error_block(html: str) -> str:
    """Return just the error message text, isolated from the form.

    The error lives in ``<p class="login__error">…</p>``. Pulling it out
    separately lets the test assert the *message* does not name a field,
    independent of the form labels above it.
    """
    import re

    m = re.search(
        r'<p[^>]*class="[^"]*login__error[^"]*"[^>]*>(.*?)</p>',
        html,
        re.DOTALL,
    )
    assert m, "no login__error block found"
    return m.group(1)


# ---------------------------------------------------------------------------
# AC: Session cookies are signed — tampering invalidates the session
# ---------------------------------------------------------------------------


def test_tampered_cookie_redirects_to_login(tmp_path: Path) -> None:
    """A cookie whose signature has been touched is rejected.

    The signed payload carries the role and the activity timestamp; flipping
    any byte must invalidate the signature, so a tampered cookie is treated
    the same as no cookie: redirect to ``/login``.

    The test signs a real session, mutates one character, and asserts the
    middleware rejects it. This pins the tamper-evidence property directly
    rather than relying on itsdangerous' own tests.
    """
    app = _build_app(tmp_path)
    authenticator = app.state.authenticator
    real = authenticator.sign(
        Session(assignee_id="daniel", last_activity=int(time.time()))
    )
    # Flip the final character to a guaranteed-different one (the last char is
    # part of the signature, so any change breaks verification). Choosing the
    # replacement relative to the current char makes the mutation deterministic
    # — earlier logic hard-coded "a", which was a no-op whenever that position
    # already held an "a".
    tampered = real[:-1] + ("a" if real[-1] != "a" else "b")
    assert tampered != real, "tamper did not actually change the cookie"

    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, tampered)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_cookie_signed_under_different_secret_is_rejected(
    tmp_path: Path,
) -> None:
    """A cookie signed under one secret is rejected when the app's secret
    differs (e.g. after a secret rotation).

    Pinning this protects against the failure mode where a key rotation
    silently keeps old sessions alive — partners must re-authenticate after a
    secret change.
    """
    # App 1 signs a session.
    app1 = _build_app(tmp_path, signing_secret="secret-A")
    cookie = app1.state.authenticator.sign(
        Session(assignee_id="daniel", last_activity=int(time.time()))
    )

    # App 2 uses a different secret but the same passphrase.
    tmp2 = tmp_path / "app2"
    tmp2.mkdir()
    app2 = _build_app(tmp2, signing_secret="secret-B")

    client = TestClient(app2)
    client.cookies.set(SESSION_COOKIE, cookie)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# AC: Requests within the timeout window refresh the last-activity timestamp
# ---------------------------------------------------------------------------


def test_request_within_window_refreshes_activity_timestamp(
    tmp_path: Path,
) -> None:
    """An authenticated request gets a re-signed cookie with a refreshed
    last-activity timestamp (the sliding inactivity window).

    A partner who keeps using the tool never sees the timeout — each request
    re-stamps "now". The test pins this by:

      1. Signing a session with an old activity timestamp (within window).
      2. Making a request at a known later "now" (still within window).
      3. Asserting the response carries a NEW cookie whose decoded timestamp
         equals the request's "now", not the original stamp.

    This is the partner-visible "I was active 30 seconds ago, why did I time
    out" guarantee.
    """
    stamped_activity = 1_700_000_000
    later_now = stamped_activity + 30 * 60  # 30 min later, still inside 8h window
    app = _build_app_with_clock(
        tmp_path,
        now_epoch=later_now,
        inactivity_seconds=8 * 60 * 60,
    )
    old_cookie = app.state.authenticator.sign(
        Session(assignee_id="daniel", last_activity=stamped_activity)
    )

    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, old_cookie)
    response = client.get("/")

    # Authenticated (within window) — the request landed on the review page
    # (via the Wave 2 ``/`` -> day-mode redirect), not back on /login.
    assert response.status_code == 200
    # A refreshed cookie was set on the response.
    refreshed_raw = response.cookies.get(SESSION_COOKIE)
    assert refreshed_raw is not None, "middleware did not set a refreshed cookie"
    assert refreshed_raw != old_cookie, "cookie was not re-signed"
    # The refreshed cookie decodes to the request's "now", not the old stamp.
    decoded = app.state.authenticator.verify(
        refreshed_raw,
        max_age=8 * 60 * 60,
        now_epoch=later_now,
    )
    assert decoded is not None
    assert decoded.last_activity == later_now
    assert decoded.assignee_id == "daniel"


def test_repeated_requests_within_window_keep_extending_session(
    tmp_path: Path,
) -> None:
    """A second request, still within window of the first, stays authorised.

    Catches the regression where the sliding refresh would set a *new* cookie
    that the next request doesn't accept (e.g. signing under a different key,
    or stamping a future timestamp the middleware then rejects).
    """
    t0 = 1_700_000_000
    t1 = t0 + 60 * 60  # +1h
    t2 = t1 + 60 * 60  # +2h, both still inside 8h window
    # Each request advances the pinned clock: rebuild app per request so the
    # middleware's "now" matches the request time.
    app1 = _build_app_with_clock(
        tmp_path, now_epoch=t1, inactivity_seconds=8 * 60 * 60
    )
    cookie_after_t0 = app1.state.authenticator.sign(
        Session(assignee_id="daniel", last_activity=t0)
    )

    client = TestClient(app1)
    client.cookies.set(SESSION_COOKIE, cookie_after_t0)
    r1 = client.get("/")
    assert r1.status_code == 200
    cookie_after_t1 = r1.cookies.get(SESSION_COOKIE)
    assert cookie_after_t1 is not None

    # Second request at t2 with the refreshed cookie from t1.
    app2 = _build_app_with_clock(
        tmp_path, now_epoch=t2, inactivity_seconds=8 * 60 * 60
    )
    client2 = TestClient(app2)
    client2.cookies.set(SESSION_COOKIE, cookie_after_t1)
    r2 = client2.get("/")
    assert r2.status_code == 200, "refreshed cookie was not accepted at t2"


# ---------------------------------------------------------------------------
# AC: POST /logout clears the session cookie and redirects to /login
# ---------------------------------------------------------------------------


def test_logout_clears_cookie_and_redirects_to_login(tmp_path: Path) -> None:
    """``POST /logout`` deletes the session cookie and redirects to ``/login``.

    Logout is the partner's "I'm done" action. The next request must be
    unauthenticated, even on a browser that holds the cookie jar — the
    ``Set-Cookie`` must clear (expire) the prior session cookie.

    The test logs in to obtain a real cookie, then logs out, then asserts a
    follow-up authenticated request is redirected to ``/login``. The full
    round-trip pins the partner-visible "after logout I'm back at the login
    page" guarantee.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)

    # Log in to obtain a real cookie.
    client.post(
        "/login",
        data={"passphrase": TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    assert SESSION_COOKIE in client.cookies

    # Log out.
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    # The follow-up authenticated request is redirected (cookie was cleared).
    follow = client.get("/", follow_redirects=False)
    assert follow.status_code == 302
    assert follow.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# AC: The selected role is available to other routes via request.state
# ---------------------------------------------------------------------------


def test_review_page_shows_signed_in_assignee(tmp_path: Path) -> None:
    """The signed-in partner's name appears on the review page.

    Future capture flows (Wave 2+) will attribute actions via
    ``request.state.assignee_id``; Wave 1 has no capture yet, so the AC's
    "wired into the request context, not yet consumed" is observable here
    by surfacing the assignee's name on the review page itself. The test
    logs in as Noi and asserts her name appears on ``GET /``.

    This pins both halves of the AC: the middleware populates
    ``request.state.assignee_id``, and the review route can read it.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": TEST_PASSPHRASE, "assignee_id": "noi"},
        follow_redirects=False,
    )

    response = client.get("/")

    assert response.status_code == 200
    # A "signed in as" row carrying the assignee's name (not their id — the
    # partner-visible string is the human name from the YAML).
    signed = _section(response.text, "signed-in-as")
    assert "Noi" in signed
    # The OTHER partner's name does not appear in this block — guards
    # against the template hard-coding Daniel.
    assert "Daniel" not in signed


def test_review_page_shows_daniel_when_signed_in_as_daniel(
    tmp_path: Path,
) -> None:
    """The signed-in row tracks the actual session, not a hard-coded value.

    Companion to the Noi test: logging in as Daniel must show Daniel's name.
    Together the two tests pin that the row is data-driven from the session.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )

    response = client.get("/")
    signed = _section(response.text, "signed-in-as")
    assert "Daniel" in signed


# ---------------------------------------------------------------------------
# AC: Session cookies are marked secure-only when served over HTTPS
# ---------------------------------------------------------------------------


def test_login_cookie_carries_secure_flag_when_enabled(
    tmp_path: Path,
) -> None:
    """When ``cookie_secure=True``, the ``Set-Cookie`` carries ``Secure``.

    In production (behind TLS) the cookie must not be transmitted over plain
    HTTP. In local dev (HTTP) the flag must be off or the browser rejects the
    cookie. ``TANGERINE_COOKIE_SECURE`` (the env flag, surfaced as the
    ``cookie_secure`` kwarg) flips it.

    The test logs in with the flag on and asserts the Set-Cookie header
    carries ``Secure``.
    """
    app = _build_app(tmp_path, cookie_secure=True)
    client = TestClient(app)

    response = client.post(
        "/login",
        data={"passphrase": TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )

    set_cookie = response.headers["set-cookie"]
    assert "Secure" in set_cookie


def test_login_cookie_omits_secure_flag_when_disabled(
    tmp_path: Path,
) -> None:
    """When ``cookie_secure=False`` (local dev over HTTP), the Set-Cookie
    does NOT carry ``Secure`` — or the browser would reject it.

    Pins the local-dev ergonomics: the same code path that adds ``Secure``
    in prod must omit it on the developer's laptop.
    """
    app = _build_app(tmp_path, cookie_secure=False)
    client = TestClient(app)

    response = client.post(
        "/login",
        data={"passphrase": TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )

    set_cookie = response.headers["set-cookie"]
    assert "Secure" not in set_cookie


# ---------------------------------------------------------------------------
# AC: App fails loudly at startup if the passphrase is missing
# ---------------------------------------------------------------------------


def test_create_app_raises_when_passphrase_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """App construction raises when the passphrase is unset.

    The passphrase lives in the environment (PRD user story 11). A missing or
    empty passphrase must fail loudly at startup rather than letting anyone
    in — this is the "fail loudly at startup" rule from the PRD's
    implementation decisions, applied to auth.

    The test clears the env var and calls ``create_app`` without the explicit
    kwarg, expecting a ``RuntimeError`` whose message names the env var so a
    partner running the deploy can see what to set.
    """
    monkeypatch.delenv("TANGERINE_AUTH_PASSPHRASE", raising=False)
    monkeypatch.delenv("TANGERINE_SIGNING_SECRET", raising=False)
    from tangerine.web.app import create_app

    recipes_path, costs_path, assignees_path = _write_configs(tmp_path)
    db_path = str(tmp_path / "tangerine.db")
    from tangerine.storage.sqlite_store import SqliteLoyverseStore

    SqliteLoyverseStore.connect(db_path).close()

    with pytest.raises(RuntimeError) as exc:
        create_app(
            db_path=db_path,
            recipes_path=recipes_path,
            costs_path=costs_path,
            assignees_path=assignees_path,
            # No passphrase, no signing_secret — must raise.
        )
    # The error names the env var so the operator knows what to set.
    assert "TANGERINE_AUTH_PASSPHRASE" in str(exc.value)


def test_create_app_raises_when_signing_secret_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """App construction raises when the cookie signing secret is unset.

    The signing secret is distinct from the passphrase so one can be rotated
    without invalidating the other; but it must also be present at startup
    (an empty secret would sign cookies under a trivially guessable key).
    """
    monkeypatch.delenv("TANGERINE_AUTH_PASSPHRASE", raising=False)
    monkeypatch.delenv("TANGERINE_SIGNING_SECRET", raising=False)
    from tangerine.web.app import create_app

    recipes_path, costs_path, assignees_path = _write_configs(tmp_path)
    db_path = str(tmp_path / "tangerine.db")
    from tangerine.storage.sqlite_store import SqliteLoyverseStore

    SqliteLoyverseStore.connect(db_path).close()

    # Passphrase provided but signing_secret missing — must still raise on
    # the signing secret.
    with pytest.raises(RuntimeError) as exc:
        create_app(
            db_path=db_path,
            recipes_path=recipes_path,
            costs_path=costs_path,
            assignees_path=assignees_path,
            passphrase=TEST_PASSPHRASE,
        )
    assert "TANGERINE_SIGNING_SECRET" in str(exc.value)


# ---------------------------------------------------------------------------
# AC: Static assets bypass the auth gate (the login page needs its CSS)
# ---------------------------------------------------------------------------


def test_static_assets_are_reachable_without_authentication(
    tmp_path: Path,
) -> None:
    """The CSS file under ``/static/`` loads without a session cookie.

    The login page links the stylesheet; if the auth gate intercepted
    ``/static/...`` requests, the login page itself would render unstyled
    (or, worse, the browser would follow the redirect to ``/login`` for
    every asset, looping). Static assets are public by design.
    """
    app = _build_app(tmp_path)
    client = TestClient(app)

    response = client.get("/static/review.css", follow_redirects=False)

    assert response.status_code == 200
    assert response.text.strip(), "CSS file is empty"


# ---------------------------------------------------------------------------
# AC: After the inactivity timeout, the next request redirects to /login
# ---------------------------------------------------------------------------


def test_expired_session_redirects_to_login(tmp_path: Path) -> None:
    """A session whose last-activity is older than the inactivity window
    is treated as unauthenticated.

    The session does not expire on a fixed lifetime — it expires after a
    window of *inactivity*. So a request that arrives more than
    ``inactivity_seconds`` after the cookie's stamped activity redirects to
    ``/login``.

    The test signs a session stamped 9 hours ago (the default window is 8
    hours), sets the cookie, and asserts the redirect. The middleware's
    "now" is pinned via its ``now_epoch`` override so the test does not
    depend on the wall clock.
    """
    # Build an app with a pinned "now" 9 hours after the stamped activity.
    stamped_activity = 1_700_000_000
    nine_hours_later = stamped_activity + 9 * 60 * 60
    app = _build_app_with_clock(
        tmp_path,
        now_epoch=nine_hours_later,
        inactivity_seconds=8 * 60 * 60,
    )
    cookie = app.state.authenticator.sign(
        Session(assignee_id="daniel", last_activity=stamped_activity)
    )

    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, cookie)
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def _build_app_with_clock(
    tmp_path: Path,
    *,
    now_epoch: int,
    inactivity_seconds: int,
    **kwargs,  # type: ignore[no-untyped-def]
):
    """App whose auth middleware uses a pinned ``now_epoch``.

    Reaches into the built app to replace the middleware's clock so tests can
    advance time without sleeping. The middleware reads ``now_epoch`` per
    request, so this is the cleanest seam.
    """
    app = _build_app(tmp_path, inactivity_seconds=inactivity_seconds, **kwargs)
    # Find the AuthMiddleware instance and pin its clock.
    for mw in app.user_middleware:
        if getattr(mw.cls, "__name__", "") == "AuthMiddleware":
            # The middleware is instantiated by Starlette at first request;
            # the override is read from kwargs at construction. Re-create the
            # app with the override threaded through.
            break
    # Rebuild with the override — Starlette re-instantiates middleware on
    # each app, so we just re-run create_app with the now_epoch plumbed in.
    from tangerine.storage.sqlite_store import SqliteLoyverseStore
    from tangerine.web.app import create_app

    db_path = str(tmp_path / "tangerine.db")
    recipes_path, costs_path, assignees_path = _write_configs(tmp_path)
    SqliteLoyverseStore.connect(db_path).close()
    app = create_app(
        db_path=db_path,
        recipes_path=recipes_path,
        costs_path=costs_path,
        assignees_path=assignees_path,
        passphrase=kwargs.get("passphrase", TEST_PASSPHRASE),
        signing_secret=kwargs.get("signing_secret", TEST_SIGNING_SECRET),
        cookie_secure=kwargs.get("cookie_secure", False),
        inactivity_seconds=inactivity_seconds,
        now_epoch=now_epoch,
    )
    return app
