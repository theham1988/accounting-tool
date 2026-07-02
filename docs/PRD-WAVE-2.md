# Wave 2 — The Reporting Surface

> Wave 1 built the 9am spine (persistence, identity, sync, the daily review).
> Wave 1.5 built the config authoring surface (recipes, mappings, costs in
> SQLite, edited in-browser). Wave 2 builds the **reporting surface**: the
> period, monthly, and trend views that turn one day's margin into a
> trustworthy picture of a week, a month, and a trend — on data the tool
> already has, with no new capture machinery.
>
> Cross-references: `docs/PRD-WAVE-1.md`, `docs/PRD-WAVE-1.5.md`,
> `docs/adr/0004-wave-2-recipe-cost-reporting-no-ocr.md` (the five
> hard-to-reverse decisions this wave implements), `CONTEXT.md`.

## Problem Statement

Wave 1.5 made the morning's margin numbers trustworthy — the cost book is
right, the mappings are right, the `needs_attention` strip is empty. But
the partner can only ever see **yesterday**. "How did last week do?"
"Are we on track this month?" "Is the bar's margin trending up or down
since we re-costed the spirits?" are all questions that today require
exporting the daily reviews into the old spreadsheet and summing by hand —
the exact spreadsheet the tool was built to replace.

The engine already contains an accrual monthly P&L (issue 08), keg
inventory (05), cafe stock counts (06), receipts/approvals (03), cash
drawer (09), anomaly detection (10), and checklists (12), all E2E-tested
as CLI contracts. The original plan wired the monthly P&L to **accrual
COGS**, which needs per-purchase transactions (the OCR/receipt flow) and
physical inventory counts. The partner has decided Wave 2 will **not use
OCR** and will focus on **reporting and UX**, not capture flows. ADR-0004
records the consequence: accrual COGS goes dormant, the monthly/period
view runs on **recipe-cost COGS** (the daily review's math, aggregated),
the cash-flow/payables view is dropped, and the cash/anomaly/checklist
surfaces are deferred to a later wave.

The smallest version that is genuinely useful is a **period reporting
surface** — one page that renders yesterday, an arbitrary date range, a
calendar month, and a trend in the same shape — plus **fixed-cost entry**
so the monthly view shows net profit, plus **drill-down** so a period
total can be acted on, not just admired. Everything runs on sales the
sync already pulls and a cost book Wave 1.5 already made editable.

Wave 2 is done when both partners have answered "how did last week and
last month do?" from the tool on real mornings, the monthly net profit
has been read against the 10K THB/day target, and the old spreadsheet was
not opened for those questions.

## Solution

A reporting surface added to the existing FastAPI + Jinja2 + HTMX web
app. One report page rendered in four **modes** — Day, Period, Month,
Trends — switched by a single top control. The daily review stays the
home at `/`, defaulting to yesterday (Wave 1 user story 19 preserved).
The Wave 1.5 config surfaces (`/skus`, `/items`, `/upload`, `/audit`)
collapse into a single **Admin** destination, so the app has two
top-level destinations: Review and Admin.

Underneath, three engine additions, all on data the tool already holds:

- A **recipe-cost period engine** (`build_period_review`) that costs
  every sale in a `[start, end]` range at the net price in effect on the
  sale's date, splits revenue and COGS by segment, surfaces unmapped
  items as the daily view does, and compares net profit to 10K THB/day ×
  days in the range.
- A **price-as-of-date lookup** that reconstructs a SKU's net price on
  any past date from the `audit_log` (each cost edit already snapshots
  the row's old/new `price_per_unit_net` + `changed_at`). The daily view,
  the period view, and the monthly view share this lookup, so they agree
  by construction.
- **Fixed costs**: recurring (defined once, auto-applies each month) and
  one-off, entity-level, day-apportioned for sub-month periods with an
  explicit "estimated" label.

Trends render as server-rendered SVG sparklines and clickable CSS bars —
no client JavaScript (ADR-0002 unchanged).

## User Stories

### One report, four modes

1. As a partner, I want the daily review to stay the page I land on at
   9am, so the morning ritual is unchanged.
2. As a partner, I want one control on the review page to switch between
   Day, Period, Month, and Trends — so I don't learn four different
   pages, I just change the time window on the same report.
3. As a partner, I want to pick any start and end date for the Period
   mode (last 7 days, pay-period, month-to-date), not only whole
   calendar months — so I can answer the question I actually have on the
   day I actually have it.
4. As a partner, I want Month mode to show a calendar month with the
   full net profit and the 10K THB/day × days-in-month comparison — so
   the monthly reconciliation the PRD calls for is one click away.
5. As a partner, I want every mode to share one shape (headline, segment
   CM, item detail) — so moving between day, period, and month feels
   like zooming the same view, not switching tools.

### Stable, trustworthy numbers (as-of-date pricing)

6. As a partner, I want a day's margin to stay the same whether I view it
   on its own morning or inside a monthly view three weeks later — so the
   numbers don't shift under me and I can trust them.
7. As a partner, I want editing a cost this morning *not* to change
   yesterday's 9am review — so a re-pricing flows into tomorrow's margin
   without rewriting history.
8. As a partner, I want the daily, period, and monthly views to agree on
   any day they overlap — so I never see two different margin numbers for
   the same day in two places.

### Drill-down (period → days → items)

9. As a partner, I want to click a day inside the Period/Month view and
   land on that day's review — so "what drove this week" is one click to
   the day, then one click to the item.
10. As a partner, I want each drill-down step to have its own URL and a
    breadcrumb (Review › Jul › 14 Jul › Cappuccino) — so I can share a
    link to a specific day or item and the browser back button works.
11. As a partner, I want to click a mapped item and see its performance
    over the period — units sold, revenue, recipe cost, gross margin and
    %, day-by-day, and its target-margin flag — so I can tell whether a
    single item is the whole story behind a week's number.
12. As a partner, I want a separate "edit recipe" link on a mapped item
    that jumps to its SKU in Admin — so fixing a mispriced item is one
    click from the report that surfaced it, without the report itself
    becoming an editor.

### Fixed costs and net profit

13. As a partner, I want to define recurring fixed costs once (rent,
    utilities, shared staff, insurance) with a monthly amount — so I am
    not re-entering rent every month.
14. As a partner, I want to enter one-off fixed costs for a specific
    period — so an irregular cost (a one-off repair, an insurance
    renewal) lands in the right month.
15. As a partner, I want the calendar-month P&L to show full net profit
    (segment contribution margin minus entity fixed costs) versus 10K
    THB/day × days in the month — so the monthly view answers "did the
    whole business hit the target."
16. As a partner, I want a sub-month period (last 7 days) to show fixed
    costs day-apportioned on a clearly-labelled "estimated fixed costs
    (apportioned)" line, with the net profit labelled as an estimate —
    so I get a net-profit number for any window without being lied to
    about its precision.
17. As a partner, I want fixed-cost edits recorded in the same audit log
    as recipe/cost/mapping edits, with the same revert — so the safety
    net Wave 1.5 built covers fixed costs too.

### Trends

18. As a partner, I want a Trends mode showing revenue, COGS, gross
    margin, and segment CM over time (week-over-week, month-over-month) —
    so I can see the *shape* of the business, not just one period's
    numbers.
19. As a partner, I want a day-of-week breakdown (how do Mondays compare
    to Saturdays across the period) — so I can spot structural patterns
    rather than chasing a single bad day.
20. As a partner, I want the 10K THB/day target tracked over weeks and
    months as a trend, not only the 7-day rolling average the daily view
    shows — so I can see whether we are trending toward or away from the
    goal.
21. As a partner, I want to click a bar in a trend and drill into that
    period — so a trend is a navigation surface, not just a picture.

### Navigation and Admin

22. As a partner, I want two top-level destinations — Review and Admin —
    so the app is legible on a phone and the 9am review is never buried
    under config chrome.
23. As a partner, I want the Admin destination to gather the Wave 1.5
    surfaces (SKUs, items, upload, audit) plus the new fixed-cost entry —
    so everything I edit lives in one place and everything I read lives
    in another.

## Implementation Decisions

### Modules to be built

- **`price_history.py`** — `price_as_of(sku_id, on_date) -> Money |
  None`. Reconstructs a SKU's net per-unit price on a date from the
  `audit_log`: take the `costs` row's `price_per_unit_net` history by
  walking that SKU's `audit_log` entries (`table='costs'`,
  `pk=sku_id`) in reverse; the first entry with `changed_at <= on_date`
  gives the price (its `new_value`); if none, the price is the earliest
  entry's `old_value` (the pre-edit/seed price), or the current
  `costs.price_per_unit_net` if the SKU was never edited. No new table
  required for correctness; a materialised `cost_price_history` is an
  option only if profiling demands it (data volume is small).

- **`period_review.py`** — `build_period_review(*, start, end, sales,
  recipes, cost, fixed_costs, audit_log) -> PeriodReview`. Costs each
  sale in `[start, end]` at `price_as_of(sku_id, sale.timestamp)`, splits
  revenue and COGS by segment (mapped sale → recipe's segment; unmapped →
  shift-stamped `sale.segment`, the existing slice-07 fallback), excludes
  unmapped revenue from the headline and surfaces those items in a
  `needs_attention` section (as the daily view does), sums fixed costs
  (apportioned for sub-month), and produces a goal status versus
  `DAILY_PROFIT_TARGET_THB × days_in_range`. Returns per-day rows (for
  drill-down) and per-item rows (for the item-performance view).

- **`fixed_costs.py`** — the recurring + one-off model and the
  apportionment function `fixed_costs_for_period(*, start, end,
  fixed_costs) -> (exact_or_estimate, lines)`. Recurring rows apply for
  every month from their start until `ended_at`; one-off rows apply for
  their `period`. For a calendar-month range the result is exact; for a
  sub-month range each recurring line is apportioned by
  `(days in range / days in month) × amount` and the result carries an
  `estimated=True` flag.

- **`daily_review.py` (modified)** — `build_daily_review` costs each sale
  via `price_as_of(sku_id, sale.timestamp)` instead of the current
  `CostBook.price`. A one-day period and the daily view then agree with
  `build_period_review` by construction (shared lookup, shared recipe-cost
  core). The daily view's day-specific sections (top/bottom 3 by margin
  and volume, below-target, unmapped, 7-day rolling goal) remain.

- **A sparkline helper** (`web/sparkline.py`) — given a list of `(label,
  value)` points, emits an inline `<svg>` polyline scaled to a given
  width/height, plus clickable `<a>`-wrapped bars for drill-down. Pure
  Python geometry, no client JS.

- **FastAPI routes** (the report page is one route, mode selected by
  query param so every view is a deep-linkable URL):
  - `GET /` — redirects to `/review?mode=day&day=<yesterday>` (the
    daily review stays home).
  - `GET /review?mode=day&day=YYYY-MM-DD` — Day mode (the existing daily
    review, now as-of-date priced).
  - `GET /review?mode=period&start=YYYY-MM-DD&end=YYYY-MM-DD` — Period
    mode.
  - `GET /review?mode=month&month=YYYY-MM` — Month mode (period over the
    calendar month, full net profit).
  - `GET /review?mode=item&item=<item_id>&start=...&end=...` — the
    item-performance drill-down view.
  - `GET /review?mode=trends&metric=...&span=...` — Trends mode.
  - `GET /admin` — the Admin landing (links to SKUs, items, upload,
    audit, fixed costs).
  - `GET /admin/fixed-costs` and `POST /admin/fixed-costs` — fixed-cost
    entry (recurring + one-off); edits write to `audit_log`.
  - `POST /admin/fixed-costs/{id}/delete` — stop/remove a fixed cost
    (logged).
  - The existing Wave 1.5 routes (`/skus`, `/items`, `/upload`,
    `/audit`) move under the Admin umbrella in the nav; their paths are
    unchanged to keep deep links (e.g. the daily review's
    `needs_attention` deep links to `/items?item=<id>`) working.

- **Jinja2 templates** — a base `review` template with the mode switcher
  and breadcrumb, and mode-specific bodies (`_day.html`, `_period.html`,
  `_month.html`, `_item.html`, `_trends.html`). Trends bodies emit the
  SVG sparklines and CSS bars from the helper. Mobile-first, consistent
  with the existing `review.css`.

- **HTMX wiring** — the mode switcher swaps the report body (an HTMX GET
  that replaces `#review-body`); drill-down links are ordinary `GET`s to
  deep-linkable URLs (so they work without JS too); the fixed-cost form
  is an HTMX POST that swaps the fixed-cost list fragment.

### Interfaces to be modified or extended

- `StoreSource` (`loyverse/source.py`) — gains `sales_in_range(start,
  end)` and exposes the `audit_log`/price-history accessor the
  period/daily engines need.
- `build_daily_review` — signature unchanged from the caller's view; it
  consumes `price_as_of` internally instead of `CostBook.price`.
- `CostBook` — unchanged shape; the as-of lookup reads `audit_log` +
  `costs`, not `CostBook`.
- The engine's existing twelve E2E seams are untouched. `monthly_pnl.py`
  (accrual), `keg_inventory.py`, `cafe_stock.py`, `approvals.py`,
  `receipts.py` remain built and tested but unwired (ADR-0004).

### Architectural decisions (cross-referenced)

- **Recipe-cost COGS, accrual dormant** — ADR-0004 decision 1.
- **As-of-sale-date pricing** — ADR-0004 decision 2.
- **Recurring + day-apportioned fixed costs** — ADR-0004 decision 3.
- **One page, four modes; drill-down as zoom; Admin second destination**
  — ADR-0004 decision 4.
- **SVG sparklines, no client JS** — ADR-0004 decision 5.
- **FastAPI + Jinja2 + HTMX** — ADR-0002 (unchanged; escape hatch not
  invoked).

### Schema changes (SQLite, new)

- `fixed_costs` — `id` (PK), `label`, `category`, `amount` (Decimal),
  `kind` (`'recurring'` | `'oneoff'`), `period` (YearMonth; the month a
  one-off applies to, or the first month a recurring row applies from),
  `day_of_month` (int, default 1; for recurring, informational), `ended_at`
  (date, nullable; when a recurring row stopped applying), `created_at`,
  `created_by`. Recurring rows with `ended_at IS NULL` apply every month
  from `period` forward; one-off rows apply only in `period`.
- `audit_log` — unchanged shape; fixed-cost edits write rows with
  `table='fixed_costs'`. Reverts work as for any other table.
- No new table required for price history (reconstructed from
  `audit_log`); a `cost_price_history` materialised view is deferred
  unless profiling shows the on-the-fly reconstruction is slow (unlikely
  at this data volume).

### API contracts

- `GET /` redirects to the day-mode review for yesterday.
- `GET /review?mode=...&...` returns HTML (the report body for the mode).
- `GET /admin/fixed-costs` returns HTML; `POST /admin/fixed-costs` returns
  an HTML fragment (the updated fixed-cost list) or per-field errors.
- All routes require an authenticated session cookie (unchanged from
  Wave 1).

### Specific interactions

- **9am open**: partner hits `/`, is redirected to
  `/review?mode=day&day=<yesterday>` if authenticated (login flow
  unchanged), sees the daily review.
- **Mode switch**: partner picks "Month" from the top control; HTMX GETs
  `/review?mode=month&month=<current>` and swaps `#review-body`; the
  breadcrumb and mode control update.
- **Drill to a day**: in Month mode, partner clicks the 14 Jul row;
  navigates to `/review?mode=day&day=2026-07-14` (a full page GET, deep-
  linkable, back button returns to the month).
- **Drill to an item**: in Day mode, partner clicks "Cappuccino";
  navigates to `/review?mode=item&item=cap&start=2026-07-14&end=2026-07-14`;
  the page shows the item's period performance and an "edit recipe" link
  to `/skus/<sku_id>`.
- **Fixed-cost entry**: partner opens Admin → Fixed costs, enters
  "Rent 50000/mo recurring" once; tomorrow's Month mode shows full net
  profit; a "last 7 days" Period view shows rent apportioned (~11,290)
  on a labelled estimate line.
- **Cost edit no longer re-states history**: partner edits butter's price
  on 15 Jul; the 3 Jul daily review (viewed on 16 Jul) still shows the
  3 Jul butter price, because `price_as_of` returns the price in effect
  on 3 Jul.

## Testing Decisions

### What makes a good test (inherited)

- Test external behaviour — the numbers and HTML the tool produces — not
  implementation details (how SQLite stores rows, how Jinja renders).
- Mock only genuine external boundaries: the SQLite connection (`:memory:`
  for tests). The Loyverse HTTP boundary is unchanged from Wave 1.
- Each test reads as a worked example: "given a week's sales with butter
  repriced mid-week, the Period view costs each day at that day's butter
  price and the 3 Jul day view agrees with the 3 Jul row in the period
  view."

### Seams

**One new engine seam, one extended UI seam.**

- `tests/test_period_review_e2e.py` — synthetic sales across a month with
  a mid-month price change; assertions:
  - Each sale is costed at the price in effect on its date (as-of-date,
    not current).
  - A one-day `build_period_review` agrees with `build_daily_review` for
    the same day (daily ⊂ period).
  - Segment split via recipe (mapped) and shift fallback (unmapped).
  - Unmapped revenue excluded from the headline, surfaced in
    `needs_attention`.
  - Fixed costs: exact for a calendar month; apportioned and
    `estimated=True` for a 7-day range.
  - Goal status vs `10K × days_in_range`.
  - `price_as_of` returns the seed price before the first edit, the
    edited price after, for a SKU edited once.

- `tests/test_reporting_ui_e2e.py` (extends the existing UI seam) —
  through FastAPI's test client, seeded with the migrated SQLite tables:
  - `/` redirects to yesterday's day-mode review.
  - The mode switcher renders; switching to Period/Month/Trends swaps
    the report body with the right numbers.
  - Drill-down: a period row links to `/review?mode=day&day=...`; an
    item links to `/review?mode=item&...`; the breadcrumb renders.
  - Month mode shows full net profit; a 7-day Period mode shows the
    apportioned estimate line with the "estimated" label.
  - Fixed-cost entry POST creates a recurring row, appears in the next
    Month view, and writes an `audit_log` row; revert undoes it.
  - A trend view emits inline `<svg>` sparkline polylines and clickable
    bars.
  - The daily review's numbers for an old day are stable across a later
    cost edit (the as-of-date fix).

The engine's existing twelve E2E seams are untouched. Wave 1's
persistence seam and UI seam are extended (the migrator runs in the
existing test setup; the new routes join the existing auth gate).

### Modules NOT covered by these seams

- The engine's existing slices — covered by their own seams; this wave
  doesn't touch them (the accrual engines stay dormant but tested).
- Loyverse HTTP client — unchanged from Wave 1.

## Out of Scope

The following are deliberately deferred or explicitly not being built in
Wave 2 (see ADR-0004):

- **OCR / receipt ingestion / approvals / purchases.** Dropped for this
  wave. The cost book stays partner-entered via the Wave 1.5 cost editor.
- **Accrual COGS, keg inventory, cafe stock counts, cash-flow/payables
  view.** The engines stay built and E2E-tested but dormant. Reviving
  them is additive (wire to surfaces, no rework of recipe-cost
  reporting).
- **Cash drawer close, anomaly flags, admin checklists.** Deferred to a
  later wave — they need capture (cash counts) and a sync extension
  (voids are not in the Wave 1 sync surface), which contradicts the
  reporting focus.
- **A JavaScript chart library / interactive charts.** The ADR-0002
  escape hatch is not invoked; server-rendered SVG covers the volume.
  Revisit if interactive hover/zoom/series-toggle becomes wanted.
- **Segment fixed-cost allocation.** Never (PRD); fixed costs stay
  entity-level.
- **Editing `assignees.yaml` in the UI.** Unchanged from Wave 1.5.
- **Multi-currency, VAT filing automation.** Out of scope per the
  original PRD.

## Further Notes

### Relationship to Wave 3 and the dormant engines

A later wave (call it Wave 3) would introduce **purchases** — by manual
entry, by OCR revival, or by supplier-CSV import — and could revive the
accrual engines (`monthly_pnl.py`, `keg_inventory.py`, `cafe_stock.py`)
and the cash-flow/payables view. Recipe-cost reporting stays alongside
accrual: a future monthly view could show *both* (recipe-cost theoretical
vs. accrual actual, with the variance being waste/yield loss) rather than
replacing one with the other. The same later wave is the natural home for
cash drawer, anomaly flags, and checklists.

The single accuracy loss most worth re-examining is **beer yield/loss**:
recipe-cost COGS assumes perfect pours and charges no cost for over-pour,
waste, or line leakage — and beer is the bar's biggest cost line. The
documented path back is the hybrid (keg-weigh accrual for the bar only,
recipe-cost for the cafe), which needs one cheap weekly capture flow and
is additive to everything Wave 2 builds.

### The daily view's numbers change (the fix, not a regression)

Moving the daily view to as-of-date pricing changes the margin shown for
any past day that was previously costed at a *later* price. This is the
latent Wave 1 bug being fixed (ADR-0004 decision 2): previously such a
day was costed at the wrong price; now it is costed at its own day's
price. The partner should be told to expect past days' margins to settle
to their day-of prices on cutover.

### Goal comparison basis

The 10K THB/day goal comparison is **net-profit-based** for Month and
Period modes (fixed costs included — exact for a month, apportioned
estimate for a sub-month) and stays **gross-margin-based** for the daily
view (the existing Wave 1 simplification — fixed costs are not daily).
The two bases are labelled so the difference is visible, not silent.

### Sequencing within Wave 2

Each step independently useful; each can ship on its own:

1. **Price-as-of lookup + daily view as-of pricing.** `price_history.py`
   + `build_daily_review` on as-of-date. *Verifiable: a cost edit no
   longer re-states an old day's margin; the daily view's numbers stay
   stable across a real price change.*
2. **Period engine + Period/Month modes + mode switcher.**
   `build_period_review` + the `mode=period` / `mode=month` routes and
   templates. *Verifiable: partner picks a range, sees period revenue /
   recipe-cost COGS / segment CM; picks a month, sees net profit vs
   10K × days.*
3. **Fixed-cost entry + net profit.** `fixed_costs.py` + the Admin
   fixed-cost routes + apportionment. *Verifiable: partner enters rent
   once; the monthly view shows net profit; a 7-day view shows the
   apportioned estimate.*
4. **Drill-down + breadcrumb + deep links + item-performance.** The
   `mode=day` drill from period/month, `mode=item`, breadcrumb, "edit
   recipe" link. *Verifiable: partner drills period → day → item; the URL
   is shareable; edit-recipe jumps to Admin.*
5. **Trends mode with SVG sparklines + clickable bars.** `mode=trends`
   + the sparkline helper + day-of-week breakdown + goal-over-time.
   *Verifiable: partner sees a 12-week revenue/margin trend and clicks a
   bar into that period.*

### Done-definition

Wave 2 is done when:

- Both partners have used the Period and Month views on real mornings to
  answer "how did last week / last month do?" without opening the
  spreadsheet.
- The monthly net profit has been read against the 10K THB/day ×
  days-in-month target on at least one real month-end.
- The daily view's numbers have stayed stable across at least one real
  cost edit (the as-of-date fix verified in production).
- Drill-down (period → day → item) has been used by each partner in a
  real morning review.
- A trend view has been opened and a bar clicked into a period.
- Fixed costs are entered (rent at minimum) and the monthly net profit
  reads sensibly.
- `CONTEXT.md`'s four new entries and ADR-0004 are referenced from the
  reporting surface's documentation.

Code-complete is necessary but not sufficient — there is a one-to-two-
week dogfooding period after the period/month views are usable, same
shape as Waves 1 and 1.5.
