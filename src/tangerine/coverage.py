"""SKU + item coverage engine (Wave 1.5, Slice 2).

Pure functions over the same recipes/costs/mappings shapes ``margin.py``
consumes, plus the current Loyverse menu — no I/O, no mutation. This is the
engine behind the two read-only visibility surfaces ADR-0003 calls for: the
SKU view (``build_sku_coverage``) and the item coverage view
(``build_item_coverage``), which joins each Loyverse item to its resolved
SKU's coverage row so a partner never has to cross-reference two pages by
hand.
"""

from __future__ import annotations

from .cost import CostBook
from .loyverse.store import MenuItem
from .margin import gross_margin_pct, recipe_cost_per_unit
from .types import (
    ItemCoverageRow,
    Money,
    Recipe,
    SkuClassification,
    SkuCoverageRow,
    SkuHealth,
    SkuMapping,
    SkuRecord,
    SkuRole,
)

#: Sort-order weight for each item coverage row's "badness" — unmapped is
#: worse than any known health, so the partner's audit starts there.
_ITEM_SORT_ORDER: dict[SkuHealth | None, int] = {
    None: 0,
    SkuHealth.RED: 1,
    SkuHealth.YELLOW: 2,
    SkuHealth.GREEN: 3,
}


def pickable_ingredient_skus(
    skus: list[SkuRecord], recipes: list[Recipe]
) -> list[SkuRecord]:
    """The SKUs the ingredient picker may honestly offer (issue #35).

    Options = every purchasable SKU (no recipe of its own) plus every prep
    (produced, declared usable inside other recipes). Sold-only dishes are
    absent: picking one as an "ingredient" silently produces a garbage cost,
    which is the mis-click this filter exists to prevent.
    """
    recipes_by_sku = {r.sku_id: r for r in recipes}
    return [
        sku
        for sku in skus
        if sku_role(recipes_by_sku.get(sku.sku_id)) is not SkuRole.PRODUCED
    ]


def sku_role(recipe: Recipe | None) -> SkuRole:
    """A SKU's role, derived from its recipe (issue #35, CONTEXT.md).

    No recipe means bought (purchasable); a recipe means made (produced);
    the recipe's prep flag upgrades produced to prep — the role that also
    makes the SKU a legal ingredient.
    """
    if recipe is None:
        return SkuRole.PURCHASABLE
    return SkuRole.PREP if recipe.prep else SkuRole.PRODUCED


def classify_sku(
    sku_id: str, *, recipes: list[Recipe], mappings: list[SkuMapping]
) -> SkuClassification:
    """Classify ``sku_id`` as ``ACTIVE`` / ``PREP_INTERNAL`` / ``DANGLING``.

    ``ACTIVE`` wins whenever a SKU is both sold (mapped) and consumed by
    another recipe (e.g. a sub-recipe also sold as a standalone cup) — being
    sold is the stronger signal for the partner's workspace.
    """
    if any(m.sku_id == sku_id for m in mappings):
        return SkuClassification.ACTIVE
    if any(
        ing.sku_id == sku_id for recipe in recipes for ing in recipe.ingredients
    ):
        return SkuClassification.PREP_INTERNAL
    return SkuClassification.DANGLING


def sku_health(
    sku_id: str,
    *,
    recipe: Recipe | None,
    cost: CostBook,
    classification: SkuClassification,
) -> SkuHealth:
    """At-a-glance costing health for one SKU.

    A dangling SKU is always red — nothing sells or consumes it, so its
    costing state is moot. Otherwise the check branches on whether the SKU
    has a recipe of its own:

    - A recipe SKU is red when it has zero ingredients (a broken, empty
      recipe); green when every ingredient is priced; yellow otherwise
      (some or none priced — a real recipe just not fully costed yet).
    - A leaf SKU (no recipe of its own — either a raw material, or a sold/
      consumed SKU that should have a recipe but doesn't) is green when it
      has its own direct price, red when it does not.
    """
    if classification is SkuClassification.DANGLING:
        return SkuHealth.RED
    if recipe is not None:
        if not recipe.ingredients:
            return SkuHealth.RED
        priced = sum(1 for ing in recipe.ingredients if cost.price(ing.sku_id) is not None)
        return SkuHealth.GREEN if priced == len(recipe.ingredients) else SkuHealth.YELLOW
    return SkuHealth.GREEN if cost.price(sku_id) is not None else SkuHealth.RED


def build_sku_coverage(
    *,
    skus: list[SkuRecord],
    recipes: list[Recipe],
    mappings: list[SkuMapping],
    cost: CostBook,
) -> list[SkuCoverageRow]:
    """One coverage row per SKU in ``skus``, sorted by ``sku_id``.

    The SKU view's whole table: for each SKU, its classification, health,
    how many Loyverse items map to it, its recipe completeness, and a
    per-unit cost — derived via the same ``recipe_cost_per_unit`` the margin
    engine uses when the recipe is fully priced (green), or read straight
    off the cost book for a priced leaf SKU. ``None`` whenever a cost cannot
    be honestly derived, mirroring the margin engine's own
    exclude-rather-than-guess rule for unpriced ingredients.
    """
    recipes_by_sku = {r.sku_id: r for r in recipes}
    mapped_counts: dict[str, int] = {}
    for m in mappings:
        mapped_counts[m.sku_id] = mapped_counts.get(m.sku_id, 0) + 1

    rows: list[SkuCoverageRow] = []
    for sku in sorted(skus, key=lambda s: s.sku_id):
        recipe = recipes_by_sku.get(sku.sku_id)
        classification = classify_sku(sku.sku_id, recipes=recipes, mappings=mappings)
        health = sku_health(
            sku.sku_id, recipe=recipe, cost=cost, classification=classification
        )
        rows.append(
            SkuCoverageRow(
                sku_id=sku.sku_id,
                name=sku.name,
                segment=sku.segment,
                unit=sku.unit,
                classification=classification,
                role=sku_role(recipe),
                health=health,
                mapped_item_count=mapped_counts.get(sku.sku_id, 0),
                has_recipe=recipe is not None,
                ingredient_count=len(recipe.ingredients) if recipe else 0,
                priced_ingredient_count=(
                    sum(1 for ing in recipe.ingredients if cost.price(ing.sku_id) is not None)
                    if recipe
                    else 0
                ),
                cost_per_unit=_derived_cost_per_unit(
                    sku.sku_id, recipe=recipe, cost=cost, health=health
                ),
            )
        )
    return rows


def _derived_cost_per_unit(
    sku_id: str, *, recipe: Recipe | None, cost: CostBook, health: SkuHealth
) -> Money | None:
    """The per-unit cost to show on a coverage row, or ``None`` if unreliable.

    A recipe SKU's cost is only shown once every ingredient is priced
    (``health`` is green) — a partial cost would understate the true cost
    and mislead the partner. A leaf SKU shows its own direct price, if any.
    """
    if recipe is not None:
        return recipe_cost_per_unit(recipe, cost) if health is SkuHealth.GREEN else None
    entry = cost.price(sku_id)
    return entry.price if entry is not None else None


def build_item_coverage(
    *,
    menu: dict[str, MenuItem],
    skus: list[SkuRecord],
    recipes: list[Recipe],
    mappings: list[SkuMapping],
    cost: CostBook,
) -> list[ItemCoverageRow]:
    """One coverage row per Loyverse item in ``menu``, unmapped/broken first.

    Each item resolves to a SKU the same way the margin engine does
    (``RecipeCatalog.for_item``'s item-equals-sku fallback included, via
    ``_resolve_mapped_sku_id``), then joins that SKU's already-computed
    coverage row (``build_sku_coverage``) for its health and cost — so the
    item coverage view's "chain health" column can never disagree with the
    SKU view's own row for the same SKU.

    Sort order is worst-first (unmapped, then red, then yellow, then green,
    each tier alphabetical by name) per the issue's "unmapped items bubble
    to the top" requirement — extended to cover broken/partial SKUs too, so
    the whole page reads as a prioritised "fix this first" audit list.
    """
    sku_rows_by_id = {
        row.sku_id: row
        for row in build_sku_coverage(skus=skus, recipes=recipes, mappings=mappings, cost=cost)
    }
    mapping_by_item = {m.item_id: m.sku_id for m in mappings}
    recipe_sku_ids = {r.sku_id for r in recipes}

    rows: list[ItemCoverageRow] = []
    for item_id, item in menu.items():
        mapped_sku_id = _resolve_mapped_sku_id(
            item_id, mapping_by_item=mapping_by_item, recipe_sku_ids=recipe_sku_ids
        )
        sku_row = sku_rows_by_id.get(mapped_sku_id) if mapped_sku_id else None
        cost_per_unit = sku_row.cost_per_unit if sku_row else None
        margin = item.sell_price - cost_per_unit if cost_per_unit is not None else None
        rows.append(
            ItemCoverageRow(
                item_id=item_id,
                name=item.name,
                sell_price=item.sell_price,
                mapped_sku_id=mapped_sku_id,
                sku_health=sku_row.health if sku_row else None,
                cost_per_unit=cost_per_unit,
                gross_margin=margin,
                gross_margin_pct=(
                    gross_margin_pct(margin, item.sell_price) if margin is not None else None
                ),
            )
        )
    rows.sort(key=lambda r: (_ITEM_SORT_ORDER[r.sku_health], r.name.lower()))
    return rows


def _resolve_mapped_sku_id(
    item_id: str, *, mapping_by_item: dict[str, str], recipe_sku_ids: set[str]
) -> str | None:
    """The SKU ``item_id`` resolves to, or ``None`` if genuinely unmapped.

    Mirrors ``RecipeCatalog.for_item``: an explicit mapping wins; failing
    that, the item id itself is tried as a SKU id (the seeded-fixture
    convention where a Loyverse item id and its SKU coincide).
    """
    if item_id in mapping_by_item:
        return mapping_by_item[item_id]
    if item_id in recipe_sku_ids:
        return item_id
    return None
