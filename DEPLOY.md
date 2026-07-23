# Deploying Tangerine Phuket (Wave 1, Slice 6)

This runbook takes a fresh cloud VPS to a live, HTTPS-served 9am review that
both partners can open from home. It is deliberately reproducible: if the
server dies, follow it from the top to rebuild from scratch. Nothing here is
secret — the secrets live only in `/etc/tangerine/env` (mode `0600`, root).

Supporting files live in [`deploy/`](deploy/):

| File | Purpose |
| --- | --- |
| [`deploy/tangerine.service`](deploy/tangerine.service) | systemd unit — runs uvicorn, restarts on crash |
| [`deploy/nginx.conf`](deploy/nginx.conf) | nginx — TLS termination, HTTP→HTTPS redirect, reverse proxy |
| [`deploy/env.example`](deploy/env.example) | template for `/etc/tangerine/env` (secrets + config) |
| [`deploy/tangerine-snapshot.sh`](deploy/tangerine-snapshot.sh) | nightly consistent SQLite snapshot + rotation |
| [`deploy/crontab.example`](deploy/crontab.example) | cron entries — nightly sync + nightly snapshot |

Architecture: **nginx (:443, TLS) → uvicorn (127.0.0.1:8000) → FastAPI app →
SQLite file**. A nightly cron runs `python -m tangerine.sync`; a nightly cron
snapshots the SQLite file.

Throughout, replace `tangerine.example.com` with the real hostname.

---

## 0. Prerequisites

- A small cloud VPS (1 vCPU / 1 GB RAM is plenty), Ubuntu/Debian assumed below.
- A DNS `A`/`AAAA` record pointing `tangerine.example.com` at the VPS IP.
- A Loyverse access token + store id (Loyverse back office → Integrations).

## 1. Base packages and timezone

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git nginx sqlite3 certbot \
  python3-certbot-nginx cron
# Cron times are local; set the VPS to Phuket time so "22:30" means 22:30 there.
sudo timedatectl set-timezone Asia/Bangkok
```

## 2. Service user and directories

```bash
sudo useradd --system --create-home --home-dir /opt/tangerine --shell /usr/sbin/nologin tangerine
sudo mkdir -p /opt/tangerine/app /var/lib/tangerine/snapshots /var/log/tangerine /etc/tangerine
sudo chown -R tangerine:tangerine /opt/tangerine /var/lib/tangerine /var/log/tangerine
```

## 3. Get the code and build the virtualenv

```bash
sudo -u tangerine git clone https://github.com/theham1988/accounting-tool /opt/tangerine/app
cd /opt/tangerine/app
sudo -u tangerine python3 -m venv .venv
# Install the app plus the production server (the `serve` extra adds uvicorn).
sudo -u tangerine .venv/bin/pip install -e ".[serve]"
```

Recipes, SKU mappings and costs are version-controlled config files
(`config/*.yaml`) and ship with the clone — review them before going live (a
wrong recipe quantity silently corrupts every margin; the app fails loudly at
startup on malformed config or unknown SKU references).

## 4. Secrets and config — `/etc/tangerine/env`

```bash
sudo install -m 0600 -o root -g root deploy/env.example /etc/tangerine/env
sudo $EDITOR /etc/tangerine/env
```

Fill in a long random `TANGERINE_AUTH_PASSPHRASE`, a random
`TANGERINE_SIGNING_SECRET` (`openssl rand -hex 32`), and the Loyverse
credentials. Keep `TANGERINE_COOKIE_SECURE=1` (we serve over HTTPS).

`LOYVERSE_CAFE_CATEGORY_IDS` is the one non-secret Loyverse value. It is the
comma-separated list of Loyverse category UUIDs that tag menu items as the
cafe segment (ADR-0009); every other category defaults to bar. Leaving it
blank tags every item bar — correct for a fresh deployment, sharper once the
real cafe UUID is configured. To find it:

```bash
sudo -u tangerine bash -c 'set -a; . /etc/tangerine/env; set +a; \
    cd /opt/tangerine/app && .venv/bin/python scripts/dump_loyverse_items.py' \
    > /tmp/menu.tsv
# Read the category_id column for the cafe items, then paste it into
# LOYVERSE_CAFE_CATEGORY_IDS in /etc/tangerine/env and restart.
```

> The systemd unit and the cron jobs both read this one file. It is the only
> place credentials exist — never in the repo, never in the database.

## 5. Run the app under systemd

```bash
sudo cp deploy/tangerine.service /etc/systemd/system/tangerine.service
sudo systemctl daemon-reload
sudo systemctl enable --now tangerine.service
systemctl status tangerine.service          # should be active (running)
curl -fsS http://127.0.0.1:8000/login >/dev/null && echo "app up"
```

systemd restarts the app on crash (`Restart=on-failure`), so a transient error
does not take the tool offline overnight. To prove it:

```bash
sudo systemctl kill -s SIGKILL tangerine.service
sleep 3; systemctl is-active tangerine.service   # active again
```

## 6. TLS certificate and nginx

Install the site config, then let certbot obtain the certificate. The simplest
path uses the nginx plugin (it edits a temporary server block, validates, and
writes the cert):

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/tangerine
sudo sed -i 's/tangerine.example.com/YOUR.REAL.HOST/g' /etc/nginx/sites-available/tangerine
sudo mkdir -p /var/www/certbot
# Obtain + install the certificate (creates /etc/letsencrypt/live/YOUR.REAL.HOST/...).
sudo certbot certonly --webroot -w /var/www/certbot -d YOUR.REAL.HOST
# Now enable the site (it references the cert paths certbot just created).
sudo ln -sf /etc/nginx/sites-available/tangerine /etc/nginx/sites-enabled/tangerine
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

certbot installs a systemd timer that auto-renews; the `/.well-known/` location
in the HTTP server block keeps renewals working. Verify:

```bash
curl -I http://YOUR.REAL.HOST/        # 301 -> https
curl -I https://YOUR.REAL.HOST/login  # 200, valid cert
```

## 7. Nightly sync and snapshot (cron)

```bash
sudo crontab -u tangerine deploy/crontab.example
sudo crontab -u tangerine -l    # confirm both entries
```

- **22:30** — `python -m tangerine.sync` pulls the day's Loyverse sales/menu
  into SQLite (the first run backfills 30 days; re-runs are idempotent).
- **03:00** — `deploy/tangerine-snapshot.sh` writes a consistent
  `tangerine.db.YYYY-MM-DD.bak` into `/var/lib/tangerine/snapshots` and keeps
  the newest `TANGERINE_SNAPSHOT_KEEP` (default 14).

Run each once by hand to confirm before trusting cron:

```bash
sudo -u tangerine bash -c 'set -a; . /etc/tangerine/env; set +a; cd /opt/tangerine/app && .venv/bin/python -m tangerine.sync'
sudo -u tangerine bash -c 'set -a; . /etc/tangerine/env; set +a; ./deploy/tangerine-snapshot.sh'
ls -l /var/lib/tangerine/snapshots
```

## 8. Smoke test as a partner

1. Open `https://YOUR.REAL.HOST/` from a phone off the venue network → it
   redirects to `/login`.
2. Enter the passphrase, pick Daniel or Noi → land on yesterday's review.
3. Click **Sync now** → the result fragment reports rows ingested / menu
   changes; headline numbers refresh.
4. Visit `https://YOUR.REAL.HOST/admin/db-snapshot` → a `tangerine-snapshot-*.db`
   file downloads (your out-of-band backup).

## 9. Operating notes

### Brute-force protection
The login route is rate-limited to **5 attempts per IP per minute**; the 6th
returns `429`. nginx forwards the real client IP via `X-Forwarded-For` and the
app keys the limiter on the right-most (nginx-appended) entry, so the limit is
genuinely per-client and a spoofed header is ignored.

### The 9:01am "cron failed" recovery
If the nightly sync silently failed (expired token, network blip, clock drift),
the numbers look stale at 9am. Click **Sync now** — it runs the same sync the
cron does. To simulate the failure for a drill, comment out the sync line in the
crontab; the button still recovers the day.

### Restoring from a snapshot
```bash
sudo systemctl stop tangerine.service
sudo -u tangerine cp /var/lib/tangerine/snapshots/tangerine.db.YYYY-MM-DD.bak /var/lib/tangerine/tangerine.db
sudo systemctl start tangerine.service
```

### Logs
```bash
journalctl -u tangerine.service -f          # app
tail -f /var/log/tangerine/sync.log         # nightly sync
tail -f /var/log/tangerine/snapshot.log     # nightly snapshot
sudo tail -f /var/log/nginx/error.log       # TLS / proxy
```

### Updating to a new release
```bash
cd /opt/tangerine/app
sudo -u tangerine git pull
sudo -u tangerine .venv/bin/pip install -e ".[serve]"
sudo systemctl restart tangerine.service
```

Forward-only SQL migrations under `tangerine/storage/migrations/` are applied
automatically at startup, so a restart is all an update needs.
