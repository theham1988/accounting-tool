"""Cash drawer reconciliation with 5pm handoff recount (slice 09).

Pure functions over inputs (no I/O, no mutation). Each shift close captures
the four numbers from the PRD "Cash control" section::

    opening cash   carried from the prior shift's close (caller-supplied)
    closing cash   counted by the closing cashier at shift end
    rung-up cash   Loyverse cash sales rung up over the shift
    variance       closing − (opening + rung_up)

The 5pm handoff is the only real control moment in a two-partner, no-manager
structure (PRD "Known control gap"): the closing cashier counts their own
drawer, so the incoming partner's recount is the segregation-of-duties
substitute. A recount that does not match the outgoing close's reported
``closing_cash`` — within tolerance — blocks shift start.

The agreed default tolerance is **0 THB** (``DEFAULT_HANDOFF_TOLERANCE``):
the recount is THE control moment, so any discrepancy surfaces. It is still
a parameter so a future manager can relax it without re-architecting the
engine. ``abs(discrepancy) > tolerance`` is the block condition; the
discrepancy itself is signed and carried on the result so the review can tell
over from short and so slice 10's anomaly detector can consume it.

Drawer variance history is recorded per shift, per cashier. This slice
produces and stores those records; the anomaly detector that consumes them
(void-rate, drawer-short-rate, clustering) is slice 10 and is explicitly out
of scope here.

Statelessness over shifts: the opening cash for a shift is the prior shift's
closing cash, but there is no carry-forward primitive in the engine. The
caller threads the value through (``opening_cash=prior.closing_cash``); the
engine is a pure function of its inputs. This matches the keg/cafe engines'
shape and keeps the test seam single-path.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .types import HandoffRecount, HandoffResult, Money, ShiftClose


#: Default 5pm-handoff recount tolerance, in THB. The recount is THE control
#: moment in a two-partner / no-manager structure (PRD "Known control gap"):
#: the closing cashier counts their own drawer, and the incoming partner's
#: recount is the only segregation-of-duties substitute. So the default is
#: strict — any discrepancy surfaces and blocks. It is still a parameter on
#: ``check_handoff`` so a future manager can relax it without re-architecting.
DEFAULT_HANDOFF_TOLERANCE: Money = Money("0")


def drawer_variance(
    *, opening_cash: Money, closing_cash: Money, rung_up_cash: Money
) -> Money:
    """Cash drawer variance for one shift.

    ``closing_cash − (opening_cash + rung_up_cash)``. Positive = over (more in
    the drawer than the system says should be), negative = short. The sign is
    surfaced as-is because both directions are operationally meaningful:
    overages can hide mis-rings or tip-jar dumps; shorts can hide theft or
    error. Slice 10's anomaly detector consumes the raw signed number.
    """
    return Money(closing_cash - (opening_cash + rung_up_cash))


def close_shift(
    *,
    shift_id: str,
    cashier_id: str,
    closed_at: datetime,
    opening_cash: Money,
    closing_cash: Money,
    rung_up_cash: Money,
) -> ShiftClose:
    """Capture one shift's closing cash record and compute its variance.

    This is the shift-close form (PRD user story 14). The returned record
    carries the cashier identity and close timestamp so a per-cashier variance
    history (slice 10's input) can be built from stored records.

    ``opening_cash`` is caller-supplied: the wiring layer threads the prior
    shift's ``closing_cash`` into this shift's opening. The engine is stateless
    over shifts by design, keeping the test seam single-path.
    """
    variance = drawer_variance(
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        rung_up_cash=rung_up_cash,
    )
    return ShiftClose(
        shift_id=shift_id,
        cashier_id=cashier_id,
        closed_at=closed_at,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        rung_up_cash=rung_up_cash,
        variance=variance,
    )


def check_handoff(
    outgoing: ShiftClose,
    recount: HandoffRecount,
    *,
    tolerance: Money = DEFAULT_HANDOFF_TOLERANCE,
) -> HandoffResult:
    """Verify the 5pm handoff recount against the outgoing shift's close.

    The incoming partner recounts the outgoing partner's drawer; if the
    recounted cash does not match the outgoing close's reported
    ``closing_cash`` within ``tolerance``, shift start is blocked.

    The block condition is ``abs(discrepancy) > tolerance`` where
    ``discrepancy = recounted_cash − reported_closing_cash`` (signed). The
    discrepancy is carried on the result even when the handoff passes, so
    within-tolerance miscounts are still visible to slice 10's anomaly
    detector and to the review surface.

    ``tolerance`` defaults to ``DEFAULT_HANDOFF_TOLERANCE`` (0 THB): the
    recount is THE control moment, so any discrepancy surfaces. It is a
    keyword argument so a future manager can relax it per-handoff without
    changing the call shape.
    """
    discrepancy = Money(recount.recounted_cash - outgoing.closing_cash)
    is_blocked = abs(discrepancy) > tolerance
    return HandoffResult(
        outgoing_shift_id=outgoing.shift_id,
        reported_closing_cash=outgoing.closing_cash,
        recounted_cash=recount.recounted_cash,
        discrepancy=discrepancy,
        tolerance=tolerance,
        is_blocked=is_blocked,
    )
