## Parent

- #14 (Wave 1 — The 9am Review Spine)

## What to build

The first vertical slice of Wave 1. Replace the engine's in-memory store and seeded data sources with a real persistence layer and a real config loader, then exercise the engine end-to-end against them via the existing CLI.

End-to-end behavior:

- `python -m tangerine` loads **recipes**, **SKU mappings**, and **current SKU prices** from YAML config files at startup (paths configurable; sensible defaults like `config/recipes.yaml` and `config/costs.yaml`).
- The loader produces a `RecipeCatalog` and a `CostBook` with the same shapes the engine already accepts.
- A **SQLite-backed `LoyverseStore`** implementation satisfies the existing `LoyverseStore` protocol (`record_sales`, `record_menu_snapshot`, `sales`, `current_menu`, `menu_change_history`).
- A **`Source` adapter** wraps the SQLite store, the loaded recipes, and the loaded cost book into an object satisfying the engine's existing `Source` Protocol.
- The CLI (`python -m tangerine`) calls `build_daily_review(...)` against the persisted data and prints the review, exactly as it does today against the seeded source.
- Restarting the process and re-running it reads the same sales back from SQLite. Refreshes work.

The engine itself is **unchanged**. This slice only adds: a SQLite store implementation, a config loader, a Source adapter, and updates the CLI to use them.

Configuration validation fails loudly at startup on malformed YAML, unknown SKU references, or missing required fields. The tool does not start in a half-working state.

The SQLite file lives at a configurable path (default e.g. `./tangerine.db`). Connection configuration comes from environment variables, not the repo.

Schema mirrors the frozen dataclasses in `types.py`:

- `sales` table — one row per `Sale`, with a unique constraint on `(receipt_number, line_id)` for idempotent sync (used by Slice 3).
- `menu_snapshots` and `menu_changes` tables — the timestamped menu history the `LoyverseStore` protocol already models.

A simple forward-only migration runner applies SQL files at startup (no need for Alembic at this scale).

## Acceptance criteria

- [ ] Loading recipes/SKU mappings/costs from a config file produces a `RecipeCatalog` and `CostBook` equivalent to constructing them inline.
- [ ] Malformed config at startup raises immediately with a readable error; the app does not start.
- [ ] A `LoyverseStore` backed by SQLite satisfies the existing protocol — `record_sales`, `record_menu_snapshot`, `sales`, `current_menu`, `menu_change_history` all behave per the contract tested in `tests/test_loyverse_sync_e2e.py`.
- [ ] Idempotency holds at the SQLite level: re-inserting a `SaleRecord` with the same `(receipt_number, line_id)` does not duplicate it.
- [ ] Menu snapshot diffing produces the same `MenuChange` history as the in-memory store does.
- [ ] A `Source` adapter wrapping the SQLite store + loaded recipes + loaded cost book satisfies the engine's `Source` Protocol.
- [ ] `python -m tangerine` prints the daily review against data read from SQLite, identical to today's seeded output when the config matches today's seeded values.
- [ ] Killing the process and re-running produces the same review numbers (data persisted).
- [ ] Persistence seam tests added (e.g. `tests/test_sqlite_store_e2e.py`) using in-memory SQLite (`:memory:`), readable as worked examples, mocking only the SQLite connection.
- [ ] No mocking of internal modules; engine functions exercised for real.

## Blocked by

- None — can start immediately.
