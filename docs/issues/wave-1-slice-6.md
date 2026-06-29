## Parent

- #14 (Wave 1 — The 9am Review Spine)

## What to build

The sixth and final vertical slice of Wave 1. Get the tool live on a cloud VPS at a real HTTPS URL, served by systemd + nginx, with cron-driven sync, nightly SQLite snapshots, rate-limited login, and a download-the-DB admin route. This is the slice that turns Wave 1 from "runs on a laptop" into "both partners open it from home at 9am."

End-to-end behavior:

- The tool is reachable at a public HTTPS URL (e.g. `https://tangerine.example.com/`), with a valid TLS certificate via Let's Encrypt / certbot.
- nginx terminates TLS and reverse-proxies to uvicorn running the FastAPI app under systemd.
- systemd restarts the app on crash so a transient error does not take the tool offline overnight.
- A **cron entry** runs `python -m tangerine.sync` nightly at 22:30 local time (configurable).
- The **SQLite file is snapshotted nightly** (e.g. `cp tangerine.db tangerine.db.YYYY-MM-DD.bak` rotated to keep the last N).
- The login route is **rate-limited** so the shared passphrase cannot be brute-forced (e.g. N attempts per IP per minute).
- An admin route `GET /admin/db-snapshot` (gated behind login from Slice 4) downloads the current SQLite file for out-of-band backup.
- Loyverse credentials and the auth passphrase live in `/etc/tangerine/env` (mode 0600, owned by root), sourced into the systemd unit's environment.
- **Deploys are reproducible from the repo**: a documented runbook (or a small deploy script) covers VPS provisioning, env setup, app install, nginx config, certbot, cron, and snapshot setup, so the server can be rebuilt from scratch if it dies.

Wiring included in this slice:

- systemd unit file
- nginx config (TLS, reverse proxy)
- certbot / Let's Encrypt setup documentation
- cron entry for nightly sync
- cron entry (or systemd timer) for nightly SQLite snapshot
- rate-limiting middleware on the login route
- `/admin/db-snapshot` route
- a `DEPLOY.md` runbook at the repo root (or under `docs/`)

Rate-limiting and the admin route are code; the rest is operations configuration living in the repo.

## Acceptance criteria

- [ ] The tool is reachable at a public HTTPS URL with a valid Let's Encrypt certificate.
- [ ] HTTP requests redirect to HTTPS.
- [ ] systemd manages the app process and restarts it on crash.
- [ ] nginx reverse-proxies to uvicorn.
- [ ] The nightly cron sync runs `python -m tangerine.sync` and writes results into the SQLite store.
- [ ] The SQLite file is snapshotted nightly and rotated.
- [ ] The login route is rate-limited (e.g. 5 attempts per IP per minute); excess attempts return 429.
- [ ] `GET /admin/db-snapshot` returns the current SQLite file as a download, gated behind login.
- [ ] Loyverse credentials and the auth passphrase live in `/etc/tangerine/env`, not in the repo or the database.
- [ ] `DEPLOY.md` documents the full VPS provisioning + deploy path so the server can be rebuilt from scratch.
- [ ] Both partners can reach the tool from home (off the venue network) and log in.
- [ ] A simulated cron failure (commenting out the entry) is recoverable via the "Sync now" button at 9:01am.

## Blocked by

- #16 (Slice 2 — FastAPI app + Jinja2 daily review)
- #17 (Slice 3 — Loyverse sync wiring + /sync route + cron entrypoint)
- #18 (Slice 4 — Identity: shared passphrase + role selector)

## Notes

This slice does not include the Wave 1 dogfooding period itself — that's the project-level done-definition (`CONTEXT.md` → Sequencing), not an issue. After Slice 6 lands, the team dogfoods the tool for ≥3 mornings before Wave 2 begins.
