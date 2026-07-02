"""Shared-passphrase + signed-cookie auth (Wave 1, Slice 4).

This is the auth gate for the daily-review web app. Per PRD user stories 2–5
and the slice-4 issue:

  - Every route except ``/login`` requires a valid signed session cookie.
  - Sessions are signed with :class:`itsdangerous.URLSafeTimedSerializer` so a
    cookie cannot be tampered with — flipping the role or the activity
    timestamp invalidates the signature.
  - The signed payload carries the selected ``assignee_id`` (the engine's
    ``cashier_id`` / ``assignee_id`` key) and the last-activity epoch.
  - An **inactivity timeout** expires the session: a request whose
    last-activity is older than ``AuthConfig.inactivity_seconds`` is treated
    as unauthenticated and redirected to ``/login``.
  - The ``Secure`` cookie flag is env-controlled so the same code runs over
    HTTP in local dev and HTTPS in production.

The deep module here is the :class:`SessionAuthenticator`: callers hand it a
cookie string and a "now" timestamp and get back either a verified
:class:`Session` or ``None``. The signing key, expiry window, and clock are
all injected — no module-level state, no hidden env reads, every seam
testable.

CSRF protection is intentionally NOT in scope for Wave 1 (the threat model
is shared-passphrase + TLS; CSRF is a follow-up). Logout is therefore a plain
POST form without a token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

#: Name of the session cookie. Constant so the middleware, the login route,
#: and the logout route all agree on it without a shared import dance.
SESSION_COOKIE = "tangerine_session"

#: Routes that are reachable without authentication. ``/login`` (GET and POST)
#: and the static asset mount live here; everything else is gated.
PUBLIC_PATHS: frozenset[str] = frozenset({"/login", "/static"})


@dataclass(frozen=True)
class Session:
    """A verified session — what the signed cookie decodes to.

    ``assignee_id`` is the role the partner selected at login; it is what
    future capture flows stamp on actions for attribution. ``last_activity``
    is the epoch seconds carried in the cookie, refreshed on each request.
    ``session_id`` is minted once at login and rides along unchanged through
    every sliding refresh — it is what groups a browser session's config
    edits in the audit log, so "revert this session" (Wave 1.5, Slice 5) can
    undo a batch. ``None`` for cookies signed before Slice 5; those sessions
    simply have no batch to revert.
    """

    assignee_id: str
    last_activity: int
    session_id: str | None = None


@dataclass(frozen=True)
class AuthConfig:
    """Everything the auth machinery needs, injected at app construction.

    Kept as one object so :func:`tangerine.web.app.create_app` has a single
    auth-shaped kwarg cluster rather than six loose parameters.

    - ``passphrase``         the shared passphrase (from env in prod). The
                             app fails loudly at startup if this is empty.
    - ``signing_secret``     the itsdangerous signing key. From env in prod.
    - ``cookie_secure``      ``True`` to set the cookie's ``Secure`` flag
                             (prod behind TLS); ``False`` for local HTTP dev.
    - ``inactivity_seconds`` the inactivity-timeout window. The default is
                             8 hours (slice-4 issue: "e.g. 8 hours").
    """

    passphrase: str
    signing_secret: str
    cookie_secure: bool = False
    inactivity_seconds: int = 8 * 60 * 60


class SessionAuthenticator:
    """Signs and verifies session cookies.

    The deep module: the rest of the app sees only ``sign(session)`` and
    ``verify(cookie_value, now)``. The itsdangerous serializer, the payload
    schema, the expiry math, and the tamper detection all live behind it.

    ``max_age`` is exposed as a method arg so tests can pass a tiny window
    without waiting for the wall clock; production callers pass
    ``config.inactivity_seconds``.
    """

    def __init__(self, signing_secret: str) -> None:
        # A fresh serializer per secret. ``salts`` namespace signatures so a
        # signature signed under one purpose (sessions) cannot be replayed
        # under another (future signed tokens) — even with the same key.
        self._serializer = URLSafeTimedSerializer(
            signing_secret, salt="tangerine.session"
        )

    def sign(self, session: Session) -> str:
        """Return the signed cookie value for ``session``."""
        return self._serializer.dumps(
            {
                "a": session.assignee_id,
                "t": session.last_activity,
                "s": session.session_id,
            }
        )

    def verify(
        self, cookie_value: str | None, *, max_age: int, now_epoch: int
    ) -> Session | None:
        """Decode and validate ``cookie_value``.

        Returns the verified :class:`Session`, or ``None`` when:
          - there is no cookie (``cookie_value is None``),
          - the signature is bad (tampered or signed under a different key),
          - the cookie's own ``max_age`` has expired (itsdangerous' built-in
            check; this catches a cookie whose timestamp itself is stale
            independent of the inactivity window — belt and braces), or
          - the last-activity timestamp is older than ``max_age`` seconds
            (the inactivity timeout — the slice-4 requirement that the session
            expires after a window of *inactivity*, not after a fixed lifetime).

        ``now_epoch`` is injected so tests can advance the clock without
        sleeping.
        """
        if not cookie_value:
            return None
        try:
            payload: Any = self._serializer.loads(
                cookie_value, max_age=max_age
            )
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            assignee_id = payload["a"]
            last_activity = int(payload["t"])
        except (KeyError, TypeError, ValueError):
            return None
        if now_epoch - last_activity > max_age:
            return None
        return Session(
            assignee_id=assignee_id,
            last_activity=last_activity,
            session_id=payload.get("s"),
        )


def set_session_cookie(
    response: Response,
    *,
    value: str,
    secure: bool,
) -> None:
    """Set the session cookie on ``response`` with the right flags.

    ``httponly`` is always set (JS must not read the session cookie — that is
    what makes a stolen-XSS-token session theft harder). ``samesite=lax`` is
    always set so the cookie is not sent on cross-site POSTs (the cheapest
    CSRF defence that ships for free with the cookie itself). ``secure``
    follows ``AuthConfig.cookie_secure``.
    """
    response.set_cookie(
        SESSION_COOKIE,
        value,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Delete the session cookie from ``response`` (logout)."""
    response.delete_cookie(SESSION_COOKIE, path="/")


class AuthMiddleware(BaseHTTPMiddleware):
    """Gates every route except those under :data:`PUBLIC_PATHS`.

    A request without a valid session cookie is redirected to ``/login`` with
    a 302. A request with a valid cookie has ``request.state.assignee_id``
    populated, and the cookie is re-issued with a refreshed last-activity
    timestamp (sliding inactivity window).

    The middleware is path-prefix based for the static mount (``/static``)
    so the CSS and future HTMX assets load without a session.
    """

    def __init__(
        self,
        app: Any,
        *,
        authenticator: SessionAuthenticator,
        config: AuthConfig,
        now_epoch: int | None = None,
    ) -> None:
        super().__init__(app)
        self._authenticator = authenticator
        self._config = config
        # ``now_epoch`` is injectable so tests can pin "now" deterministically.
        # In production it is read per-request from the wall clock below.
        self._now_epoch_override = now_epoch

    async def dispatch(
        self, request: Request, call_next: Any
    ) -> Response:
        if _is_public(request.url.path):
            return cast(Response, await call_next(request))

        now = self._now(request)
        cookie_value = request.cookies.get(SESSION_COOKIE)
        session = self._authenticator.verify(
            cookie_value,
            max_age=self._config.inactivity_seconds,
            now_epoch=now,
        )
        if session is None:
            return _redirect_to_login()

        # Expose the verified assignee to downstream routes. Future capture
        # flows read ``request.state.assignee_id``; the Wave 1 review routes
        # simply ignore it. ``session_id`` is what the config-write routes
        # stamp on audit entries so a whole browser session is revertable
        # as a batch.
        request.state.assignee_id = session.assignee_id
        request.state.session_id = session.session_id

        response = cast(Response, await call_next(request))

        # ``POST /logout`` clears the session cookie itself; the sliding
        # refresh must not re-stamp it on top of that clear, or logout
        # silently fails (the cleared cookie gets immediately re-issued).
        # Detecting this by route name (rather than "does the response
        # already carry a Set-Cookie for this name") keeps the middleware
        # honest: a logout is the one route that intentionally ends the
        # session, so it gets an explicit carve-out.
        if request.method == "POST" and request.url.path == "/logout":
            return response

        # Sliding inactivity window: refresh the last-activity timestamp on
        # every authenticated request so active use does not time out. The
        # cookie is re-signed because the timestamp is part of the signed
        # payload.
        refreshed = Session(
            assignee_id=session.assignee_id,
            last_activity=now,
            session_id=session.session_id,
        )
        set_session_cookie(
            response,
            value=self._authenticator.sign(refreshed),
            secure=self._config.cookie_secure,
        )
        return response

    def _now(self, request: Request) -> int:
        """Epoch seconds for "now".

        Override first (tests), then per-request wall clock (prod). Per-request
        rather than construction-time so a long-lived test app does not pin a
        stale "now".
        """
        if self._now_epoch_override is not None:
            return self._now_epoch_override
        # ``request`` carries no first-class epoch; derive from UTC now.
        return int(datetime.now(tz=timezone.utc).timestamp())


def _is_public(path: str) -> bool:
    """True when ``path`` is reachable without authentication.

    ``/login`` is exact-matched. The static mount is prefix-matched so
    ``/static/review.css`` and any future asset resolve.
    """
    if path == "/login":
        return True
    return path == "/static" or path.startswith("/static/")


def _redirect_to_login() -> Response:
    """A 302 to ``/login`` — the unauthenticated-request response."""
    from starlette.responses import RedirectResponse

    return RedirectResponse(url="/login", status_code=302)


__all__ = [
    "AuthConfig",
    "AuthMiddleware",
    "SESSION_COOKIE",
    "Session",
    "SessionAuthenticator",
    "clear_session_cookie",
    "set_session_cookie",
]
