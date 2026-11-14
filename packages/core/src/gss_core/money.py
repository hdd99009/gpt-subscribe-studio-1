"""从结账 / 税费响应里抽出应付金额。

优先用 ChatGPT 自己的 `checkout_amount_minor`、发票 `amount_due`、
`total.taxInclusive`；Stripe 这类整数字段按最小货币单位换算。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Tuple

_ZERO_DECIMAL = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)
_MINOR_NAMES = (
    "checkout_amount_minor",
    "amount_total",
    "amount_due",
    "unit_amount",
    "taxInclusive",
    "tax_inclusive",
)


def quote_from_payload(payload: Any, default_currency: str = "") -> Tuple[Optional[Decimal], str]:
    if isinstance(payload, dict) and isinstance(payload.get("checkout_session"), dict):
        payload = payload["checkout_session"]
    currency = _currency_of(payload, default_currency)
    if isinstance(payload, dict):
        top = payload.get("amount_total")
        if top is not None:
            parsed = _coerce(top, currency, minor=True)
            if parsed is not None:
                return parsed, currency
        state = payload.get("checkout_state")
        if isinstance(state, dict):
            parsed = _checkout_state_amount(state, currency)
            if parsed is not None:
                return parsed, currency
    amount = _amount_of(payload, currency)
    return amount, currency


def _checkout_state_amount(state: dict, currency: str) -> Optional[Decimal]:
    total = state.get("total")
    if isinstance(total, dict):
        for key in ("total", "subtotal", "amount"):
            parsed = _minor_bucket(total.get(key), currency)
            if parsed is not None:
                return parsed
        parsed = _coerce(total.get("minorUnitsAmount"), currency, minor=True)
        if parsed is not None:
            return parsed
    items = state.get("lineItems")
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            for key in ("subtotal", "total", "amount"):
                parsed = _minor_bucket(first.get(key), currency)
                if parsed is not None:
                    return parsed
    return None


def _minor_bucket(bucket: Any, currency: str) -> Optional[Decimal]:
    if isinstance(bucket, dict) and bucket.get("minorUnitsAmount") is not None:
        return _coerce(bucket.get("minorUnitsAmount"), currency, minor=True)
    return None


def _currency_of(payload: Any, default: str) -> str:
    if isinstance(payload, dict):
        for name in ("checkout_state", "billing_details"):
            found = _find_string(payload.get(name), ("currency", "currency_code"), depth=0)
            if found:
                return found.upper()
    found = _find_string(payload, ("currency", "currency_code"), depth=0)
    return (found or default or "").strip().upper()


def _amount_of(payload: Any, currency: str) -> Optional[Decimal]:
    for name in _MINOR_NAMES:
        found = _find_named(payload, name, depth=0)
        if found is not None:
            parsed = _coerce(found, currency, minor=True)
            if parsed is not None:
                return parsed
    total = _find_named(payload, "total", depth=0)
    if isinstance(total, dict):
        for name in ("taxInclusive", "tax_inclusive", "total", "amount"):
            if name not in total:
                continue
            value = total[name]
            parsed = _coerce(value, currency, minor=isinstance(value, int))
            if parsed is not None:
                return parsed
    for name in ("total_amount", "display_amount", "amount", "total"):
        found = _find_named(payload, name, depth=0)
        if found is None or isinstance(found, (dict, list)):
            continue
        parsed = _coerce(found, currency, minor=False)
        if parsed is not None:
            return parsed
    return None


def _find_named(payload: Any, name: str, *, depth: int) -> Any:
    if depth > 6 or payload is None:
        return None
    if isinstance(payload, dict):
        if name in payload and payload[name] is not None:
            return payload[name]
        for key, value in payload.items():
            if key == "subscription_conversion_context":
                continue
            found = _find_named(value, name, depth=depth + 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_named(item, name, depth=depth + 1)
            if found is not None:
                return found
    return None


def _find_string(payload: Any, names: tuple[str, ...], *, depth: int) -> str:
    if depth > 6 or payload is None:
        return ""
    if isinstance(payload, dict):
        for key in names:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key, value in payload.items():
            if key == "subscription_conversion_context":
                continue
            found = _find_string(value, names, depth=depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_string(item, names, depth=depth + 1)
            if found:
                return found
    return ""


def _coerce(value: Any, currency: str, *, minor: bool) -> Optional[Decimal]:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    if isinstance(value, str):
        text = value.replace(",", "").replace(currency, "").strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        if minor and currency not in _ZERO_DECIMAL:
            return (Decimal(value) / Decimal(100)).quantize(Decimal("0.01"))
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    return None


__all__ = ["quote_from_payload"]
