# 04 — Recipe and per-item cost engine

## What to build

Introduce recipes that map each Loyverse menu item to its inputs (e.g. "500ml Chang draft = X ml beer"). Derive cost-per-unit for each item from the latest approved purchase price (from slice 03) and the recipe's input quantities.

Compute per-item:
- Cost per unit (current)
- Gross margin per unit (sale price − cost) using the Loyverse sale price
- Gross margin %
- Sell volume (units sold in period)

Output a per-item margin and sell-volume table for the daily view, sorted to surface both top performers and underperformers.

Items with a target gross margin set should be flagged when actual margin falls below target.

This slice also resolves the SKU/master-item mapping that slice 03 referenced — recipes are defined against SKUs, and Loyverse items map to SKUs.

## Acceptance criteria

- [ ] Recipe schema exists with inputs (SKU + qty) and yield
- [ ] Loyverse items map to recipes via SKU; unmapped items are flagged
- [ ] Cost-per-unit is derived from latest approved purchase price + recipe inputs
- [ ] Per-item margin table shows cost, margin, margin %, sell volume for the period
- [ ] Target-margin violations are flagged per item
- [ ] End-to-end test feeds synthetic recipes, sales, and approved purchases; asserts margin numbers
- [ ] Keg-based recipes can reference a keg as input with conversion (ml of beer per item) — actual yield math is slice 05, but the recipe shape must support it

## Blocked by

- 02 — Loyverse API sync
- 03 — Receipt ingestion pipeline
