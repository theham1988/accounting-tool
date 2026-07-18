# ADR-0004: Wave 2 drops OCR; reporting runs on recipe-cost COGS

Date: 2026-07-02

## Status

Accepted.

## Context

Wave 1.5 (ADR-0003) made the cost book trustworthy — config moved into
SQLite, an in-browser editor replaced the YAML-plus-PR workflow, and a
per-entry `vat_inclusive` flag fixed the latent gross-not-net bug. The
engine already contains, as E2E-tested CLI contracts, the accrual monthly
P&L (issue 08), keg inventory (05), cafe stock counts (06), receipt
ingestion/approvals (03), cash drawer (09), anomaly detection (10), and
admin checklists (12) — none yet surfaced in the UI. Wave 2 is where the
UI meets these engines.

The original plan wired the monthly P&L to **accrual COGS**
(`beginning inventory + purchases − ending inventory`), which requires
per-purchase *transactions* — the input the receipt/OCR flow (issue 03)
was to feed — plus physical inventory counts (keg weighs, cafe stock
counts). The cash-flow/payables view (PRD user story 24) is built entirely
from `purchases`.

The partner has decided Wave 2 will **not use OCR**, and wants the wave
to focus on **reporting and UX**, not new capture flows. Dropping OCR does
not touch the cost book (Wave 1.5's cost editor supplies per-unit net
prices without receipts), but it removes the `purchases` input, which
takes down three things at once: cafe accrual COGS (the deliveries term
vanishes, so a mid-month milk delivery makes consumption go negative), the
cash-flow/payables view (no invoices), and the reference-price checks
(stories 4–5, no `last_known_price`). Bar (keg) accrual COGS survives —
kegs *are* inventory, `beginning_volume − ending_volume`, no purchases
term — but wiring it in drags a capture flow back into a reporting-focused
wave.

Five sub-decisions were reached during design. Each is hard to reverse,
surprising without context, and the result of a real trade-off.

## Decision

**1. Wave 2's reporting surfaces use recipe-cost COGS, not accrual.**
Monthly/period COGS = Σ over sold items of (recipe cost at the net price
in effect on the sale's date × units sold) — the daily review's math,
aggregated over the period. The accrual engines (`monthly_pnl.py`'s
purchases/inventory path, `keg_inventory.py`, `cafe_stock.py`) stay built
and E2E-tested but are **dormant**: not wired to any surface this wave.
The cash-flow/payables view (PRD story 24) is dropped — it has no meaning
without `purchases`. Unmapped items are handled as the daily view handles
them (revenue excluded from headline totals, surfaced in a needs-attention
section), because recipe-cost COGS is unknown for them; the accrual view's
reason for *including* unmapped revenue (consumption-derived COGS catches
their cost regardless of the sale) no longer applies.

Rejected: keep accrual and surface the capture flows minus OCR (Wave 2
becomes capture UX, not reporting; manual purchase entry is the exact
labor OCR was to remove). Rejected: hybrid bar-accrual + cafe-recipe-cost
(one capture flow plus two COGS methodologies on one P&L; deferred to a
future wave if beer-yield accuracy justifies the keg-weigh ritual).

**2. Every reporting surface costs sales at as-of-sale-date prices.**
Each sale is costed at the net price in effect on its date, reconstructed
from `audit_log` (each cost edit snapshots the row's old/new
`price_per_unit_net` + `changed_at`); pre-cutover sales use the seed
price. The daily review, the period view, and the monthly view share one
as-of-date lookup, so they agree by construction. This also corrects a
latent Wave 1 behaviour: the daily review previously costed at *current*
price, so viewing an old day — or editing a cost before the 9am review —
re-costed history. One truth, not two — the principle ADR-0003 applied to
VAT, now applied to pricing.

Rejected: current-price (reuses the daily engine verbatim but re-states
history after every price change — the two-truths failure ADR-0003
rejects). Rejected: period-average (hides the change point, still
disagrees with the daily view's day-of numbers).

*Amendment (2026-07-18): a SKU's first-ever price reaches back.* As-of
pricing assumed every cost edit is a *repricing*; it had no answer for a
cost row *created* after the SKU's sales had already happened (an
always-unmapped item finally authored), so those days stayed flagged
unknown-price forever — a bulk authoring session visibly failed to heal
history. Now a date before every recorded change resolves to the first
change's before-value (the seed price) as before, but when that first
change created the row, the first-ever price reaches back over the
unknown days instead of answering nothing. History there was unknown, not
different — the first known price is the only honest number available,
so every surface heals on the next render with no backfill job. A later
repricing still governs only from its own day, and a creation undone by
revert does not reach back (the row was declared a mistake). Healing is
silent — no per-row label; the audit log stays the paper trail. Pushing a
*correction* of an already-existing price into the past remains out of
scope (a wrong seed price still poisons history; revisit if one bites).

**3. Fixed costs are recurring + day-apportioned for sub-month periods.**
A fixed cost is recurring (defined once, auto-applies each month) or
one-off (entered for a period); fixed costs remain entity-level, never
allocated to a segment. A calendar-month P&L shows full net profit. A
sub-month arbitrary period (e.g. the last 7 days) shows fixed costs
day-apportioned — `(days in range / days in month) × monthly amount` — on
a clearly-labelled "estimated fixed costs (apportioned)" line, with the
resulting net profit labelled as an estimate. Apportionment is a
documented estimate (utilities are not truly linear); the un-apportioned
monthly number remains the honest one. This is new engine work (recurring
+ apportionment) beyond the existing `(amount, category, period)` model.

Rejected: net profit only on calendar months, sub-month stops at
contribution margin (honest, less engine work, but "last 7 days" never
shows net profit). Rejected: manual per-period entry (re-entering rent
every month is the repetitive entry the tool exists to avoid).

**4. The reporting surface is one page with Day/Period/Month/Trends
modes; drill-down is zoom; Admin is the second destination.** The daily
review stays home at `/` (Wave 1 user story 19 preserved). A top control
switches modes (HTMX swap of one report shape). Drill-down is linear zoom
across modes — period → days (Day mode for that date) → an item's period
performance — each step a deep-linkable URL with a breadcrumb (Review ›
Jul › 14 Jul › Cappuccino). A mapped item's row carries a separate "edit
recipe" link to Admin. The Wave 1.5 config surfaces (`/skus`, `/items`,
`/upload`, `/audit`) collapse into the Admin destination. Two top-level
destinations, mobile-clean.

Rejected: a dashboard home that branches to each surface (adds a click
between "open the tool" and "see the review"; revises story 19).
Rejected: a flat 8-item top nav bar (crowded on mobile, daily review stops
being home).

**5. Trends render as server-rendered SVG sparklines + CSS bars, no
client JavaScript.** ADR-0002's no-SPA / no-build-pipeline stance is
unchanged; its escape hatch (a JS lib on one route) is not invoked. The
data volume (~30 points/month) is scanning, not interactive exploration;
clickable bars carry the drill-down. No vendored JS, no build step,
offline-capable, one language end-to-end.

Rejected: a self-hosted JS chart lib on the trends route (the ADR-0002
escape hatch — bounded and legitimate, but unjustified at this data
volume; revisit if interactive hover/zoom/series-toggle becomes wanted).
Rejected: tables only (a 12-week trend reads slower as a column of
numbers than as a shape).

## Consequences

- **The accrual/inventory/receipt engines go dormant.** `monthly_pnl.py`
  (accrual path), `keg_inventory.py`, `cafe_stock.py`, `approvals.py`,
  and `receipts.py` remain built with their E2E tests green but drive no
  UI. A future reader will see fully-built, tested accrual code beside a
  separate recipe-cost reporting engine — this ADR is the "why." Reviving
  accrual (if OCR or manual purchase entry is later adopted, or beer-
  yield accuracy demands keg weighs) is additive: wire the dormant
  engines to surfaces, no rework of recipe-cost reporting.
- **Cash-flow/payables (PRD story 24) and reference-price checks (stories
  4–5) are dropped** pending a future wave that introduces purchases.
- **Wave 2 scope is reporting + UX only.** Cash drawer close (issue 09),
  anomaly flags (10), and admin checklists (12) are deferred to a later
  wave — they need capture (cash counts) and a sync extension (voids are
  not in the Wave 1 sync surface), which contradicts the reporting focus.
  Their engines stay built and tested.
- **As-of-date pricing adds a price-history lookup** built from
  `audit_log`. This is new engine work but no new capture — the audit log
  already records every cost edit. Pre-cutover sales use the seed price
  (no finer history, and no daily review exists pre-cutover, so no
  two-truths conflict).
- **The daily view's numbers change** for any day viewed after a price
  edit on a later day. This is the latent bug being fixed, not a
  regression: previously such a day was costed at the wrong (later)
  price; now it is costed at its own day's price.
- **Sub-month net profit is an estimate.** The label makes this explicit;
  calendar-month net profit remains exact. The 10K/day goal comparison is
  net-profit-based for month/period views (honest) and gross-margin-based
  for the daily view (the existing simplification — fixed costs are not
  daily).
- **`CONTEXT.md` gains four entries** — COGS recognition, As-of-date
  pricing, Fixed costs, Reporting periods and modes.

## Considered and rejected

- **Keep accrual, surface capture flows minus OCR.** Rejected: turns Wave
  2 into capture UX (keg-weigh form, stock-count form, manual purchase-
  entry form), contradicting the reporting focus; manual purchase entry is
  the labor OCR was to remove.
- **Hybrid bar-accrual + cafe-recipe-cost.** Rejected: one capture flow
  plus two COGS methodologies on one P&L, needing clear per-line labeling.
  Deferred — beer yield/loss is real money and keg weighing is cheap, so
  this remains the path back if the recipe-cost bar number ever reads
  suspicious.
- **Current-price costing.** Rejected as it re-states history after every
  price change — the two-truths failure ADR-0003 rejects.
- **Period-average costing.** Rejected as it hides the change point and
  still disagrees with the daily view.
- **Net profit only on calendar months.** Rejected as "last 7 days" then
  never shows net profit, undermining the arbitrary-period reporting
  scope.
- **Manual per-period fixed-cost entry.** Rejected as it re-entering rent
  monthly is the repetitive entry the tool exists to avoid.
- **Dashboard home / flat nav bar.** Rejected as both revise Wave 1 story
  19 ("opening the tool is the review") and/or crowd mobile.
- **JS chart library on the trends route.** Rejected as the ADR-0002
  escape hatch is unjustified at ~30-points/month scanning volume;
  server-rendered SVG covers it.

## References

- `docs/PRD.md` → user stories 22–24 (monthly P&L, accrual COGS,
  payables). This ADR's recipe-cost model supersedes the accrual basis for
  Wave 2; story 24's payables view is dropped pending purchases.
- `docs/issues/03-receipt-ingestion-pipeline.md` → the OCR flow Wave 2
  does not build.
- `docs/issues/08-fixed-costs-monthly-accrual-pnl.md` → the accrual P&L;
  the recipe-cost reporting engine replaces its surface this wave, the
  accrual engine stays dormant.
- `docs/adr/0003-config-authoring-surface-and-source-of-truth.md` → the
  no-two-truths principle this ADR reuses for pricing; the cost book this
  ADR's recipe-cost engine consumes.
- `docs/adr/0002-fastapi-jinja2-htmx-for-frontend.md` → the stack this
  ADR's reporting surfaces stay within; the escape hatch this ADR declines
  to invoke.
- `CONTEXT.md` → COGS recognition, As-of-date pricing, Fixed costs,
  Reporting periods and modes.
- Companion: `docs/PRD-WAVE-2.md` (to be drafted).
