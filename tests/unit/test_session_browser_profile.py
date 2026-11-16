"""Browser metadata survives credential rotation and region changes without network access."""
from __future__ import annotations

import asyncio
import base64
import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from gss_contracts import BillingMode, CheckoutRequest, PaymentRequest, PaymentResult, PaymentStatus, Plan
from gss_core.chatgpt import ChatGPTClient
from gss_core.plus_har_protocol import PlusHarProtocol
from gss_core.saved_cards import session_snapshot
from gss_core.stripe_protocol import StripeProtocol
from gss_checkout.adapter import UpstreamCheckoutAdapter
from gss_payment.adapter import UpstreamPaymentAdapter


FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:160.0) Gecko/20100101 Firefox/160.0"
METADATA = {"userAgent": FIREFOX_UA, "locale": "zh-CN", "deviceId": "original-device",
            "ip": "192.0.2.50", "country": "US", "region": "California", "regionCode": "CA",
            "timezone": "Asia/Shanghai", "adsSegmentId": 7}


def access_token(label):
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "account-profile"}, "fixture": label}
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "none"}) + "." + encode(claims) + ".fixture"


def original_session():
    return {"accessToken": access_token("original"), "sessionToken": "original-cookie",
            "statsigContext": deepcopy(METADATA)}


@pytest.fixture(autouse=True)
def isolated_device_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GSS_DEVICE_STORE", str(tmp_path / "devices.json"))


def test_repeated_session_import_reuses_the_accounts_device_values():
    raw = json.dumps(original_session())
    first = ChatGPTClient.from_raw_session(raw)
    second = ChatGPTClient.from_raw_session(raw)
    try:
        assert first.browser_profile.identity_key() == second.browser_profile.identity_key()
        assert first.browser_profile.guid == second.browser_profile.guid
        assert first.browser_profile.muid == second.browser_profile.muid
        assert first.browser_profile.sid == second.browser_profile.sid
    finally:
        first.close()
        second.close()


def test_original_browser_metadata_and_profile_survive_snapshot_and_region_selection():
    source = original_session()
    client = ChatGPTClient.from_raw_session(json.dumps(source), proxy="http://ph.invalid:3000")
    client.configure_checkout_environment("PH")
    snapshot = json.loads(session_snapshot(client))
    restored = ChatGPTClient.from_raw_session(json.dumps(snapshot))
    try:
        assert client.ua == FIREFOX_UA
        assert client.language == "zh-CN" and client.timezone == "Asia/Shanghai"
        assert client.device_id == "original-device"
        assert client.client_hints == {}
        assert client.browser_profile.impersonate.startswith("firefox")
        assert snapshot["statsigContext"] == METADATA == source["statsigContext"]
        assert restored.browser_profile.to_dict() == client.browser_profile.to_dict()
        restored.configure_checkout_environment("JP")
        assert restored.browser_profile.to_dict() == client.browser_profile.to_dict()
    finally:
        client.close()
        restored.close()


@pytest.mark.parametrize("plan", list(Plan))
def test_refresh_checkout_payment_and_renewal_share_original_browser_for_every_plan(monkeypatch, plan):
    requests = []
    transport_profiles = []
    checkout_body = {}
    subscription = {"will_renew": True}
    refreshed_metadata = {**METADATA, "country": "PH", "region": "Fixture Region", "ip": "198.51.100.20"}

    def response(payload):
        return SimpleNamespace(status_code=200, json=lambda: payload, headers={}, cookies={})

    class Transport:
        def __init__(self):
            self.headers = {}
            self.cookies = {}
        def record(self, method, url, kwargs):
            headers = {**self.headers, **kwargs.get("headers", {})}
            assert headers["User-Agent"] == FIREFOX_UA
            assert headers["oai-language"] == "zh-CN"
            assert headers["oai-device-id"] == "original-device"
            assert not any(name.lower().startswith("sec-ch-ua") for name in headers)
            requests.append((method, url))
        def get(self, url, **kwargs):
            self.record("GET", url, kwargs)
            if url.endswith("/api/auth/session"):
                assert kwargs["params"]["refresh"] == "true"
                return response({"accessToken": access_token("refreshed"), "sessionToken": "refreshed-cookie",
                                 "statsigContext": refreshed_metadata})
            assert url.endswith("/backend-api/subscriptions")
            return response({"plan_type": str(plan), "is_active": True,
                             "will_renew": subscription["will_renew"]})
        def post(self, url, **kwargs):
            self.record("POST", url, kwargs)
            if url.endswith("/backend-api/payments/checkout"):
                checkout_body.update(kwargs["json"])
                hosted = plan is Plan.TEAM
                checkout_id = "cs_test_profile" if hosted else "oaics_profile"
                url = ("https://pay.openai.com/c/pay/" if hosted else "https://chatgpt.com/checkout/openai_llc/") + checkout_id
                return response({"checkout_session_id": checkout_id, "url": url,
                                 "publishable_key": "pk_test_profile", "currency": "php"})
            assert url.endswith("/backend-api/subscriptions/cancel")
            subscription["will_renew"] = False
            return response({"success": True})
        def close(self):
            pass

    def transport_factory(**kwargs):
        transport_profiles.append(kwargs["impersonate"])
        assert kwargs["proxy"] == "http://ph.invalid:3000"
        return Transport()

    monkeypatch.setattr("gss_core.chatgpt._new_session", transport_factory)
    monkeypatch.setattr("gss_checkout.adapter._probe_stripe_proxy", lambda *args, **kwargs: True)
    monkeypatch.setattr(ChatGPTClient, "prepare_risk", lambda *args, **kwargs: None)
    monkeypatch.setattr(ChatGPTClient, "_quote_from_context", lambda *args, **kwargs: (Decimal("982.14"), "PHP"))
    monkeypatch.setattr(ChatGPTClient, "_quote_from_taxes", lambda *args, **kwargs: (Decimal("982.14"), "PHP"))
    context = {"session": json.dumps(original_session())}
    checkout = asyncio.run(UpstreamCheckoutAdapter().create_checkout(CheckoutRequest(
        session=context["session"], plan=plan, billing_country="PH", billing_mode=BillingMode.STANDARD,
        proxy="http://ph.invalid:3000", session_context=context,
    )))
    assert checkout_body["billing_details"] == {"country": "PH", "currency": "PHP"}
    updated = json.loads(context["session"])
    assert updated["accessToken"] == access_token("refreshed")
    assert updated["statsigContext"] == METADATA
    assert updated["observedStatsigContext"] == refreshed_metadata
    profile = updated["browserProfile"]
    payment = UpstreamPaymentAdapter()

    def pay(client, request):
        assert client.browser_profile.to_dict() == profile
        assert client.access_token == access_token("refreshed")
        assert client.language == "zh-CN" and client.timezone == "Asia/Shanghai"
        client.session_token = "payment-cookie"
        return PaymentResult(status=PaymentStatus.SUCCEEDED)

    monkeypatch.setattr(payment, "_pay_custom", pay)
    monkeypatch.setattr(payment, "_pay_team", pay)
    result = asyncio.run(payment.pay(PaymentRequest(
        session=context["session"], session_context=context, checkout_url=checkout.checkout_url,
        checkout_session_id=checkout.checkout_session_id, checkout_proxy="http://ph.invalid:3000",
        country="PH", plan=str(plan), billing={"country": "US"}, payment_method_token="pm_fixture",
        publishable_key="pk_test_profile",
    )))
    assert result.status is PaymentStatus.SUCCEEDED
    client = ChatGPTClient.from_raw_session(context["session"], proxy="http://ph.invalid:3000")
    if plan is Plan.TEAM:
        engine = StripeProtocol(profile=profile)
        payment_device = engine.browser_profile
    else:
        engine = PlusHarProtocol(client=client, session="", checkout_url=checkout.checkout_url,
                                 publishable_key="pk_test_profile", country="PH", plan=str(plan))
        payment_device = engine.device
    try:
        assert payment_device.to_dict() == profile
        assert client.session_token == "payment-cookie"
        assert client.sentinel_profile["impersonate"] == profile["impersonate"]
        assert client.cancel_subscription_renewal(expected_plan=str(plan))["will_renew"] is False
        assert client.browser_profile.to_dict() == profile
    finally:
        engine.close()
        client.close()
    assert len([row for row in requests if row[1].endswith("/backend-api/payments/checkout")]) == 1
    assert all(target.startswith("firefox") for target in transport_profiles)


def test_session_inspection_keeps_original_browser_metadata(monkeypatch):
    from gss_api.routers.sessions import _inspect_session
    monkeypatch.setattr(ChatGPTClient, "subscription_payload", lambda *args, **kwargs: {"plan_type": "free", "is_active": False})
    result = _inspect_session(json.dumps(original_session()))
    snapshot = json.loads(result.session)
    assert result.valid is True and result.active is False
    assert snapshot["statsigContext"] == METADATA
    assert snapshot["browserProfile"]["ua"] == FIREFOX_UA
