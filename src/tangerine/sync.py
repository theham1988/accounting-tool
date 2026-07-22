"""Cron entrypoint: run a Loyverse sync against persisted state.

    python -m tangerine.sync

Wave 1, Slice 3: the script cron invokes nightly (slice 6 wires the actual
cron entry). It calls the same ``run_sync`` function the ``POST /sync`` route
calls — the two callers differ only in how they surface the result (a printed
line vs. an HTML fragment), so they can never drift apart.

Paths are configurable so tests can drive the script in-process without env
mutation (mirroring ``tangerine.__main__``). The real entrypoint (run by
``python -m tangerine.sync``) reads defaults from environment variables and
delegates to :func:`main`.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from .loyverse.config import LoyverseCredentials, cafe_category_ids_from_env
from .loyverse.sync import SyncResult, run_sync
from .storage.sqlite_store import SqliteLoyverseStore

#: Environment variable holding the SQLite database path. Lives in the
#: environment, not the repo, per ADR-0001. Mirrors the CLI / web app.
DB_PATH_ENV = "TANGERINE_DB_PATH"
DEFAULT_DB_PATH = "./tangerine.db"

#: Loyverse credentials live in the environment, never in the database or the
#: repo (PRD user story 11). Mirrors the web app's env var names so the same
#: ``/etc/tangerine/env`` file sources both.
LOYVERSE_TOKEN_ENV = "LOYVERSE_ACCESS_TOKEN"
LOYVERSE_STORE_ID_ENV = "LOYVERSE_STORE_ID"


def main(
    *,
    db_path: str | None = None,
    access_token: str | None = None,
    store_id: str | None = None,
    urlopen: Any = None,
    today: date | None = None,
    cafe_category_ids: frozenset[str] | None = None,
) -> None:
    """Run one Loyverse sync and print a one-line summary.

    ``db_path`` defaults to ``$TANGERINE_DB_PATH`` or ``./tangerine.db``.
    Credentials default to ``$LOYVERSE_ACCESS_TOKEN`` / ``$LOYVERSE_STORE_ID``.
    ``cafe_category_ids`` defaults to ``$LOYVERSE_CAFE_CATEGORY_IDS`` parsed
    into a set (ADR-0009); pass it explicitly to drive the script in-process
    without env mutation. ``urlopen`` is injectable so tests stub Loyverse's
    HTTP boundary without env mutation. All parameters are explicit so tests
    drive the script in-process; the real ``python -m tangerine.sync``
    entrypoint reads env defaults.

    Recipes and costs are deliberately not loaded here: the sync only writes
    sales and menu snapshots into the store, so config validity is the
    review's concern, not the sync's. Loading them would couple a legitimate
    sync to config-correctness (a partner mid-editing recipes would block
    their own nightly sync).

    A Loyverse failure (auth, transport, other API error) is caught inside
    ``run_sync`` and surfaced as a readable ``SyncResult.errors`` entry; this
    function prints it rather than raising, so cron's emailed output is a
    human-readable line instead of a traceback.
    """
    db = db_path or os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)
    token = access_token or os.environ.get(LOYVERSE_TOKEN_ENV)
    sid = store_id if store_id is not None else os.environ.get(LOYVERSE_STORE_ID_ENV)
    cafe_ids = (
        cafe_category_ids
        if cafe_category_ids is not None
        else cafe_category_ids_from_env()
    )

    if token is None:
        print(
            f"sync skipped: ${LOYVERSE_TOKEN_ENV} is not set "
            "(Loyverse credentials not configured)"
        )
        return

    credentials = LoyverseCredentials(access_token=token, store_id=sid)
    store = SqliteLoyverseStore.connect(db)
    try:
        result: SyncResult = run_sync(
            store=store,
            credentials=credentials,
            urlopen=urlopen,
            today=today,
            cafe_category_ids=cafe_ids,
        )
    finally:
        store.close()

    _print_summary(result)


def _print_summary(result: SyncResult) -> None:
    """Print a one-line sync summary a partner can read in cron output.

    On success the line names the rows-ingested and menu-changes counts. On
    failure it names the error so the partner knows what to fix (expired token,
    network blip, etc.) without digging through a traceback.
    """
    if result.errors:
        joined = "; ".join(result.errors)
        print(f"sync failed: {joined}")
        return
    print(
        f"sync ok: {result.rows_ingested} rows ingested, "
        f"{result.menu_changes} menu changes"
    )


if __name__ == "__main__":
    main()
