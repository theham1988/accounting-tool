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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, PackageLoader, select_autoescape

from ..config.loader import load_costs, load_recipes
from ..daily_review import build_daily_review
from ..loyverse.source import StoreSource
from ..storage.sqlite_store import SqliteLoyverseStore
from ..types import Segment, SegmentMargin

#: Default config file locations (operator-editable, shipped with the repo).
#: Mirror the CLI defaults in ``tangerine.__main__`` so the web app and the
#: CLI read the same source of truth by default.
DEFAULT_RECIPES_PATH = "config/recipes.yaml"
DEFAULT_COSTS_PATH = "config/costs.yaml"

#: Environment variable holding the SQLite database path. Lives in the
#: environment, not the repo, per ADR-0001. Mirrors the CLI.
DB_PATH_ENV = "TANGERINE_DB_PATH"
DEFAULT_DB_PATH = "./tangerine.db"

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
    today: date | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``db_path`` defaults to ``$TANGERINE_DB_PATH`` or ``./tangerine.db``.
    ``recipes_path`` / ``costs_path`` default to the shipped config files.
    ``today`` defaults to ``date.today()``; injectable so tests can pin
    "yesterday" deterministically.

    The store is opened once and held for the app's lifetime (closed on
    shutdown). Config is loaded once at construction — a malformed config
    raises immediately, per the PRD's "fail loudly at startup" rule.
    """
    db = db_path or os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)
    recipes_yaml = recipes_path or DEFAULT_RECIPES_PATH
    costs_yaml = costs_path or DEFAULT_COSTS_PATH
    today_date = today or date.today()

    catalog = load_recipes(recipes_yaml)
    cost = load_costs(costs_yaml)
    # FastAPI serves sync route handlers from a threadpool, so the SQLite
    # connection must be safe to use across threads. ``check_same_thread=False``
    # lifts Python's default same-thread guard; serialised access is guaranteed
    # because each request opens its own short transaction and the underlying
    # SQLite writes are serialised by the database file lock. The store itself
    # is connection-agnostic (Slice 1), so we hand it a pre-built connection.
    conn = sqlite3.connect(db, check_same_thread=False)
    store = SqliteLoyverseStore(conn)
    source = StoreSource(store=store, recipes=list(catalog.all()), cost=cost)

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
    app.state.templates = templates
    app.state.today = today_date

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

    return app


def _render_review(
    request: Request, app: FastAPI, review_date: date
) -> HTMLResponse:
    """Build the review for ``review_date`` and render the daily template.

    Centralises the build→render path so ``GET /`` and ``GET /review`` cannot
    drift apart. ``has_sales`` drives the empty-state note; everything else is
    taken straight from the engine result.
    """
    templates: Jinja2Templates = app.state.templates
    source: StoreSource = app.state.source

    review = build_daily_review(source=source, review_date=review_date)
    segment_margins = sorted(review.segment_margins, key=_segment_sort_key)
    has_sales = review.revenue != 0 or review.cogs != 0 or any(
        im.units_sold for im in review.daily.item_margins
    )
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

    return templates.TemplateResponse(
        request=request,
        name="daily_review.html",
        context={
            "request": request,
            "review": review,
            "segment_margins": segment_margins,
            "has_sales": has_sales,
            "needs_attention": needs_attention,
        },
    )


__all__ = ["create_app"]
