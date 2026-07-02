"""Seeded in-repo data source.

Used by slice 01's E2E test and by the `python -m tangerine` runner. Later
slices replace this with real integrations against the same `Source` protocol.

Slice 04 added an optional ``cost`` argument: the margin engine resolves
recipe ingredient costs from a ``CostBook`` rather than from prices baked
into recipes. Tests and the runner seed the cost book directly; real sources
build it from the ``ApprovalBook`` via ``CostBook.from_book``.
"""

from __future__ import annotations

from datetime import date

from .cost import CostBook
from .types import Recipe, Sale, SkuMapping


class SeededSource:
    """In-memory source built from explicit sales, recipes, and cost book.

    ``mappings`` is optional (defaults to none) — most seeded fixtures sell
    items whose id already equals a recipe's ``sku_id`` directly, so no
    mapping is needed; pass one explicitly to exercise the item -> SKU ->
    recipe indirection.
    """

    def __init__(
        self,
        sales: list[Sale],
        recipes: list[Recipe],
        cost: CostBook | None = None,
        mappings: list[SkuMapping] | None = None,
    ) -> None:
        self._sales = list(sales)
        self._recipes = list(recipes)
        self._cost = cost if cost is not None else CostBook()
        self._mappings = list(mappings or [])

    def sales(self) -> list[Sale]:
        return list(self._sales)

    def recipes(self) -> list[Recipe]:
        return list(self._recipes)

    def cost_book(self) -> CostBook:
        return self._cost

    def cost_book_as_of(self, day: date) -> CostBook:
        """The seeded book, whatever the day — a seeded source has no history."""
        return self._cost

    def mappings(self) -> list[SkuMapping]:
        return list(self._mappings)
