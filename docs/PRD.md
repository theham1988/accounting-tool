# Tangerine Phuket — Bar & Cafe Accounting Tool

## Problem Statement

We run a dual-concept venue in Phuket: a cafe (8am–5pm) and a bar (5pm–10pm). Today, receipts pile up in a Google Drive folder, sales data sits in Loyverse (which we can access via API), and our actual profitability is invisible to us until we manually cobble together a P&L in a spreadsheet. We have no reliable per-item margin data, no accurate keg-yield accounting, no cash-control reconciliation, and no segmented view of which half of the business (cafe vs. bar) is paying for itself. Our real target is 10,000 THB/day profit; we currently cannot tell whether we're hitting it.

## Solution

A single accounting tool that ingests sales (Loyverse API), purchases (receipts via OCR), and physical inventory (weekly keg weights, cafe stock counts), and produces:

- A daily-updated P&L with revenue, COGS, gross margin, and contribution margin per segment (cafe vs. bar)
- Per-item margins using accurate cost-per-pour derived from current invoice prices and weighed keg yields
- Anomaly detection on voids and cash-drawer variances (no on-site manager — the system must do the segregation-of-duties work)
- A daily morning review view (9am) showing yesterday's item-level margins, segment performance vs. the 10K THB/day goal, and any flags needing attention
- A monthly reconciliation view with proper accrual-basis COGS

The tool is not fully hands-off — by deliberate design it requires 10–15 hours/week of partner labor (cash counts, keg weighs, receipt approvals, daily review). The "hands-off" goal was revised during planning to "minimal, structured partner labor with the system doing the heavy lifting on data extraction, sync, and anomaly flagging."

## User Stories

### Data Ingestion

1. As a partner, I want the tool to automatically poll the Loyverse API for sales, items, and menu changes on a schedule, so that I do not have to manually export or mirror data between systems.
2. As a partner, I want to upload receipt photos and supplier invoices (uploaded from Google Drive or directly), so that all purchases are captured in one place.
3. As a partner, I want the tool to OCR/extract line items from receipts using an LLM, so that supplier, item, quantity, unit price, and VAT are captured as structured data.
4. As a partner, I want the tool to flag extracted line items whose unit price deviates more than 5% from the last-known price for that SKU/supplier, so that OCR errors and real price changes both get human attention.
5. As a partner, I want the tool to auto-reject any receipt whose extracted line items + VAT do not reconcile to the stated total, so that broken extractions bounce back without polluting the books.
6. As a partner, I want to see a weekly queue of receipts flagged for review, so I can approve, correct, or reject each one before it lands in the books.
7. As a partner, I want to record a weekly keg weighing per brand, so that actual beer yield can be computed against rings-up sales.

### Recipe and Cost Modeling

8. As a partner, I want to define recipes (e.g. "500ml Chang draft = X ml beer"), so that cost-per-pour is derived from current keg cost.
9. As a partner, I want to enter per-brand keg tare weights once (since draught rotation is low), so that weekly weighing converts gross weight to beer volume.
10. As a partner, I want the tool to accept a beer density approximation per brand (or default to water-density with documented ~0.5–1.5% tolerance), so that volume-from-weight math works without precise specific-gravity data.
11. As a partner, I want each Loyverse menu item to map to a recipe, so that sales can be tied to accurate COGS.
12. As a partner, I want the system to detect when a Loyverse menu item has no recipe mapping, so that unmapped sales surface immediately.
13. As a partner, I want to set target gross margins per item, so that mispriced items are visible in the daily review.

### Cash and Loss Controls

14. As a partner, I want each shift's cashier to enter a closing cash count at shift end, so that drawer variance vs. Loyverse rung-up cash is recorded.
15. As a partner, I want the system to require a recount at the 5pm handoff, so that the incoming partner verifies the outgoing partner's drawer before starting their shift.
16. As a partner, I want the tool to flag statistical anomalies in voids and drawer variances (e.g. voids clustered in one user's shifts, voids spiking on busy nights, drawer-short rates per user), so that theft and errors are surfaced without an on-site manager.
17. As a partner, I want anomaly flags to appear in the 9am daily review, so that they get acted on at a defined cadence.

### Reporting

18. As a partner, I want a daily P&L showing yesterday's revenue, COGS, gross margin, and contribution margin split by cafe and bar segment, so I can review at 9am whether we're trending toward the 10K THB/day goal.
19. As a partner, I want a per-item margin and sell-volume table for yesterday, so I can spot mispriced or underperforming items while they're still relevant.
20. As a partner, I want fixed costs (rent, utilities, shared staff) recorded at the entity level, not allocated to segments, so that segment contribution margins stay clean and defensible.
21. As a partner, I want segment contribution margin to be flagged red when it falls below 0, so that a segment failing to cover its own variable costs triggers an explicit conversation.
22. As a partner, I want a monthly reconciliation view that uses proper accrual-basis COGS (beginning inventory + purchases − ending inventory), so that delivery timing doesn't distort month-to-month margins.
23. As a partner, I want the monthly view to show full entity-level net profit (segments' contribution margin minus fixed costs), so we can see whether the whole business is hitting its 10K THB/day target.
24. As a partner, I want cash-basis payables tracked by invoice date separately from accrual COGS, so that both the accounting view (COGS by consumption) and the cash-flow view (when bills are due) are available.

### Inventory

25. As a partner, I want to record weekly cafe stock counts (milk, beans, pastries, etc.) at whatever cadence each item's shelf life demands, so that perishable COGS is captured accurately.
26. As a partner, I want low-stock alerts on key cafe items based on sales velocity, so that I order before running out.
27. As a partner, I want to record beer keg purchases and current kegs-on-hand per brand, so that the keg weigh-in feeds both inventory and COGS.

### Operational Workflows

28. As a partner, I want a structured checklist for the weekly admin ritual (keg weigh, receipt approval, cafe stock count), so that nothing gets skipped under shift pressure.
29. As a partner, I want a structured checklist for the daily 9am review (margin review, anomaly flags, segment status, goal tracking), so that the review is fast and consistent.
30. As a partner, I want admin task assignments to be split-able between the day-shift partner and the night-shift partner, so the labor load is genuinely shared (the night-shift partner cannot reasonably do admin at 10pm or 9am — the system must allow admin tasks to be scheduled during each partner's available windows).
31. As a partner, I want to be able to onboard a future manager into the admin workflow without re-architecting the tool, so that growth doesn't break the process.

## Implementation Decisions

### Data sources and sync

- **Loyverse API** is the authoritative source for sales, items, and menu state. The tool polls on a schedule (default: daily after close; configurable). Margin numbers computed between a menu change and the next sync are explicitly accepted as stale until sync — this is documented behaviour, not a bug, given the 9am daily review cadence.
- **Receipts** are ingested from upload (Google Drive import or direct upload). A receipt processor (LLM-based OCR) extracts: supplier, invoice date, line items (description, qty, unit price), VAT, and total.
- **Inventory** is captured via partner-entered counts: weekly keg weights (gross weight per brand), weekly cafe stock counts, with cadence per item type.

### Recipe model

- Recipes define the inputs (e.g. ml of beer, ml of milk, g of beans) and yield for each Loyverse item.
- Each Loyverse item maps to exactly one recipe; unmapped items are flagged.
- Keg tare weight and density approximation are stored per brand.

### COGS recognition

- **Accrual COGS** for monthly P&L: `beginning_inventory_value + purchases − ending_inventory_value`, where ending inventory comes from the weekly keg weigh and cafe stock counts.
- **Payables** (cash-flow view) are tracked by invoice date.
- Sales are recognized by Loyverse transaction timestamp.

### Pricing reference data

- A `last_known_price` table per (SKU, supplier) is updated whenever a receipt is approved.
- New receipts' extracted unit prices are compared against this reference; >5% deviation flags for human review.
- A sum-check (lines + VAT == total, within tolerance) auto-rejects broken extractions before human review.

### Segmentation

- Each transaction, recipe, and item is tagged `cafe` or `bar` (driven by Loyverse category or shift timestamp).
- Contribution margin is reported per segment (revenue − variable costs).
- Fixed costs are stored at entity level only — no allocation to segments.
- A segment is flagged red when its contribution margin for the period is < 0.

### Cash control

- Each shift close records: opening cash, closing cash, Loyverse rung-up cash, variance.
- The 5pm handoff requires the incoming partner to re-enter the closing drawer count, which is compared to the outgoing partner's reported count. Mismatch blocks shift start.
- Anomaly detection runs on the historical drawer-variance and void log per user; flags appear in the 9am review. Initial detection rules: void-rate per user above venue median, void clustering at peak hours, drawer-short rate per user above threshold.

### Daily and monthly views

- **Daily 9am review view**: yesterday's revenue, COGS, gross margin, contribution margin per segment, top/bottom items by margin and volume, anomaly flags, progress toward 10K THB/day.
- **Monthly reconciliation view**: full P&L using accrual COGS, segment CM, fixed costs, net profit, comparison to 10K THB/day target.

### Schema sketch (prototype-driven decisions)

Reference-price check decision shape (from the planning conversation):

```
ReceiptLine {
  sku_id, supplier_id, extracted_unit_price
}
last_known_price: Map<(sku_id, supplier_id), {price, updated_at}>
deviation = |extracted_unit_price - last_known_price| / last_known_price
if deviation > 0.05  -> flag_for_review
if sum(lines) + vat != total (within tolerance) -> auto_reject
```

Segment decision shape:

```
Segment = "cafe" | "bar"
contribution_margin(segment, period) =
    revenue(segment, period) - variable_costs(segment, period)
fixed_costs(segment, period) = 0   // never allocated
entity_net_profit(period) =
    sum_over_segments(contribution_margin) - fixed_costs(entity, period)
segment_status(segment, period) =
    if contribution_margin(segment, period) < 0 -> "red"
```

## Testing Decisions

### Testing seam

A single end-to-end pipeline seam. Synthetic inputs in, reports/flags/margins out.

- **Inputs**: synthetic Loyverse sales payloads, synthetic receipt images/extracted payloads, synthetic keg weights, synthetic cafe stock counts, synthetic recipes, synthetic cash counts.
- **Assertions**: P&L numbers, per-item margins, segment contribution margins, anomaly flags, drawer-variance computations, reference-price flagging, sum-check rejections.

### What makes a good test

- Test external behaviour (numbers and flags the tool produces), not implementation details (how OCR is called, how the DB stores rows).
- No mocking of internal modules unless they're genuine external boundaries (Loyverse HTTP, the LLM provider).
- Each test should be readable as a worked example: "given a keg of Chang costing X, sold as Y pours, the 500ml margin is Z."

### Modules covered by the seam

- Loyverse sync client
- Receipt processor (OCR + extraction + reference check + sum check)
- Recipe and cost engine
- Inventory (keg weigh, cafe stock) → COGS engine
- Cash reconciliation engine
- Anomaly detector
- Daily and monthly reporting views

## Out of Scope

- **Hardware flow meters on beer lines** — considered for true real-time yield, not budgeted. May revisit if scale justifies.
- **Specific gravity / density tracking per beer** — accepted as ~0.5–1.5% volume error using water-density approximation.
- **Manager role features** — the tool must be onboarding-friendly for a future manager, but manager-specific features (e.g. shift scheduling, payroll) are not built now.
- **Refined anomaly-detection models** — initial rules-based detection (void-rate, drawer-short-rate, clustering). ML/statistical model tuning deferred.
- **Segment fixed-cost allocation** — explicitly excluded; segments carry contribution margin only.
- **Daily per-item review beyond the 9am view** — the team has agreed on a single 9am review window; intra-day margin watching is out of scope.
- **Per-keg yield tracking** — accepted that weekly weighing gives aggregate yield; per-keg variance attribution is not built.
- **Multi-currency** — assumed THB only.
- **VAT filing/submission automation** — VAT is captured for reconciliation but the tool does not file returns.

## Further Notes

### Known control gap

With only two partners (one per shift) and no manager, segregation of duties is structurally limited. The tool mitigates this via partner-handoff recount and anomaly detection, but the system is designed around partners being on-site. When a manager is hired, the control model must be revisited — particularly the "bartender counts their own drawer" pattern, which is only safe because a partner is the bartender today.

### "Hands-off" revision

The original brief asked for "as hands-off as possible." Through the planning conversation it became clear that the accuracy required (keg yield, receipt data quality, cash control, segmented P&L) cannot be achieved hands-off given the team size. The accepted model is 10–15 hours/week of structured partner labor, with the tool automating data extraction, sync, calculation, and flagging. This should be re-evaluated when a manager is hired or scale changes.

### Open items for the implementing agent to resolve

Resolved in slice 01 (pipeline skeleton):

- **Tech stack**: Python 3.12+ with a `src/` layout (single package `tangerine`), pytest for tests, mypy (strict) for type checking. Money is `decimal.Decimal` throughout to avoid float drift in THB. The testing seam is a single end-to-end pipeline test (see `tests/test_pipeline_e2e.py`).

Still open:

- Specific Loyverse API endpoints and auth flow (use Loyverse API docs).
- Choice of LLM/OCR provider for receipt extraction (cost vs. accuracy tradeoff).
- Polling cadence default (proposed: daily post-close, with menu-change diffing on each poll).
- Storage choice (relational DB recommended; schema for receipts, recipes, inventory, sales, segments, fixed costs).
- Deployment target (single-instance server is sufficient; no multi-tenancy required).
