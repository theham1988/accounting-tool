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
- **Slice 06** — cafe stock counts → accrual COGS (per-item count cadence,
  `consumed = beginning + purchases − ending`, priced at the latest approved
  price; standalone period result for the monthly P&L). See
  [`docs/issues/06-cafe-stock-counts-accrual-cogs.md`](docs/issues/06-cafe-stock-counts-accrual-cogs.md).

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
```
