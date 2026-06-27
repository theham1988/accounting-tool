"""Segment tagging (slice 07).

Every transaction, recipe, and item is tagged ``cafe`` or ``bar`` (PRD
"Segmentation"). The default source is the Loyverse category, carried via the
recipe; the shift-timestamp fallback is used when a sale has no recipe (an
unmapped item) — in that case the Loyverse parser resolves the segment from
the receipt's ``created_at`` (8am–5pm = cafe, else bar) and stamps it on the
``Sale``.

These functions are the single place the resolution rule lives; the margin
engine calls them when building per-segment contribution margin.

Shift windows (PRD: cafe 8am–5pm, bar 5pm–10pm):

- ``[8, 17)``  -> ``cafe``
- ``[17, 22)`` -> ``bar``
- anything outside (early morning, late night) -> ``bar``

Out-of-hours sales default to bar (the late shift) rather than being dropped,
so an after-hours sale is never lost. This is a documented default, not a
third segment; the venue has exactly two segments.
"""

from __future__ import annotations

from datetime import datetime

from .types import Recipe, Sale, Segment

# Cafe shift window (PRD: cafe 8am–5pm). Half-open: 8 inclusive, 17 exclusive.
CAFE_OPEN_HOUR = 8
CAFE_CLOSE_HOUR = 17  # 5pm handoff (exclusive)

# The bar window is nominally 5pm–10pm (PRD), but out-of-hours sales default
# to bar so they are never dropped, so ``segment_for_timestamp`` has a single
# branch: cafe inside the window above, bar everywhere else. There is no third
# segment.


def segment_for_timestamp(ts: datetime) -> Segment:
    """Resolve a segment from a transaction timestamp (the shift fallback).

    ``[8, 17)`` -> ``cafe``; everything else -> ``bar``. The bar window
    nominally ends at 22:00, but out-of-hours sales default to bar so they
    are never dropped on the floor.
    """
    hour = ts.hour
    if CAFE_OPEN_HOUR <= hour < CAFE_CLOSE_HOUR:
        return Segment.CAFE
    return Segment.BAR


def segment_of_sale(sale: Sale, recipe: Recipe | None) -> Segment:
    """Resolve a sale's segment.

    Rule (issue 07): the recipe's segment (the Loyverse category default) wins
    when the sale is mapped; otherwise the sale's pre-resolved shift-stamped
    ``segment`` is the fallback; if neither is set, default to bar (the late
    shift — matches the out-of-hours default).
    """
    if recipe is not None:
        return recipe.segment
    if sale.segment is not None:
        return sale.segment
    return Segment.BAR
