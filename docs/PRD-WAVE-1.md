# Wave 1 — The 9am Review Spine

> This is the first wave of the UI build. The destination is a full operational
> UI for every engine slice; Wave 1 builds the spine — persistence, identity,
> deployment, sync, and the daily 9am review surface. Subsequent waves layer
> capture flows and controls on top of the stack Wave 1 forces into existence.
>
> Cross-references: `docs/PRD.md` (the original product brief), `CONTEXT.md`
> (glossary + resolved decisions), `docs/adr/0001-sqlite-for-persistence.md`,
> `docs/adr/0002-fastapi-jinja2-htmx-for-frontend.md`.

## Problem Statement

The accounting engine has been built slice-by-slice through twelve E2E-tested
contracts, but today it is a black box: a CLI that prints seeded data. The two
partners cannot open it at 9am and see yesterday's real margins. Receipts still
pile up in Google Drive; sales data still sits in Loyverse; actual profitability
is still invisible until someone manually cobbles together a P&L in a
spreadsheet.

The smallest version of the UI that is genuinely useful is the **daily 9am
review surface** wired to **real Loyverse data**, sitting on a real persistence
layer with real identity. That forces the whole stack into existence (database,
auth, deployment, sync, frontend framework) for one high-value surface, and
every subsequent wave builds on top.

Wave 1 is done when both partners have opened the 9am review on at least three
real mornings, the sync has run unattended those nights, and the manual
spreadsheet wasn't opened.

## Solution

A responsive web application deployed to a cloud VPS that:

- Authenticates the two partners via a shared passphrase and asks "who am I
  right now?" via a role selector (Daniel / Noi).
- Syncs sales and menu state from Loyverse on a nightly cron schedule, with a
  "Sync now" button for manual triggers (the 9:01am "cron failed" recovery
  path).
- Loads recipes, SKU mappings, and current SKU prices from config files in the
  repo at startup.
- Renders the daily 9am review against the synced sales — yesterday's revenue,
  COGS, gross margin; per-segment contribution margin with red flags; top and
  bottom items by margin and by volume; below-target items; unmapped items; and
  the 7-day rolling average vs the 10,000 THB/day target.
- Persists everything to SQLite so refreshes and cross-device access work.

The engine itself is unchanged. Wave 1 wraps it: a SQLite-backed store
implementation, a config loader, and a FastAPI + Jinja2 + HTMX web layer.

## User Stories

### Identity and access

1. As a partner, I want to open the tool in a browser from home or the venue,
   so that I can do admin during my own availability window rather than at
   fixed on-site hours.
2. As a partner, I want to log in with a single shared passphrase, so that the
   tool is gated without per-user account overhead.
3. As a partner, I want to identify myself (Daniel / Noi) via a selector when
   I log in, so that any action I take is attributed to me for the engine's
   audit trail (`cashier_id`, `assignee_id`).
4. As a partner, I want to be logged out automatically after a period of
   inactivity, so that an unattended browser does not stay authenticated.
5. As a partner, I want the tool to be served over HTTPS with a valid
   certificate, so that the passphrase is not transmitted in the clear.

### Loyverse sync

6. As a partner, I want sales and menu state to sync from Loyverse
   automatically every night after close, so that the 9am review is already
   fresh when I open it.
7. As a partner, I want a "Sync now" button in the UI, so that I can force a
   sync at 9:01am when I notice the nightly sync silently failed (clock drift,
   expired token, network blip).
8. As a partner, I want the sync to surface its result — how many sales
   ingested, how many menu changes, any errors — so that I can trust it ran.
9. As a partner, I want the first sync to backfill the last 30 days of sales,
   so that the 7-day rolling average has data immediately rather than
   reporting zeros for the first week.
10. As a partner, I want sync runs to be idempotent, so that re-running a sync
    (manual button press after a successful cron, or overlapping page ranges)
    never double-counts a sale.
11. As a partner, I want Loyverse credentials to live in an environment
    variable on the server, so that they are never in the database or the repo.

### Daily 9am review

12. As a partner, I want to open the tool at 9am and see yesterday's revenue,
    COGS, and gross margin at the top of the page, so I can scan the headline
    numbers first.
13. As a partner, I want to see per-segment contribution margin (cafe vs bar)
    with a red flag where a segment's CM is below zero, so that I notice
    immediately if half the business is failing to cover its variable costs.
14. As a partner, I want to see the top three and bottom three items by gross
    margin, so I can spot what is paying for itself and what is dragging.
15. As a partner, I want to see the top three and bottom three items by units
    sold, so I can spot what is moving and what is sitting.
16. As a partner, I want items whose actual margin is below their set target
    flagged, so that mispriced items are visible while they are still
    relevant.
17. As a partner, I want items sold without a recipe mapping surfaced in a
    dedicated "unmapped" section, so that revenue I cannot cost is visible
    rather than silently dropped.
18. As a partner, I want to see a 7-day rolling average daily gross margin
    compared to the 10,000 THB/day target with a met/missing indicator, so I
    can see whether we are trending toward the goal.
19. As a partner, I want yesterday's review to be the default view, so that
    opening the tool is the review — no navigation required.
20. As a partner, I want to be able to navigate to a previous day's review, so
    that I can look back at yesterday-if-I-missed-it or last week's pattern.
21. As a partner, I want the review to render in well under a second, so that
    the 9am scan is fast and not a chore.

### Config (recipes, SKU mappings, costs)

22. As a partner, I want recipes and SKU mappings to live in a config file
    under version control, so that recipe changes go through code review
    (recipes are high-stakes — a wrong quantity silently corrupts every
    margin number).
23. As a partner, I want current SKU prices to live in a config file in Wave 1,
    so that the cost book is populated and the review shows real margins even
    though receipt approvals are not built yet.
24. As a partner, I want the tool to fail loudly at startup if the config is
    malformed or references unknown SKUs, so that a bad deploy is caught before
    partners open the tool.

### Persistence and reliability

25. As a partner, I want all data — synced sales, menu snapshots, future
    captures — to persist in SQLite, so that nothing is lost on a process
    restart or server reboot.
26. As a partner, I want refreshes and cross-device access to work, so that I
    can start a review on my phone at the venue and finish on a laptop at home.
27. As a partner, I want the database to be backed up nightly (file snapshot),
    so that a server failure does not lose accounting history.
28. As a partner, I want to be able to download a snapshot of the database
    from an admin route, so that I can take an out-of-band backup before
    risky maintenance.

### Deployment and operations

29. As a partner, I want the tool deployed to a cloud VPS, so that I can reach
    it from anywhere (home, venue, travel) rather than only on the venue
    network.
30. As a partner, I want deploys to be reproducible from the repo, so that I
    can rebuild the server from scratch if it dies.
31. As a partner, I want the server to restart the app on crash, so that a
    transient error does not take the tool offline overnight.
32. As a partner, I want request rate-limiting on the login route, so that
    the shared passphrase cannot be brute-forced.

## Implementation Decisions

### Modules to be built

- **A SQLite-backed `LoyverseStore` implementation** that satisfies the
  existing `LoyverseStore` protocol (`record_sales`, `record_menu_snapshot`,
  `sales`, `current_menu`, `menu_change_history`). Idempotency by
  `(receipt_number, line_id)` is preserved at the SQLite level (unique
  constraint + `INSERT OR IGNORE`). Menu snapshot history is a separate table
  of `MenuChange` rows; `current_menu` is the latest snapshot's view.

- **A config loader** that reads recipes, SKU mappings, and current SKU prices
  from YAML files at startup. The loader produces a `RecipeCatalog` and a
  `CostBook` with the same shapes the engine already accepts. Validation fails
  loudly at startup on malformed YAML, unknown SKU references, or missing
  required fields.

- **A FastAPI application** with routes for:
  - `GET /` — the daily 9am review (defaults to yesterday)
  - `GET /review?day=YYYY-MM-DD` — the review for a specific day
  - `POST /sync` — trigger a Loyverse sync now (HTMX form)
  - `GET /login` and `POST /login` — passphrase + role selector
  - `GET /admin/db-snapshot` — download the SQLite file (gated behind login)

- **Jinja2 templates** rendering the engine's result objects. The daily review
  template iterates `DailyReview` fields and renders numbers, segment CM rows,
  top/bottom rankings, flags, and the goal progress indicator. CSS is mobile-
  first responsive; the same template renders on phone and desktop.

- **HTMX wiring** for the "Sync now" button (POSTs to `/sync`, swaps the
  sync-result fragment back) and the day-navigation control.

- **A `Source` adapter** that wraps the SQLite-backed store, the loaded
  recipes, and the loaded cost book into a `Source` satisfying the engine's
  existing Protocol. This is the bridge between persistence and the engine —
  `build_daily_review(source=..., review_date=...)` is called unchanged.

- **Auth middleware** that gates every route except `/login`. Sessions are
  signed cookies carrying the selected role. Inactivity timeout (e.g. 8 hours)
  expires the session.

- **A sync entrypoint script** (`python -m tangerine.sync`) that cron invokes
  nightly. The script and the `/sync` route share the same sync function;
  they differ only in invocation.

### Interfaces to be modified or extended

- The `LoyverseStore` protocol is unchanged. The SQLite implementation is a new
  class that satisfies it.
- The `Source` Protocol is unchanged. The new adapter is a new class that
  satisfies it.
- `build_daily_review`, `compute_daily_margin`, and the rest of the engine are
  unchanged.

### Architectural decisions (cross-referenced)

- **SQLite** for persistence — see ADR-0001.
- **FastAPI + Jinja2 + HTMX** for the frontend — see ADR-0002.
- **Cloud VPS** deployment — partners reach the tool from anywhere; TLS via
  Let's Encrypt; strong passphrase; rate-limited login.
- **Shared passphrase + role selector** for identity — honest about the
  two-equal-partners threat model.
- **Cron + manual sync button** for the Loyverse integration — cron is the
  reliable nightly path; the button is the 9:01am recovery.
- **Config files in the repo** for recipes, SKU mappings, and current SKU
  prices — high-stakes rarely-changed config goes through code review.

### Schema changes (SQLite, new)

Tables mirror the frozen dataclasses:

- `sales` — one row per `Sale`, keyed by `(receipt_number, line_id)` for
  idempotent sync.
- `menu_snapshots` and `menu_changes` — the timestamped menu history the
  `LoyverseStore` protocol already models.
- (Future waves add `purchases`, `keg_weigh_ins`, `cafe_stock_counts`,
  `shift_closes`, `fixed_costs`, `completion_entries` — not in Wave 1.)

The SQLite file is created on first run; migrations are forward-only SQL
files applied at startup (a simple migration runner — no need for Alembic at
this scale).

### API contracts

- `GET /` and `GET /review?day=YYYY-MM-DD` return HTML.
- `POST /sync` returns an HTML fragment describing the sync result (rows
  ingested, menu changes, errors).
- `GET /admin/db-snapshot` returns the SQLite file as a download.
- All routes except `/login` require an authenticated session cookie.

### Specific interactions

- **9am open**: partner hits `/`, is redirected to `/login` if unauthenticated,
  enters passphrase + picks role, lands on `/` showing yesterday's review.
- **Sync recovery**: partner notices stale numbers, clicks "Sync now", the
  button swaps to "Syncing...", the route runs the orchestrator, the result
  fragment replaces the button, the page is reloaded with fresh data.
- **Day navigation**: partner picks a previous date from a date input, HTMX
  GETs `/review?day=...` and swaps the review body.
- **Startup failure**: malformed config or unreachable DB at startup raises
  immediately; the app does not start in a half-working state.

## Testing Decisions

### What makes a good test (inherited from the repo)

- Test external behaviour — the numbers and HTML the tool produces — not
  implementation details (how SQLite is queried, how Jinja renders).
- Mock only genuine external boundaries: the Loyverse HTTP endpoint (via the
  existing `StubHttp` urlopen injection pattern) and the SQLite connection
  (use `:memory:` for tests).
- Each test reads as a worked example: "given yesterday's Loyverse sales of X
  with recipe Y, opening the 9am review shows gross margin Z."
- No mocking of internal modules. The engine's twelve existing E2E seams
  continue to test the math; this PRD adds two new seams for the new external
  boundaries.

### Seams

**Two new seams.** The one-seam ideal is not met because Wave 1 introduces two
distinct external boundaries (filesystem/SQLite and HTTP). The engine's
existing twelve seams are untouched.

1. **Persistence seam** (`tests/test_sqlite_store_e2e.py`) — tests the SQLite-
   backed `LoyverseStore` against the same contract the in-memory store
   satisfies. Synthetic `SaleRecord`s in, query them out, verify idempotency
   holds on replay, menu snapshots diff correctly. The genuine boundary is
   the SQLite connection (`:memory:` for tests).

2. **UI seam** (`tests/test_daily_review_ui_e2e.py`) — tests the FastAPI routes
   through FastAPI's test client. Synthetic data seeded into the SQLite store
   (and the config loader), GET `/` returns HTML containing the right numbers
   and flags (parse the response, assert on text), the "Sync now" button
   triggers the orchestrator and the response fragment describes the result.
   Login gating is tested (unauthenticated → redirect; authenticated → 200).

### Modules covered by the seams

- SQLite-backed `LoyverseStore` (persistence seam)
- Config loader (UI seam — loaded into the test app)
- FastAPI routes (UI seam)
- Jinja2 templates (UI seam — assertions on rendered HTML)
- HTMX-driven sync and day-navigation (UI seam)
- Auth middleware (UI seam)

### Modules NOT covered by these seams

- The engine itself — covered by the existing twelve E2E seams; not re-tested
  here.
- Loyverse HTTP client — covered by `test_loyverse_sync_e2e.py`; the UI seam
  stubs the orchestrator's HTTP boundary using the same `StubHttp` pattern.
- Loyverse payload parsing — covered by `test_loyverse_sync_e2e.py`.

## Out of Scope

The following are deliberately deferred to later waves or are explicitly not
being built:

- **Wave 2 surfaces**: monthly P&L, segment CM surface over arbitrary periods,
  keg weigh capture, cafe stock count capture. Wave 1 builds the spine these
  will sit on; it does not build them.
- **Wave 3 surfaces**: cash drawer close, 5pm handoff recount, receipt upload
  and approval, anomaly flags surface, admin checklists, fixed cost entry.
- **Receipt-driven cost book**: Wave 1 seeds the cost book from a config file.
  Wave 3 migrates `CostBook` to `CostBook.from_book(...)` once receipt
  approvals are live. The migration is one line; the interim is flagged with
  a TODO.
- **Receipt OCR/LLM provider choice**: unresolved in the PRD; lives in Wave 3.
  Wave 1 does not touch receipts.
- **Per-user accounts, SSO, OAuth**: the shared-passphrase + role-selector
  model is the Wave 1 identity story. Onboarding a future manager is a
  selector entry, not a code change.
- **Anomaly detection inputs (voids, closes, sales_counts)**: Wave 1's daily
  review passes empty anomaly inputs (the engine's documented behaviour — a
  day with no cash/void data has an empty anomaly section). Surfacing
  anomaly flags is Wave 3.
- **Multi-currency, VAT filing automation**: out of scope per the original
  PRD.
- **Admin UI for editing recipes, SKU mappings, or costs in-browser**: the
  config-file workflow is the Wave 1 path. A future wave may add a UI that
  writes to these files.
- **Multi-tenant or multi-instance**: the tool is single-instance (PRD).

## Further Notes

### Done-definition

Wave 1 is done when both partners have opened the 9am review on at least three
real mornings, the sync has run unattended those nights, and the manual
spreadsheet was not opened those days. Code-complete is necessary but not
sufficient — there is a one-to-two-week dogfooding period after code-complete
before Wave 2 begins, to surface UX papercuts while the spine is the only
thing on top.

### Migration path for the interim cost book

Wave 1's config-file cost book is explicitly temporary. The migration to
receipt-driven pricing in Wave 3 is:

1. Build the receipt approval flow (Wave 3).
2. Replace the config-file loader's contribution to the cost book with
   `CostBook.from_book(approval_book)` once approvals are flowing.
3. Remove the cost file (or keep it as a fallback for SKUs that have no recent
   receipt — both options are fine).

The engine change is one line; the migration is additive, not breaking.

### Two ADRs cover the hard-to-reverse decisions

- `docs/adr/0001-sqlite-for-persistence.md` — SQLite over Postgres / flat files.
- `docs/adr/0002-fastapi-jinja2-htmx-for-frontend.md` — FastAPI+HTMX over
  React SPA / Streamlit.

A third borderline ADR (the interim config-file cost book) was considered and
rejected — the decision is temporary with a documented exit, so it lives in
`CONTEXT.md` under "Cost Book (Wave 1 interim)" rather than as an ADR.

### Deployment sketch

A small cloud VPS (any provider; $5-10/month is overkill). Systemd unit runs
the FastAPI app under uvicorn; nginx terminates TLS (Let's Encrypt via certbot)
and reverse-proxies to uvicorn. Cron entry runs `python -m tangerine.sync`
nightly at 22:30 local. Nightly snapshot of the SQLite file is the backup.
Loyverse credentials and the auth passphrase live in `/etc/tangerine/env`
(mode 0600, owned by root), sourced into the systemd unit's environment.

### Sequencing within Wave 1

A reasonable build order, each step independently testable:

1. SQLite-backed `LoyverseStore` (persistence seam).
2. Config loader (recipes, mappings, costs).
3. `Source` adapter wrapping store + recipes + cost.
4. FastAPI app skeleton + auth middleware + login route.
5. Daily review route + template.
6. Sync route + cron entrypoint script.
7. Day-navigation, sync-result fragment, polish.
8. Deployment to VPS, TLS, cron, snapshots.
9. Dogfooding period (3+ mornings, both partners).
