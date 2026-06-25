# 09 — Cash drawer reconciliation with 5pm handoff recount

## What to build

Each shift close captures:
- Opening cash (carried from prior shift's close)
- Closing cash (counted by the closing cashier)
- Loyverse rung-up cash for the shift
- Variance = closing cash − (opening cash + rung-up cash)

The 5pm handoff is the only real control moment (since the closing cashier counts their own drawer — the partner structure is the mitigation). The incoming partner must recount the drawer at handoff. If the recounted closing cash does not match the outgoing partner's reported closing cash (within tolerance), shift start is blocked until reconciled.

Drawer variance history is recorded per shift, per cashier — this is the input to anomaly detection in slice 10.

## Acceptance criteria

- [ ] Shift close form captures opening, closing, rung-up cash, and computes variance
- [ ] Variance is stored per shift with cashier identity and timestamp
- [ ] 5pm handoff requires incoming partner to recount closing drawer
- [ ] Mismatch between outgoing reported close and incoming recount (outside tolerance) blocks shift start
- [ ] End-to-end test feeds synthetic shift closes + handoff recounts; asserts variance and block-on-mismatch behaviour

## Blocked by

- 01 — Pipeline skeleton with seeded single-item margin
