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

## Deployment

The tool is served over HTTPS from a cloud VPS (nginx terminates TLS and
reverse-proxies to uvicorn under systemd; cron runs the nightly sync and a
nightly SQLite snapshot; the login route is rate-limited and an admin route
downloads a database backup). The full reproducible runbook — provisioning,
secrets, systemd, nginx, certbot, cron, snapshots, recovery — lives in
[`DEPLOY.md`](DEPLOY.md), with the ops files under [`deploy/`](deploy/).

## Status

### Engine slices (01–12)

The accounting engine — twelve E2E-tested contracts. Pure computation over
frozen dataclasses; no persistence, identity, or UI of its own.

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

### Wave 1 — The 9am Review Spine (UI build) — complete

The full operational UI wrapped around the engine: real persistence, identity,
nightly sync, the daily 9am review surface, and a reproducible cloud deployment.
The engine is unchanged — Wave 1 builds the spine every later wave sits on. See
[`docs/PRD-WAVE-1.md`](docs/PRD-WAVE-1.md).

- **Wave 1 · Slice 1** — SQLite-backed `LoyverseStore` + a YAML config loader
  (recipes, SKU mappings, current SKU prices) + a `Source` adapter, exercised
  end-to-end through the CLI. See
  [`docs/issues/wave-1-slice-1.md`](docs/issues/wave-1-slice-1.md).
- **Wave 1 · Slice 2** — FastAPI + Jinja2 + HTMX web layer rendering
  yesterday's daily 9am review as a mobile-first HTML page. See
  [`docs/issues/wave-1-slice-2.md`](docs/issues/wave-1-slice-2.md).
- **Wave 1 · Slice 3** — Loyverse sync wiring: a "Sync now" button
  (`POST /sync`) and the `python -m tangerine.sync` cron entrypoint, idempotent
  with a 30-day first-run backfill. See
  [`docs/issues/wave-1-slice-3.md`](docs/issues/wave-1-slice-3.md).
- **Wave 1 · Slice 4** — Identity: shared-passphrase login + role selector
  (Daniel / Noi), signed-cookie sessions with an inactivity timeout, gating
  every route except `/login`. See
  [`docs/issues/wave-1-slice-4.md`](docs/issues/wave-1-slice-4.md).
- **Wave 1 · Slice 5** — UX polish: day navigation (`/review?day=YYYY-MM-DD`),
  the sync in-flight indicator and result fragment, and readable empty/error
  states for the dogfooding period. See
  [`docs/issues/wave-1-slice-5.md`](docs/issues/wave-1-slice-5.md).
- **Wave 1 · Slice 6** — Deployment hardening: systemd + nginx + Let's Encrypt
  TLS, nightly cron sync, nightly rotated SQLite snapshots, login
  rate-limiting, and the login-gated `GET /admin/db-snapshot` backup route —
  full runbook in [`DEPLOY.md`](DEPLOY.md). See
  [`docs/issues/wave-1-slice-6.md`](docs/issues/wave-1-slice-6.md).

### Wave 1.5 — The Config Authoring Surface — complete

The in-browser editor that lets a non-coding partner see which Loyverse items
are mapped and which SKUs are priced, and fix the gaps without touching YAML
or git. Config moves out of the seed files into SQLite (the YAML becomes
seed-only), with an audit-and-revert safety net replacing the old code-review
gate. See [`docs/PRD-WAVE-1.5.md`](docs/PRD-WAVE-1.5.md) and
[`docs/adr/0003-config-authoring-surface-and-source-of-truth.md`](docs/adr/0003-config-authoring-surface-and-source-of-truth.md).

- **Migration + SQLite config tables** — `skus`, `recipes`,
  `recipe_ingredients`, `costs`, `mappings`, `audit_log`; the YAML loader
  becomes a first-run seeder; the engine reads config from SQLite.
- **SKU view + item coverage view** — mapping health (green/yellow/red),
  recipe completeness, dangling-SKU detection, and a gap report sorted so
  unmapped items bubble to the top.
- **Cost editor + spreadsheet upload** — pack price + quantity +
  `vat_inclusive` → derived net per-unit cost; bulk CSV/XLSX upload with a
  pre-filled template and per-row error reporting.
- **Recipe editor** — `(ingredient, quantity)` rows with an inline
  "create new ingredient" sub-form, unit-shorthand conversion (`1 tbsp` →
  15 ml), and a live per-row + total cost preview.
- **Audit log + revert + daily-review diff link** — every edit records
  who/when/old/new, with per-change and per-session revert and an "N changes
  since last review" link on the 9am view.
- **Recipe-model refinements (issues #34–#38)** — unified decimal yield,
  SKU roles (purchasable / produced / prep), fully derived costing
  (recurse into preps, no leaf-price-wins;
  [`docs/adr/0005-derived-costing.md`](docs/adr/0005-derived-costing.md)),
  and sold-as-is quick-create. See the "Recipes and per-item cost" and
  "Sold-as-is quick-create" sections below.

### Wave 2 — The Reporting Surface — complete

The period, monthly, and trend views that turn one day's margin into a
trustworthy picture of a week, a month, and a trend — on data the tool
already holds, with no new capture machinery. The monthly/period view runs
on **recipe-cost COGS** (the daily review's math, aggregated); accrual COGS,
receipts/OCR, keg/cafe inventory, cash drawer, and anomaly surfaces stay
built and tested but dormant (ADR-0004). See
[`docs/PRD-WAVE-2.md`](docs/PRD-WAVE-2.md) and
[`docs/adr/0004-wave-2-recipe-cost-reporting-no-ocr.md`](docs/adr/0004-wave-2-recipe-cost-reporting-no-ocr.md).

- **Price-as-of-date lookup** — reconstructs a SKU's net price on any past
  date from the `audit_log`, so the daily, period, and monthly views agree
  by construction and editing a cost never re-states an old day's margin.
- **Period engine + Period/Month modes** — `build_period_review` costs every
  sale at its day's price, splits revenue/COGS by segment, and compares net
  profit to 10K THB/day × days in range; one report page switches between
  Day, Period, Month, and Trends via a single mode control.
- **Fixed costs + net profit** — recurring (defined once, auto-applies each
  month) and one-off entity-level costs, day-apportioned for sub-month
  periods with an explicit "estimated" label; edits share the audit-log
  revert.
- **Drill-down + breadcrumb + deep links** — period → day → item navigation,
  each step its own shareable URL, plus an item-performance view and an
  "edit recipe" jump into Admin.
- **Trends mode** — server-rendered SVG sparklines and clickable CSS bars
  (no client JS), a day-of-week breakdown, and the 10K target tracked over
  weeks and months.

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
items map to SKUs via a `SkuMapping`. The margin engine produces a per-item
table (cost/unit, margin, margin %, sell volume, target-margin flags). Items
with no recipe, or whose recipe references an unpriced SKU, are flagged and
excluded from the daily totals — their COGS is unknown, so their revenue is
surfaced separately as `flagged_revenue` rather than booked as margin.

### SKU roles (issue #35)

Every SKU has a **role** derived from its relations, so the model stays
honest without a redundant type field:

- **purchasable** — no recipe; its cost is a directly-entered price.
- **produced** — has a recipe; its cost is *derived* from that recipe.
- **prep** — a produced SKU whose output is usable as an ingredient in other
  recipes. This is the one stored fact: a boolean `prep` flag on the recipe.
  Usage is the declaration (a recipe whose output another recipe references
  is auto-flagged), so the old `prep-` naming convention no longer matters.

The recipe editor's ingredient picker offers only purchasables and preps —
never sold-only dishes — and recipe save rejects cycles transitively (a prep
cannot contain itself directly or through other preps), naming the loop.

### Unified yield (issue #34)

A recipe's yield is a single decimal `yield_qty` denominated in the output
SKU's own unit, with a `yield_estimated` marker. Cost per output unit is
`batch cost ÷ yield_qty` at every level. **Estimated** yields recompute from
the sum of the recipe's weight/volume inputs on each row edit (count-unit
inputs like leaves or eggs are excluded from the sum); **measured** yields
are fixed until a partner edits them. Zero or negative yields are rejected,
and yield edits are audited and revertable.

### Derived costing (issues #36 / #37, ADR-0005)

A produced SKU's cost is **always derived from its recipe, never typed
directly** — one source of truth per role. `CostResolver` resolves a SKU's
per-unit cost:

- a **purchasable** takes its price from the `CostBook`;
- a **produced** SKU is `Σ(ingredient qty × unit_cost) ÷ yield_qty`,
  **recursing through preps down to purchasables**.

Resolution is memoised per costing pass (a sauce used by eight dishes is
walked once) and cycle-safe. **No leaf-price-wins**: a stale direct price on
a produced SKU is ignored, not honoured. **Unknown price propagates
recursively** — a prep with an unpriced leaf makes every dish using it flag
`unknown_price`. Because the resolver runs against the as-of-date cost book,
re-pricing a leaf reprices a prep-containing dish for later days only.

Direct-cost entry is rejected for produced SKUs on both cost-entry seams (the
web cost form returns `400`; a CSV upload row for a produced SKU is a
per-row error). Their SKU page shows a read-only derived cost plus a
per-ingredient breakdown (`cost_breakdown`, preps shown as single priced
rows) so a partner can trace a dish's cost down to receipts, or see which
leaf is unpriced. Deleting a recipe flips its SKU back to purchasable and
restores the cost-entry form (audited, revertable). The offline cost
spreadsheet (`scripts/build_cost_spreadsheet.py`) imports the same
`CostResolver`, so the offline and running tools can never disagree.

See [`tests/test_recipes_e2e.py`](tests/test_recipes_e2e.py) and
[`docs/adr/0005-derived-costing.md`](docs/adr/0005-derived-costing.md) for
the contract and rationale.

```python
from tangerine.cost import CostBook
from tangerine.margin import CostResolver, compute_item_margins
from tangerine.recipes import RecipeCatalog

recipes = RecipeCatalog(recipes)
cost = CostBook.from_book(book)

# Derived per-unit cost, recursing through preps to purchasables:
resolver = CostResolver(recipes=recipes, cost=cost)
sauce_cost = resolver.unit_cost("tomato-sauce")     # None if any leaf is unpriced

margins = compute_item_margins(sales=sales, recipes=recipes, cost=cost, day=day)
```

## Sold-as-is quick-create (issue #38)

Directly-sold purchasables (beer, wine, soft drinks) cost through the same
`item → SKU → recipe` path as every dish — there is no second costing path
in the engine. From an unmapped Loyverse item's row, one action
(`GET`/`POST /items/{item_id}/sold-as-is`) creates the purchasable SKU
(receipt-priced), a one-line serving recipe, the produced sold SKU the recipe
outputs (auto-named `<purchasable>:served`, inheriting the item's segment so
the contribution-margin view stays honest), and the item mapping.

No migration and no engine change were needed: the existing recipe schema and
`CostResolver` already model a serving recipe (one ingredient line, yield 1),
so a bottle (330 ml) and a draught (473 ml of a 30 l keg) both cost through
the identical path. The generated recipe is editable afterwards, and the
purchasable is reusable as an ingredient in other recipes.

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
