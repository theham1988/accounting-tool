# 03 — Receipt ingestion pipeline

## What to build

End-to-end receipt flow, from upload to stored purchase. Replaces the seeded purchase price from slice 01.

Flow:
1. Partner uploads a receipt (direct upload, or import from Google Drive).
2. An LLM/OCR processor extracts: supplier, invoice date, line items (description, qty, unit price), VAT, total.
3. **Sum-check**: extracted line items + VAT must equal the stated total within tolerance. Mismatch → auto-reject, bounce back, do not enter the books.
4. **Reference-price check**: for each line, compare extracted unit price against `last_known_price` for that (SKU, supplier). Deviation > 5% → flag for human review.
5. Flagged or rejected receipts enter a weekly approval queue.
6. Approved receipts are stored as purchases. Approving updates `last_known_price` for each line's (SKU, supplier).
7. Receipts that pass both checks with no flag may auto-approve (configurable — start conservative: require human approval on all, relax once confidence is established).

A SKU/master-item mapping is required to tie receipt lines to recipes (recipes themselves are slice 04). Lines without a SKU mapping go into the approval queue regardless of price check.

## Acceptance criteria

- [ ] Receipt upload works (direct + Google Drive import)
- [ ] OCR processor extracts supplier, invoice date, line items, VAT, total
- [ ] Sum-check auto-rejects receipts whose lines + VAT != total within tolerance
- [ ] Reference-price check flags lines deviating >5% from last known price
- [ ] Lines without a SKU mapping are always queued for human review
- [ ] Approval queue exists; partners can approve, correct (edit extracted fields), or reject
- [ ] Approved receipts update `last_known_price` for each line's (SKU, supplier)
- [ ] End-to-end test feeds a synthetic receipt, asserts flag/reject/approve outcomes and stored purchase data

## Blocked by

- 01 — Pipeline skeleton with seeded single-item margin
