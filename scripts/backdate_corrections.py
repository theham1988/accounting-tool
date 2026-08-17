"""Backdate the Aug-17 cost corrections to their receipt dates.

    python scripts/backdate_corrections.py --dry-run   # plan only, no writes
    python scripts/backdate_corrections.py             # back up, then apply

The partner corrected 32 seeded prices on 2026-08-17 (avocado, butter,
salmon, ...). A correction is forward-only by design (ADR-0004 decision 2
amendment: pushing a correction into the past is "consciously
unsupported") — but those seed prices were wrong from the day they
landed, and the venue wants post-June-2026 history restated at the
corrected prices. The ``backdate_cost`` store method is the affordance:
it re-dates the SKU's current price to the receipt date, price_history
folds the marker into the reconstructed change-log, and every past
surface re-costs on its next render.

This script drives it from a CSV the partner fills in:

    sku_id,receipt_date
    avocado,2026-07-02
    butter,2026-06-28

Only SKUs whose Aug-17 edit was a *correction* (the audit entry has an
old price) are eligible; the CSV may cover any subset. A row naming a
SKU with no correction is an error, not a skip — a typo'd sku_id must
not silently no-op.

Runs through the audited store under one session_id: one "Revert this
session" click on ``/audit`` unwinds every re-dating.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

DEFAULT_DB_PATH = "./tangerine.db"
DEFAULT_CSV_PATH = os.path.join(
    os.path.expanduser("~"), "Downloads", "tangerine-backdates.csv"
)

ACTOR = "backdate-corrections"


@dataclass(frozen=True)
class Backdate:
    """One SKU's re-dating: its corrected price applies from receipt_date."""

    sku_id: str
    receipt_date: date


class ImportError_(Exception):
    """A validation failure — nothing has been written."""


def _correction_skus(conn: sqlite3.Connection) -> set[str]:
    """SKUs whose latest costs audit entry was a correction (old price set).

    The eligibility rule: ``backdate_cost`` re-dates the *current* price,
    which is only what the partner wants when that price arrived as a
    correction of an earlier one. (A first-ever price already reaches
    back — backdating it is legal but pointless, so it is not planned.)
    """
    rows = conn.execute(
        "SELECT pk, old_value FROM audit_log WHERE table_name = 'costs' "
        "ORDER BY entry_id DESC"
    ).fetchall()
    seen: set[str] = set()
    corrections: set[str] = set()
    for pk, old_value in rows:
        if pk in seen:
            continue
        seen.add(pk)
        if old_value is not None:
            corrections.add(pk)
    return corrections


def parse_csv(path: str | Path) -> list[Backdate]:
    """The partner's sku_id,receipt_date rows."""
    out: list[Backdate] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "sku_id" not in reader.fieldnames or (
            "receipt_date" not in reader.fieldnames
        ):
            raise ImportError_("CSV must have columns: sku_id, receipt_date")
        for row in reader:
            sku_id = (row.get("sku_id") or "").strip()
            when = (row.get("receipt_date") or "").strip()
            if not sku_id and not when:
                continue
            try:
                receipt_date = date.fromisoformat(when)
            except ValueError:
                raise ImportError_(
                    f"{sku_id!r}: receipt_date {when!r} is not YYYY-MM-DD"
                ) from None
            out.append(Backdate(sku_id=sku_id, receipt_date=receipt_date))
    if not out:
        raise ImportError_(f"{path}: no rows")
    return out


def validate(plan: list[Backdate], corrections: set[str]) -> None:
    dupes = sorted(
        {b.sku_id for b in plan if [x.sku_id for x in plan].count(b.sku_id) > 1}
    )
    if dupes:
        raise ImportError_(f"duplicate sku_id in CSV: {dupes}")
    unknown = sorted({b.sku_id for b in plan} - corrections)
    if unknown:
        raise ImportError_(
            f"sku_ids with no corrected price to backdate: {unknown}"
        )
    if any(b.receipt_date > date.today() for b in plan):
        raise ImportError_("receipt_date cannot be in the future")


def apply_backdates(
    store: Any, plan: list[Backdate], *, session_id: str, today: date
) -> None:
    with store.batch():
        for b in plan:
            store.backdate_cost(
                b.sku_id,
                effective_on=b.receipt_date,
                changed_by=ACTOR,
                session_id=session_id,
                reason="backdated to receipt date",
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("TANGERINE_DB_PATH", DEFAULT_DB_PATH),
        help="SQLite database path (default: $TANGERINE_DB_PATH or ./tangerine.db)",
    )
    parser.add_argument(
        "--csv",
        default=DEFAULT_CSV_PATH,
        help="backdates CSV path (default: ~/Downloads/tangerine-backdates.csv)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and print the plan only"
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        from tangerine.storage.config_store import SqliteConfigStore

        store = SqliteConfigStore(conn)
        plan = parse_csv(args.csv)
        corrections = _correction_skus(conn)
        validate(plan, corrections)

        for b in plan:
            print(f"  {b.sku_id:32s} corrected price applies from {b.receipt_date}")

        if args.dry_run:
            print("dry run — nothing written.")
            return 0

        backup = f"{args.db}.pre-backdates-{date.today():%Y%m%d}.bak"
        shutil.copyfile(args.db, backup)
        print(f"backup written: {backup}")

        session_id = f"backdate-corrections-{date.today():%Y%m%d}"
        apply_backdates(store, plan, session_id=session_id, today=date.today())
        print(f"applied (session {session_id})")
        return 0
    except ImportError_ as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type:ignore[union-attr]
    sys.exit(main())
