"""Subscription status and timestamps returned by the account service."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from gss_contracts import SubscriptionInfo


_ACTIVE_PLANS = {
    "plus", "chatgptplusplan", "go", "chatgptgoplan",
    "pro", "chatgptpro", "chatgptproplan", "pro_lite", "prolite", "chatgptprolite",
    "pro_max", "promax", "chatgptpromax", "team", "business", "chatgptteamplan",
    "chatgptbusinessplan", "enterprise", "active", "trialing", "paid",
}


def parse_subscription_time(value: Any) -> datetime | None:
    """Normalize upstream timestamps to UTC, including legacy naive UTC values."""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
            parsed = datetime.fromtimestamp(seconds, timezone.utc)
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _first_time(*values: Any) -> datetime | None:
    for value in values:
        parsed = parse_subscription_time(value)
        if parsed is not None:
            return parsed
    return None


def subscription_info_from_payload(payload: dict[str, Any]) -> SubscriptionInfo:
    entitlement = payload.get("entitlement")
    entitlement = entitlement if isinstance(entitlement, dict) else {}
    subscription = payload.get("subscription")
    subscription = subscription if isinstance(subscription, dict) else {}
    legacy_plan = payload.get("plan")
    legacy_plan = legacy_plan if isinstance(legacy_plan, dict) else {}
    plan = str(
        payload.get("plan_type") or entitlement.get("subscription_plan")
        or subscription.get("plan") or legacy_plan.get("type") or ""
    ).lower()
    status = str(subscription.get("status") or payload.get("status") or "").lower()
    active = (
        plan in _ACTIVE_PLANS or status in {"active", "trialing", "paid"}
        or payload.get("is_active") is True or entitlement.get("has_active_subscription") is True
    ) and payload.get("is_active") is not False and entitlement.get("has_active_subscription") is not False
    return SubscriptionInfo(
        active=active,
        started_at=_first_time(
            payload.get("active_start"), subscription.get("current_period_start"),
            payload.get("current_period_start"), subscription.get("start_date"), payload.get("start_date"),
        ),
        expires_at=_first_time(
            entitlement.get("expires_at"), subscription.get("expires_at"), payload.get("expires_at"),
        ),
        channel="card" if active else None,
        raw_kind=plan or status or "unknown",
    )
