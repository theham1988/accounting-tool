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

import calendar
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.background import BackgroundTask

from ..cash_spend import CashSpendEntry
from ..config.loader import load_assignees
from ..coverage import (
    build_item_coverage,
    build_sku_coverage,
    classify_sku,
    pickable_ingredient_skus,
    sku_health,
    sku_role,
)
from ..daily_review import DailyReview, build_daily_review
from ..period_review import build_item_performance, build_period_review
from ..trends import WeekdayAggregate, build_trends
from ..loyverse.config import LoyverseCredentials, cafe_category_ids_from_env
from ..loyverse.source import StoreSource
from ..loyverse.sync import SyncResult, run_sync
from ..margin import CostResolver, cost_breakdown
from ..quantity import QuantityError, estimated_yield, parse_quantity
from ..recipes import RecipeCatalog, find_recipe_cycle
from ..serving_recipe import (
    ServingRecipeSetup,
    create_serving_recipe_setup,
)
from ..sku_authoring import (
    SkuAuthoringError,
    SkuAuthoringInput,
    create_sku as author_new_sku,
    parse_price,
)
from ..storage.config_store import (
    AuditEntry,
    SqliteConfigStore,
    net_price_per_unit,
    seed_config,
)
from ..storage.sqlite_store import SqliteLoyverseStore
from ..upload import generate_template_csv, parse_upload
from ..types import Assignee, Segment, SegmentMargin, SkuHealth, SkuRole
from .sparkline import ChartPoint, bar_row, sparkline_svg
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


#: Stock filter chips (Wave 3 #46). View-layer only — maps onto SKU health.
STOCK_FILTER_NEEDS_WORK = "needs-work"
STOCK_FILTER_RED = "red"
STOCK_FILTER_HEALTHY = "healthy"
_STOCK_FILTERS: tuple[str, ...] = (
    STOCK_FILTER_NEEDS_WORK,
    STOCK_FILTER_RED,
    STOCK_FILTER_HEALTHY,
)


def _normalise_stock_filter(raw: str | None) -> str:
    if raw is None:
        return "all"
    key = raw.strip().lower()
    return key if key in _STOCK_FILTERS else "all"


def _sku_matches_filter(row, filter_key: str) -> bool:  # type: ignore[no-untyped-def]
    if filter_key == STOCK_FILTER_NEEDS_WORK:
        return row.health is not SkuHealth.GREEN
    if filter_key == STOCK_FILTER_RED:
        return row.health is SkuHealth.RED
    if filter_key == STOCK_FILTER_HEALTHY:
        return row.health is SkuHealth.GREEN
    return True


def _item_matches_filter(row, filter_key: str) -> bool:  # type: ignore[no-untyped-def]
    health = row.sku_health
    if filter_key == STOCK_FILTER_NEEDS_WORK:
        return health is not SkuHealth.GREEN
    if filter_key == STOCK_FILTER_RED:
        return health is None or health is SkuHealth.RED
    if filter_key == STOCK_FILTER_HEALTHY:
        return health is SkuHealth.GREEN
    return True


_SKU_HEALTH_SORT: dict[SkuHealth, int] = {
    SkuHealth.RED: 0,
    SkuHealth.YELLOW: 1,
    SkuHealth.GREEN: 2,
}


def _sort_sku_coverage_rows(rows: list) -> list:  # type: ignore[type-arg,no-untyped-def]
    return sorted(rows, key=lambda r: (_SKU_HEALTH_SORT[r.health], r.name.lower()))


def _stock_filter_query(filter_key: str) -> str:
    return "" if filter_key == "all" else f"?filter={filter_key}"


def _stock_tab_urls(filter_key: str) -> dict[str, str]:
    q = _stock_filter_query(filter_key)
    return {"items": f"/items{q}", "skus": f"/skus{q}"}


def _stock_filter_urls(tab: str, filter_key: str) -> dict[str, str]:
    base = f"/{tab}"
    return {
        "all": base,
        STOCK_FILTER_NEEDS_WORK: f"{base}?filter={STOCK_FILTER_NEEDS_WORK}",
        STOCK_FILTER_RED: f"{base}?filter={STOCK_FILTER_RED}",
        STOCK_FILTER_HEALTHY: f"{base}?filter={STOCK_FILTER_HEALTHY}",
    }


def _count_uncostable_items(rows: list) -> int:  # type: ignore[type-arg,no-untyped-def]
    return sum(
        1
        for r in rows
        if r.mapped_sku_id is None or r.sku_health is not SkuHealth.GREEN
    )


def _count_sku_needs_work(rows: list) -> int:  # type: ignore[type-arg,no-untyped-def]
    return sum(1 for r in rows if r.health is not SkuHealth.GREEN)


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
    loyverse_cafe_category_ids: frozenset[str] | None = None,
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
    ``loyverse_cafe_category_ids`` defaults to
    ``$LOYVERSE_CAFE_CATEGORY_IDS`` parsed into a set (ADR-0009); the venue's
    cafe category UUID is opaque, so it lives in the environment, never the
    repo.

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
    cafe_category_ids = (
        loyverse_cafe_category_ids
        if loyverse_cafe_category_ids is not None
        else cafe_category_ids_from_env()
    )
    loyverse_urlopen_param = loyverse_urlopen

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
    # One shared lock serialises every touch of this connection across the
    # two stores that wrap it — two independent locks over one connection
    # would defeat the whole point of locking (see SqliteLoyverseStore's
    # docstring). Wave 1.5 Slice 1 (ADR-0003 decision 1): recipes/costs/
    # mappings are seeded into SQLite once, then read live on every request
    # instead of being loaded into memory at startup.
    #
    # An ``RLock`` (reentrant) rather than a plain ``Lock`` so the config
    # store's :meth:`~tangerine.storage.config_store.SqliteConfigStore.batch`
    # context manager can hold the lock across multiple writes that each
    # re-enter it — the multi-write-stroke transaction (serving-recipe
    # authoring, sold-as-is quick-create) the store exposes for atomic
    # authoring. The Loyverse store never re-enters, so an ``RLock`` is a
    # strict superset of its needs.
    conn_lock = threading.RLock()
    store = SqliteLoyverseStore(conn, lock=conn_lock)
    seed_config(conn, recipes_path=recipes_yaml, costs_path=costs_yaml)

    # The config store's clock is driven by the same injectable ``now_epoch``
    # as the auth middleware and the stale-sync banner, so the "changes in
    # the last 24 hours" comparison in ``_render_review`` reads audit
    # timestamps and "now" from one clock — tests pin both sides together.
    def _config_now_iso() -> str:
        if now_epoch is not None:
            return datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
        return datetime.now(timezone.utc).isoformat()

    config_store = SqliteConfigStore(conn, lock=conn_lock, now=_config_now_iso)
    source = StoreSource(store=store, config=config_store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="Tangerine Phuket — 9am Review", lifespan=lifespan)
    templates = _build_templates()

    # Bottom-nav destinations (Wave 3 foundation chrome, ADR-0006). Registered
    # as a Jinja global so the shared ``_bottom_nav.html`` partial links each
    # cell without any route threading them through. REPORTS is anchored on
    # the app's "today" so it lands on the current calendar month; TODAY, STOCK
    # and LOG are static entry points.
    templates.env.globals["nav_urls"] = {
        "today": "/",
        "reports": f"/review?mode=month&month={today_date.strftime('%Y-%m')}",
        "stock": "/skus",
        "log": "/audit",
    }

    # Mount the packaged static directory so the CSS (and any future HTMX JS)
    # is served at a stable URL. Resolves the directory relative to this module
    # so it works regardless of how the package is installed.
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.state.store = store
    app.state.source = source
    app.state.config_store = config_store
    app.state.db_path = db
    app.state.templates = templates
    app.state.today = today_date
    app.state.loyverse_urlopen = loyverse_urlopen_param
    app.state.loyverse_credentials = (
        LoyverseCredentials(access_token=token, store_id=store_id)
        if token is not None
        else None
    )
    app.state.cafe_category_ids = cafe_category_ids
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
        # A fresh session id per login groups this browser session's config
        # edits in the audit log (Slice 5's "revert this session").
        session = Session(
            assignee_id=assignee_id,
            last_activity=now,
            session_id=uuid4().hex,
        )
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

    @app.get("/")
    def root(request: Request) -> RedirectResponse:
        """Redirect to yesterday's Day-mode review (ADR-0004 decision 4).

        The daily review stays the 9am landing surface (Wave 1 user story 19),
        but the landing state is now the same deep-linkable URL as every
        other mode, so the back button and shared links behave consistently.
        """
        yesterday = app.state.today - timedelta(days=1)
        return RedirectResponse(
            f"/review?mode=day&day={yesterday.isoformat()}", status_code=302
        )

    @app.get("/review", response_class=HTMLResponse)
    def review(
        request: Request,
        mode: str = "day",
        day: str | None = None,
        start: str | None = None,
        end: str | None = None,
        month: str | None = None,
        item: str | None = None,
        metric: str = "gross_margin",
        span: str = "weeks",
        rank: str = "margin",
    ) -> HTMLResponse:
        """The one report page, rendered in a mode (Wave 2 slice 2).

        ``?mode=day&day=YYYY-MM-DD`` is the existing daily review; a bare
        ``?day=...`` (the Wave 1 URL shape) still works and means Day mode,
        so pre-Wave-2 deep links keep resolving.
        ``?mode=period&start=...&end=...`` is the period engine over an
        arbitrary inclusive range; ``?mode=month&month=YYYY-MM`` is the same
        engine over the calendar month. ``?mode=trends&metric=...&span=...``
        is the trend view (slice 5): the period engine per weekly/monthly
        bucket, rendered as server-side SVG. Malformed or backwards params
        are client errors (400) rather than misleading zero-filled reports.

        In Day mode ``?rank=margin|volume`` (issue #45) selects which
        TOP & BOTTOM pair the page shows; the default is ``margin``.
        """
        if mode == "day":
            # The TOP & BOTTOM toggle's query param (issue #45). The two
            # values map to the ranking pair the page shows; anything else is
            # a client error rather than silently falling back to a default
            # the partner did not ask for.
            if rank not in ("margin", "volume"):
                return HTMLResponse(
                    "Invalid rank (expected margin or volume).",
                    status_code=400,
                )
            if day is None:
                day = (app.state.today - timedelta(days=1)).isoformat()
            try:
                review_date = date.fromisoformat(day)
            except ValueError:
                # A malformed date is a client error; surface it as 400 rather
                # than rendering a misleading review for an unintended day.
                return HTMLResponse(
                    "Invalid day (expected YYYY-MM-DD).", status_code=400
                )
            return _render_review(request, app, review_date, rank=rank)
        if mode == "period":
            if start is None or end is None:
                return HTMLResponse(
                    "Period mode needs start and end (YYYY-MM-DD).",
                    status_code=400,
                )
            try:
                start_date = date.fromisoformat(start)
                end_date = date.fromisoformat(end)
            except ValueError:
                return HTMLResponse(
                    "Invalid start/end (expected YYYY-MM-DD).", status_code=400
                )
            if end_date < start_date:
                return HTMLResponse(
                    "Period end precedes start.", status_code=400
                )
            return _render_period_review(request, app, start_date, end_date)
        if mode == "month":
            if month is None:
                return HTMLResponse(
                    "Month mode needs month (YYYY-MM).", status_code=400
                )
            try:
                first_day = date.fromisoformat(f"{month}-01")
            except ValueError:
                return HTMLResponse(
                    "Invalid month (expected YYYY-MM).", status_code=400
                )
            days_in_month = calendar.monthrange(first_day.year, first_day.month)[1]
            last_day = first_day.replace(day=days_in_month)
            return _render_period_review(
                request, app, first_day, last_day, mode="month"
            )
        if mode == "item":
            if item is None or start is None or end is None:
                return HTMLResponse(
                    "Item mode needs item, start, and end.", status_code=400
                )
            try:
                start_date = date.fromisoformat(start)
                end_date = date.fromisoformat(end)
            except ValueError:
                return HTMLResponse(
                    "Invalid start/end (expected YYYY-MM-DD).", status_code=400
                )
            if end_date < start_date:
                return HTMLResponse(
                    "Period end precedes start.", status_code=400
                )
            return _render_item_review(request, app, item, start_date, end_date)
        if mode == "trends":
            return _render_trends(request, app, metric=metric, span=span)
        return HTMLResponse(
            "Unknown mode (expected day, period, month, item, or trends).",
            status_code=400,
        )

    @app.get("/admin", response_class=HTMLResponse)
    def admin_landing(request: Request) -> HTMLResponse:
        """The Admin destination (Wave 2 slice 3, ADR-0004 decision 4).

        The app's second top-level surface beside the Review: gathers the
        Wave 1.5 config surfaces (SKUs, items, upload, audit) plus the
        fixed-cost entry under one navigation umbrella. The gathered pages
        keep their existing paths so pre-Wave-2 deep links (e.g. the daily
        review's needs-attention link into ``/items?item=...``) still
        resolve.
        """
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request, name="admin.html", context={"request": request}
        )

    def _fixed_costs_page_context(
        request: Request,
        *,
        form_error: str | None = None,
        form_values: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Shared template context for the fixed-costs admin page.

        Computes the active recurring monthly total the CURRENT card header
        shows. Presentation-only — end/delete semantics live in the store.
        """
        cfg: SqliteConfigStore = app.state.config_store
        entries = cfg.fixed_costs()
        recurring_monthly_total = sum(
            (entry.amount for entry in entries
             if entry.kind == "recurring" and entry.ended_at is None),
            Decimal("0"),
        )
        return {
            "request": request,
            "entries": entries,
            "recurring_monthly_total": recurring_monthly_total,
            "form_error": form_error,
            "form_values": form_values,
        }

    @app.get("/admin/fixed-costs", response_class=HTMLResponse)
    def fixed_costs_page(request: Request) -> HTMLResponse:
        """The fixed-cost entry surface (Wave 3 Reports sub-page).

        One page: the add form plus the current entries with end/delete
        actions. Fixed costs are entity-level — the page never asks for a
        segment (ADR-0004 decision 3: never allocated). Bottom nav stays
        with REPORTS active (issue #50).
        """
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="fixed_costs.html",
            context=_fixed_costs_page_context(request),
        )

    @app.post("/admin/fixed-costs", response_model=None)
    def create_fixed_cost(
        request: Request,
        label: str = Form(""),
        category: str = Form("other"),
        amount: str = Form(""),
        kind: str = Form("recurring"),
        period: str = Form(""),
    ) -> HTMLResponse | RedirectResponse:
        """Store a new fixed cost from the form, audit-logged.

        ``period`` arrives as ``YYYY-MM`` (the HTML month input's format):
        the month a one-off applies to, or the first month a recurring cost
        applies from. The redirect lands back on the list, which re-reads
        the DB — what the partner sees after saving is what the next Month
        view will subtract.

        On a validation failure the page re-renders (200) with the inline
        error in the ADD A COST card and the partner's submitted values
        echoed back into the form — never a bare 400, never a silent save
        (issue #50 AC). Nothing is written on a failed submit.
        """
        cfg: SqliteConfigStore = app.state.config_store
        t: Jinja2Templates = app.state.templates
        label = label.strip()
        amount_stripped = amount.strip()

        def _rerender_with_error(message: str) -> HTMLResponse:
            return t.TemplateResponse(
                request=request,
                name="fixed_costs.html",
                context=_fixed_costs_page_context(
                    request,
                    form_error=message,
                    form_values={
                        "label": label,
                        "category": category,
                        "amount": amount_stripped,
                        "kind": kind,
                        "period": period.strip(),
                    },
                ),
            )

        # A missing label or amount is the common mistake — the canonical
        # "nothing was saved" message the design specifies.
        if not label or not amount_stripped:
            return _rerender_with_error(
                "Needs a label and an amount — nothing was saved."
            )
        if kind not in ("recurring", "oneoff"):
            return _rerender_with_error(
                "Kind must be recurring or one-off — nothing was saved."
            )
        try:
            amount_value = Decimal(amount_stripped)
        except InvalidOperation:
            return _rerender_with_error(
                "Amount must be a number — nothing was saved."
            )
        if amount_value < 0:
            return _rerender_with_error(
                "Amount must be 0 or more — nothing was saved."
            )
        try:
            first_day = date.fromisoformat(f"{period}-01")
        except (ValueError, TypeError):
            return _rerender_with_error(
                "Pick a from-month — nothing was saved."
            )
        cfg.create_fixed_cost(
            label=label,
            category=category,
            amount=amount_value,
            kind=kind,
            period=(first_day.year, first_day.month),
            created_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        return RedirectResponse(url="/admin/fixed-costs", status_code=303)

    @app.post("/admin/fixed-costs/{entry_id}/end", response_model=None)
    def end_fixed_cost(
        request: Request, entry_id: int
    ) -> HTMLResponse | RedirectResponse:
        """Stop a recurring fixed cost applying after this month (logged).

        Ending is dated *today*: the month the partner ends it in still
        charges in full (it was already owed); later months charge nothing.
        """
        cfg: SqliteConfigStore = app.state.config_store
        ended = cfg.end_fixed_cost(
            entry_id,
            ended_on=app.state.today,
            updated_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        if not ended:
            return HTMLResponse("Unknown fixed cost.", status_code=404)
        return RedirectResponse(url="/admin/fixed-costs", status_code=303)

    @app.post("/admin/fixed-costs/{entry_id}/delete", response_model=None)
    def delete_fixed_cost(
        request: Request, entry_id: int
    ) -> HTMLResponse | RedirectResponse:
        """Remove a fixed cost from every month (logged; revert restores)."""
        cfg: SqliteConfigStore = app.state.config_store
        deleted = cfg.delete_fixed_cost(
            entry_id,
            deleted_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        if not deleted:
            return HTMLResponse("Unknown fixed cost.", status_code=404)
        return RedirectResponse(url="/admin/fixed-costs", status_code=303)

    # --- Suppliers admin (issue #94) ------------------------------------------
    #
    # The controlled vendor list the cash-spend entry surface (slice #96)
    # will FK into. Mirrors the fixed-costs admin shape, minus recurring /
    # one-off / ended-at — vendors have no lifecycle, just CRUD. Every write
    # is audit-logged through the existing machinery (``table_name=
    # 'suppliers'``), so ``/audit`` shows the change and Revert works
    # without any new revert code.

    def _suppliers_page_context(
        request: Request,
        *,
        form_error: str | None = None,
        form_values: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Shared template context for the suppliers admin page."""
        cfg: SqliteConfigStore = app.state.config_store
        return {
            "request": request,
            "suppliers": cfg.suppliers(),
            "form_error": form_error,
            "form_values": form_values,
        }

    @app.get("/admin/suppliers", response_class=HTMLResponse)
    def suppliers_page(request: Request) -> HTMLResponse:
        """The vendor list (issue #94). One page: add form + current rows."""
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="suppliers.html",
            context=_suppliers_page_context(request),
        )

    @app.post("/admin/suppliers", response_model=None)
    def create_supplier(
        request: Request,
        supplier_id: str = Form(""),
        name: str = Form(""),
    ) -> HTMLResponse | RedirectResponse:
        """Add a new vendor from the form, audit-logged.

        On a validation failure the page re-renders (200) with the inline
        error and the partner's submitted values echoed back — never a bare
        400, never a silent save (the Wave 1.5 admin-surface pattern). A
        duplicate ``supplier_id`` re-renders with the same shape: the PK is
        the FK target for #96's cash-spend rows, so two "makro" rows would
        silently corrupt per-vendor aggregation.
        """
        cfg: SqliteConfigStore = app.state.config_store
        t: Jinja2Templates = app.state.templates
        supplier_id = supplier_id.strip()
        name = name.strip()

        def _rerender_with_error(message: str) -> HTMLResponse:
            return t.TemplateResponse(
                request=request,
                name="suppliers.html",
                context=_suppliers_page_context(
                    request,
                    form_error=message,
                    form_values={"supplier_id": supplier_id, "name": name},
                ),
            )

        if not supplier_id or not name:
            return _rerender_with_error(
                "Needs an id and a name — nothing was saved."
            )
        created = cfg.create_supplier(
            supplier_id,
            name=name,
            created_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        if not created:
            return _rerender_with_error(
                f"A supplier with id '{supplier_id}' already exists"
                " — nothing was saved."
            )
        return RedirectResponse(url="/admin/suppliers", status_code=303)

    @app.post("/admin/suppliers/{supplier_id}/edit", response_model=None)
    def edit_supplier(
        request: Request, supplier_id: str, name: str = Form("")
    ) -> HTMLResponse | RedirectResponse:
        """Rename a vendor (typo fix). The id is immutable — it is the FK target."""
        cfg: SqliteConfigStore = app.state.config_store
        t: Jinja2Templates = app.state.templates
        name = name.strip()
        if not name:
            return t.TemplateResponse(
                request=request,
                name="suppliers.html",
                context=_suppliers_page_context(
                    request,
                    form_error="Needs a name — nothing was saved.",
                    form_values={"supplier_id": supplier_id, "name": ""},
                ),
            )
        updated = cfg.update_supplier(
            supplier_id,
            name=name,
            updated_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        if not updated:
            return HTMLResponse("Unknown supplier.", status_code=404)
        return RedirectResponse(url="/admin/suppliers", status_code=303)

    @app.post("/admin/suppliers/{supplier_id}/delete", response_model=None)
    def delete_supplier(
        request: Request, supplier_id: str
    ) -> HTMLResponse | RedirectResponse:
        """Hard-delete a vendor (logged; revert restores it).

        Refuses with a partner-readable message when the vendor is in use by
        a cash-spend row — the route-level referential-integrity guard that
        #96's FK constraint will enforce at the DB layer. The guard ships
        now against the empty table and stands ready.
        """
        cfg: SqliteConfigStore = app.state.config_store
        t: Jinja2Templates = app.state.templates
        # The authoritative referential-integrity check is the *inner* one —
        # ``_delete_supplier_impl`` re-checks ``supplier_in_use`` inside the
        # held transaction (config_store.py), so a referencing row that
        # appears between this read and the delete still blocks the delete.
        # This outer read is only here to choose the response shape: re-render
        # the page with a partner-readable message rather than letting the
        # store's bare-False fall through to the "not found" branch.
        if cfg.supplier_in_use(supplier_id):
            return t.TemplateResponse(
                request=request,
                name="suppliers.html",
                context=_suppliers_page_context(
                    request,
                    form_error=(
                        f"'{supplier_id}' is in use by cash-spend rows and"
                        " cannot be deleted — remove those rows first."
                    ),
                ),
            )
        deleted = cfg.delete_supplier(
            supplier_id,
            deleted_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        if not deleted:
            return HTMLResponse("Unknown supplier.", status_code=404)
        return RedirectResponse(url="/admin/suppliers", status_code=303)

    # --- Spend buckets (issue #95, parent #82) --------------------------------
    #
    # The controlled vocabulary cash-spend rows (slice #96) FK into. This
    # slice ships the partner-facing admin surface: list the seeded six
    # plus any partner additions, create a new bucket, retire one a partner
    # no longer uses, hard-delete an empty typo bucket. Every write goes
    # through ``audit_log`` with ``table_name='spend_buckets'``, so the
    # existing per-entry / per-session Revert works without new revert
    # code — the ADR-0003 pattern, unchanged.
    #
    # Buckets are cash-spend-only and deliberately NOT shared with
    # ``fixed_costs.category`` (issue #82 decision D): the cash-spend
    # buckets are COGS-side product-family; fixed-costs categories are
    # entity-overhead. They overlap on ``staff`` and ``rent`` only because
    # the HTML crammed them into one column.

    def _spend_buckets_page_context(
        request: Request,
        *,
        form_error: str | None = None,
        form_values: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Shared template context for the spend-buckets admin page.

        Presentation-only — create / retire / delete semantics live in the
        store. The list is rendered verbatim from ``spend_buckets()`` (seeded
        six first in display order, then partner additions in creation
        order), including retired rows so a partner keeps the historical
        context that ``retired_at`` preserves.
        """
        cfg: SqliteConfigStore = app.state.config_store
        buckets = cfg.spend_buckets()
        return {
            "request": request,
            "buckets": buckets,
            "form_error": form_error,
            "form_values": form_values,
        }

    @app.get("/admin/spend-buckets", response_class=HTMLResponse)
    def spend_buckets_page(request: Request) -> HTMLResponse:
        """The spend-bucket vocabulary surface (issue #95).

        One page: the add form plus the current buckets with retire/delete
        actions. Retired buckets stay visible (struck-through) so a partner
        reading the page keeps the historical context that ``retired_at``
        preserves; slice #96's new-entry picker filters them out.
        """
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="spend_buckets.html",
            context=_spend_buckets_page_context(request),
        )

    @app.post("/admin/spend-buckets", response_model=None)
    def create_spend_bucket(
        request: Request,
        bucket_id: str = Form(""),
        name: str = Form(""),
    ) -> HTMLResponse | RedirectResponse:
        """Add a new bucket from the form, audit-logged.

        ``bucket_id`` is the stable slug (lower-cased and trimmed here so
        ``Taps`` and ``taps`` don't drift into two rows); ``name`` is the
        display label. On a validation failure the page re-renders with an
        inline error and the partner's submitted values echoed back — never
        a bare 400, never a silent save. Nothing is written on a failed
        submit.
        """
        cfg: SqliteConfigStore = app.state.config_store
        t: Jinja2Templates = app.state.templates
        bucket_id = bucket_id.strip().lower()
        name = name.strip()

        def _rerender_with_error(message: str) -> HTMLResponse:
            return t.TemplateResponse(
                request=request,
                name="spend_buckets.html",
                context=_spend_buckets_page_context(
                    request,
                    form_error=message,
                    form_values={"bucket_id": bucket_id, "name": name},
                ),
            )

        if not bucket_id or not name:
            return _rerender_with_error(
                "Needs a bucket id and a name — nothing was saved."
            )
        try:
            cfg.create_spend_bucket(
                bucket_id,
                name=name,
                created_by=request.state.assignee_id,
                session_id=request.state.session_id,
            )
        except sqlite3.IntegrityError:
            return _rerender_with_error(
                f"A bucket named '{bucket_id}' already exists — nothing was saved."
            )
        return RedirectResponse(url="/admin/spend-buckets", status_code=303)

    @app.post("/admin/spend-buckets/{bucket_id}/retire", response_model=None)
    def retire_spend_bucket(
        request: Request, bucket_id: str
    ) -> HTMLResponse | RedirectResponse:
        """Soft-retire a bucket (logged; historical aggregation stays honest)."""
        cfg: SqliteConfigStore = app.state.config_store
        retired = cfg.retire_spend_bucket(
            bucket_id,
            retired_at=app.state.today.isoformat(),
            updated_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        if not retired:
            return HTMLResponse("Unknown spend bucket.", status_code=404)
        return RedirectResponse(url="/admin/spend-buckets", status_code=303)

    @app.post("/admin/spend-buckets/{bucket_id}/delete", response_model=None)
    def delete_spend_bucket(
        request: Request, bucket_id: str
    ) -> HTMLResponse | RedirectResponse:
        """Hard-delete an empty bucket (logged; revert restores).

        Deleting a bucket that slice #96's cash-spend rows reference would
        corrupt historical aggregation, so the route guards on
        :meth:`SqliteConfigStore.spend_bucket_in_use` and rejects with a
        partner-readable message. The FK constraint itself lands with #96;
        this route-level guard ships now so the surface is honest from day
        one.
        """
        cfg: SqliteConfigStore = app.state.config_store
        if cfg.spend_bucket_in_use(bucket_id):
            return HTMLResponse(
                "That bucket is in use by cash-spend rows — retire it instead.",
                status_code=409,
            )
        deleted = cfg.delete_spend_bucket(
            bucket_id,
            deleted_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        if not deleted:
            return HTMLResponse("Unknown spend bucket.", status_code=404)
        return RedirectResponse(url="/admin/spend-buckets", status_code=303)

    # --- Cash spend (issue #96, parent #82) -----------------------------------
    #
    # The partner-facing entry surface for cash-basis supplier purchases —
    # the row that produces the HTML's "Cost of goods — purchases (cash)"
    # line. Filterable list (by date range + bucket + supplier), create,
    # edit, delete, behind the existing auth. Supplier and bucket pickers
    # draw from #94 and #95; retired buckets are not offered in the create
    # picker (a retired bucket stays in the spend-buckets list so
    # historical rows keep aggregating under it, but new entry excludes it).
    #
    # Every write goes through ``audit_log`` with ``table_name='cash_spend'``
    # (registered in ``_PK_COLUMNS``), so ``/audit`` shows each change and
    # the existing per-entry / per-session Revert restores it — the
    # ADR-0003 pattern, unchanged.

    def _cash_spend_page_context(
        request: Request,
        *,
        rows: list[CashSpendEntry] | None = None,
        suppliers: list | None = None,
        buckets: list | None = None,
        filters: dict[str, str] | None = None,
        form_error: str | None = None,
        form_values: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Shared template context for the cash-spend admin page.

        Presentation-only — create / edit / delete semantics live in the
        store. ``rows`` is already filtered (the route applies the query
        params before passing them in); ``suppliers`` and ``buckets`` feed
        the pickers, with retired buckets excluded from the create form's
        bucket picker by the template filtering on ``retired_at``.
        """
        return {
            "request": request,
            "rows": rows if rows is not None else [],
            "suppliers": suppliers if suppliers is not None else [],
            "buckets": buckets if buckets is not None else [],
            "filters": filters or {},
            "form_error": form_error,
            "form_values": form_values or {},
        }

    def _parse_row_date(value: str) -> date | None:
        """Tolerant ISO-date parse for the filter + form inputs."""
        value = value.strip()
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    @app.get("/admin/cash-spend", response_class=HTMLResponse)
    def cash_spend_page(
        request: Request,
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        bucket: str | None = Query(default=None),
        supplier_id: str | None = Query(default=None),
    ) -> HTMLResponse:
        """The cash-spend entry surface (issue #96).

        Renders the filter form (date range + bucket + supplier) and the
        matching rows. Retired buckets stay selectable in the *filter*
        (a partner filtering for historical spend under a now-retired
        bucket should see those rows); only the *create* picker excludes
        them.
        """
        cfg: SqliteConfigStore = app.state.config_store
        t: Jinja2Templates = app.state.templates
        all_rows = cfg.cash_spend_rows()
        start_d = _parse_row_date(start) if start else None
        end_d = _parse_row_date(end) if end else None
        bucket_q = bucket.strip() if bucket else None
        supplier_q = supplier_id.strip() if supplier_id else None

        def _matches(row: CashSpendEntry) -> bool:
            if start_d is not None and row.date < start_d:
                return False
            if end_d is not None and row.date > end_d:
                return False
            if bucket_q and row.bucket_id != bucket_q:
                return False
            if supplier_q and row.supplier_id != supplier_q:
                return False
            return True

        rows = [r for r in all_rows if _matches(r)]
        filters = {
            "start": start or "",
            "end": end or "",
            "bucket": bucket or "",
            "supplier_id": supplier_id or "",
        }
        return t.TemplateResponse(
            request=request,
            name="cash_spend.html",
            context=_cash_spend_page_context(
                request,
                rows=rows,
                suppliers=cfg.suppliers(),
                buckets=cfg.spend_buckets(),
                filters=filters,
            ),
        )

    @app.post("/admin/cash-spend", response_model=None)
    def create_cash_spend_row(
        request: Request,
        entry_date: str = Form(""),
        supplier_id: str = Form(""),
        description: str = Form(""),
        bucket_id: str = Form(""),
        amount: str = Form(""),
        vat_inclusive: str = Form(""),
    ) -> HTMLResponse | RedirectResponse:
        """Add a cash-spend row from the form, audit-logged.

        On a validation failure the page re-renders (200) with the inline
        error and the partner's submitted values echoed back — never a
        bare 400, never a silent save (the Wave 1.5 admin-surface pattern).
        The ``vat_inclusive`` checkbox ships as an unchecked-by-default
        input; ADR-0003 decision 4's "default false" rule is enforced by
        the column default and reflected here.
        """
        cfg: SqliteConfigStore = app.state.config_store
        t: Jinja2Templates = app.state.templates
        form_values = {
            "date": entry_date.strip(),
            "supplier_id": supplier_id.strip(),
            "description": description.strip(),
            "bucket_id": bucket_id.strip(),
            "amount": amount.strip(),
            "vat_inclusive": "on" if vat_inclusive else "",
        }

        def _rerender(message: str) -> HTMLResponse:
            return t.TemplateResponse(
                request=request,
                name="cash_spend.html",
                context=_cash_spend_page_context(
                    request,
                    rows=cfg.cash_spend_rows(),
                    suppliers=cfg.suppliers(),
                    buckets=cfg.spend_buckets(),
                    form_error=message,
                    form_values=form_values,
                ),
            )

        parsed_date = _parse_row_date(entry_date)
        if parsed_date is None:
            return _rerender("Needs a valid date (YYYY-MM-DD) — nothing was saved.")
        if not supplier_id.strip() or not bucket_id.strip():
            return _rerender(
                "Needs a supplier and a bucket — nothing was saved."
            )
        if not description.strip():
            return _rerender("Needs a description — nothing was saved.")
        try:
            parsed_amount = Decimal(amount.strip())
        except (InvalidOperation, ValueError):
            return _rerender(
                f"Amount '{amount}' is not a number — nothing was saved."
            )
        if parsed_amount <= 0:
            return _rerender(
                "Amount must be positive — nothing was saved."
            )
        # Guard against a partner picking a retired bucket (the picker
        # excludes them, but a hand-crafted POST could carry one). A
        # retired bucket stays in the table for history; new entry
        # against it is refused so the live-vocabulary invariant holds.
        bucket = next(
            (b for b in cfg.spend_buckets() if b.bucket_id == bucket_id.strip()),
            None,
        )
        if bucket is None:
            return _rerender(
                f"Unknown bucket '{bucket_id}' — nothing was saved."
            )
        if bucket.retired_at is not None:
            return _rerender(
                f"Bucket '{bucket_id}' is retired — pick a live bucket."
            )
        cfg.create_cash_spend(
            CashSpendEntry(
                row_id=0,
                date=parsed_date,
                supplier_id=supplier_id.strip(),
                description=description.strip(),
                bucket_id=bucket_id.strip(),
                amount=parsed_amount,
                vat_inclusive=bool(vat_inclusive),
            ),
            created_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        return RedirectResponse(url="/admin/cash-spend", status_code=303)

    @app.post("/admin/cash-spend/{row_id}/edit", response_model=None)
    def edit_cash_spend_row(
        request: Request,
        row_id: int,
        entry_date: str = Form(""),
        supplier_id: str = Form(""),
        description: str = Form(""),
        bucket_id: str = Form(""),
        amount: str = Form(""),
        vat_inclusive: str = Form(""),
    ) -> HTMLResponse | RedirectResponse:
        """Replace a row from the edit form, audit-logged (revert restores).

        On a validation failure the page re-renders (200) with the inline
        error and the partner's submitted values echoed back — the same
        contract as the create route (never a bare 400). The edit form
        posts the whole row; this writes every field from the posted
        values. The bucket picker on an edit form may offer the row's
        *current* bucket even if it is now retired (so a partner can fix
        an amount typo without first re-routing the row to a live bucket)
        — but we still refuse to *move* a row onto a different retired
        bucket.
        """
        cfg: SqliteConfigStore = app.state.config_store
        t: Jinja2Templates = app.state.templates
        existing = next(
            (r for r in cfg.cash_spend_rows() if r.row_id == row_id), None
        )
        if existing is None:
            return HTMLResponse("Unknown cash-spend row.", status_code=404)
        form_values = {
            "date": entry_date.strip(),
            "supplier_id": supplier_id.strip(),
            "description": description.strip(),
            "bucket_id": bucket_id.strip(),
            "amount": amount.strip(),
            "vat_inclusive": "on" if vat_inclusive else "",
        }

        def _rerender(message: str) -> HTMLResponse:
            return t.TemplateResponse(
                request=request,
                name="cash_spend.html",
                context=_cash_spend_page_context(
                    request,
                    rows=cfg.cash_spend_rows(),
                    suppliers=cfg.suppliers(),
                    buckets=cfg.spend_buckets(),
                    form_error=message,
                    form_values=form_values,
                ),
            )

        parsed_date = _parse_row_date(entry_date)
        if parsed_date is None:
            return _rerender("Needs a valid date (YYYY-MM-DD) — nothing was saved.")
        try:
            parsed_amount = Decimal(amount.strip())
        except (InvalidOperation, ValueError):
            return _rerender(
                f"Amount '{amount}' is not a number — nothing was saved."
            )
        if parsed_amount <= 0:
            return _rerender(
                "Amount must be positive — nothing was saved."
            )
        # Refuse moving a row onto a retired bucket it is not already on.
        if bucket_id.strip() != existing.bucket_id:
            bucket = next(
                (b for b in cfg.spend_buckets() if b.bucket_id == bucket_id.strip()),
                None,
            )
            if bucket is None:
                return _rerender(
                    f"Unknown bucket '{bucket_id}' — nothing was saved."
                )
            if bucket.retired_at is not None:
                return _rerender(
                    f"Bucket '{bucket_id}' is retired — pick a live bucket."
                )
        cfg.update_cash_spend(
            CashSpendEntry(
                row_id=row_id,
                date=parsed_date,
                supplier_id=supplier_id.strip() or existing.supplier_id,
                description=description.strip() or existing.description,
                bucket_id=bucket_id.strip() or existing.bucket_id,
                amount=parsed_amount,
                vat_inclusive=bool(vat_inclusive),
            ),
            updated_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        return RedirectResponse(url="/admin/cash-spend", status_code=303)

    @app.post("/admin/cash-spend/{row_id}/delete", response_model=None)
    def delete_cash_spend_row(
        request: Request, row_id: int
    ) -> HTMLResponse | RedirectResponse:
        """Remove a cash-spend row (logged; revert restores it).

        Deletion is for rows that should never have existed (a typo, a
        duplicate); the audit-log revert is the safety net. Returns 404
        for an unknown id.
        """
        cfg: SqliteConfigStore = app.state.config_store
        deleted = cfg.delete_cash_spend(
            row_id,
            deleted_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        if not deleted:
            return HTMLResponse("Unknown cash-spend row.", status_code=404)
        return RedirectResponse(url="/admin/cash-spend", status_code=303)

    @app.get("/skus", response_class=HTMLResponse)
    def skus_view(request: Request, filter: str | None = Query(default=None)) -> HTMLResponse:
        """The SKU view (Wave 1.5, Slice 2): one row per SKU, mapping/recipe/
        pricing health at a glance. Read-only — no editing yet.

        Stock filter chips (ALL / NEEDS WORK / RED / HEALTHY) arrive as
        ``?filter=`` query params mapping onto SKU-health classification.
        """
        cfg: SqliteConfigStore = app.state.config_store
        all_rows = build_sku_coverage(
            skus=cfg.skus(), recipes=cfg.recipes(), mappings=cfg.mappings(), cost=cfg.cost_book()
        )
        active_filter = _normalise_stock_filter(filter)
        rows = _sort_sku_coverage_rows(
            [r for r in all_rows if _sku_matches_filter(r, active_filter)]
        )
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="sku_coverage.html",
            context={
                "request": request,
                "rows": rows,
                "total_count": len(all_rows),
                "needs_work_count": _count_sku_needs_work(all_rows),
                "filter": active_filter,
                "filter_urls": _stock_filter_urls("skus", active_filter),
                "tab_urls": _stock_tab_urls(active_filter),
                "stock_tab": "skus",
            },
        )

    @app.get("/skus/new", response_class=HTMLResponse)
    def new_sku_form(request: Request, item_id: str | None = None) -> HTMLResponse:
        """The create-SKU form — one page, two entry points.

        The SKU view's "New SKU" button arrives plain; the item coverage
        view's "create new SKU…" option arrives with ``?item_id=``, and the
        created SKU is mapped to that item in the same stroke. (Registered
        before ``GET /skus/{sku_id}`` so "new" is never read as a sku_id.)
        """
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="sku_new.html",
            context={"request": request, "item_id": item_id},
        )

    @app.post("/skus", response_model=None)
    def create_sku(
        request: Request,
        sku_id: str = Form(""),
        name: str = Form(""),
        unit: str = Form(""),
        price: str = Form(""),
        recipe_sku_id: str = Form(""),
        item_id: str = Form(""),
    ) -> HTMLResponse | RedirectResponse:
        """Create a new SKU and land in its (empty) editor.

        The four fields are the inline sub-form's shape (issue 26): sku_id,
        name, unit, price. ``price`` is the per-unit price in THB, optional —
        stored net as entered (pack quantity 1, no VAT flag; a receipt-shaped
        cost can replace it any time through the cost editor).

        Two callers, two responses: a plain form post (the "New SKU" page)
        redirects into the new SKU's editor; the recipe editor's inline
        sub-form posts over HTMX and gets back a replacement picker with the
        new ingredient selected — so the partner finishes the recipe without
        context-switching (``recipe_sku_id`` says whose editor the picker
        belongs to, for the preview wiring).

        The stroke itself — SKU + optional cost + optional mapping, written
        atomically when more than one write is involved — lives in the
        :mod:`tangerine.sku_authoring` domain module so it is testable
        without HTTP. This route is a thin adapter: parse the form, call the
        module, translate :class:`~tangerine.sku_authoring.SkuAuthoringError`
        into HTTP 400 (the pattern the serving-recipe setup will reuse).
        """
        cfg: SqliteConfigStore = app.state.config_store
        price_value: Decimal | None = None
        if price.strip():
            try:
                price_value = parse_price(price)
            except SkuAuthoringError as err:
                return HTMLResponse(str(err), status_code=400)
        try:
            author_new_sku(
                cfg,
                SkuAuthoringInput(
                    sku_id=sku_id,
                    name=name,
                    unit=unit,
                    price_per_unit=price_value,
                    item_id=item_id,
                ),
                created_by=request.state.assignee_id,
                effective_on=app.state.today,
                session_id=request.state.session_id,
            )
        except SkuAuthoringError as err:
            return HTMLResponse(str(err), status_code=400)
        # The module strips the identity fields; read the canonical sku_id
        # back so the redirect/HTMX paths use exactly what was persisted.
        persisted_sku_id = sku_id.strip()
        if request.headers.get("hx-request"):
            t: Jinja2Templates = app.state.templates
            return cast(
                HTMLResponse,
                t.TemplateResponse(
                    request=request,
                    name="ingredient_picker_fragment.html",
                    context={
                        "request": request,
                        "recipe_sku_id": recipe_sku_id,
                        # Same filter as the editor page: a just-created SKU
                        # is purchasable (no recipe yet), so it appears.
                        "all_skus": pickable_ingredient_skus(
                            cfg.skus(), cfg.recipes()
                        ),
                        "selected_sku_id": persisted_sku_id,
                    },
                ),
            )
        return RedirectResponse(url=f"/skus/{persisted_sku_id}", status_code=303)

    @app.get("/items/{item_id}/sold-as-is", response_class=HTMLResponse)
    def sold_as_is_form(request: Request, item_id: str) -> HTMLResponse:
        """The sold-as-is quick-create form (issue 38).

        Reachable from an unmapped item's row in ``/items``; collects the
        seven facts that cost a directly-sold purchasable through the same
        path as every dish and posts to the sibling handler below.
        """
        store: SqliteLoyverseStore = app.state.store
        item = store.current_menu().get(item_id)
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="sold_as_is.html",
            context={
                "request": request,
                "item_id": item_id,
                "item_name": item.name if item is not None else None,
            },
        )

    @app.post("/items/{item_id}/sold-as-is", response_model=None)
    def sold_as_is_quick_create(
        request: Request,
        item_id: str,
        sku_id: str = Form(""),
        name: str = Form(""),
        unit: str = Form(""),
        pack_price: str = Form(""),
        pack_quantity: str = Form(""),
        vat_inclusive: str = Form(""),
        serving_size: str = Form(""),
    ) -> HTMLResponse | RedirectResponse:
        """The sold-as-is quick-create (issue 38 / parent PRD 33).

        From an unmapped item's row, one stroke creates the four facts that
        cost a directly-sold purchasable through the same path as every dish:
        the purchasable SKU (receipt-priced), its one-line serving recipe, the
        produced sold SKU the recipe outputs, and the item → sold-SKU mapping.
        No second costing path exists — the serving recipe is an ordinary
        recipe (one ingredient line, yield 1 in the sold SKU's unit).

        This route is a thin adapter: it resolves the Loyverse item's segment
        (the one fact the form cannot carry), packs the form fields into a
        :class:`~tangerine.serving_recipe.ServingRecipeSetup`, and delegates
        the stroke — validation, the five writes, the atomic ``batch()`` — to
        :func:`~tangerine.serving_recipe.create_serving_recipe_setup`.
        :class:`~tangerine.sku_authoring.SkuAuthoringError` is the partner-
        facing error type that module raises; the route maps it verbatim to
        HTTP 400. "sold-as-is" is the Books UI label; the domain name lives
        in the module.
        """
        cfg: SqliteConfigStore = app.state.config_store
        store: SqliteLoyverseStore = app.state.store
        item = store.current_menu().get(item_id)
        # The sold SKU inherits the item's segment so the segment-CM view
        # attributes the sale correctly; the purchasable stays segment-NULL
        # (an ingredient may feed both cafe and bar).
        sold_segment = item.segment if item is not None else None
        try:
            sold_sku_id = create_serving_recipe_setup(
                cfg,
                item_id=item_id,
                setup=ServingRecipeSetup(
                    sku_id=sku_id,
                    name=name,
                    unit=unit,
                    pack_price=pack_price,
                    pack_quantity=pack_quantity,
                    vat_inclusive=bool(vat_inclusive),
                    serving_qty=serving_size,
                    sold_segment=sold_segment,
                ),
                actor=request.state.assignee_id,
                session_id=request.state.session_id,
                today=app.state.today,
            )
        except SkuAuthoringError as err:
            return HTMLResponse(str(err), status_code=400)
        return RedirectResponse(url=f"/skus/{sold_sku_id}", status_code=303)

    @app.get("/skus/{sku_id}", response_class=HTMLResponse)
    def sku_detail(
        request: Request, sku_id: str, saved: str | None = None
    ) -> HTMLResponse:
        """The editor page for one SKU: recipe (Slice 4) + cost (Slice 3).

        The recipe section renders the ingredient rows in stored order with
        an existing-SKUs-only picker (plus inline create); the cost section
        captures what the partner actually sees on a receipt — pack price,
        pack quantity, VAT-inclusive flag — with the current stored (net)
        cost for context.

        ``saved`` carries the in-place confirmation signal (issue #48): the
        recipe/cost save routes redirect with ``?saved=recipe|cost`` and the
        template renders a "SAVED — logged to the audit log" banner so the
        partner knows the edit landed and was audit-logged.
        """
        cfg: SqliteConfigStore = app.state.config_store
        sku = cfg.sku(sku_id)
        if sku is None:
            return HTMLResponse("Unknown SKU.", status_code=404)
        recipes = cfg.recipes()
        mappings = cfg.mappings()
        recipe = next((r for r in recipes if r.sku_id == sku_id), None)
        role = sku_role(recipe)
        cost = cfg.cost_book()
        catalog = RecipeCatalog(recipes, mappings)
        resolver = CostResolver(catalog, cost)
        breakdown = None
        if role is not SkuRole.PURCHASABLE:
            breakdown = cost_breakdown(
                sku_id,
                recipes=catalog,
                cost=cost,
                name_of={s.sku_id: s.name for s in cfg.skus()},
            )
        classification = classify_sku(
            sku_id, recipes=recipes, mappings=mappings
        )
        health = sku_health(
            sku_id,
            recipe=recipe,
            resolver=resolver,
            classification=classification,
        )
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="sku_editor.html",
            context={
                "request": request,
                "sku": sku,
                "current_cost": cfg.cost_book().price(sku_id),
                "recipe": recipe,
                "role": role,
                "is_produced": role is not SkuRole.PURCHASABLE,
                "breakdown": breakdown,
                "health": health,
                "saved": saved,
                "all_skus": pickable_ingredient_skus(cfg.skus(), recipes),
            },
        )

    @app.get("/skus/{sku_id}/cost-preview", response_class=HTMLResponse)
    def cost_preview(
        sku_id: str,
        pack_price: str = "",
        pack_quantity: str = "",
        vat_inclusive: str | None = None,
    ) -> HTMLResponse:
        """The live derived-price fragment the editor's inputs swap in.

        Spells out the arithmetic (``380 / 2000 / 1.07 = 0.177570 THB/g``)
        so the partner sees the number the save will store, as they type.
        Half-typed or malformed input renders an empty fragment rather than
        an error — the partner is mid-keystroke, not wrong.
        """
        try:
            price = Decimal(pack_price)
            quantity = Decimal(pack_quantity)
        except InvalidOperation:
            return HTMLResponse("")
        if price < 0 or quantity <= 0:
            return HTMLResponse("")
        with_vat = vat_inclusive is not None
        net = net_price_per_unit(price, quantity, with_vat)
        cfg: SqliteConfigStore = app.state.config_store
        sku = cfg.sku(sku_id)
        unit = sku.unit if sku is not None and sku.unit else "unit"
        vat_step = " / 1.07" if with_vat else ""
        return HTMLResponse(
            f'<span class="cost-preview__derivation">'
            f"{price} / {quantity}{vat_step} = "
            f'<strong class="cost-preview__net">{net}</strong> THB/{unit} net'
            f"</span>"
        )

    @app.post("/skus/{sku_id}/cost", response_model=None)
    def save_cost(
        request: Request,
        sku_id: str,
        pack_price: str = Form(""),
        pack_quantity: str = Form(""),
        vat_inclusive: str | None = Form(None),
    ) -> HTMLResponse | RedirectResponse:
        """Save a cost entry from the editor's receipt-shaped inputs.

        The store derives and persists the net per-unit price; the redirect
        lands back on the editor, which re-reads the DB — so the number the
        partner sees after saving is the number tomorrow's review will use.
        ``vat_inclusive`` arrives only when the checkbox is ticked (HTML
        checkbox semantics).
        """
        cfg: SqliteConfigStore = app.state.config_store
        if cfg.sku(sku_id) is None:
            return HTMLResponse("Unknown SKU.", status_code=404)
        # One source of truth per role (issue #37): a produced SKU (one with
        # a recipe) is never priced directly — the picker/editor hides the
        # form, and this rejects a crafted POST so the rule is not merely
        # cosmetic.
        recipe = next((r for r in cfg.recipes() if r.sku_id == sku_id), None)
        if sku_role(recipe) is not SkuRole.PURCHASABLE:
            return HTMLResponse(
                f"{sku_id} is a produced SKU — its cost is derived from its "
                "recipe and cannot be entered directly. Edit the recipe "
                "instead, or delete it to make the SKU purchasable.",
                status_code=400,
            )
        try:
            price = Decimal(pack_price)
            quantity = Decimal(pack_quantity)
        except InvalidOperation:
            return HTMLResponse(
                "Pack price and pack quantity must be numbers.", status_code=400
            )
        if price < 0 or quantity <= 0:
            return HTMLResponse(
                "Pack price must be ≥ 0 and pack quantity > 0.", status_code=400
            )
        cfg.save_cost(
            sku_id,
            pack_price=price,
            pack_quantity=quantity,
            vat_inclusive=vat_inclusive is not None,
            updated_by=request.state.assignee_id,
            updated_on=app.state.today,
            session_id=request.state.session_id,
        )
        return RedirectResponse(
            url=f"/skus/{sku_id}?saved=cost", status_code=303
        )

    @app.get("/skus/{sku_id}/recipe-preview", response_class=HTMLResponse)
    def recipe_preview(
        sku_id: str,
        ingredient_sku_id: list[str] = Query([]),
        quantity: list[str] = Query([]),
    ) -> HTMLResponse:
        """The live recipe-cost fragment the editor's rows swap in.

        Spells out each row's arithmetic (``0.65/g × 18 g = 11.70 THB``)
        and the recipe total below — so a typo'd quantity is a visibly
        wrong number *before* save. Applies the same shorthand conversion
        as the save, so the preview never disagrees with what saving would
        store. Half-typed rows and unpriceable ingredients are skipped calmly
        — the partner is mid-edit, not wrong.

        Each ingredient is priced via ``CostResolver`` (issue #36, ADR-0005),
        so a prep ingredient shows its *derived* unit cost even when the prep
        has no direct cost-book entry — the same honesty the saved
        ``cost_breakdown`` applies, kept in step mid-edit. A row is skipped
        only when its resolved unit cost is ``None`` (a missing leaf anywhere
        in the prep's own recipe), never merely because the prep itself lacks
        a cost-book row.
        """
        del sku_id  # the preview depends only on the rows, not the recipe SKU
        cfg: SqliteConfigStore = app.state.config_store
        unit_by_sku = {s.sku_id: s.unit for s in cfg.skus()}
        book = cfg.cost_book()
        # The preview prices the same way the saved breakdown does (ADR-0005):
        # one resolver over the catalog + current cost book, recursing into
        # prep recipes down to purchasables. Mid-edit rows that are not yet a
        # saved recipe are still resolved — a prep in the catalog costs the
        # same whether you are about to save it into a dish or already have.
        resolver = CostResolver(RecipeCatalog(cfg.recipes(), cfg.mappings()), book)
        row_html: list[str] = []
        total = Decimal("0")
        for ing_sku_id, qty_text in zip(ingredient_sku_id, quantity):
            if ing_sku_id not in unit_by_sku:
                continue
            try:
                qty = parse_quantity(qty_text, unit_by_sku[ing_sku_id])
            except QuantityError:
                continue
            unit_cost = resolver.unit_cost(ing_sku_id)
            # Skip only when genuinely unpriceable — a prep with no direct
            # cost-book entry but a fully priced recipe still resolves; a
            # missing leaf anywhere in its tree returns None and is skipped
            # calmly rather than zero-costed.
            if unit_cost is None:
                continue
            unit = unit_by_sku[ing_sku_id] or "unit"
            row_cost = unit_cost * qty
            total += row_cost
            row_html.append(
                f'<li class="recipe-preview__row">{ing_sku_id}: '
                f"{unit_cost}/{unit} × {qty} {unit} = "
                f"<strong>{_money(row_cost)}</strong> THB</li>"
            )
        if not row_html:
            return HTMLResponse("")
        return HTMLResponse(
            '<ul class="recipe-preview__rows">'
            + "".join(row_html)
            + "</ul>"
            + f'<p class="recipe-preview__total">Recipe cost: '
            f"<strong>{_money(total)}</strong> THB</p>"
        )

    @app.post("/skus/{sku_id}/recipe", response_model=None)
    def save_recipe(
        request: Request,
        sku_id: str,
        ingredient_sku_id: list[str] = Form([]),
        quantity: list[str] = Form([]),
        yield_qty: str = Form(""),
        target_gross_margin_pct: str = Form(""),
        prep: str = Form(""),
    ) -> HTMLResponse | RedirectResponse:
        """Save the recipe editor's rows, yield, target margin and prep flag
        for one SKU.

        The form posts parallel lists — one picker + one quantity input per
        row, in display order — so the saved positions mirror what the
        partner saw. ``yield_qty`` is the recipe's yield in its output SKU's
        own unit (issue #34); whether it counts as measured is decided by
        comparing it against the stored yield, so older forms that omit the
        field leave the yield alone. The redirect lands back on the editor,
        which re-reads the DB: the rows and yield shown after saving are the
        ones tomorrow's review will cost.
        """
        cfg: SqliteConfigStore = app.state.config_store
        if cfg.sku(sku_id) is None:
            return HTMLResponse("Unknown SKU.", status_code=404)
        unit_by_sku = {s.sku_id: s.unit for s in cfg.skus()}
        # The same rule the picker renders, enforced server-side so the
        # filter is not merely cosmetic (issue #35): an ingredient must be
        # purchasable (no recipe) or a prep — never a sold-only dish.
        recipes_by_sku = {r.sku_id: r for r in cfg.recipes()}
        ingredients: list[tuple[str, Decimal]] = []
        for ing_sku_id, qty_text in zip(ingredient_sku_id, quantity):
            if ing_sku_id not in unit_by_sku:
                return HTMLResponse(f"Unknown ingredient SKU: {ing_sku_id}.", status_code=400)
            if sku_role(recipes_by_sku.get(ing_sku_id)) is SkuRole.PRODUCED:
                return HTMLResponse(
                    f"{ing_sku_id} is a sold-only dish, not an allowed "
                    "ingredient — only purchasable SKUs and preps may go "
                    "into a recipe. Declare it a prep first if it is one.",
                    status_code=400,
                )
            try:
                qty = parse_quantity(qty_text, unit_by_sku[ing_sku_id])
            except QuantityError as exc:
                return HTMLResponse(f"{ing_sku_id}: {exc}", status_code=400)
            ingredients.append((ing_sku_id, qty))
        if not ingredients:
            # An empty recipe would be costed as zero COGS — the margin
            # engine can't tell "no ingredients" from "free" — so it is
            # rejected rather than silently inflating tomorrow's review.
            return HTMLResponse(
                "A recipe needs at least one ingredient row.", status_code=400
            )
        # Issue #34: the yield must be a positive number. Zero or negative
        # would divide-by-zero or invert the cost sign — neither is ever a
        # real recipe, only a typo.
        #
        # The estimated/measured decision is server-side, no JS. A posted
        # value that differs from the stored yield is a partner-typed
        # measurement and fixes the yield. An unchanged (or absent) value
        # means the partner left the field alone: a measured yield stays
        # exactly as stored, an estimated one is recomputed from the
        # (possibly edited) rows. A recipe with no stored row starts out
        # estimated unless the partner types a yield up front.
        stored = next((r for r in cfg.recipes() if r.sku_id == sku_id), None)
        posted: Decimal | None = None
        if yield_qty.strip():
            try:
                posted = Decimal(yield_qty.strip())
            except InvalidOperation:
                return HTMLResponse(
                    "Yield must be a number.", status_code=400
                )
            if posted <= 0:
                return HTMLResponse(
                    "Yield must be greater than zero.", status_code=400
                )
        touched = posted is not None and (
            stored is None or posted != stored.yield_qty
        )
        if posted is not None and touched:
            yld, estimated = posted, False
        elif stored is not None and not stored.yield_estimated:
            yld, estimated = stored.yield_qty, False
        else:
            yld, estimated = estimated_yield(ingredients, unit_by_sku), True
        cycle = find_recipe_cycle(
            list(recipes_by_sku.values()), sku_id, [i for i, _ in ingredients]
        )
        if cycle is not None:
            return HTMLResponse(
                "This save would create a recipe cycle — costing could "
                f"never terminate: {' \u2192 '.join(cycle)}.",
                status_code=400,
            )
        target: Decimal | None = None
        if target_gross_margin_pct.strip():
            try:
                target = Decimal(target_gross_margin_pct.strip())
            except InvalidOperation:
                return HTMLResponse(
                    "Target gross margin must be a number.", status_code=400
                )
        cfg.save_recipe(
            sku_id,
            ingredients=ingredients,
            yield_qty=yld,
            yield_estimated=estimated,
            target_gross_margin_pct=target,
            prep=bool(prep),
            updated_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        return RedirectResponse(
            url=f"/skus/{sku_id}?saved=recipe", status_code=303
        )

    @app.post("/skus/{sku_id}/recipe/delete", response_model=None)
    def delete_recipe(
        request: Request, sku_id: str
    ) -> HTMLResponse | RedirectResponse:
        """Delete a SKU's recipe, flipping it back to purchasable (issue #37).

        The role-flip demo: once the recipe is gone the SKU is bought again,
        so the editor re-offers the cost-entry form. The delete is audited
        (and revertible) like any config change; the redirect lands back on
        the editor, which re-reads the DB and renders the restored form.
        """
        cfg: SqliteConfigStore = app.state.config_store
        if cfg.sku(sku_id) is None:
            return HTMLResponse("Unknown SKU.", status_code=404)
        cfg.delete_recipe(
            sku_id,
            updated_by=request.state.assignee_id,
            session_id=request.state.session_id,
        )
        return RedirectResponse(url=f"/skus/{sku_id}", status_code=303)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_log(request: Request) -> HTMLResponse:
        """The change log (Wave 1.5, Slice 5; redesigned Wave 3 Slice 9):
        every config edit, newest first.

        The safety net that replaces the removed code-review gate (ADR-0003
        decision 2): who changed what, when, from what to what. Each entry's
        field-level diff is derived from the whole-row snapshots at render
        time. Entries the signed-in partner has not yet reviewed are
        highlighted with a mustard keyline and a NEW chip — that is the
        "diff of what changed" the 9am review's link promises — and a
        "Mark as reviewed" control (an explicit POST, so merely loading or
        prefetching this page never moves the mark) clears the nag for them
        and only them.

        ``?reverted=<entry_id>`` is set by the revert route on its redirect;
        the entry it names renders its REVERT control replaced in place by
        the teal "REVERTED — change undone" confirmation, so the partner
        sees the undo landed without a separate toast.
        """
        cfg: SqliteConfigStore = app.state.config_store
        entries = [
            {"entry": e, "changes": _audit_field_changes(e)}
            for e in cfg.audit_entries()
        ]
        unreviewed_ids = {
            e.entry_id for e in cfg.unreviewed_changes(request.state.assignee_id)
        }
        reverted_id_raw = request.query_params.get("reverted")
        reverted_id: int | None = None
        if reverted_id_raw:
            try:
                reverted_id = int(reverted_id_raw)
            except (TypeError, ValueError):
                reverted_id = None
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="audit_log.html",
            context={
                "request": request,
                "entries": entries,
                "unreviewed_ids": unreviewed_ids,
                "reverted_id": reverted_id,
            },
        )

    @app.post("/audit/reviewed")
    def mark_audit_reviewed(request: Request) -> RedirectResponse:
        """Move the signed-in partner's review mark to now (Wave 1.5, Slice 5).

        Pressing "Mark as reviewed" on the audit page is what counts as
        reviewing: the 9am review's "N changes since last review" nag clears
        for this partner (and only them) until the next edit. A POST rather
        than a side effect of viewing, so a prefetch or an accidental page
        load cannot silently swallow the nag.
        """
        cfg: SqliteConfigStore = app.state.config_store
        cfg.mark_reviewed(request.state.assignee_id)
        return RedirectResponse(url="/audit", status_code=303)

    @app.post("/audit/{entry_id}/revert", response_model=None)
    def revert_audit_entry(
        request: Request, entry_id: int, reason: str = Form("")
    ) -> HTMLResponse | RedirectResponse:
        """Undo exactly one logged change (Wave 1.5, Slice 5).

        The surgical fix for "I know which change was wrong": sets back
        only the fields that entry changed, so later edits to other fields
        of the same row survive. The revert lands as its own audit entry,
        attributed to whoever clicked, carrying the optional reason they
        typed (ADR-0003: the log records intent).
        """
        cfg: SqliteConfigStore = app.state.config_store
        reverted = cfg.revert_entry(
            entry_id,
            changed_by=request.state.assignee_id,
            session_id=request.state.session_id,
            reason=reason.strip() or None,
        )
        if not reverted:
            return HTMLResponse("Unknown audit entry.", status_code=404)
        # Carry the just-reverted entry's id back so the change log can
        # render the in-place "REVERTED — change undone" confirmation on it.
        return RedirectResponse(
            url=f"/audit?reverted={entry_id}", status_code=303
        )

    @app.post("/audit/session/{session_id}/revert", response_model=None)
    def revert_audit_session(
        request: Request, session_id: str, reason: str = Form("")
    ) -> HTMLResponse | RedirectResponse:
        """Undo every edit sharing one ``session_id`` (Wave 1.5, Slice 5).

        The panic undo for "I broke something this session but I don't know
        what": unwinds the whole batch, newest first. Every individual
        revert is logged (with the optional reason), so the panic undo
        leaves the same paper trail as careful surgery.
        """
        cfg: SqliteConfigStore = app.state.config_store
        reverted_count = cfg.revert_session(
            session_id,
            changed_by=request.state.assignee_id,
            reverter_session_id=request.state.session_id,
            reason=reason.strip() or None,
        )
        if reverted_count == 0:
            return HTMLResponse("Unknown session.", status_code=404)
        return RedirectResponse(url="/audit", status_code=303)

    @app.get("/items", response_class=HTMLResponse)
    def items_view(
        request: Request,
        item: str | None = None,
        filter: str | None = Query(default=None),
    ) -> HTMLResponse:
        """The item coverage view (Wave 1.5, Slice 2): one row per Loyverse
        item, unmapped/broken items bubbled to the top. ``?item=<id>`` (used
        by the daily review's needs-attention deep link) filters the table
        down to that single item. Stock filter chips arrive as ``?filter=``.
        """
        cfg: SqliteConfigStore = app.state.config_store
        store: SqliteLoyverseStore = app.state.store
        all_rows = build_item_coverage(
            menu=store.current_menu(),
            skus=cfg.skus(),
            recipes=cfg.recipes(),
            mappings=cfg.mappings(),
            cost=cfg.cost_book(),
        )
        if item:
            rows = [r for r in all_rows if r.item_id == item]
            active_filter = "all"
        else:
            active_filter = _normalise_stock_filter(filter)
            rows = [r for r in all_rows if _item_matches_filter(r, active_filter)]
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="item_coverage.html",
            context={
                "request": request,
                "rows": rows,
                "total_count": len(all_rows),
                "uncostable_count": _count_uncostable_items(all_rows),
                "filtered_item_id": item,
                "filter": active_filter,
                "filter_urls": _stock_filter_urls("items", active_filter),
                "tab_urls": _stock_tab_urls(active_filter),
                "stock_tab": "items",
            },
        )

    @app.get("/upload", response_class=HTMLResponse)
    def upload_page(
        request: Request, applied: int | None = None
    ) -> HTMLResponse:
        """The bulk-upload surface: three progressive step cards.

        ``applied`` carries the in-place confirmation signal (issue #49): the
        confirm POST redirects with ``?applied=N`` and the template renders
        the teal "APPLIED — N changes live" banner (SKU editor's ``?saved=``
        pattern).
        """
        t: Jinja2Templates = app.state.templates
        return t.TemplateResponse(
            request=request,
            name="upload.html",
            context={"request": request, "applied": applied},
        )

    @app.get("/upload/template")
    def upload_template() -> Response:
        """The downloadable CSV template, pre-filled with current state."""
        cfg: SqliteConfigStore = app.state.config_store
        store: SqliteLoyverseStore = app.state.store
        csv_text = generate_template_csv(
            menu=store.current_menu(),
            skus=cfg.skus(),
            mappings=cfg.mappings(),
            cost_rows=cfg.cost_rows(),
        )
        stamp = app.state.today.isoformat()
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="tangerine-config-{stamp}.csv"'
                )
            },
        )

    @app.post("/upload", response_model=None)
    def upload_submit(
        request: Request,
        file: UploadFile | None = None,
        csv_text: str = Form(""),
        confirm: str | None = Form(None),
    ) -> HTMLResponse | RedirectResponse:
        """Parse an uploaded CSV and preview what will change.

        Two-step, stateless: the first POST carries the file and re-renders
        the progressive upload page with step 3 open (CSV embedded in the
        confirm form); the confirm POST re-submits that text with
        ``confirm=1``, re-derives the changes, applies them, and redirects
        to ``GET /upload?applied=N``. Re-parsing on confirm means nothing
        needs to be held in a server-side session between the two steps.
        """
        cfg: SqliteConfigStore = app.state.config_store
        filename: str | None = None
        if file is not None and file.filename:
            # ``utf-8-sig`` strips the BOM Excel prepends when saving CSV.
            text = file.file.read().decode("utf-8-sig")
            filename = file.filename
        else:
            text = csv_text
        preview = parse_upload(
            text,
            skus=cfg.skus(),
            mappings=cfg.mappings(),
            cost_rows=cfg.cost_rows(),
            # A produced SKU (one with a recipe) is never priced directly —
            # the same rule the cost form enforces, held on the bulk path
            # (issue #37).
            produced_sku_ids={r.sku_id for r in cfg.recipes()},
        )
        t: Jinja2Templates = app.state.templates
        if confirm is None or preview.errors or not preview.has_changes:
            # Data rows only (header excluded) — the partner-facing count
            # beside the filename in step 2.
            data_lines = [ln for ln in text.splitlines() if ln.strip()]
            rows_parsed = max(0, len(data_lines) - 1) if data_lines else 0
            return t.TemplateResponse(
                request=request,
                name="upload.html",
                context={
                    "request": request,
                    "preview": preview,
                    "csv_text": text,
                    "filename": filename or "uploaded.csv",
                    "rows_parsed": rows_parsed,
                    "error_count": len(preview.errors),
                    "applied": None,
                },
            )

        actor: str = request.state.assignee_id
        session_id: str | None = request.state.session_id
        for mapping_change in preview.mapping_changes:
            cfg.save_mapping(
                mapping_change.item_id,
                mapping_change.new_sku_id,
                updated_by=actor,
                session_id=session_id,
            )
        for cost_change in preview.cost_changes:
            cfg.save_cost(
                cost_change.sku_id,
                pack_price=cost_change.pack_price,
                pack_quantity=cost_change.pack_quantity,
                vat_inclusive=cost_change.vat_inclusive,
                updated_by=actor,
                updated_on=app.state.today,
                session_id=session_id,
            )
        applied_count = len(preview.mapping_changes) + len(preview.cost_changes)
        return RedirectResponse(
            url=f"/upload?applied={applied_count}", status_code=303
        )

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
                cafe_category_ids=app.state.cafe_category_ids,
            )

        # Refresh yesterday's headline numbers so the partner sees fresh data
        # immediately, without a manual reload (PRD: "the page is reloaded with
        # fresh data"). The review renders against the same store the sync just
        # wrote into, so these numbers reflect the sync result.
        yesterday = today_date - timedelta(days=1)
        review = build_daily_review(source=app.state.source, review_date=yesterday)
        return _render_sync_fragment(request, result, review)

    return app


def _audit_field_changes(entry: AuditEntry) -> list[dict[str, Any]]:
    """Field-level diff of an audit entry's whole-row snapshots.

    Returns ``[{"field", "old", "new"}, ...]`` for every column whose value
    differs between the before and after snapshots, in snapshot column order.
    A creation (no old row) diffs every column against nothing; a deletion
    the reverse.
    """
    old = entry.old_value or {}
    new = entry.new_value or {}
    fields = list(old.keys()) + [k for k in new.keys() if k not in old]
    return [
        {"field": field, "old": old.get(field), "new": new.get(field)}
        for field in fields
        if old.get(field) != new.get(field)
    ]


def _month_label(day: date) -> str:
    """The breadcrumb's month wording, e.g. ``Jul 2026``."""
    return day.strftime("%b %Y")


def _day_label(day: date) -> str:
    """The breadcrumb's day wording, e.g. ``15 Jul`` (no zero padding)."""
    return f"{day.day} {day.strftime('%b')}"


def _day_crumbs(day: date) -> list[dict[str, str | None]]:
    """The zoom trail down to one day: Review › Jul 2026 › 15 Jul.

    Home links to ``/``, the month crumb to that month's Month mode; the
    day itself is the current step (its ``href`` is filled in only when a
    deeper view — the item drill — appends another level).
    """
    return [
        {"label": "Review", "href": "/"},
        {
            "label": _month_label(day),
            "href": f"/review?mode=month&month={day.strftime('%Y-%m')}",
        },
        {"label": _day_label(day), "href": None},
    ]


def _period_crumbs(start: date, end: date, mode: str) -> list[dict[str, str | None]]:
    """The zoom trail for a range: Review › Jul 2026 (or › 9 Jul – 15 Jul)."""
    label = (
        _month_label(start)
        if mode == "month"
        else f"{_day_label(start)} – {_day_label(end)}"
    )
    return [{"label": "Review", "href": "/"}, {"label": label, "href": None}]


def _item_crumbs(name: str, start: date, end: date) -> list[dict[str, str | None]]:
    """The item drill's trail: one level deeper than the range it came from.

    A one-day drill (the Day-mode click) walks Review › month › day › item;
    a multi-day drill steps back to its Period view instead of a single day.
    """
    if start == end:
        crumbs = _day_crumbs(start)
        crumbs[-1]["href"] = f"/review?mode=day&day={start.isoformat()}"
    else:
        crumbs = _period_crumbs(start, end, "period")
        crumbs[-1]["href"] = (
            f"/review?mode=period&start={start.isoformat()}&end={end.isoformat()}"
        )
    crumbs.append({"label": name, "href": None})
    return crumbs


def _mode_switcher_urls(anchor: date) -> dict[str, str]:
    """The mode control's four deep-linkable targets, anchored on a date.

    From any view whose anchor day is ``anchor``: Day is that day, Period is
    the 7 days ending on it, Month is its calendar month, Trends is the
    default trend view (always anchored on yesterday — a trend is about the
    business's shape now, not about the day being viewed). Every target is an
    ordinary URL (mode + date/range as query params), so switching modes
    works without JavaScript and every state is shareable (ADR-0004
    decision 4).
    """
    week_start = anchor - timedelta(days=6)
    return {
        "day": f"/review?mode=day&day={anchor.isoformat()}",
        "period": (
            f"/review?mode=period&start={week_start.isoformat()}"
            f"&end={anchor.isoformat()}"
        ),
        "month": f"/review?mode=month&month={anchor.strftime('%Y-%m')}",
        "trends": "/review?mode=trends",
    }


#: The trend metrics the partner can plot, in display order. Each maps its
#: query-param value to a human title (issue #32 AC: revenue, COGS, gross
#: margin, and segment CM week-over-week / month-over-month). Wave 3 #47
#: shortens the primary chip labels (MARGIN / REVENUE / COGS); the chart
#: titles below stay the longer partner-readable form.
_TREND_METRICS: dict[str, str] = {
    "gross_margin": "Gross margin",
    "revenue": "Revenue",
    "cogs": "COGS",
    "segment_cm": "Segment CM",
    "goal": "Goal",
}

#: Chip labels on the Trends tab (Wave 3 #47). Primary chips match the
#: design handoff; segment_cm and goal stay deep-linkable secondary chips.
_TREND_METRIC_CHIPS: dict[str, str] = {
    "gross_margin": "Margin",
    "revenue": "Revenue",
    "cogs": "COGS",
    "segment_cm": "Segment CM",
    "goal": "Goal",
}

_TREND_PRIMARY_METRICS = frozenset({"gross_margin", "revenue", "cogs"})

_TREND_SPANS: dict[str, str] = {"weeks": "Weekly", "months": "Monthly"}

#: 10,000 THB/day goal — used for the dashed reference line on margin
#: trends. Weekly buckets compare against a full-week target; monthly
#: against a 30-day nominal month (exact month targets vary; the line is
#: a guide, not the attainment math which lives on ``metric=goal``).
_GOAL_PER_DAY = Decimal("10000")
_GOAL_WEEK = _GOAL_PER_DAY * 7
_GOAL_MONTH_NOMINAL = _GOAL_PER_DAY * 30


def _review_date_bounds(app: FastAPI) -> tuple[date, date]:
    """Earliest synced sale day and latest reviewable day (yesterday).

    View-layer only — the same bound rule Today (#45) uses for day-nav
    arrows. An empty store collapses both ends to yesterday so arrows dim.
    """
    latest_reviewable = app.state.today - timedelta(days=1)
    sales_dates = [s.timestamp for s in app.state.source.sales()]
    earliest = min(sales_dates) if sales_dates else latest_reviewable
    return earliest, latest_reviewable


def _shift_month(day: date, *, months: int) -> date:
    """First day of the calendar month ``months`` away from ``day``'s month."""
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _period_range_nav(
    app: FastAPI, start: date, end: date, *, mode: str
) -> dict[str, Any]:
    """Prev/next URLs and dimming for the Period/Month range navigator.

    Period slides the window by its length; Month steps calendar months.
    Arrows dim at the synced sales bounds (view-layer, not engine).
    """
    earliest, latest = _review_date_bounds(app)
    if mode == "month":
        prev_month = _shift_month(start, months=-1)
        next_month = _shift_month(start, months=1)
        prev_days = calendar.monthrange(prev_month.year, prev_month.month)[1]
        next_days = calendar.monthrange(next_month.year, next_month.month)[1]
        prev_end = prev_month.replace(day=prev_days)
        next_end = next_month.replace(day=next_days)
        prev_dimmed = prev_end < earliest
        next_dimmed = next_month > latest
        return {
            "prev_dimmed": prev_dimmed,
            "next_dimmed": next_dimmed,
            "prev_range_url": (
                f"/review?mode=month&month={prev_month.strftime('%Y-%m')}"
            ),
            "next_range_url": (
                f"/review?mode=month&month={next_month.strftime('%Y-%m')}"
            ),
            "range_label": (
                f"{start.isoformat()} — {end.isoformat()}"
            ),
        }

    length = (end - start).days + 1
    prev_start = start - timedelta(days=length)
    prev_end = end - timedelta(days=length)
    next_start = start + timedelta(days=length)
    next_end = end + timedelta(days=length)
    return {
        "prev_dimmed": start <= earliest,
        "next_dimmed": end >= latest,
        "prev_range_url": (
            f"/review?mode=period&start={prev_start.isoformat()}"
            f"&end={prev_end.isoformat()}"
        ),
        "next_range_url": (
            f"/review?mode=period&start={next_start.isoformat()}"
            f"&end={next_end.isoformat()}"
        ),
        "range_label": f"{start.isoformat()} — {end.isoformat()}",
    }


def _fixed_costs_summary(review: Any) -> str:
    """Short meta line for the Fixed Costs row-link card."""
    lines = review.fixed_costs.lines
    if not lines:
        return "None entered"
    n = len(lines)
    total = _money(review.fixed_costs.total)
    unit = "line" if n == 1 else "lines"
    if review.fixed_costs.estimated:
        return f"{n} {unit} · {total} THB (est)"
    return f"{n} {unit} · {total} THB"


def _render_trends(
    request: Request, app: FastAPI, *, metric: str, span: str
) -> HTMLResponse:
    """Build the trend report and render it as server-side SVG + CSS bars.

    Each bucket is the period engine over that bucket's range (same engine,
    same as-of-date prices as Period/Month mode), and each bar links into
    the bucket's Period/Month view — a trend is a navigation surface
    (ADR-0004 decision 5, issue #32). ``metric`` and ``span`` are query
    params, so every chart is a deep-linkable, shareable URL.
    """
    if metric not in _TREND_METRICS:
        expected = ", ".join(_TREND_METRICS)
        return HTMLResponse(
            f"Unknown trend metric (expected {expected}).", status_code=400
        )
    if span not in _TREND_SPANS:
        return HTMLResponse(
            "Unknown trend span (expected weeks or months).", status_code=400
        )

    templates: Jinja2Templates = app.state.templates
    source: StoreSource = app.state.source
    anchor = app.state.today - timedelta(days=1)

    report = build_trends(source=source, anchor=anchor, span=span)

    # A bucket's drill-in target is the exact view that renders its range:
    # a month bucket is a calendar month (Month mode), a week bucket is its
    # inclusive [start, end] (Period mode) — so the drilled-in page shows
    # the identical numbers by construction.
    def _bucket_href(bucket) -> str:  # type: ignore[no-untyped-def]
        if span == "months":
            return f"/review?mode=month&month={bucket.start.strftime('%Y-%m')}"
        return (
            f"/review?mode=period&start={bucket.start.isoformat()}"
            f"&end={bucket.end.isoformat()}"
        )

    bucket_noun = "month" if span == "months" else "week"

    def _chart(
        key: str,
        title: str,
        values: list[Any],
        *,
        display: Any = None,
        note: str = "",
        reference_value: Decimal | None = None,
    ) -> dict[str, str]:
        fmt = display if display is not None else _money
        points = [
            ChartPoint(
                label=bucket.label,
                value=value,
                href=_bucket_href(bucket),
                display=fmt(value),
            )
            for bucket, value in zip(report.buckets, values)
        ]
        return {
            "key": key,
            "title": title,
            "svg": sparkline_svg(points, reference_value=reference_value),
            "bars": bar_row(points),
            "note": note,
        }

    if metric == "segment_cm":
        # One chart per segment, cafe then bar (the engine's canonical
        # order) — two series that would blur into one polyline otherwise.
        charts = [
            _chart(
                f"segment_cm-{segment.value}",
                f"{segment.value.capitalize()} contribution margin by {bucket_noun}",
                [
                    next(
                        sm.contribution_margin
                        for sm in bucket.review.segment_margins
                        if sm.segment is segment
                    )
                    for bucket in report.buckets
                ],
            )
            for segment in _SEGMENT_ORDER
        ]
    elif metric == "goal":
        # Per-bucket attainment of 10K THB/day × days in the bucket, as a
        # percentage — so a truncated week and a full month compare on one
        # scale. The engine's goal carries its target (10K × days_in_range)
        # and its honest basis (gross margin until fixed costs land).
        charts = [
            _chart(
                "goal",
                f"Goal attainment by {bucket_noun}"
                f" (10,000 THB/day \u00d7 days in the {bucket_noun})",
                [
                    (
                        bucket.review.goal.actual / bucket.review.goal.target * 100
                    ).quantize(Decimal("0.01"))
                    for bucket in report.buckets
                ],
                display=lambda pct: f"{pct}%",
                note=(
                    "Compared on gross margin — fixed costs are not yet "
                    "entered, so this is not a net-profit number."
                ),
                reference_value=Decimal("100"),
            )
        ]
    else:
        reference: Decimal | None = None
        note = ""
        if metric == "gross_margin":
            reference = _GOAL_WEEK if span == "weeks" else _GOAL_MONTH_NOMINAL
            note = "Dashed line = the 10,000 THB/day goal (full bucket)."
        charts = [
            _chart(
                metric,
                f"{_TREND_METRICS[metric]} by {bucket_noun}",
                [getattr(bucket.review, metric) for bucket in report.buckets],
                note=note,
                reference_value=reference,
            )
        ]

    # The Mondays-vs-Saturdays breakdown: the selected metric averaged per
    # weekday across the span. Segment CM has no single per-day scalar, so
    # its breakdown falls back to gross margin (and says so in the title).
    weekday_metric = metric if metric in ("revenue", "cogs", "gross_margin") else "gross_margin"

    def _weekday_average(agg: WeekdayAggregate) -> Decimal:
        total: Decimal = getattr(agg, weekday_metric)
        return total / agg.day_count if agg.day_count else Decimal("0")

    weekday_avgs = [_weekday_average(agg) for agg in report.weekdays]
    weekday_max = max(weekday_avgs) if weekday_avgs else Decimal("0")
    weekday_rows = []
    for agg, avg in zip(report.weekdays, weekday_avgs):
        pct = int(avg / weekday_max * 100) if weekday_max else 0
        weekday_rows.append(
            {
                "label": agg.label,
                "display": _money(avg),
                "pct": pct,
            }
        )
    weekday_chart = {
        "title": (
            f"By day of week (average {_TREND_METRICS[weekday_metric].lower()}"
            " per day across the span)"
        ),
        "subtitle": f"Avg {_TREND_METRICS[weekday_metric].lower()}",
        "rows": weekday_rows,
    }

    def _trends_url(m: str, s: str) -> str:
        return f"/review?mode=trends&metric={m}&span={s}"

    metric_links = [
        {
            "label": _TREND_METRIC_CHIPS[m],
            "href": _trends_url(m, span),
            "active": m == metric,
            "primary": m in _TREND_PRIMARY_METRICS,
        }
        for m in _TREND_METRICS
    ]
    span_links = [
        {"label": label, "href": _trends_url(metric, s), "active": s == span}
        for s, label in _TREND_SPANS.items()
    ]

    return templates.TemplateResponse(
        request=request,
        name="trends_review.html",
        context={
            "request": request,
            "mode": "trends",
            "charts": charts,
            "weekday_chart": weekday_chart,
            "metric_links": metric_links,
            "span_links": span_links,
            "span_label": _TREND_SPANS[span],
            "switcher": _mode_switcher_urls(anchor),
        },
    )


def _render_period_review(
    request: Request,
    app: FastAPI,
    start: date,
    end: date,
    *,
    mode: str = "period",
) -> HTMLResponse:
    """Build the period review for ``[start, end]`` and render it.

    Month mode is the same engine over the calendar month (``mode`` only
    changes the page's framing — title, active switcher tab, month picker),
    so Period and Month cannot disagree. Fixed costs come from the config
    store (Wave 2 slice 3): exact over calendar months, a labelled
    apportioned estimate otherwise — the engine flags which, the template
    labels it.
    """
    templates: Jinja2Templates = app.state.templates
    source: StoreSource = app.state.source
    cfg: SqliteConfigStore = app.state.config_store

    review = build_period_review(
        source=source, start=start, end=end, fixed_costs=cfg.fixed_costs()
    )
    range_nav = _period_range_nav(app, start, end, mode=mode)
    return templates.TemplateResponse(
        request=request,
        name="period_review.html",
        context={
            "request": request,
            "mode": mode,
            "review": review,
            # Already in canonical cafe-then-bar order (the engine builds
            # them so); named for the shared _segment_cm.html partial.
            "segment_margins": review.segment_margins,
            "switcher": _mode_switcher_urls(end),
            "breadcrumb": _period_crumbs(start, end, mode),
            "fixed_costs_summary": _fixed_costs_summary(review),
            **range_nav,
        },
    )


def _render_item_review(
    request: Request,
    app: FastAPI,
    item_id: str,
    start: date,
    end: date,
) -> HTMLResponse:
    """Build one item's performance view and render it (issue #31).

    The drill-down's last zoom step. An unmapped item has no recipe-cost to
    show, so its URL is a 404 — the drill is never fabricated; the item's fix
    path stays the needs-attention link into item coverage.
    """
    templates: Jinja2Templates = app.state.templates
    source: StoreSource = app.state.source

    perf = build_item_performance(
        source=source, item_id=item_id, start=start, end=end
    )
    if perf is None:
        return HTMLResponse(
            "Unknown or unmapped item — no recipe cost to show.",
            status_code=404,
        )
    return templates.TemplateResponse(
        request=request,
        name="item_review.html",
        context={
            "request": request,
            "mode": "item",
            "perf": perf,
            "switcher": _mode_switcher_urls(end),
            "breadcrumb": _item_crumbs(perf.name, start, end),
        },
    )


def _render_review(
    request: Request, app: FastAPI, review_date: date, *, rank: str = "margin"
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

    ``rank`` is the TOP & BOTTOM toggle's active mode (issue #45): ``margin``
    shows the top/bottom-by-margin pair, ``volume`` shows top/bottom-by-volume.
    Both pairs stay in the DOM (their section anchors are sliced across the
    wider test suite); CSS hides the inactive pair, so no ranking data is lost
    by the toggle.

    The day navigator's arrows dim at the bounds of the synced range: prev
    dims once ``review_date`` is at (or before) the earliest day the store has
    a sale for; next dims once it is at (or after) yesterday (the latest day
    that can have settled sales — "today" is not yet a reviewable day).
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

    # Config changes this partner has not yet reviewed (Slice 5): the diff
    # link that replaces the removed code-review gate. Counted per partner —
    # the audit page's "Mark as reviewed" button clears it for the presser
    # only. Changes made in the last 24 hours upgrade the link to a banner,
    # so a fresh edit can't be missed on a quiet day (same clock as the
    # stale-sync banner *and* as the store's audit timestamps — see
    # ``_config_now_iso`` in ``create_app``).
    cfg: SqliteConfigStore = app.state.config_store
    unreviewed = (
        cfg.unreviewed_changes(assignee_id) if assignee_id is not None else []
    )
    unreviewed_recent = any(
        (now_dt - datetime.fromisoformat(e.changed_at)).total_seconds()
        <= STALE_AFTER_SECONDS
        for e in unreviewed
    )

    # Day-nav bounds (issue #45): the arrows dim at the ends of the synced
    # range so a partner can feel where the data starts and stops. ``latest``
    # is the day before "today" — today is not yet a reviewable day (its sales
    # have not settled), so the next arrow never steps onto it. ``earliest`` is
    # the first day the store has any sale for; before that there is nothing
    # to walk back to. An empty store has no bounds, so both arrows dim and
    # only the date input can move the page.
    latest_reviewable = app.state.today - timedelta(days=1)
    sales_dates = [s.timestamp for s in source.sales()]
    earliest = min(sales_dates) if sales_dates else latest_reviewable
    prev_day = review_date - timedelta(days=1)
    next_day = review_date + timedelta(days=1)
    prev_dimmed = review_date <= earliest
    next_dimmed = review_date >= latest_reviewable

    # The TOP & BOTTOM toggle's two targets — deep-linkable URLs so the back
    # button walks the toggle and the choice is shareable. Each carries the
    # current day so toggling does not also move the page.
    rank_urls = {
        "margin": f"/review?mode=day&day={review_date.isoformat()}&rank=margin",
        "volume": f"/review?mode=day&day={review_date.isoformat()}&rank=volume",
    }

    return templates.TemplateResponse(
        request=request,
        name="daily_review.html",
        context={
            "request": request,
            "mode": "day",
            "switcher": _mode_switcher_urls(review_date),
            "breadcrumb": _day_crumbs(review_date),
            "review": review,
            "segment_margins": segment_margins,
            "has_sales": has_sales,
            "store_empty": store_empty,
            "stale": stale,
            "stale_days": stale_days,
            "last_sync": last_sync,
            "needs_attention": needs_attention,
            "signed_in_name": signed_in_name,
            "unreviewed_count": len(unreviewed),
            "unreviewed_recent": unreviewed_recent,
            "prev_day": prev_day,
            "next_day": next_day,
            "prev_dimmed": prev_dimmed,
            "next_dimmed": next_dimmed,
            "rank_active": rank,
            "rank_urls": rank_urls,
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
