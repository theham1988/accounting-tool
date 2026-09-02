"""July 2026 replay harness — the IN-01 derivation's parity proof (issue #147).

Replays the venue's real July Loyverse receipts (the dashboard CSV export
that produced ``july-books-v1``'s IN-01 rows) through the tool's *actual*
derivation path — converter → ``parse_receipt_facts`` → store →
``derive_five_numbers`` — and proves parity against ``july_entry_plan.json``,
the oracle the poster consumed (25 trading days; cash ฿83,225 / QR
฿111,522.50 / card ฿10,570 / disc ฿15,272.50 → GROSS ฿220,590, the
documented P-11 figure).

    python scripts/replay_july.py [--csv PATH] [--plan PATH] [--verbose]

Defaults resolve to the sibling ``Account 2026 Redesign/assets`` tree (the
map's seed-spec home), so the harness runs unattended from the repo root on
this machine. Exit codes: 0 parity, 1 mismatch/usage, 2 files missing.

The CSV is the Loyverse *dashboard* export; the tool consumes the *API*
shape. ``csv_to_api_receipts`` bridges the two: each CSV row (one per
receipt — July had no split tenders) becomes an API-shaped receipt whose
``payments[]`` carry the row's payment type and ``Total collected``
(negative on refunds), and whose ``total_discounts[]`` carry ``Discounts``.
The channel map assigns placeholder ids — the venue's real payment-type
UUIDs aren't provided yet (the #147 sub-task); the mapping here mirrors
July's ground truth: Transfer→qr, Cash→cash, Card→card (the #142
resolution's method).

What "parity" means here, per the #142 resolution: the tool's derivation
reproduces ``july_entry_plan.py``'s method exactly — same channel map, same
net-collected measurement (refunds negative on their own day/channel), same
discount family (Σ total_discounts), same local-date buckets. The oracle's
per-day rows are the tool's per-day five numbers, to the baht.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent

# Allow running from a source checkout without installing the package
# (mirrors scripts/dump_loyverse_items.py and pyproject's pytest pythonpath).
sys.path.insert(0, str(_REPO / "src"))

from tangerine.loyverse.config import PaymentChannelMap  # noqa: E402
from tangerine.loyverse.derive import DayFiveNumbers, derive_five_numbers  # noqa: E402
from tangerine.loyverse.parser import parse_receipt_facts  # noqa: E402
from tangerine.loyverse.store import InMemoryLoyverseStore  # noqa: E402

# Default data home: the map's seed-spec tree sits beside the repo.
_DEFAULT_ASSETS = _REPO.parent / "Account 2026 Redesign" / "assets"
_DEFAULT_CSV = _DEFAULT_ASSETS / "july-2026" / "pos" / (
    "loyverse-receipts-2026-07-01-2026-07-28.csv"
)
_DEFAULT_PLAN = _DEFAULT_ASSETS / "july_entry_plan.json"

# July ground truth (july-books-v1 / the documented P-11 figure). The harness
# asserts these independently of the plan file so a corrupted plan cannot
# silently re-bless itself through the replay.
PARITY_TOTALS = {
    "trading_days": 25,
    "cash": Decimal("83225"),
    "qr": Decimal("111522.50"),
    "card": Decimal("10570"),
    "discount": Decimal("15272.50"),
    "gross": Decimal("220590"),
}

# The #142 resolution's July method: POS "Transfer" is the venue's till-QR
# tender. Placeholder ids stand in for the real UUIDs the venue hasn't
# provided yet; the parser sees only id→channel routing.
JULY_CHANNEL_MAP = {
    "pay-cash": "cash",
    "pay-transfer": "qr",
    "pay-card": "card",
}
CSV_PAYMENT_TO_ID = {
    "Cash": "pay-cash",
    "Transfer": "pay-transfer",
    "Card": "pay-card",
}

#: The dashboard export formats local timestamps like "7/28/26 1:33 PM",
#: with U+202F (narrow no-break space) or a plain space before the hour.
_CSV_DT = "%m/%d/%y %I:%M %p"


def _parse_csv_datetime(raw: str) -> datetime:
    """Parse a dashboard ``Date`` cell into a naive venue-local datetime."""
    cleaned = raw.replace(" ", " ").replace(" ", " ").strip()
    return datetime.strptime(cleaned, _CSV_DT)


def _local_to_utc_z(local: datetime) -> str:
    """Render a naive Asia/Bangkok datetime as a UTC ISO-8601 ``Z`` string.

    Bangkok is UTC+7 with no DST, so the conversion is a fixed −7h. The
    parser re-derives the local day (+7h) and lands back on the CSV's date —
    the round-trip is the same convention both sides of the API boundary use.
    """
    utc = local - timedelta(hours=7)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def csv_to_api_receipts(csv_path: Path) -> list[dict[str, Any]]:
    """Convert the dashboard receipts CSV into API-shaped receipt payloads.

    One CSV row = one receipt (July had no split tenders; the receipt-number
    column is unique across the export). Each becomes the shape
    ``parse_receipt_facts`` consumes:

    - ``payments``: one entry — the row's payment type (id via
      ``CSV_PAYMENT_TO_ID``) and its ``Total collected`` (already signed:
      refunds export negative), with ``name`` for documentation.
    - ``total_discounts``: one entry when ``Discounts`` is non-zero (the
      parser sums either way; emitting only non-zero keeps payloads honest).
    - ``total_money``: the row's ``Total collected`` — the API's per-receipt
      integrity assert (Σ payments == total_money) then holds by
      construction, mirroring the real API where equality holds today.

    Only ``Closed`` receipts should appear in the export; anything else is a
    shape change worth refusing rather than silently folding in.
    """
    receipts: list[dict[str, Any]] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row["Status"] != "Closed":
                raise ValueError(
                    f"receipt {row['Receipt number']!r}: unexpected Status "
                    f"{row['Status']!r} — the replay expects only Closed rows"
                )
            local_dt = _parse_csv_datetime(row["Date"])
            payment_name = row["Payment type"]
            payment_id = CSV_PAYMENT_TO_ID[payment_name]
            collected = row["Total collected"] or "0"
            discount = row["Discounts"] or "0"
            receipts.append(
                {
                    "receipt_number": row["Receipt number"],
                    "receipt_type": (
                        "SALE" if row["Receipt type"] == "Sale" else "REFUND"
                    ),
                    "created_at": _local_to_utc_z(local_dt),
                    "total_money": float(collected),
                    "payments": [
                        {
                            "payment_type_id": payment_id,
                            "name": payment_name,
                            "money_amount": float(collected),
                        }
                    ],
                    "total_discounts": (
                        [
                            {
                                "id": "disc",
                                "name": "Discounts",
                                "scope": "RECEIPT",
                                "money_amount": float(discount),
                            }
                        ]
                        if Decimal(discount) != 0
                        else []
                    ),
                }
            )
    return receipts


def derive_from_csv(csv_path: Path) -> list[DayFiveNumbers]:
    """Run the tool's real path over the CSV: parse → store → derive."""
    receipts = csv_to_api_receipts(csv_path)
    facts = parse_receipt_facts(
        {"receipts": receipts},
        PaymentChannelMap(channels=dict(JULY_CHANNEL_MAP)),
    )
    store = InMemoryLoyverseStore()
    store.record_receipt_facts(facts)
    return derive_five_numbers(store.receipt_facts())


def _fmt(n: Decimal) -> str:
    return f"฿{n:,.2f}"


def run_replay(csv_path: Path, plan_path: Path, *, verbose: bool = False) -> bool:
    """Run the replay; return True on parity. Prints a report either way."""
    derived = derive_from_csv(csv_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    expected_days = plan["days"]

    ok = True
    derived_by_date = {d.date: d for d in derived}

    # Per-day parity: every oracle day present with identical five numbers,
    # and no extra tool days.
    for day, exp in sorted(expected_days.items()):
        got = derived_by_date.get(day)
        if got is None:
            print(f"MISSING day {day}")
            ok = False
            continue
        pairs = [
            ("cash", got.cash, Decimal(str(exp["cash"]))),
            ("qr", got.qr, Decimal(str(exp["qr"]))),
            ("card", got.card, Decimal(str(exp["card"]))),
            ("discount", got.discount, Decimal(str(exp["disc"]))),
        ]
        for field, tool_v, oracle_v in pairs:
            if tool_v != oracle_v:
                print(
                    f"DELTA {day} {field}: tool {_fmt(tool_v)} "
                    f"vs oracle {_fmt(oracle_v)}"
                )
                ok = False
        if verbose:
            print(
                f"ok {day}: cash {_fmt(got.cash)} qr {_fmt(got.qr)} "
                f"card {_fmt(got.card)} disc {_fmt(got.discount)}"
            )
    extra = set(derived_by_date) - set(expected_days)
    if extra:
        print(f"EXTRA tool days absent from oracle: {sorted(extra)}")
        ok = False

    # Headline parity: the four channel sums + day count + gross identity.
    totals: dict[str, Decimal | int] = {
        "trading_days": len(derived),
        "cash": sum((d.cash for d in derived), Decimal("0")),
        "qr": sum((d.qr for d in derived), Decimal("0")),
        "card": sum((d.card for d in derived), Decimal("0")),
        "discount": sum((d.discount for d in derived), Decimal("0")),
    }
    totals["gross"] = (
        totals["cash"] + totals["qr"] + totals["card"] + totals["discount"]
    )
    for key, expected in PARITY_TOTALS.items():
        got = totals[key]
        if got != expected:
            print(f"TOTAL DELTA {key}: tool {got} vs oracle {expected}")
            ok = False

    print(
        f"\n{'PARITY' if ok else 'MISMATCH'} — {totals['trading_days']} trading days: "
        f"cash {_fmt(totals['cash'])} / QR {_fmt(totals['qr'])} / "
        f"card {_fmt(totals['card'])} / disc {_fmt(totals['discount'])} "
        f"→ GROSS {_fmt(totals['gross'])}"
    )
    return ok


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy codepage that can't encode ฿;
    # force UTF-8 so the report prints identically everywhere.
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=_DEFAULT_CSV)
    parser.add_argument("--plan", type=Path, default=_DEFAULT_PLAN)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.csv.is_file():
        print(f"missing receipts CSV: {args.csv}", file=sys.stderr)
        return 2
    if not args.plan.is_file():
        print(f"missing entry plan: {args.plan}", file=sys.stderr)
        return 2

    return 0 if run_replay(args.csv, args.plan, verbose=args.verbose) else 1


if __name__ == "__main__":
    raise SystemExit(main())
