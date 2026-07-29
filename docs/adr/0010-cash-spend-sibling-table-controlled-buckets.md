# ADR-0010: Cash-basis supplier spend lives in a sibling table; buckets are controlled vocabulary

Date: 2026-07-29

## Status

Accepted.

## Context

Parent issue #82 ("model cash-basis supplier spend") grilling produced five
locked decisions (A–E) that this slice (#96) encodes in code, schema, and
glossary. This ADR records them; it does not re-open them.

The tool has reported **recipe-cost COGS** since ADR-0004 (decision 1): each
sold item is costed at the net recipe price in effect on its sale date. That
engine answers "what did the coffee we sold *cost* to make?" but never "what
did we *pay* Makro this month, and for what?" The HTML P&L the partner
reconciles against has a line — "Cost of goods — purchases (cash)" — with a
per-bucket breakdown (taps / kitchen / coffee / bakery / staff / rent) that
no engine in the tool has ever produced. Every cost-side ticket in the Wave 2
map (#79) hangs off what this slice decides a "cash-spend row" *is*.

Two reference surfaces must already exist for a cash-spend row to point at
them: the **suppliers** table (#94) and the **spend buckets** controlled
vocabulary (#95). Both landed before this slice; this ADR assumes them.

Five sub-decisions were reached during the #82 grilling. Each is
hard-to-reverse (new table + migration), surprising without context (why a
sibling table and not a new `fixed_costs.kind`? why controlled vocabulary and
not free-form? why leave the accrual types built?), and the result of a real
trade-off.

## Decision

**A — Granularity: one row per invoice-bucket; multi-bucket bills are N
sibling rows, no parent.** When a single Makro tax invoice crosses coffee
beans and taps glassware, the partner enters it as **N independent rows
sharing `date` + `supplier_id` but differing `bucket_id` + `amount`**. There
is no parent row and no `invoice_id` grouping column. The invoice total is a
**derived fact**, never stored: `SUM(amount) WHERE date=X AND supplier_id=Y`
reconstructs it.

The UI may offer an "add another bucket on this bill" affordance that
pre-fills date + supplier, but that is a typing convenience only — the
storage shape is N independent audited writes (see decision on atomicity
below). The dormant `receipts.py` sum-check (which compared an extracted
invoice total against its line items) is **permanently gone**, consistent
with ADR-0004 dropping OCR: there is no extracted invoice total to sum-check
against, and the partner is the source of truth for what each row's amount
is.

Rejected: a parent `invoice` row with child line-items (a real schema, but
re-introduces the line-item + sum-check machinery ADR-0004 just retired, and
imposes a two-screen entry flow on the partner for the common one-bucket
case). Rejected: a denormalised `invoice_total` column (a stored fact that
can drift from the sum of its rows; the sum is trivially computable when
needed).

**B — Fields: `date`, `supplier_id`, `description`, `bucket_id`, `amount`
(gross-as-paid), `vat_inclusive` (default false).** The `amount` column
stores the THB amount **as the partner paid it** — gross when the bill is
VAT-inclusive, net when it is not. The aggregation layer (a pure function,
see below) divides by 1.07 only when `vat_inclusive` is set, mirroring
ADR-0003 decision 4's rule for the cost book. VAT-ness is a property of the
*purchase*, not the supplier or the bucket, because the same SKU bought from
a VAT-registered supplier (Makro, ARO) on one occasion and a wet-market
stall on another carries a different flag each time.

The `vat_inclusive` column **ships now with a UI surface** (a checkbox on the
entry form), unlike the original #82 plan which deferred the UI. Shipping
both together avoids the irreversible trap of having to backfill "was this
gross or net?" for rows recorded between now and whenever a later slice would
have wired the UI — the same rule ADR-0003 decision 4 applied to the cost
book ("default false so the migration never makes a number worse by guessing
wrong").

Rejected: storing net and capturing gross in the form (the invoice-total
reconstruction story breaks — `SUM(amount)` no longer reconstructs what the
partner paid). Rejected: VAT-ness on the supplier (wrong granularity; the
same supplier can issue VAT and non-VAT invoices under different business
names, and the partner pays whoever the receipt names).

**C — Where it lives: a new `cash_spend` sibling table, not an extension of
`fixed_costs` with a new `kind`.** Four reasons that all pull the same
direction:

1. **Day-apportionment is wrong for cash spend.** `fixed_costs_for_period`
   apportions a monthly cost across a sub-month range (a 4,200 THB rent bill
   viewed over 7 days contributes ~1,000 THB). A cash purchase belongs to its
   own *date* — a 4,200 THB Makro bill is 4,200 THB whether the period is one
   day or seven, and a row outside `[start, end]` is excluded entirely. A
   sibling table lets each engine apply its own time semantics without the
   other's conditionals leaking in.
2. **The recurring/ended-at lifecycle is wrong.** A fixed cost has a period
   (`[valid_from, ended_at]`); a cash purchase has a *date*. Forcing the
   cash-purchase date into the fixed-cost period columns would leave
   `ended_at` permanently null and overload `valid_from` to mean "the day
   this happened."
3. **Cash spend has a supplier FK; fixed costs do not.** Rent, salaries, and
   utilities don't have a vendor row in the suppliers table (the landlord is
   not a recurring `Supplier`). Adding a nullable `supplier_id` to
   `fixed_costs` to accommodate cash spend would make the column
   half-meaningful across the combined table.
4. **Cash spend aggregates COGS-side (below revenue, segment-able by
   bucket); fixed costs aggregate entity-overhead-side (above the segment
   line).** They live on different P&L lines and answer different questions.

The two tables share only the shape "it has a THB amount" — not enough to
justify merging. The table is named `cash_spend`, **not `purchases`**: that
name collides with the dormant accrual `Purchase` type (which carries
line-item + sum-check semantics from the OCR story) and would mislead readers
into thinking the accrual engine had been wired in.

Rejected: a `fixed_costs.kind = 'cash_purchase'` extension (fails all four
reasons above; one shared table, four wrong axes). Rejected: a single
unified `costs` table with a `cost_class` discriminator (the same four
reasons, plus it re-opens the cost book that ADR-0003 stabilised).

**D — Bucket axis: controlled vocabulary (#95), pre-seeded with the HTML's
six, cash-spend-only.** The `bucket_id` column is FK → `spend_buckets`, a
table whose rows (`taps`, `kitchen`, `coffee`, `bakery`, `staff`, `rent`)
landed in #95 seeded from the HTML the partner reconciles against. Buckets
are **product-family / cost-category, never segment** — "taps" means
*bar-product-family spend*; which *segment* a bucket's cost falls against is
a downstream P&L computation against recipes (ADR-0007 pure-clock
segmentation), not a fact of the purchase. A cash-spend row carries no
segment field by design.

This is the trap the glossary entry for "Spend bucket" exists to prevent:
readers conflating bucket (a cost-category key on the purchase) with segment
(a revenue-side attribution derived from the clock). They are different axes
on different sides of the P&L.

Rejected: free-form bucket text (no aggregation guarantee; the HTML's six
buckets drift into "taps", "tap", "bar", "bar supplies"). Rejected: a bucket
column that doubles as a segment (collapses two distinct concepts; violates
ADR-0007's rule that segments come from the clock, not the item).

**E — Dormant receipts/approvals: leave built, leave unchanged.** The
accrual-side modules (`receipts.py`, `approvals.py`) and types
(`ExtractedReceipt`, `Purchase`, `PurchaseLine`, `CheckedReceipt`,
`ReceiptState`, `LineFlag`) stay built and E2E-tested, driving no surface.
`Supplier` is **reused in place** from `types.py` (where it already lives)
— no relocation, no new type. Deleting the dormant accrual code is a
separate ticket with its own scope (it touches ADR-0004 and deletes E2E
tests); this slice does not do it.

The consequence is two `Supplier`-keyed concepts in the codebase for now:
the live cash-spend FK target and the dormant accrual purchase target.
They share the type by design — when (or if) the accrual story is revived,
the supplier list is already the right one.

Rejected: delete `receipts.py` / `approvals.py` now (out of scope; separate
ticket; touches ADR-0004). Rejected: relocate `Supplier` into a new
`suppliers.py` module (churn with no behavioural payoff; the type already
lives where both the live and dormant code find it).

### Two implementation decisions this slice made (not from the grilling)

These surfaced during implementation and are recorded here so the next
reader doesn't re-derive them.

**F — Referential integrity is declarative `REFERENCES` plus application-level
in-use guards, mirroring #94 and #95.** SQLite is opened with
`PRAGMA foreign_keys` at its default (OFF) in this codebase, so the
`REFERENCES` clauses in migration 0011 are declarative — they document intent
and would enforce if the PRAGMA were flipped, but the engine does not
enforce them today. Actual protection against deleting a supplier or bucket
that cash-spend rows reference comes from the existing `supplier_in_use` /
`spend_bucket_in_use` guards in `SqliteConfigStore`, which the delete routes
already call. This matches exactly what #94 and #95 shipped; flipping the
PRAGMA repo-wide is a separate decision.

**G — Multi-bucket bill entry is N independent audited writes, not a single
`batch()` stroke.** The `batch()` atomic-stroke helper exists for the
sold-as-is quick-create flow, and the issue left its use here optional. The
first cut enters each row of a multi-bucket bill as its own audited write —
each lands in `/audit` independently and reverts independently. A partial
failure mid-multi-bucket-bill would leave some rows written and others not,
which the partner resolves by entering the missing rows (the audit log shows
what made it). Promoting multi-bucket entry to an atomic stroke is a later
slice if the partner reports the partial-failure case as a real pain.

## Consequences

- **A new `cash_spend` table lands** (migration 0011), FK-declarative against
  `suppliers` and `spend_buckets`. The migration is idempotent and runs
  against the existing production database without data loss (the table is
  empty on cutover; there is no historical cash-spend data to backfill).
- **Two more FK targets exist for the in-use guards.** `supplier_in_use`
  (from #94) and `spend_bucket_in_use` (from #95) now query the real
  `cash_spend` table; their forward-looking test stubs (which simulated the
  table before it existed) are updated in this slice to insert against the
  real schema.
- **The recipe-cost COGS engine (ADR-0004) is unchanged.** This slice adds
  the *purchases* line, it does not alter the *consumption* line. The two
  answer different questions and coexist on the P&L.
- **The HTML's "Cost of goods — purchases (cash)" line is unblocked** for
  #81 (cash-basis P&L), #84 (Profit-Report screen), and #85 (two-lens P&L).
  Those tickets consume `cash_spend_for_period` over `cash_spend_rows()`;
  neither this engine nor this table knows about them.
- **The `vat_inclusive` column is live from day one** with a UI checkbox,
  not deferred. Every cash-spend row recorded from cutover on carries the
  partner's explicit gross/net choice — there is no backfill window.
- **Two dormant `Supplier`-keyed code paths now coexist** (live cash-spend,
  dormant accrual purchases). This is intentional and temporary; the
  accrual revival or deletion is a separate ticket.
- **The five sub-decisions are independently reversible in principle but
  coupled in practice.** A sibling table whose rows don't carry a
  `vat_inclusive` flag would re-open the backfill trap (B + C); a controlled
  bucket vocabulary with no sibling table to consume it has no purpose
  (C + D). They ship together.

## Considered and rejected

- **Parent invoice row with child line-items.** Rejected as it re-introduces
  the line-item + sum-check machinery ADR-0004 retired and imposes a
  two-screen flow on the common one-bucket case.
- **A denormalised `invoice_total` column.** Rejected as a stored fact that
  can drift from the sum of its rows; the sum is trivially computable.
- **Storing net, capturing gross in the form.** Rejected as it breaks the
  `SUM(amount)` invoice-total reconstruction and silently changes what the
  amount column means.
- **VAT-ness on the supplier.** Rejected as wrong granularity — the same
  supplier can issue VAT and non-VAT invoices, and the partner pays whoever
  the receipt names.
- **`fixed_costs.kind = 'cash_purchase'` extension.** Rejected as it fails
  all four reasons for a sibling table (day-apportionment, lifecycle,
  supplier FK, COGS-side aggregation) at once.
- **Free-form bucket text.** Rejected as it loses the aggregation guarantee
  and lets the HTML's six buckets drift into spelling variants.
- **A bucket column that doubles as a segment.** Rejected as it collapses
  two distinct concepts and violates ADR-0007's rule that segments come from
  the receipt clock, not the item.
- **Deleting `receipts.py` / `approvals.py` / the dormant accrual types in
  this slice.** Rejected as out of scope; it touches ADR-0004 and deletes
  E2E tests, and deserves its own ticket.
- **Relocating `Supplier` into a new module.** Rejected as churn with no
  behavioural payoff; the type already lives where both live and dormant code
  find it.
- **Flipping `PRAGMA foreign_keys=ON` repo-wide to make the REFERENCES
  clauses enforced.** Rejected for this slice; it is a repo-wide decision
  that would change behaviour for every existing FK and deserves its own
  ADR. The in-use guards are the enforced protection today.
- **Atomic `batch()` multi-bucket entry.** Rejected for the first cut as
  over-engineering; N independent audited writes are simpler, each reverts
  cleanly, and the partial-failure case is partner-recoverable. Revisit if
  reported as a real pain.

## References

- `CONTEXT.md` → Cash-spend row, Spend bucket, Supplier, VAT model (new and
  amended entries land with this slice)
- ADR-0003 decision 4 — the VAT-default-false rule this slice's
  `vat_inclusive` column inherits
- ADR-0004 decision 1 — recipe-cost COGS, the consumption-side engine this
  slice's purchases-side engine coexists with; decision 3 — the fixed-costs
  admin surface precedent this slice's `/admin/cash-spend` mirrors;
  Consequences — the dormant receipts/approvals story this slice's decision
  E leaves unchanged
- ADR-0007 — pure-clock segmentation, the rule that keeps buckets
  product-family and segments clock-derived (the trap decision D + the
  glossary entry exist to prevent)
- ADR-0009 — the precedent for documenting a configurable vocabulary that
  ships with a sensible default (the spend-bucket seeded six)
- Parent: #82 ("model cash-basis supplier spend")
- This slice: #96
- Blockers: #94 (Suppliers), #95 (Spend buckets)
