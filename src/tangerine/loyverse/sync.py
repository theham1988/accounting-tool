"""Sync orchestrator (slice 02) + run_sync wrapper (slice 03).

Pulls receipts and items from the Loyverse client, parses them, and records the
result in the store. Sales sync is idempotent (the store dedupes); menu sync
records a snapshot with a timestamp, diffing against the previous one to
preserve menu-change history.

PRD note: margins computed between a menu change and the next sync are accepted
as stale until sync — that is documented behaviour, not a bug, given the daily
review cadence. The timestamped change history is what makes that staleness
auditable.

Slice 03 adds ``run_sync``: the shared entry point the ``POST /sync`` route
and ``python -m tangerine.sync`` both call. It builds the client (with an
injectable ``urlopen`` for tests), detects the first sync (empty sales table)
and backfills the last 30 days, computes the result counts via before/after
store snapshots, and turns Loyverse errors into a readable ``SyncResult.errors``
entry instead of crashing the caller. The orchestrator itself gained only an
additive ``since`` parameter that forwards ``created_at_min`` to the receipts
pages — its default behaviour is unchanged, so slice 02's tests stay green.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .config import LoyverseCredentials, PaymentChannelMap
from .http import LoyverseApiError, LoyverseHttpClient
from .parser import (
    LoyverseParseError,
    parse_items_snapshot,
    parse_receipt_facts,
    parse_receipts_to_sales,
)
from .store import DEFAULT_CAFE_CATEGORY_IDS, LoyverseStore

#: How many days of sales the first run backfills so the 7-day rolling average
#: has data immediately rather than reporting zeros for the first week
#: (PRD user story 9). Surfaced as a module constant so the script and route
#: share one source of truth and a future operator can override it.
BACKFILL_DAYS: int = 30


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a sync run, as surfaced to the partner.

    Counts come from before/after snapshots of the store (the store is the
    source of truth), so they are exact even when the orchestrator's
    ``record_sales`` silently deduped replayed receipts. ``errors`` carries
    human-readable Loyverse error strings; the sync-result fragment renders
    them verbatim so a partner reading the page can see what went wrong
    without the page crashing.
    """

    rows_ingested: int
    menu_changes: int
    errors: tuple[str, ...]


class SyncOrchestrator:
    """Drives a sales+menu+facts sync from the Loyverse client into the store."""

    def __init__(
        self,
        client: LoyverseHttpClient,
        store: LoyverseStore,
        *,
        cafe_category_ids: frozenset[str] = DEFAULT_CAFE_CATEGORY_IDS,
        payment_channels: PaymentChannelMap | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._cafe_category_ids = cafe_category_ids
        self._payment_channels = payment_channels

    def sync_sales_and_menu(
        self,
        at: datetime | None = None,
        since: date | None = None,
    ) -> None:
        """Pull receipts and items, parse, and record into the store.

        ``at`` is the sync timestamp for the menu snapshot; defaults to now.

        ``since`` (slice 03) optionally forwards ``created_at_min`` to the
        receipts endpoint, filtering the pull to receipts on or after that
        date. ``None`` (the default) preserves slice 02's behaviour — pull
        every receipt, with idempotency at the store handling the overlap. The
        first-run backfill sets this to ``today - BACKFILL_DAYS`` so the 7-day
        rolling average has data immediately.

        The one flattened receipts list feeds both grains (issue #147): the
        line-grain path (sales for the margin engine, refunds still skipped)
        and the receipt-grain path (facts for the IN-01 derivation, refunds
        included with signed splits). A ``LoyverseParseError`` from the facts
        path (unmapped payment type, payments≠total, unknown receipt type)
        fails the sync loudly — the same refusal family as a bad quantity.

        Menu items are tagged cafe/bar from the configured ``cafe_category_ids``
        (ADR-0009). Under pure-clock segmentation (#65) this segment no longer
        drives revenue splitting, but it still feeds menu-shape views and the
        sold-as-is quick-create.
        """
        moment = at or datetime.now(timezone.utc)

        # Flatten all receipt pages into one list, then parse once.
        receipt_params: dict[str, Any] | None = (
            {"created_at_min": _iso_z(since)} if since is not None else None
        )
        all_receipts: list[dict[str, Any]] = []
        for page in self._client.get_pages("receipts", params=receipt_params):
            all_receipts.extend(page.get("receipts", []))
        records = parse_receipts_to_sales({"receipts": all_receipts})
        self._store.record_sales(records)
        if self._payment_channels is not None:
            facts = parse_receipt_facts(
                {"receipts": all_receipts}, self._payment_channels
            )
            self._store.record_receipt_facts(facts)

        # Items: one snapshot from all pages.
        all_items: list[dict[str, Any]] = []
        for page in self._client.get_pages("items"):
            all_items.extend(page.get("items", []))
        snapshot = parse_items_snapshot(
            {"items": all_items},
            store_id=self._client.store_id,
            cafe_category_ids=self._cafe_category_ids,
        )
        self._store.record_menu_snapshot(snapshot, at=moment)


def run_sync(
    *,
    store: LoyverseStore,
    credentials: LoyverseCredentials,
    urlopen: Any = None,
    today: date | None = None,
    backfill_days: int = BACKFILL_DAYS,
    cafe_category_ids: frozenset[str] = DEFAULT_CAFE_CATEGORY_IDS,
    payment_channels: PaymentChannelMap | None = None,
) -> SyncResult:
    """Run one Loyverse sales+menu+facts sync and return its result.

    Shared by the ``POST /sync`` route and ``python -m tangerine.sync`` — the
    two callers differ only in how they surface the result (an HTML fragment
    vs. a printed line). Both call this so they can never drift apart.

    First-run detection: when the store's sales table is empty, this is the
    first sync, so the orchestrator is asked to backfill
    ``today - backfill_days`` (PRD user story 9). Subsequent syncs pull
    everything (no date filter); idempotency at the store handles the overlap.

    ``cafe_category_ids`` is the configured set of Loyverse cafe category
    UUIDs (ADR-0009). Defaults to empty — every item bar — matching the
    observable behaviour of the slice-02 placeholder bug, so a deployment
    that hasn't configured the real UUIDs yet stays correct rather than
    silently mis-tagging half the menu.

    ``payment_channels`` (issue #147) routes receipt payments to channels for
    the IN-01 derivation. When given, the orchestrator also records receipt
    facts; ``None`` skips the facts path entirely (the derivation isn't
    configured — its data is simply absent until the venue provides its
    payment-type UUIDs). The **empty** map is different: it is given, routes
    nothing, and makes every receipt with a payment a hard parse error —
    correct for a deployment that wants the derivation but hasn't mapped any
    ids yet.

    A Loyverse HTTP failure (auth, transport, other API error) or a receipt
    parse failure (unmapped payment type, payments≠total, unknown receipt
    type) is caught and surfaced as a readable entry in ``SyncResult.errors``
    rather than raising; the route renders that string verbatim and the
    script prints it. Either way the app stays up — a 9:01am recovery must
    not crash the page.
    """
    today_date = today or date.today()
    first_sync = len(store.sales()) == 0
    since = today_date - timedelta(days=backfill_days) if first_sync else None

    sales_before = len(store.sales())
    changes_before = len(store.menu_change_history())

    client = LoyverseHttpClient(credentials, urlopen=urlopen)
    orchestrator = SyncOrchestrator(
        client=client,
        store=store,
        cafe_category_ids=cafe_category_ids,
        payment_channels=payment_channels,
    )
    try:
        orchestrator.sync_sales_and_menu(since=since)
    except (LoyverseApiError, LoyverseParseError) as exc:
        return SyncResult(
            rows_ingested=0,
            menu_changes=0,
            errors=(str(exc),),
        )

    rows_ingested = len(store.sales()) - sales_before
    menu_changes = len(store.menu_change_history()) - changes_before
    return SyncResult(
        rows_ingested=rows_ingested,
        menu_changes=menu_changes,
        errors=(),
    )


def _iso_z(day: date) -> str:
    """Render ``day`` as a Loyverse ``created_at_min`` filter value.

    Loyverse expects an ISO-8601 timestamp with a trailing ``Z``; midnight UTC
    on the requested day makes the filter inclusive of that whole day.
    """
    return f"{day.isoformat()}T00:00:00.000Z"


__all__ = [
    "BACKFILL_DAYS",
    "SyncOrchestrator",
    "SyncResult",
    "run_sync",
]
