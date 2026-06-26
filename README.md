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
