"""Cost engine (slice 04).

Resolves the current cost per unit for a SKU from the latest approved
purchase prices held in the ``ApprovalBook`` (populated by slice 03).

A recipe carries only ``sku_id`` + ``quantity`` for each input — it is a
formula, not a procurement decision. The cost of producing one unit of a
recipe is therefore looked up at margin time, so a re-pricing after the
next receipt approval flows straight into tomorrow's margin without the
recipe having to change.

The ``ApprovalBook`` keys prices by ``(sku_id, supplier_id)`` because the
same SKU can be bought from more than one supplier. The cost engine is
supplier-agnostic: it picks the most-recently-updated price for a SKU
across all suppliers. Supplier choice is a procurement concern; costing
cares about what the unit most recently cost the business.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .approvals import ApprovalBook
from .types import Money


@dataclass(frozen=True)
class SkuPrice:
    """The current per-unit price for a SKU and the date it was set."""

    price: Money
    updated_at: date


class CostBook:
    """SKU -> current per-unit cost, supplier-agnostic.

    Built from an ``ApprovalBook`` snapshot (the latest approved price per
    ``(sku, supplier)`` pair) by keeping the most-recently-updated entry per
    SKU. Tests and seeded fixtures may also construct one directly from a
    ``{sku_id: (price, updated_at)}`` mapping.
    """

    def __init__(
        self,
        prices: dict[str, tuple[Decimal, date]] | None = None,
    ) -> None:
        self._prices: dict[str, SkuPrice] = {}
        for sku_id, (price, updated_at) in (prices or {}).items():
            self._prices[sku_id] = SkuPrice(price=Money(price), updated_at=updated_at)

    @classmethod
    def from_book(cls, book: ApprovalBook) -> CostBook:
        """Build a cost book from an approval book's price table.

        For each SKU, the entry with the latest ``updated_at`` wins; ties are
        broken by taking the last-seen supplier's price (deterministic given
        the book's dict iteration order is insertion order on approval).
        """
        latest: dict[str, SkuPrice] = {}
        for sku_id, supplier_id in book.price_keys():
            entry = book.last_known_price(sku_id, supplier_id)
            if entry is None:
                continue
            price, updated_at = entry
            current = latest.get(sku_id)
            if current is None or updated_at >= current.updated_at:
                latest[sku_id] = SkuPrice(price=Money(price), updated_at=updated_at)
        obj = cls()
        obj._prices = latest
        return obj

    def price(self, sku_id: str) -> SkuPrice | None:
        """The current price entry for a SKU, or None if it has none yet."""
        return self._prices.get(sku_id)


def cost_per_unit(book: CostBook, sku_id: str) -> Money:
    """Current cost per unit for ``sku_id``, or zero when unknown.

    Returns ``Decimal("0")`` for an unknown SKU rather than raising. The
    margin engine checks ``CostBook.price`` separately to flag recipes that
    reference an unpriced SKU (their margin is meaningless); this helper is
    for the costing arithmetic once a price is known.
    """
    entry = book.price(sku_id)
    if entry is None:
        return Money("0")
    return entry.price
