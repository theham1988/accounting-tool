"""End-to-end seam for the menu-dump helper (scripts/dump_loyverse_items.py).

The dump script is the recipe-authoring worksheet generator (see
CONTEXT.md "Regular item"): it walks Loyverse's /items endpoint via the
existing :class:`LoyverseHttpClient` and writes a TSV. The genuine
external boundary is Loyverse HTTP, stubbed here with the same
``StubHttp`` shape the sync seam (tests/test_loyverse_sync_e2e.py) uses.

These tests pin the four properties a partner relies on:

- the worksheet has the expected columns and one row per Loyverse
  *variant* (SKUs live on variants, not items — see module docstring);
- an item with several variants (e.g. sizes) gets one row per variant,
  each with its own SKU, since each sells under its own SKU and needs
  its own mapping line;
- items are sorted by ``item_id`` so diffs against an earlier dump are
  stable (matters because the worksheet is regenerated on every menu
  change);
- a Loyverse auth failure surfaces as a readable stderr line and a
  non-zero exit, so a partner running it manually knows the dump didn't
  succeed rather than getting a silent empty file.
"""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen as _real_urlopen

from scripts.dump_loyverse_items import HEADER, dump_menu


def _variant_json(
    *,
    variant_id: str,
    name: str,
    sku: str | None = None,
    price: float | None = None,
    store_price: float | None = None,
    store_id: str = "store-1",
) -> dict[str, Any]:
    """One minimal Loyverse variant entry (real field names, not guessed ones).

    The SKU lives here, not on the item. ``price`` maps to the real API's
    flat ``default_price`` field; ``store_price`` maps to a per-store
    override in ``stores`` (this venue prices everything per-store, so
    ``default_price`` is always ``None`` there and every real price comes
    through ``stores`` instead — pass ``store_price`` to exercise that path).
    """
    out: dict[str, Any] = {"variant_id": variant_id, "option1_value": name}
    if sku is not None:
        out["sku"] = sku
    out["default_price"] = price
    if store_price is not None:
        out["stores"] = [{"store_id": store_id, "price": store_price}]
    return out


def _item_json(
    *,
    item_id: str,
    name: str,
    category_id: str = "cat-bar",
    sku: str | None = None,
    price: float | None = None,
    variants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One minimal Loyverse /items entry (mirrors the sync seam's helper).

    ``sku``/``price`` are convenience shorthand for the common single-variant
    item — they build one variant under the hood. Pass ``variants`` directly
    for a multi-variant item (e.g. sizes), each with its own SKU.
    """
    if variants is None:
        variants = []
        if price is not None or sku is not None:
            variants.append(
                _variant_json(variant_id=f"{item_id}-v1", name=name, sku=sku, price=price)
            )
    return {
        "id": item_id,
        "item_name": name,
        "category_id": category_id,
        "variants": variants,
    }


def _envelope(items: list[dict[str, Any]], cursor: str | None = None) -> bytes:
    return json.dumps({"items": items, "cursor": cursor}).encode("utf-8")


class _StubResponse:
    """Minimal HTTPResponse stand-in matching the LoyverseHttpClient seam."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self, amt: int = -1) -> bytes:
        del amt  # the client reads everything in one go
        return self._body


def _stub_pages(pages: list[bytes]) -> Any:
    """Return a urlopen stub that yields each page in turn.

    Mirrors the sync seam: the client calls ``urlopen(url, headers,
    params)`` per page, so the stub pops one canned response per call.
    """
    seq = iter(pages)

    def _open(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> _StubResponse:
        del url, headers, params
        body = next(seq)
        return _StubResponse(body)

    return _open


# --- tests ------------------------------------------------------------------


def test_dump_writes_header_and_one_row_per_variant() -> None:
    """A successful dump produces the header line plus one TSV row per variant.

    The worksheet's job is to give a partner the (item_id, item_name, sku)
    triples needed to author Loyverse-item → SKU mappings — and the SKU
    lives on the *variant*, not the item, in real Loyverse payloads. This
    pins both the column shape and the row-per-variant invariant so a
    future change can't silently drop or rename a column the partner's
    spreadsheet template is built on, or regress to reading the item-level
    ``sku`` field (which is always blank for this venue's real menu).
    """
    page = _envelope([
        _item_json(item_id="b", name="Chang Draft", sku="chang-draft-500", price=120),
        _item_json(item_id="a", name="Espresso Latte", category_id="cat-cafe"),
    ])
    out = io.StringIO()

    rc = dump_menu(
        access_token="tok",
        store_id="store-1",
        urlopen=_stub_pages([page]),
        out=out,
    )

    assert rc == 0
    lines = out.getvalue().splitlines()
    assert lines[0] == "\t".join(HEADER)
    # Sorted by item_id: "a" (latte) before "b" (chang).
    assert lines[1].split("\t")[0] == "a"
    assert lines[1].split("\t")[1] == "Espresso Latte"
    assert lines[2].split("\t")[0] == "b"
    assert lines[2].split("\t")[1] == "Chang Draft"
    assert lines[2].split("\t")[4] == "chang-draft-500"  # sku column
    assert lines[2].split("\t")[5] == "120"               # price


def test_dump_expands_multi_variant_item_into_one_row_per_sku() -> None:
    """An item with several variants (e.g. sizes) gets one row per variant.

    Each variant sells under its own SKU, so a partner needs one mapping
    line per variant, not one per item. This is the exact defect the
    item-level (rather than variant-level) dump used to hide: a multi-
    variant item collapsed into a single row that only reflected the
    first variant, silently losing every other variant's SKU.
    """
    page = _envelope([
        _item_json(
            item_id="c",
            name="Latte",
            category_id="cat-cafe",
            variants=[
                _variant_json(variant_id="c-v1", name="Latte Small", sku="10046", price=90),
                _variant_json(variant_id="c-v2", name="Latte Large", sku="10069", price=110),
            ],
        ),
    ])
    out = io.StringIO()

    rc = dump_menu(access_token="tok", urlopen=_stub_pages([page]), out=out)

    assert rc == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 3  # header + one row per variant
    row1, row2 = lines[1].split("\t"), lines[2].split("\t")
    # Sorted by variant_id within the item.
    assert row1[3] == "c-v1" and row1[4] == "10046" and row1[5] == "90"
    assert row2[3] == "c-v2" and row2[4] == "10069" and row2[5] == "110"


def test_dump_reads_per_store_price_when_default_price_is_none() -> None:
    """When a variant has no flat price, the dump reads the store's override.

    This venue prices every variant per-store rather than with a single
    flat price, so ``default_price`` is ``None`` on real payloads and the
    real price only exists in ``stores``. Passing the dump's own
    ``store_id`` (the same one used to scope the API call) picks the right
    entry rather than an arbitrary one.
    """
    page = _envelope([
        _item_json(
            item_id="e",
            name="Chang Draft",
            variants=[
                _variant_json(
                    variant_id="e-v1",
                    name="Chang Draft",
                    sku="10099",
                    price=None,
                    store_price=140,
                    store_id="store-9",
                ),
            ],
        ),
    ])
    out = io.StringIO()

    rc = dump_menu(
        access_token="tok", store_id="store-9", urlopen=_stub_pages([page]), out=out
    )

    assert rc == 0
    row = out.getvalue().splitlines()[1].split("\t")
    assert row[4] == "10099" and row[5] == "140"


def test_dump_handles_item_with_no_variants() -> None:
    """An item with zero variants still surfaces as one row, fields blank.

    A stray item with no variants has no SKU to report, but it should not
    vanish from the worksheet — a partner needs to see it to notice it's
    unpriced/unmapped, same as the daily review's "needs attention" list.
    """
    page = _envelope([_item_json(item_id="d", name="Misc Item")])
    out = io.StringIO()

    rc = dump_menu(access_token="tok", urlopen=_stub_pages([page]), out=out)

    assert rc == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 2
    row = lines[1].split("\t")
    assert row[0] == "d" and row[1] == "Misc Item"
    assert row[3] == "" and row[4] == "" and row[5] == ""


def test_dump_follows_pagination_cursor() -> None:
    """A menu spanning multiple /items pages is flattened into one worksheet.

    Loyverse paginates via ``cursor``; the underlying client already
    follows it (slice 02). This guards that the dump script hasn't
    accidentally re-introduced a one-page-only assumption — a menu with
    >50 items would otherwise be silently truncated.
    """
    page1 = _envelope(
        [_item_json(item_id="a", name="Item A")], cursor="next-page"
    )
    page2 = _envelope([_item_json(item_id="b", name="Item B")])
    out = io.StringIO()

    rc = dump_menu(
        access_token="tok",
        urlopen=_stub_pages([page1, page2]),
        out=out,
    )

    assert rc == 0
    lines = out.getvalue().splitlines()
    # header + two rows.
    assert len(lines) == 3
    assert {lines[1].split("\t")[0], lines[2].split("\t")[0]} == {"a", "b"}


def test_dump_handles_empty_menu() -> None:
    """An empty Loyverse menu produces just the header, exit 0.

    A fresh / misconfigured Loyverse account returns no items; the
    worksheet is the header only. This is not an error — a partner
    running the dump before any menu exists should get a clean empty
    worksheet, not a crash.
    """
    out = io.StringIO()
    rc = dump_menu(
        access_token="tok",
        urlopen=_stub_pages([_envelope([])]),
        out=out,
    )
    assert rc == 0
    assert out.getvalue().splitlines() == ["\t".join(HEADER)]


def test_dump_auth_failure_returns_nonzero(capsys: Any) -> None:
    """A Loyverse 401 surfaces as a readable stderr line and exit code 2.

    The dump is run interactively by a partner authoring mappings; a
    rejected token must read as 'failed' rather than producing an empty
    file the partner mistakes for a successful empty menu. Exit 2
    distinguishes 'Loyverse error' from exit 1 'creds not configured'.
    """
    def _raise(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        raise HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]

    rc = dump_menu(access_token="bad-token", urlopen=_raise, out=io.StringIO())
    assert rc == 2
    captured = capsys.readouterr()
    assert "dump failed" in captured.err
    assert "401" in captured.err


def test_dump_missing_token_exits_one(capsys: Any, monkeypatch: Any) -> None:
    """No token in arg or env → exit 1 with a readable stderr hint.

    Mirrors the sync entrypoint's behaviour: misconfiguration prints a
    'set $LOYVERSE_ACCESS_TOKEN' hint to stderr rather than a traceback.
    A partner running the script for the first time learns what to set.
    """
    monkeypatch.delenv("LOYVERSE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LOYVERSE_STORE_ID", raising=False)
    rc = dump_menu(out=io.StringIO())
    assert rc == 1
    captured = capsys.readouterr()
    assert "LOYVERSE_ACCESS_TOKEN" in captured.err
