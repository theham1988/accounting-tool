"""Schema-level types for the accounting domain.

These are the shapes that flow across the ingestion boundary and through the
margin engine. They are deliberately plain dataclasses: later slices may add
persistence, but the in-memory contract stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
