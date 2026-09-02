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


#: Environment variable holding the payment-type → channel map for the IN-01
#: derivation (issue #147, per the #142 resolution): comma-separated
#: ``payment_type_id:channel`` pairs, e.g.
#: ``"abc...:cash,def...:card,ghi...:qr"``. The ids are opaque account UUIDs
#: (like the cafe category ids, ADR-0009) — Loyverse's "Cash"/"Card" tenders
#: and the venue's custom till-QR tender named "Transfer" all surface as
#: UUIDs, so the production map cannot ship in the repo. The payment ``name``
#: is documentation only; the derivation routes strictly by id.
#:
#: Unset or empty → empty map → every receipt with a payment is a hard
#: derivation error. That is correct today: the venue has not yet provided
#: its three UUIDs (a one-line back-office/API lookup records them here).
#: Until then the derivation must fail loudly rather than guess where money
#: landed — filing-grade books never guess.
PAYMENT_TYPE_CHANNELS_ENV = "LOYVERSE_PAYMENT_TYPE_CHANNELS"


@dataclass(frozen=True)
class PaymentChannelMap:
    """Immutable payment-type-id → channel routing for the IN-01 derivation.

    Three channels, no fourth (CONTEXT.md "Channel"): cash, qr, card. Built
    from env (``LOYVERSE_PAYMENT_TYPE_CHANNELS``) via :func:`parse_payment_channels`;
    the empty map routes nothing, and any payment hitting it is the
    derivation's hard "unknown payment type" error — never a best-guess.
    """

    channels: dict[str, str]

    def channel_for(self, payment_type_id: str) -> str | None:
        """The channel a payment type routes to, or ``None`` when unmapped.

        ``None`` is the error signal the parser turns into a
        ``LoyverseParseError`` — the caller never substitutes a default.
        """
        return self.channels.get(payment_type_id)


def parse_payment_channels(raw: str | None) -> dict[str, str]:
    """Parse ``LOYVERSE_PAYMENT_TYPE_CHANNELS`` into an id → channel dict.

    Comma-separated ``id:channel`` pairs. Whitespace around each part is
    dropped; empty entries are skipped. A pair with a missing colon or an
    unknown channel name is a hard ``ValueError`` — a typo'd env line must
    stop the derivation at configuration time, not corrupt books silently
    at sync time. Pure so tests pin the parsing rules.
    """
    if not raw:
        return {}
    result: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(
                f"bad payment-type channel entry {part!r}: expected "
                "<payment_type_id>:<channel>"
            )
        payment_type_id, _, channel = part.partition(":")
        payment_type_id = payment_type_id.strip()
        channel = channel.strip()
        if channel not in ("cash", "qr", "card"):
            raise ValueError(
                f"bad payment-type channel entry {part!r}: channel must be "
                "one of cash, qr, card"
            )
        result[payment_type_id] = channel
    return result


def payment_channels_from_env(
    env: dict[str, str] | None = None,
) -> PaymentChannelMap:
    """Read ``LOYVERSE_PAYMENT_TYPE_CHANNELS`` from ``env`` (defaults to ``os.environ``).

    Mirrors ``cafe_category_ids_from_env``'s shape: injectable env for
    tests, live environment for production callers.
    """
    source = env if env is not None else os.environ
    return PaymentChannelMap(
        channels=parse_payment_channels(source.get(PAYMENT_TYPE_CHANNELS_ENV))
    )


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
