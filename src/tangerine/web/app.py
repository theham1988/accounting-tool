"""FastAPI application factory for the daily 9am review (Wave 1, Slice 2).

``create_app(...)`` wires up:
  - the SQLite store (opened once at app construction, closed on shutdown),
  - the config-loaded recipes + cost book,
  - the ``StoreSource`` adapter (Slice 1) the engine consumes unchanged,
  - Jinja2 templates packaged inside this module (``templates/``),

and registers the review routes. Routes call ``build_daily_review(...)`` and
render the result; they hold no business logic of their own.

The factory takes the DB and config paths as explicit kwargs (with env-var
defaults) so tests can drive the app in-process without env mutation — the
same testability pattern the CLI's ``main()`` uses. ``today`` is injectable so
"yesterday" is deterministic in tests; in production it defaults to
``date.today()``.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.background import BackgroundTask

from ..config.loader import load_assignees, load_costs, load_recipes
from ..daily_review import DailyReview, build_daily_review
from ..loyverse.config import LoyverseCredentials
from ..loyverse.source import StoreSource
from ..loyverse.sync import SyncResult, run_sync
from ..storage.sqlite_store import SqliteLoyverseStore
from ..types import Assignee, Segment, SegmentMargin
from .auth import (
    AuthConfig,
    AuthMiddleware,
    SESSION_COOKIE,
    Session,
    SessionAuthenticator,
    clear_session_cookie,
    set_session_cookie,
)
from .ratelimit import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_WINDOW_SECONDS,
    LoginRateLimitMiddleware,
    RateLimiter,
)

#: Default config file locations (operator-editable, shipped with the repo).
#: Mirror the CLI defaults in ``tangerine.__main__`` so the web app and the
#: CLI read the same source of truth by default.
DEFAULT_RECIPES_PATH = "config/recipes.yaml"
DEFAULT_COSTS_PATH = "config/costs.yaml"
DEFAULT_ASSIGNEES_PATH = "config/assignees.yaml"

#: Environment variable holding the SQLite database path. Lives in the
#: environment, not the repo, per ADR-0001. Mirrors the CLI.
DB_PATH_ENV = "TANGERINE_DB_PATH"
DEFAULT_DB_PATH = "./tangerine.db"

#: Loyverse credentials live in the environment, never in the database or the
#: repo (PRD user story 11). Access token is mandatory; store id is optional
#: but the venue has one store, so we read both from env by default.
LOYVERSE_TOKEN_ENV = "LOYVERSE_ACCESS_TOKEN"
LOYVERSE_STORE_ID_ENV = "LOYVERSE_STORE_ID"

#: Environment variable holding the shared auth passphrase. PRD user story 11 /
#: Wave 1 slice 4: credentials live in the environment, never in the repo.
#: The app fails loudly at startup if this is unset or empty.
AUTH_PASSPHRASE_ENV = "TANGERINE_AUTH_PASSPHRASE"

#: Environment variable holding the cookie-signing secret. Distinct from the
#: passphrase so rotating one does not invalidate existing sessions.
SIGNING_SECRET_ENV = "TANGERINE_SIGNING_SECRET"

#: Environment variable controlling the cookie ``Secure`` flag. Set to a
#: truthy value in prod (behind TLS); leave unset for local HTTP dev.
COOKIE_SECURE_ENV = "TANGERINE_COOKIE_SECURE"


def _truthy_env(name: str) -> bool:
    """Read a bool from env: ``1``/``true``/``yes`` (case-insensitive) -> True."""
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}

#: How old the most recent successful sync may be before the review shows a
#: stale-data banner (slice 5). 24 hours: the nightly cron runs once a day, so
#: anything older than a day means a sync was missed and the numbers on screen
#: predate yesterday's close.
STALE_AFTER_SECONDS: int = 24 * 60 * 60

#: Canonical segment display order (cafe first, then bar) so the template's
#: segment-CM rows render in a stable, predictable order regardless of dict
#: iteration.
_SEGMENT_ORDER: tuple[Segment, ...] = (Segment.CAFE, Segment.BAR)


def _money(value) -> str:  # type: ignore[no-untyped-def]
    """Render a Decimal money value to a 2-dp THB string.

    Surfaced as a Jinja filter so templates never format money inline; this is
    the single place the on-page number formatting lives.
    """
    from decimal import Decimal

    if value is None:
        return "0.00"
    return str(Decimal(value).quantize(Decimal("0.01")))


def _build_templates() -> Jinja2Templates:
    """Construct the Jinja2 templates bound to this package's ``templates/``."""
    # A standalone Environment lets us register filters before handing the
    # templates off; Jinja2Templates picks it up via its `env` argument.
    env = Environment(
        loader=PackageLoader("tangerine.web", "templates"),
        autoescape=select_autoescape(["html", "htm"]),
    )
    env.filters["money"] = _money
    return Jinja2Templates(env=env)


def _segment_sort_key(row: SegmentMargin) -> int:
    """Stable display order for per-segment rows (cafe then bar).

    Takes a ``SegmentMargin`` (the row the template iterates) and returns the
    sort position of its segment — cafe first, then bar.
    """
    try:
        return _SEGMENT_ORDER.index(row.segment)
    except ValueError:  # pragma: no cover — defensive; both enum members listed
        return len(_SEGMENT_ORDER)


def create_app(
    *,
    db_path: str | None = None,
    recipes_path: str | None = None,
    costs_path: str | None = None,
    assignees_path: str | None = None,
    today: date | None = None,
    loyverse_urlopen: Any = None,
    loyverse_access_token: str | None = None,
    loyverse_store_id: str | None = None,
    passphrase: str | None = None,
    signing_secret: str | None = None,
    cookie_secure: bool | None = None,
    inactivity_seconds: int | None = None,
    login_rate_limit: int | None = None,
    login_rate_window_seconds: int | None = None,
    now_epoch: int | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``db_path`` defaults to ``$TANGERINE_DB_PATH`` or ``./tangerine.db``.
    ``recipes_path`` / ``costs_path`` / ``assignees_path`` default to the
    shipped config files. ``today`` defaults to ``date.today()``; injectable
    so tests can pin "yesterday" deterministically.

    Auth (slice 4):
      - ``passphrase``         defaults to ``$TANGERINE_AUTH_PASSPHRASE``;
                               app construction raises if it is empty.
      - ``signing_secret``     defaults to ``$TANGERINE_SIGNING_SECRET``;
                               app construction raises if it is empty.
      - ``cookie_secure``      defaults to truthy ``$TANGERINE_COOKIE_SECURE``
                               (False in local dev).
      - ``inactivity_seconds`` defaults to 8 hours (slice-4 issue).

    Loyverse credentials default to ``$LOYVERSE_ACCESS_TOKEN`` /
    ``$LOYVERSE_STORE_ID``; ``loyverse_urlopen`` is injectable so the UI seam
    tests stub the Loyverse HTTP endpoint without mutating the environment.

    The store is opened once and held for the app's lifetime (closed on
    shutdown). Config is loaded once at construction — a malformed config
    or missing passphrase raises immediately, per the PRD's "fail loudly at
    startup" rule.
    """
    db = db_path or os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)
    recipes_yaml = recipes_path or DEFAULT_RECIPES_PATH
    costs_yaml = costs_path or DEFAULT_COSTS_PATH
    assignees_yaml = assignees_path or DEFAULT_ASSIGNEES_PATH
    today_date = today or date.today()
    token = loyverse_access_token or os.environ.get(LOYVERSE_TOKEN_ENV)
    store_id = (
        loyverse_store_id
        if loyverse_store_id is not None
        else os.environ.get(LOYVERSE_STORE_ID_ENV)
    )
    loyverse_urlopen_param = loyverse_urlopen

    catalog = load_recipes(recipes_yaml)
    cost = load_costs(costs_yaml)
    assignees = load_assignees(assignees_yaml)

    # Fail loudly at startup on a missing/empty passphrase or signing secret.
    # A half-working app that gates on an empty passphrase would let anyone
    # in; a half-working signer would sign cookies under an empty key.
    resolved_passphrase = passphrase if passphrase is not None else os.environ.get(
        AUTH_PASSPHRASE_ENV, ""
    )
    resolved_secret = signing_secret if signing_secret is not None else os.environ.get(
        SIGNING_SECRET_ENV, ""
    )
    if not resolved_passphrase:
        raise RuntimeError(
            f"auth passphrase is required: set ${AUTH_PASSPHRASE_ENV} "
            "or pass passphrase=... to create_app"
        )
    if not resolved_secret:
        raise RuntimeError(
            f"cookie signing secret is required: set ${SIGNING_SECRET_ENV} "
            "or pass signing_secret=... to create_app"
        )
    resolved_secure = (
        cookie_secure if cookie_secure is not None else _truthy_env(COOKIE_SECURE_ENV)
    )
    resolved_inactivity = (
        inactivity_seconds if inactivity_seconds is not None else 8 * 60 * 60
    )
    auth_config = AuthConfig(
        passphrase=resolved_passphrase,
        signing_secret=resolved_secret,
        cookie_secure=resolved_secure,
        inactivity_seconds=resolved_inactivity,
    )
    authenticator = SessionAuthenticator(resolved_secret)

    # FastAPI serves sync route handlers from a threadpool, so the SQLite
    # connection must be safe to use across threads. ``check_same_thread=False``
    # lifts Python's default same-thread guard — but that is *not* sufficient
    # for safe concurrent use: the underlying C-level connection is not
    # thread-safe, and unsynchronised concurrent writes surface as
    # ``sqlite3.InterfaceError: bad parameter or other API misuse``. The store
    # owns a per-instance lock and serialises every connection touch, so this
    # single shared connection is safe under the threadpool and alongside the
    # nightly sync cron.
    conn = sqlite3.connect(db, check_same_thread=False)
    store = SqliteLoyverseStore(conn)
    source = StoreSource(
        store=store,
        recipes=list(catalog.all()),
        cost=cost,
        mappings=list(catalog.mappings()),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="Tangerine Phuket — 9am Review", lifespan=lifespan)
    templates = _build_templates()

    # Mount the packaged static directory so the CSS (and any future HTMX JS)
    # is served at a stable URL. Resolves the directory relative to this module
    # so it works regardless of how the package is installed.
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.state.store = store
    app.state.source = source
    app.state.db_path = db
    app.state.templates = templates
    app.state.today = today_date
    app.state.loyverse_urlopen = loyverse_urlopen_param
    app.state.loyverse_credentials = (
        LoyverseCredentials(access_token=token, store_id=store_id)
        if token is not None
        else None
    )
    app.state.assignees = assignees
    app.state.auth_config = auth_config
    app.state.authenticator = authenticator
    app.state.now_epoch = now_epoch

    # The auth gate sits between Starlette's routing and our route handlers.
    # Public paths (``/login``, ``/static``) bypass it; everything else
    # requires a valid signed session cookie and populates
    # ``request.state.assignee_id``. ``now_epoch`` is injected so tests can
    # advance the clock without sleeping; production leaves it None and the
    # middleware reads the wall clock per request.
    app.add_middleware(
        AuthMiddleware,
        authenticator=authenticator,
        config=auth_config,
        now_epoch=now_epoch,
    )

    # Login rate-limiting (slice 6): the shared passphrase is the only secret,
    # so the login POST is the brute-force surface. The limiter is added after
    # the auth middleware so it is the *outermost* layer — an over-budget POST
    # is rejected with 429 before any other processing. ``/login`` is public to
    # the auth gate, so the two never conflict. The limiter's clock reads
    # ``app.state.now_epoch`` per request (None -> wall clock), so tests pin and
    # advance it the same way the auth middleware's clock is driven.
    resolved_login_limit = (
        login_rate_limit if login_rate_limit is not None else DEFAULT_MAX_ATTEMPTS
    )
    resolved_login_window = (
        login_rate_window_seconds
        if login_rate_window_seconds is not None
        else DEFAULT_WINDOW_SECONDS
    )
    login_limiter = RateLimiter(
        max_attempts=resolved_login_limit,
        window_seconds=resolved_login_window,
    )
    app.state.login_rate_limiter = login_limiter

    def _login_now() -> int:
        override = app.state.now_epoch
        return override if override is not None else int(time.time())

    app.add_middleware(
        LoginRateLimitMiddleware,
        limiter=login_limiter,
        now=_login_now,
    )

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        """Render the login form.

        Minimal in this step: the tracer-bullet test only checks that the
        unauthenticated redirect *target* exists and is not the protected
        page. Form fields and error rendering arrive in the next cycles.
        """
        t: Jinja2Templates = app.state.templates
        assignees: list[Assignee] = app.state.assignees
        return t.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "assignees": assignees,
                "error": None,
            },
        )

    @app.post("/login", response_model=None)
    def login_submit(
        request: Request,
        passphrase: str = Form(""),
        assignee_id: str = Form(""),
    ) -> HTMLResponse | RedirectResponse:
        """Validate the passphrase + role, set a session cookie, redirect."""
        valid_assignee_ids = {a.assignee_id for a in app.state.assignees}
        ok = (
            passphrase == app.state.auth_config.passphrase
            and assignee_id in valid_assignee_ids
        )
        if not ok:
            t = app.state.templates
            return cast(
                HTMLResponse,
                t.TemplateResponse(
                    request=request,
                    name="login.html",
                    context={
                        "request": request,
                        "assignees": app.state.assignees,
                        "error": "Sign in failed.",
                    },
                ),
            )

        now = app.state.now_epoch if app.state.now_epoch is not None else int(time.time())
        session = Session(assignee_id=assignee_id, last_activity=now)
        cookie_value = app.state.authenticator.sign(session)
        response = RedirectResponse(url="/", status_code=303)
        set_session_cookie(
            response,
            value=cookie_value,
            secure=app.state.auth_config.cookie_secure,
        )
        return response

    @app.post("/logout")
    def logout() -> RedirectResponse:
        """Clear the session cookie and redirect to ``/login``.

        Gated by the auth middleware like every other non-public route: a
        request without a valid cookie is redirected to ``/login`` by the
        middleware before reaching here, which is the same end state logout
        produces — so logout from an already-expired session still ends at
        ``/login``.
        """
        response = RedirectResponse(url="/login", status_code=303)
        clear_session_cookie(response)
        return response

    @app.get("/admin/db-snapshot")
    def db_snapshot() -> FileResponse:
        """Download a consistent snapshot of the SQLite database.

        PRD user story 28 / slice-6 issue: a partner can take an out-of-band
        backup before risky maintenance by downloading the current database.
        Gated behind login by the auth middleware (the path is not in
        ``PUBLIC_PATHS``), so an unauthenticated request is redirected to
        ``/login`` before reaching here.

        The route copies the live database into a temp file via SQLite's
        online-backup API rather than streaming the file off disk directly:
        the backup is internally consistent even if a write lands mid-request
        (the running app holds its own connection to the same file), and it
        sidesteps OS-level file-locking on the open database. The temp file is
        removed after the response is sent.
        """
        db_file: str = app.state.db_path
        fd, tmp = tempfile.mkstemp(prefix="tangerine-snapshot-", suffix=".db")
        os.close(fd)
        src = sqlite3.connect(db_file)
        try:
            dst = sqlite3.connect(tmp)
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        stamp = app.state.today.isoformat()
        return FileResponse(
            tmp,
            media_type="application/octet-stream",
            filename=f"tangerine-snapshot-{stamp}.db",
            background=BackgroundTask(os.remove, tmp),
        )

    @app.get("/", response_class=HTMLResponse)
    def root(request: Request) -> HTMLResponse:
        """Yesterday's review — the default 9am landing surface."""
        yesterday = app.state.today - timedelta(days=1)
        return _render_review(request, app, yesterday)

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request, day: str) -> HTMLResponse:
        """The review for a specific day (``?day=YYYY-MM-DD``).

        Day-navigation control arrives in Slice 5; the route is here now so a
        partner can already reach a previous day by URL.
        """
        try:
            review_date = date.fromisoformat(day)
        except ValueError:
            # A malformed date is a client error; surface it as 400 rather than
            # rendering a misleading review for an unintended day.
            return HTMLResponse("Invalid day (expected YYYY-MM-DD).", status_code=400)
        return _render_review(request, app, review_date)

    @app.post("/sync", response_class=HTMLResponse)
    def sync(request: Request) -> HTMLResponse:
        """Trigger a Loyverse sync now (HTMX form).

        Runs the orchestrator against real Loyverse (the HTTP boundary is the
        only stub in tests), then returns a result fragment that swaps into
        the page beside the "Sync now" button. The fragment reports rows
        ingested, menu changes, and any errors — the AC's "the partner can
        trust the sync ran" requirement.

        Even on a Loyverse failure the route returns 200 with the error string
        rendered in the fragment: a 9:01am recovery must not crash the page
        (PRD user story 7). The store is also re-read so the headline numbers
        in the out-of-band swap reflect any sales the sync managed to land
        before the failure.
        """
        store: SqliteLoyverseStore = app.state.store
        credentials: LoyverseCredentials | None = app.state.loyverse_credentials
        today_date: date = app.state.today

        if credentials is None:
            result = SyncResult(
                rows_ingested=0,
                menu_changes=0,
                errors=(
                    "Loyverse credentials not configured "
                    f"(set ${LOYVERSE_TOKEN_ENV}).",
                ),
            )
        else:
            result = run_sync(
                store=store,
                credentials=credentials,
                urlopen=app.state.loyverse_urlopen,
                today=today_date,
            )

        # Refresh yesterday's headline numbers so the partner sees fresh data
        # immediately, without a manual reload (PRD: "the page is reloaded with
        # fresh data"). The review renders against the same store the sync just
        # wrote into, so these numbers reflect the sync result.
        yesterday = today_date - timedelta(days=1)
        review = build_daily_review(source=app.state.source, review_date=yesterday)
        return _render_sync_fragment(request, result, review)

    return app


def _render_review(
    request: Request, app: FastAPI, review_date: date
) -> HTMLResponse:
    """Build the review for ``review_date`` and render the daily template.

    Centralises the build→render path so ``GET /`` and ``GET /review`` cannot
    drift apart. ``has_sales`` drives the empty-state note; everything else is
    taken straight from the engine result.

    ``signed_in_name`` is the partner-visible name of whoever the middleware
    authenticated this request as. Wave 1 has no capture yet; surfacing the
    name on the review pins the AC "the selected role is available to other
    routes" and gives Wave 2 capture flows a working seam in
    ``request.state.assignee_id``.
    """
    templates: Jinja2Templates = app.state.templates
    source: StoreSource = app.state.source

    review = build_daily_review(source=source, review_date=review_date)
    segment_margins = sorted(review.segment_margins, key=_segment_sort_key)
    has_sales = review.revenue != 0 or review.cogs != 0 or any(
        im.units_sold for im in review.daily.item_margins
    )
    # ``store_empty`` distinguishes "nothing has ever been synced" (a friendly
    # first-run empty state with a prominent Sync-now CTA) from "this particular
    # day has no data" (a calm per-day note while other days do have data). The
    # whole-store signal is any sale in the store at all, independent of the day
    # being viewed.
    store_empty = len(source.sales()) == 0

    # Sync freshness: the most recent successful sync's timestamp (derived from
    # the latest menu snapshot, which every successful sync records) drives both
    # the always-on "last synced" indicator and the stale-data banner. ``now``
    # is injectable (``now_epoch``) so tests pin staleness deterministically;
    # production reads the wall clock.
    store: SqliteLoyverseStore = app.state.store
    last_sync = store.last_sync_at()
    now_epoch = app.state.now_epoch
    now_dt = (
        datetime.fromtimestamp(now_epoch, tz=timezone.utc)
        if now_epoch is not None
        else datetime.now(timezone.utc)
    )
    stale = False
    stale_days = 0
    if last_sync is not None:
        age = now_dt - last_sync
        stale = age.total_seconds() > STALE_AFTER_SECONDS
        stale_days = age.days
    # Items excluded from totals come in two flavours: unmapped (no recipe) and
    # unknown_price (a recipe ingredient has no cost entry). The engine exposes
    # only the unmapped ones on the review object; we re-derive both here so the
    # template can surface them together, each labelled with its reason. The
    # partner sees everything that was sold but could not be costed, rather than
    # having unknown-price revenue silently disappear.
    needs_attention = [
        im
        for im in review.daily.item_margins
        if im.unmapped or im.unknown_price
    ]

    # Resolve the signed-in assignee's name (None if the middleware somehow
    # did not stamp one — defensive, should not happen for gated routes).
    assignee_id = getattr(request.state, "assignee_id", None)
    assignees: list[Assignee] = app.state.assignees
    signed_in_name = next(
        (a.name for a in assignees if a.assignee_id == assignee_id), None
    )

    return templates.TemplateResponse(
        request=request,
        name="daily_review.html",
        context={
            "request": request,
            "review": review,
            "segment_margins": segment_margins,
            "has_sales": has_sales,
            "store_empty": store_empty,
            "stale": stale,
            "stale_days": stale_days,
            "last_sync": last_sync,
            "needs_attention": needs_attention,
            "signed_in_name": signed_in_name,
        },
    )


def _render_sync_fragment(
    request: Request, result: SyncResult, review: DailyReview
) -> HTMLResponse:
    """Render the sync-result fragment plus an out-of-band headline refresh.

    The fragment is wrapped in ``<!--section:sync-result-->`` anchors so the UI
    seam test can pin it without depending on incidental markup. Alongside the
    fragment, an out-of-band element (``hx-swap-oob``) carries yesterday's
    refreshed headline numbers: HTMX swaps that into the page on the client,
    so the partner sees fresh revenue / COGS / gross-margin without a manual
    reload (PRD: "the page is reloaded with fresh data").
    """
    del request  # the fragment is built inline; no Jinja context needed
    revenue = _money(review.revenue)
    cogs = _money(review.cogs)
    gross_margin = _money(review.gross_margin)

    error_lines = "".join(
        f"<li class=\"sync-result__error\">{_escape(err)}</li>"
        for err in result.errors
    )
    has_errors = "true" if result.errors else "false"

    html = f"""<!--section:sync-result-->
<section class="sync-result" data-sync-errors="{has_errors}">
  <h3>Sync result</h3>
  <dl class="sync-result__counts">
    <dt>Rows ingested</dt><dd class="sync-result__rows">{result.rows_ingested}</dd>
    <dt>Menu changes</dt><dd class="sync-result__menu-changes">{result.menu_changes}</dd>
  </dl>
  {'<ul class="sync-result__errors">' + error_lines + '</ul>' if result.errors else ''}
</section>
<!--/section:sync-result-->
<!-- HTMX swaps this out-of-band into the page so the partner sees fresh
     headline numbers immediately after a sync, without a manual reload. -->
<div id="headline-oob" hx-swap-oob="true">
  <span class="headline-oob__revenue">{revenue}</span>
  <span class="headline-oob__cogs">{cogs}</span>
  <span class="headline-oob__gross-margin">{gross_margin}</span>
</div>
"""
    return HTMLResponse(html)


def _escape(text: str) -> str:
    """Minimal HTML-escape for sync error strings rendered into the fragment.

    Error strings come from Loyverse responses (untrusted) and could contain
    angle brackets; escaping keeps the fragment well-formed. The page's main
    template relies on Jinja autoescaping, but this fragment is built inline,
    so the escape is explicit here.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


__all__ = ["create_app"]
