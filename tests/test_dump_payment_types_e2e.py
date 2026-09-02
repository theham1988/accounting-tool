"""End-to-end seam for the payment-types dump helper
(scripts/dump_payment_types.py, issue #147).

The genuine external boundary is the ``GET /payment_types`` endpoint's
shape; these tests stub the HTTP seam (same style as
``test_dump_loyverse_items_e2e.py``) and pin:

- the TSV output columns and sorting;
- exit 1 when credentials are absent;
- exit 2 on a Loyverse API failure.
"""

from __future__ import annotations

import io
import json
from typing import Any

from scripts.dump_payment_types import dump_payment_types


class StubResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._buf = io.BytesIO(body)
        self.status = status

    def read(self, amt: int = -1) -> bytes:
        return self._buf.read(-1 if amt is None else amt)

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self._buf.close()


class StubHttp:
    """Serves canned pages by path; the only seam where HTTP would live."""

    def __init__(self, routes: dict[str, list[bytes]]) -> None:
        self._routes = {k: list(v) for k, v in routes.items()}

    def __call__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> StubResponse:
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        pages = self._routes.get(path)
        if pages is None or not pages:
            raise AssertionError(f"unexpected request to {url!r}")
        return StubResponse(pages.pop(0))


def _page(entries: list[dict[str, Any]]) -> bytes:
    return json.dumps({"payment_types": entries, "cursor": None}).encode("utf-8")


def test_dump_lists_payment_types_sorted(monkeypatch) -> None:
    monkeypatch.delenv("LOYVERSE_ACCESS_TOKEN", raising=False)
    stub = StubHttp(
        routes={
            "/v1.0/payment_types": [
                _page([
                    {"id": "zzz-transfer", "name": "Transfer", "type": "OTHER"},
                    {"id": "aaa-cash", "name": "Cash", "type": "CASH"},
                    {"id": "mmm-card", "name": "Card", "type": "NONINTEGRATEDCARD"},
                ])
            ]
        }
    )
    out = io.StringIO()

    code = dump_payment_types(access_token="tok", urlopen=stub, out=out)

    assert code == 0
    lines = out.getvalue().splitlines()
    assert lines[0] == "payment_type_id\tname\ttype"
    assert lines[1] == "aaa-cash\tCash\tCASH"
    assert lines[2] == "mmm-card\tCard\tNONINTEGRATEDCARD"
    assert lines[3] == "zzz-transfer\tTransfer\tOTHER"


def test_dump_exit_1_without_token(monkeypatch) -> None:
    monkeypatch.delenv("LOYVERSE_ACCESS_TOKEN", raising=False)
    code = dump_payment_types(out=io.StringIO())
    assert code == 1


def test_dump_exit_2_on_api_error(monkeypatch) -> None:
    monkeypatch.delenv("LOYVERSE_ACCESS_TOKEN", raising=False)

    def fail(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> StubResponse:
        return StubResponse(b'{"errors":[{"detail":"bad token"}]}', status=401)

    code = dump_payment_types(access_token="tok", urlopen=fail, out=io.StringIO())
    assert code == 2
