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

from dataclasses import dataclass
from typing import Literal


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
