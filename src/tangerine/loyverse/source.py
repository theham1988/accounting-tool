"""Adapter from a ``LoyverseStore`` to the pipeline's ``Source`` protocol.

This is how slice 02 plugs back into the slice-01 margin engine without
touching it: ``StoreSource`` implements ``ingestion.Source`` (``sales()``,
``recipes()``, ``cost_book()``, ``mappings()``) backed by the synced store.
Recipes come from slice 04; until then ``recipes()`` returns whatever the
caller wires in (empty by default), so any sold item the recipes don't cover
surfaces as unmapped (PRD user story 12).

Wave 1.5 Step 1 (ADR-0003 decision 1) adds an optional ``config`` parameter:
when given a :class:`~tangerine.storage.config_store.SqliteConfigStore`,
``recipes()`` / ``cost_book()`` / ``mappings()`` read live from SQLite on
every call instead of returning the fixed lists captured at construction.
The in-memory path (``recipes=``/``cost=``/``mappings=``) stays supported
unchanged for existing callers (tests, the CLI) that have no SQLite config
store to wire in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..cost import CostBook
from ..recipes import RecipeCatalog
from ..types import Recipe, Sale, SkuMapping
from .store import LoyverseStore

if TYPE_CHECKING:
    from ..storage.config_store import SqliteConfigStore


class StoreSource:
    """``ingestion.Source`` view over a ``LoyverseStore``.

    ``recipes`` is the recipe set the margin engine maps sales onto. Slice 02
    ships none; slice 04 supplies them. ``mappings`` is the Loyverse-item ->
    SKU mapping list (also slice 04) — without it a sold item only resolves
    to a recipe when its raw Loyverse identity happens to equal a recipe's
    ``sku_id`` directly, which is never true for real menu data (see
    ``config/recipes.yaml``'s mappings section). Unmapped sold items are
    surfaced via ``unmapped_sold_item_ids``.

    ``cost`` is the cost book the margin engine looks ingredient prices up
    in. Real callers build it from the ``ApprovalBook``; tests seed it.

    ``config``, when given, takes over ``recipes()`` / ``cost_book()`` /
    ``mappings()`` entirely — the ``recipes``/``cost``/``mappings`` arguments
    are ignored in that case (Wave 1.5 Step 1, ADR-0003 decision 1).
    """

    def __init__(
        self,
        store: LoyverseStore,
        recipes: list[Recipe] | None = None,
        cost: CostBook | None = None,
        mappings: list[SkuMapping] | None = None,
        config: "SqliteConfigStore | None" = None,
    ) -> None:
        self._store = store
        self._config = config
        self._recipes = list(recipes or [])
        self._cost = cost if cost is not None else CostBook()
        self._mappings = list(mappings or [])

    def sales(self) -> list[Sale]:
        return self._store.sales()

    def recipes(self) -> list[Recipe]:
        if self._config is not None:
            return self._config.recipes()
        return list(self._recipes)

    def cost_book(self) -> CostBook:
        if self._config is not None:
            return self._config.cost_book()
        return self._cost

    def mappings(self) -> list[SkuMapping]:
        if self._config is not None:
            return self._config.mappings()
        return list(self._mappings)

    def unmapped_sold_item_ids(self) -> tuple[str, ...]:
        """Item ids that were sold but have no recipe, sorted and de-duped.

        Per PRD user story 12 these must be visible immediately. Resolution
        goes through the same item -> SKU -> recipe path the margin engine
        uses (``RecipeCatalog.for_item``), so this agrees with the daily
        review's own "needs attention" list rather than checking a sold
        item's raw Loyverse identity against a recipe's ``sku_id`` directly
        (which is only ever true by coincidence in seeded fixtures).
        """
        catalog = RecipeCatalog(self.recipes(), self.mappings())
        sold_ids = {s.item_id for s in self._store.sales()}
        return tuple(
            sorted(
                item_id
                for item_id in sold_ids
                if catalog.for_item(item_id) is None
            )
        )
