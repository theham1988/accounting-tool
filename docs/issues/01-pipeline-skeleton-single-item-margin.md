# 01 — Pipeline skeleton with seeded single-item margin

## What to build

Establish the project scaffold and a single end-to-end pipeline test seam. This first tracer bullet cuts through every layer (storage, computation, output) but uses hardcoded, seeded inputs — no real integrations yet.

Inputs (all seeded, in-repo or via test fixtures):
- One sale (one item, one unit, one timestamp)
- One recipe for that item
- One purchase price for the recipe's input

The pipeline must produce a single number: that item's gross margin for that day.

This slice also locks the project's tech stack, directory structure, testing framework, and the single end-to-end test seam that all subsequent slices will extend. The implementing agent should pick the stack now and document the choice in the PRD's "Open items" section.

## Acceptance criteria

- [ ] Project scaffold exists and runs (selected tech stack documented)
- [ ] A single end-to-end test feeds seeded inputs and asserts the gross margin output
- [ ] The pipeline reads inputs from a defined ingestion boundary (no real integrations yet, but the boundary exists so later slices can swap in real sources)
- [ ] Schema for sale, recipe, purchase price, and computed margin exists
- [ ] Running the pipeline produces a daily gross margin number for the seeded item
- [ ] Test seam is reusable: a second seeded item can be added to the test without changing pipeline code

## Blocked by

None - can start immediately.
