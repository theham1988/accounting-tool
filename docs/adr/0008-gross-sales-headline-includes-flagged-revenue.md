# ADR-0008: Gross-sales headline — unmapped revenue joins the headline

Date: 2026-07-21

## Status

Accepted.

Reverses the slice-04 reliable-rows-only rule for **revenue** (recorded in
`CONTEXT.md` → COGS recognition, in the `DailyMargin` / `PeriodReview`
docstrings, and in the slice-04 E2E tests). The slice-04 rule for **COGS**
and per-segment **contribution margin** is unchanged — only revenue moves.

## Context

Slice 04 (Wave 1) excluded unmapped and unknown-price sales from every
headline total — revenue, COGS, and gross margin alike — on the principle
that a row whose COGS the tool cannot compute is not reliable enough to
total. The revenue from those flagged rows surfaced in a separate
`flagged_revenue` field and a `needs_attention` section, "visible, not
silently dropped". The rule was documented as deliberate (the COGS
recognition entry in `CONTEXT.md`) and tested as such.

Issue #64 / map #62 surfaced the cost of that rule for the venue's
reconciliation story. The two partners open the Loyverse dashboard and
Books side by side every morning; Loyverse's headline is **Gross sales**
(every sale, before discounts and refunds). Books' headline was reliable
rows only. In July 2026 the gap between the two was the bulk of the
month's revenue — most of the menu was unmapped at the time — so Books was
silently answering a different question than the dashboard beside it. The
partners could not tell at a glance whether the tool disagreed with
Loyverse (a defect) or was merely defining "revenue" differently (a
label). That ambiguity is the failure mode this ADR ends.

The decision (issue #64, decided 2026-07-18): **Books' headline ties to
Loyverse Gross sales**. Issue #71 is the implementation ticket; this ADR
records the reversal.

## Decision

**1. The headline revenue number includes every sale, mapped or not.**
`DailyMargin.total_revenue` and `PeriodReview.revenue` (and the per-day
`PeriodDay.revenue` drilldown rows) sum every sale's revenue — reliable
rows plus flagged rows. The number a partner reads equals Loyverse Gross
sales for the same range, by construction. The implementation lives in
`compute_daily_margin` and `build_period_review`.

**2. COGS stays recipe-cost over reliable rows only.** A flagged row's
COGS is unknown; booking it at zero would understate COGS, and fabricating
a number would over-state reliability. `total_cogs` / `review.cogs` /
`PeriodDay.cogs` continue to sum the reliable rows only — exactly the
slice-04 rule, unchanged.

**3. Gross margin is `revenue − cogs`, by construction.**
`total_gross_margin` and `review.gross_margin` equal the new revenue minus
the unchanged COGS. The arithmetic the partner can do themselves (revenue
minus COGS) agrees with the hero number, so the headline trio stays
internally consistent. The implicit assumption — flagged revenue carries
zero COGS — overstates the margin on the uncosted portion; that is the
honest-labelling problem decision 4 addresses.

**4. Honest labelling lives on the template, not in the number.** When
`flagged_revenue > 0`, the headline card carries a callout: "Revenue
includes **N THB** of sales whose cost the tool cannot compute — the gross
margin implicitly zero-costs them. Map the items in Needs a fix to cost
them." The callout links into the needs-attention anchor. This names the
zero-COGS assumption rather than hiding it in the math, and it keeps the
fix path (mapping the items, pricing the ingredients) one click away. The
`flagged_revenue` field and the `needs_attention` section still surface
the same residue as before — the callout and the card share one source of
truth.

**5. Per-segment contribution margin stays reliable-only.** PRD user story
20 — segment CM must stay "clean and defensible" — still holds: a flagged
row's COGS is unknown, so its revenue cannot honestly land in a segment's
CM. `segment_margins` rolls up reliable rows only, exactly as before. The
headline moves; the cards stay.

**6. The gross-margin % shown on the daily headline is unchanged in
shape.** It is still `gross_margin / revenue` to 1 dp; what changes is the
denominator (now the gross-sales revenue). The % now reads lower when
flagged revenue is present, which is honest: the day's margin over its
gross take, with the uncosted portion in the denominator.

## Consequences

- Books and Loyverse now answer the same question for the same range.
  Reconciliation is "the two numbers agree", not "the two numbers disagree
  for an explainable reason".
- The headline gross margin is **overstated** by the uncosted portion's
  implicit zero COGS. The callout (decision 4) is the remedy, not a
  number that fixes it; partners who want the strictly-honest margin can
  read it off `(revenue - flagged_revenue - cogs) / revenue` themselves,
  but the headline stays arithmetically consistent with the two numbers
  beside it.
- Segment CM cards continue to understate a segment that has unmapped
  sales — they were never meant to be a revenue total, only a clean
  contribution-margin number, and the unmapped revenue's segment is
  ambiguous anyway until #73 (pure-clock segmentation) lands.
- The slice-04 tests that asserted the old contract are updated in this
  same change to assert the new one. The contract reversal is recorded
  here, not in the tests; the tests pin the new contract.
- A future "net sales" headline (gross minus discounts and refunds, per
  Loyverse's Net sales) is consciously out of scope for this effort (map
  #62 closed [issue #67](https://github.com/theham1988/accounting-tool/issues/67)
  accordingly). The gross-sales headline is the smallest reversal that
  makes Books and Loyverse agree; netting discounts and refunds would be a
  fresh effort.

## What does not change

- The `flagged_revenue` field, the `needs_attention` section, and the
  needs-attention card continue to surface the same residue — they are
  the fix path, and the headline's new callout links into them.
- The segment CM cards continue to roll up reliable rows only.
- The recipe-cost COGS model continues to cost each sale at the as-of-date
  price over its recipe's ingredients (`CostResolver`, ADR-0005). Only the
  headline roll-up's **revenue** line moves.
- Loyverse REFUND receipts are still excluded from sales (parser rule
  unchanged); the gross-sales headline is "gross of refunds in the
  Loyverse sense", not "including refunds". The PRD's "discounts and
  refunds do not reduce the headline" rule (issue #64) is honoured by
  leaving the parser's refund-skipping rule alone.
