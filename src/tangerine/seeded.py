"""Seeded in-repo data source.

Used by slice 01's E2E test and by the `python -m tangerine` runner. Later
slices replace this with real integrations against the same `Source` protocol.

Slice 04 added an optional ``cost`` argument: the margin engine resolves
recipe ingredient costs from a ``CostBook`` rather than from prices baked
into recipes. Tests and the runner seed the cost book directly; real sources
build it from the ``ApprovalBook`` via ``CostBook.from_book``.
"""

from __future__ import annotations

from .cost import CostBook
from .types import Recipe, Sale


class SeededSource:
    """In-memory source built from explicit sales, recipes, and cost book."""

    def __init__(
        self,
        sales: list[Sale],
        recipes: list[Recipe],
        cost: CostBook | None = None,
    ) -> None:
        self._sales = list(sales)
        self._recipes = list(recipes)
        self._cost = cost if cost is not None else CostBook()

    def sales(self) -> list[Sale]:
        return list(self._sales)

    def recipes(self) -> list[Recipe]:
        return list(self._recipes)

    def cost_book(self) -> CostBook:
        return self._cost
