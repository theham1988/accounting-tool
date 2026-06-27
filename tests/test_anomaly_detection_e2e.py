"""End-to-end anomaly detection over voids + drawer variance (slice 10).

Per the PRD testing rules these tests read as worked examples: "given two
cashiers where Alice voids 6 of her 20 sales and Bob voids 1 of his 20, Alice's
void rate (0.30) is above the venue median (0.15) and the flag fires." They
feed synthetic voids and shift closes through the real anomaly detector and
assert each of issue 10's four rules:

  - void rate per cashier above venue median
  - void clustering at peak hours (configurable peak window)
  - drawer-short rate per cashier above threshold
  - drawer short three shifts in a row by the same cashier

Plus the cross-cutting AC:

  - flags carry enough context (cashier, period, offending pattern)
  - clean history produces no flags

Scope (issue 10): this slice is the rules-based detector only. The Loyverse
``/voids`` endpoint is not yet plumbed into a store (slice 02 wired SALE/REFUND
receipts only); the detector consumes the minimal ``Void`` boundary type
defined in slice 10. A later slice parses raw Loyverse ``/voids`` payloads into
that same shape. ML/statistical tuning is explicitly deferred (PRD out of
scope).

The period is caller-supplied (``AnomalyConfig.start`` ... ``end``); the 9am
review (slice 11) chooses the window. Records outside the window are ignored.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from tangerine.anomaly import (
    DEFAULT_PEAK_HOURS,
    RUN_OF_SHIFTS_THRESHOLD,
    AnomalyConfig,
    detect_anomalies,
)
from tangerine.cash_drawer import close_shift
from tangerine.types import AnomalyKind, Money, ShiftClose, Void

D = Decimal


# --- helpers: build synthetic voids + closes --------------------------------


def _void(
    *,
    void_id: str,
    cashier_id: str,
    created_at: str,
    item_id: str = "chang-draft-500",
    quantity: int = 1,
    price: str = "120",
) -> Void:
    """One synthetic void (UTC ISO timestamp, mirroring Loyverse ``created_at``)."""
    return Void(
        void_id=void_id,
        cashier_id=cashier_id,
        created_at=datetime.fromisoformat(created_at.replace("Z", "+00:00")),
        item_id=item_id,
        quantity=quantity,
        price=Money(price),
    )


def _close(
    *,
    shift_id: str,
    cashier_id: str,
    closed_at: str,
    variance: str,
) -> ShiftClose:
    """One shift close with a chosen variance.

    The detector only consumes ``cashier_id``, ``closed_at``, and ``variance``
    from a ``ShiftClose``; we synthesise the rest with neutral values so the
    record is internally consistent (``closing = opening + rung_up + variance``).
    """
    v = Money(variance)
    opened = Money("5000")
    rung_up = Money("8000")
    closing = opened + rung_up + v
    return close_shift(
        shift_id=shift_id,
        cashier_id=cashier_id,
        closed_at=datetime.fromisoformat(closed_at.replace("Z", "+00:00")),
        opening_cash=opened,
        closing_cash=closing,
        rung_up_cash=rung_up,
    )


PERIOD = (date(2026, 6, 1), date(2026, 6, 30))


# --- 1. void rate per cashier above venue median -----------------------------


def test_void_rate_above_venue_median_flags_offending_cashier() -> None:
    """AC: "Void rate per cashier is computed and compared to venue median."

    Worked example. 20 sales each for Alice and Bob. Alice voids 6 (rate 0.30),
    Bob voids 1 (rate 0.05). Venue median of {0.30, 0.05} = 0.175. Alice's
    0.30 > 0.175 fires the flag; Bob's 0.05 does not. (Sales counts come from
    the non-void Loyverse receipts and are passed in as ``sales_counts``.)
    """
    voids = [
        *[_void(void_id=f"a-{i}", cashier_id="alice", created_at="2026-06-10T12:00:00Z")
          for i in range(6)],
        _void(void_id="b-0", cashier_id="bob", created_at="2026-06-10T12:00:00Z"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=voids,
        closes=[],
        sales_counts={"alice": 20, "bob": 20},
    )

    void_rate_flags = [f for f in flags if f.kind is AnomalyKind.VOID_RATE_ABOVE_VENUE_MEDIAN]
    flagged = {f.cashier_id for f in void_rate_flags}
    assert flagged == {"alice"}
    alice = next(f for f in void_rate_flags if f.cashier_id == "alice")
    assert alice.observed == D("0.30")
    assert alice.reference == D("0.175")
    assert alice.period_start == PERIOD[0]
    assert alice.period_end == PERIOD[1]
    assert "alice" in alice.detail


def test_void_rate_no_flags_when_all_cashiers_below_median() -> None:
    """Cashiers at or below the venue median do not fire.

    Two cashiers with the same rate (0.10): median = 0.10, neither exceeds it.
    """
    voids = [
        *[_void(void_id=f"a-{i}", cashier_id="alice", created_at="2026-06-10T12:00:00Z")
          for i in range(2)],
        *[_void(void_id=f"b-{i}", cashier_id="bob", created_at="2026-06-10T12:00:00Z")
          for i in range(2)],
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=voids,
        closes=[],
        sales_counts={"alice": 20, "bob": 20},
    )

    assert [f for f in flags if f.kind is AnomalyKind.VOID_RATE_ABOVE_VENUE_MEDIAN] == []


def test_void_rate_ignores_voids_outside_the_period() -> None:
    """A void outside ``[start, end]`` is dropped before rates are computed.

    Two cashiers, 10 sales each. Alice has 1 void in June plus 5 in May (out
    of window); Bob has 1 void in June. In-window rates are both 0.10, median
    0.10, neither fires. **If** the May voids leaked in, Alice's rate would be
    0.60 (6/10), median 0.35, and Alice would fire — so asserting *no* void-
    rate flags proves the window filter worked.
    """
    voids = [
        _void(void_id="a-in", cashier_id="alice", created_at="2026-06-10T12:00:00Z"),
        _void(void_id="b-in", cashier_id="bob", created_at="2026-06-10T12:00:00Z"),
        *[_void(void_id=f"a-out-{i}", cashier_id="alice", created_at="2026-05-10T12:00:00Z")
          for i in range(5)],
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=voids,
        closes=[],
        sales_counts={"alice": 10, "bob": 10},
    )

    assert [f for f in flags if f.kind is AnomalyKind.VOID_RATE_ABOVE_VENUE_MEDIAN] == []


# --- 2. void clustering at peak hours ----------------------------------------


def test_void_clustering_at_peak_flags_cashier_above_share() -> None:
    """AC: "Void clustering at peak hours is detected."

    Worked example. Default peak window is the bar peak (18:00-21:00). Alice
    voids 5 times, 4 of them inside the peak window. Peak share = 4/5 = 0.80.
    Threshold (default 0.5): a cashier whose peak share > 0.5 fires.
    """
    voids = [
        _void(void_id="a-peak-1", cashier_id="alice", created_at="2026-06-10T19:00:00Z"),
        _void(void_id="a-peak-2", cashier_id="alice", created_at="2026-06-10T19:30:00Z"),
        _void(void_id="a-peak-3", cashier_id="alice", created_at="2026-06-11T20:00:00Z"),
        _void(void_id="a-peak-4", cashier_id="alice", created_at="2026-06-11T20:30:00Z"),
        _void(void_id="a-off",    cashier_id="alice", created_at="2026-06-11T12:00:00Z"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
            peak_hours=DEFAULT_PEAK_HOURS,
            peak_share_threshold=D("0.5"),
        ),
        voids=voids,
        closes=[],
        sales_counts={"alice": 20},
    )

    cluster = [f for f in flags if f.kind is AnomalyKind.VOID_CLUSTERING_AT_PEAK]
    assert {f.cashier_id for f in cluster} == {"alice"}
    alice = cluster[0]
    assert alice.observed == D("0.80")
    assert alice.reference == D("0.5")
    assert "peak" in alice.detail.lower()


def test_void_clustering_no_flag_when_voids_spread_out() -> None:
    """A cashier whose voids are spread across peak and off-peak does not fire."""
    voids = [
        _void(void_id="a-1", cashier_id="alice", created_at="2026-06-10T19:00:00Z"),
        _void(void_id="a-2", cashier_id="alice", created_at="2026-06-10T12:00:00Z"),
        _void(void_id="a-3", cashier_id="alice", created_at="2026-06-11T09:00:00Z"),
        _void(void_id="a-4", cashier_id="alice", created_at="2026-06-11T14:00:00Z"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
            peak_hours=DEFAULT_PEAK_HOURS,
            peak_share_threshold=D("0.5"),
        ),
        voids=voids,
        closes=[],
        sales_counts={"alice": 20},
    )

    assert [f for f in flags if f.kind is AnomalyKind.VOID_CLUSTERING_AT_PEAK] == []


def test_void_clustering_disabled_when_peak_window_empty() -> None:
    """An empty ``peak_hours`` disables the clustering rule (no flags)."""
    voids = [
        _void(void_id="a-1", cashier_id="alice", created_at="2026-06-10T19:00:00Z"),
        _void(void_id="a-2", cashier_id="alice", created_at="2026-06-10T19:30:00Z"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
            peak_hours=(),
            peak_share_threshold=D("0.5"),
        ),
        voids=voids,
        closes=[],
        sales_counts={"alice": 5},
    )

    assert [f for f in flags if f.kind is AnomalyKind.VOID_CLUSTERING_AT_PEAK] == []


# --- 3. drawer-short rate per cashier above threshold ------------------------


def test_drawer_short_rate_above_threshold_flags_cashier() -> None:
    """AC: "Drawer-short rate per cashier is computed and compared to threshold."

    Worked example. Alice closes 5 shifts, 3 short (variance < 0). Short rate
    = 3/5 = 0.60. Threshold 0.25 -> fires. Bob closes 5 shifts, 0 short -> no.
    """
    closes = [
        _close(shift_id="a-1", cashier_id="alice", closed_at="2026-06-01T17:00:00Z", variance="-50"),
        _close(shift_id="a-2", cashier_id="alice", closed_at="2026-06-02T17:00:00Z", variance="-80"),
        _close(shift_id="a-3", cashier_id="alice", closed_at="2026-06-03T17:00:00Z", variance="0"),
        _close(shift_id="a-4", cashier_id="alice", closed_at="2026-06-04T17:00:00Z", variance="-30"),
        _close(shift_id="a-5", cashier_id="alice", closed_at="2026-06-05T17:00:00Z", variance="20"),
        *[_close(shift_id=f"b-{i}", cashier_id="bob", closed_at=f"2026-06-0{i+1}T17:00:00Z", variance="0")
          for i in range(1, 6)],
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=[],
        closes=closes,
        sales_counts={},
    )

    short_rate_flags = [f for f in flags if f.kind is AnomalyKind.DRAWER_SHORT_RATE_ABOVE_THRESHOLD]
    assert {f.cashier_id for f in short_rate_flags} == {"alice"}
    alice = short_rate_flags[0]
    assert alice.observed == D("0.60")
    assert alice.reference == D("0.25")


def test_drawer_short_rate_no_flag_below_threshold() -> None:
    """A cashier whose short rate is at/below the threshold does not fire."""
    closes = [
        _close(shift_id="a-1", cashier_id="alice", closed_at="2026-06-01T17:00:00Z", variance="-50"),
        _close(shift_id="a-2", cashier_id="alice", closed_at="2026-06-02T17:00:00Z", variance="0"),
        _close(shift_id="a-3", cashier_id="alice", closed_at="2026-06-03T17:00:00Z", variance="0"),
        _close(shift_id="a-4", cashier_id="alice", closed_at="2026-06-04T17:00:00Z", variance="0"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=[],
        closes=closes,
        sales_counts={},
    )

    assert [f for f in flags if f.kind is AnomalyKind.DRAWER_SHORT_RATE_ABOVE_THRESHOLD] == []


def test_drawer_short_rate_ignores_closes_outside_period() -> None:
    """Closes outside ``[start, end]`` are dropped before rates are computed."""
    closes = [
        # In period: 1 short of 2.
        _close(shift_id="a-1", cashier_id="alice", closed_at="2026-06-01T17:00:00Z", variance="-50"),
        _close(shift_id="a-2", cashier_id="alice", closed_at="2026-06-02T17:00:00Z", variance="0"),
        # Out of period (May): 3 more shorts -> would push rate to 0.80 if counted.
        *[_close(shift_id=f"a-may-{i}", cashier_id="alice", closed_at=f"2026-05-0{i}T17:00:00Z", variance="-50")
          for i in range(1, 4)],
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=[],
        closes=closes,
        sales_counts={},
    )

    short_rate_flags = [f for f in flags if f.kind is AnomalyKind.DRAWER_SHORT_RATE_ABOVE_THRESHOLD]
    # In-period: 1 of 2 = 0.50 > 0.25 -> fires. If May closes had leaked in,
    # alice's rate would still fire but for a different reason; the assertion
    # is on observed value to prove the window filter works.
    assert short_rate_flags
    assert short_rate_flags[0].observed == D("0.50")


def test_drawer_rule_requires_threshold() -> None:
    """No threshold configured -> ValueError rather than a silent default.

    A short rate of "0 of 3" is normal; any non-zero default would silently
    fire on honest cashiers. Surfacing the policy choice forces the caller
    (slice 11) to pick one deliberately.
    """
    config = AnomalyConfig(start=PERIOD[0], end=PERIOD[1])  # no threshold
    closes = [
        _close(shift_id="a-1", cashier_id="alice", closed_at="2026-06-01T17:00:00Z", variance="-50"),
    ]

    with pytest.raises(ValueError, match="drawer_short_rate_threshold"):
        detect_anomalies(config=config, voids=[], closes=closes, sales_counts={})


# --- 4. consecutive short shifts (three in a row) ----------------------------


def test_three_short_shifts_in_a_row_flags_cashier() -> None:
    """AC: "Consecutive short shifts by same cashier are flagged."

    Worked example. Alice has three consecutive short closes (variance < 0)
    with no other cashier or balanced shift in between. The run length is 3 ->
    fires. Bob has two shorts then a balanced shift -> run breaks at 2 -> no.
    """
    closes = [
        _close(shift_id="a-1", cashier_id="alice", closed_at="2026-06-01T17:00:00Z", variance="-50"),
        _close(shift_id="a-2", cashier_id="alice", closed_at="2026-06-02T17:00:00Z", variance="-60"),
        _close(shift_id="a-3", cashier_id="alice", closed_at="2026-06-03T17:00:00Z", variance="-40"),
        _close(shift_id="b-1", cashier_id="bob",   closed_at="2026-06-04T17:00:00Z", variance="-10"),
        _close(shift_id="b-2", cashier_id="bob",   closed_at="2026-06-05T17:00:00Z", variance="-20"),
        _close(shift_id="b-3", cashier_id="bob",   closed_at="2026-06-06T17:00:00Z", variance="0"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=[],
        closes=closes,
        sales_counts={},
    )

    run_flags = [f for f in flags if f.kind is AnomalyKind.DRAWER_SHORT_THREE_SHIFTS_RUNNING]
    assert {f.cashier_id for f in run_flags} == {"alice"}
    alice = run_flags[0]
    assert alice.observed == D("3")
    assert alice.reference == D(RUN_OF_SHIFTS_THRESHOLD)


def test_run_does_not_break_across_another_cashiers_interleaved_close() -> None:
    """AC: "three shifts in a row by the same cashier" keys per-cashier.

    The PRD structure is two partners, one per shift (PRD "Known control gap":
    "two partners (one per shift)"), alternating day/night. So alice's closes
    are normally interleaved with bob's. A "three in a row" rule that broke on
    any other cashier's close would be a no-op in the real rotation — alice
    could never get three. The streak must break only on the SAME cashier's
    own non-short close, not on a different cashier appearing in between.

    Here alice closes short at 5pm on three consecutive days; bob closes his
    own night shifts (any variance) in between. Alice's run of 3 must still
    fire.
    """
    closes = [
        _close(shift_id="a-1", cashier_id="alice", closed_at="2026-06-01T17:00:00Z", variance="-50"),
        _close(shift_id="b-1", cashier_id="bob",   closed_at="2026-06-01T22:00:00Z", variance="0"),
        _close(shift_id="a-2", cashier_id="alice", closed_at="2026-06-02T17:00:00Z", variance="-60"),
        _close(shift_id="b-2", cashier_id="bob",   closed_at="2026-06-02T22:00:00Z", variance="-10"),
        _close(shift_id="a-3", cashier_id="alice", closed_at="2026-06-03T17:00:00Z", variance="-40"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=[],
        closes=closes,
        sales_counts={},
    )

    run_flags = [f for f in flags if f.kind is AnomalyKind.DRAWER_SHORT_THREE_SHIFTS_RUNNING]
    assert {f.cashier_id for f in run_flags} == {"alice"}
    assert run_flags[0].observed == D("3")


def test_run_breaks_when_a_balanced_shift_appears() -> None:
    """A zero-or-over variance close breaks the streak even for the same cashier."""
    closes = [
        _close(shift_id="a-1", cashier_id="alice", closed_at="2026-06-01T17:00:00Z", variance="-50"),
        _close(shift_id="a-2", cashier_id="alice", closed_at="2026-06-02T17:00:00Z", variance="0"),  # breaks
        _close(shift_id="a-3", cashier_id="alice", closed_at="2026-06-03T17:00:00Z", variance="-40"),
        _close(shift_id="a-4", cashier_id="alice", closed_at="2026-06-04T17:00:00Z", variance="-30"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=[],
        closes=closes,
        sales_counts={},
    )

    # Max run is 2 (a-3, a-4); the rule fires only at >= 3.
    assert [f for f in flags if f.kind is AnomalyKind.DRAWER_SHORT_THREE_SHIFTS_RUNNING] == []


# --- 5. flag context carries cashier, period, detail -------------------------


def test_flag_detail_is_human_readable_and_mentions_cashier() -> None:
    """AC: "Flags include enough context (cashier, period, offending pattern)."

    Each flag's ``detail`` is a single readable sentence naming the cashier and
    the offending pattern, so the 9am review can render it verbatim.
    """
    closes = [
        _close(shift_id="a-1", cashier_id="alice", closed_at="2026-06-01T17:00:00Z", variance="-50"),
        _close(shift_id="a-2", cashier_id="alice", closed_at="2026-06-02T17:00:00Z", variance="-60"),
        _close(shift_id="a-3", cashier_id="alice", closed_at="2026-06-03T17:00:00Z", variance="-40"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=[],
        closes=closes,
        sales_counts={},
    )

    assert flags, "expected at least one flag"
    for f in flags:
        assert f.cashier_id in f.detail
        assert f.period_start == PERIOD[0]
        assert f.period_end == PERIOD[1]
        assert f.detail.strip().endswith(".")


# --- 6. clean history produces no flags --------------------------------------


def test_clean_history_produces_no_flags() -> None:
    """AC: "clean history produces no flags."

    Two cashiers, balanced drawers, no voids -> the detector returns an empty
    list. This is the most important AC: an honest team must not be flagged.
    """
    closes = [
        *[_close(shift_id=f"a-{i}", cashier_id="alice", closed_at=f"2026-06-0{i}T17:00:00Z", variance="0")
          for i in range(1, 6)],
        *[_close(shift_id=f"b-{i}", cashier_id="bob", closed_at=f"2026-06-{10+i}T22:00:00Z", variance="0")
          for i in range(5)],
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
        ),
        voids=[],
        closes=closes,
        sales_counts={"alice": 0, "bob": 0},
    )

    assert flags == []


# --- 7. end-to-end: voids + drawers together --------------------------------


def test_end_to_end_voids_and_drawers_fire_expected_flags() -> None:
    """The full slice-10 seam: synthetic voids + closes -> the expected flags.

    Alice: 6 voids of 20 sales (0.30 > median), 4 of her 6 voids in the peak
    window (0.80 > 0.5), 3 consecutive short shifts, 4 of 5 shifts short
    (0.80 > 0.25). Bob: clean. Assert Alice fires all four rule kinds and Bob
    fires none.
    """
    voids = [
        # 4 peak, 2 off-peak for alice (6 of her 20 sales = 0.30).
        _void(void_id="a-1", cashier_id="alice", created_at="2026-06-01T19:00:00Z"),
        _void(void_id="a-2", cashier_id="alice", created_at="2026-06-02T19:30:00Z"),
        _void(void_id="a-3", cashier_id="alice", created_at="2026-06-03T20:00:00Z"),
        _void(void_id="a-4", cashier_id="alice", created_at="2026-06-04T20:30:00Z"),
        _void(void_id="a-5", cashier_id="alice", created_at="2026-06-05T12:00:00Z"),
        _void(void_id="a-6", cashier_id="alice", created_at="2026-06-06T09:00:00Z"),
        # Bob: 1 void of 20 sales = 0.05, so the venue median is 0.175 and
        # alice's 0.30 fires the void-rate rule.
        _void(void_id="b-1", cashier_id="bob", created_at="2026-06-03T13:00:00Z"),
    ]
    closes = [
        _close(shift_id="a-1", cashier_id="alice", closed_at="2026-06-01T22:00:00Z", variance="-50"),
        _close(shift_id="a-2", cashier_id="alice", closed_at="2026-06-02T22:00:00Z", variance="-60"),
        _close(shift_id="a-3", cashier_id="alice", closed_at="2026-06-03T22:00:00Z", variance="-40"),
        _close(shift_id="a-4", cashier_id="alice", closed_at="2026-06-04T22:00:00Z", variance="-30"),
        _close(shift_id="a-5", cashier_id="alice", closed_at="2026-06-05T22:00:00Z", variance="20"),
    ]

    flags = detect_anomalies(
        config=AnomalyConfig(
            start=PERIOD[0],
            end=PERIOD[1],
            drawer_short_rate_threshold=D("0.25"),
            peak_hours=DEFAULT_PEAK_HOURS,
            peak_share_threshold=D("0.5"),
        ),
        voids=voids,
        closes=closes,
        sales_counts={"alice": 20, "bob": 20},
    )

    kinds_for_alice = {f.kind for f in flags if f.cashier_id == "alice"}
    assert kinds_for_alice == {
        AnomalyKind.VOID_RATE_ABOVE_VENUE_MEDIAN,
        AnomalyKind.VOID_CLUSTERING_AT_PEAK,
        AnomalyKind.DRAWER_SHORT_RATE_ABOVE_THRESHOLD,
        AnomalyKind.DRAWER_SHORT_THREE_SHIFTS_RUNNING,
    }
    # Bob fires nothing.
    assert not [f for f in flags if f.cashier_id == "bob"]


def test_default_peak_hours_is_bar_peak_window() -> None:
    """The default peak window is the bar peak (PRD: cafe 8am-5pm, bar 5pm-10pm).

    Bar peak demand is the evening rush; out of the box the clustering rule
    keys on ``[18, 21)``. Callers override it via ``AnomalyConfig.peak_hours``.
    """
    assert DEFAULT_PEAK_HOURS == (18, 21)


def test_default_peak_share_threshold_is_half() -> None:
    """Default peak-share threshold: more than half of a cashier's voids in peak."""
    config = AnomalyConfig(
        start=PERIOD[0], end=PERIOD[1], drawer_short_rate_threshold=D("0.25"),
    )
    assert config.peak_share_threshold == D("0.5")
