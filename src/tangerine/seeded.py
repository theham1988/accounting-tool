"""Seeded in-repo data source.

Used by slice 01's E2E test and by the `python -m tangerine` runner. Later
slices replace this with real integrations against the same `Source` protocol.
"""

from __future__ import annotations

from .types import Recipe, Sale


class SeededSource:
    """In-memory source built from explicit sales and recipes lists."""

    def __init__(self, sales: list[Sale], recipes: list[Recipe]) -> None:
        self._sales = list(sales)
        self._recipes = list(recipes)

    def sales(self) -> list[Sale]:
        return list(self._sales)

    def recipes(self) -> list[Recipe]:
        return list(self._recipes)
