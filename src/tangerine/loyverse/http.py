"""Loyverse HTTP client (slice 02).

The genuine external boundary. Real implementation talks to
``https://api.loyverse.com`` over HTTPS using only the standard library
(``urllib.request``) — no new dependency for slice 02. Authentication is a
single ``Authorization: Bearer <token>`` header per Loyverse's token scheme.

The HTTP call itself is injected as ``urlopen`` so tests substitute a stub that
returns canned JSON pages; the client's URL building, header setting, cursor
pagination, and error mapping are all exercised for real.

Pagination: Loyverse list endpoints return a ``cursor`` until exhausted. The
client exposes ``get_pages`` which yields each decoded page and follows the
cursor automatically.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Protocol
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen as _stdlib_urlopen

from .config import LoyverseCredentials


BASE_URL = "https://api.loyverse.com"


class LoyverseApiError(Exception):
    """Any non-2xx, non-401 response from the Loyverse API."""


class LoyverseAuthError(LoyverseApiError):
    """The stored access token was rejected (HTTP 401)."""


class _Response(Protocol):
    """The subset of ``http.client.HTTPResponse`` the client reads."""

    def read(self, amt: int = -1) -> bytes: ...
    @property
    def status(self) -> int: ...


class Urlopen(Protocol):
    """The injected HTTP seam.

    The real binding is ``urllib.request.urlopen``. Tests pass a stub with the
    same call shape ``(url, headers, params) -> response``.

    Note on ``params``: the client URL-encodes params into ``url`` itself
    (``_url``), so the real stdlib binding ignores this argument. It is kept on
    the signature so test stubs can capture and assert on the params the client
    built (e.g. cursor pagination, store_id scoping).
    """

    def __call__(
        self,
        url: str,
        headers: dict[str, str] | None = ...,
        params: dict[str, Any] | None = ...,
    ) -> _Response: ...


class LoyverseHttpClient:
    """Thin Loyverse REST client with cursor pagination and typed errors."""

    def __init__(
        self,
        credentials: LoyverseCredentials,
        urlopen: Urlopen | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._creds = credentials
        # Bind the stdlib urlopen into the injected shape.
        self._urlopen: Urlopen = urlopen or _stdlib_open
        self._base_url = base_url.rstrip("/")

    def get_pages(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield each page of a Loyverse list endpoint, following cursors.

        ``path`` is the path under ``/v1.0/`` without the leading slash, e.g.
        ``"receipts"`` or ``"items"``. Pagination via the ``cursor`` query
        parameter is handled automatically; the caller just iterates.
        """
        cursor: str | None = None
        while True:
            page_params = dict(params or {})
            if self._creds.store_id:
                page_params.setdefault("store_id", self._creds.store_id)
            if cursor:
                page_params["cursor"] = cursor
            body = self.get(path, params=page_params)
            yield body
            cursor = body.get("cursor")
            if not cursor:
                return

    def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue a single GET and return the decoded JSON body.

        Raises ``LoyverseAuthError`` on 401 and ``LoyverseApiError`` on any
        other non-2xx status.
        """
        url = self._url(path, params)
        resp = self._urlopen(
            url,
            headers={"Authorization": f"Bearer {self._creds.access_token}"},
            params=dict(params or {}),
        )
        status = getattr(resp, "status", 200)
        raw = resp.read()
        if status == 401:
            raise LoyverseAuthError(
                "Loyverse rejected the access token (HTTP 401)"
            )
        if status >= 400:
            raise LoyverseApiError(
                f"Loyverse API error: HTTP {status} for {url}"
            )
        if not raw:
            return {}
        decoded: dict[str, Any] = json.loads(raw.decode("utf-8"))
        return decoded

    def _url(self, path: str, params: dict[str, Any] | None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        full = f"{self._base_url}/v1.0{path}"
        if params:
            full = f"{full}?{urlencode(params)}"
        return full


def _stdlib_open(
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> _Response:
    """Adapt ``urllib.request.urlopen`` to the ``Urlopen`` seam shape.

    ``params`` is intentionally unused: the client already URL-encoded them
    into ``url`` (see ``Urlopen``). It is accepted here only to satisfy the
    seam's call shape; the stdlib binding reads everything off ``url``.
    """
    del params  # already encoded into url by the client
    parsed = urlsplit(url)
    req = Request(parsed.geturl(), headers=headers or {}, method="GET")
    resp: _Response = _stdlib_urlopen(req)
    return resp
