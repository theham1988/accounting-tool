# Tangerine Phuket — Bar & Cafe Accounting Tool

Accounting tool for the Tangerine Phuket dual-concept venue (cafe 8am–5pm, bar 5pm–10pm).
See [`docs/PRD.md`](docs/PRD.md) for the full product brief.

## Tech stack

- **Language**: Python 3.12+
- **Test framework**: pytest
- **Type checking**: mypy (strict)
- **Layout**: `src/` layout, single package `tangerine`

## Getting started

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -e ".[dev]"
```

## Running the pipeline

```bash
python -m tangerine
```

## Running tests

```bash
pytest
```

## Type checking

```bash
mypy
```

## Status

- **Slice 01** — pipeline skeleton with seeded single-item margin. See
  [`docs/issues/01-pipeline-skeleton-single-item-margin.md`](docs/issues/01-pipeline-skeleton-single-item-margin.md).
- **Slice 02** — Loyverse API sync (sales, items, menu history). See
  [`docs/issues/02-loyverse-api-sync.md`](docs/issues/02-loyverse-api-sync.md).
- **Slice 03** — receipt ingestion pipeline (sum-check, reference-price and
  SKU-mapping checks, approval queue, `last_known_price` updates). See
  [`docs/issues/03-receipt-ingestion-pipeline.md`](docs/issues/03-receipt-ingestion-pipeline.md).
- **Slice 04** — recipe and per-item cost engine (recipes per SKU with yield,
  Loyverse item → SKU mapping, cost derived from latest approved price). See
  [`docs/issues/04-recipe-and-item-cost-engine.md`](docs/issues/04-recipe-and-item-cost-engine.md).
- **Slice 05** — keg inventory via weekly weighing (per-brand tare + density,
  `(gross − tare) ÷ density` volume, consumed volume × cost-per-ml accrual
  COGS, actual-vs-theoretical loss %). See
  [`docs/issues/05-keg-inventory-weekly-weighing.md`](docs/issues/05-keg-inventory-weekly-weighing.md).
- **Slice 06** — cafe stock counts → accrual COGS (per-item count cadence,
  `consumed = beginning + purchases − ending`, priced at the latest approved
  price; standalone period result for the monthly P&L). See
  [`docs/issues/06-cafe-stock-counts-accrual-cogs.md`](docs/issues/06-cafe-stock-counts-accrual-cogs.md).
- **Slice 07** — segment tagging (cafe vs bar) and per-segment contribution
  margin. See
  [`docs/issues/07-segment-tagging-and-contribution-margin.md`](docs/issues/07-segment-tagging-and-contribution-margin.md).
- **Slice 08** — fixed costs entry and monthly accrual P&L (entity-level fixed
  costs, accrual-basis COGS per segment, entity net profit vs the 10K THB/day
  target, separate cash-flow view). See
  [`docs/issues/08-fixed-costs-monthly-accrual-pnl.md`](docs/issues/08-fixed-costs-monthly-accrual-pnl.md).
- **Slice 09** — cash drawer reconciliation with 5pm handoff recount (per-shift
  variance, recount-gated shift start). See
  [`docs/issues/09-cash-drawer-reconciliation-handoff.md`](docs/issues/09-cash-drawer-reconciliation-handoff.md).
- **Slice 10** — rules-based anomaly detection over voids + drawer variance
  (void-rate vs venue median, peak-hour void clustering, drawer-short rate,
  three-short-shift run). See
  [`docs/issues/10-anomaly-detection-voids-drawer.md`](docs/issues/10-anomaly-detection-voids-drawer.md).
- **Slice 11** — daily 9am review view: yesterday's revenue / COGS / gross
  margin, per-segment contribution margin with red flags, top/bottom items by
  margin and volume, below-target and unmapped item flags, anomaly flags
  passthrough, and a 7-day rolling average vs the 10K THB/day target. See
  [`docs/issues/11-daily-9am-review-view.md`](docs/issues/11-daily-9am-review-view.md).
- **Slice 12** — admin checklists + partner task assignment: the daily 9am
  review checklist (five steps) and the weekly admin checklist (four rituals),
  each task assignable to a specific partner with partner-specific availability
  windows so the night-shift partner is never asked to act at 9am or 10pm.
  See
  [`docs/issues/12-admin-checklists-partner-task-assignment.md`](docs/issues/12-admin-checklists-partner-task-assignment.md).

## Loyverse sync

Sales and menu state are pulled from the Loyverse API (`https://api.loyverse.com/v1.0`)
on a configurable schedule (default: daily after close). The client authenticates
with a single bearer access token issued from Loyverse's back-office Integrations page.

The HTTP boundary is injected, so tests feed synthetic Loyverse payloads without
live HTTP — see [`tests/test_loyverse_sync_e2e.py`](tests/test_loyverse_sync_e2e.py)
for the contract.

```python
from tangerine.loyverse.config import LoyverseCredentials
from tangerine.loyverse.http import LoyverseHttpClient
from tangerine.loyverse.store import InMemoryLoyverseStore
from tangerine.loyverse.sync import SyncOrchestrator

client = LoyverseHttpClient(LoyverseCredentials(access_token="...", store_id="..."))
store = InMemoryLoyverseStore()
SyncOrchestrator(client=client, store=store).sync_sales_and_menu()
```

## Receipt ingestion

Uploaded receipts are turned into stored purchases through a three-check
pipeline: a **sum-check** (lines + VAT must reconcile to the total within
tolerance, else auto-reject), a **reference-price check** (a line whose unit
price deviates >5% from the last-known price for its (SKU, supplier) is
flagged), and a **SKU-mapping check** (lines with no SKU are always queued).
Receipts that pass the sum-check land in a partner approval queue; approving
(or correcting-then-approving) promotes them to a stored `Purchase` and
updates `last_known_price` for each mapped line.

The OCR/LLM provider is the only genuine external boundary. Tests feed
`ExtractedReceipt` payloads directly — see
[`tests/test_receipts_e2e.py`](tests/test_receipts_e2e.py) for the contract.

```python
from tangerine.approvals import ApprovalBook, apply_decision
from tangerine.receipts import check_receipt
from tangerine.types import ReceiptDecision, ReceiptState

checked = check_receipt(extracted, skus=skus, reference_prices=book.price_snapshot())
result = apply_decision(checked, ReceiptDecision(decision=ReceiptState.APPROVED), book)
```

## Recipes and per-item cost

Recipes are defined per **SKU** (a formula: inputs + a yield) and Loyverse
items map to SKUs via a `SkuMapping`. Each ingredient's current cost is
looked up from the `ApprovalBook` (supplier-agnostic — the latest approved
price wins), so a re-pricing after the next receipt approval flows straight
into margin without the recipe changing. The margin engine produces a
per-item table (cost/unit, margin, margin %, sell volume, target-margin
flags). Items with no recipe, or whose recipe references an unpriced SKU,
are flagged and excluded from the daily totals — their COGS is unknown, so
their revenue is surfaced separately as `flagged_revenue` rather than booked
as margin.

See [`tests/test_recipes_e2e.py`](tests/test_recipes_e2e.py) for the
contract.

```python
from tangerine.cost import CostBook
from tangerine.margin import compute_item_margins
from tangerine.recipes import RecipeCatalog

cost = CostBook.from_book(book)
margins = compute_item_margins(sales=sales, recipes=RecipeCatalog(recipes), cost=cost, day=day)
```

## Keg inventory

Weekly keg weighing turns a physical measurement into a beer-volume number,
which is the periodic-inventory input that makes accrual COGS work. Per brand,
a `KegBrand` carries the empty-keg tare weight and a density approximation
(defaulting to water density, 1.0 g/ml, with a documented ~0.5–1.5% volume
tolerance surfaced on every report row rather than silently absorbed). Each
weekly `KegWeighIn` records the aggregate gross weight across the brand's
kegs; the beer volume is `(gross − tare) ÷ density`.

A period runs from one weigh-in to the next. The beer consumed over the period
is `beginning_volume − ending_volume`, and its accrual COGS is consumed volume
× the brand's current cost per ml (looked up supplier-agnostic from the same
`CostBook` slice 04 uses). Actual yield (Loyverse rung-up beer ml, resolved
through the recipe catalog) vs theoretical yield (the consumed volume) gives
the loss %; the variance is surfaced but not attributed to individual kegs.
A brand whose only weigh is the very first one is reported as `unstarted` —
its volume seeds the next period's beginning inventory.

This slice produces the inventory/COGS numbers; slice 08 (monthly accrual
P&L) wires them into the books. See
[`tests/test_keg_inventory_e2e.py`](tests/test_keg_inventory_e2e.py) for the
contract.

```python
from tangerine.cost import CostBook
from tangerine.keg_inventory import compute_keg_inventory
from tangerine.recipes import RecipeCatalog
from tangerine.types import KegBrand, KegWeighIn

report = compute_keg_inventory(
    brands=[KegBrand(brand_id="chang", name="Chang Draught",
                     beer_sku_id="chang-keg", tare_weight_g=Decimal("5000"))],
    weigh_ins=[KegWeighIn("chang", week1, Decimal("25000")),
               KegWeighIn("chang", week2, Decimal("20000"))],
    sales=sales, recipes=RecipeCatalog(recipes),
    cost=CostBook.from_book(book), period_end=week2,
)
# report.rows[0].volume_consumed_ml, .accrual_cogs, .loss_pct
```

## Cafe stock counts → accrual COGS

Perishable cafe items (milk, beans, pastries) are tracked by physical
stock counts. Each item carries its own count cadence by shelf life
(`daily`/`weekly`). Consumed quantity for a period is the accrual-COGS
primitive `beginning + purchases − ending`, priced at the SKU's latest
approved price — the number the monthly P&L books. This is a standalone
period result; the daily 9am view keeps using recipe-based margins.

A SKU with no approved price is flagged `unpriced`: its consumption is
still surfaced, but COGS is reported as zero rather than silently booked
(matching the recipe engine's `unknown_price` convention).

See [`tests/test_cafe_stock_e2e.py`](tests/test_cafe_stock_e2e.py) for
the contract.

```python
from tangerine.cafe_stock import compute_cafe_consumed_cogs
from tangerine.types import CafeCountCadence, CafeItem, CafeStockCount

items = [CafeItem(sku_id="milk-fresh", name="Fresh milk", unit="ml", cadence=CafeCountCadence.DAILY)]
beginning = [CafeStockCount(sku_id="milk-fresh", quantity=Decimal("5000"), timestamp=period_start)]
ending = [CafeStockCount(sku_id="milk-fresh", quantity=Decimal("3000"), timestamp=period_end)]
results = compute_cafe_consumed_cogs(
    items=items, beginning=beginning, ending=ending, purchases=purchases, cost=cost,
)
## Segments and contribution margin

Every transaction, recipe, and item is tagged `cafe` or `bar` (PRD
"Segmentation"). The default source is the Loyverse **category** (carried on
the recipe); the **shift-timestamp fallback** tags sales whose item has no
recipe — the Loyverse parser resolves `8am–5pm` to cafe and everything else
(5pm–10pm plus out-of-hours) to bar from the receipt's `created_at`, because
that is the only place the time-of-day lives.

Per segment, per period: revenue, variable costs (= COGS today; direct labor
is "if tracked" and not tracked yet), and **contribution margin = revenue −
variable costs**. Fixed costs are deliberately **not** allocated to segments
(PRD user story 20) — they live at entity level only (slice 08) — so the
segment's only profitability number is its contribution margin. A segment is
flagged **red** when its contribution margin for the period is `< 0`.

Flagged margin rows (unmapped / unknown-price) are excluded from segment CM
for the same reason they are excluded from the daily roll-up: their COGS is
unknown, so booking their revenue as CM would over-state the segment. Both
segments are always reported; a segment with no reliable sales carries zeros.

See [`tests/test_segment_contribution_margin_e2e.py`](tests/test_segment_contribution_margin_e2e.py)
for the contract.

```python
from tangerine.margin import compute_daily_margin, compute_period_segment_margins

daily = compute_daily_margin(source, day)
for sm in daily.segment_margins:
    print(sm.segment, sm.contribution_margin, sm.is_red)

# Any inclusive period (issue 07: "for any period"):
period = compute_period_segment_margins(source, start=day1, end=day2)
```

## Fixed costs and monthly accrual P&L

The monthly reconciliation view (PRD user story 23) is built on proper
**accrual-basis COGS** — `beginning inventory value + purchases − ending
inventory value` — rather than the recipe-based margins the daily 9am view
uses. Per segment, the monthly contribution margin is
`revenue − accrual_cogs`; the bar's accrual COGS comes from slice 05 (keg
weigh-ins) and the cafe's from slice 06 (cafe stock counts). The monthly
engine calls both internally, so a caller passes raw inventory inputs and
gets a single `MonthlyPnl`.

**Fixed costs** are recorded against the entity (the whole business), never
against a segment (PRD user story 20), and are matched to a `(year, month)`
period. Entity net profit is the sum of segment contribution margins minus
the month's fixed costs; that is compared against the 10,000 THB/day target
scaled by days in the month (issue 08 AC).

A separate **cash-flow view** reports payables recognised by invoice date
(PRD user story 24), so the accounting view (COGS by consumption) and the
cash-flow view (when bills are due) are both available — the two are
genuinely different numbers when a delivery lands in one month but is mostly
consumed in another.

See [`tests/test_monthly_pnl_e2e.py`](tests/test_monthly_pnl_e2e.py) for the
contract.

```python
from tangerine.monthly_pnl import compute_monthly_pnl
from tangerine.types import FixedCost, FixedCostCategory

pnl = compute_monthly_pnl(
    month=(2026, 6),
    sales=sales, recipes=RecipeCatalog(recipes), cost=CostBook.from_book(book),
    brands=brands, weigh_ins=weigh_ins,
    cafe_items=cafe_items, cafe_beginning=opening_counts, cafe_ending=closing_counts,
    purchases=purchases,
    fixed_costs=[FixedCost(amount=Decimal("30000"),
                           category=FixedCostCategory.RENT, period=(2026, 6))],
)
# pnl.segment_pnl[0].contribution_margin   # per-segment accrual CM
# pnl.entity_net_profit                    # segment CM sum − fixed costs
# pnl.goal.met / .surplus                  # vs 10K THB/day × days in month
# pnl.cash_flow.total_payables             # payables by invoice date
```

## Cash control and anomaly detection

There is no on-site manager, so the tool does the segregation-of-duties work
a manager would otherwise do (PRD "Known control gap"). Each shift close
captures the drawer variance `closing − (opening + rung_up)`, and the 5pm
handoff requires the incoming partner to recount the outgoing partner's
drawer; a recount mismatch outside tolerance blocks shift start (default
tolerance 0 THB — the recount is *the* control moment).

On top of that history, rules-based anomaly detection (slice 10) flags the
patterns a manager would catch: a cashier whose **void rate** exceeds the
venue median, **void clustering** at peak hours, a **drawer-short rate** above
a chosen threshold, and a run of **three short shifts in a row** by the same
cashier. The "three in a row" rule keys per-cashier — the two partners
alternate day/night shifts, so their closes are always interleaved, and a
rule that broke the streak on any other cashier's close would never fire in
the real rotation. Flags carry the cashier, the period, and a readable
sentence describing the offending pattern, ready for the 9am review (slice
11) to surface.

The Loyverse `/voids` endpoint (distinct from refunds) is not yet wired into
a store; slice 02 only parses SALE/REFUND receipts. Slice 10 consumes a
minimal `Void` boundary type and a caller-supplied per-cashier sales count,
so the detector is fully testable now; a later slice parses raw Loyverse
`/voids` payloads into the same `Void` shape.

See [`tests/test_anomaly_detection_e2e.py`](tests/test_anomaly_detection_e2e.py)
for the contract.

```python
from tangerine.anomaly import AnomalyConfig, detect_anomalies

flags = detect_anomalies(
    config=AnomalyConfig(
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
        drawer_short_rate_threshold=Decimal("0.25"),
    ),
    voids=voids, closes=closes, sales_counts={"alice": 20, "bob": 20},
)
# flags[i].kind, .cashier_id, .observed, .reference, .detail
```

## Daily 9am review

The single daily surface (PRD user story 29; issue 11). A partner opens it at
9am and sees, in one fast-scan view, everything that needs attention from
yesterday: revenue, COGS, gross margin; per-segment contribution margin with
red flags where CM < 0; top/bottom items by margin and by units sold; items
whose actual margin is below their set target; items sold without a recipe
mapping; anomaly flags from slice 10; and a 7-day rolling-average gross margin
vs the 10,000 THB/day target.

The review composes the slice-04 daily margin engine (financial numbers + per-
segment CM + per-item margin flags) with the slice-10 anomaly detector
(yesterday's window only). It does not widen the `Source` ingestion boundary
— voids, shift closes, and per-cashier sales counts are passed in explicitly,
so a review for a day with no cash/void data still builds cleanly (its anomaly
section is just empty).

**Goal-comparison number.** The 7-day rolling average is the daily
`total_gross_margin` (= sum of segment CMs today). Direct labor is not tracked,
and fixed costs are deliberately not daily-allocated (PRD user story 20 / slice
08 — they land at entity level on the monthly view only). So the 9am goal
progress is a contribution-margin view, not a net-profit view; the monthly P&L
(slice 08) carries the net-profit comparison.

**Rankings.** Top/bottom lists cap at three items each. Flagged rows (unmapped
or unknown-price) are excluded from the rankings because their margins are
meaningless; they surface in the `unmapped_items` section instead.

See [`tests/test_daily_review_e2e.py`](tests/test_daily_review_e2e.py) for the
contract.

```python
from tangerine.daily_review import build_daily_review

review = build_daily_review(
    source=source,
    review_date=date(2026, 6, 24),
    closes=closes, sales_counts={"alice": 20}, drawer_short_rate_threshold=Decimal("0.25"),
)
# review.revenue, .cogs, .gross_margin
# review.segment_margins[i].contribution_margin, .is_red
# review.top_by_margin.items / .bottom_by_margin.items / .top_by_volume / .bottom_by_volume
# review.below_target_items, .unmapped_items, .anomaly_flags
# review.goal.rolling_average, .target, .met, .surplus
```

## Admin checklists + partner task assignment

Structured checklists for the partner admin rituals, so nothing gets skipped
under shift pressure (PRD user stories 28-31; issue 12). Two checklists:

- **Daily 9am review checklist** — the five steps a partner works through
  when they open slice 11's view (open the review, review segment flags,
  review margin anomalies, review cash/void flags, mark done). The checklist
  names the steps; it does not embed slice 11's numbers — it is the ritual,
  decoupled from yesterday's data.
- **Weekly admin checklist** — the four weekly rituals (keg weigh, cafe
  stock count, receipt approval queue cleared, fixed cost entry).

Each task is assignable to a specific partner, and each partner carries its
own availability windows so the night-shift partner is never asked to act at
9am (asleep) or 10pm (after close). The model is role-agnostic: a partner is
just an `Assignee` with `AvailabilityWindow`s, so onboarding a future manager
is data, not a code change.

This is the first slice that needs state across time. The shape is a pure
`build_checklists` function plus a thin in-memory `CompletionLog` of
`CompletionEntry` rows (mirrors slice 03's `ApprovalBook` pattern). A skipped
task surfaces in subsequent sessions (`skipped_for` names the original skip's
date) so it cannot be silently lost; a completion records only against its
own occurrence date.

See [`tests/test_admin_checklists_e2e.py`](tests/test_admin_checklists_e2e.py)
for the contract.

```python
from datetime import date, time

from tangerine.checklists import CompletionLog, build_checklists, complete_task, skip_task
from tangerine.types import Assignee, AvailabilityWindow, ChecklistKind, TaskTemplate

day = Assignee(assignee_id="daniel", name="Daniel (day)",
               windows=(AvailabilityWindow(weekday=0, start=time(8, 0), end=time(17, 0)),))
night = Assignee(assignee_id="noi", name="Noi (night)",
                 windows=(AvailabilityWindow(weekday=0, start=time(14, 0), end=time(17, 0)),))

log = CompletionLog()
log = complete_task(log=log, task_id="keg-weigh", occurrence_date=date(2026, 6, 29),
                    assignee_id="daniel")
log = skip_task(log=log, task_id="receipt-queue", occurrence_date=date(2026, 6, 29),
                assignee_id="noi", reason="no receipts this week")

set_ = build_checklists(assignees=[day, night], anchor=date(2026, 6, 29), completion_log=log)
# set_.daily.tasks[i].state          # PENDING / DONE / SKIPPED
# set_.weekly.tasks[i].window.start  # the assignee's availability window
# set_.weekly.tasks[i].skipped_for   # carried-over skip's original date
```
