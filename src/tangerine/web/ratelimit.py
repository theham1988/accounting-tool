"""Login rate-limiting (Wave 1, Slice 6).

The shared passphrase is the whole authentication story (PRD: two equal
partners, no per-user accounts), so the login route is the brute-force
surface. PRD user story 32 / the slice-6 issue require the route to be
rate-limited: a client gets a small budget of attempts per window, and
excess attempts are rejected with HTTP 429 before the passphrase is checked.

The deep module here is :class:`RateLimiter`: callers hand it a key and a
"now" timestamp and get back a single bool. The per-key fixed-window
bookkeeping (window start, attempt count, roll-over) lives entirely behind
that one method, and the clock is injected so the window behaviour is testable
without sleeping. The limiter is in-memory and process-local, which is
sufficient because the Wave 1 deployment is a single uvicorn instance (PRD:
single-instance) — a multi-instance deploy would swap this for a shared store
without changing the call site.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

#: Default budget: five login attempts per IP per minute. Generous for a human
#: partner fat-fingering a passphrase, far too small to brute-force a strong
#: shared passphrase. The slice-6 issue gives "e.g. 5 attempts per IP per
#: minute" as the example; these are the defaults, overridable per deploy.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_WINDOW_SECONDS = 60

#: The login path the middleware guards. A module constant so the app factory
#: and the middleware agree without a magic string in two places.
LOGIN_PATH = "/login"


class RateLimiter:
    """A fixed-window per-key attempt counter.

    ``allow(key, now_epoch)`` records one attempt against ``key`` and returns
    ``True`` when it is within budget for the current window, ``False`` when the
    budget is already spent. The window is fixed (not sliding): the first
    attempt for a key starts the window, and the window rolls over once
    ``window_seconds`` have elapsed, resetting the count.

    The clock is passed in on every call rather than read internally, so tests
    advance time deterministically and the limiter holds no hidden global
    state beyond its per-key buckets.
    """

    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        self._max = max_attempts
        self._window = window_seconds
        # key -> (window_start_epoch, attempts_in_window)
        self._buckets: dict[str, tuple[int, int]] = {}

    def allow(self, key: str, *, now_epoch: int) -> bool:
        """Record an attempt for ``key``; return whether it is within budget."""
        start, count = self._buckets.get(key, (now_epoch, 0))
        if now_epoch - start >= self._window:
            # Window rolled over: start a fresh window at "now".
            start, count = now_epoch, 0
        if count >= self._max:
            # Budget spent for this window; do not count the rejected attempt
            # (so a client hammering the endpoint cannot extend its own lockout
            # indefinitely — the window still expires on schedule).
            self._buckets[key] = (start, count)
            return False
        self._buckets[key] = (start, count + 1)
        return True


def client_ip(request: Request) -> str:
    """Best-effort real client IP for rate-limit keying.

    Behind nginx the socket peer is always the proxy (``127.0.0.1``), so keying
    on it would lump every partner into one bucket. The real client address is
    read from ``X-Forwarded-For`` instead. nginx is configured with
    ``proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`` (see
    ``deploy/nginx.conf``), which *appends* the address nginx actually saw to
    whatever the client supplied — so the right-most entry is trustworthy and a
    client-supplied spoofed value sits to its left and is ignored. Falls back to
    the socket peer when no header is present (direct/local access, tests).
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    client = request.client
    return client.host if client is not None else "unknown"


class LoginRateLimitMiddleware(BaseHTTPMiddleware):
    """Rejects excess ``POST /login`` attempts with HTTP 429.

    Only the login POST is limited: ``GET /login`` (rendering the form) and
    every other route are passed straight through, so a partner reloading the
    login page or using the tool normally is never throttled. The "now" clock
    is injected (a callable) so tests can pin and advance it; production passes
    a wall-clock reader.
    """

    def __init__(
        self,
        app: Any,
        *,
        limiter: RateLimiter,
        now: Callable[[], int],
        path: str = LOGIN_PATH,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._now = now
        self._path = path

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.method == "POST" and request.url.path == self._path:
            key = client_ip(request)
            if not self._limiter.allow(key, now_epoch=self._now()):
                return PlainTextResponse(
                    "Too many login attempts. Please wait a minute and try again.",
                    status_code=429,
                )
        return await call_next(request)  # type: ignore[no-any-return]


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_WINDOW_SECONDS",
    "LOGIN_PATH",
    "LoginRateLimitMiddleware",
    "RateLimiter",
    "client_ip",
]
