-- Issue #147 (semantics locked in the #142 resolution, decision 6b): the
-- receipt-grain facts table behind the IN-01 five-number derivation.
--
-- One row per Loyverse receipt, PRIMARY KEY receipt_number, written at sync
-- time with INSERT OR IGNORE idempotency (exactly like sales) so replayed
-- pages never double-count. Payments and discounts are receipt-grain facts:
-- folding them per-line into the line-grain `sales` table would denormalize
-- (a split tender has no single per-line home), so they get their own table.
-- The line-grain `sales` table stays untouched.
--
-- Both SALE and REFUND receipts land here. A REFUND row carries its signed
-- channel splits (negatives) on its own local day and channel — the books'
-- locked P-11 deviation; `discount` is never negative (Loyverse refunds the
-- discounted amount actually paid, so refunds carry no discount rows).
--
-- Money is TEXT holding exact Decimal strings (two places), matching sales'
-- sell_price convention — never float, per the parser-boundary rule.
-- `local_date` is the venue-local (Asia/Bangkok) calendar day of created_at
-- (issue #66), i.e. the trading-day bucket; TEXT ISO-8601 date.
-- `receipt_type` is constrained to SALE/REFUND at parse time (a hard
-- LoyverseParseError on anything else), so the CHECK here is belt-and-braces
-- against a hand-edited row.

CREATE TABLE IF NOT EXISTS receipt_facts (
    receipt_number  TEXT PRIMARY KEY,
    receipt_type    TEXT NOT NULL CHECK (receipt_type IN ('SALE', 'REFUND')),
    local_date      TEXT NOT NULL,
    cash            TEXT NOT NULL,
    qr              TEXT NOT NULL,
    card            TEXT NOT NULL,
    discount        TEXT NOT NULL,
    total_money     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipt_facts_local_date
    ON receipt_facts(local_date);
