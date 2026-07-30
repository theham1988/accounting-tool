# ADR-0011: Books is the cost source of truth; Loyverse mirrors via CSV

Date: 2026-07-30

## Status

Accepted.

Reverses nothing — it is the first cost-mirror decision, building on #69's "no
API write" ruling and #72's CSV-format facts.

## Context

The partners open the Loyverse dashboard and Books side by side every morning.
ADR-0008 made the **revenue** side agree (Books' headline ties to Loyverse Gross
sales). The **cost/COGS** side still doesn't: Books holds the recipes, the cost
book, and the net-of-VAT derivation (ADR-0003 decision 4, ADR-0005), and Loyverse
holds whatever cost a partner last typed into each item's back-office row. The two
drift, and the partner reconciles by hand or wonders which side is right.

Two prior wayfinder issues settled the mechanics this ADR stands on:

- [#69](https://github.com/theham1988/accounting-tool/issues/69) — established
  that Books does **not** write costs over the Loyverse API. The venue's token is
  read-only for items; a write token would be a fresh recovery-surface and a
  fresh trust boundary. The mirror is therefore a **back-office action a partner
  performs**, not an automated push.
- [#72](https://github.com/theham1988/accounting-tool/issues/72) — established the
  import-format facts: target column `Cost`, joined on `SKU` per variant; **blank
  cells don't overwrite**; the safe flow is a **full round-trip** (export from
  Loyverse → fill `Cost` → re-import); cost applies **forward-only** (no recost of
  historical sales); and the whole flow only holds while **Advanced Inventory is
  off** (or Track Stock is off on the items Books costs).

#70's grilling ([resolution comment](https://github.com/theham1988/accounting-tool/issues/70))
locked five decisions that #72 deliberately left open — the partner-facing
semantics: *which number, which items, what happens to Loyverse-side edits, when
the file is produced, and what paper trail the export leaves*. The implementation
landed in three slices (#101 round-trip CSV, #102 paper trail, #103 drift badge)
and the partners have exercised the flow in production. This ADR records what was
decided and why, now that its consequences have been observed rather than guessed
at.

## The gate

**Advanced Inventory is off** (and items Books costs are Track-Stock-off).
Confirmed by the partner during #70's grilling. The CSV `Cost` import only works
without Advanced Inventory (or with Track Stock off); if a future subscription
turns it on with tracked stock, `Cost` becomes a read-only Average Cost
recalculated from purchase orders and #69 must be revisited **before** this
mirror. The gate is a precondition for everything below, not a decision this ADR
makes.

## Decision

Books is the **source of truth** for per-item cost; Loyverse **mirrors** via a
partner-run CSV round-trip. The five #70 resolutions each carry their one-line
*why*:

**1. Which number — the net per-unit recipe cost (`CostResolver.cost_per_unit` for
the sold SKU), rounded 2 dp half-up at emit.**
*Why:* it is ADR-0005's single public costing face and ADR-0003 decision 4's
net-of-VAT number, so Loyverse's COGS then mirrors Books' exactly. Sending gross
would silently reintroduce the ~7% COGS understatement ADR-0003 decision 4 fixed.

**2. Which items — a round-trip file (one row per Loyverse item), joined from a
partner-supplied Loyverse items export.** Mapped+costable rows get `Cost` filled.
Mapped-unknown-price and unmapped rows get `Cost` **blank, row intact** — Loyverse's
existing cost is untouched (blank doesn't overwrite, per #72 §2). **Zero is never
emitted** — it would zero Loyverse's COGS for that item. The "Books has no number"
signal lives in Books' diff UI, not in the CSV cell.
*Why:* the partner never loses a Loyverse-side cost Books can't compute, and the
gap is honest (a "no Books cost" line, not a silent zero).

**3. Direction of truth on Loyverse-side edits — detect drift, report, overwrite
on partner confirm.** At prepare time Books diffs Loyverse's current `Cost` (read
from the same export the round-trip is built on) against its own net number, shows
the drift report ("N items differ; Loyverse X vs Books Y"), and the partner
confirms before the filled file is written. Books is source of truth; the diff is
the **visibility layer over an unconditional overwrite**, not a gate that skips
rows. The diff the partner sees *is* the audit-row payload.
*Why:* holds both the source-of-truth claim (Books wins) and ADR-0003's audit
posture (the win is visible before it happens; "we are too small to have gates").

**4. When/how produced — on-demand "Prepare Loyverse cost import" action in Admin
(beside the cost book, where the data it mirrors lives), running the
diff→confirm→emit flow, plus a drift badge: "N costs changed since last export on
\<date\>".** Partner-driven cadence — no scheduled job; the documented rhythm is
"ahead of a profitability check, not daily."
*Why:* the mirror lands at the moment a partner is about to read Loyverse
profitability, which is the only moment staleness matters; a nightly job would
produce files nobody uploads.

**5. Paper trail — a dedicated `loyverse_exports` table (migration `0012`), not a
new `kind` on `audit_log`.** One row per confirmed export: `partner_id`,
`confirmed_at`, `item_count`, `changed_count`, `drift_payload` (the per-SKU
Loyverse-vs-Books JSON diff the partner approved). A **zero-drift** export still
records a row (`changed_count = 0`, `drift_payload = "[]"`) — the null-state proof
that "the mirror was confirmed current on \<date\>" is visible, not inferred from
absence. The partner who confirms the diff also uploads the produced CSV to
Loyverse Back Office (same eyes end-to-end; consistent with ADR-0003's "too small
for gates").
*Why:* a row on `audit_log` would conflate a Loyverse-bound export with an in-Books
config edit and pollute the 9am "N config changes since last review" count that
`unreviewed_changes` drives; the dedicated table keeps the two paper trails honest.

## Consequences

- **The cost/COGS side mirrors exactly.** For costable rows, Loyverse's COGS now
  agrees with Books' COGS by construction (same `CostResolver.cost_per_unit`,
  same 2 dp rounding). Reconciliation on the cost line becomes "the two numbers
  agree", not "the two numbers disagree for an explainable reason" — the same
  property ADR-0008 bought for the revenue line.
- **Trust-boundary caveat — Books does not track Loyverse-side ingestion.** Books
  records that a file was *produced and confirmed*; it does **not** record whether
  the partner uploaded it to Loyverse Back Office, nor whether Loyverse ingested
  it. That is outside Books' trust boundary. The closed loop is next-prepare drift
  detection: if Loyverse's `Cost` doesn't match what Books last sent, drift shows
  up and the partner investigates. The drift badge therefore says **"since last
  export"**, not "since last successful Loyverse import" — the "forgot to upload"
  case understates staleness (badge may read zero while Loyverse is stale), and
  the remedy is procedural, not technical.
- **Gross-profit-number caveat — the cost/COGS side mirrors exactly, but Loyverse's
  gross-profit *number* still cannot.** Loyverse's Gross profit = Net sales − COGS,
  where Net = Gross − discounts − refunds; Books' headline is Gross
  (ADR-0008). The Δ is discounts + refunds (฿12,905 for Jul 1–21). Net-parity was
  ruled out of scope by [#67](https://github.com/theham1988/accounting-tool/issues/67);
  this ADR does **not** move Books to a net-sales headline. Partners comparing the
  two gross-profit numbers will see a persistent, explainable gap equal to
  discounts + refunds.
- **Imported cost is forward-only.** Loyverse applies an imported cost to sales
  after the import; it does not recost historical sales. Books' as-of-date pricing
  (`CONTEXT.md`, ADR-0005 decision 4) is unaffected — the mirror shapes Loyverse's
  **future** profitability reads only.
- **A new `loyverse_exports` table lands** (migration `0012`), separate from
  `audit_log`. The 9am "N config changes" count is unpolluted; the mirror has its
  own paper trail of who/when/what-changed.
- **The cost book, the daily review, the period view, and `CostResolver`
  (ADR-0005) are unchanged** — this mirror is a read-only consumer of
  `CostResolver.cost_per_unit`. It composes with the cost book; it does not alter
  it.
- **The five resolutions are independently reversible in principle but coupled in
  practice.** A round-trip file (decision 2) without drift detection (decision 3)
  would silently erase Loyverse-side edits; drift detection without the dedicated
  paper trail (decision 5) would leave no audit of what was overwritten; the
  on-demand action (decision 4) is the only seam that exercises any of them. They
  ship together.

## Independence from #82

This ADR is **independent of #82** (cash-basis supplier spend, recorded as
ADR-0010). The two efforts answer different questions: #82 is the
*purchases*-side engine internal to Books (what did we pay Makro this month, and
for what bucket?); this ADR is the *consumption*-side mirror from Books to
Loyverse (what does each sold item cost to make?). Neither depends on the other;
they can land and evolve on separate cadences.

## Considered and rejected

- **Writing costs over the Loyverse API.** Rejected by #69: the venue's token is
  read-only for items, and a write token would be a fresh recovery-surface and a
  fresh trust boundary. Returns as a fresh effort only if the CSV mirror proves
  insufficient.
- **Reading Loyverse items over the API to build the round-trip file
  automatically.** Rejected: the partner supplies the items export (the
  Loyverse-recommended flow). A future "fetch from Loyverse" button is a separate
  slice.
- **Sending gross (VAT-inclusive) cost.** Rejected as it silently reintroduces
  the ~7% COGS understatement ADR-0003 decision 4 fixed.
- **Emitting `0` / `0.00` for uncostable items.** Rejected as it would zero
  Loyverse's COGS for that item; blank is the only safe "Books has no number"
  value (#72 §2: blank doesn't overwrite).
- **A partial CSV (only the items Books wants to set).** Rejected as unsafe — a
  partial import can blank or overwrite columns the file omits (#72 §2). The
  full round-trip is the only safe shape.
- **Recording the export as a new `kind` on `audit_log`.** Rejected as it
  conflates a Loyverse-bound export with an in-Books config edit and pollutes the
  9am "N config changes since last review" count. The dedicated
  `loyverse_exports` table is the Q5 decision.
- **A scheduled / nightly export job.** Rejected as the mirror only matters at
  the moment a partner is about to read Loyverse profitability; cadence is
  partner-driven ("ahead of a profitability check, not daily").
- **Tracking whether the produced file was uploaded to Loyverse, or whether
  Loyverse ingested it.** Rejected as outside Books' trust boundary; the closed
  loop is next-prepare drift detection. The badge says "since last export", not
  "since last successful Loyverse import".
- **Net-sales parity in Loyverse's Gross profit number.** Rejected as out of
  scope: the Δ is discounts + refunds, and #67 closed that effort. The cost side
  mirrors exactly; the gross-profit *number* cannot, and this ADR does not move
  Books to a net-sales headline.
- **Recosting historical Loyverse sales on import.** Rejected: Loyverse applies
  imported cost forward-only, and Books' as-of-date pricing is unaffected either
  way.
- **Multi-currency.** Rejected: THB only; Loyverse and Books are both THB.

## References

- `CONTEXT.md` → VAT model, as-of-date pricing (the cost book this mirror reads
  from)
- ADR-0003 — decision 2 (audit-and-revert posture; "too small for gates", which
  decision 3 and decision 5 inherit) and decision 4 (net-of-VAT cost, which
  decision 1 mirrors exactly)
- ADR-0005 — `CostResolver.cost_per_unit`, the single public costing face
  decision 1 emits
- ADR-0008 — the gross-sales headline, which is why Loyverse's gross-profit
  *number* cannot mirror (the gross-profit-number caveat)
- #69 — no API write; the mirror is a back-office CSV action
- #72 — the CSV-format facts (target column `Cost`, joined on `SKU`; blank
  doesn't overwrite; full round-trip; forward-only; Advanced Inventory off)
- #70 — the grilling whose five resolutions decisions 1–5 record
- Parent spec: #100 on map #62
- This slice: #104 (implementation slices #101, #102, #103)
