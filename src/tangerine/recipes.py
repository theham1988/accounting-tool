"""Recipe catalog (slice 04).

A ``RecipeCatalog`` is the lookup the margin engine uses to resolve a sold
Loyverse item id to its ``Recipe``. Per the PRD / issue 04 the resolution is
two-step: a Loyverse item maps to a master **SKU** via a ``SkuMapping``, and
the SKU has exactly one ``Recipe``. This keeps the recipe (a formula keyed by
the SKU it produces) decoupled from Loyverse menu identity, so two menu items
can share one SKU/recipe without redefinition.

A sold item with no SKU mapping resolves to ``None``; the margin engine
reports that as a flagged row (PRD user story 12) excluded from the day's
margin totals, so one unmapped item neither aborts the run nor silently
inflates profitability.
"""

from __future__ import annotations

from .types import Recipe, SkuMapping


class RecipeCatalog:
    """Recipes keyed by SKU, with Loyverse-item -> SKU mappings."""

    def __init__(
        self,
        recipes: list[Recipe],
        mappings: list[SkuMapping] | None = None,
    ) -> None:
        self._by_sku: dict[str, Recipe] = {r.sku_id: r for r in recipes}
        self._item_to_sku: dict[str, str] = {
            m.item_id: m.sku_id for m in (mappings or [])
        }

    def sku_for_item(self, item_id: str) -> str | None:
        """The master SKU a Loyverse item maps to, or None if unmapped."""
        return self._item_to_sku.get(item_id)

    def recipe_for_sku(self, sku_id: str) -> Recipe | None:
        """The recipe that produces a SKU, or None if no recipe is defined."""
        return self._by_sku.get(sku_id)

    def for_item(self, item_id: str) -> Recipe | None:
        """Resolve a sold Loyverse item id to its recipe, or None if unmapped.

        Resolution: item -> SKU (via SkuMapping) -> recipe (keyed by SKU).
        Falls back to treating the item id itself as a SKU when no mapping
        exists but a recipe is defined for that id — the common seeded-fixture
        case where the Loyverse item id and the SKU coincide.
        """
        sku_id = self._item_to_sku.get(item_id, item_id)
        return self._by_sku.get(sku_id)

    def all(self) -> tuple[Recipe, ...]:
        """All recipes in the catalog, for introspection."""
        return tuple(self._by_sku.values())
