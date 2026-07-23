"""Loyverse sync storage (slice 02).

A ``LoyverseStore`` holds the sales and menu history the sync writes and the
pipeline reads. The protocol is the seam a future relational backend
implements; ``InMemoryLoyverseStore`` is the in-process implementation used by
tests and by ``python -m tangerine``.

Stored sales carry their Loyverse transaction timestamp (the PRD requirement:
"sales are stored with their Loyverse transaction timestamp"). Menu state is
kept as a timestamped history so a price change between two syncs is auditable
— per the PRD, margins computed between a menu change and the next sync are
accepted as stale, not silently overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from ..types import Money, Sale, Segment


@dataclass(frozen=True)
class MenuItem:
    """One sellable Loyverse variant as of the most recent menu snapshot.

    ``item_id`` is the variant's SKU (falling back to the Loyverse item id
    when a variant has none) — the same identity a receipt line carries and
    recipe mappings key on, so menu rows join to sales and mappings. A
    multi-variant Loyverse item yields one ``MenuItem`` per variant.
    ``sell_price`` is that variant's price at sync time. ``segment`` is
    resolved by the parser from the item's category: cafe when the category
    id is in the configured cafe set, else bar (ADR-0009).
    """

    item_id: str
    name: str
    sell_price: Money
    segment: Segment


@dataclass(frozen=True)
class SaleRecord:
    """A ``Sale`` plus its Loyverse identity for idempotent storage.

    Loyverse receipt line items are uniquely identified by
    ``(receipt_number, line_id)``. We dedupe on that key so that replaying a
    sync (or re-fetching an overlapping page range) never double-counts a sale
    — even when two genuinely different sales of the same SKU happen on the
    same day at the same price and quantity (which a value-based key would
    wrongly collapse).

    ``created_at_utc`` is the raw Loyverse ``created_at`` (UTC) the record was
    parsed from. Stored alongside the derived ``Sale`` (issue #66) so a future
    fix can re-derive ``Sale.timestamp`` and ``Sale.segment`` from it without
    re-fetching the receipt from Loyverse. ``None`` only on records built
    directly by tests; production syncs always populate it.
    """

    sale: Sale
    receipt_number: str
    line_id: str
    created_at_utc: datetime | None = None

    @property
    def source_ref(self) -> tuple[str, str]:
        return (self.receipt_number, self.line_id)


class MenuChangeKind(str, Enum):
    """How an item changed between two consecutive menu snapshots."""

    ADDED = "added"
    PRICE_CHANGE = "price_change"
    RENAMED = "renamed"
    DISCONTINUED = "discontinued"


@dataclass(frozen=True)
class MenuChange:
    item_id: str
    change_kind: MenuChangeKind
    at: datetime
    from_value: str | None
    to_value: str | None


class LoyverseStore(Protocol):
    """Read+write storage for synced sales and menu history."""

    def record_sales(self, records: list[SaleRecord]) -> None:
        """Persist sales. Idempotent on each record's ``source_ref``."""
        ...

    def record_menu_snapshot(
        self, snapshot: "MenuSnapshot", at: datetime
    ) -> None:
        """Record a menu snapshot, diffing against the previous one."""
        ...

    def sales(self) -> list[Sale]:
        ...

    def current_menu(self) -> dict[str, MenuItem]:
        ...

    def menu_change_history(self) -> tuple[MenuChange, ...]:
        ...


@dataclass(frozen=True)
class MenuSnapshot:
    """The menu as seen at one sync point.

    A tuple of ``MenuItem``s, ordered by ``item_id`` for deterministic diffs.
    Built by ``parser.parse_items_snapshot``.
    """

    items: tuple[MenuItem, ...]


def diff_menu(
    previous: dict[str, MenuItem],
    incoming: dict[str, MenuItem],
    at: datetime,
) -> list[MenuChange]:
    """Diff two menu snapshots into a list of ``MenuChange`` records.

    The single source of truth for the four change kinds (ADDED,
    PRICE_CHANGE, RENAMED, DISCONTINUED). Both the in-memory and the SQLite
    store call this so their histories are byte-identical for the same inputs
    — a divergence here would silently produce different audit trails.

    Order: incoming items first (ADDED / PRICE_CHANGE / RENAMED in dict
    iteration order), then discontinuations (in ``previous`` iteration order).
    Callers that need deterministic ordering across runs should iterate over
    sorted keys; the in-memory store preserves insertion order, which is
    deterministic given ``parse_items_snapshot`` sorts by ``item_id``.
    """
    changes: list[MenuChange] = []
    for item_id, new in incoming.items():
        old = previous.get(item_id)
        if old is None:
            changes.append(
                MenuChange(item_id, MenuChangeKind.ADDED, at, None, new.name)
            )
            continue
        if new.sell_price != old.sell_price:
            changes.append(
                MenuChange(
                    item_id,
                    MenuChangeKind.PRICE_CHANGE,
                    at,
                    str(old.sell_price),
                    str(new.sell_price),
                )
            )
        if new.name != old.name:
            changes.append(
                MenuChange(
                    item_id,
                    MenuChangeKind.RENAMED,
                    at,
                    old.name,
                    new.name,
                )
            )
    # Items present before but absent now are discontinuations (issue 02
    # lists discontinuations as a menu change to preserve and timestamp).
    for item_id in previous.keys() - incoming.keys():
        old = previous[item_id]
        changes.append(
            MenuChange(item_id, MenuChangeKind.DISCONTINUED, at, old.name, None)
        )
    return changes


#: Loyverse category ids that map to the cafe segment. The venue has one cafe
#: and one bar category today; Loyverse allows sub-categories, so the shape is
#: a set. The ids are opaque UUIDs unique to this venue's Loyverse account, so
#: they cannot ship in the repo — the production set is configured at runtime
#: via ``LOYVERSE_CAFE_CATEGORY_IDS`` (env, comma-separated) and passed to
#: :func:`tangerine.loyverse.parser.parse_items_snapshot` by the sync. The
#: default empty set makes every item bar — the honest restatement of the
#: slice-02 placeholder bug (``CAFE_CATEGORY_ID = "cat-cafe"`` never matched a
#: real UUID, so every item was bar *de facto*). With the real cafe UUIDs
#: configured, items in that category carry the cafe segment; everything else
#: is bar by construction (two segments, no third). See ADR-0009.
DEFAULT_CAFE_CATEGORY_IDS: frozenset[str] = frozenset()


class InMemoryLoyverseStore:
    """In-process implementation of ``LoyverseStore``."""

    def __init__(self) -> None:
        self._sales: list[Sale] = []
        self._seen_refs: set[tuple[str, str]] = set()
        self._menu: dict[str, MenuItem] = {}
        self._history: list[MenuChange] = []

    def record_sales(self, records: list[SaleRecord]) -> None:
        for rec in records:
            ref = rec.source_ref
            if ref in self._seen_refs:
                continue
            self._seen_refs.add(ref)
            self._sales.append(rec.sale)

    def record_menu_snapshot(self, snapshot: MenuSnapshot, at: datetime) -> None:
        incoming = {mi.item_id: mi for mi in snapshot.items}
        self._history.extend(diff_menu(self._menu, incoming, at))
        self._menu = incoming

    def sales(self) -> list[Sale]:
        return list(self._sales)

    def current_menu(self) -> dict[str, MenuItem]:
        return dict(self._menu)

    def menu_change_history(self) -> tuple[MenuChange, ...]:
        return tuple(self._history)
