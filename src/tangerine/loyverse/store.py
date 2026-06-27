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
    """One Loyverse item as of the most recent menu snapshot.

    ``sell_price`` is the variant price at sync time (Loyverse stores price on
    the variant, not the item; we read the first variant). ``segment`` is
    resolved by the parser from the item's category (cafe category -> cafe,
    else bar); slice 07 generalises segment tagging.
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
    """

    sale: Sale
    receipt_number: str
    line_id: str

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


# Category id that maps to the cafe segment. Loyverse category ids are opaque;
# the venue has one cafe and one bar category. Slice 02 hard-codes the cafe
# category id; slice 07 (segment tagging) generalises this.
CAFE_CATEGORY_ID = "cat-cafe"


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
        for item_id, new in incoming.items():
            old = self._menu.get(item_id)
            if old is None:
                self._history.append(
                    MenuChange(item_id, MenuChangeKind.ADDED, at, None, new.name)
                )
            else:
                if new.sell_price != old.sell_price:
                    self._history.append(
                        MenuChange(
                            item_id,
                            MenuChangeKind.PRICE_CHANGE,
                            at,
                            str(old.sell_price),
                            str(new.sell_price),
                        )
                    )
                if new.name != old.name:
                    self._history.append(
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
        for item_id in self._menu.keys() - incoming.keys():
            old = self._menu[item_id]
            self._history.append(
                MenuChange(
                    item_id,
                    MenuChangeKind.DISCONTINUED,
                    at,
                    old.name,
                    None,
                )
            )
        self._menu = incoming

    def sales(self) -> list[Sale]:
        return list(self._sales)

    def current_menu(self) -> dict[str, MenuItem]:
        return dict(self._menu)

    def menu_change_history(self) -> tuple[MenuChange, ...]:
        return tuple(self._history)
