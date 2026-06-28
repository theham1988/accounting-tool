"""End-to-end admin checklists + partner task assignment (slice 12).

Partners spend 10-15 hours/week of *structured* labour on the admin rituals the
PRD calls out (PRD user story 28-31; issue 12): the daily 9am review, the
weekly keg weigh, the cafe stock count, clearing the receipt approval queue,
and entering any new fixed costs. The point of this slice is that those rituals
do not get skipped under shift pressure.

Two checklists (issue 12):

  - **Daily 9am review checklist** -- the five steps a partner works through
    when they open slice 11's view. The checklist names the steps; it does
    NOT embed slice 11's data (the steps are the partner-facing ritual
    regardless of what yesterday's numbers were).
  - **Weekly admin checklist** -- the four weekly rituals (keg weigh, cafe
    stock, receipt approval queue, fixed cost entry).

Each task is **assignable** to a specific partner, and each partner carries
their own **availability windows** so the night-shift partner is never asked to
do a task at 9am (asleep) or 10pm (after close, exhausted). The model is
generic on the assignee so onboarding a future manager is data, not code.

These tests read as worked examples: synthetic assignments and completions go
in; the checklist render carries the right tasks, assignees, windows, and
overdue surfacing out.

Scope decisions (confirmed with the partner before code):

  - **Daily checklist wrapping slice 11**: the daily checklist is a pure task
    list. It names the five steps a partner does during the 9am review but
    does NOT carry a slice-11 ``DailyReview`` object. That keeps this slice
    decoupled from slice 11's data inputs (sales, recipes, cost, closes) --
    a partner who has not yet run the review still sees the checklist.
  - **Completion state**: this is the first slice that needs state across
    time (per-occurrence completion + overdue surfacing). The pattern is a
    pure ``build_checklists`` function plus a thin in-memory ``CompletionLog``
    (mutable, modelled after slice 03's ``ApprovalBook``). No DB yet; a
    later slice swaps storage without changing the engine's shape.
  - **Scheduling windows**: an ``Assignee`` carries its own availability
    windows (e.g. night partner 14:00-17:00). A task scheduled to an assignee
    surfaces that assignee's window, so the partner knows *when* in their day
    to do it without the system trying to pick an absolute time.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from tangerine.checklists import (
    CompletionLog,
    build_checklists,
    complete_task,
    skip_task,
)
from tangerine.types import (
    Assignee,
    AvailabilityWindow,
    ChecklistKind,
    TaskOutcome,
    TaskState,
    TaskTemplate,
)


# --- shared fixtures --------------------------------------------------------


@pytest.fixture
def day_partner() -> Assignee:
    """The day-shift partner.

    Available in the morning (the cafe open through the bar close at 5pm),
    so they are the natural owner of the 9am review and the weekly rituals
    that need light (keg weigh, cafe stock count).
    """
    return Assignee(
        assignee_id="daniel",
        name="Daniel (day)",
        windows=(
            AvailabilityWindow(
                weekday=0, start=time(8, 0), end=time(17, 0),
            ),
        ),
    )


@pytest.fixture
def night_partner() -> Assignee:
    """The night-shift partner.

    Available in a quiet pre-rush window (early afternoon) and on a
    designated admin slot. NOT available at 9am (asleep) or 10pm (after
    close) -- the whole point of issue 12 / PRD user story 30 is that the
    night partner's share is genuinely doable.
    """
    return Assignee(
        assignee_id="noi",
        name="Noi (night)",
        windows=(
            AvailabilityWindow(
                weekday=0, start=time(14, 0), end=time(17, 0),
            ),
        ),
    )


@pytest.fixture
def daily_review_date() -> date:
    """A Monday in 2026 -- weekday 0, so day_partner / night_partner windows
    both apply."""
    return date(2026, 6, 29)  # Monday


@pytest.fixture
def weekly_anchor() -> date:
    """The Monday starting the admin week."""
    return date(2026, 6, 29)  # Monday


# --- AC 1: daily 9am review checklist exists --------------------------------


def test_daily_checklist_exists_with_the_five_review_steps(
    day_partner: Assignee, daily_review_date: date,
) -> None:
    """AC: "Daily 9am review checklist exists and wraps the daily review view".

    The daily checklist names the five steps a partner works through during
    the 9am review (issue 12's bullet list). It does not embed slice 11's
    numbers -- it is the ritual, decoupled from yesterday's data.

    Worked example: a Monday review owned by the day partner. The five steps
    appear in their issue-12 order, the checklist is the daily kind, and the
    occurrence carries the right date.
    """
    set_ = build_checklists(assignees=[day_partner], anchor=daily_review_date)

    assert set_.daily.kind is ChecklistKind.DAILY
    assert set_.daily.occurrence_date == daily_review_date
    # The five steps from issue 12, in order.
    titles = [t.template.title for t in set_.daily.tasks]
    assert titles == [
        "Open the daily review",
        "Review segment flags",
        "Review item-level margin anomalies",
        "Review cash/void flags",
        "Mark done",
    ]


# --- AC 2: weekly admin checklist exists with the four rituals -------------


def test_weekly_checklist_exists_with_the_four_weekly_rituals(
    day_partner: Assignee, night_partner: Assignee, weekly_anchor: date,
) -> None:
    """AC: "Weekly admin checklist exists with the four weekly rituals".

    The weekly checklist carries the four rituals from issue 12: keg weigh
    (per brand), cafe stock count (per cadence), receipt approval queue
    cleared, fixed cost entry (if any new this week).
    """
    set_ = build_checklists(
        assignees=[day_partner, night_partner], anchor=weekly_anchor
    )

    assert set_.weekly.kind is ChecklistKind.WEEKLY
    assert set_.weekly.occurrence_date == weekly_anchor
    titles = [t.template.title for t in set_.weekly.tasks]
    assert titles == [
        "Keg weigh (per brand)",
        "Cafe stock count (per cadence)",
        "Receipt approval queue cleared",
        "Fixed cost entry (if any new this week)",
    ]


# --- AC 3: each task can be assigned to a specific partner -----------------


def test_tasks_can_be_assigned_to_specific_partners(
    day_partner: Assignee, night_partner: Assignee, weekly_anchor: date,
) -> None:
    """AC: "Each task can be assigned to a specific partner".

    A task template carries the assignee id of the partner who owns it. The
    built occurrence surfaces that assignment so the review can show "this
    one is yours".

    Worked example. The four weekly rituals are split between partners: keg
    weigh + cafe stock (need light, partner on-site in the morning) to the
    day partner; receipt approval queue + fixed cost entry (admin work,
    quiet-window friendly) to the night partner. Each occurrence's
    ``assignee_id`` matches the template.
    """
    templates = [
        TaskTemplate(
            task_id="keg-weigh",
            title="Keg weigh (per brand)",
            kind=ChecklistKind.WEEKLY,
            assignee_id="daniel",
        ),
        TaskTemplate(
            task_id="cafe-stock",
            title="Cafe stock count (per cadence)",
            kind=ChecklistKind.WEEKLY,
            assignee_id="daniel",
        ),
        TaskTemplate(
            task_id="receipt-queue",
            title="Receipt approval queue cleared",
            kind=ChecklistKind.WEEKLY,
            assignee_id="noi",
        ),
        TaskTemplate(
            task_id="fixed-cost-entry",
            title="Fixed cost entry (if any new this week)",
            kind=ChecklistKind.WEEKLY,
            assignee_id="noi",
        ),
    ]
    set_ = build_checklists(
        assignees=[day_partner, night_partner],
        anchor=weekly_anchor,
        task_templates=templates,
    )

    by_id = {t.template.task_id: t for t in set_.weekly.tasks}
    assert by_id["keg-weigh"].assignee_id == "daniel"
    assert by_id["cafe-stock"].assignee_id == "daniel"
    assert by_id["receipt-queue"].assignee_id == "noi"
    assert by_id["fixed-cost-entry"].assignee_id == "noi"


# --- AC 4: tasks can be scheduled to partner-specific time windows ---------


def test_task_surfaces_assignees_availability_window(
    day_partner: Assignee, night_partner: Assignee, weekly_anchor: date,
) -> None:
    """AC: "Tasks can be scheduled to a partner-specific time window".

    Each task occurrence carries the assignee's availability window, so the
    partner knows *when* in their day to do it. The night-shift partner's
    window is the pre-rush quiet slot (14:00-17:00), never 9am or 10pm.

    Worked example. A weekly task assigned to the night partner surfaces their
    14:00-17:00 window; the same task assigned to the day partner surfaces
    their 08:00-17:00 window. The window travels WITH the assignment.
    """
    templates = [
        TaskTemplate(
            task_id="task-night",
            title="Receipt approval queue cleared",
            kind=ChecklistKind.WEEKLY,
            assignee_id="noi",
        ),
        TaskTemplate(
            task_id="task-day",
            title="Keg weigh (per brand)",
            kind=ChecklistKind.WEEKLY,
            assignee_id="daniel",
        ),
    ]
    set_ = build_checklists(
        assignees=[day_partner, night_partner],
        anchor=weekly_anchor,
        task_templates=templates,
    )

    by_id = {t.template.task_id: t for t in set_.weekly.tasks}
    # Night partner's task surfaces THEIR window.
    assert by_id["task-night"].window == night_partner.windows[0]
    assert by_id["task-night"].window.start == time(14, 0)
    assert by_id["task-night"].window.end == time(17, 0)
    # Day partner's task surfaces a DIFFERENT window.
    assert by_id["task-day"].window == day_partner.windows[0]
    assert by_id["task-day"].window.start == time(8, 0)


def test_night_partner_window_does_not_include_9am_or_10pm(
    night_partner: Assignee,
) -> None:
    """Issue 12 core constraint: the night partner is never asked to act at
    9am (asleep) or 10pm (after close).

    Their availability windows must not include 09:00 or 22:00. This is a
    focused regression check on the window model itself, separate from the
    "task surfaces window" check above.
    """
    for window in night_partner.windows:
        assert not window.contains(time(9, 0)), (
            f"night partner is not available at 9am but window {window} includes it"
        )
        assert not window.contains(time(22, 0)), (
            f"night partner is not available at 10pm but window {window} includes it"
        )
        # And the pre-rush window they ARE available in.
        assert window.contains(time(15, 0))


# --- AC 5: completion state tracked per task per occurrence -----------------


def test_completion_state_tracked_per_task_per_occurrence(
    day_partner: Assignee, daily_review_date: date,
) -> None:
    """AC: "Completion state tracked per task per occurrence".

    A ``CompletionLog`` records the outcome of each (task, occurrence) pair.
    Completing a task on Monday does NOT mark it done for Tuesday -- each
    occurrence carries its own state.

    Worked example. The daily checklist's first task is completed on Monday
    and skipped on Tuesday. The log records both, with the right outcome and
    the right state per occurrence.
    """
    log = CompletionLog()
    monday = daily_review_date
    tuesday = monday + timedelta(days=1)

    # Monday: complete the first daily task.
    log = complete_task(
        log=log, task_id="open-daily-review", occurrence_date=monday,
        assignee_id="daniel",
    )
    # Tuesday: skip the same task.
    log = skip_task(
        log=log, task_id="open-daily-review", occurrence_date=tuesday,
        assignee_id="daniel", reason="out of office",
    )

    set_mon = build_checklists(
        assignees=[day_partner], anchor=monday, completion_log=log,
    )
    set_tue = build_checklists(
        assignees=[day_partner], anchor=tuesday, completion_log=log,
    )

    mon_first = set_mon.daily.tasks[0]
    tue_first = set_tue.daily.tasks[0]
    assert mon_first.state is TaskState.DONE
    assert mon_first.outcome is TaskOutcome.COMPLETED
    assert tue_first.state is TaskState.SKIPPED
    assert tue_first.outcome is TaskOutcome.SKIPPED
    assert tue_first.skipped_reason == "out of office"


def test_a_task_with_no_log_entry_is_pending(
    day_partner: Assignee, daily_review_date: date,
) -> None:
    """A task with no log entry for that occurrence reports PENDING."""
    log = CompletionLog()
    set_ = build_checklists(
        assignees=[day_partner], anchor=daily_review_date, completion_log=log,
    )

    for task in set_.daily.tasks:
        assert task.state is TaskState.PENDING
        assert task.outcome is None


# --- AC 6: skipped tasks surface in subsequent sessions --------------------


def test_skipped_tasks_surface_in_subsequent_sessions(
    day_partner: Assignee, daily_review_date: date,
) -> None:
    """AC: "Skipped tasks surface in subsequent sessions".

    A task skipped on Monday stays visible as SKIPPED on Tuesday's checklist
    build. The build is the surface; the partner sees the carried-over skip
    in the next session and can either complete it or accept the skip.

    Worked example. The "review segment flags" task is skipped on Monday. On
    Tuesday's build it appears as SKIPPED (with the Monday occurrence date
    named as ``skipped_for`` so the partner knows *which* day was skipped),
    NOT as PENDING.
    """
    monday = daily_review_date
    tuesday = monday + timedelta(days=1)

    log = CompletionLog()
    log = skip_task(
        log=log, task_id="review-segment-flags", occurrence_date=monday,
        assignee_id="daniel", reason="ran out of time",
    )

    # Tuesday's build carries Monday's skip forward.
    set_tue = build_checklists(
        assignees=[day_partner], anchor=tuesday, completion_log=log,
    )

    seg_task = next(
        t for t in set_tue.daily.tasks
        if t.template.task_id == "review-segment-flags"
    )
    # The skip is carried over, not silently dropped.
    assert seg_task.state is TaskState.SKIPPED
    assert seg_task.skipped_for == monday
    assert seg_task.skipped_reason == "ran out of time"


def test_completed_task_does_not_surface_as_skipped_next_day(
    day_partner: Assignee, daily_review_date: date,
) -> None:
    """Sanity: a DONE task does not get carried forward as a skip.

    Only skips carry over. A done task on Monday stays done for Monday and
    does not appear in Tuesday's build as anything other than PENDING (it is
    a new occurrence with no entry yet).
    """
    monday = daily_review_date
    tuesday = monday + timedelta(days=1)

    log = CompletionLog()
    log = complete_task(
        log=log, task_id="review-segment-flags", occurrence_date=monday,
        assignee_id="daniel",
    )

    set_mon = build_checklists(
        assignees=[day_partner], anchor=monday, completion_log=log,
    )
    set_tue = build_checklists(
        assignees=[day_partner], anchor=tuesday, completion_log=log,
    )

    mon_task = next(
        t for t in set_mon.daily.tasks
        if t.template.task_id == "review-segment-flags"
    )
    tue_task = next(
        t for t in set_tue.daily.tasks
        if t.template.task_id == "review-segment-flags"
    )
    assert mon_task.state is TaskState.DONE
    # Tuesday is a new occurrence -> pending (no carry-over).
    assert tue_task.state is TaskState.PENDING
    assert tue_task.skipped_for is None


def test_completion_after_a_skip_resolves_the_carry(
    day_partner: Assignee, daily_review_date: date,
) -> None:
    """AC 6 refinement: a completion after a skip stops the carry.

    A skip surfaces in subsequent sessions (issue 12 AC 6) UNTIL the partner
    resolves it by completing the task on a fresh occurrence. After that
    completion, a later session with no entry of its own reports PENDING --
    the old skip is no longer carried forward.

    Worked example. Skip on Monday, complete on Tuesday, build Wednesday.
    Wednesday's task must be PENDING (skip resolved Tuesday), not SKIPPED
    (the Monday skip carried forward).
    """
    monday = daily_review_date
    tuesday = monday + timedelta(days=1)
    wednesday = monday + timedelta(days=2)

    log = CompletionLog()
    log = skip_task(
        log=log, task_id="review-segment-flags", occurrence_date=monday,
        assignee_id="daniel", reason="ran out of time",
    )
    log = complete_task(
        log=log, task_id="review-segment-flags", occurrence_date=tuesday,
        assignee_id="daniel",
    )

    set_wed = build_checklists(
        assignees=[day_partner], anchor=wednesday, completion_log=log,
    )

    wed_task = next(
        t for t in set_wed.daily.tasks
        if t.template.task_id == "review-segment-flags"
    )
    assert wed_task.state is TaskState.PENDING
    assert wed_task.skipped_for is None
    assert wed_task.skipped_reason is None


# --- AC 7: a new "manager" role can be added without code changes ----------


def test_manager_can_be_added_and_assigned_without_code_changes(
    day_partner: Assignee, night_partner: Assignee, daily_review_date: date,
) -> None:
    """AC: "A new 'manager' role can be added and assigned tasks without code
    changes".

    The ``Assignee`` model is role-agnostic -- a partner is just an assignee
    with availability windows. Onboarding a future manager means constructing
    a new ``Assignee`` (data), not editing engine code. Tasks assigned to the
    manager's id surface that manager's window on the built checklist.

    Worked example. A manager "kanya" with morning availability is onboarded
    and assigned the daily review. The build surfaces their window with no
    code change to the engine.
    """
    manager = Assignee(
        assignee_id="kanya",
        name="Kanya (manager)",
        windows=(
            AvailabilityWindow(
                weekday=0, start=time(9, 0), end=time(12, 0),
            ),
        ),
    )
    templates = [
        TaskTemplate(
            task_id="open-daily-review",
            title="Open the daily review",
            kind=ChecklistKind.DAILY,
            assignee_id="kanya",
        ),
    ]
    set_ = build_checklists(
        assignees=[day_partner, night_partner, manager],
        anchor=daily_review_date,
        task_templates=templates,
    )

    first = set_.daily.tasks[0]
    assert first.assignee_id == "kanya"
    assert first.window == manager.windows[0]
    assert first.window.start == time(9, 0)


# --- AC 8: end-to-end feeds synthetic assignments; asserts scheduling
#         and completion behaviour -------------------------------------------


def test_end_to_end_synthetic_assignments_split_between_partners(
    day_partner: Assignee, night_partner: Assignee, weekly_anchor: date,
) -> None:
    """AC: "End-to-end test feeds synthetic assignments; asserts scheduling
    and completion behaviour".

    Full slice-12 seam. A synthetic admin week is fed in:

      - Four weekly rituals split between the day partner (light-dependent
        rituals) and the night partner (quiet-window admin rituals).
      - The night partner's tasks surface a 14:00-17:00 window, never 9am.
      - One task is completed, one skipped. The next session's build surfaces
        the carried-over skip.

    Asserts scheduling (right assignee, right window) and completion behaviour
    (per-occurrence state + skip carry-over) in one stroke.
    """
    log = CompletionLog()
    anchor = weekly_anchor

    templates = [
        TaskTemplate(
            task_id="keg-weigh", title="Keg weigh (per brand)",
            kind=ChecklistKind.WEEKLY, assignee_id="daniel",
        ),
        TaskTemplate(
            task_id="cafe-stock", title="Cafe stock count (per cadence)",
            kind=ChecklistKind.WEEKLY, assignee_id="daniel",
        ),
        TaskTemplate(
            task_id="receipt-queue",
            title="Receipt approval queue cleared",
            kind=ChecklistKind.WEEKLY, assignee_id="noi",
        ),
        TaskTemplate(
            task_id="fixed-cost-entry",
            title="Fixed cost entry (if any new this week)",
            kind=ChecklistKind.WEEKLY, assignee_id="noi",
        ),
    ]

    # Half-done week: keg weigh completed, receipt queue skipped.
    log = complete_task(
        log=log, task_id="keg-weigh", occurrence_date=anchor,
        assignee_id="daniel",
    )
    log = skip_task(
        log=log, task_id="receipt-queue", occurrence_date=anchor,
        assignee_id="noi", reason="no receipts this week",
    )

    set_ = build_checklists(
        assignees=[day_partner, night_partner],
        anchor=anchor,
        task_templates=templates,
        completion_log=log,
    )

    by_id = {t.template.task_id: t for t in set_.weekly.tasks}

    # Scheduling: the right tasks went to the right partners with the right
    # windows. The night partner's tasks carry THEIR pre-rush window, never
    # 9am.
    assert by_id["keg-weigh"].assignee_id == "daniel"
    assert by_id["keg-weigh"].window.start == time(8, 0)
    assert by_id["receipt-queue"].assignee_id == "noi"
    assert by_id["receipt-queue"].window.start == time(14, 0)
    assert by_id["receipt-queue"].window.end == time(17, 0)

    # Completion behaviour: the completed task is DONE for this occurrence;
    # the skipped task is SKIPPED with its reason carried through.
    assert by_id["keg-weigh"].state is TaskState.DONE
    assert by_id["keg-weigh"].outcome is TaskOutcome.COMPLETED
    assert by_id["receipt-queue"].state is TaskState.SKIPPED
    assert by_id["receipt-queue"].outcome is TaskOutcome.SKIPPED
    assert by_id["receipt-queue"].skipped_reason == "no receipts this week"

    # The other two weekly rituals are still pending.
    assert by_id["cafe-stock"].state is TaskState.PENDING
    assert by_id["fixed-cost-entry"].state is TaskState.PENDING

    # Skip carry-over (issue 12 AC 6). Two assertions:
    #   1. Re-opening the SAME week's checklist surfaces the skip (the partner
    #      closed the tool and came back; the skip is still there).
    #   2. A LATER week with no entry of its own still surfaces the carried
    #      skip -- until the partner resolves it with a fresh outcome.
    next_week = anchor + timedelta(days=7)

    set_reopened = build_checklists(
        assignees=[day_partner, night_partner],
        anchor=anchor,
        task_templates=templates,
        completion_log=log,
    )
    reopened_by_id = {t.template.task_id: t for t in set_reopened.weekly.tasks}
    assert reopened_by_id["receipt-queue"].state is TaskState.SKIPPED
    assert reopened_by_id["receipt-queue"].skipped_reason == "no receipts this week"
    assert reopened_by_id["receipt-queue"].skipped_for == anchor

    set_next_week = build_checklists(
        assignees=[day_partner, night_partner],
        anchor=next_week,
        task_templates=templates,
        completion_log=log,
    )
    next_week_by_id = {t.template.task_id: t for t in set_next_week.weekly.tasks}
    # The skip carries forward to the next week (still unresolved).
    assert next_week_by_id["receipt-queue"].state is TaskState.SKIPPED
    assert next_week_by_id["receipt-queue"].skipped_for == anchor
    assert next_week_by_id["receipt-queue"].skipped_reason == "no receipts this week"
