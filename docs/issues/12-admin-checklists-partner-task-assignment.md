# 12 — Admin checklists + partner task assignment

## What to build

Structured checklists for the partner admin rituals, so nothing gets skipped under shift pressure. Two checklists:

**Daily 9am review checklist** (wraps slice 11's view):
- Open the daily review
- Review segment flags
- Review item-level margin anomalies
- Review cash/void flags
- Mark done

**Weekly admin checklist**:
- Keg weigh (per brand)
- Cafe stock count (per cadence)
- Receipt approval queue cleared
- Fixed cost entry (if any new this week)

Tasks must be assignable to either partner. The night-shift partner cannot reasonably do admin at 10pm (after close) or 9am (asleep). The system must allow tasks to be scheduled into each partner's available windows — e.g. night partner does their share during a quiet pre-rush window or on a designated admin slot, not at literal shift end.

Onboarding a future manager into these checklists should not require re-architecting the tool.

## Acceptance criteria

- [ ] Daily 9am review checklist exists and wraps the daily review view
- [ ] Weekly admin checklist exists with the four weekly rituals
- [ ] Each task can be assigned to a specific partner
- [ ] Tasks can be scheduled to a partner-specific time window (night partner accommodated)
- [ ] Completion state tracked per task per occurrence
- [ ] Skipped tasks surface in subsequent sessions
- [ ] A new "manager" role can be added and assigned tasks without code changes
- [ ] End-to-end test feeds synthetic assignments; asserts scheduling and completion behaviour

## Blocked by

- 11 — Daily 9am review view
