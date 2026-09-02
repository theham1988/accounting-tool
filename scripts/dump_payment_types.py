"""One-shot helper: list the venue's Loyverse payment types (issue #147).

    python scripts/dump_payment_types.py

Why this exists: the IN-01 five-number derivation routes each receipt
payment through an env-configured payment-type → channel map
(``LOYVERSE_PAYMENT_TYPE_CHANNELS``, e.g.
``"<uuid>:cash,<uuid>:qr,<uuid>:card"``). The ids are opaque account UUIDs
that cannot ship in the repo, so this script lists them from Loyverse's
``GET /v1.0/payment_types`` for the partner to read and paste into the env
file. It reuses the engine's existing ``LoyverseHttpClient`` — the HTTP
boundary is the only seam, same as ``dump_loyverse_items.py``.

Output columns (TSM, one row per payment type):

    payment_type_id    name    type

Expected for this venue (July ground truth): "Cash" (type CASH), "Card"
(type NONINTEGRATEDCARD or similar), and the custom till-QR tender named
"Transfer" (type OTHER). Route them as cash / card / qr respectively.
Built-in CASH/Card ids are stable account constants; the custom "Transfer"
tender's id is what the env line is really for.

Exit codes:
    0  success (payment types listed, possibly empty)
    1  Loyverse credentials not configured
    2  Loyverse API error (auth failure, transport error, non-2xx)

Usage:
    # From the repo root, with creds in the environment:
    LOYVERSE_ACCESS_TOKEN=... python scripts/dump_payment_types.py

    # Or via the same /etc/tangerine/env file cron uses:
    set -a; . /etc/tangerine/env; set +a \\
        python scripts/dump_payment_types.py
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterator

# Allow ``python scripts/dump_payment_types.py`` from a source checkout
# without installing the package (mirrors scripts/dump_loyverse_items.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tangerine.loyverse.config import LoyverseCredentials  # noqa: E402
from tangerine.loyverse.http import (  # noqa: E402
    LoyverseApiError,
    LoyverseHttpClient,
)

#: Environment variables — mirror tangerine.sync so the same
#: /etc/tangerine/env file sources every Loyverse-touching entrypoint.
LOYVERSE_TOKEN_ENV = "LOYVERSE_ACCESS_TOKEN"
LOYVERSE_STORE_ID_ENV = "LOYVERSE_STORE_ID"

#: TSV header — also serves as documentation of the columns.
HEADER = ("payment_type_id", "name", "type")


def dump_payment_types(
    *,
    access_token: str | None = None,
    store_id: str | None = None,
    urlopen: Any = None,
    out: Any = None,
) -> int:
    """Fetch every Loyverse payment type and write a TSV list.

    Parameters mirror :func:`tangerine.sync.main`: env-driven defaults with
    explicit overrides, ``urlopen`` injectable for tests. Returns the
    process exit code so the ``__main__`` block surfaces a non-zero code on
    misconfiguration.
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
        rows = list(_iter_rows(client))
    except LoyverseApiError as exc:
        print(f"dump failed: {exc}", file=sys.stderr)
        return 2

    sink.write("\t".join(HEADER) + "\n")
    for row in rows:
        sink.write("\t".join(row) + "\n")
    return 0


def _iter_rows(client: LoyverseHttpClient) -> Iterator[tuple[str, str, str]]:
    """Yield one TSV row per payment type, sorted by id for stable diffs."""
    types: list[tuple[str, str, str]] = []
    for page in client.get_pages("payment_types"):
        for entry in page.get("payment_types", []):
            types.append(
                (
                    str(entry.get("id", "")),
                    str(entry.get("name", "")),
                    str(entry.get("type", "")),
                )
            )
    yield from sorted(types)


if __name__ == "__main__":
    raise SystemExit(dump_payment_types())
