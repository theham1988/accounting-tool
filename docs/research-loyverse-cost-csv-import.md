# Research — Loyverse back-office CSV import (item costs)

Map: #62. Ticket: #72 (research, AFK, docs-only — no imports into the production account).
Unblocks: #70 (CSV semantics grilling).

All claims are cited to Loyverse's own help center (primary source). Where the docs imply a
behaviour but do not state it crisply, the confidence is flagged and the safe workflow that
makes the question moot is recommended.

---

## TL;DR for #70

- The cost column is literally named **`Cost`**; the join key to Books is **`SKU` per variant row** (exact match — same key Books already uses). [§1](#1-column-set--the-join-key)
- **Blank cells do not overwrite** — a partial CSV (Handle + SKU + Cost only) updates cost and leaves prices, names, etc. untouched. *Implication strongly supported but not crisply stated in docs; the recommended export→fill→re-import round-trip sidesteps the question entirely.* [§2](#2-partial-imports--does-blank-mean-skip-or-overwrite)
- Round-trip is safe and officially recommended. Limits: 5 MB / 10 000 items (Tangerine ≈ 232). Encoding UTF-8; money is digits + point only (e.g. `45.50`). [§3](#3-round-trip-safety)
- **Imported cost does NOT recost historical sales** — it applies to sales after the import. [§4](#4-retroactivity--does-an-imported-cost-affect-historical-gross-profit)
- **Two facts #70 must confirm with a partner before deciding workflow**, because they determine whether the CSV mirror is viable *at all*:
  1. **Is Advanced Inventory active?** If yes, and items have Track Stock on, the `Cost` field becomes a read-only **Average Cost** recalculated by purchase orders — the CSV `Cost` column can't maintain it. CSV mirroring only works without Advanced Inventory (or with Track Stock off on these items).
  2. **Loyverse "Gross profit" = Net sales − COGS**, not Gross − COGS. Books' headline is Gross (ADR-0008). So the *cost* side can mirror exactly; the *gross-profit number* cannot, by Δ = discounts + refunds (฿12,905 for Jul 1–21).

---

## 1. Column set & the join key

Source: [`help.loyverse.com/help/importing-and-exporting`](https://help.loyverse.com/help/importing-and-exporting) ("The meaning of columns").

- **`Cost`** — "the amount of money you paid to purchase the item … specify the cost of the items only using digits — no currency symbols. Do not fill in the cost field for composite items. This will be calculated automatically as a sum of costs for each individual component."
- **`SKU`** — obligatory, unique per **variant row**, max 40 chars. "the key point is that no numbers are repeated."
- **`Handle`** — item identifier, same across all variants of one item; **required for items with variants**, blank/auto for ordinary items.
- Each variant combination is its **own row** with its own SKU (variant docs: [`help.loyverse.com/help/how-use-variants-items`](https://help.loyverse.com/help/how-use-variants-items)).

**Join to Books.** Books keys sales and mappings on variant SKU. The CSV keys the same way — one row per variant, joined on `SKU`. The join is exact; no Handle/UUID translation needed. ✓

**Two cost-ish columns — don't confuse them:**

| Column | Used by | Drives Gross profit? |
| --- | --- | --- |
| `Cost` | General item cost | **Yes** (when Advanced Inventory is off, or Track Stock off) |
| `Purchase cost` (UI: "Default Purchase Cost") | Advanced Inventory only — autofills purchase orders | **No** — only the order-entry default |

A CSV that fills `Purchase cost` instead of `Cost` will look like it worked but won't move the profitability number. #70's output must target the `Cost` column.

## 2. Partial imports — does blank mean skip or overwrite?

Source: same import doc + variants doc; no single crisp sentence, but strongly implied.

- The variants doc explicitly models blank-as-no-value: "Only the fields of the first item with variants have values. The fields that are common to all variants of the same item are left blank." Blanks are a normal, supported state meaning *no value supplied*.
- `Track stock` column: "If you leave this field empty, then it will **not** track the inventory" — but more tellingly, the import is described as updating item records, not replacing them; an item matched by SKU is an *edit*, not a re-create.
- `Available for sale`: "if you ignore this field, then the item will be available for sale **by default**" — i.e. blank does not toggle an existing value off.

**Reading (high but not certain confidence):** a CSV containing only `Handle, SKU, Cost` (every other column blank) will set cost on the matched SKUs and leave every other field — price, name, category, stock — untouched. The dangerous case (a partial file wiping prices or names) is not how the importer behaves.

**Recommendation for #70:** rely on the **safe round-trip** rather than this reading. Always export-from-Loyverse first, so the file carries every column with its current value; Books fills/overwrites only `Cost`; re-import. Then the blank-vs-skip question is moot. A partial emit is a size optimisation, not a correctness move, and can be deferred.

## 3. Round-trip safety

Source: import doc, "Import of items" + "Import errors".

- **Officially recommended flow:** create sample items in Back Office → **Export** → edit in Google Sheets / Excel / LibreOffice → **Import**. Loyverse says this (not the blank template) is "the best way." The export carries the exact header row and every existing value, so re-import can't accidentally rename a column or drop a field.
- **Hard limits:** file ≤ **5 MB**, ≤ **10 000 items**. Tangerine is ≈ 232 items and a few KB — orders of magnitude under both.
- **Column names are immutable** — editing a header name fails the import ("You edited the names of columns that should have been left unedited"). Books must emit headers exactly as the export produced them. (The export header is the canonical source of truth for spelling/casing — Books should snapshot it, not type it.)
- **Money format:** "only numbers — no currency symbols. The decimal separator must be a point." THB → e.g. `45.50`, never `฿45.50` or `45,50`.
- **Encoding:** Loyverse's own export-to-LibreOffice doc specifies `Character set: Unicode (UTF-8)` ([`help.loyverse.com/help/export-libreoffices`](https://help.loyverse.com/help/export-libreoffices)). Thai names in the file are fine under UTF-8. Because the recommended flow re-imports a file Loyverse itself exported, encoding round-trips by construction — **Books emitting its own file should write UTF-8 (BOM preferred for Thai-name compatibility in Excel)**.
- **Errors at upload:** wrong format, > 5 MB, edited column names, > 10 000 items. **Errors after upload:** critical (red, must fix — blocks import) vs warnings (yellow, informational, imports anyway), each pinpointed to row/column/cell.

## 4. Retroactivity — does an imported cost affect historical Gross profit?

Source: Sales by Item report doc + Advanced Inventory doc; plus support/community synthesis.

- **No retroactive recost.** Updating an item's cost applies to sales **after** the change; historical Sales by Item rows keep the cost that was in effect when they were sold. (Synthesis of support answers; the report doc defines "Cost of goods = the item cost" without stating a recost rule, so treat the *no-recost* reading as the operationally safe assumption — but it should be confirmed with a one-off export test before #70 banks on it.)
- **Gross profit is on NET, not Gross.** From [`help.loyverse.com/help/sales-item-report-back-office`](https://help.loyverse.com/help/sales-item-report-back-office): `Net Sales = Gross Sales − Discounts − Refunds`, then `Gross Profit = Net Sales − Cost of Goods`. Books' headline is on Gross (ADR-0008), so Books' "gross margin" (Gross − COGS) and Loyverse's "Gross profit" (Net − COGS) differ by exactly discounts + refunds. For Jul 1–21 that Δ was ฿11,985 + ฿920 = **฿12,905**. The map's destination says "Loyverse's cost/gross-profit side mirrors Books" — the **cost/COGS side can mirror exactly**; the gross-profit *number* cannot unless Books also moves to Net, which was ruled out of scope for this effort ([#67 closed](https://github.com/theham1988/accounting-tool/issues/67)).

### Advanced Inventory — the fork in the road

Source: [`help.loyverse.com/help/advanced-inventory-management`](https://help.loyverse.com/help/advanced-inventory-management). Paid subscription.

If Advanced Inventory is active **and** an item has Track Stock on:

- After first save, "the Cost field is replaced by a read-only **Average Cost** field."
- Average Cost recalculates on every inventory receipt (purchase order received, stock adjustment, transfer) per the weighted-average formula in the doc.
- "The resulting Average Cost is used across **all reports and profitability calculations** in the system."
- The CSV `Cost` column can set the **initial** cost and the **Default Purchase Cost**, but it **cannot maintain ongoing cost** — that flows from purchase orders.

**Consequence for the map:** the #69 decision ("costs mirror to Loyverse via a back-office CSV import, keeping the integration simple") only holds in the **non-Advanced-Inventory** regime (or with Track Stock off on the items Books is costing). If Tangerine runs Advanced Inventory with tracked stock, the CSV mirror is the wrong vehicle — cost would have to flow through purchase orders, which is a materially different integration (and well past "keep it simple"). **This is the first question #70 must settle with a partner.** If the answer is "yes, Advanced Inventory is on and stock is tracked," the #69 decision should be revisited before #70 grills workflow.

---

## Open questions to confirm with a partner (inputs to #70, not blockers for this ticket)

1. **Is Advanced Inventory active on the production account? Are items Track-Stock-on?** Determines whether CSV `Cost` import can carry ongoing cost at all. (Check: Back Office → Settings → Billing & Subscriptions; and any one item's Inventory section.)
2. **Does Books emit the full round-trip file or a minimal `{Handle, SKU, Cost}` file?** Recommended: full round-trip (safer, sidesteps §2's ambiguity). Decision for #70.
3. **Confirm no-recost empirically** — export Sales by Item for a past period, note COGS, change one item's cost via import, re-export the same period, confirm COGS unchanged. One partner-driven test; this ticket is docs-only so it remains a #70 checklist item.

## Sources

- [Exporting and Importing Items](https://help.loyverse.com/help/importing-and-exporting) — column meanings, import errors, variant row shape, money format.
- [How to Use Variants of Items](https://help.loyverse.com/help/how-use-variants-items) — Handle/SKU per variant, blank-shared-fields pattern.
- [Sales by Item Report in the Back Office](https://help.loyverse.com/help/sales-item-report-back-office) — Net vs Gross profit formula, COGS column meaning, export shape.
- [What is Advanced Inventory Management](https://help.loyverse.com/help/advanced-inventory-management) — read-only Average Cost, what overrides CSV cost.
- [How to Work with Purchase Orders and Suppliers](https://help.loyverse.com/help/how-purchase-orders-and-suppliers) — purchase-order cost flow, import PO template.
- [How to Export Data from Reports and Open in LibreOffice Calc](https://help.loyverse.com/help/export-libreoffices) — UTF-8 encoding, comma separator.
