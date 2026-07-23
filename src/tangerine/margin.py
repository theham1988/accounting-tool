"""Margin engine.

Given sales, recipes, and a cost book, compute per-item and daily gross
margin. Pure functions over inputs — no I/O, no mutation. The PRD defines:

    gross_margin = revenue - cogs
    cogs(item)   = (sum over recipe ingredients of quantity * unit_cost)
                   / yield_qty

An ingredient's **unit cost** is resolved recursively (issue #36, ADR-0005):

  - a **purchasable** SKU (no recipe) takes its price from the ``CostBook``;
  - a **produced** SKU (has a recipe) is costed as ``sum(ingredient qty ×
    unit_cost) / yield_qty``, recursing through preps down to purchasables.

The resolver is memoised per costing pass and cycle-safe. There is no
leaf-price-wins branch — a produced SKU's cost is *always* derived from its
recipe, never typed directly. A produced SKU with an unpriceable leaf (a
missing price, or a cycle) is itself unpriceable; dishes using it flag
``unknown_price`` exactly as a direct missing price does.

The current unit cost of each purchasable ingredient SKU is looked up from
the ``CostBook`` (which tracks the latest approved purchase price), so a
recipe is a formula and a re-pricing flows straight into margin without the
recipe changing.

Rows whose margin cannot be trusted — unmapped items (no recipe) or items
where an ingredient SKU has no approved price — are flagged and excluded
from the daily totals: their COGS is unknown, so booking their revenue as
margin would over-state profitability. Their revenue is surfaced separately
on the ``DailyMargin`` so it stays visible.

There is one public recipe-cost face: :class:`CostResolver` (and the thin
``unit_cost`` one-shot that wraps it). The Wave 1 slice-04 bare helpers
(``recipe_input_cost`` / ``recipe_cost`` / ``recipe_cost_per_unit`` / the
module-level ``has_unknown_price``) have been retired — they could not
recurse into preps and so silently understated any dish that used one. Any
caller wanting a recipe's cost builds a ``CostResolver`` and calls its
``unit_cost`` / ``cost_per_unit`` / ``has_unknown_price`` (ADR-0005
amendment 2026-07-16).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .cost import CostBook
from .ingestion import Source
from .recipes import RecipeCatalog
from .segments import segment_of_sale
from .types import (
    CostBreakdown,
    CostBreakdownLine,
    DailyMargin,
    ItemMargin,
    Money,
    Recipe,
    Sale,
    Segment,
    SegmentMargin,
)

# Gross margin % is reported to two decimal places (a THB cent of precision
# on a ratio). Items with no revenue, or flagged rows whose ratio is
# meaningless, carry None instead.
_MARGIN_PCT_QUANT = Decimal("0.01")


def unit_cost(
    sku_id: str, *, recipes: RecipeCatalog, cost: CostBook
) -> Decimal | None:
    """A SKU's per-unit cost, recursing into sub-recipes (issue #36, ADR-0005).

    A **purchasable** SKU (no recipe) takes its price from ``cost``. A
    **produced** SKU (has a recipe) is costed as ``sum(ingredient qty ×
    unit_cost) / yield_qty`` — recursing through preps down to purchasables.
    Returns ``None`` when any leaf needed to derive the price is itself
    unpriceable, or when the SKU is on its own resolution stack (defense in
    depth behind the save-time cycle rejection).

    No leaf-price-wins: a produced SKU's cost is *always* derived from its
    recipe. The seed migration removes any pre-existing direct cost rows
    on produced SKUs, and the cost editor rejects them; a stale one
    reaching this function is silently ignored rather than honoured.

    This is the unmemoised one-shot form. For a margin pass over many sales
    of the same dish, build a single :class:`CostResolver` instead — its per-
    SKU memo walks each prep's graph once.
    """
    return CostResolver(recipes, cost).unit_cost(sku_id)


def gross_margin_pct(gross_margin: Money, revenue: Money) -> Decimal | None:
    """Gross margin as a percentage of revenue, to 2 dp. None when no revenue."""
    if revenue == 0:
        return None
    return (gross_margin / revenue * Decimal("100")).quantize(_MARGIN_PCT_QUANT)


class CostResolver:
    """Resolves a SKU's per-unit cost, recursing into sub-recipes.

    Issue #36 (ADR-0005): a produced SKU's cost is *always* derived from its
    recipe, never typed directly. A purchasable SKU takes its price from the
    cost book; a produced SKU's cost is ``sum(ingredient qty × unit_cost) /
    yield_qty``, recursing through preps down to purchasables.

    Per-SKU memo so a sauce used by eight dishes is walked once. Cycle-safe:
    a SKU on its own resolution stack returns ``None`` rather than looping
    (defense in depth behind the save-time cycle rejection). No leaf-price-
    wins branch — a produced SKU with a stale direct cost-book entry is
    costed from its recipe regardless.
    """

    def __init__(self, recipes: RecipeCatalog, cost: CostBook) -> None:
        self._recipes = recipes
        self._cost = cost
        # ``None`` is cached too (still unpriceable): a sauce used by eight
        # dishes with one unpriced leaf is only walked once.
        self._memo: dict[str, Decimal | None] = {}

    def unit_cost(
        self, sku_id: str, seen: frozenset[str] = frozenset()
    ) -> Decimal | None:
        if sku_id in self._memo and not seen:
            return self._memo[sku_id]
        # No leaf-price-wins: a produced SKU's cost comes from its recipe,
        # never a direct price (issue #36). The cost book is consulted only
        # for purchasables — SKUs with no recipe of their own.
        recipe = self._recipes.recipe_for_sku(sku_id)
        if recipe is None:
            entry = self._cost.price(sku_id)
            result = entry.price if entry is not None else None
            if not seen:
                self._memo[sku_id] = result
            return result
        # Cycle: ``sku_id`` is already on the path being resolved. Save-time
        # rejection should prevent this; the engine treats a runtime cycle
        # as unpriceable so costing can never loop (defense in depth).
        if sku_id in seen:
            return None
        if recipe.yield_qty <= 0:
            return None
        total = Decimal("0")
        for ing in recipe.ingredients:
            child = self.unit_cost(ing.sku_id, seen | {sku_id})
            if child is None:
                # An unpriceable ingredient (missing leaf, deeper cycle)
                # makes this SKU unpriceable too — its dishes flag
                # ``unknown_price`` exactly as a direct missing price does.
                if not seen:
                    self._memo[sku_id] = None
                return None
            total += ing.quantity * child
        per_unit = total / recipe.yield_qty
        if not seen:
            self._memo[sku_id] = per_unit
        return per_unit

    def has_unknown_price(self, recipe: Recipe) -> bool:
        """Recursive unknown-price check for one recipe."""
        return any(self.unit_cost(ing.sku_id) is None for ing in recipe.ingredients)

    def cost_per_unit(self, recipe: Recipe) -> Money:
        """Cost of one saleable unit of ``recipe`` via the memoised resolver."""
        total = Decimal("0")
        for ing in recipe.ingredients:
            unit = self.unit_cost(ing.sku_id)
            if unit is None:
                continue
            total += ing.quantity * unit
        return Money(total / recipe.yield_qty)

    def unpriced_leaves(
        self, sku_id: str, seen: frozenset[str] = frozenset()
    ) -> list[str]:
        """The purchasable leaf SKUs reachable from ``sku_id`` that lack a price.

        Walks the recipe tree to the purchasables at the bottom and returns,
        in stable first-seen order, those with no cost-book entry — the
        answer to "which leaf is unpriced" (issue #37). A cycle contributes
        nothing (the runtime guard treats it as unpriceable elsewhere).
        """
        recipe = self._recipes.recipe_for_sku(sku_id)
        if recipe is None:
            return [] if self._cost.price(sku_id) is not None else [sku_id]
        if sku_id in seen:
            return []
        leaves: list[str] = []
        for ing in recipe.ingredients:
            for leaf in self.unpriced_leaves(ing.sku_id, seen | {sku_id}):
                if leaf not in leaves:
                    leaves.append(leaf)
        return leaves


def cost_breakdown(
    sku_id: str, *, recipes: RecipeCatalog, cost: CostBook, name_of: dict[str, str]
) -> CostBreakdown:
    """A produced SKU's read-only derived cost and per-ingredient breakdown.

    One line per direct ingredient (quantity × resolved unit cost), a prep
    shown as a single priced row via its own derived figure — never expanded
    inline. ``per_unit`` is the recursive cost of one output unit (``None``
    when unpriceable); ``unpriced_leaves`` names the leaf SKUs blocking it.
    ``name_of`` maps sku_id → display name for the lines.
    """
    resolver = CostResolver(recipes, cost)
    recipe = recipes.recipe_for_sku(sku_id)
    if recipe is None:
        raise ValueError(f"{sku_id} has no recipe; it is not a produced SKU")
    lines: list[CostBreakdownLine] = []
    for ing in recipe.ingredients:
        unit_cost = resolver.unit_cost(ing.sku_id)
        line_cost = (
            Money(ing.quantity * unit_cost) if unit_cost is not None else None
        )
        lines.append(
            CostBreakdownLine(
                sku_id=ing.sku_id,
                name=name_of.get(ing.sku_id, ing.sku_id),
                quantity=ing.quantity,
                unit_cost=unit_cost,
                line_cost=line_cost,
            )
        )
    per_unit = resolver.unit_cost(sku_id)
    return CostBreakdown(
        sku_id=sku_id,
        per_unit=Money(per_unit) if per_unit is not None else None,
        yield_qty=recipe.yield_qty,
        lines=tuple(lines),
        unpriced_leaves=tuple(resolver.unpriced_leaves(sku_id)),
    )


def compute_item_margins(
    *,
    sales: list[Sale],
    recipes: RecipeCatalog,
    cost: CostBook,
    day: date,
) -> list[ItemMargin]:
    """Per-(item, segment) margin table for a single day.

    Sales on other days are ignored. Each sale is resolved to a recipe via
    the catalog (item -> SKU -> recipe) and to a segment via
    :func:`segment_of_sale` (ADR-0007: pure clock — the sale's shift-stamped
    ``segment``, never the recipe's). Three outcomes per (item, segment):

      - mapped and fully priced     -> normal margin row, included in totals
      - mapped but a SKU unpriced   -> flagged ``unknown_price``, excluded
      - unmapped (no SKU/recipe)    -> flagged ``unmapped``, excluded

    Flagged rows are returned (so the daily review surfaces them) but
    ``excluded_from_totals`` is True on them, so ``compute_daily_margin``
    sums only reliable rows.

    Output is one ``ItemMargin`` per **(item id, segment)** that sold that
    day, sorted by ``(item_id, segment)`` for determinism. An item that
    sells in both the cafe and bar windows on the same day therefore
    produces two rows — one carrying only its cafe-window units/revenue,
    the other only its bar-window units/revenue. This is what makes a
    clock-segment split honest: revenue on each segment card ties to the
    item rows behind it, with no phantom revenue in a card whose items
    never sold there. Pre-#73 this aggregated by ``item_id`` alone and
    let the recipe's segment win, so cross-shift items silently
    mis-split.
    """
    # Key is (item_id, segment): an item selling in both shifts produces
    # two buckets. The first sale seen for a key wins its sell_price
    # (Loyverse sell price is the menu price; intra-day repricing between
    # syncs is accepted as stale per the PRD sync note).
    units: dict[tuple[str, Segment], int] = {}
    revenue: dict[tuple[str, Segment], Money] = {}
    sell_price: dict[tuple[str, Segment], Money] = {}

    for sale in sales:
        if sale.timestamp != day:
            continue
        # ADR-0007: the sale's segment is its clock-stamped segment, not
        # the recipe's. The parser stamps every production sale post-#66;
        # segment_of_sale is the single read of that stamp.
        seg = segment_of_sale(sale)
        key = (sale.item_id, seg)
        units[key] = units.get(key, 0) + sale.quantity
        revenue[key] = (
            revenue.get(key, Money("0")) + sale.sell_price * sale.quantity
        )
        sell_price.setdefault(key, sale.sell_price)

    rows: list[ItemMargin] = []
    resolver = CostResolver(recipes, cost)
    # Sort by (item_id, segment) — item first for the daily review's item
    # table grouping, segment second so the cafe row precedes the bar row
    # of the same item (matches the roll-up's cafe-then-bar order).
    for key in sorted(units, key=lambda k: (k[0], _SEGMENT_ORDER[k[1]])):
        item_id, seg = key
        recipe = recipes.for_item(item_id)
        row_units = units[key]
        row_revenue = revenue[key]
        row_sell_price = sell_price[key]

        if recipe is None:
            rows.append(_flagged_row(
                item_id=item_id,
                name=item_id,
                segment=seg,
                day=day,
                units=row_units,
                sell_price=row_sell_price,
                revenue=row_revenue,
            ))
            continue

        unpriced = resolver.has_unknown_price(recipe)
        if unpriced:
            # Mapped, but at least one ingredient has no approved price —
            # directly or recursively through a prep's recipe. Surface the
            # row (with the recipe's name and the **sale's clock segment**)
            # but exclude it from totals — its COGS is unknown. The recipe's
            # menu-segment is irrelevant for revenue splitting (ADR-0007).
            rows.append(_flagged_row(
                item_id=item_id,
                name=recipe.name,
                segment=seg,
                day=day,
                units=row_units,
                sell_price=row_sell_price,
                revenue=row_revenue,
                unknown_price=True,
            ))
            continue

        cpu = resolver.cost_per_unit(recipe)
        cogs = cpu * row_units
        gm = row_revenue - cogs
        pct = gross_margin_pct(gm, row_revenue)
        below = (
            recipe.target_gross_margin_pct is not None
            and pct is not None
            and pct < recipe.target_gross_margin_pct
        )
        rows.append(
            ItemMargin(
                item_id=item_id,
                name=recipe.name,
                segment=seg,
                day=day,
                units_sold=row_units,
                sell_price=row_sell_price,
                cost_per_unit=cpu,
                revenue=row_revenue,
                cogs=cogs,
                gross_margin=gm,
                gross_margin_pct=pct,
                unmapped=False,
                unknown_price=False,
                below_target=below,
            )
        )
    return rows


def _flagged_row(
    *,
    item_id: str,
    name: str,
    segment: Segment,
    day: date,
    units: int,
    sell_price: Money,
    revenue: Money,
    unknown_price: bool = False,
) -> ItemMargin:
    """Build a flagged margin row (unmapped or unknown-price).

    Flagged rows carry the real revenue (so it can be surfaced) but zero
    COGS and a None margin %: their cost is unknown, so any margin number
    would be misleading. ``excluded_from_totals`` is True on them.

    ``segment`` is the resolved segment for the row: the recipe's segment when
    the row is mapped-but-unpriced, or the shift-fallback segment (from the
    sale) when the row is unmapped. Slice 07 tags every row.
    """
    return ItemMargin(
        item_id=item_id,
        name=name,
        segment=segment,
        day=day,
        units_sold=units,
        sell_price=sell_price,
        cost_per_unit=Money("0"),
        revenue=revenue,
        cogs=Money("0"),
        gross_margin=Money("0"),
        gross_margin_pct=None,
        unmapped=not unknown_price,
        unknown_price=unknown_price,
        below_target=False,
    )


@dataclass(frozen=True)
class DayMargins:
    """One day's per-item margin rows, as produced by ``margins_over_range``.

    A *per-day* slice of a multi-day pass: the item-margin table for a single
    day inside the range, carrying only the per-item rows (not the rolled-up
    totals / flagged revenue / segment CMs those rows aggregate into). The
    daily and period roll-ups both project over a sequence of these slices,
    so a one-day range and ``compute_daily_margin`` agree by construction
    (the daily view is the one-day projection of the range pass).

    ``day`` repeats each row's ``ItemMargin.day``; it is carried here so a
    caller iterating the sequence can read the day off the slice without
    reaching into a row.
    """

    day: date
    item_margins: tuple[ItemMargin, ...]


def margins_over_range(
    source: Source, start: date, end: date
) -> tuple[DayMargins, ...]:
    """Per-day item-margin rows for every day in the inclusive ``[start, end]``.

    The single multi-day pass the reporting surfaces project from. The
    recipe catalog is built **once** (recipes + mappings do not change per
    day); each day is then costed at that day's prices
    (``source.cost_book_as_of(day)``, ADR-0004 decision 2) and run through
    ``compute_item_margins``. One ``DayMargins`` is emitted per day in the
    range, in date order, including quiet days (``item_margins == ()``) so
    the sequence is total over the range — the same rule the period review
    applies to its per-day drilldown rows.

    Rejects ``end < start`` with ``ValueError`` (mirrors ``build_period_review``).
    Does not roll up totals, flagged revenue, or segment CMs — those are a
    projection the daily and period views layer on top of the per-day rows.
    """
    if end < start:
        raise ValueError(
            f"range end {end} precedes start {start}; range must be inclusive"
        )

    sales = source.sales()
    recipes = RecipeCatalog(list(source.recipes()), list(source.mappings()))

    slices: list[DayMargins] = []
    current = start
    while current <= end:
        rows = compute_item_margins(
            sales=sales,
            recipes=recipes,
            cost=source.cost_book_as_of(current),
            day=current,
        )
        slices.append(DayMargins(day=current, item_margins=tuple(rows)))
        current += timedelta(days=1)
    return tuple(slices)


def compute_daily_margin(source: Source, day: date) -> DailyMargin:
    """Compute item-level and rolled-up gross margin for a single day.

    A thin projection over ``margins_over_range(source, day, day)``: the
    one-day range pass yields exactly this day's item-margin rows (catalog
    built once, this day costed via ``source.cost_book_as_of(day)`` — the
    prices in effect on the day being costed, not at render time, so a cost
    edit does not re-state history; Wave 2 slice 1, ADR-0004 decision 2).
    The rows are then rolled up exactly as before: flagged
    ``unmapped`` / ``unknown_price`` rows are excluded from the totals (their
    COGS is unknown) and their revenue is summed into ``flagged_revenue`` so
    it stays visible.

    Per-segment contribution margins (slice 07) are populated from the
    reliable rows only: flagged rows have unknown COGS, so booking their
    revenue into a segment's CM would over-state it. Both segments are always
    present; a segment with no reliable sales carries zeros.
    """
    rows = margins_over_range(source, day, day)[0].item_margins
    counted = [im for im in rows if not im.excluded_from_totals]
    flagged = [im for im in rows if im.excluded_from_totals]
    return DailyMargin(
        day=day,
        item_margins=rows,
        total_revenue=sum((im.revenue for im in counted), Money("0")),
        total_cogs=sum((im.cogs for im in counted), Money("0")),
        total_gross_margin=sum((im.gross_margin for im in counted), Money("0")),
        flagged_revenue=sum((im.revenue for im in flagged), Money("0")),
        segment_margins=segment_margins_from_items(counted, flagged),
    )


def segment_margins_from_items(
    counted_rows: list[ItemMargin],
    flagged_rows: list[ItemMargin] | None = None,
) -> tuple[SegmentMargin, ...]:
    """Roll reliable item-margin rows up into per-segment contribution margin.

    Only reliable rows (``excluded_from_totals`` False) are summed into
    ``revenue`` / ``variable_costs``: a flagged row's COGS is unknown, so
    its revenue cannot honestly contribute to a segment's CM (PRD user
    story 20: segment CM must stay "clean and defensible"). Both segments
    are always returned, in canonical order (see ``_SEGMENT_ORDER``); a
    segment with no reliable rows carries zeros.

    ADR-0007 (issue #73): the **flagged** rows' revenue is attributed to
    each segment by clock and surfaced as ``SegmentMargin.flagged_revenue``
    — the per-card honest-labelling line. Pre-#73 flagged revenue sat only
    at the daily level; surfacing it per-segment lets a partner see *which*
    segment the uncosted revenue sits in, without booking it into the CM.

    Today variable costs == COGS (direct labor is "if tracked" per issue 07
    and not tracked yet), so ``variable_costs`` is the sum of each row's COGS.
    """
    by_segment = _empty_segment_buckets()
    for im in counted_rows:
        bucket = by_segment[im.segment]
        bucket["revenue"] += im.revenue
        bucket["cogs"] += im.cogs
    # Per-segment flagged revenue: every flagged row contributes its
    # revenue to the segment it was sold in (its clock-stamped segment,
    # which is the row's ``segment`` post-#73).
    for im in flagged_rows or []:
        by_segment[im.segment]["flagged"] += im.revenue
    return _build_segment_margins(by_segment)


# Canonical display order for segment roll-ups: cafe first, then bar. The
# ``Segment`` enum lists BAR before CAFE and ``Segment.value`` is alphabetical
# ("bar" < "cafe"), so neither enum order nor ``.value`` sort gives cafe-first
# — hence this explicit key.
_SEGMENT_ORDER: dict[Segment, int] = {Segment.CAFE: 0, Segment.BAR: 1}


def _empty_segment_buckets() -> dict[Segment, dict[str, Money]]:
    """A fresh ``{segment: {revenue, cogs, flagged}}`` accumulator."""
    return {
        seg: {"revenue": Money("0"), "cogs": Money("0"), "flagged": Money("0")}
        for seg in Segment
    }


def _build_segment_margins(
    by_segment: dict[Segment, dict[str, Money]]
) -> tuple[SegmentMargin, ...]:
    """Turn a ``{segment: {revenue, cogs, flagged}}`` accumulator into
    SegmentMargins.

    One ``SegmentMargin`` per segment in canonical order (cafe-then-bar).
    Today variable costs == COGS, so ``contribution_margin = revenue - cogs``.
    ``flagged_revenue`` carries the segment's unmapped / unknown-price
    revenue (ADR-0007) — surfaced per-card, excluded from CM.
    """
    ordered = sorted(
        by_segment.items(), key=lambda kv: _SEGMENT_ORDER[kv[0]]
    )
    return tuple(
        SegmentMargin(
            segment=seg,
            revenue=bucket["revenue"],
            variable_costs=bucket["cogs"],
            contribution_margin=bucket["revenue"] - bucket["cogs"],
            flagged_revenue=bucket["flagged"],
        )
        for seg, bucket in ordered
    )
