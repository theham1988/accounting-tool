"""Keg inventory via weekly weighing -> beer yield -> accrual COGS (slice 05).

Pure functions over inputs (no I/O, no mutation). The weekly keg weigh turns a
physical measurement into a beer-volume number, which is the periodic-inventory
input that makes accrual COGS work (consumed volume x current cost per ml).
This slice produces the number; slice 08 wires it into the monthly P&L.

Per docs/issues/05-keg-inventory-weekly-weighing.md and the agreed scope:

    volume_consumed = beginning_volume - ending_volume
    beginning_volume = (beginning_gross - tare) / density
    ending_volume    = (ending_gross    - tare) / density
    accrual_cogs     = volume_consumed * current cost per ml  (CostBook lookup)

A period runs from one weigh-in to the next for the same brand; the first weigh
has no prior, so its brand is reported as ``unstarted`` (its volume seeds the
next period's beginning inventory).

Actual yield (Loyverse rung-up beer ml) vs theoretical yield (consumed volume)
gives the loss %; the variance is surfaced but not attributed to individual
kegs (PRD out of scope: "per-keg yield tracking").

The cost-per-ml lookup reuses the slice-04 ``CostBook`` (supplier-agnostic,
latest approved price). Density defaults to water (1.0 g/ml) with a documented
~0.5-1.5% volume tolerance surfaced on the report rather than silently
absorbed.

Rung-up pours are resolved through the existing slice-04 ``RecipeCatalog``: a
sold item maps to a recipe, and the recipe ingredient whose ``sku_id`` matches
the brand's ``beer_sku_id`` contributes its ml to that brand's rung-up total.
This reuses the slice-04 vocabulary rather than introducing a parallel mapping.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .cost import CostBook, cost_per_unit
from .recipes import RecipeCatalog
from .types import (
    DENSITY_TOLERANCE_NOTE,
    KegBrand,
    KegInventoryReport,
    KegInventoryRow,
    KegWeighIn,
    Money,
    Sale,
)


def beer_volume_ml(brand: KegBrand, weigh: KegWeighIn) -> Decimal:
    """Beer volume (ml) for one brand at one weigh-in.

    ``(gross_weight_g - tare_weight_g) / density_g_per_ml``. The conversion is
    physical; density defaults to water (1.0 g/ml) with a documented tolerance
    surfaced on the report rather than silently absorbed. A gross weight at or
    below tare yields zero (never negative) — an empty or under-recorded keg is
    treated as empty, not as a negative volume.
    """
    net_g = weigh.gross_weight_g - brand.tare_weight_g
    if net_g <= 0:
        return Decimal("0")
    return net_g / brand.density_g_per_ml


def rung_up_pours_ml(
    *, sales: list[Sale], recipes: RecipeCatalog, brand: KegBrand
) -> Decimal:
    """Total Loyverse rung-up beer ml for a brand across the given sales.

    Each sold item resolves to a recipe via the catalog (slice-04 resolution:
    item -> SKU -> recipe). The recipe ingredient whose ``sku_id`` matches the
    brand's ``beer_sku_id`` contributes its quantity (in ml) times the units
    sold. Sales whose recipe references no ingredient for this brand contribute
    nothing (they are either a different brand or a non-beer item).
    """
    total = Decimal("0")
    for sale in sales:
        recipe = recipes.for_item(sale.item_id)
        if recipe is None:
            continue
        for ing in recipe.ingredients:
            if ing.sku_id == brand.beer_sku_id:
                total += ing.quantity * sale.quantity
    return total


def compute_keg_inventory(
    *,
    brands: list[KegBrand],
    weigh_ins: list[KegWeighIn],
    sales: list[Sale],
    recipes: RecipeCatalog,
    cost: CostBook,
    period_end: date,
) -> KegInventoryReport:
    """Compute the weekly keg inventory result for the period ending ``period_end``.

    For each brand, the period is bounded by the two most recent weigh-ins on
    or before ``period_end``: the earlier one's volume is the beginning
    inventory, the later one's volume is the ending inventory. The beer
    consumed over the period is ``beginning - ending``; its accrual COGS is
    consumed volume x the brand's current cost per ml (CostBook lookup,
    supplier-agnostic per slice 04). Rung-up beer ml (resolved from sales via
    the recipe catalog) gives the actual yield, against which the theoretical
    yield (consumed volume) gives the loss %.

    Brands whose only weigh on or before ``period_end`` is the very first one
    appear in ``unstarted_brand_ids`` — their first volume becomes the next
    period's beginning inventory, so this period has no consumed volume to
    cost. Brands with no weigh-in at all on or before ``period_end`` are
    likewise unstarted.
    """
    by_brand: dict[str, list[KegWeighIn]] = {}
    for w in weigh_ins:
        by_brand.setdefault(w.brand_id, []).append(w)

    rows: list[KegInventoryRow] = []
    unstarted: list[str] = []
    for brand in brands:
        brand_weighs = sorted(
            (w for w in by_brand.get(brand.brand_id, []) if w.weighed_on <= period_end),
            key=lambda w: w.weighed_on,
        )
        if len(brand_weighs) < 2:
            unstarted.append(brand.brand_id)
            continue

        beginning = brand_weighs[-2]
        ending = brand_weighs[-1]
        beginning_vol = beer_volume_ml(brand, beginning)
        ending_vol = beer_volume_ml(brand, ending)
        consumed = beginning_vol - ending_vol
        # A negative consumed volume (ending weigh heavier than beginning) is
        # a mid-period refill without a separate weigh. Per the issue, variance
        # is surfaced rather than attributed to individual kegs, so the
        # negative consumption, its negative accrual COGS, and its negative
        # loss % are all reported as-is — the same treatment as an over-pour.
        # Slice 08 will reconcile purchases against this signal.

        rung_up = rung_up_pours_ml(sales=sales, recipes=recipes, brand=brand)
        per_ml = cost_per_unit(cost, brand.beer_sku_id)
        accrual_cogs = Money(consumed * per_ml)

        loss_pct = _loss_pct(rung_up, consumed)
        rows.append(
            KegInventoryRow(
                brand_id=brand.brand_id,
                name=brand.name,
                beginning_weighed_on=beginning.weighed_on,
                ending_weighed_on=ending.weighed_on,
                beginning_volume_ml=beginning_vol,
                ending_volume_ml=ending_vol,
                volume_consumed_ml=consumed,
                rung_up_pours_ml=rung_up,
                accrual_cogs=accrual_cogs,
                theoretical_yield_ml=consumed,
                loss_pct=loss_pct,
                density_g_per_ml=brand.density_g_per_ml,
                density_tolerance_note=DENSITY_TOLERANCE_NOTE,
            )
        )

    rows.sort(key=lambda r: r.brand_id)
    unstarted.sort()
    return KegInventoryReport(
        period_start=min((r.beginning_weighed_on for r in rows), default=period_end),
        period_end=period_end,
        rows=tuple(rows),
        unstarted_brand_ids=tuple(unstarted),
        total_accrual_cogs=sum((r.accrual_cogs for r in rows), Money("0")),
    )


def _loss_pct(rung_up_ml: Decimal, consumed_ml: Decimal) -> Decimal | None:
    """Loss % for a brand's period, or None when the ratio is meaningless.

    ``1 - rung_up / consumed``. None when consumed is zero (a brand that sold
    nothing, or was weighed identically): surfacing 0% would imply perfect
    yield, and the ratio is undefined anyway. Negative results (over-pour /
    under-weigh) are surfaced as-is.
    """
    if consumed_ml == 0:
        return None
    return Decimal("1") - (rung_up_ml / consumed_ml)
