"""Sync orchestrator (slice 02).

Pulls receipts and items from the Loyverse client, parses them, and records the
result in the store. Sales sync is idempotent (the store dedupes); menu sync
records a snapshot with a timestamp, diffing against the previous one to
preserve menu-change history.

PRD note: margins computed between a menu change and the next sync are accepted
as stale until sync — that is documented behaviour, not a bug, given the daily
review cadence. The timestamped change history is what makes that staleness
auditable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .http import LoyverseHttpClient
from .parser import (
    parse_items_snapshot,
    parse_receipts_to_sales,
)
from .store import LoyverseStore


class SyncOrchestrator:
    """Drives a sales+menu sync from the Loyverse client into the store."""

    def __init__(
        self,
        client: LoyverseHttpClient,
        store: LoyverseStore,
    ) -> None:
        self._client = client
        self._store = store

    def sync_sales_and_menu(self, at: datetime | None = None) -> None:
        """Pull receipts and items, parse, and record into the store.

        ``at`` is the sync timestamp for the menu snapshot; defaults to now.
        """
        moment = at or datetime.now(timezone.utc)

        # Flatten all receipt pages into one list, then parse once.
        all_receipts: list[dict[str, Any]] = []
        for page in self._client.get_pages("receipts"):
            all_receipts.extend(page.get("receipts", []))
        records = parse_receipts_to_sales({"receipts": all_receipts})
        self._store.record_sales(records)

        # Items: one snapshot from all pages.
        all_items: list[dict[str, Any]] = []
        for page in self._client.get_pages("items"):
            all_items.extend(page.get("items", []))
        snapshot = parse_items_snapshot({"items": all_items})
        self._store.record_menu_snapshot(snapshot, at=moment)
