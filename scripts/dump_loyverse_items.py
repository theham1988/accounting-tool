"""One-shot helper: dump the venue's Loyverse menu as TSV to stdout.

    python scripts/dump_loyverse_items.py > menu.tsv

Why this exists (post-Wave-1 grilling, see CONTEXT.md "Regular item"):
the daily review only costs revenue for items with a Loyverse-item → SKU
mapping in ``config/recipes.yaml``. Authoring those mappings needs the
real Loyverse item IDs, names, and categories as a worksheet — and the
menu changes over time, so this is reusable on every recipe refresh,
not a one-off.

It reuses the engine's existing ``LoyverseHttpClient`` (slice 02) — the
HTTP boundary is the only stub in tests, same as the sync seam. No new
Loyverse code is added; this is a thin caller of the existing client.

Usage:

    # From the repo root, with creds in the environment:
    LOYVERSE_ACCESS_TOKEN=... LOYVERSE_STORE_ID=... \\
        python scripts/dump_loyverse_items.py > menu.tsv

    # Or via the same /etc/tangerine/env file cron uses:
    set -a; . /etc/tangerine/env; set +a \\
        python scripts/dump_loyverse_items.py > menu.tsv

Output columns (tab-separated, one row per Loyverse *variant*):

    item_id    item_name    category_id    variant_id    sku    price

Loyverse stores the SKU on the variant, not the item (an item's own ``sku``
field is a distinct, usually-unset field — dumping it produced a worksheet
with an always-blank SKU column, which is exactly what shipped and caused
every recipe mapping to be authored against the item UUID instead of the
SKU sales actually resolve by). An item with N variants (e.g. sizes) gets N
rows, one per SKU, because each variant sells under its own SKU and needs
its own mapping line. An item with zero variants gets one row with blank
``variant_id``/``sku``/``price``, so it still surfaces in the worksheet for
the partner to notice and price.

Sorted by ``(item_id, variant_id)`` for stable diffs against an earlier
dump. ``sku`` and ``price`` may be blank — Loyverse variants don't have to
carry a SKU, and a stray variant without a price shows as 0.

Exit codes:
    0  success (menu dumped, possibly empty)
    1  Loyverse credentials not configured
    2  Loyverse API error (auth failure, transport error, non-2xx)
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterator

# Allow ``python scripts/dump_loyverse_items.py`` from a source checkout
# without installing the package: prepend src/ to the path. Mirrors the
# implicit pythonpath in pyproject.toml's [tool.pytest.ini_options].
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tangerine.loyverse.config import LoyverseCredentials  # noqa: E402
from tangerine.loyverse.http import (  # noqa: E402
    LoyverseApiError,
    LoyverseHttpClient,
)
from tangerine.loyverse.parser import variant_price  # noqa: E402
from tangerine.loyverse.payloads import LoyverseItem  # noqa: E402

#: Environment variables — mirror tangerine.sync so the same
#: /etc/tangerine/env file sources every Loyverse-touching entrypoint.
LOYVERSE_TOKEN_ENV = "LOYVERSE_ACCESS_TOKEN"
LOYVERSE_STORE_ID_ENV = "LOYVERSE_STORE_ID"

#: TSV header — also serves as documentation of the columns.
HEADER = ("item_id", "item_name", "category_id", "variant_id", "sku", "price")


def dump_menu(
    *,
    access_token: str | None = None,
    store_id: str | None = None,
    urlopen: Any = None,
    out: Any = None,
) -> int:
    """Fetch every Loyverse item and write a TSV worksheet.

    Parameters mirror :func:`tangerine.sync.main`: env-driven defaults
    with explicit overrides, and ``urlopen`` is injectable so tests stub
    Loyverse's HTTP boundary without network or env mutation. ``out`` is
    the writable stream (defaults to ``sys.stdout``); injected in tests
    to capture output.

    Returns the process exit code so the ``__main__`` block can surface
    a non-zero code on misconfiguration without a try/except ladder.
    """
    token = access_token or os.environ.get(LOYVERSE_TOKEN_ENV)
    if not token:
        print(
            f"dump skipped: ${LOYVERSE_TOKEN_ENV} is not set "
            "(Loyverse credentials not configured)",
            file=sys.stderr,
        )
        return 1
    sid = store_id if store_id is not None else os.environ.get(LOYVERSE_STORE_ID_ENV)
    sink = out if out is not None else sys.stdout

    credentials = LoyverseCredentials(access_token=token, store_id=sid)
    client = LoyverseHttpClient(credentials=credentials, urlopen=urlopen)

    try:
        rows = list(_iter_rows(client, store_id=sid))
    except LoyverseApiError as exc:
        print(f"dump failed: {exc}", file=sys.stderr)
        return 2

    sink.write("\t".join(HEADER) + "\n")
    for row in rows:
        sink.write("\t".join(row) + "\n")
    return 0


def _iter_rows(
    client: LoyverseHttpClient, *, store_id: str | None
) -> Iterator[tuple[str, str, str, str, str, str]]:
    """Yield one TSV-formatted row per Loyverse *variant*, sorted for stable diffs.

    Implemented as a generator so the caller's ``list(...)`` materialises
    only after the API walk completes — a mid-walk failure raises before
    any partial output is written, so the worksheet file is never a
    half-broken mix of header + partial rows.

    A variant's price is resolved by :func:`tangerine.loyverse.parser.
    variant_price` — the same function the sync path uses to populate
    ``MenuItem.sell_price`` — so the worksheet and the synced menu snapshot
    can never read a variant's price differently from each other again.
    Blank (rather than ``"0"``) when the variant has no price on record, so
    a partner sees an unpriced item rather than mistaking it for a free one.
    """
    items: list[LoyverseItem] = []
    for page in client.get_pages("items"):
        items.extend(page.get("items", []))
    items.sort(key=lambda raw: raw.get("id", ""))

    for raw in items:
        item_id = str(raw.get("id", ""))
        item_name = str(raw.get("item_name", ""))
        category_id = str(raw.get("category_id", ""))
        variants = raw.get("variants") or []
        if not variants:
            yield (item_id, item_name, category_id, "", "", "")
            continue
        for variant in sorted(variants, key=lambda v: v.get("variant_id", "")):
            variant_id = str(variant.get("variant_id", ""))
            sku = str(variant.get("sku", "") or "")
            price = variant_price(variant, store_id=store_id)
            yield (
                item_id,
                item_name,
                category_id,
                variant_id,
                sku,
                "" if price is None else str(price),
            )


if __name__ == "__main__":
    # Force UTF-8 on stdout/stderr regardless of platform default. On Windows
    # the default text encoding is the OEM/ANSI codepage (cp1252 on en-US),
    # which can't encode Thai item names — a partner piping the dump to a file
    # via ``> menu.tsv`` gets a UnicodeEncodeError mid-write. UTF-8 round-trips
    # every Loyverse payload cleanly and matches what the test fixtures assert.
    #
    # ``reconfigure`` lives on io.TextIOWrapper; the static stubs type
    # ``sys.stdout`` as the narrower TextIO protocol, which doesn't carry it,
    # so the call needs a ``union-attr`` ignore rather than ``attr-defined``.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.exit(dump_menu())
