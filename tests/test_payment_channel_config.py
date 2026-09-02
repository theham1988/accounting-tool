"""Unit seam for the IN-01 derivation's env configuration (issue #147).

The payment-type → channel map is the #142 resolution's decision 1: ids are
opaque account UUIDs, configured via ``LOYVERSE_PAYMENT_TYPE_CHANNELS``
(mirroring ADR-0009's ``LOYVERSE_CAFE_CATEGORY_IDS``), and a payment the map
cannot route is a hard error — never a guess. These tests pin the parsing
rules and the absent/empty semantics the resolution locked.
"""

from __future__ import annotations

import pytest

from tangerine.loyverse.config import (
    PAYMENT_TYPE_CHANNELS_ENV,
    PaymentChannelMap,
    parse_payment_channels,
    payment_channels_from_env,
)


def test_parse_basic_pairs() -> None:
    assert parse_payment_channels("uuid-a:cash,uuid-b:qr,uuid-c:card") == {
        "uuid-a": "cash",
        "uuid-b": "qr",
        "uuid-c": "card",
    }


def test_parse_strips_whitespace_and_skips_empties() -> None:
    assert parse_payment_channels(" uuid-a:cash , uuid-b:qr ,, ") == {
        "uuid-a": "cash",
        "uuid-b": "qr",
    }


def test_unset_or_blank_is_empty_map() -> None:
    """Unset/blank env = derivation not configured (routes nothing)."""
    assert parse_payment_channels(None) == {}
    assert parse_payment_channels("") == {}
    assert parse_payment_channels("   ") == {}


def test_missing_colon_is_value_error() -> None:
    with pytest.raises(ValueError, match="expected"):
        parse_payment_channels("uuid-a")


def test_unknown_channel_is_value_error() -> None:
    with pytest.raises(ValueError, match="channel must be one of"):
        parse_payment_channels("uuid-a:transfer")


def test_from_env_reads_configured_map() -> None:
    env = {PAYMENT_TYPE_CHANNELS_ENV: "uuid-a:cash,uuid-b:qr"}
    assert payment_channels_from_env(env).channels == {
        "uuid-a": "cash",
        "uuid-b": "qr",
    }


def test_from_env_absent_is_empty_map() -> None:
    assert payment_channels_from_env({}).channels == {}


def test_channel_for_returns_none_when_unmapped() -> None:
    """``None`` is the error signal — the parser turns it into a hard error."""
    channel_map = PaymentChannelMap(channels={"uuid-a": "cash"})
    assert channel_map.channel_for("uuid-a") == "cash"
    assert channel_map.channel_for("uuid-unknown") is None
