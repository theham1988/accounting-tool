"""Adapter from a ``LoyverseStore`` to the pipeline's ``Source`` protocol.

This is how slice 02 plugs back into the slice-01 margin engine without
touching it: ``StoreSource`` implements ``ingestion.Source`` (``sales()`` and
``recipes()``) backed by the synced store. Recipes come from slice 04; until
then ``recipes()`` returns whatever the caller wires in (empty by default), so
any sold item the recipes don't cover surfaces as unmapped (PRD user story 12).
"""

from __future__ import annotations

from ..types import Recipe, Sale
from .store import LoyverseStore


class StoreSource:
    """``ingestion.Source`` view over a ``LoyverseStore``.

    ``recipes`` is the recipe set the margin engine maps sales onto. Slice 02
    ships none; slice 04 supplies them. Unmapped sold items are surfaced via
    ``unmapped_sold_item_ids``.
    """

    def __init__(
        self,
        store: LoyverseStore,
        recipes: list[Recipe] | None = None,
    ) -> None:
        self._store = store
        self._recipes = list(recipes or [])

    def sales(self) -> list[Sale]:
        return self._store.sales()

    def recipes(self) -> list[Recipe]:
        return list(self._recipes)

    def unmapped_sold_item_ids(self) -> tuple[str, ...]:
        """Item ids that were sold but have no recipe, sorted and de-duped.

        Per PRD user story 12 these must be visible immediately. Recipes are
        slice 04, so against real Loyverse data this is non-empty until the
        partner maps items to recipes.
        """
        mapped = {r.item_id for r in self._recipes}
        sold = {s.item_id for s in self._store.sales()}
        return tuple(sorted(sold - mapped))
