## Parent

- #14 (Wave 1 — The 9am Review Spine)

## What to build

The third vertical slice of Wave 1. Wire the existing `SyncOrchestrator` (slice 02 of the engine) to real Loyverse and expose it both as a UI button and a cron-runnable script.

End-to-end behavior:

- A **"Sync now" button** on the review page POSTs to `/sync`, which runs the existing `SyncOrchestrator.sync_sales_and_menu()` against real Loyverse.
- Loyverse credentials (access token, store id) come from **environment variables** — never in the database or the repo.
- The orchestrator writes results into the SQLite store from Slice 1.
- The `/sync` response is an **HTML fragment** that swaps into the page showing the sync result: how many sales ingested, how many menu changes, any errors.
- A standalone script (`python -m tangerine.sync`) does the same work, for cron to invoke nightly.
- The **first sync backfills** the last 30 days of sales so the 7-day rolling average has data immediately rather than reporting zeros for the first week.
- Sync runs are **idempotent** — re-running (manual button press after a successful cron, or overlapping page ranges) never double-counts a sale. Idempotency is by `(receipt_number, line_id)` at the SQLite level (unique constraint from Slice 1).

Routes / scripts added:

- `POST /sync` — trigger a sync now (HTMX form). Returns an HTML fragment describing the result.
- `python -m tangerine.sync` — cron entrypoint. Same sync function as the route; differs only in invocation.

The engine's `SyncOrchestrator` is called unchanged. The Loyverse HTTP client (`LoyverseHttpClient`) is unchanged — credentials come in via the existing `LoyverseCredentials` config object, populated from env.

The cron job itself is wired up in Slice 6 (deployment); this slice only needs the script to exist and be runnable.

## Acceptance criteria

- [ ] Clicking "Sync now" on the review page triggers a real Loyverse sync and writes results into the SQLite store.
- [ ] The button swaps to "Syncing..." while in flight (HTMX indicator).
- [ ] On completion, the result fragment shows rows ingested, menu changes, and any errors.
- [ ] `python -m tangerine.sync` runs the same sync from the command line.
- [ ] First-run backfill pulls the last 30 days of sales (configurable).
- [ ] Re-running a sync (manual press after cron, or overlapping ranges) does not duplicate sales.
- [ ] Loyverse credentials come from environment variables, not the database or repo.
- [ ] A sync that hits a Loyverse auth error surfaces a readable error in the result fragment rather than crashing the app.
- [ ] UI seam tests extended to cover the `/sync` route — the Loyverse HTTP boundary is stubbed using the existing `StubHttp` pattern from `tests/test_loyverse_sync_e2e.py`.
- [ ] Tests assert on the result fragment's contents (rows ingested, menu changes) and on the persisted sales in the SQLite store.

## Blocked by

- #16 (Slice 2 — FastAPI app + Jinja2 daily review)
