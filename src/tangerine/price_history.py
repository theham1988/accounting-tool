"""Price-as-of-date lookup (Wave 2 slice 1, ADR-0004 decision 2).

Answers "what was this SKU's net per-unit price on date D" so every
reporting surface can cost a sale at the price in effect on the sale's
date, not the price at render time. The history is reconstructed from the
audit log — every cost edit already snapshots the row's old/new
``price_per_unit_net`` — so no new capture and no new table is needed.
Pre-cutover sales (no audit history) use the seed price; a SKU never
edited uses its current price.

This module is pure engine: it consumes plain ``PriceChange`` records and
a current ``CostBook``. Parsing audit-log rows into ``PriceChange``s is
the storage layer's job (``SqliteConfigStore.price_history``), keeping the
engine free of storage imports like the rest of the margin pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .cost import CostBook
from .types import Money


@dataclass(frozen=True)
class PriceChange:
    """One cost edit's effect on a SKU's net per-unit price.

    Distilled from an audit-log entry for the ``costs`` table:
    ``old_price`` is ``None`` when the edit created the cost row (the SKU
    had no price before it); ``new_price`` is ``None`` when the edit
    removed the row (a reverted creation).
    """

    sku_id: str
    changed_on: date
    old_price: Money | None
    new_price: Money | None


class PriceHistory:
    """SKU -> net per-unit price on any past date.

    Built from the current cost book plus the chronological list of price
    changes the audit log records. A SKU with no recorded changes costs at
    its current (seed) price on every date — the pre-cutover fallback.
    """

    def __init__(
        self,
        *,
        current: CostBook,
        changes: list[PriceChange],
    ) -> None:
        self._current = current
        self._changes_by_sku: dict[str, list[PriceChange]] = {}
        # Sort is stable, so same-day changes keep their recorded (audit
        # entry) order and the last write of a day wins, as it should.
        for change in sorted(changes, key=lambda c: c.changed_on):
            self._changes_by_sku.setdefault(change.sku_id, []).append(change)

    def price_as_of(self, sku_id: str, on_date: date) -> Money | None:
        """The SKU's net per-unit price in effect on ``on_date``.

        The latest recorded change on or before ``on_date`` governs; a
        change made on a day applies to that day's sales (the partner
        repriced that morning). A date before every recorded change takes
        the first change's before-value — the seed price. A SKU with no
        recorded changes costs at its current (seed) price. ``None`` when
        the SKU had no price on that date.
        """
        changes = self._changes_by_sku.get(sku_id, [])
        governing = self._governing_change(sku_id, on_date)
        if governing is not None:
            return governing.new_price
        if changes:
            return changes[0].old_price
        entry = self._current.price(sku_id)
        if entry is None:
            return None
        return entry.price

    def cost_book_as_of(self, on_date: date) -> CostBook:
        """The whole cost book frozen at ``on_date``.

        One ``price_as_of`` per known SKU (current book plus every SKU the
        change log mentions), shaped as the ``CostBook`` the margin engine
        already consumes — so as-of-date costing is a drop-in for costing
        at current prices. SKUs with no price on that date are simply
        absent, which the margin engine surfaces as unknown-price rows.

        Each entry's ``updated_at`` is the day its governing change landed,
        or the current entry's date for a SKU whose price the change log
        does not touch on or before ``on_date``.
        """
        sku_ids = set(self._current.sku_ids()) | set(self._changes_by_sku)
        prices: dict[str, tuple[Money, date]] = {}
        for sku_id in sku_ids:
            price = self.price_as_of(sku_id, on_date)
            if price is None:
                continue
            prices[sku_id] = (price, self._effective_date(sku_id, on_date))
        return CostBook(prices)

    def _governing_change(self, sku_id: str, on_date: date) -> PriceChange | None:
        """The latest recorded change on or before ``on_date``, if any."""
        governing = None
        for change in self._changes_by_sku.get(sku_id, []):
            if change.changed_on <= on_date:
                governing = change
        return governing

    def _effective_date(self, sku_id: str, on_date: date) -> date:
        """When the price in effect on ``on_date`` was set."""
        governing = self._governing_change(sku_id, on_date)
        if governing is not None:
            return governing.changed_on
        entry = self._current.price(sku_id)
        if entry is not None:
            return entry.updated_at
        return on_date
