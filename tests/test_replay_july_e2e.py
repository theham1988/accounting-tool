"""End-to-end July replay: the tool's derivation vs ``july-books-v1``'s
IN-01 rows (issue #147's acceptance oracle).

The replay is the parity proof the #142 resolution demands: replay the
venue's real July receipts (the dashboard CSV that produced the books' IN-01
rows) through the tool's actual derivation path — converter →
``parse_receipt_facts`` → store → ``derive_five_numbers`` — and match every
day and headline against ``july_entry_plan.json`` (the poster's oracle).

The data lives in the sibling ``Account 2026 Redesign/assets`` tree (the
map's seed-spec home); when those files are absent (e.g. CI, a bare clone)
the tests **skip** rather than fail — the parity proof is a machine-local
acceptance run, not a repo-carried fixture (the July dataset is 700 rows of
real revenue and stays where it lives).

In-repo fixtures are pinned separately: a compact slice of July-shaped
receipts proves the converter, and the hard totals are asserted so a
corrupted plan file cannot re-bless itself through the replay.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from scripts.replay_july import (
    JULY_CHANNEL_MAP,
    PARITY_TOTALS,
    csv_to_api_receipts,
    derive_from_csv,
    run_replay,
)

_REPO = Path(__file__).resolve().parents[1]
_ASSETS = _REPO.parent / "Account 2026 Redesign" / "assets"
_CSV = _ASSETS / "july-2026" / "pos" / "loyverse-receipts-2026-07-01-2026-07-28.csv"
_PLAN = _ASSETS / "july_entry_plan.json"

requires_july_data = pytest.mark.skipif(
    not (_CSV.is_file() and _PLAN.is_file()),
    reason="July dataset lives in the sibling Account 2026 Redesign tree",
)


_CSV_SLICE = """\
Date,Receipt number,Receipt type,Gross sales,Discounts,Net sales,Taxes,Total collected,Cost of goods,Gross profit,Payment type,Description,Dining option,POS,Store,Cashier name,Customer name,Customer contacts,Status
7/28/26 1:33 PM,4-10378,Sale,90.00,0.00,90.00,0.00,90.00,18.35,71.65,Transfer,1 x Chocoholic,Dine in,POS 1,Tangerine Cafe,Tangerine,,,Closed
7/28/26 12:57 PM,4-10371,Sale,200.00,0.00,200.00,0.00,200.00,40.00,160.00,Cash,1 x Chicken Salad,Dine in,POS 1,Tangerine Cafe,Tangerine,,,Closed
7/28/26 12:03 PM,4-10366,Sale,150.00,50.00,100.00,0.00,100.00,20.00,80.00,Card,1 x Beet the Heat,Dine in,POS 1,Tangerine Cafe,Tangerine,,,Closed
7/26/26 11:46 AM,4-10340,Refund,180.00,0.00,-180.00,0.00,-180.00,0.00,-180.00,Cash,1 x Refund,Dine in,POS 1,Tangerine Cafe,Tangerine,,,Closed
"""


def test_converter_maps_rows_to_api_shape(tmp_path: Path) -> None:
    csv_path = tmp_path / "slice.csv"
    csv_path.write_text(_CSV_SLICE, encoding="utf-8")

    receipts = csv_to_api_receipts(csv_path)

    assert [r["receipt_number"] for r in receipts] == [
        "4-10378",
        "4-10371",
        "4-10366",
        "4-10340",
    ]
    # Transfer → the till-QR placeholder id, per the #142 method.
    assert receipts[0]["payments"][0]["payment_type_id"] == "pay-transfer"
    # Refunds carry signed payments and SALE maps to SALE.
    assert receipts[3]["receipt_type"] == "REFUND"
    assert receipts[3]["payments"][0]["money_amount"] == -180.0
    assert receipts[3]["created_at"] == "2026-07-26T04:46:00.000Z"
    # Discounts emit a total_discounts entry; zero discounts emit none.
    assert receipts[2]["total_discounts"][0]["money_amount"] == 50.0
    assert receipts[0]["total_discounts"] == []


def test_converter_refuses_non_closed_status(tmp_path: Path) -> None:
    csv_path = tmp_path / "open.csv"
    # Rewrite only the first receipt's Status (the trailing column).
    header, first, *rest = _CSV_SLICE.splitlines()
    first = first[: first.rfind(",") + 1] + "Open"
    csv_path.write_text("\n".join([header, first, *rest]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected Status"):
        csv_to_api_receipts(csv_path)


def test_july_channel_map_mirrors_locked_method() -> None:
    """Transfer→qr, Cash→cash, Card→card — the #142 resolution's mapping."""
    assert set(JULY_CHANNEL_MAP.values()) == {"cash", "qr", "card"}


def test_parity_totals_match_resolution_figures() -> None:
    """The harness's hardcoded totals are the resolution's July figures."""
    assert PARITY_TOTALS["trading_days"] == 25
    assert PARITY_TOTALS["cash"] == D("83225")
    assert PARITY_TOTALS["qr"] == D("111522.50")
    assert PARITY_TOTALS["card"] == D("10570")
    assert PARITY_TOTALS["discount"] == D("15272.50")
    assert PARITY_TOTALS["gross"] == D("220590")


@requires_july_data
def test_replay_july_parity() -> None:
    """25 trading days, every day matching, headline = GROSS ฿220,590."""
    assert run_replay(_CSV, _PLAN)


@requires_july_data
def test_replay_derives_the_documented_day() -> None:
    """Spot-day: 26 Jul nets the ฿180 refund into cash (P-11 in action)."""
    days = {d.date: d for d in derive_from_csv(_CSV)}
    july26 = days["2026-07-26"]
    assert july26.cash == D("3785")
    assert july26.qr == D("3240")
    assert july26.card == D("200")
    assert july26.discount == D("180")
