"""Ingestion boundary.

Real integrations (Loyverse API, receipt OCR, keg weighs) plug in here. For
slice 01 the only implementation is a seeded in-repo source, but the boundary
is explicit so later slices swap in real sources without touching the margin
engine or the pipeline.

Slice 04 added ``cost_book()``: the margin engine now resolves each recipe
ingredient's current cost from a ``CostBook`` rather than from a price baked
into the recipe. Concrete sources construct their cost book from the
``ApprovalBook`` (``CostBook.from_book``) or seed it directly.

Wave 2 slice 1 (ADR-0004 decision 2) added ``cost_book_as_of(day)``: the
margin engine costs each day's sales at the prices in effect on that day,
so a cost edit no longer re-states history. Sources with no price history
(seeded fixtures) return their one fixed book for every day — for them the
current book *is* all of history.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from .cost import CostBook
from .types import Recipe, Sale, SkuMapping


class Source(Protocol):
    """Read-side boundary for the pipeline.

    A source yields the sales, recipes, cost book, and Loyverse-item -> SKU
    mappings the margin engine consumes. Concrete sources (seeded fixtures
    today; Loyverse + receipt processor later) satisfy this protocol.

    ``mappings()`` was added alongside the recipes.yaml SKU re-key: the
    margin engine resolves a sold item to its recipe via ``RecipeCatalog``
    (item -> SKU -> recipe), and that resolution is meaningless without the
    mappings a Source's mapping-carrying recipe catalog was built from.
    """

    def sales(self) -> list[Sale]: ...

    def recipes(self) -> list[Recipe]: ...

    def cost_book(self) -> CostBook: ...

    def cost_book_as_of(self, day: date) -> CostBook: ...

    def mappings(self) -> list[SkuMapping]: ...
