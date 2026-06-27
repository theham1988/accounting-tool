"""End-to-end cafe stock counts → accrual COGS test seam (slice 06).

Per the PRD testing rules these tests read as worked examples: "given an
opening milk count of 5000 ml, a 10000 ml delivery, and a closing count of
3000 ml, consumed milk is 12000 ml → at 0.025 THB/ml that is 300 THB of
cafe COGS for the period." They feed synthetic cafe stock counts and
approved purchases through the inventory engine and assert the consumed
quantity and its COGS contribution.

Scope (issue 06): this slice computes consumed quantity
(``beginning + purchases − ending``) per cafe SKU and prices it at the
SKU's latest approved purchase price, as a **standalone period result**.
It deliberately does not touch the daily recipe-based margin engine
(slice 04) — per the PRD, accrual COGS belongs to the monthly view, which
slice 08 wires up. Per-item count cadence is stored configuration only;
scheduling/overdue-count enforcement is slice 12's job.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tangerine.approvals import ApprovalBook, apply_decision
from tangerine.cafe_stock import (
    compute_cafe_consumed_cogs,
    consumed_quantity,
)
from tangerine.cost import CostBook
from tangerine.receipts import check_receipt
from tangerine.types import (
    CafeConsumedCogs,
    CafeCountCadence,
    CafeItem,
    CafeStockCount,
    ExtractedReceipt,
    ExtractedReceiptLine,
    Purchase,
    ReceiptDecision,
    ReceiptState,
    Sku,
)

D = Decimal


# --- shared fixtures --------------------------------------------------------


@pytest.fixture
def period_start() -> date:
    return date(2026, 6, 1)


@pytest.fixture
def period_end() -> date:
    return date(2026, 6, 30)


@pytest.fixture
def milk_item() -> CafeItem:
    return CafeItem(
        sku_id="milk-fresh",
        name="Fresh milk",
        unit="ml",
        cadence=CafeCountCadence.DAILY,
    )


@pytest.fixture
def beans_item() -> CafeItem:
    return CafeItem(
        sku_id="beans-arabica",
        name="Arabica beans",
        unit="g",
        cadence=CafeCountCadence.WEEKLY,
    )


# --- AC 1: per-item count cadence is configurable (daily, weekly) -----------


def test_cafe_item_carries_configurable_count_cadence(
    milk_item: CafeItem, beans_item: CafeItem
) -> None:
    """Each cafe item carries a count cadence of daily or weekly.

    Perishables cadence by shelf life (issue 06): milk counted daily,
    beans counted weekly. The cadence is stored on the item as
    configuration; this slice records whatever counts the partner enters
    (enforcement is slice 12).
    """
    assert milk_item.cadence == CafeCountCadence.DAILY
    assert beans_item.cadence == CafeCountCadence.WEEKLY

    # Both enum values exist and round-trip through their string form so a
    # future config/persistence layer can read/write them as plain strings.
    assert CafeCountCadence("daily") is CafeCountCadence.DAILY
    assert CafeCountCadence("weekly") is CafeCountCadence.WEEKLY


# --- AC 2: stock count entry captures item, quantity, timestamp -------------


def test_stock_count_captures_item_quantity_timestamp(
    milk_item: CafeItem, period_start: date
) -> None:
    """A cafe stock count records which item, how much, and when.

    The count is the minimal partner-entry shape (issue 06: "keep the
    UI/input path minimal"). ``sku_id`` ties it to the cafe item; the
    quantity is expressed in the SKU's own unit (ml of milk); the
    timestamp is when the count was taken.
    """
    count = CafeStockCount(
        sku_id=milk_item.sku_id,
        quantity=D("5000"),
        timestamp=period_start,
    )

    assert count.sku_id == "milk-fresh"
    assert count.quantity == D("5000")
    assert count.timestamp == period_start


# --- AC 3: consumed quantity = beginning + purchases − ending ----------------


def test_consumed_quantity_is_beginning_plus_purchases_minus_ending() -> None:
    """The accrual-COGS consumption primitive.

    Pure formula (issue 06): ``consumed = beginning + purchases − ending``.
    5000 ml opening + 10000 ml delivered − 3000 ml closing = 12000 ml
    consumed. Pricing happens in the engine; this is the bare arithmetic
    so it can be unit-checked in isolation.
    """
    assert consumed_quantity(
        beginning=D("5000"), purchased=D("10000"), ending=D("3000")
    ) == D("12000")


def test_consumed_quantity_can_be_negative_to_surface_anomaly() -> None:
    """A negative consumed quantity is surfaced, not clamped.

    Ending (15000) > beginning (5000) + purchases (5000) means stock
    appeared from nowhere — a count error or unrecorded purchase. The
    formula returns −5000 so a later slice can flag it; silently
    clamping to zero would hide the discrepancy.
    """
    assert consumed_quantity(
        beginning=D("5000"), purchased=D("5000"), ending=D("15000")
    ) == D("-5000")


# --- AC 3 + AC 4: engine computes consumed qty and prices it ----------------


def test_engine_computes_consumed_quantity_and_cogs_for_single_item(
    milk_item: CafeItem, period_start: date, period_end: date
) -> None:
    """Worked example: one milk SKU across a month.

    Opening 5000 ml (Jun 1) + 10000 ml delivered (Jun 15) − 3000 ml
    closing (Jun 30) = 12000 ml consumed. Priced at the latest approved
    purchase price (0.025 THB/ml) → 300 THB of cafe COGS for the period.
    """
    cost = CostBook({"milk-fresh": (D("0.025"), date(2026, 6, 15))})
    purchases = [
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 6, 15),
            lines=(
                _purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),
            ),
            vat=D("0"),
            total=D("250"),
        ),
    ]
    beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=period_start),
    ]
    ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("3000"), timestamp=period_end),
    ]

    results = compute_cafe_consumed_cogs(
        items=[milk_item],
        beginning=beginning,
        ending=ending,
        purchases=purchases,
        cost=cost,
    )

    assert len(results) == 1
    r = results[0]
    assert r.sku_id == "milk-fresh"
    assert r.cadence == CafeCountCadence.DAILY
    assert r.beginning_quantity == D("5000")
    assert r.purchased_quantity == D("10000")
    assert r.ending_quantity == D("3000")
    assert r.consumed_quantity == D("12000")
    assert r.unit_cost == D("0.025")
    assert r.cogs == D("300")
    assert r.unpriced is False


def test_engine_costs_each_cafe_item_independently(
    milk_item: CafeItem,
    beans_item: CafeItem,
    period_start: date,
    period_end: date,
) -> None:
    """Two cafe SKUs in the same period each get their own consumed-COGS row.

    Milk (daily): 5000 + 10000 − 3000 = 12000 ml @ 0.025 → 300 THB.
    Beans (weekly): 2000 + 5000 − 1000 = 6000 g @ 2 → 12000 THB.
    """
    cost = CostBook(
        {
            "milk-fresh": (D("0.025"), date(2026, 6, 15)),
            "beans-arabica": (D("2"), date(2026, 6, 10)),
        }
    )
    purchases = [
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 6, 15),
            lines=(
                _purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),
            ),
            vat=D("0"),
            total=D("250"),
        ),
        Purchase(
            supplier_id="phuket-coffee",
            invoice_date=date(2026, 6, 10),
            lines=(
                _purchase_line("beans-arabica", "Arabica 1kg", D("5000"), D("2")),
            ),
            vat=D("0"),
            total=D("10000"),
        ),
    ]
    beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=period_start),
        CafeStockCount(sku_id="beans-arabica", quantity=D("2000"), timestamp=period_start),
    ]
    ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("3000"), timestamp=period_end),
        CafeStockCount(sku_id="beans-arabica", quantity=D("1000"), timestamp=period_end),
    ]

    results = compute_cafe_consumed_cogs(
        items=[milk_item, beans_item],
        beginning=beginning,
        ending=ending,
        purchases=purchases,
        cost=cost,
    )

    by_sku = {r.sku_id: r for r in results}
    assert by_sku["milk-fresh"].consumed_quantity == D("12000")
    assert by_sku["milk-fresh"].cogs == D("300")
    assert by_sku["beans-arabica"].consumed_quantity == D("6000")
    assert by_sku["beans-arabica"].cogs == D("12000")


# --- purchases outside the period window are excluded ----------------------


def test_purchases_outside_period_window_are_excluded(
    milk_item: CafeItem, period_start: date, period_end: date
) -> None:
    """Only purchases received between the two counts count toward consumption.

    A delivery dated before the opening count belongs to the prior period;
    one dated after the closing count belongs to the next. The engine's
    window is ``(beginning.timestamp, ending.timestamp]`` — a purchase on
    the closing-count day is in, one on the opening-count day is out.

    Here: opening 5000 (Jun 1), closing 3000 (Jun 30). A 10000 ml May
    delivery and a 10000 ml July delivery are both excluded → purchased
    quantity for the period is 0 → consumed = 5000 + 0 − 3000 = 2000.
    """
    cost = CostBook({"milk-fresh": (D("0.025"), date(2026, 6, 15))})
    purchases = [
        # Before the opening count → prior period.
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 5, 20),
            lines=(
                _purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),
            ),
            vat=D("0"),
            total=D("250"),
        ),
        # After the closing count → next period.
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 7, 5),
            lines=(
                _purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),
            ),
            vat=D("0"),
            total=D("250"),
        ),
    ]
    beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=period_start),
    ]
    ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("3000"), timestamp=period_end),
    ]

    results = compute_cafe_consumed_cogs(
        items=[milk_item],
        beginning=beginning,
        ending=ending,
        purchases=purchases,
        cost=cost,
    )

    r = results[0]
    assert r.purchased_quantity == D("0")
    assert r.consumed_quantity == D("2000")  # 5000 + 0 − 3000
    assert r.cogs == D("50")  # 2000 × 0.025


# --- per-SKU purchase window: counts on different days for different items -----


def test_purchase_window_is_per_sku_not_global(
    milk_item: CafeItem,
    beans_item: CafeItem,
) -> None:
    """Each cafe SKU's purchase window is bounded by ITS OWN counts, not a
    global earliest/latest across all SKUs.

    Issue 06 sets count cadence per item by shelf life (milk daily, beans
    weekly), so counts for different SKUs land on different days. The
    purchase window must therefore be per-SKU: a beans delivery dated
    between the milk opening and the beans opening belongs to the *prior*
    period for beans, not the current one — otherwise purchases get
    mis-attributed to a SKU whose opening count hadn't happened yet.

    Setup:
      - milk (daily):   opening Jun 1,  closing Jun 30
      - beans (weekly): opening Jun 8,  closing Jun 28
      - A beans purchase of 5000 g dated Jun 5 — strictly before the beans
        opening count (Jun 8), so it belongs to the prior beans period.

    Expected beans row: purchased = 0 (the Jun 5 delivery is out of
    beans' window), consumed = 2000 + 0 − 1000 = 1000 g.

    A global window (Jun 1 → Jun 30) would wrongly include the Jun 5
    delivery and report beans purchased = 5000, consumed = 6000.
    """
    cost = CostBook(
        {
            "milk-fresh": (D("0.025"), date(2026, 6, 15)),
            "beans-arabica": (D("2"), date(2026, 6, 5)),
        }
    )
    purchases = [
        Purchase(
            supplier_id="phuket-coffee",
            invoice_date=date(2026, 6, 5),
            lines=(
                _purchase_line("beans-arabica", "Arabica 1kg", D("5000"), D("2")),
            ),
            vat=D("0"),
            total=D("10000"),
        ),
    ]
    beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=date(2026, 6, 1)),
        CafeStockCount(sku_id="beans-arabica", quantity=D("2000"), timestamp=date(2026, 6, 8)),
    ]
    ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("3000"), timestamp=date(2026, 6, 30)),
        CafeStockCount(sku_id="beans-arabica", quantity=D("1000"), timestamp=date(2026, 6, 28)),
    ]

    results = compute_cafe_consumed_cogs(
        items=[milk_item, beans_item],
        beginning=beginning,
        ending=ending,
        purchases=purchases,
        cost=cost,
    )

    by_sku = {r.sku_id: r for r in results}
    # The Jun 5 beans delivery predates the Jun 8 beans opening count ->
    # prior period for beans. Beans purchased this period must be 0.
    assert by_sku["beans-arabica"].purchased_quantity == D("0")
    assert by_sku["beans-arabica"].consumed_quantity == D("1000")  # 2000 + 0 − 1000


# --- unpriced cafe SKU is flagged, not silently zero-costed -----------------


def test_unpriced_cafe_sku_flagged_not_silently_zero_costed(
    milk_item: CafeItem, period_start: date, period_end: date
) -> None:
    """A cafe SKU with no approved price is flagged, not silently zero-costed.

    Mirrors the slice-04 margin-engine convention (``unknown_price``): the
    consumption quantity is still computed and surfaced, but COGS is
    reported as 0 with ``unpriced=True`` so it cannot be silently booked
    as zero — booking zero COGS would under-report cost and over-state
    period margin.
    """
    cost = CostBook({})  # no price for milk-fresh
    purchases = [
        Purchase(
            supplier_id="phuket-dairy",
            invoice_date=date(2026, 6, 15),
            lines=(
                _purchase_line("milk-fresh", "Fresh milk 1L", D("10000"), D("0.025")),
            ),
            vat=D("0"),
            total=D("250"),
        ),
    ]
    beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=period_start),
    ]
    ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("3000"), timestamp=period_end),
    ]

    results = compute_cafe_consumed_cogs(
        items=[milk_item],
        beginning=beginning,
        ending=ending,
        purchases=purchases,
        cost=cost,
    )

    r = results[0]
    assert r.consumed_quantity == D("12000")  # quantity is still known
    assert r.unpriced is True
    assert r.cogs == D("0")  # but COGS cannot be honestly booked
    assert r.unit_cost == D("0")


# --- AC 5: end-to-end — counts + approved purchases → consumed COGS ---------


def test_end_to_end_approved_purchases_drive_cafe_consumed_cogs(
    milk_item: CafeItem, beans_item: CafeItem, period_start: date, period_end: date
) -> None:
    """Full slice-06 seam.

    The cafe SKU prices are set the real way — by approving purchase
    receipts through the slice-03 pipeline, which writes
    ``last_known_price`` into the ``ApprovalBook`` and yields a stored
    ``Purchase``. The cost book is then built via ``CostBook.from_book``
    (no seeding). The inventory engine turns the opening/closing counts
    plus the period's approved purchases into consumed quantity and COGS
    per cafe SKU.

      milk:  5000 + 10000 − 3000 = 12000 ml @ 0.025 → 300 THB
      beans: 2000 + 5000 − 1000  = 6000 g  @ 2     → 12000 THB
    """
    book = ApprovalBook()
    skus = {
        "milk-fresh": Sku(sku_id="milk-fresh", name="Fresh milk", unit="ml"),
        "beans-arabica": Sku(sku_id="beans-arabica", name="Arabica beans", unit="g"),
    }
    milk_result = _approve_purchase(
        supplier_id="phuket-dairy",
        on=date(2026, 6, 15),
        sku_id="milk-fresh",
        per_unit_qty=D("10000"),
        unit_price=D("0.025"),
        skus=skus,
        book=book,
    )
    beans_result = _approve_purchase(
        supplier_id="phuket-coffee",
        on=date(2026, 6, 10),
        sku_id="beans-arabica",
        per_unit_qty=D("5000"),
        unit_price=D("2"),
        skus=skus,
        book=book,
    )
    purchases = [milk_result.purchase, beans_result.purchase]
    assert all(p is not None for p in purchases)  # both approvals stored a purchase

    cost = CostBook.from_book(book)
    beginning = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("5000"), timestamp=period_start),
        CafeStockCount(sku_id="beans-arabica", quantity=D("2000"), timestamp=period_start),
    ]
    ending = [
        CafeStockCount(sku_id="milk-fresh", quantity=D("3000"), timestamp=period_end),
        CafeStockCount(sku_id="beans-arabica", quantity=D("1000"), timestamp=period_end),
    ]

    results = compute_cafe_consumed_cogs(
        items=[milk_item, beans_item],
        beginning=beginning,
        ending=ending,
        purchases=purchases,
        cost=cost,
    )

    by_sku = {r.sku_id: r for r in results}
    assert by_sku["milk-fresh"].purchased_quantity == D("10000")
    assert by_sku["milk-fresh"].consumed_quantity == D("12000")
    assert by_sku["milk-fresh"].unit_cost == D("0.025")
    assert by_sku["milk-fresh"].cogs == D("300")
    assert by_sku["beans-arabica"].purchased_quantity == D("5000")
    assert by_sku["beans-arabica"].consumed_quantity == D("6000")
    assert by_sku["beans-arabica"].unit_cost == D("2")
    assert by_sku["beans-arabica"].cogs == D("12000")

    # Sanity: every result row is a CafeConsumedCogs carrying all the
    # numbers the monthly P&L (slice 08) will need.
    for r in results:
        assert isinstance(r, CafeConsumedCogs)
        assert r.unit == ("ml" if r.sku_id == "milk-fresh" else "g")


# --- helpers ----------------------------------------------------------------


def _purchase_line(
    sku_id: str, description: str, quantity: Decimal, unit_price: Decimal
):
    from tangerine.types import PurchaseLine

    return PurchaseLine(
        sku_id=sku_id,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
    )


def _approve_purchase(
    *,
    supplier_id: str,
    on: date,
    sku_id: str,
    per_unit_qty: Decimal,
    unit_price: Decimal,
    skus: dict[str, Sku],
    book: ApprovalBook,
):
    """Approve a single-line purchase at a given per-unit price.

    Same shape as the slice-04 test helper: a quantity of ``per_unit_qty``
    units at ``unit_price`` THB each, zero VAT, reconciles through the
    sum-check. On approval the line's ``(sku_id, supplier_id)`` price is
    recorded in ``book``, and the stored ``Purchase`` is returned via the
    ``ApprovalResult`` so the inventory engine can consume it.
    """
    line_total = (per_unit_qty * unit_price).quantize(D("0.01"))
    receipt = ExtractedReceipt(
        supplier_id=supplier_id,
        invoice_date=on,
        lines=(
            ExtractedReceiptLine(
                description=f"{sku_id} purchase",
                quantity=per_unit_qty,
                unit_price=unit_price,
                sku_id=sku_id,
            ),
        ),
        vat=D("0"),
        total=line_total,
    )
    checked = check_receipt(receipt, skus=skus, reference_prices={})
    return apply_decision(checked, ReceiptDecision(decision=ReceiptState.APPROVED), book)
