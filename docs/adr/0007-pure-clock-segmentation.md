# ADR-0007: Pure-clock segmentation for revenue (reversing slice 07)

Date: 2026-07-23

## Status

Accepted.

Reverses the slice-07 revenue-segmentation rule documented in
`docs/issues/07-segment-tagging-and-contribution-margin.md` and the
`segment_of_sale` docstring as it stood pre-#73. The menu-segment axis
of slice 07 (recipe / Loyverse category → segment for `/items`, `/skus`,
`coverage.py`) is **unchanged** — see ADR-0009 for how that axis is now
sourced.

## Context

Slice 07 split revenue across the two segments (cafe, bar) using a
**category-wins** rule:

- a **mapped** sale took its revenue segment from its recipe
  (``recipe.segment``, sourced from the Loyverse category);
- an **unmapped** sale fell back to a shift-stamped segment the Loyverse
  parser resolved at the sync boundary from the receipt's local timestamp
  (``[8, 17)`` = cafe, else bar).

The fallback existed because an unmapped sale has no recipe to read a
segment from; slice 07 treated the fallback as the second-class path.

Issue #65 grilled this against the venue's actual shape. The cafe is
8am–5pm; the bar (Taps) is 5pm–10pm; the venue is a single physical
space that flips concept at 5pm. Two facts exposed the category-wins
rule as wrong for **revenue**:

1. **The same item sells in both shifts.** A draught beer (a bar-recipe
   item) sells at 4:30pm *and* at 6pm. Under category-wins both sales
   rolled into the bar card — the cafe card never saw the 4:30pm pour's
   revenue, even though that pour happened during the cafe shift. The
   partner reading "CAFE · DAY" at 9am was being lied to about how
   much money the day side of the business actually made. Conversely a
   cappuccino sold at 7pm rolled into cafe, inflating the day card with
   a night sale.

2. **Unmapped revenue had no per-segment home.** Slice 07 surfaced
   unmapped revenue only at the daily level (``flagged_revenue``); a
   partner could not see *which* segment the uncosted revenue sat in.
   Under pure-clock every sale flows into a segment by clock, so the
   unmapped revenue has a natural per-card home — it just has to be
   shown honestly, without booking it into the card's CM.

The clock is the only honest source of "which side of the business this
sale belongs to". The recipe / Loyverse category is a fact about the
**menu** (a cappuccino is a cafe *menu item*), not about the **sale**
(a cappuccino sold at 7pm is bar *revenue*).

## Decision

A sale's segment for **revenue splitting** is decided **entirely by its
local timestamp**, never by its recipe.

**1. ``segment_of_sale`` is pure-clock.** The function returns the
sale's pre-resolved clock-stamped segment (``sale.segment``, the value
the Loyverse parser resolved from the local ``created_at`` at the sync
boundary — post-#66, in Asia/Bangkok). The ``recipe`` argument is kept
in the signature for source compatibility (every caller passes it), but
it no longer influences the result.

**2. The clock rule is ``[8, 17)`` local = cafe, else bar.** Half-open
at 17:00 — 5pm sharp is bar. Out-of-hours sales (early morning, late
night) default to bar so they are never dropped. Bangkok has no DST, so
the offset is a fixed +07:00 forever. No third segment exists.

**3. ``recipe.segment`` becomes a menu-shape fact only.** It still tags
recipes and items for menu-shape views (``/items``, ``/skus``,
``coverage.py``) and is inherited by the sold-as-is quick-create's
output SKU. It no longer drives revenue splitting. ADR-0009 records
how the menu-segment is sourced (configurable set of Loyverse cafe
category UUIDs, replacing slice 02's placeholder `cat-cafe`).

**4. Per-item aggregation keys by ``(item_id, segment)``.** An item sold
in both shifts on the same day produces **two** ``ItemMargin`` rows —
one carrying only its cafe-window units/revenue, the other only its
bar-window units/revenue. This is what makes a clock-segment split
honest: revenue on each segment card ties to the item rows behind it,
with no phantom revenue in a card whose items never sold there. Pre-#73
aggregation keyed by ``item_id`` alone and let the recipe's segment win,
so cross-shift items silently mis-split.

**5. Unmapped revenue lands per-card as flagged, excluded from CM.**
``SegmentMargin.flagged_revenue`` carries the unmapped / unknown-price
revenue that landed in that segment by clock. The card surfaces it as a
labelled line ("+ N THB uncosted revenue in this segment") so a partner
sees *which* segment the uncosted revenue sits in. It is **excluded**
from the card's contribution margin — a flagged row's COGS is unknown,
so booking its revenue into the CM would over-state the segment (the
slice-07 "clean and defensible" rule, applied per-card post-#73). The
daily-level ``flagged_revenue`` total is unchanged.

**6. The Loyverse parser is the single stamping point.** It already
resolves the clock segment from the local ``created_at`` (post-#66);
every downstream surface trusts that stamp via ``segment_of_sale``.
There is no second resolution at margin time.

## Trade-off

A beer at 2pm is cafe; a cappuccino at 7pm is bar. Neither "follows" its
recipe. This is intentional: a partner reading the CAFE card at 9am
wants to know how much money the **day shift** made, regardless of
whether some of that money came from a beer poured at 4:30pm. The recipe
segment still answers the menu-shape question ("is a cappuccino a cafe
item?") — it just no longer answers the revenue question.

The cost is that an item's menu segment and its revenue segment can
disagree. This is acceptable because they answer different questions and
are surfaced on different views (menu-shape on `/items` and `/skus`;
revenue on the segment cards).

## Prerequisite

This rule means nothing without correct local timestamps. Every revenue
row's segment depends on the local hour, so #66 (UTC→Asia/Bangkok
conversion at the parser) is a hard prerequisite — landing #73 before
#66 would have made the segment cards *more* wrong, not less (an
18:00-local bar sale would have stamped cafe under the UTC hour).

## Consequences

- A beer sold at 2pm appears on the CAFE card; a cappuccino sold at 7pm
  appears on the TAPS card. Each segment card reflects the shift that
  actually earned the revenue.
- An item sold in both shifts produces two item-margin rows. The daily
  review's item table therefore shows the same item twice on a cross-
  shift day — this is honest, not a bug. (Ranked top/bottom lists
  continue to filter to reliable rows; a cross-shift item appears once
  per segment, each row carrying only that segment's units.)
- Unmapped revenue now has a per-segment home on the card it belongs to,
  with an honest label and no contribution to CM. The daily-level
  ``flagged_revenue`` total is unchanged.
- Slice-07 tests that built synthetic sales without an explicit
  ``segment=`` were updated to stamp the segment explicitly. Pre-#73 an
  unset segment defaulted to the recipe's segment; under pure-clock an
  unset segment defaults to bar (``segment_of_sale``'s out-of-hours
  default), which would silently mis-split revenue. Production sales
  always have a stamp post-#66; the test convention now matches.
- The monthly accrual view (``monthly_pnl._revenue_by_segment``) was
  already clock-driven via ``segment_of_sale``; its docstring is updated
  to drop the "slice-07 recipe segment" framing.
