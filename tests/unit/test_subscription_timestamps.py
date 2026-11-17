"""Subscription dates come from upstream subscription fields and retain their timezone."""
from datetime import datetime, timezone

import pytest

from gss_core.subscriptions import parse_subscription_time, subscription_info_from_payload


START = datetime(2026, 11, 16, 0, 49, 43, tzinfo=timezone.utc)
EXPIRY = datetime(2026, 12, 16, 6, 49, 43, tzinfo=timezone.utc)


def test_entitlement_expiry_and_current_activation_are_preserved():
    info = subscription_info_from_payload({
        "plan_type": "plus", "is_active": True,
        "active_start": "2026-11-16T00:49:43Z",
        "active_until": "2026-12-16T00:49:43Z",
        "entitlement": {"expires_at": "2026-12-16T06:49:43+00:00"},
        "expires_at": "2026-12-16T00:49:43Z",
    })
    assert info.active and info.raw_kind == "plus"
    assert info.started_at == START
    assert info.expires_at == EXPIRY


@pytest.mark.parametrize("value", [
    START, "2026-11-16T00:49:43Z", "2026-11-16T08:49:43+08:00",
    "2026-11-16T00:49:43", START.timestamp(), START.timestamp() * 1000,
])
def test_subscription_timestamps_are_normalized_to_utc(value):
    parsed = parse_subscription_time(value)
    assert parsed == START and parsed.tzinfo is timezone.utc


@pytest.mark.parametrize("value", [None, "", "bad-date", True, False, {}, [], float("nan"), float("inf")])
def test_missing_and_invalid_dates_remain_unknown(value):
    assert parse_subscription_time(value) is None


def test_missing_dates_are_not_inferred_from_other_account_fields():
    info = subscription_info_from_payload({
        "plan_type": "go", "is_active": True,
        "active_until": "2026-12-16T00:49:43Z",
        "created_at": "2026-11-15T01:00:00Z", "iat": START.timestamp(),
        "last_active_subscription": {"created_at": "2026-11-15T02:00:00Z"},
    })
    assert info.active and info.started_at is None and info.expires_at is None


def test_legacy_subscription_dates_are_still_supported():
    info = subscription_info_from_payload({"subscription": {
        "plan": "team", "status": "active", "current_period_start": START.timestamp(),
        "expires_at": EXPIRY.isoformat(),
    }})
    assert info.active and info.raw_kind == "team"
    assert info.started_at == START and info.expires_at == EXPIRY


def test_invalid_preferred_date_uses_valid_legacy_date():
    info = subscription_info_from_payload({
        "plan_type": "plus", "entitlement": {"expires_at": "invalid"},
        "expires_at": EXPIRY.isoformat(),
    })
    assert info.expires_at == EXPIRY


def test_explicitly_inactive_subscription_is_not_marked_active():
    info = subscription_info_from_payload({
        "plan_type": "plus", "is_active": False, "entitlement": None, "subscription": None, "plan": None,
    })
    assert info.active is False
