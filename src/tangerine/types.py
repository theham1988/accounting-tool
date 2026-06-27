"""Schema-level types for the accounting domain.

These are the shapes that flow across the ingestion boundary and through the
margin engine. They are deliberately plain dataclasses: later slices may add
persistence, but the in-memory contract stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class Segment(str, Enum):
    """Business segment. Per PRD, every transaction/recipe/item is tagged."""

    CAFE = "cafe"
    BAR = "bar"


# Money is represented as Decimal throughout to avoid float rounding in THB.
Money = Decimal


@dataclass(frozen=True)
class RecipeIngredient:
    """One input into a recipe.

    A recipe is a formula, not a procurement decision: it carries only the
    SKU and the quantity consumed per unit produced. The current cost per
    unit is looked up at margin time from the ``CostBook`` (which tracks the
    latest approved purchase price). That keeps a re-pricing after the next
    receipt approval flowing straight into tomorrow's margin without the
    recipe having to change.

    ``quantity`` is expressed in the SKU's own ``unit`` (e.g. ml of beer,
    g of beans), so recipe-level and receipt-level quantities share a basis.
    """

    sku_id: str
    quantity: Decimal


@dataclass(frozen=True)
class Recipe:
    """How a saleable SKU is produced from input SKUs.

    Per the PRD recipe model and issue 04: recipes are defined against SKUs
    (the master items), not against Loyverse item ids. A Loyverse menu item
    maps to a SKU via a ``SkuMapping``; the recipe for that SKU is what the
    margin engine costs. This decouples the formula (a recipe) from the menu
    identity (a Loyverse item id) — two menu items can share one SKU/recipe.

    - ``sku_id``        the SKU this recipe produces (its key in a catalog)
    - ``ingredients``   the inputs; ``recipe_cost`` sums each input's
                        ``quantity`` × the input SKU's current cost per unit
    - ``yield_units``   how many saleable units one execution of the recipe
                        produces (PRD: "inputs and yield"). A single 500ml
                        pour yields 1; a 1L pitcher yielding two 500ml pours
                        is yield 2. Per-unit cost = input cost / yield_units.
    - ``target_gross_margin_pct``  optional; when set, the margin engine
                        flags items whose actual gross-margin % is below it
                        (PRD user story 13).
    """

    sku_id: str
    name: str
    segment: Segment
    ingredients: tuple[RecipeIngredient, ...]
    yield_units: int = 1
    target_gross_margin_pct: Decimal | None = None


@dataclass(frozen=True)
class SkuMapping:
    """Maps a Loyverse menu item id to a master SKU.

    Per the PRD / issue 04: "recipes are defined against SKUs, and Loyverse
    items map to SKUs." A sold Loyverse item resolves to its SKU through this
    mapping, and the SKU resolves to its recipe in the catalog. An item with
    no mapping is flagged in the margin table (PRD user story 12) rather than
    silently costed at zero.
    """

    item_id: str
    sku_id: str


@dataclass(frozen=True)
class Sale:
    """One unit of one item sold at a point in time.

    Slice 01 is single-unit: one Sale == one sold unit. Quantity is carried on
    the sale (defaulting to 1) so later slices can extend without reshaping.

    ``segment`` carries a pre-resolved segment tag for the sale, used as the
    **shift-timestamp fallback** (slice 07) when the sale's item has no recipe
    (and therefore no category-derived segment). The Loyverse parser resolves
    it from the receipt's ``created_at`` (8am–5pm cafe, else bar) and stamps
    it here, because that is the only place the time-of-day lives; the in-memory
    ``Sale.timestamp`` is a calendar date. For a mapped sale the recipe's
    segment always wins (see ``segments.segment_of_sale``).
    """

    item_id: str
    timestamp: date
    sell_price: Money
    quantity: int = 1
    segment: Segment | None = None


@dataclass(frozen=True)
class ItemMargin:
    """Per-item margin for a single day.

    The per-item margin table for the daily review (PRD user story 19). All
    money fields are per-period totals except ``cost_per_unit`` and
    ``sell_price`` which are per-unit reference values.

    - ``cost_per_unit``  recipe cost per unit, derived from current SKU costs
    - ``sell_price``     per-unit sell price (Loyverse)
    - ``units_sold``     total units sold in the period
    - ``revenue``        sell_price * units_sold
    - ``cogs``           cost_per_unit * units_sold
    - ``gross_margin``   revenue - cogs
    - ``gross_margin_pct``  gross_margin / revenue, to 2 dp (None if no revenue
                         or the row is flagged so the ratio is meaningless)
    - ``unmapped``       True when the sold item has no SKU/recipe mapping
                         (PRD user story 12). Flagged rows are surfaced for
                         review and excluded from the daily margin totals
                         (their cost is unknown, so booking full revenue as
                         margin would over-state profitability).
    - ``unknown_price``  True when the item is mapped but a recipe ingredient
                         SKU has no approved purchase price. Same treatment as
                         unmapped: flagged, excluded from totals.
    - ``below_target``   True when a target margin is set and actual < target.

    ``excluded_from_totals`` is True when ``unmapped`` or ``unknown_price`` is
    set; the daily roll-up sums only over rows where it is False.
    """

    item_id: str
    name: str
    segment: Segment
    day: date
    units_sold: int
    sell_price: Money
    cost_per_unit: Money
    revenue: Money
    cogs: Money
    gross_margin: Money
    gross_margin_pct: Decimal | None
    unmapped: bool = False
    unknown_price: bool = False
    below_target: bool = False

    @property
    def excluded_from_totals(self) -> bool:
        """True when this row's margin is not reliable enough to total.

        Unmapped items (no recipe) and items with an unknown ingredient price
        both have meaningless COGS; including them would over-state the day's
        gross margin. The daily roll-up sums only over non-excluded rows.
        """
        return self.unmapped or self.unknown_price


@dataclass(frozen=True)
class DailyMargin:
    """Roll-up of all item margins for a single day, across all segments.

    Totals are flat (not split by segment) and include only items whose margin
    is reliable: rows flagged ``unmapped`` (no recipe) or ``unknown_price``
    (an ingredient SKU has no approved price) are excluded from
    ``total_revenue``/``total_cogs``/``total_gross_margin`` because their COGS
    is unknown and booking their revenue as margin would over-state
    profitability. The revenue sitting in those flagged rows is surfaced
    separately as ``flagged_revenue`` so it is visible, not silently dropped.
    Per-item segment lives on each ``ItemMargin``; per-segment contribution
    margin is added in a later slice.
    """

    day: date
    item_margins: tuple[ItemMargin, ...]
    total_revenue: Money
    total_cogs: Money
    total_gross_margin: Money
    flagged_revenue: Money
    # Per-segment contribution margin for the day (slice 07). One entry per
    # segment, both segments always present (a segment with no reliable sales
    # carries zeros). Fixed costs are deliberately NOT allocated here — per
    # PRD user story 20 they live at entity level (slice 08).
    segment_margins: tuple[SegmentMargin, ...] = ()


@dataclass(frozen=True)
class SegmentMargin:
    """Per-segment contribution margin for a period (slice 07).

    Per the PRD segmentation model and issue 07:

    - ``revenue``             reliable revenue in the segment for the period
                              (unmapped / unknown-price rows are excluded —
                              their COGS is unknown, so booking their revenue
                              as CM would over-state the segment)
    - ``variable_costs``      segment COGS for the period (direct labor is
                              "if tracked" per the issue and not tracked yet,
                              so today this equals COGS)
    - ``contribution_margin`` revenue − variable_costs
    - ``is_red``              True when contribution_margin < 0 (PRD: a segment
                              failing to cover its own variable costs triggers
                              an explicit conversation)

    Fixed costs are never allocated to a segment (PRD user story 20); the
    segment's only profitability number is its contribution margin. Entity-
    level net profit (segments' CM minus fixed costs) is slice 08.
    """

    segment: Segment
    revenue: Money
    variable_costs: Money
    contribution_margin: Money

    @property
    def is_red(self) -> bool:
        """True when the segment's CM is negative (the failing threshold)."""
        return self.contribution_margin < 0


# --- Receipt ingestion (slice 03) -------------------------------------------
#
# The receipt pipeline turns an uploaded image into an approved purchase. The
# flow has three checkpoints, matching docs/issues/03-receipt-ingestion-pipeline.md:
#
#   1. Sum-check:   lines + VAT must reconcile to the stated total (tolerance).
#                   Failure -> auto-reject; the receipt never reaches the books.
#   2. Price-check: each line's unit price is compared to `last_known_price`
#                   for that (SKU, supplier). Deviation > 5% -> flag for review.
#   3. SKU mapping: lines without a SKU mapping are always queued for review,
#                   regardless of price check outcome.
#
# The dataclasses below model the boundary payloads and the processed results.
# They are frozen so the engine is a pure function over its inputs.


@dataclass(frozen=True)
class Sku:
    """A master item that ties a receipt line to a recipe.

    `unit` is the unit the SKU is priced and consumed in (e.g. "ml", "g").
    Slice 03 only needs the identity + unit; slice 04 wires recipes to SKUs.
    """

    sku_id: str
    name: str
    unit: str


@dataclass(frozen=True)
class Supplier:
    supplier_id: str
    name: str


class LineFlag(str, Enum):
    """Reason a receipt line was flagged for human review."""

    PRICE_DEVIATION = "price_deviation"  # unit price deviates >5% from last known
    UNMAPPED_SKU = "unmapped_sku"        # line description did not resolve to a SKU


class ReceiptState(str, Enum):
    """Lifecycle state of a receipt within the pipeline."""

    NEW = "new"                # uploaded, not yet checked
    AUTO_REJECTED = "auto_rejected"  # failed sum-check; bounced back
    QUEUED = "queued"          # passed sum-check; awaiting human decision
    APPROVED = "approved"      # partner approved (or corrected then approved)
    REJECTED = "rejected"      # partner rejected in the approval queue


@dataclass(frozen=True)
class ExtractedReceiptLine:
    """One line as produced by the OCR/LLM extraction step.

    `sku_id` is None when the extractor could not confidently map the
    description to a known SKU. Such lines are always queued for review.
    """

    description: str
    quantity: Decimal
    unit_price: Money
    sku_id: str | None = None


@dataclass(frozen=True)
class ExtractedReceipt:
    """Raw structured output from the OCR/LLM processor.

    This is the genuine external boundary of the receipt pipeline (PRD testing
    rule: only mock genuine external boundaries). Real implementations call a
    provider; tests and the seeded source supply this payload directly.
    """

    supplier_id: str
    invoice_date: date
    lines: tuple[ExtractedReceiptLine, ...]
    vat: Money
    total: Money


@dataclass(frozen=True)
class LastKnownPrice:
    """Reference price for a (SKU, supplier) pair.

    Updated whenever a receipt containing that pair is approved. New receipts'
    extracted unit prices are compared against this; >5% deviation flags for
    review. See PRD "Pricing reference data".
    """

    price: Money
    updated_at: date


@dataclass(frozen=True)
class CheckedLine:
    """A receipt line after the sum-check + price-check + SKU-check pass.

    Carries the flags raised for that line so the approval queue can show
    partners exactly what needs their attention.
    """

    description: str
    quantity: Decimal
    unit_price: Money
    sku_id: str | None
    flags: tuple[LineFlag, ...]


@dataclass(frozen=True)
class CheckedReceipt:
    """A receipt that has been through the check pipeline.

    Either `state` is AUTO_REJECTED (sum-check failed) or it is QUEUED with
    the per-line flags populated. Partners act on QUEUED receipts.
    """

    supplier_id: str
    invoice_date: date
    vat: Money
    total: Money
    state: ReceiptState
    lines: tuple[CheckedLine, ...]
    # Human-readable reason for an auto-reject. None for queued/approved.
    rejection_reason: str | None = None


@dataclass(frozen=True)
class ReceiptDecision:
    """A partner's decision on a queued receipt.

    `corrected_lines` is only meaningful for CORRECTED approvals: it lets a
    partner fix an OCR mistake (e.g. wrong unit price) and approve the
    corrected values. For plain APPROVE decisions it is None.
    """

    decision: ReceiptState
    corrected_lines: tuple[ExtractedReceiptLine, ...] | None = None


@dataclass(frozen=True)
class PurchaseLine:
    """A stored purchase line: a receipt line that has entered the books."""

    sku_id: str | None
    description: str
    quantity: Decimal
    unit_price: Money


@dataclass(frozen=True)
class Purchase:
    """A receipt that has been approved and entered the books.

    Purchases are the input to accrual COGS (slice 06) and to updating
    `last_known_price`. Each approved receipt becomes exactly one Purchase.
    """

    supplier_id: str
    invoice_date: date
    lines: tuple[PurchaseLine, ...]
    vat: Money
    total: Money


# --- Keg inventory (slice 05) ------------------------------------------------
#
# Weekly keg weighing turns a physical measurement into beer volume, which is
# the periodic-inventory number that makes accrual COGS work (see slice 08).
# Per docs/issues/05-keg-inventory-weekly-weighing.md:
#
#   volume = (gross_weight - tare_weight) / density
#
# A period runs from one weigh-in to the next; the beer consumed in that period
# is `beginning_volume - ending_volume`, and its accrual COGS is consumed
# volume x the brand's current cost per ml (from the CostBook, supplier-agnostic
# per slice 04). Actual yield (Loyverse rung-up pours) vs theoretical yield
# gives the loss %; that variance is surfaced but not attributed to individual
# kegs (PRD out of scope: "per-keg yield tracking").
#
# Density defaults to water (1.0 g/ml) with a documented ~0.5-1.5% volume
# tolerance. Each KegBrand carries its own density so the approximation can be
# overridden per brand when better data exists.


#: Default density approximation, in grams per millilitre. Water density is
#: used because beer specific-gravity data is out of scope (PRD); the volume
#: derived from this carries a documented ~0.5-1.5% tolerance surfaced on the
#: report rather than silently absorbed. See docs/issues/05 AC and PRD "Out of
#: Scope" -> "Specific gravity / density tracking per beer".
DEFAULT_KEG_DENSITY: Decimal = Decimal("1.0")

#: Human-readable note describing the density-approximation tolerance, surfaced
#: on every keg-inventory row so a reader cannot mistake the volume for exact.
#: Kept beside ``DEFAULT_KEG_DENSITY`` (the field default) since both describe
#: the same water-density approximation.
DENSITY_TOLERANCE_NOTE: str = (
    "Volume derived from water-density approximation (1.0 g/ml); "
    "documented ~0.5-1.5% volume tolerance per PRD out-of-scope."
)


@dataclass(frozen=True)
class KegBrand:
    """A draught beer brand and the physical constants needed to weigh it.

    Per docs/issues/05 AC: per-brand keg records exist, carrying the tare
    weight and a density approximation. ``beer_sku_id`` ties the brand to the
    master beer SKU whose per-ml cost the engine looks up in the CostBook
    (the same SKU slice-04 recipes reference as an ingredient).

    - ``brand_id``    stable identifier for the brand (e.g. "chang", "leo")
    - ``name``        human-readable brand name
    - ``beer_sku_id`` the master beer SKU this brand pours (e.g. "chang-keg")
    - ``tare_weight_g`` empty keg weight in grams (entered once; draught
                      rotation is low so this is rarely edited)
    - ``density_g_per_ml``  beer density used to convert net weight to volume.
                      Defaults to water density (1.0 g/ml) with the documented
                      ~0.5-1.5% tolerance surfaced on the report.

    The issue's "theoretical pours per 20L keg at glass size (e.g. 40 x 500ml)"
    framing is deliberately NOT carried as a per-brand field: loss is computed
    on a single physical basis (beer volume in ml), so a glass-size conversion
    would only re-express the same ratio. See ``KegInventoryRow``.
    """

    brand_id: str
    name: str
    beer_sku_id: str
    tare_weight_g: Decimal
    density_g_per_ml: Decimal = DEFAULT_KEG_DENSITY


@dataclass(frozen=True)
class KegWeighIn:
    """One weekly weigh of one brand, captured as an aggregate gross weight.

    Per the agreed scope, a weigh-in records the aggregate gross weight across
    all kegs of the brand on that date (the issue's "per keg (or per keg batch)
    per brand" collapses to one aggregate record per brand, matching the PRD's
    "aggregate yield" wording). The beer volume at that moment is
    ``(gross_weight_g - tare_weight_g) / density``.

    A period runs from one weigh-in to the next for the same brand: the first
    weigh has no prior, so its period COGS is undefined (its volume is the
    beginning inventory for the next period).
    """

    brand_id: str
    weighed_on: date
    gross_weight_g: Decimal


@dataclass(frozen=True)
class KegInventoryRow:
    """One brand's period inventory result.

    Covers exactly one period for one brand: from the previous weigh-in
    (``beginning_weighed_on``) to the current one (``ending_weighed_on``).
    Carries the numbers the monthly accrual P&L (slice 08) consumes and that
    the daily review surfaces as a loss flag:

    - ``volume_consumed_ml``    beginning - ending volume (negative when the
                                ending weigh is heavier than the beginning —
                                a mid-period refill without a separate weigh;
                                surfaced as-is, not clamped)
    - ``rung_up_pours_ml``      Loyverse rung-up beer ml for the brand over the
                                period (sum of sold recipe ml, from sales)
    - ``accrual_cogs``          consumed volume x brand's current cost per ml
                                (negative when consumption is negative)
    - ``theoretical_yield_ml``  the volume the rung-up pours are compared
                                against: it is the consumed volume itself, so
                                the loss is computed on a single physical
                                basis (beer ml) rather than re-expressed in
                                pours at a glass size
    - ``loss_pct``              1 - (rung_up_pours_ml / volume_consumed_ml),
                                or None when consumed volume is zero (a brand
                                that sold nothing or was weighed identically)
    - ``beginning_volume_ml`` / ``ending_volume_ml``  the inventory numbers
                                themselves, so slice 08 can also report
                                "beginning + purchases - ending" if needed

    Loss is computed on a single physical basis (beer ml). The issue's
    "theoretical pours per 20L keg at glass size (e.g. 40 x 500ml)" framing is
    honoured by the loss ratio itself: the consumed volume IS the theoretical
    yield, and comparing it to rung-up ml (the actual yield) gives the loss %.
    A glass-size conversion would only re-express the same ratio in pour
    units, so it is not carried here.
    """

    brand_id: str
    name: str
    beginning_weighed_on: date
    ending_weighed_on: date
    beginning_volume_ml: Decimal
    ending_volume_ml: Decimal
    volume_consumed_ml: Decimal
    rung_up_pours_ml: Decimal
    accrual_cogs: Money
    theoretical_yield_ml: Decimal
    loss_pct: Decimal | None
    density_g_per_ml: Decimal
    density_tolerance_note: str


@dataclass(frozen=True)
class KegInventoryReport:
    """All-brand weekly keg inventory result for one weigh period.

    One row per brand that had a weigh-in on the period's ending date with a
    prior weigh on file. Brands whose only weigh is the very first one appear
    in ``unstarted_brand_ids`` (their first volume becomes the next period's
    beginning inventory).
    """

    period_start: date
    period_end: date
    rows: tuple[KegInventoryRow, ...]
    unstarted_brand_ids: tuple[str, ...]
    total_accrual_cogs: Money
# --- Cafe stock counts → accrual COGS (slice 06) ----------------------------
#
# Issue 06 introduces partner-entered cafe stock counts for perishables (milk,
# beans, pastries). Each item type has its own count cadence by shelf life
# (milk daily, beans weekly, etc.). Consumed quantity for the period is the
# accrual-COGS primitive:
#
#     consumed = beginning + purchases − ending
#
# Priced at the SKU's latest approved purchase price, that becomes the cafe
# segment's consumption-based COGS for the period — the monthly-view number
# that slice 08 wires into the P&L. The daily 9am view keeps using the
# recipe-based margin engine (slice 04); this slice does not touch it.
#
# Per the issue, this mirrors the keg-inventory approach in slice 05 but is
# self-contained: there is no shared inventory abstraction yet.


class CafeCountCadence(str, Enum):
    """How often a cafe item is physically counted.

    Per issue 06: per-item cadence based on shelf life (milk daily, beans
    weekly). This slice records the cadence as stored configuration; whether
    a count is overdue or missing is enforced by slice 12 (admin checklists),
    not here.
    """

    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass(frozen=True)
class CafeItem:
    """A perishable cafe SKU that is tracked by physical stock counts.

    ``cadence`` is the partner-count schedule for this item. The ``unit`` is
    the SKU's own unit (ml of milk, g of beans) so a count and a purchase of
    the same SKU share a basis.
    """

    sku_id: str
    name: str
    unit: str
    cadence: CafeCountCadence


@dataclass(frozen=True)
class CafeStockCount:
    """One physical count of one cafe SKU at a point in time.

    The minimal partner-entry shape (issue 06: "keep the UI/input path
    minimal"). ``quantity`` is in the SKU's own unit. ``timestamp`` is when
    the count was taken — the engine uses the opening and closing counts'
    timestamps to bound which purchases belong to the period.
    """

    sku_id: str
    quantity: Decimal
    timestamp: date


@dataclass(frozen=True)
class CafeConsumedCogs:
    """Consumed quantity and its COGS contribution for one cafe SKU over a period.

    The accrual-COGS result for one cafe SKU, ready for the monthly P&L
    (slice 08). All quantity fields are in ``unit``; all money fields are THB.

    - ``beginning_quantity``  on-hand at the opening count
    - ``purchased_quantity``  purchases received strictly after the opening
                              count and on/before the closing count
    - ``ending_quantity``     on-hand at the closing count
    - ``consumed_quantity``   ``beginning + purchased − ending`` (can be
                              negative — a count error or unrecorded purchase;
                              surfaced, not clamped)
    - ``unit_cost``           the SKU's latest approved price per ``unit``,
                              or ``0`` when ``unpriced``
    - ``cogs``                ``consumed_quantity × unit_cost``, or ``0`` when
                              ``unpriced`` (consumption is still surfaced)
    - ``unpriced``            True when the SKU has no approved price. The
                              consumed quantity is still computed and surfaced
                              so a missing price cannot silently zero-cost a
                              whole category, but COGS is not booked

    A negative ``consumed_quantity`` is reported as-is rather than clamped to
    zero so a later slice can flag it; clamping would hide stock appearing
    from nowhere (a count error or an unrecorded purchase).
    """

    sku_id: str
    name: str
    unit: str
    cadence: CafeCountCadence
    beginning_quantity: Decimal
    purchased_quantity: Decimal
    ending_quantity: Decimal
    consumed_quantity: Decimal
    unit_cost: Money
    cogs: Money
    unpriced: bool = False

# --- Fixed costs + monthly accrual P&L (slice 08) ----------------------------
#
# Issue 08 introduces entity-level fixed cost entry and the monthly reconciliation
# view using proper accrual-basis COGS. Per the PRD segmentation model:
#
#     entity_net_profit(month) =
#         sum_over_segments(contribution_margin) - fixed_costs(entity, month)
#
# where the monthly contribution margin per segment is revenue − that segment's
# accrual COGS (beginning inventory value + purchases − ending inventory value).
# The bar's accrual COGS comes from slice 05 (keg weigh-ins), the cafe's from
# slice 06 (cafe stock counts); the monthly engine calls both internally.
#
# Fixed costs are recorded against the entity (the whole business), never against
# a segment (PRD user story 20), and are matched to a month. The 10,000 THB/day
# target (PRD problem statement) becomes a monthly target = 10K × days in month.
#
# A separate cash-flow view reports payables by invoice date — the cash the
# business owes in the month — so the accounting view (COGS by consumption) and
# the cash-flow view (when bills are due) are both available (PRD user story 24).


#: The daily profit target, in THB. PRD problem statement: "Our real target is
#: 10,000 THB/day profit." The monthly view compares entity net profit against
#: this × days in month (issue 08 AC).
DAILY_PROFIT_TARGET_THB: Decimal = Decimal("10000")


class FixedCostCategory(str, Enum):
    """Entity-level fixed cost category (issue 08 AC: amount, category, period).

    Fixed costs are recorded against the entity — the whole business — never
    against a segment (PRD user story 20). The category groups them on the
    monthly P&L. ``OTHER`` covers anything outside the known set so the entry
    shape stays open-ended (the issue lists "rent, utilities, shared staff
    salaries, insurance, etc." — the "etc." is the catch-all).
    """

    RENT = "rent"
    UTILITIES = "utilities"
    STAFF_SALARIES = "staff_salaries"
    INSURANCE = "insurance"
    OTHER = "other"


#: A (year, month) period identifier for a recurring monthly fixed cost.
#: Year is the full calendar year (e.g. 2026); month is 1–12.
YearMonth = tuple[int, int]


@dataclass(frozen=True)
class FixedCost:
    """One entity-level fixed cost for one month.

    Per issue 08 AC: a fixed cost entry carries its amount, category, and the
    period it applies to. ``period`` is a ``(year, month)`` tuple because these
    are monthly recurring costs (rent, salaries) — the natural granularity for
    matching against a monthly P&L. The monthly engine picks up the fixed
    costs whose ``period`` matches the P&L month.

    Fixed costs are never allocated to a segment (PRD user story 20); they are
    subtracted from the sum of segment contribution margins to reach entity
    net profit (PRD "Segmentation" decision shape).
    """

    amount: Money
    category: FixedCostCategory
    period: YearMonth


@dataclass(frozen=True)
class SegmentAccrualPnl:
    """One segment's monthly accrual-basis P&L (issue 08).

    The monthly contribution margin is computed on the **accrual basis** —
    revenue minus that segment's accrual COGS (beginning inventory value +
    purchases − ending inventory value) — distinct from the daily recipe-based
    margin in slice 04. This is the "proper accrual-basis COGS" the PRD
    monthly view requires (PRD user story 22).

    - ``revenue``         segment revenue for the month (Loyverse sales by
                          transaction timestamp, reliable rows only)
    - ``accrual_cogs``    beginning inventory value + purchases − ending
                          inventory value, for the segment's inventory:
                          keg weigh-ins (slice 05) for bar, cafe stock counts
                          (slice 06) for cafe
    - ``contribution_margin``  ``revenue − accrual_cogs``
    - ``is_red``          True when the segment's accrual CM < 0 (mirrors the
                          slice-07 daily flag on the monthly number)
    """

    segment: Segment
    revenue: Money
    accrual_cogs: Money
    contribution_margin: Money

    @property
    def is_red(self) -> bool:
        """True when the segment's monthly accrual CM is negative."""
        return self.contribution_margin < 0


@dataclass(frozen=True)
class CashFlowEntry:
    """One payable recognised by invoice date (the cash-flow view).

    Per PRD user story 24, cash-basis payables are tracked by invoice date
    separately from accrual COGS, so both views are available: the accounting
    view (COGS by consumption) and the cash-flow view (when bills are due).
    This row is the cash-flow view's per-invoice line.
    """

    supplier_id: str
    invoice_date: date
    total: Money


@dataclass(frozen=True)
class CashFlowView:
    """Cash-flow view: payables recognised by invoice date for the month.

    All purchases whose invoice date falls in the month, summed by total (the
    cash the business owes for goods received that month, on a cash basis).
    Reported separately from accrual COGS because the two answer different
    questions: accrual asks "what did we consume?", cash-flow asks "what bills
    landed this month?".
    """

    month: YearMonth
    total_payables: Money
    entries: tuple[CashFlowEntry, ...]


@dataclass(frozen=True)
class GoalStatus:
    """Entity net profit vs the 10,000 THB/day target for the month.

    Per the PRD problem statement the venue's real target is 10,000 THB/day
    profit; the monthly view scales that to ``target = 10,000 × days in month``
    (issue 08 AC) and reports whether net profit met, missed, or hit exactly.
    """

    target: Money
    actual: Money
    #: Calendar days in the month the target was scaled over.
    days_in_month: int

    @property
    def met(self) -> bool:
        """True when actual net profit ≥ the monthly target."""
        return self.actual >= self.target

    @property
    def surplus(self) -> Money:
        """``actual − target`` (negative when the target was missed)."""
        return self.actual - self.target


@dataclass(frozen=True)
class MonthlyPnl:
    """Full monthly accrual P&L for the entity (issue 08).

    The monthly reconciliation view the PRD calls for (PRD user story 23):
    full entity-level net profit from segments' contribution margin minus
    fixed costs. Revenue and accrual COGS are per segment (``segment_pnl``),
    so a reader can see which half of the business earned its keep; fixed
    costs are entity-level only (PRD user story 20), not allocated.

    - ``month``           ``(year, month)`` the P&L covers
    - ``segment_pnl``     one ``SegmentAccrualPnl`` per segment (both segments
                          always present), in canonical cafe-then-bar order
    - ``fixed_costs``     the entity-level fixed costs recognised this month,
                          one row per ``FixedCost`` entry
    - ``total_fixed_costs``   sum of ``fixed_costs`` amounts
    - ``entity_net_profit``   sum of segment CM − total_fixed_costs
    - ``goal``            the ``GoalStatus`` comparing net profit to the
                          10K THB/day × days-in-month target
    - ``cash_flow``       payables by invoice date (a separate view from the
                          accrual COGS above)
    """

    month: YearMonth
    segment_pnl: tuple[SegmentAccrualPnl, ...]
    fixed_costs: tuple[FixedCost, ...]
    total_fixed_costs: Money
    entity_net_profit: Money
    goal: GoalStatus
    cash_flow: CashFlowView
# --- Cash drawer reconciliation (slice 09) -----------------------------------
#
# Each shift close captures the four numbers in the PRD "Cash control" section:
#
#     opening cash   carried from the prior shift's close
#     closing cash   counted by the closing cashier at shift end
#     rung-up cash   Loyverse cash sales rung up over the shift
#     variance       closing − (opening + rung-up)
#
# A positive variance means cash is OVER (more in the drawer than the system
# says should be); negative means SHORT. The sign is surfaced as-is because
# both directions are operationally meaningful (overages can hide mis-rings;
# shorts can hide theft) and slice 10's anomaly detector consumes the raw
# number.
#
# The 5pm handoff is the only real control moment in the two-partner, no-
# manager structure (PRD "Known control gap"): the closing cashier counts
# their own drawer, so the incoming partner's recount is the segregation-of-
# duties substitute. If the recounted closing cash does not match the
# outgoing partner's reported closing cash (within tolerance), shift start is
# BLOCKED. The agreed default tolerance is 0 THB — the recount is the control
# moment, so any discrepancy surfaces — but it is a parameter so a future
# manager can relax it without re-architecting.
#
# Drawer variance history is recorded per shift, per cashier. That history is
# the input to the slice-10 anomaly detector; this slice only produces and
# stores it.


@dataclass(frozen=True)
class ShiftClose:
    """One shift's closing cash record, captured at shift end.

    The minimal partner-entry shape for cash control (PRD user stories 14–15).
    All four money fields are THB, carried as ``Decimal`` to avoid float drift.

    - ``shift_id``       stable identifier for the shift (e.g. "2026-06-24-day")
    - ``cashier_id``     who counted this drawer (for per-cashier variance
                         history → slice 10 anomaly detection)
    - ``closed_at``      when the shift closed (and the drawer was counted)
    - ``opening_cash``   cash in the drawer at shift start, carried from the
                         prior shift's ``closing_cash``
    - ``closing_cash``   cash the closing cashier counted at shift end
    - ``rung_up_cash``   Loyverse cash sales rung up over the shift
    - ``variance``       ``closing_cash − (opening_cash + rung_up_cash)``;
                         positive = over, negative = short
    """

    shift_id: str
    cashier_id: str
    closed_at: datetime
    opening_cash: Money
    closing_cash: Money
    rung_up_cash: Money
    variance: Money


@dataclass(frozen=True)
class HandoffRecount:
    """The incoming partner's recount of the outgoing partner's drawer.

    Per PRD user story 15 and the "Cash control" implementation decision: at
    the 5pm handoff the incoming partner re-counts the drawer and the recount
    is compared to the outgoing shift's reported ``closing_cash``. A mismatch
    outside tolerance blocks shift start.

    - ``outgoing_shift_id``  the shift whose ``closing_cash`` is being verified
    - ``incoming_cashier_id`` the partner doing the recount
    - ``recounted_at``       when the recount happened
    - ``recounted_cash``     the cash the incoming partner counted
    """

    outgoing_shift_id: str
    incoming_cashier_id: str
    recounted_at: datetime
    recounted_cash: Money


@dataclass(frozen=True)
class HandoffResult:
    """Outcome of checking a handoff recount against the outgoing close.

    ``is_blocked`` is the control signal the shift-start flow gates on (PRD:
    "Mismatch ... blocks shift start"). ``discrepancy`` is signed
    (``recounted_cash − reported_closing_cash``) so the review can tell over
    from short; ``abs(discrepancy)`` is what the tolerance is compared against.

    ``discrepancy`` is deliberately stored even when ``is_blocked`` is False:
    a within-tolerance discrepancy is still a real signal (a small miscount
    or rounding) that slice 10's anomaly detector may want to see, and
    surfacing it is cheaper than recomputing it later.
    """

    outgoing_shift_id: str
    reported_closing_cash: Money
    recounted_cash: Money
    discrepancy: Money
    tolerance: Money
    is_blocked: bool
