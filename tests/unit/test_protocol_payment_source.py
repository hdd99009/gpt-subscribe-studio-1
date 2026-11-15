"""协议支付两种取 pm_ 方式的校验。

不碰网络：只测入参解析，pay-protocol 本身会真的打 Stripe。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

os.environ.setdefault("GSS_USE_FAKE", "1")

from fastapi import HTTPException

from gss_api.routers.orders import ProtocolPayRequest, resolve_protocol_payment_method
from gss_core import stripe_protocol

BILLING = {
    "email": "a@example.com",
    "name": "Fixture Holder",
    "line1": "1 Fixture St",
    "city": "Portland",
    "state": "OR",
    "postal_code": "97211",
    "country": "US",
}

CARD = {"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2030"}


def _payload(**extra) -> ProtocolPayRequest:
    return ProtocolPayRequest(billing=BILLING, **extra)


def test_card_only_is_accepted():
    assert resolve_protocol_payment_method(_payload(card=CARD)) == ""


def test_payment_method_id_only_is_accepted():
    assert resolve_protocol_payment_method(
        _payload(payment_method_id="pm_1AbCdEf")
    ) == "pm_1AbCdEf"


def test_neither_source_is_rejected():
    with pytest.raises(HTTPException) as exc:
        resolve_protocol_payment_method(_payload())
    assert exc.value.status_code == 400


def test_both_sources_are_rejected():
    # 卡号与 pm_ 同时给会让服务端和浏览器各提交一次，必须拦掉
    with pytest.raises(HTTPException) as exc:
        resolve_protocol_payment_method(
            _payload(card=CARD, payment_method_id="pm_1AbCdEf")
        )
    assert exc.value.status_code == 400


def test_payment_method_id_must_look_like_stripe_id():
    with pytest.raises(HTTPException) as exc:
        resolve_protocol_payment_method(_payload(payment_method_id="tok_not_a_pm"))
    assert exc.value.status_code == 400


def test_blank_payment_method_id_is_treated_as_absent():
    with pytest.raises(HTTPException):
        resolve_protocol_payment_method(_payload(payment_method_id="   "))


@pytest.mark.parametrize("curl_transport", [True, False])
def test_hosted_direct_transport_disables_environment_proxies(monkeypatch, curl_transport):
    received = []

    def transport(**kwargs):
        received.append(kwargs)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(stripe_protocol, "_HAS_CURL_CFFI", curl_transport)
    monkeypatch.setattr(stripe_protocol, "curl_requests", SimpleNamespace(Session=transport))
    monkeypatch.setattr(stripe_protocol.httpx, "Client", transport)
    engine = stripe_protocol.StripeProtocol(proxy="http://fixed.invalid:3000", stripe_direct=True)
    engine._http()
    assert engine.proxy is None
    assert received[0]["trust_env"] is False
    if curl_transport:
        assert received[0]["proxies"] == {"http": "", "https": ""}
    else:
        assert received[0]["proxy"] is None
    engine.close()


def test_hosted_init_uses_the_order_publishable_key(monkeypatch):
    engine = stripe_protocol.StripeProtocol(publishable_key="pk_test_order")
    sent = []

    def post(url, body, headers):
        sent.append(parse_qs(body))
        return SimpleNamespace(status_code=200, json=lambda: {"total_summary": {"due": 100}})

    monkeypatch.setattr(engine, "_post", post)
    monkeypatch.setattr(engine, "_align_locale", lambda proxy: ("en-US", "UTC"))
    engine.init_checkout("cs_test_fixture")
    assert sent[0]["key"] == ["pk_test_order"]
