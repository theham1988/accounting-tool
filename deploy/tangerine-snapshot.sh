#!/usr/bin/env bash
#
# Tangerine Phuket — nightly SQLite snapshot + rotation (Wave 1, Slice 6).
#
# Takes a consistent snapshot of the live database using SQLite's `.backup`
# (safe even while the app is writing — it does not just `cp` an open file),
# then rotates old snapshots so only the most recent N are kept. Driven by
# cron (see deploy/crontab.example); the SQLite file snapshot is the Wave 1
# backup story.
#
# Configuration via environment (sourced from /etc/tangerine/env in cron):
#   TANGERINE_DB_PATH        path to the live database (default below)
#   TANGERINE_SNAPSHOT_DIR   where snapshots are written
#   TANGERINE_SNAPSHOT_KEEP  how many snapshots to retain (default 14)

set -euo pipefail

DB="${TANGERINE_DB_PATH:-/var/lib/tangerine/tangerine.db}"
DEST="${TANGERINE_SNAPSHOT_DIR:-/var/lib/tangerine/snapshots}"
KEEP="${TANGERINE_SNAPSHOT_KEEP:-14}"

mkdir -p "$DEST"
STAMP="$(date +%F)"
OUT="$DEST/tangerine.db.$STAMP.bak"

# Consistent online backup of the live DB.
sqlite3 "$DB" ".backup '$OUT'"

# Rotate: keep the newest $KEEP snapshots, delete the rest.
ls -1t "$DEST"/tangerine.db.*.bak 2>/dev/null \
  | tail -n +"$((KEEP + 1))" \
  | xargs -r rm -f

echo "snapshot ok: $OUT (keeping newest $KEEP)"
