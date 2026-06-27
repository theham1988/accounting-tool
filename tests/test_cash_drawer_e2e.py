"""End-to-end cash drawer reconciliation + 5pm handoff test seam (slice 09).

Per the PRD testing rules these tests read as worked examples: "given an
opening drawer of 5000 THB, 8000 THB rung up in cash sales, and a closing
count of 13050 THB, the variance is +50 THB (over)." They feed synthetic
shift closes and handoff recounts through the real cash-drawer engine and
assert:

  - the variance formula: ``closing − (opening + rung_up)`` with the right sign
  - variance stored per shift with cashier identity and timestamp
  - the 5pm handoff recount path: incoming partner recounts the outgoing
    partner's drawer
  - matching recount (within tolerance) lets the shift start
  - mismatch outside tolerance blocks shift start

Scope (issue 09): this slice produces and stores the drawer-variance history
(per shift, per cashier) and enforces the handoff-recount control. The
anomaly detector that *consumes* the variance history (void-rate, drawer-
short-rate, clustering) is slice 10 — explicitly out of scope here.

The agreed recount tolerance default is 0 THB: the 5pm recount is THE control
moment (PRD "Known control gap" — the closing cashier counts their own drawer
and there is no manager), so any discrepancy surfaces. The tolerance is still
a parameter so a future manager can relax it without re-architecting.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from tangerine.cash_drawer import (
    DEFAULT_HANDOFF_TOLERANCE,
    check_handoff,
    close_shift,
)
from tangerine.types import HandoffRecount, Money, ShiftClose

D = Decimal


# --- shared fixtures --------------------------------------------------------


@pytest.fixture
def day_close() -> datetime:
    """The cafe shift close: 5pm handoff moment on a given day."""
    return datetime(2026, 6, 24, 17, 0, 0)


@pytest.fixture
def night_close() -> datetime:
    """The bar shift close: 10pm."""
    return datetime(2026, 6, 24, 22, 0, 0)


@pytest.fixture
def float_amount() -> Money:
    """The drawer float each shift starts from."""
    return Money("5000")


# --- 1. variance = closing − (opening + rung_up) -----------------------------


def test_variance_zero_when_closing_equals_opening_plus_rung_up(
    float_amount: Money, day_close: datetime
) -> None:
    """Worked example: 5000 opening, 8000 rung up, 13000 counted -> 0 variance.

    The drawer balances exactly: opening + rung-up == closing. Variance 0 is
    a balanced drawer.
    """
    record = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )

    assert record.variance == D("0")


def test_variance_positive_when_drawer_is_over(
    float_amount: Money, day_close: datetime
) -> None:
    """Worked example: 5000 opening, 8000 rung up, 13050 counted -> +50 over.

    More cash in the drawer than the system says should be there. Overages are
    surfaced with a positive sign: they can hide mis-rings or tip-jar dumps,
    so the sign matters and is not collapsed to an absolute value.
    """
    record = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13050"),
        rung_up_cash=Money("8000"),
    )

    assert record.variance == D("50")


def test_variance_negative_when_drawer_is_short(
    float_amount: Money, day_close: datetime
) -> None:
    """Worked example: 5000 opening, 8000 rung up, 12900 counted -> -100 short.

    Less cash than expected. Shorts are surfaced with a negative sign — they
    are the theft/error signal slice 10's anomaly detector keys on.
    """
    record = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("12900"),
        rung_up_cash=Money("8000"),
    )

    assert record.variance == D("-100")


# --- 2. variance stored per shift with cashier + timestamp -------------------


def test_shift_close_record_carries_cashier_and_timestamp(
    float_amount: Money, day_close: datetime
) -> None:
    """AC: "Variance is stored per shift with cashier identity and timestamp."

    The record must carry the cashier who counted and when, because per-cashier
    variance history is the input to slice 10's anomaly detector.
    """
    record = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )

    assert record.shift_id == "2026-06-24-day"
    assert record.cashier_id == "alice"
    assert record.closed_at == day_close


def test_two_cashiers_produce_independent_variance_history(
    float_amount: Money, day_close: datetime, night_close: datetime
) -> None:
    """AC: per-cashier variance history. Two cashiers, two shifts, two records.

    The day-shift partner (alice) and the night-shift partner (bob) each close
    their own shift; each record is stamped with its own cashier so a per-
    cashier variance history (slice 10 input) can be built from the records.
    """
    day = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )
    night = close_shift(
        shift_id="2026-06-24-night",
        cashier_id="bob",
        closed_at=night_close,
        # Night opens from the day shift's closing drawer.
        opening_cash=day.closing_cash,
        closing_cash=Money("21000"),
        rung_up_cash=Money("8000"),
    )

    history = {r.cashier_id: r for r in (day, night)}
    assert history["alice"].variance == D("0")
    assert history["bob"].variance == D("0")  # 21000 - (13000 + 8000) = 0


# --- 3. the 5pm handoff recount ---------------------------------------------


def test_handoff_match_within_tolerance_lets_shift_start(
    float_amount: Money, day_close: datetime
) -> None:
    """AC: "5pm handoff requires incoming partner to recount closing drawer."

    The outgoing partner reports 13000 closing. The incoming partner recounts
    and gets 13000. The recount matches exactly (default tolerance is 0 THB),
    so the handoff passes and shift start is NOT blocked.
    """
    outgoing = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )
    recount = HandoffRecount(
        outgoing_shift_id=outgoing.shift_id,
        incoming_cashier_id="bob",
        recounted_at=day_close,
        recounted_cash=Money("13000"),
    )

    result = check_handoff(outgoing, recount)

    assert result.is_blocked is False
    assert result.discrepancy == D("0")


def test_handoff_mismatch_outside_tolerance_blocks_shift_start(
    float_amount: Money, day_close: datetime
) -> None:
    """AC: "Mismatch ... blocks shift start."

    The outgoing partner reports 13000 closing. The incoming partner recounts
    and gets 12800 — a 200 THB short. With the default 0 THB tolerance this is
    outside tolerance, so shift start is BLOCKED. The recount is THE control
    moment (PRD "Known control gap"), so any discrepancy surfaces.
    """
    outgoing = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )
    recount = HandoffRecount(
        outgoing_shift_id=outgoing.shift_id,
        incoming_cashier_id="bob",
        recounted_at=day_close,
        recounted_cash=Money("12800"),
    )

    result = check_handoff(outgoing, recount)

    assert result.is_blocked is True
    # discrepancy is recounted − reported, signed so the review sees over/short.
    assert result.discrepancy == D("-200")


def test_handoff_mismatch_in_either_direction_blocks(
    float_amount: Money, day_close: datetime
) -> None:
    """An over-count (recount > reported) blocks just as a short does.

    Both directions are control signals: an over-count by the incoming partner
    means the outgoing partner under-reported (or mis-counted), which is the
    same loss-of-cash signal in the other direction. The block is symmetric.
    """
    outgoing = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )
    recount = HandoffRecount(
        outgoing_shift_id=outgoing.shift_id,
        incoming_cashier_id="bob",
        recounted_at=day_close,
        recounted_cash=Money("13250"),  # 250 over
    )

    result = check_handoff(outgoing, recount)

    assert result.is_blocked is True
    assert result.discrepancy == D("250")


# --- 4. tolerance default + configurability ---------------------------------


def test_default_handoff_tolerance_is_zero(
    float_amount: Money, day_close: datetime
) -> None:
    """The agreed default tolerance is 0 THB.

    The 5pm recount is THE segregation-of-duties substitute in a two-partner,
    no-manager structure (PRD "Known control gap"), so any discrepancy — even
    one baht — surfaces and blocks. The tolerance is a parameter so a future
    manager can relax it without re-architecting, but the default is strict.
    """
    assert DEFAULT_HANDOFF_TOLERANCE == D("0")

    outgoing = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )
    # Even a 1-THB discrepancy blocks under the strict default.
    recount = HandoffRecount(
        outgoing_shift_id=outgoing.shift_id,
        incoming_cashier_id="bob",
        recounted_at=day_close,
        recounted_cash=Money("12999"),
    )

    result = check_handoff(outgoing, recount)

    assert result.is_blocked is True


def test_relaxed_tolerance_lets_small_discrepancy_pass(
    float_amount: Money, day_close: datetime
) -> None:
    """A non-zero tolerance lets a within-band discrepancy pass.

    A future manager who relaxes the tolerance (e.g. to allow counting noise)
    gets the same engine, just with a wider band. The discrepancy is still
    surfaced on the result so slice 10 can see it.
    """
    outgoing = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )
    recount = HandoffRecount(
        outgoing_shift_id=outgoing.shift_id,
        incoming_cashier_id="bob",
        recounted_at=day_close,
        recounted_cash=Money("12995"),  # 5 short
    )

    result = check_handoff(outgoing, recount, tolerance=Money("10"))

    assert result.is_blocked is False  # 5 <= 10
    assert result.discrepancy == D("-5")  # but still surfaced
    assert result.tolerance == D("10")


def test_relaxed_tolerance_still_blocks_large_discrepancy(
    float_amount: Money, day_close: datetime
) -> None:
    """Even with a relaxed tolerance, a big discrepancy still blocks.

    10-THB tolerance, 200-THB discrepancy -> blocked. The tolerance sets the
    noise floor; anything above it is still a control event.
    """
    outgoing = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )
    recount = HandoffRecount(
        outgoing_shift_id=outgoing.shift_id,
        incoming_cashier_id="bob",
        recounted_at=day_close,
        recounted_cash=Money("12800"),  # 200 short
    )

    result = check_handoff(outgoing, recount, tolerance=Money("10"))

    assert result.is_blocked is True


def test_handoff_result_surfaces_reported_and_recounted_amounts(
    float_amount: Money, day_close: datetime
) -> None:
    """The result carries both amounts so the review can show what happened.

    A blocked handoff is useless without the numbers that triggered it; the
    result carries the reported closing, the recounted amount, the signed
    discrepancy, and the tolerance that was applied.
    """
    outgoing = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13000"),
        rung_up_cash=Money("8000"),
    )
    recount = HandoffRecount(
        outgoing_shift_id=outgoing.shift_id,
        incoming_cashier_id="bob",
        recounted_at=day_close,
        recounted_cash=Money("12800"),
    )

    result = check_handoff(outgoing, recount, tolerance=Money("50"))

    assert result.outgoing_shift_id == "2026-06-24-day"
    assert result.reported_closing_cash == D("13000")
    assert result.recounted_cash == D("12800")
    assert result.discrepancy == D("-200")
    assert result.tolerance == D("50")
    assert result.is_blocked is True


# --- 5. end-to-end: a full day's two shifts + handoff ------------------------


def test_end_to_end_day_shift_then_handoff_then_night_shift(
    float_amount: Money, day_close: datetime, night_close: datetime
) -> None:
    """The full control flow for a two-shift day (PRD user stories 14–16).

    1. Alice closes the day shift: 5000 opening, 8000 rung up, 13050 counted
       -> +50 variance (over).
    2. At the 5pm handoff Bob recounts the drawer. He also gets 13050, so the
       recount matches and his night shift can start.
    3. Bob closes the night shift, opening from Alice's verified 13050.

    The day's two variance records (Alice +50, Bob ...) are the input to slice
    10's anomaly detector; this slice just produces and stores them, and gates
    the handoff.
    """
    # 1. Alice closes the day shift.
    alice_close = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13050"),
        rung_up_cash=Money("8000"),
    )
    assert alice_close.variance == D("50")

    # 2. Bob recounts at handoff; matches -> not blocked.
    recount = HandoffRecount(
        outgoing_shift_id=alice_close.shift_id,
        incoming_cashier_id="bob",
        recounted_at=day_close,
        recounted_cash=Money("13050"),
    )
    handoff = check_handoff(alice_close, recount)
    assert handoff.is_blocked is False

    # 3. Bob's night shift opens from the verified closing drawer.
    bob_close = close_shift(
        shift_id="2026-06-24-night",
        cashier_id="bob",
        closed_at=night_close,
        opening_cash=alice_close.closing_cash,
        closing_cash=Money("21500"),
        rung_up_cash=Money("8450"),
    )
    assert bob_close.variance == D("0")  # 21500 - (13050 + 8450) = 0


def test_end_to_end_handoff_mismatch_blocks_night_shift_start(
    float_amount: Money, day_close: datetime, night_close: datetime
) -> None:
    """The control-moment failure path: recount mismatch blocks the night shift.

    Alice reports 13050 closing. Bob recounts and gets 12500 — a 550 short.
    Under the strict default tolerance this blocks, so the night shift cannot
    start until reconciled. This is the segregation-of-duties substitute the
    whole control model rests on (PRD "Known control gap").
    """
    alice_close = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13050"),
        rung_up_cash=Money("8000"),
    )

    recount = HandoffRecount(
        outgoing_shift_id=alice_close.shift_id,
        incoming_cashier_id="bob",
        recounted_at=day_close,
        recounted_cash=Money("12500"),
    )
    handoff = check_handoff(alice_close, recount)

    assert handoff.is_blocked is True
    assert handoff.discrepancy == D("-550")
    # The caller gates shift start on ``is_blocked``; no night ShiftClose can
    # be opened from a blocked handoff in the wiring layer (slice 09 produces
    # the signal; enforcing it in a UI/CLI is downstream of the engine).


# --- 6. opening cash carried from prior close (no separate field needed) -----


def test_opening_cash_is_caller_supplied_from_prior_close(
    float_amount: Money, day_close: datetime, night_close: datetime
) -> None:
    """The opening cash for a shift is the prior shift's closing cash.

    Issue 09: "Opening cash (carried from prior shift's close)". There is no
    separate carry-forward primitive: the caller passes the prior close's
    ``closing_cash`` as this shift's ``opening_cash``. The engine is stateless
    over shifts; the wiring layer threads the value through. This keeps the
    engine a pure function of its inputs.
    """
    day = close_shift(
        shift_id="2026-06-24-day",
        cashier_id="alice",
        closed_at=day_close,
        opening_cash=float_amount,
        closing_cash=Money("13050"),
        rung_up_cash=Money("8000"),
    )
    night = close_shift(
        shift_id="2026-06-24-night",
        cashier_id="bob",
        closed_at=night_close,
        opening_cash=day.closing_cash,  # carried forward
        closing_cash=Money("13050"),  # nothing sold for cash, drawer unchanged
        rung_up_cash=Money("0"),
    )

    assert night.opening_cash == D("13050")
    assert night.variance == D("0")
