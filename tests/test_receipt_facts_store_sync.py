"""Persistence and sync-wiring seam for the receipt-grain facts path
(issue #147).

Covers the migration (``receipt_facts`` table), the SQLite round-trip
(idempotent on ``receipt_number``, signed money preserved), and the sync
orchestrator's wiring: with a channel map configured the sync records
facts; without one the facts path is skipped entirely; a parse failure
(unmapped payment type) surfaces as a readable ``SyncResult.errors`` entry
rather than a crash.
"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal as D
from typing import Any

import pytest

from tangerine.loyverse.config import LoyverseCredentials, PaymentChannelMap
from tangerine.loyverse.http import LoyverseHttpClient
from tangerine.loyverse.store import InMemoryLoyverseStore, ReceiptFact
from tangerine.loyverse.sync import SyncOrchestrator, run_sync
from tangerine.storage.sqlite_store import SqliteLoyverseStore


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


def _receipts_envelope(receipts: list[dict]) -> bytes:
    return json.dumps({"receipts": receipts, "cursor": None}).encode("utf-8")


def _items_envelope() -> bytes:
    return json.dumps({"items": [], "cursor": None}).encode("utf-8")


CHANNELS = PaymentChannelMap(
    channels={"pay-cash": "cash", "pay-transfer": "qr", "pay-card": "card"}
)


def _fact(**overrides: object) -> ReceiptFact:
    defaults: dict = {
        "receipt_number": "4-10000",
        "receipt_type": "SALE",
        "local_date": date(2026, 7, 1),
        "cash": D("150.50"),
        "qr": D("0"),
        "card": D("200"),
        "discount": D("49.50"),
        "total_money": D("350.50"),
    }
    defaults.update(overrides)
    return ReceiptFact(**defaults)


def _api_receipt(receipt_number: str = "4-10000") -> dict:
    return {
        "receipt_number": receipt_number,
        "receipt_type": "SALE",
        "refund_for": None,
        "created_at": "2026-07-01T06:30:00.000Z",
        "receipt_date": "2026-07-01",
        "total_money": 350.5,
        "total_tax": 0,
        "payments": [
            {"payment_type_id": "pay-cash", "name": "Cash", "money_amount": 150.5},
            {"payment_type_id": "pay-card", "name": "Card", "money_amount": 200.0},
        ],
        "total_discounts": [
            {"id": "d1", "name": "Happy hour", "scope": "RECEIPT", "money_amount": 49.5}
        ],
        "line_items": [
            {
                "id": "li-1",
                "item_id": "item-1",
                "variant_id": "v-1",
                "item_name": "Latte",
                "sku": "latte",
                "quantity": 1,
                "price": 350.5,
                "total_money": 350.5,
            }
        ],
    }


# --- SQLite round-trip -------------------------------------------------------


def test_sqlite_round_trip_preserves_exact_decimals() -> None:
    store = SqliteLoyverseStore.connect(":memory:")
    store.record_receipt_facts([_fact()])
    facts = store.receipt_facts()
    assert len(facts) == 1
    f = facts[0]
    assert f.cash == D("150.50")
    assert f.card == D("200")
    assert f.discount == D("49.50")
    assert f.local_date == date(2026, 7, 1)
    assert f.receipt_type == "SALE"


def test_sqlite_idempotent_on_receipt_number() -> None:
    store = SqliteLoyverseStore.connect(":memory:")
    store.record_receipt_facts([_fact()])
    store.record_receipt_facts([_fact()])
    assert len(store.receipt_facts()) == 1


def test_sqlite_stores_refunds_with_negative_splits() -> None:
    store = SqliteLoyverseStore.connect(":memory:")
    store.record_receipt_facts([
        _fact(
            receipt_number="4-10340",
            receipt_type="REFUND",
            cash=D("-180"),
            qr=D("0"),
            card=D("0"),
            discount=D("0"),
            total_money=D("-180"),
        )
    ])
    f = store.receipt_facts()[0]
    assert f.receipt_type == "REFUND"
    assert f.cash == D("-180")


def test_migration_has_receipt_type_check() -> None:
    """The CHECK constraint backs the parser's refusal at the storage layer."""
    store = SqliteLoyverseStore.connect(":memory:")
    conn = store._conn  # noqa: SLF001 - test asserts schema shape directly
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO receipt_facts VALUES ('x', 'VOID', '2026-07-01',"
            " '0', '0', '0', '0', '0')"
        )


# --- sync wiring -------------------------------------------------------------


def _sync_stub(receipts: list[dict]) -> StubHttp:
    return StubHttp(
        routes={
            "/v1.0/receipts": [_receipts_envelope(receipts)],
            "/v1.0/items": [_items_envelope()],
        }
    )


def test_sync_records_facts_when_channel_map_given() -> None:
    stub = _sync_stub([_api_receipt()])
    store = InMemoryLoyverseStore()
    orchestrator = SyncOrchestrator(
        client=_client(stub), store=store, payment_channels=CHANNELS
    )

    orchestrator.sync_sales_and_menu(
        at=datetime(2026, 7, 2, tzinfo=timezone.utc)
    )

    facts = store.receipt_facts()
    assert len(facts) == 1
    assert facts[0].cash == D("150.5")
    assert facts[0].card == D("200")
    assert facts[0].discount == D("49.5")


def test_sync_skips_facts_path_without_channel_map() -> None:
    stub = _sync_stub([_api_receipt()])
    store = InMemoryLoyverseStore()
    orchestrator = SyncOrchestrator(
        client=_client(stub), store=store, payment_channels=None
    )

    orchestrator.sync_sales_and_menu(
        at=datetime(2026, 7, 2, tzinfo=timezone.utc)
    )

    assert store.receipt_facts() == []
    assert len(store.sales()) == 1  # line-grain sales still recorded


def test_run_sync_surfaces_parse_error_readably() -> None:
    receipt = _api_receipt()
    receipt["payments"] = [
        {"payment_type_id": "uuid-unmapped", "name": "???", "money_amount": 350.5}
    ]
    stub = _sync_stub([receipt])
    store = InMemoryLoyverseStore()
    result = run_sync(
        store=store,
        credentials=LoyverseCredentials(access_token="tok", store_id="s"),
        urlopen=stub,
        today=date(2026, 7, 28),
        payment_channels=CHANNELS,
    )
    assert result.errors, "expected the unmapped payment type to fail the sync"
    assert "not mapped to a channel" in result.errors[0]
    assert "uuid-unmapped" in result.errors[0]


def test_run_sync_still_works_without_channel_map() -> None:
    """The line-grain sync is unchanged when the derivation isn't configured."""
    stub = _sync_stub([_api_receipt()])
    store = InMemoryLoyverseStore()
    result = run_sync(
        store=store,
        credentials=LoyverseCredentials(access_token="tok", store_id="s"),
        urlopen=stub,
        today=date(2026, 7, 28),
        payment_channels=None,
    )
    assert result.errors == ()
    assert result.rows_ingested == 1
    assert store.receipt_facts() == []


def _client(stub: StubHttp) -> LoyverseHttpClient:
    return LoyverseHttpClient(
        LoyverseCredentials(access_token="tok", store_id="s"), urlopen=stub
    )
