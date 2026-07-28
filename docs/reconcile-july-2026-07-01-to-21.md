# July 2026 reconciliation — window 2026-07-01 .. 2026-07-21

Read-only reconciliation of Loyverse dashboard exports against Books' seed `config/recipes.yaml`. Map: #62. Ticket: #63.

Window covers 21 calendar days (19 trading). The map's cited figures (฿130,005 gross / ฿75,095 Books / ฿54,910 gap) were a smaller earlier-July snapshot; this reconciliation works against the actual export window named above.

## Step 1 — the Loyverse side (from the daily sales summary)

| Line | THB |
| --- | ---: |
| Gross sales | ฿166,235 |
| Less: refunds | −฿920 |
| Less: discounts | −฿11,985 |
| **= Net sales** | **฿153,330** |
| Loyverse COGS (their cost book) | ฿35,689 |
| **= Loyverse Gross profit** | **฿117,641** |
| Loyverse Gross margin | 70.8% |

Loyverse reports its own COGS and Gross profit alongside the sales totals — an independent ground truth for the cost half of the map's destination (Books producing a Loyverse-importable cost CSV so the two sides mirror).

## Step 2 — the Books side (from items export × recipes.yaml)

| Bucket | Items | Revenue | % of Gross |
| --- | ---: | ---: | ---: |
| mapped (recipe + price path) | 92 | ฿102,070 | 61.4% |
| unmapped (no SKU mapping → `unmapped`) | 52 | ฿64,165 | 38.6% |
| **Total (items export)** | **144** | **฿166,235** | **100.0%** |

Items-export total (฿166,235) ties the daily-summary Gross (฿166,235).

Two Books headlines, two readings:

| Headline | THB | Rule |
| --- | ---: | --- |
| Books pre-#71 (reliable rows only) | ฿102,070 | excluded flagged revenue — the number the map originally cited |
| Books post-#71 (every sale) | ฿166,235 | issue #71 / ADR-0008 — ties to Loyverse Gross by construction |

Gap under the pre-#71 rule: ฿64,165 (= Gross − mapped). Post-#71 closes it by definition.

## Q1 — flagged_revenue, ranked by item

Total flagged: ฿64,165 (38.6% of Gross). Unmapped revenue: ฿64,165 across 52 items.

Ranked by gross revenue. The flag column is the Books side's view; Loyverse's category is shown for follow-up.

| Rank | Item | Loyverse category | Flag | Units | Gross | % of flagged |
| ---: | --- | --- | --- | ---: | ---: | ---: |
| 1 | 500 ml HoD Pale Ale | Beer Taps | unmapped | 27 | ฿8,100 | 12.6% |
| 2 | 500 ml S&B Saijai Bright IPA | Beer Taps | unmapped | 23 | ฿6,900 | 10.8% |
| 3 | 500 ml - S&B Modern Lager | Beer Taps | unmapped | 21 | ฿5,355 | 8.3% |
| 4 | Sushi Taco Trio Set | Sushi Tacos | unmapped | 13 | ฿4,160 | 6.5% |
| 5 | 330 ml S&B Modern Lager | Beer Taps | unmapped | 13 | ฿2,340 | 3.6% |
| 6 | 330 ml - S&B Saijai Bright IPA | Beer Taps | unmapped | 11 | ฿2,310 | 3.6% |
| 7 | 500 ml - Beerfest Lager | Beer Taps | unmapped | 9 | ฿2,295 | 3.6% |
| 8 | 330 ml HoD Pale Ale | Beer Taps | unmapped | 10 | ฿2,100 | 3.3% |
| 9 | Mexi Sushi Taco Set | Sushi Tacos | unmapped | 7 | ฿2,100 | 3.3% |
| 10 | Chorizo Kai Kata โชริโซ่ ไข่กระทะ | Breakfast | unmapped | 8 | ฿1,600 | 2.5% |
| 11 | Teriyaki Chicken on Rice ข้าวหน้าไก่เทริยากิ | Rice Bowls | unmapped | 8 | ฿1,440 | 2.2% |
| 12 | Chicken Pop! | Snacks & Bites | unmapped | 14 | ฿1,400 | 2.2% |
| 13 | French Fries (Paprika) | Snacks & Bites | unmapped | 11 | ฿1,320 | 2.1% |
| 14 | Slice passionfruit sour 5% | Bottled Beer & Cider & Wine | unmapped | 7 | ฿1,295 | 2.0% |
| 15 | Pizzolato organic wine | Bottled Beer & Cider & Wine | unmapped | 1 | ฿1,250 | 1.9% |
| 16 | Castown Soda | Soft Drinks | unmapped | 15 | ฿1,200 | 1.9% |
| 17 | Nuggets | Snacks & Bites | unmapped | 12 | ฿1,200 | 1.9% |
| 18 | Thai Tea ชาไทยเย็น | Tea | unmapped | 15 | ฿1,200 | 1.9% |
| 19 | Sundown RTD | Beer Taps | unmapped | 6 | ฿1,080 | 1.7% |
| 20 | French Fries (Normal) | Snacks & Bites | unmapped | 10 | ฿1,000 | 1.6% |
| 21 | Vana Honey | Bottled Beer & Cider & Wine | unmapped | 5 | ฿975 | 1.5% |
| 22 | ไวน์ขาว ขวด | Bottled Beer & Cider & Wine | unmapped | 1 | ฿960 | 1.5% |
| 23 | Mineral Water น้ำแร่ | Soft Drinks | unmapped | 46 | ฿920 | 1.4% |
| 24 | 330 ml  Beerfest Lager | Beer Taps | unmapped | 5 | ฿900 | 1.4% |
| 25 | HOD West Coast 330ml | Bottled Beer & Cider & Wine | unmapped | 3 | ฿900 | 1.4% |
| 26 | Sweet potato fries with parmesan + aioli sauce | Snacks & Bites | unmapped | 6 | ฿880 | 1.4% |
| 27 | Coke โค้ก | Soft Drinks | unmapped | 30 | ฿750 | 1.2% |
| 28 | RTD Cocktail - Margarita | Beer Taps | unmapped | 4 | ฿720 | 1.1% |
| 29 | RTD Cocktail Sangria | Beer Taps | unmapped | 4 | ฿720 | 1.1% |
| 30 | 500 ml - S&B West Coast Anda IPA | Beer Taps | unmapped | 2 | ฿600 | 0.9% |
| 31 | Spinach Ham Cheese ผักโขมอบชีส | Breakfast | unmapped | 4 | ฿600 | 0.9% |
| 32 | Wila Weizen | Bottled Beer & Cider & Wine | unmapped | 3 | ฿585 | 0.9% |
| 33 | Alska Nordic Berry Cider 4% | Bottled Beer & Cider & Wine | unmapped | 2 | ฿570 | 0.9% |
| 34 | Es Yen เอส เย็น | Coffee | unmapped | 6 | ฿540 | 0.8% |
| 35 | Smoked string cheese | Snacks & Bites | unmapped | 4 | ฿480 | 0.7% |
| 36 | Taps Taco Trio - Rtd cocktail | Taps Taco Trio | unmapped | 1 | ฿450 | 0.7% |
| 37 | Schweppes vodka manao/citrus 5% | Bottled Beer & Cider & Wine | unmapped | 5 | ฿400 | 0.6% |
| 38 | Mango& passion fruit wheat | Bottled Beer & Cider & Wine | unmapped | 2 | ฿390 | 0.6% |
| 39 | Original Kai Ka-ta ไข่กระทะ ออริจินัล | Breakfast | unmapped | 3 | ฿390 | 0.6% |
| 40 | Grilled Chicken Rice ข้าวไก่ย่างคลุกฝุ่น | Rice Bowls | unmapped | 2 | ฿280 | 0.4% |
| 41 | Fried Garlic Tofu เต้าหู้คั่วพริกเกลือ | Snacks & Bites | unmapped | 2 | ฿260 | 0.4% |
| 42 | 330 ml - S&B West Coast Anda IPA | Beer Taps | unmapped | 1 | ฿210 | 0.3% |
| 43 | Soda โซดา | Soft Drinks | unmapped | 7 | ฿210 | 0.3% |
| 44 | Extra | Food Extras | unmapped | 4 | ฿180 | 0.3% |
| 45 | Fried Egg ไข่ดาว | Food Extras | unmapped | 7 | ฿140 | 0.2% |
| 46 | Milk นม | No category | unmapped | 2 | ฿140 | 0.2% |
| 47 | Plain Toast ขนมปังเปล่า (White Bread) | Food Extras | unmapped | 3 | ฿90 | 0.1% |
| 48 | 7 Up | Soft Drinks | unmapped | 2 | ฿60 | 0.1% |
| 49 | Avocado อโวคาโด | Food Extras | unmapped | 1 | ฿60 | 0.1% |
| 50 | Plain Rice ข้าวญี่ปุ่น | Food Extras | unmapped | 2 | ฿60 | 0.1% |
| 51 | Plain Toast ขนมปังเปล่า (Sourdough) | Food Extras | unmapped | 2 | ฿60 | 0.1% |
| 52 | Scrambled Egg ไข่คน | Food Extras | unmapped | 2 | ฿40 | 0.1% |

## Q2 — discount gap

Discounts total ฿11,985 for the window. Books values each SALE line at `price × quantity` gross of any discount, so the Books headline *includes* the undiscounted price of every discounted line. **The discount gap is a Gross-vs-Net delta, not a Books-vs-Loyverse delta.** Zero impact on the missing-revenue gap; it explains part of why Loyverse Net (฿153,330) sits below Gross (฿166,235).

## Q3 — refund gap

Refunds total ฿920 for the window. Books' sync parser skips every REFUND receipt, so refunded revenue never enters Books in either direction. **Same axis as Q2:** the refund gap is between Gross and Net, not between Books and Loyverse. Zero impact on the missing-revenue gap.

## Q4 — UTC date-bucketing leak

Books stores each SALE's date as the UTC calendar date of `created_at`. Asia/Bangkok is UTC+7, so a SALE near local midnight can bucket into the previous UTC day. This only crosses a Books month boundary when the local date is in one month and the UTC date in another — i.e. the last day of a month or the first day of the next.

The reconciliation window 2026-07-01 .. 2026-07-21 **does not cross a calendar month boundary**, so Q4 is **not applicable** for this window. Any month-boundary window (e.g. a full-July export spanning Jul 1 and Jul 31) would need this check; it cannot be done from the dashboard CSVs (which bucket by Loyverse's local date) — it requires either the prod DB or a fresh Loyverse API pull carrying the UTC timestamp.

## Q5 — remainder

Closing the loop: Loyverse Gross should equal mapped revenue + flagged revenue (unknown_price + unmapped). Any non-zero remainder is unexplained.

| Term | THB |
| --- | ---: |
| Loyverse Gross | ฿166,235 |
| − mapped revenue | −฿102,070 |
| − unknown_price revenue | −฿0 |
| − unmapped revenue | −฿64,165 |
| **= remainder** | **฿0** |

**The parts reconcile to the baht.** The pre-#71-vs-Gross gap is fully explained by unmapped revenue (plus zero unknown_price revenue this window). Q2 (discounts) and Q3 (refunds) are Gross-vs-Net deltas and do not contribute.

## Appendix — category-level Gross (Loyverse)

Useful as the input to the segmentation decision (#65, pure clock; #73 implementation). Categories are Loyverse's; the post-#73 segment call is by clock, not category, so this table is informational only.

| Category | Items sold | Gross | % of Gross |
| --- | ---: | ---: | ---: |
| Beer Taps | 136 | ฿33,630 | 20.2% |
| Coffee | 260 | ฿23,395 | 14.1% |
| Breakfast | 76 | ฿14,400 | 8.7% |
| Rice Bowls | 72 | ฿12,340 | 7.4% |
| Signature Drinks | 93 | ฿10,700 | 6.4% |
| Poke bowls | 40 | ฿9,420 | 5.7% |
| Sushi Tacos | 43 | ฿8,940 | 5.4% |
| Snacks & Bites | 71 | ฿8,120 | 4.9% |
| Bottled Beer & Cider & Wine | 29 | ฿7,325 | 4.4% |
| Sandwiches | 35 | ฿6,980 | 4.2% |
| Matcha | 39 | ฿4,315 | 2.6% |
| Pasta | 23 | ฿4,140 | 2.5% |
| Cold-Pressed Juice | 27 | ฿3,760 | 2.3% |
| Desserts | 23 | ฿3,760 | 2.3% |
| Non-Coffee | 28 | ฿3,170 | 1.9% |
| Soft Drinks | 100 | ฿3,140 | 1.9% |
| Tea | 32 | ฿2,820 | 1.7% |
| Salads | 12 | ฿2,630 | 1.6% |
| Sweet Soda | 20 | ฿2,030 | 1.2% |
| Food Extras | 21 | ฿630 | 0.4% |
| Taps Taco Trio | 1 | ฿450 | 0.3% |
| No category | 2 | ฿140 | 0.1% |

## Conclusion

- For window 2026-07-01 .. 2026-07-21, Loyverse Gross was ฿166,235; Books' post-#71 headline ties to it by construction. The pre-#71 rule would have shown ฿102,070 — a ฿64,165 gap.
- **Q1**: the gap is entirely unmapped revenue — ฿64,165 across 52 items, dominated by draught beer (Beer Taps, ฿33,630 — no recipes by design, per the recipes.yaml header) and a long tail of menu items added since the recipe book was last refreshed.
- **No unknown_price revenue this window** — every mapped SKU has a recipe. (Ingredient-level pricing can't be checked without the prod DB; the per-SKU `costs` table there would surface any unpriced leaves.)
- **Q2 (฿11,985 discounts) and Q3 (฿920 refunds) do not contribute** to the Books-vs-Loyverse gap. They are Gross-vs-Net deltas. Books' gross-of-discount line valuation and REFUND-skipping put its headline on the Gross axis.
- **Q4 UTC leak**: N/A — the window does not cross a month boundary.
- **Q5 remainder**: ฿0 — the parts reconcile to the baht.

---

The headline half of the map's destination is delivered by #71 / ADR-0008: post-#71 Books headline equals Loyverse Gross by construction. The remaining destination work is cost-side (Books → Loyverse cost CSV; Loyverse's own COGS this window was ฿35,689 at 70.8% gross margin) and segmentation (#73 pure-clock).

The largest unmapped-revenue cluster — draught beer — is the natural first target for a costing pass: it is high-volume, high-margin, and follows the serving-recipe pattern (one keg SKU per brand, one pours-per-ml recipe per size). Closing it would absorb most of the flagged_revenue into the reliable-rows side and bring Books' COGS into the conversation against Loyverse's ฿35,689 cost figure.
