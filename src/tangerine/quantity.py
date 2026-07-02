"""Quantity shorthand for the recipe editor (Wave 1.5, Slice 4).

Per ADR-0003 decision 3: the stored unit vocabulary is strict (``g`` /
``ml`` / ``unit``), but partners think in Thai spoon measures. The editor
accepts ``1 tbsp`` and converts to the ingredient's canonical unit before
saving — the shorthand names a spoon, the ingredient's ``unit`` field
decides whether the 15 means 15 ml (milk) or 15 g (flour). The data
underneath stays canonical; shorthand never reaches the database.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

#: What one spoon/pinch/knob/grind holds, in the ingredient's canonical
#: unit (ml for liquids, g for solids — same number either way, which is
#: why the measure itself is unit-agnostic).
_MEASURES: dict[str, Decimal] = {
    "tbsp": Decimal("15"),
    "tsp": Decimal("5"),
    "pinch": Decimal("2"),
    "knob": Decimal("10"),
    "pepper grind": Decimal("0.2"),
}

#: ``<count> <measure>`` with an optional plural "s" ("2 knobs", "3 pinches"
#: is *not* supported — the vocabulary is the partner's own, and they say
#: "3 pinch"; a trailing plain "s" is tolerated).
_SHORTHAND_RE = re.compile(r"^(\d+(?:\.\d+)?)\s+([a-z ]+?)s?$")


class QuantityError(ValueError):
    """A quantity string the editor cannot turn into a canonical number."""


def parse_quantity(text: str, unit: str | None) -> Decimal:
    """Parse an editor quantity into the ingredient's canonical unit.

    A plain number passes through unchanged (it is already canonical);
    it must be positive — a zero or negative quantity is never a real
    recipe row, only a typo. A
    shorthand like ``1 tbsp`` multiplies the count by the measure — but only
    when the ingredient's unit is ``g`` or ``ml``: a spoon of a countable
    (eggs) or of a SKU whose unit is still unconfirmed has no meaning, so
    that raises rather than guessing.
    """
    cleaned = text.strip().lower()
    try:
        plain = Decimal(cleaned)
    except InvalidOperation:
        plain = None
    if plain is not None:
        # Decimal() also accepts "nan"/"inf" — neither is a quantity.
        if not plain.is_finite() or plain <= 0:
            raise QuantityError(
                f"quantity must be a positive number, got {text!r}"
            )
        return plain
    match = _SHORTHAND_RE.match(cleaned)
    if not match:
        raise QuantityError(
            f"cannot read quantity {text!r} — use a number or a measure like '1 tbsp'"
        )
    count_text, measure = match.groups()
    per_measure = _MEASURES.get(measure.strip())
    if per_measure is None:
        raise QuantityError(
            f"unknown measure {measure.strip()!r} — known: {', '.join(sorted(_MEASURES))}"
        )
    if unit not in ("g", "ml"):
        raise QuantityError(
            f"'{measure.strip()}' needs an ingredient measured in g or ml, "
            f"but this one is measured in {unit or 'an unconfirmed unit'}"
        )
    quantity = Decimal(count_text) * per_measure
    if quantity <= 0:
        raise QuantityError(f"quantity must be a positive number, got {text!r}")
    return quantity


__all__ = ["QuantityError", "parse_quantity"]
