# ADR-0009: Configurable Loyverse category → segment mapping

Date: 2026-07-22

## Status

Accepted.

Reverses the slice-02 hard-coded `CAFE_CATEGORY_ID = "cat-cafe"` placeholder
(`src/tangerine/loyverse/store.py`).

## Context

Slice 02 tagged each Loyverse menu item's segment by comparing the item's
`category_id` against a single hard-coded constant:

```python
CAFE_CATEGORY_ID = "cat-cafe"
```

The comment said slice 07 would generalise this. Slice 07 came and went
without doing so; the constant stayed. The value `"cat-cafe"` is a placeholder
no real Loyverse category UUID matches — Loyverse category ids are opaque
UUIDs unique to each account. So **every menu item's `category_id` failed the
comparison and every item was tagged `bar`**. The cafe showed no items in the
menu-shape views.

Issue #68 surfaced this as part of the "empty Taps segment" map. Issue #65
then reversed slice 07's category-wins rule for **revenue** splitting: under
pure-clock segmentation, a sale's revenue segment comes from the receipt
timestamp, not the item's category. So the bug no longer empties the Taps
revenue card — but `MenuItem.segment` still feeds two places the clock does
not reach:

1. **Menu-shape views** — `/items` and `/skus` display each item's/SKU's
   segment (`MenuItem.segment`, `SkuRecord.segment`). A partner reading the
   page sees every item mis-segmented.
2. **Sold-as-is quick-create** — `/items/{item_id}/sold-as-is` inherits the
   Loyverse item's segment onto the sold SKU it creates
   (`sold_segment = item.segment`), so newly-costed bar items inherit the
   wrong segment without the partner ever hand-tagging it.

The segment on a menu row is a **menu-shape fact** — which half of the menu
this item belongs to. After #65 it no longer drives revenue attribution, but
it still has to be right for the two consumers above. The clock cannot supply
it: a menu item has no timestamp.

Loyverse returns the real category UUID on every `LoyverseItem` payload
(`category_id`). The venue has exactly one cafe category and one bar category
today; Loyverse allows sub-categories, so the shape must accept more than one
UUID per segment. The UUIDs are opaque and unique to this venue's Loyverse
account, so they cannot ship in the repo — they must be configured at runtime,
alongside the existing `LOYVERSE_ACCESS_TOKEN` and `LOYVERSE_STORE_ID`.

## Decision

The parser resolves segment from a **configurable set of cafe category UUIDs**.
`bar` is the complement (the venue has two segments, no third):

```python
DEFAULT_CAFE_CATEGORY_IDS: frozenset[str] = frozenset()

def parse_items_snapshot(
    payload, *,
    store_id=None,
    cafe_category_ids: frozenset[str] = DEFAULT_CAFE_CATEGORY_IDS,
) -> MenuSnapshot: ...
```

**1. Cafe is a set, bar is the complement.** An item whose `category_id` is
in `cafe_category_ids` is cafe; everything else is bar. The set shape carries
Loyverse sub-categories ('Hot Coffee' and 'Cold Brew' as distinct UUIDs both
served in the cafe) without a code change. There is no bar set to configure
— bar is the default.

**2. The default empty set preserves the placeholder's observable behaviour.**
With no UUIDs configured, every item is bar — exactly what the placeholder
produced, because `"cat-cafe"` never matched a real UUID. This is the honest
restatement of the bug: the previous behaviour was "everything is bar by
accident"; now it is "everything is bar by default, sharpening as the partner
configures the real cafe UUID". A fresh deployment starts correct.

**3. The production UUIDs are configured via `LOYVERSE_CAFE_CATEGORY_IDS`
(env, comma-separated).** The partner dumps them via
`scripts/dump_loyverse_items.py` (the `category_id` column) and sets the env
var in `/etc/tangerine/env`, alongside the other Loyverse credentials. The
sync (`SyncOrchestrator`, `run_sync`) and the `POST /sync` route both forward
the set to the parser; `python -m tangerine.sync` (cron) and the web app both
read the env var at startup. Parsing tolerates whitespace and trailing
commas so a partner-pasted value cannot silently corrupt the set.

**4. `bar ≡ Taps` still holds.** This ADR does not change the
brand-display-name rule (CONTEXT.md): the rendered card says "TAPS · NIGHT"
for `bar`, the engine value stays `bar`. Configuring cafe category UUIDs
sharpens the menu-shape views; it does not rename anything.

## Consequences

- The slice-02 `CAFE_CATEGORY_ID = "cat-cafe"` constant is removed. Code that
  imported it must move to the configurable `cafe_category_ids` parameter
  (or to `DEFAULT_CAFE_CATEGORY_IDS` for the empty default). The one test
  that imported the constant was updated.

- Under pure-clock segmentation (#65 / ADR-0007) this segment does **not**
  drive revenue attribution. Configuring the cafe UUID fixes menu-shape
  views and sold-as-is inheritance; it does not, on its own, move revenue
  between the CAFE and TAPS cards. The two fixes are independent: #73 (the
  pure-clock implementation) handles the revenue side; this ADR handles the
  menu-shape side.

- A deployment that never configures `LOYVERSE_CAFE_CATEGORY_IDS` stays
  correct rather than wrong: every menu item shows as bar (the documented
  default), never a silent mix of right and wrong. The configuration is
  additive — the partner sharpens it after the first sync, not before the
  first boot.

- The venue's actual cafe category UUID is a fact about its Loyverse account,
  not about the codebase. It is recorded once in `/etc/tangerine/env` and
  surfaces only through the configured set; it never enters the repo, the
  database, or the audit log. `scripts/dump_loyverse_items.py` is the
  documented way to discover it.

- Loyverse `Gross profit` parity (the cost-side of the map's destination,
  #72) is unaffected: the category→segment mapping is a Books-internal
  menu-shape fact; the Loyverse cost CSV import (#69) joins on SKU, not
  category, so the two concerns do not interact.
