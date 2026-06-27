"""Rules-based anomaly detection over voids + drawer variance (slice 10).

There is no on-site manager (PRD "Known control gap"); this module does the
segregation-of-duties work a manager would otherwise do. Per the PRD's "Out of
Scope" note this is the **initial rules-based** detection — ML/statistical
tuning is explicitly deferred to a later slice.

The detector is a pure function: synthetic voids + closes in, ``AnomalyFlag``s
out. The four initial rules (issue 10):

  Voids
    1. void rate per cashier above venue median for the period
    2. void clustering at peak hours (configurable peak window)
  Drawer
    3. drawer-short rate per cashier above threshold
    4. drawer short three shifts in a row by the same cashier

``detect_anomalies`` consumes a list of ``Void`` and a list of ``ShiftClose``
that fall inside ``AnomalyConfig.start`` ... ``end`` (inclusive on both bounds,
keyed on ``Void.created_at.date()`` / ``ShiftClose.closed_at.date()``). Records
outside the window are dropped before any rate is computed. ``sales_counts``
maps cashier -> number of non-void Loyverse sales in the window; the void rate
per cashier is ``void_count / sales_count`` (zero when a cashier has no sales,
which is the honest-cashier case).

Voids carry their own ``cashier_id`` (mirrors Loyverse's ``/voids`` resource
and the drawer side, which keys on ``ShiftClose.cashier_id``). The window is
caller-supplied; the 9am review (slice 11) chooses it ("yesterday",
"trailing 7 days", ...).

The "three in a row" rule (rule 4) counts a streak **per cashier**, ordered by
``closed_at``. The PRD structure is two partners alternating day/night shifts
(PRD "Known control gap": "two partners (one per shift)"), so a given
cashier's closes are normally interleaved with the other's — a rule that
broke the streak on any other cashier's close would be a no-op in the real
rotation. The streak therefore breaks only on the SAME cashier's own
non-short close (``variance >= 0``); another cashier's close in between is
ignored.

Slice 02 wired SALE/REFUND receipts only; Loyverse's ``/voids`` endpoint is
not yet plumbed into a store. This slice consumes the minimal ``Void``
boundary type defined in ``types.py``; a later slice parses raw Loyverse
``/voids`` payloads into that same shape (analogous to
``parse_receipts_to_sales``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median

from .types import (
    AnomalyFlag,
    AnomalyKind,
    ShiftClose,
    Void,
)


#: Default peak-hour window, as ``(start_hour, end_hour)`` exclusive on the end.
#: PRD: cafe 8am-5pm, bar 5pm-10pm. Peak demand is the bar's evening rush, so
#: out of the box the clustering rule keys on ``[18, 21)``. An empty tuple
#: disables the rule (issue 10: peak window is "configurable").
DEFAULT_PEAK_HOURS: tuple[int, int] = (18, 21)


#: Default share of a cashier's voids that must land in the peak window for the
#: clustering rule to fire. More than half is a strong "voids cluster at peak"
#: signal; callers override via ``AnomalyConfig.peak_share_threshold``.
DEFAULT_PEAK_SHARE_THRESHOLD: Decimal = Decimal("0.5")


#: The "three shifts in a row" rule fires at this run length (issue 10).
#: Surfaced as a module constant so the flag's ``reference`` field can name it
#: and so a future slice can relax it without re-architecting.
RUN_OF_SHIFTS_THRESHOLD: int = 3


@dataclass(frozen=True)
class AnomalyConfig:
    """Configuration for one anomaly-detection pass.

    - ``start`` / ``end``  the inclusive window the detector measures over
                           (records outside are dropped). Caller-supplied; the
                           9am review (slice 11) chooses it.
    - ``drawer_short_rate_threshold``
                           a cashier whose short rate (share of their closes
                           with ``variance < 0``) exceeds this fires rule 3.
                           **No default**: a short rate of "0 of 3" is normal,
                           and any non-zero default would silently fire on
                           honest cashiers. The caller must pick one
                           deliberately; passing ``None`` while drawer closes
                           are present raises ``ValueError``.
    - ``peak_hours``       ``(start_hour, end_hour)`` exclusive on the end, or
                           ``()`` to disable rule 2. Default is the bar peak.
    - ``peak_share_threshold``
                           share of a cashier's in-window voids that must land
                           inside ``peak_hours`` for rule 2 to fire. Default
                           ``0.5`` (more than half).
    """

    start: date
    end: date
    drawer_short_rate_threshold: Decimal | None = None
    peak_hours: tuple[int, int] = DEFAULT_PEAK_HOURS
    peak_share_threshold: Decimal = DEFAULT_PEAK_SHARE_THRESHOLD


def detect_anomalies(
    *,
    config: AnomalyConfig,
    voids: list[Void],
    closes: list[ShiftClose],
    sales_counts: dict[str, int],
) -> list[AnomalyFlag]:
    """Run all four anomaly rules and return every flag that fires.

    Pure function. Records outside ``[config.start, config.end]`` are dropped
    first. The four rules run independently and their flags are concatenated;
    a cashier can appear on several flags (e.g. high void rate AND a short
    run) and each flag names exactly one rule via its ``kind``.

    ``sales_counts`` is the per-cashier non-void Loyverse sale count over the
    same window; the void-rate rule divides each cashier's void count by it.
    A cashier with voids but no sales recorded is treated as rate 0 (their
    voids still feed the clustering rule, but a missing sales count must not
    produce a divide-by-zero flag).

    The drawer-short-rate rule requires ``config.drawer_short_rate_threshold``
    to be set when any closes are passed in; ``None`` raises ``ValueError``
    rather than silently defaulting (issue 10 gives no threshold, and a silent
    default would either always fire or never fire on honest cashiers).
    """
    in_window_voids = [v for v in voids if config.start <= v.created_at.date() <= config.end]
    in_window_closes = [c for c in closes if config.start <= c.closed_at.date() <= config.end]

    flags: list[AnomalyFlag] = []
    flags.extend(_void_rate_flags(config, in_window_voids, sales_counts))
    flags.extend(_void_peak_clustering_flags(config, in_window_voids))
    flags.extend(_drawer_short_rate_flags(config, in_window_closes))
    flags.extend(_drawer_run_flags(config, in_window_closes))
    return flags


# --- rule 1: void rate per cashier above venue median -----------------------


def _void_rate_flags(
    config: AnomalyConfig,
    voids: list[Void],
    sales_counts: dict[str, int],
) -> list[AnomalyFlag]:
    """Flag each cashier whose void rate exceeds the venue median.

    Per-cashier void rate = ``void_count / sales_count``. Cashiers with no
    sales recorded contribute rate 0 (so they never falsely fire). The venue
    median is taken over the same set of cashiers. A cashier fires when their
    rate is **strictly above** the median (a sole cashier equals the median
    and so never self-flags, which is the right behaviour: with only one
    cashier there is no venue to compare against).
    """
    cashiers = _cashiers_in_voids_or_sales(voids, sales_counts)
    if not cashiers:
        return []

    def _rate(cid: str) -> Decimal:
        sales = sales_counts.get(cid, 0)
        if sales <= 0:
            return Decimal("0")
        void_count = sum(1 for v in voids if v.cashier_id == cid)
        return Decimal(void_count) / Decimal(sales)

    rates = {cid: _rate(cid) for cid in cashiers}
    venue_median = median(rates.values()) if rates else Decimal("0")

    flags: list[AnomalyFlag] = []
    for cid, rate in rates.items():
        if rate > venue_median:
            flags.append(
                AnomalyFlag(
                    kind=AnomalyKind.VOID_RATE_ABOVE_VENUE_MEDIAN,
                    cashier_id=cid,
                    period_start=config.start,
                    period_end=config.end,
                    observed=rate,
                    reference=venue_median,
                    detail=(
                        f"{cid}'s void rate ({_pct(rate)}) is above the "
                        f"venue median ({_pct(venue_median)}) for "
                        f"{config.start.isoformat()} to {config.end.isoformat()}."
                    ),
                )
            )
    return flags


# --- rule 2: void clustering at peak hours ----------------------------------


def _void_peak_clustering_flags(
    config: AnomalyConfig,
    voids: list[Void],
) -> list[AnomalyFlag]:
    """Flag each cashier whose peak-hour void share exceeds the threshold.

    A cashier's peak share = (voids inside ``config.peak_hours``) / (their
    total voids). A cashier fires when that share is **strictly above**
    ``config.peak_share_threshold``. Cashiers with no voids never fire. An
    empty ``peak_hours`` disables the rule entirely (returns no flags).
    """
    if not config.peak_hours:
        return []
    peak_lo, peak_hi = config.peak_hours
    threshold = config.peak_share_threshold

    by_cashier: dict[str, list[Void]] = {}
    for v in voids:
        by_cashier.setdefault(v.cashier_id, []).append(v)

    flags: list[AnomalyFlag] = []
    for cid, c_voids in by_cashier.items():
        total = len(c_voids)
        if total == 0:
            continue
        in_peak = sum(1 for v in c_voids if peak_lo <= v.created_at.hour < peak_hi)
        share = Decimal(in_peak) / Decimal(total)
        if share > threshold:
            flags.append(
                AnomalyFlag(
                    kind=AnomalyKind.VOID_CLUSTERING_AT_PEAK,
                    cashier_id=cid,
                    period_start=config.start,
                    period_end=config.end,
                    observed=share,
                    reference=threshold,
                    detail=(
                        f"{in_peak} of {total} of {cid}'s voids "
                        f"({_pct(share)}) fell in the peak hours "
                        f"{peak_lo:02d}:00-{peak_hi:02d}:00."
                    ),
                )
            )
    return flags


# --- rule 3: drawer-short rate per cashier above threshold ------------------


def _drawer_short_rate_flags(
    config: AnomalyConfig,
    closes: list[ShiftClose],
) -> list[AnomalyFlag]:
    """Flag each cashier whose drawer-short rate exceeds the threshold.

    Per-cashier short rate = (closes with ``variance < 0``) / (total closes),
    both over the window. The threshold is ``config.drawer_short_rate_threshold``;
    if it is ``None`` and any closes are present, raise ``ValueError`` (no
    silent default — see ``AnomalyConfig``).
    """
    threshold = config.drawer_short_rate_threshold
    if threshold is None:
        if closes:
            raise ValueError(
                "drawer_short_rate_threshold must be set when running the "
                "drawer-short-rate rule (issue 10 specifies no default; the "
                "9am review must choose the policy deliberately)."
            )
        return []

    by_cashier: dict[str, list[ShiftClose]] = {}
    for c in closes:
        by_cashier.setdefault(c.cashier_id, []).append(c)

    flags: list[AnomalyFlag] = []
    for cid, c_closes in by_cashier.items():
        total = len(c_closes)
        if total == 0:
            continue
        shorts = sum(1 for c in c_closes if c.variance < 0)
        rate = Decimal(shorts) / Decimal(total)
        if rate > threshold:
            flags.append(
                AnomalyFlag(
                    kind=AnomalyKind.DRAWER_SHORT_RATE_ABOVE_THRESHOLD,
                    cashier_id=cid,
                    period_start=config.start,
                    period_end=config.end,
                    observed=rate,
                    reference=threshold,
                    detail=(
                        f"{cid} was short on {shorts} of {total} closes "
                        f"({_pct(rate)}), above the {_pct(threshold)} threshold."
                    ),
                )
            )
    return flags


# --- rule 4: drawer short three shifts in a row -----------------------------


def _drawer_run_flags(
    config: AnomalyConfig,
    closes: list[ShiftClose],
) -> list[AnomalyFlag]:
    """Flag each cashier with a run of ``RUN_OF_SHIFTS_THRESHOLD``+ short closes.

    "In a row" is counted **per cashier**, ordered by ``closed_at``. The PRD
    structure is two partners alternating day/night shifts, so a given
    cashier's closes are normally interleaved with the other partner's. A rule
    that broke the streak on any other cashier's close would be unreachable in
    the real rotation; the streak therefore breaks only on the SAME cashier's
    own non-short close (``variance >= 0``). Another cashier's close in
    between is ignored — it does not extend alice's run, but it does not end
    it either.

    A cashier fires once if their longest such run is at least
    ``RUN_OF_SHIFTS_THRESHOLD``; the flag's ``observed`` is that longest run.
    """
    threshold = Decimal(RUN_OF_SHIFTS_THRESHOLD)

    longest_per_cashier: dict[str, int] = {}
    # Per-cashier running streaks, advanced as we walk closes in time order.
    # A non-short close by cashier X resets X's streak to 0; a short close
    # extends it. Other cashiers' closes do not touch X's streak.
    streaks: dict[str, int] = {}
    for c in sorted(closes, key=lambda cl: cl.closed_at):
        cid = c.cashier_id
        if c.variance < 0:
            streaks[cid] = streaks.get(cid, 0) + 1
        else:
            streaks[cid] = 0
        longest_per_cashier[cid] = max(longest_per_cashier.get(cid, 0), streaks[cid])

    flags: list[AnomalyFlag] = []
    for cid, longest in longest_per_cashier.items():
        if longest >= RUN_OF_SHIFTS_THRESHOLD:
            flags.append(
                AnomalyFlag(
                    kind=AnomalyKind.DRAWER_SHORT_THREE_SHIFTS_RUNNING,
                    cashier_id=cid,
                    period_start=config.start,
                    period_end=config.end,
                    observed=Decimal(longest),
                    reference=threshold,
                    detail=(
                        f"{cid} was short on {longest} consecutive closes "
                        f"(threshold {RUN_OF_SHIFTS_THRESHOLD})."
                    ),
                )
            )
    return flags


# --- helpers ----------------------------------------------------------------


def _cashiers_in_voids_or_sales(
    voids: list[Void], sales_counts: dict[str, int]
) -> list[str]:
    """Distinct cashier ids appearing in voids or sales_counts, sorted."""
    ids: set[str] = set(sales_counts.keys())
    ids.update(v.cashier_id for v in voids)
    return sorted(ids)


def _pct(x: Decimal) -> str:
    """Format a 0..1 ratio as a human-readable percentage for flag details."""
    return f"{(x * 100):.0f}%"


__all__ = [
    "AnomalyConfig",
    "DEFAULT_PEAK_HOURS",
    "DEFAULT_PEAK_SHARE_THRESHOLD",
    "RUN_OF_SHIFTS_THRESHOLD",
    "detect_anomalies",
]
