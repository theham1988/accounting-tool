# 02 — Loyverse API sync (sales, items, menu storage)

## What to build

Replace the seeded sales data with real data polled from the Loyverse API. The pipeline (built in slice 01) must now pull real sales, items, and menu state from Loyverse on a configurable schedule (default: daily after close).

The Loyverse client is treated as an external boundary — the end-to-end test seam from slice 01 should be extended so that sales can be injected as synthetic Loyverse payloads (no live HTTP in tests).

Menu changes (new items, price changes, renames, discontinuations) are stored and timestamped. Margin numbers computed between a menu change and the next sync are explicitly accepted as stale until sync — this is documented behaviour, not a bug, given the daily review cadence.

Items sold in Loyverse that have no recipe mapping (recipes are slice 04) surface as "unmapped" so they are visible immediately.

## Acceptance criteria

- [ ] Loyverse API client exists and authenticates via stored credentials
- [ ] Sales are polled on a schedule and stored with their Loyverse transaction timestamp
- [ ] Items and menu state are polled and stored; menu-change history is preserved with timestamps
- [ ] End-to-end test feeds synthetic Loyverse payloads and asserts that stored sales match
- [ ] Items sold without a recipe mapping are flagged as "unmapped" in output
- [ ] Polling cadence is configurable; default is daily after close

## Blocked by

- 01 — Pipeline skeleton with seeded single-item margin
