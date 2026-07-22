"""Loyverse sync configuration (slice 02).

Two concerns:

- ``LoyverseCredentials``: the stored access token (and store filter) the
  client authenticates with. Loyverse uses a single bearer access token issued
  from the back-office Integrations page (see PRD open item: "Specific Loyverse
  API endpoints and auth flow"). No client-secret/OAuth dance is needed for a
  single-instance internal tool.
- ``PollingConfig``: the polling cadence. PRD default is daily after close; the
  bar closes at 10pm, so the default ``after_close_hour`` is 22.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


#: Environment variable holding the comma-separated Loyverse cafe category UUIDs
#: (ADR-0009). The venue's cafe category id is opaque and unique to its Loyverse
#: account, so it cannot ship in the repo; the partner dumps it via
#: ``scripts/dump_loyverse_items.py`` and sets it here. Multiple UUIDs are
#: allowed (Loyverse sub-categories); empty values are skipped so a trailing
#: comma or a stray blank does not corrupt the set. Unset or empty → empty set
#: → every item is tagged bar (the documented default, ADR-0009).
CAFE_CATEGORY_IDS_ENV = "LOYVERSE_CAFE_CATEGORY_IDS"


def parse_cafe_category_ids(raw: str | None) -> frozenset[str]:
    """Parse ``LOYVERSE_CAFE_CATEGORY_IDS`` into a set of category UUIDs.

    Comma-separated; whitespace and empty entries are dropped so the value
    ``"uuid-1, uuid-2,"`` parses to ``frozenset({"uuid-1", "uuid-2"})``.
    ``None`` or an all-whitespace value parses to an empty set (the default
    that tags every item bar — ADR-0009). Pure so tests pin the parsing rule
    without mutating the environment.
    """
    if not raw:
        return frozenset()
    return frozenset(
        part.strip() for part in raw.split(",") if part.strip()
    )


def cafe_category_ids_from_env(
    env: dict[str, str] | None = None,
) -> frozenset[str]:
    """Read ``LOYVERSE_CAFE_CATEGORY_IDS`` from ``env`` (defaults to ``os.environ``).

    The explicit ``env`` parameter lets tests inject a fake environment
    without mutating the real one. Mirrors the env-reading style of the rest
    of the package: callers pass ``None`` to read the live environment.
    """
    source = env if env is not None else os.environ
    return parse_cafe_category_ids(source.get(CAFE_CATEGORY_IDS_ENV))


@dataclass(frozen=True)
class LoyverseCredentials:
    """Stored Loyverse access token plus optional store scoping.

    ``access_token`` is the bearer token from Loyverse's Integrations page.
    ``store_id`` optionally scopes every request to one store (the venue has a
    single store, but Loyverse is multi-store so the field is explicit).
    """

    access_token: str
    store_id: str | None = None


Cadence = Literal["hourly", "daily"]


@dataclass(frozen=True)
class PollingConfig:
    """How often the orchestrator polls Loyverse.

    Default matches the PRD: daily, after close. The bar closes at 10pm local
    so ``after_close_hour`` defaults to 22 (24h clock).
    """

    cadence: Cadence = "daily"
    after_close_hour: int = 22
