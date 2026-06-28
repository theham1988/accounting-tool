"""Admin checklists + partner task assignment (slice 12).

Structured checklists for the partner admin rituals (PRD user stories 28-31;
issue 12), so nothing gets skipped under shift pressure. Two checklists:

  - **Daily 9am review checklist** -- the five steps a partner works through
    when they open slice 11's view. The checklist names the steps; it does
    NOT embed slice 11's data (the steps are the partner-facing ritual
    regardless of what yesterday's numbers were). This keeps slice 12
    decoupled from slice 11's data inputs.
  - **Weekly admin checklist** -- the four weekly rituals (keg weigh, cafe
    stock count, receipt approval queue, fixed cost entry).

Each task is **assignable** to a specific partner (``TaskTemplate.assignee_id``),
and each partner carries its own **availability windows**
(``Assignee.windows``). A task scheduled to a partner surfaces that partner's
window for the occurrence's weekday, so the night-shift partner is never asked
to act at 9am (asleep) or 10pm (after close) -- PRD user story 30. The model
is role-agnostic, so onboarding a future manager is data, not a code change
(issue 12 AC).

This is the first slice that needs state across time. The shape is a pure
``build_checklists`` function plus a thin in-memory ``CompletionLog`` of
``CompletionEntry`` rows (mirrors slice 03's ``ApprovalBook`` pattern). No DB
yet; a later slice swaps storage without changing the engine's shape.

Skip carry-over (issue 12 AC: "skipped tasks surface in subsequent sessions"):
a skip recorded against (task, occurrence-date-N) is surfaced as SKIPPED on
every subsequent build of the same task, with ``skipped_for`` naming the
original occurrence date -- so a partner re-opening the checklist sees the
carried-over skip and can resolve it. A completion records only against its
own occurrence date (completing Monday's task does not mark Tuesday's done).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .types import (
    Assignee,
    AvailabilityWindow,
    ChecklistKind,
    ChecklistOccurrence,
    ChecklistSet,
    CompletionEntry,
    TaskOccurrence,
    TaskOutcome,
    TaskState,
    TaskTemplate,
)


#: Default daily 9am review checklist -- the five steps from issue 12, in order.
#: Each step defaults to the day-shift partner (``daniel``) as a sane out-of-
#: the-box owner; partners re-assign via ``task_templates`` to split the load.
#: Surfaced as a module constant so the canonical step wording lives in one
#: place and a later slice can localise it without re-architecting.
DEFAULT_DAILY_TEMPLATES: tuple[TaskTemplate, ...] = (
    TaskTemplate(
        task_id="open-daily-review",
        title="Open the daily review",
        kind=ChecklistKind.DAILY,
        assignee_id="daniel",
    ),
    TaskTemplate(
        task_id="review-segment-flags",
        title="Review segment flags",
        kind=ChecklistKind.DAILY,
        assignee_id="daniel",
    ),
    TaskTemplate(
        task_id="review-margin-anomalies",
        title="Review item-level margin anomalies",
        kind=ChecklistKind.DAILY,
        assignee_id="daniel",
    ),
    TaskTemplate(
        task_id="review-cash-void-flags",
        title="Review cash/void flags",
        kind=ChecklistKind.DAILY,
        assignee_id="daniel",
    ),
    TaskTemplate(
        task_id="mark-daily-done",
        title="Mark done",
        kind=ChecklistKind.DAILY,
        assignee_id="daniel",
    ),
)


#: Default weekly admin checklist -- the four weekly rituals from issue 12, in
#: order. Same default-owner reasoning as the daily set.
DEFAULT_WEEKLY_TEMPLATES: tuple[TaskTemplate, ...] = (
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
)


@dataclass
class CompletionLog:
    """Mutable store of recorded task outcomes per (task, occurrence).

    Mirrors slice 03's ``ApprovalBook`` pattern: a mutable container of
    append-only entries, with the build (a pure function) reading a snapshot
    of its current state. No DB yet; a later slice swaps storage without
    changing the engine's shape.

    Entries are keyed by ``(task_id, occurrence_date)``; recording a new
    outcome for the same key overwrites the prior one (a partner who skips
    then completes the same occurrence ends up recorded as completed).
    """

    _entries: dict[tuple[str, date], CompletionEntry] = field(default_factory=dict)

    def entries(self) -> list[CompletionEntry]:
        """Snapshot of the recorded entries (caller cannot mutate the store)."""
        return list(self._entries.values())

    def get(self, task_id: str, occurrence_date: date) -> CompletionEntry | None:
        """The recorded entry for one (task, occurrence), or None."""
        return self._entries.get((task_id, occurrence_date))

    def latest_skip_for_task(self, task_id: str) -> CompletionEntry | None:
        """The most recent SKIP entry for a task, unless a later entry resolves it.

        Used to carry a skip forward to subsequent sessions (issue 12 AC:
        "skipped tasks surface in subsequent sessions"). "Most recent" is by
        occurrence date. Returns None when the task has never been skipped.

        A skip carries forward only until the partner resolves it by recording
        any later entry (a completion OR a fresh skip on a later occurrence).
        Concretely: skip Monday -> complete Tuesday -> build Wednesday (no
        entry of its own) reports PENDING, not SKIPPED -- the Tuesday
        completion resolved the Monday skip. This matches the spec intent
        ("surface until resolved") and the docstring above.
        """
        task_entries = [
            e for e in self._entries.values() if e.task_id == task_id
        ]
        if not task_entries:
            return None
        latest = max(task_entries, key=lambda e: e.occurrence_date)
        if latest.outcome is not TaskOutcome.SKIPPED:
            return None
        return latest

    def _record(self, entry: CompletionEntry) -> None:
        self._entries[(entry.task_id, entry.occurrence_date)] = entry


def complete_task(
    *,
    log: CompletionLog,
    task_id: str,
    occurrence_date: date,
    assignee_id: str,
) -> CompletionLog:
    """Record a COMPLETED outcome for one (task, occurrence).

    Returns the same log (mutated in place) so callers can chain. Recording
    against an occurrence that already had an outcome overwrites it -- the
    latest outcome is the truth.
    """
    log._record(
        CompletionEntry(
            task_id=task_id,
            occurrence_date=occurrence_date,
            assignee_id=assignee_id,
            outcome=TaskOutcome.COMPLETED,
            reason=None,
        )
    )
    return log


def skip_task(
    *,
    log: CompletionLog,
    task_id: str,
    occurrence_date: date,
    assignee_id: str,
    reason: str,
) -> CompletionLog:
    """Record a SKIPPED outcome for one (task, occurrence) with a reason.

    The reason is surfaced verbatim in subsequent sessions (issue 12 AC:
    "skipped tasks surface in subsequent sessions") so the carried-over skip
    explains itself rather than appearing as a bare flag.
    """
    log._record(
        CompletionEntry(
            task_id=task_id,
            occurrence_date=occurrence_date,
            assignee_id=assignee_id,
            outcome=TaskOutcome.SKIPPED,
            reason=reason,
        )
    )
    return log


def build_checklists(
    *,
    assignees: list[Assignee],
    anchor: date,
    task_templates: list[TaskTemplate] | None = None,
    completion_log: CompletionLog | None = None,
) -> ChecklistSet:
    """Materialise the daily and weekly checklists for one occurrence build.

    Pure function over its inputs. For each task template it produces one
    ``TaskOccurrence`` carrying:

      - the assignee who owns it
      - that assignee's availability window for the occurrence's weekday
        (None when the assignee has no window for that day)
      - the derived state from the completion log (PENDING / DONE / SKIPPED)

    Skip carry-over: if a task has any prior SKIP on record and no entry for
    this exact occurrence, its occurrence surfaces as SKIPPED with
    ``skipped_for`` naming the original skip's occurrence date. This is what
    makes a skipped task visible in the next session without the partner
    having to remember it. Recording a fresh outcome for the current
    occurrence resolves the carry (the exact-match entry wins).

    ``task_templates`` defaults to the canonical issue-12 sets (daily 5 steps,
    weekly 4 rituals) when not supplied. ``completion_log`` defaults to an
    empty log (everything pending) when not supplied.

    ``anchor`` is the occurrence date for both checklists: the daily checklist
    is for that day, and the weekly checklist is for the admin week starting
    on that date (issue 12 does not pin a week-start weekday; the caller
    chooses by passing the appropriate anchor).
    """
    templates = task_templates or list(DEFAULT_DAILY_TEMPLATES) + list(
        DEFAULT_WEEKLY_TEMPLATES
    )
    log = completion_log or CompletionLog()
    by_assignee = {a.assignee_id: a for a in assignees}

    daily_tasks: list[TaskOccurrence] = []
    weekly_tasks: list[TaskOccurrence] = []
    for tpl in templates:
        occ = _materialise(
            tpl=tpl, anchor=anchor, by_assignee=by_assignee, log=log
        )
        if tpl.kind is ChecklistKind.DAILY:
            daily_tasks.append(occ)
        else:
            weekly_tasks.append(occ)

    return ChecklistSet(
        daily=ChecklistOccurrence(
            kind=ChecklistKind.DAILY,
            occurrence_date=anchor,
            tasks=tuple(daily_tasks),
        ),
        weekly=ChecklistOccurrence(
            kind=ChecklistKind.WEEKLY,
            occurrence_date=anchor,
            tasks=tuple(weekly_tasks),
        ),
    )


# --- helpers ----------------------------------------------------------------


def _materialise(
    *,
    tpl: TaskTemplate,
    anchor: date,
    by_assignee: dict[str, Assignee],
    log: CompletionLog,
) -> TaskOccurrence:
    """Build one task occurrence from its template + the current log state.

    Window selection picks the assignee's window whose ``weekday`` matches the
    anchor's ISO weekday; None when the assignee has no window for that day.
    State derivation prefers an exact (task, occurrence) entry, then falls
    back to the latest carried-over skip for the task.
    """
    assignee = by_assignee.get(tpl.assignee_id)
    window = _window_for_weekday(assignee, anchor) if assignee is not None else None

    entry = log.get(tpl.task_id, anchor)
    if entry is not None:
        return _occurrence_from_entry(
            tpl=tpl, anchor=anchor, assignee_id=tpl.assignee_id,
            window=window, entry=entry,
        )

    carried = log.latest_skip_for_task(tpl.task_id)
    if carried is not None:
        return TaskOccurrence(
            template=tpl,
            occurrence_date=anchor,
            assignee_id=tpl.assignee_id,
            window=window,
            state=TaskState.SKIPPED,
            outcome=TaskOutcome.SKIPPED,
            skipped_for=carried.occurrence_date,
            skipped_reason=carried.reason,
        )

    return TaskOccurrence(
        template=tpl,
        occurrence_date=anchor,
        assignee_id=tpl.assignee_id,
        window=window,
        state=TaskState.PENDING,
        outcome=None,
        skipped_for=None,
        skipped_reason=None,
    )


def _occurrence_from_entry(
    *,
    tpl: TaskTemplate,
    anchor: date,
    assignee_id: str,
    window: AvailabilityWindow | None,
    entry: CompletionEntry,
) -> TaskOccurrence:
    """Translate a log entry into a task occurrence's terminal state."""
    if entry.outcome is TaskOutcome.COMPLETED:
        return TaskOccurrence(
            template=tpl,
            occurrence_date=anchor,
            assignee_id=assignee_id,
            window=window,
            state=TaskState.DONE,
            outcome=TaskOutcome.COMPLETED,
            skipped_for=None,
            skipped_reason=None,
        )
    return TaskOccurrence(
        template=tpl,
        occurrence_date=anchor,
        assignee_id=assignee_id,
        window=window,
        state=TaskState.SKIPPED,
        outcome=TaskOutcome.SKIPPED,
        skipped_for=entry.occurrence_date,
        skipped_reason=entry.reason,
    )


def _window_for_weekday(
    assignee: Assignee, anchor: date
) -> AvailabilityWindow | None:
    """The assignee's availability window for the anchor's weekday.

    Weekday convention: Monday=0 ... Sunday=6 (Python's ``date.weekday()``).
    The first window whose ``weekday`` matches wins; None when the assignee
    has no window for that day (e.g. a partner who never works Sundays).
    """
    anchor_weekday = anchor.weekday()
    for w in assignee.windows:
        if w.weekday == anchor_weekday:
            return w
    return None


__all__ = [
    "DEFAULT_DAILY_TEMPLATES",
    "DEFAULT_WEEKLY_TEMPLATES",
    "CompletionLog",
    "build_checklists",
    "complete_task",
    "skip_task",
]
