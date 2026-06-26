"""End-to-end Loyverse API sync test seam (slice 02).

Per the PRD testing rules these tests are readable as worked examples, and per
issue 02 the Loyverse client is the genuine external boundary: the seam injects
synthetic Loyverse payloads (no live HTTP). The real client takes a `urlopen`
callable, so tests pass a stub that returns canned JSON pages.

Acceptance criteria covered here (docs/issues/02-loyverse-api-sync.md):

- client authenticates via stored credentials           (test_client_sets_bearer_token)
- sales polled and stored with Loyverse timestamp       (test_sales_polled_and_stored_with_timestamp)
- items + menu state polled; menu-change history kept   (test_menu_change_history_preserved_with_timestamps)
- e2e: synthetic payloads -> stored sales match         (test_end_to_end_sync_stores_sales)
- items sold without a recipe mapping flagged unmapped  (test_unmapped_items_surfacable_from_store)
- polling cadence configurable; default daily after close (test_polling_config_defaults)
- pagination across cursor pages                       (test_pagination_follows_cursor)
- refunds excluded from positive sales                 (test_refunds_excluded_from_sales)
- auth failure raises a typed error                    (test_auth_failure_raises)
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from tangerine.loyverse.config import LoyverseCredentials, PollingConfig
from tangerine.loyverse.http import (
    LoyverseApiError,
    LoyverseAuthError,
    LoyverseHttpClient,
)
from tangerine.loyverse.parser import (
    parse_receipts_to_sales,
    parse_items_snapshot,
)
from tangerine.loyverse.store import InMemoryLoyverseStore
from tangerine.loyverse.source import StoreSource
from tangerine.loyverse.sync import SyncOrchestrator

D = Decimal


# --- helpers: build synthetic Loyverse payloads -----------------------------


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
    return {
        "id": item_id,
        "item_name": name,
        "category_id": category_id,
        "sku": sku,
        "variants": [
            {
                "id": f"{item_id}-v1",
                "name": name,
                "sku": sku,
                "price": price,
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

    This is the only seam where real HTTP would live. Everything inside the
    client beyond `urlopen` is exercised for real.
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


def _credentials() -> LoyverseCredentials:
    return LoyverseCredentials(access_token="tok-secret", store_id="store-1")


# --- 1. client authenticates via stored credentials -------------------------


def test_client_sets_bearer_token() -> None:
    """Every request from the client must carry the stored token as Bearer."""
    stub = StubHttp(
        routes={"/v1.0/receipts": [_receipts_envelope([])]}
    )
    client = LoyverseHttpClient(_credentials(), urlopen=stub)

    client.get("/receipts", params={})

    assert stub.requests, "client made no request"
    _, headers, _ = stub.requests[0]
    assert headers is not None
    assert headers.get("Authorization") == "Bearer tok-secret"


# --- 2 + 8. auth failure raises a typed error -------------------------------


def _erroring_http(status: int) -> StubHttp:
    def _raise(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> StubResponse:
        resp = StubResponse(b"{}", status=status)
        # Mimic HTTPError-style: the client checks .status and raises.
        resp.status = status  # type: ignore[misc]
        return resp

    return _raise  # type: ignore[return-value]


def test_auth_failure_raises() -> None:
    """A 401 from Loyverse surfaces as LoyverseAuthError, not a raw exception."""
    def urlopen_401(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> StubResponse:
        return StubResponse(b'{"errors":[{"detail":"bad token"}]}', status=401)

    client = LoyverseHttpClient(_credentials(), urlopen=urlopen_401)  # type: ignore[arg-type]

    with pytest.raises(LoyverseAuthError):
        client.get("/receipts", params={})


# --- 3. sales polled and stored with Loyverse timestamp ---------------------


def test_parser_extracts_sales_with_transaction_timestamp() -> None:
    """A SALE receipt with one line becomes one Sale at the receipt timestamp.

    Worked example: one Chang draft at 120 THB sold 2026-06-24 09:15 UTC.
    """
    created = "2026-06-24T09:15:00.000Z"
    payload = {"receipts": [_receipt_json(receipt_number="2-1", created_at=created)]}

    records = parse_receipts_to_sales(payload)

    assert len(records) == 1
    sale = records[0].sale
    assert sale.item_id == "chang-draft-500"
    assert sale.timestamp == date(2026, 6, 24)
    assert sale.sell_price == D("120")
    assert sale.quantity == 1
    # The record carries its Loyverse identity for idempotent storage.
    assert records[0].receipt_number == "2-1"
    assert records[0].line_id == "li-1"


def test_parser_aggregates_lines_into_sales() -> None:
    """A line with quantity 3 becomes one Sale with quantity 3 (single line)."""
    payload = {
        "receipts": [
            _receipt_json(
                receipt_number="2-2",
                created_at="2026-06-24T10:00:00.000Z",
                line_items=[
                    {
                        "id": "li-1",
                        "item_id": "d5fe0da6-44b3-4633-9915-e9dc5118cbfc",
                        "variant_id": "v-1",
                        "item_name": "Chang Draft 500ml",
                        "sku": "chang-draft-500",
                        "quantity": 3,
                        "price": 120,
                        "total_money": 360,
                    }
                ],
            )
        ]
    }

    records = parse_receipts_to_sales(payload)

    assert records[0].sale.quantity == 3
    assert records[0].sale.sell_price == D("120")


# --- 4. refunds excluded from positive sales --------------------------------


def test_refunds_excluded_from_sales() -> None:
    """A REFUND receipt (receipt_type=REFUND) must not produce a Sale.

    Refund handling is deferred to a later slice; for sales polling the sync
    must not count a refunded sale as fresh revenue.
    """
    payload = {
        "receipts": [
            _receipt_json(receipt_number="2-10", created_at="2026-06-24T11:00:00.000Z"),
            _receipt_json(
                receipt_number="2-11",
                created_at="2026-06-24T11:30:00.000Z",
                receipt_type="REFUND",
            ),
        ]
    }

    records = parse_receipts_to_sales(payload)

    assert len(records) == 1
    assert records[0].sale.timestamp == date(2026, 6, 24)


# --- 5. menu snapshot + change history --------------------------------------


def test_parse_items_snapshot_captures_current_menu() -> None:
    payload = {
        "items": [
            _item_json(item_id="i-1", name="Chang Draft 500ml", sku="chang-draft-500", price=120),
            _item_json(item_id="i-2", name="Espresso Latte", sku="espresso-latte", price=80, category_id="cat-cafe"),
        ]
    }

    snapshot = parse_items_snapshot(payload)

    by_id = {mi.item_id: mi for mi in snapshot.items}
    assert set(by_id) == {"i-1", "i-2"}
    assert by_id["i-1"].name == "Chang Draft 500ml"
    assert by_id["i-1"].sell_price == D("120")


def test_menu_change_history_preserved_with_timestamps() -> None:
    """Two snapshots at different times: a price change and a new item both
    appear in the store's menu-change history with their snapshot timestamps."""
    store = InMemoryLoyverseStore()

    # First snapshot: Chang at 120.
    store.record_menu_snapshot(
        parse_items_snapshot(
            {"items": [_item_json(item_id="i-1", name="Chang Draft", sku="chang-draft-500", price=120)]}
        ),
        at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )
    # Second snapshot: Chang repriced to 140, new Latte added.
    store.record_menu_snapshot(
        parse_items_snapshot(
            {
                "items": [
                    _item_json(item_id="i-1", name="Chang Draft", sku="chang-draft-500", price=140),
                    _item_json(item_id="i-2", name="Latte", sku="espresso-latte", price=80, category_id="cat-cafe"),
                ]
            }
        ),
        at=datetime(2026, 6, 24, tzinfo=timezone.utc),
    )

    history = store.menu_change_history()

    # Two change records: the reprice of i-1 and the addition of i-2.
    by_item = {h.item_id: h for h in history}
    assert by_item["i-1"].change_kind == "price_change"
    assert by_item["i-1"].at == datetime(2026, 6, 24, tzinfo=timezone.utc)
    assert by_item["i-2"].change_kind == "added"
    assert by_item["i-2"].at == datetime(2026, 6, 24, tzinfo=timezone.utc)


# --- 6. polling cadence configurable; default daily after close -------------


def test_polling_config_defaults() -> None:
    """Default polling cadence is daily after close (per PRD)."""
    cfg = PollingConfig()

    assert cfg.cadence == "daily"
    # "After close" expressed as an hour-of-day > the bar close (10pm).
    assert cfg.after_close_hour == 22


def test_polling_config_is_configurable() -> None:
    cfg = PollingConfig(cadence="hourly", after_close_hour=23)
    assert cfg.cadence == "hourly"
    assert cfg.after_close_hour == 23


# --- 7. pagination follows cursor -------------------------------------------


def test_pagination_follows_cursor() -> None:
    """Two pages of receipts linked by a cursor become one flat list of sales.

    Page 1 returns two receipts + a cursor; page 2 returns one receipt + no
    cursor. The client must follow the cursor and the parser must flatten both
    pages into three sales.
    """
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [
                _receipts_envelope(
                    [
                        _receipt_json(receipt_number="p1-1", created_at="2026-06-24T09:00:00.000Z"),
                        _receipt_json(receipt_number="p1-2", created_at="2026-06-24T09:30:00.000Z"),
                    ],
                    cursor="next-page",
                ),
                _receipts_envelope(
                    [_receipt_json(receipt_number="p2-1", created_at="2026-06-24T10:00:00.000Z")],
                    cursor=None,
                ),
            ]
        }
    )
    client = LoyverseHttpClient(_credentials(), urlopen=stub)

    pages = client.get_pages("/receipts")

    all_pages = list(pages)
    flat_receipts = [r for page in all_pages for r in page.get("receipts", [])]
    records = parse_receipts_to_sales({"receipts": flat_receipts})

    assert len(records) == 3
    # The client must have requested page 2 with the cursor returned by page 1.
    second_req = stub.requests[1]
    assert second_req[2].get("cursor") == "next-page"


# --- 9. unmapped items surfaced from the store ------------------------------


def test_unmapped_items_surfacable_from_store() -> None:
    """Items that were sold but have no recipe mapping are surfaced as unmapped.

    Recipes arrive in slice 04; for slice 02 we must surface the sold-but-
    unmapped item ids so they are visible immediately (PRD user story 12).
    The store knows what was sold; the StoreSource adapter, given an empty
    recipe set, reports those sold ids as unmapped.
    """
    store = InMemoryLoyverseStore()
    store.record_sales(
        parse_receipts_to_sales(
            {
                "receipts": [
                    _receipt_json(receipt_number="u-1", created_at="2026-06-24T09:00:00.000Z")
                ]
            }
        )
    )

    source = StoreSource(store, recipes=[])
    unmapped = source.unmapped_sold_item_ids()

    assert unmapped == ("chang-draft-500",)


# --- end-to-end: synthetic payloads through the orchestrator ----------------


def test_end_to_end_sync_stores_sales_and_menu() -> None:
    """Full slice-02 seam: orchestrator -> client -> parser -> store.

    Given synthetic Loyverse receipts+items pages, the store ends up holding
    the sales (with Loyverse transaction timestamps) and a menu snapshot with
    change history.
    """
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [
                _receipts_envelope(
                    [_receipt_json(receipt_number="e2e-1", created_at="2026-06-24T12:00:00.000Z")],
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
    client = LoyverseHttpClient(_credentials(), urlopen=stub)
    store = InMemoryLoyverseStore()
    orchestrator = SyncOrchestrator(client=client, store=store)

    orchestrator.sync_sales_and_menu()

    # Sales stored with the Loyverse transaction date.
    sales = store.sales()
    assert len(sales) == 1
    assert sales[0].item_id == "chang-draft-500"
    assert sales[0].timestamp == date(2026, 6, 24)
    assert sales[0].sell_price == D("120")
    # Menu snapshot + at least one change record (the initial add).
    assert store.current_menu().get("i-1") is not None
    assert any(h.change_kind == "added" for h in store.menu_change_history())


def test_sync_is_idempotent() -> None:
    """Syncing the same receipts twice must not double the stored sales.

    Idempotency is by receipt_number + line id. The same payload replayed
    produces no new sales on the second sync.
    """
    page = _receipts_envelope(
        [_receipt_json(receipt_number="idem-1", created_at="2026-06-24T12:00:00.000Z")],
        cursor=None,
    )
    empty_items = _envelope([], cursor=None)
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [page, page],
            "/v1.0/items": [empty_items, empty_items],
        }
    )
    client = LoyverseHttpClient(_credentials(), urlopen=stub)
    store = InMemoryLoyverseStore()
    orchestrator = SyncOrchestrator(client=client, store=store)

    orchestrator.sync_sales_and_menu()
    orchestrator.sync_sales_and_menu()

    assert len(store.sales()) == 1


def test_two_distinct_sales_colliding_on_value_are_both_kept() -> None:
    """Idempotency must not collapse two genuinely different sales.

    Two different receipts each selling the same SKU on the same day at the same
    price and quantity would collide under a value-based dedup key. Loyverse
    identifies lines by (receipt_number, line_id), so both sales survive.
    """
    receipt_a = _receipt_json(receipt_number="2-100", created_at="2026-06-24T12:00:00.000Z")
    receipt_b = _receipt_json(receipt_number="2-101", created_at="2026-06-24T12:30:00.000Z")
    # Same SKU, same price, same qty, same day — only receipt_number differs.
    page = _receipts_envelope([receipt_a, receipt_b], cursor=None)
    stub = StubHttp(
        routes={
            "/v1.0/receipts": [page],
            "/v1.0/items": [_envelope([], cursor=None)],
        }
    )
    client = LoyverseHttpClient(_credentials(), urlopen=stub)
    store = InMemoryLoyverseStore()
    SyncOrchestrator(client=client, store=store).sync_sales_and_menu()

    assert len(store.sales()) == 2


def test_discontinued_item_recorded_in_change_history() -> None:
    """An item present in one snapshot but absent in the next is a discontinuation.

    Issue 02 lists discontinuations among the menu changes to preserve and
    timestamp. The store must record a DISCONTINUED change, not silently drop it.
    """
    store = InMemoryLoyverseStore()
    first = parse_items_snapshot(
        {"items": [_item_json(item_id="i-1", name="Chang Draft", sku="c", price=120)]}
    )
    store.record_menu_snapshot(first, at=datetime(2026, 6, 23, tzinfo=timezone.utc))
    # Next snapshot: i-1 is gone (discontinued).
    second = parse_items_snapshot({"items": []})
    store.record_menu_snapshot(second, at=datetime(2026, 6, 24, tzinfo=timezone.utc))

    history = store.menu_change_history()
    disc = [h for h in history if h.change_kind.value == "discontinued"]
    assert len(disc) == 1
    assert disc[0].item_id == "i-1"
    assert disc[0].at == datetime(2026, 6, 24, tzinfo=timezone.utc)
    assert disc[0].from_value == "Chang Draft"
    assert disc[0].to_value is None
    # And it is no longer in the current menu.
    assert "i-1" not in store.current_menu()
