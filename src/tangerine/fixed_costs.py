"""Fixed costs: recurring + one-off, day-apportioned for sub-month periods.

Wave 2 slice 3 (ADR-0004 decision 3). A fixed cost is entity-level (rent,
utilities, shared staff, insurance) and is never allocated to a segment — it
sits above the segment line and turns contribution margin into net profit.
A **recurring** cost is defined once with a monthly amount and auto-applies
every month from its first month until ended; a **one-off** applies to a
single month.

For a range covering whole calendar months the result is exact. For a range
partially covering a month, each applicable cost is day-apportioned —
``(days in range / days in month) × monthly amount`` — and the result carries
``estimated=True`` so no surface can present it as exact (see the Fixed
costs entry in ``CONTEXT.md``: apportionment is a documented estimate).

Pure engine — no I/O, no storage imports.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from .types import Money, YearMonth


@dataclass(frozen=True)
class FixedCostEntry:
    """One stored fixed cost, as the Admin surface captures it.

    ``kind`` is ``"recurring"`` (applies every month from ``period`` until
    ``ended_at``'s month, inclusive) or ``"oneoff"`` (applies only in
    ``period``). ``amount`` is the monthly amount in THB.
    """

    entry_id: int
    label: str
    category: str
    amount: Money
    kind: str
    period: YearMonth
    ended_at: date | None = None


@dataclass(frozen=True)
class FixedCostLine:
    """One cost's contribution to a period — what the report renders.

    ``amount`` is the THB charged to this period; when ``apportioned`` it is
    the day-apportioned fraction of ``monthly_amount``, otherwise the two are
    equal.
    """

    label: str
    category: str
    monthly_amount: Money
    amount: Money
    apportioned: bool


@dataclass(frozen=True)
class FixedCostsForPeriod:
    """Fixed costs summed over an inclusive ``[start, end]`` range.

    ``estimated`` is True when any month in the range was only partially
    covered (so at least one line is apportioned) — the flag every surface
    must label its net profit with.
    """

    estimated: bool
    lines: tuple[FixedCostLine, ...]
    total: Money


def fixed_costs_for_period(
    *, start: date, end: date, entries: list[FixedCostEntry]
) -> FixedCostsForPeriod:
    """The fixed costs applying to the inclusive ``[start, end]`` range."""
    lines: list[FixedCostLine] = []
    for entry in entries:
        line = _line_for_entry(entry, start, end)
        if line is not None:
            lines.append(line)
    return FixedCostsForPeriod(
        estimated=any(line.apportioned for line in lines),
        lines=tuple(lines),
        total=sum((line.amount for line in lines), Money("0")),
    )


def _line_for_entry(
    entry: FixedCostEntry, start: date, end: date
) -> FixedCostLine | None:
    """One entry's line over the range, or ``None`` when it doesn't apply."""
    amount = Money("0")
    apportioned = False
    applied = False
    for month, month_start, month_end in _months_overlapping(start, end):
        if not _applies_in_month(entry, month):
            continue
        applied = True
        covered_days = (min(end, month_end) - max(start, month_start)).days + 1
        days_in_month = (month_end - month_start).days + 1
        if covered_days == days_in_month:
            amount += entry.amount
        else:
            apportioned = True
            amount += (
                entry.amount * covered_days / days_in_month
            ).quantize(Money("0.01"))
    if not applied:
        return None
    return FixedCostLine(
        label=entry.label,
        category=entry.category,
        monthly_amount=entry.amount,
        amount=amount,
        apportioned=apportioned,
    )


def _applies_in_month(entry: FixedCostEntry, month: YearMonth) -> bool:
    """Whether ``entry`` charges its monthly amount in ``month``.

    A one-off applies only in its ``period``. A recurring entry applies from
    its ``period`` until the month of ``ended_at``, inclusive — ending a
    cost mid-month does not un-charge the month it was ended in.
    """
    if entry.kind == "oneoff":
        return month == entry.period
    if month < entry.period:
        return False
    if entry.ended_at is not None:
        return month <= (entry.ended_at.year, entry.ended_at.month)
    return True


def _months_overlapping(
    start: date, end: date
) -> list[tuple[YearMonth, date, date]]:
    """Each calendar month touching ``[start, end]``, with its own bounds."""
    months: list[tuple[YearMonth, date, date]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        last_day = calendar.monthrange(year, month)[1]
        months.append(
            ((year, month), date(year, month, 1), date(year, month, last_day))
        )
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


__all__ = [
    "FixedCostEntry",
    "FixedCostLine",
    "FixedCostsForPeriod",
    "fixed_costs_for_period",
]
