"""Pure IN-01 five-number derivation (issue #147, semantics locked in #142).

From receipt-grain facts to the five numbers per trading day:

    (date, cash, QR, card, discount)

Per the #142 resolution the derivation is a **pure function** over the facts
table, computed on the fly — no per-day persisted table. Delivery and its
idempotence are the delivery-mechanism ticket's concern; a late-arriving
receipt legitimately changes a day's numbers on re-sync (day-mutation policy
stays where the map put it). This mirrors ADR-0011's shape: raw Loyverse
facts persisted, derived views pure.

Semantics (the seven locked decisions in brief):

- **Trading day** — a venue-local calendar day with ≥1 receipt of either
  type. A closed day with no receipts emits **nothing** — no row, not zeros.
  A refund-only day *is* a trading day; its channels may honestly go
  negative.
- **Channels** — cash / QR / card per day = Σ of that day's receipts'
  channel splits. REFUND receipts contribute their negatives on their own
  day and channel (P-11); no re-attribution to the original sale's day or
  channel.
- **Zero semantics** — within a trading day the full five-tuple is emitted
  including ``0.00`` channels (a no-card day carries ``card=0``); the
  receiving gate drops zero lines per the frozen IN-01 template. "Emit
  nothing" belongs to the day level only, never the field level.
- The derivation never sends a gross — ``cash + QR + card + discount`` is
  computed by the receiving side (Contract A, unchanged).

The tool never sends a gross: the GROSS identity is the books' job.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..types import Channel
from .store import ReceiptFact


@dataclass(frozen=True)
class DayFiveNumbers:
    """The five IN-01 numbers for one trading day.

    ``cash`` / ``qr`` / ``card`` are net-collected per channel (refunds as
    negatives on their own day); ``discount`` is the day's Σ receipt
    discounts, refunds never included.
    """

    date: str  # ISO-8601 venue-local calendar day
    cash: Decimal
    qr: Decimal
    card: Decimal
    discount: Decimal


def derive_five_numbers(
    facts: list[ReceiptFact],
) -> list[DayFiveNumbers]:
    """Aggregate receipt facts into per-trading-day five-number tuples.

    Pure: facts in, tuples out, ordered by date ascending. Days with no
    receipts are absent (a trading day requires ≥1 receipt of either type).
    """
    sums: dict[str, dict[str, Decimal]] = {}
    for fact in facts:
        day = sums.setdefault(
            fact.local_date.isoformat(),
            {ch.value: Decimal("0") for ch in Channel},
        )
        day[Channel.CASH.value] += fact.cash
        day[Channel.QR.value] += fact.qr
        day[Channel.CARD.value] += fact.card
        day["discount"] = day.get("discount", Decimal("0")) + fact.discount
    return [
        DayFiveNumbers(
            date=day,
            cash=channels[Channel.CASH.value],
            qr=channels[Channel.QR.value],
            card=channels[Channel.CARD.value],
            discount=channels["discount"],
        )
        for day, channels in sorted(sums.items())
    ]
