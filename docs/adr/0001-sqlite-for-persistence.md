# ADR-0001: SQLite as the persistence layer

Date: 2026-06-28

## Status

Accepted.

## Context

Every store in the accounting engine today is in-memory: `InMemoryLoyverseStore`,
`ApprovalBook`, `CompletionLog`, `CostBook`. They evaporate when the process
exits. Any UI build requires a persistence layer — yesterday's 9am review cannot
disappear on refresh.

The PRD lists "storage choice" as an unresolved open item, recommending a
relational DB. The decision is between:

- **Postgres** — relational DB server; multi-user concurrency, flexibility, ops
  cost.
- **SQLite** — single-file, zero-server, embeds in the Python process; ACID,
  queryable, easy to back up; one-writer-at-a-time concurrency.
- **Flat files (JSON/CSV per record type)** — simplest possible; no query
  surface, no schema enforcement.
- **No persistence (in-memory)** — current state; rules out multi-session use.

The tool has two users (the two partners) with a possible future manager. It is
a single-instance internal tool (PRD: "single-instance server is sufficient; no
multi-tenancy required"). The frozen dataclasses in `types.py` map 1:1 to
relational tables.

## Decision

**Use SQLite** as the persistence layer.

The frozen dataclasses (`Sale`, `Recipe`, `KegWeighIn`, `CafeStockCount`,
`ShiftClose`, `Purchase`, `FixedCost`, `CompletionEntry`, etc.) become tables.
Each existing store protocol (`LoyverseStore`, `ApprovalBook`, etc.) gains a
SQLite-backed implementation that satisfies the same protocol the in-memory
implementation satisfies today.

Credentials and connection configuration live in environment variables, not in
the database or the repo.

## Consequences

- **Zero ops.** No database server to install, run, secure, or back up. The
  DB is a file; backups are a `cp`.
- **Schema mirrors dataclasses.** No ORM impedance mismatch; the table shape
  is recognisable from `types.py`.
- **Concurrency is not a constraint.** Two partners on one tool will not hit
  SQLite's one-writer limit in practice.
- **Migration to Postgres is a connection-string change.** The schema is plain
  relational SQL; if a future manager introduces real write concurrency, the
  same schema runs on Postgres with the engine's store protocols unchanged.
- **Backups are filesystem operations.** Nightly snapshot of the SQLite file
  is sufficient (and the cloud VPS provider's snapshot covers it).

## Considered and rejected

- **Postgres** — ops overhead (server, backups, security) is unjustified for
  two users on a single instance. The "concurrent writers" argument doesn't
  apply to a two-person team.
- **Flat files** — no query surface; the engine already benefits from set-style
  queries ("sales for date X", "approvals since Y") that a relational DB serves
  natively. Re-implementing that over files is more work for less capability.
- **No persistence** — rules out the UI build entirely.

## References

- PRD "Open items": storage choice
- `CONTEXT.md` → Persistence
- Wave 1 PRD (this ADR's first application)
