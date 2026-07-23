"""Segment tagging.

Every transaction, recipe, and item carries a ``cafe`` or ``bar`` segment
(PRD "Segmentation"). Two **independent** segment facts exist after
ADR-0007 (issue #73, reversing slice 07):

  - **Clock segment** (revenue splitting). A *sale's* segment is decided
    entirely by its **local** timestamp (the shift-stamped segment the
    Loyverse parser resolved at the sync boundary post-#66, in Asia/Bangkok).
    ``segment_for_timestamp`` is the rule, and it is applied once at the
    parser; the margin engine trusts that stamp. A beer at 2pm is cafe;
    a cappuccino at 7pm is bar — neither follows its recipe.

  - **Menu segment** (recipe / item shape). ``recipe.segment`` is a fact
    about the menu, derived from the Loyverse category (see ADR-0009 for
    how categories map to segments). It drives ``/items``, ``/skus``,
    ``coverage.py``, and the sold-as-is quick-create's inherited stamp —
    but it **no longer drives revenue splitting**.

This module owns the clock rule (the only segment rule left that decides
revenue); the menu-segment rule lives in the menu snapshot
(``parse_items_snapshot``) and the seed config.

Shift windows (PRD: cafe 8am–5pm, bar 5pm–10pm), applied to the local hour:

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
    """Resolve a clock segment from a transaction timestamp.

    ``[8, 17)`` -> ``cafe``; everything else -> ``bar``. Applied at the
    Loyverse parser to the **local** timestamp (post-#66, Asia/Bangkok),
    and the result is stamped on the ``Sale``; this function never sees
    the raw Loyverse UTC timestamp in production. The bar window nominally
    ends at 22:00, but out-of-hours sales default to bar so they are never
    dropped on the floor.
    """
    hour = ts.hour
    if CAFE_OPEN_HOUR <= hour < CAFE_CLOSE_HOUR:
        return Segment.CAFE
    return Segment.BAR


def segment_of_sale(sale: Sale, recipe: Recipe | None = None) -> Segment:
    """Resolve a sale's segment for **revenue splitting** (ADR-0007).

    Pure-clock rule (issue #73): a sale's segment is its clock-stamped
    segment — the one the parser resolved from the local transaction
    timestamp and stamped on ``sale.segment``. The ``recipe`` argument is
    accepted for callers that still pass it, but it **does not influence
    revenue segmentation**; ``recipe.segment`` is a menu-shape fact only
    (see module docstring). Keeping the parameter preserves the existing
    call signature — every caller already passes the recipe — so the
    pure-clock change is contained here, not threaded through every
    call site.

    The clock stamp is always present on production sales (the parser
    stamps every SALE receipt post-#66). The fallback to ``Segment.BAR``
    only triggers on a hand-built ``Sale`` with no stamp — a defensive
    default mirroring the out-of-hours rule, not a path production takes.
    """
    if sale.segment is not None:
        return sale.segment
    return Segment.BAR
