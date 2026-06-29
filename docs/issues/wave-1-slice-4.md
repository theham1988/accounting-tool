## Parent

- #14 (Wave 1 — The 9am Review Spine)

## What to build

The fourth vertical slice of Wave 1. Gate every route behind a shared passphrase and ask the partner to identify themselves via a role selector. The selected role feeds the engine's existing per-attribution fields (`cashier_id`, `assignee_id`) on any action that needs it.

End-to-end behavior:

- Every route except `/login` redirects to `/login` if the request lacks a valid session cookie.
- The login page shows:
  - a passphrase field
  - a role selector (Daniel / Noi — populated from a config list so the future manager is data, not a code change)
- On submit: passphrase checked against a value from environment variables; on success, a **signed session cookie** is set carrying the selected role and the user lands on `/` (the review).
- On failure: the login page re-renders with an error; no indication of which field was wrong (avoid passphrase enumeration).
- Session cookies have an **inactivity timeout** (e.g. 8 hours); subsequent requests within the window refresh it; after the window, the next request redirects to `/login`.
- Logout route clears the cookie.

Routes added:

- `GET /login` — render the login form
- `POST /login` — validate, set session cookie, redirect to `/`
- `POST /logout` — clear session cookie, redirect to `/login`

Auth middleware gates all other routes. Session cookies are signed (e.g. via `itsdangerous`) so they cannot be tampered with; the signed payload carries the role and the last-activity timestamp.

The role selector's options come from a config (env or file) — the existing `Assignee` shape in `types.py` already models partners, so the selector is populated from a list of `Assignee`s. Adding the future manager (PRD user story 31) is a config entry, not a code change.

TLS itself (HTTPS) is Slice 6 — this slice implements the auth flow assuming TLS will be there in production. Sessions must still be marked secure-only when served over HTTPS (controlled by an env flag so local dev over HTTP still works).

## Acceptance criteria

- [ ] Unauthenticated requests to any route except `/login` redirect to `/login`.
- [ ] The login page shows a passphrase field and a role selector populated from config.
- [ ] Correct passphrase + role selection sets a signed session cookie and lands the user on `/`.
- [ ] Wrong passphrase re-renders the login page with an error and no hint about which field was wrong.
- [ ] Session cookies are signed — tampering with the cookie invalidates the session.
- [ ] After the inactivity timeout, the next request redirects to `/login`.
- [ ] Requests within the timeout window refresh the last-activity timestamp.
- [ ] `POST /logout` clears the session cookie and redirects to `/login`.
- [ ] The selected role is available to other routes (so future capture flows can attribute actions) — wired into the request context, not yet consumed by Wave 1 capture (which doesn't exist yet).
- [ ] Session cookies are marked secure-only when served over HTTPS (env-controlled so local HTTP dev works).
- [ ] Role options come from config; adding a new role (the future manager) is a config change, not a code change.
- [ ] UI seam tests extended to cover login flow: unauthenticated → redirect; correct login → 200 on `/`; wrong passphrase → login re-renders; tampered cookie → redirect to login; expired session → redirect to login.

## Blocked by

- #16 (Slice 2 — FastAPI app + Jinja2 daily review)
