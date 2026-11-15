"""适配器契约测试：能力声明、Fake 行为、上游 fail-fast（tasks.md T012/T013）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gss_account import FakeAccountAdapter, UpstreamAccountAdapter
from gss_checkout import FakeCheckoutAdapter, UpstreamCheckoutAdapter
from gss_contracts import (
    BillingMode,
    CheckoutRequest,
    ErrorCode,
    PaymentRequest,
    PaymentStatus,
    Plan,
)
from gss_contracts.errors import DomainError
from gss_payment import FakePaymentAdapter, UpstreamPaymentAdapter
from gss_payment.adapter import _confirmation_token


ALL_ADAPTERS = [FakeCheckoutAdapter(), FakePaymentAdapter(), FakeAccountAdapter()]


@pytest.mark.parametrize("adapter", ALL_ADAPTERS)
def test_every_adapter_declares_capabilities(adapter) -> None:
    """每个 adapter 必须能回答来源 / 许可证 / 能力 / 失败模式（ADR 0001）。"""

    caps = adapter.capabilities()
    assert caps.source
    assert caps.license
    assert caps.supports
    assert caps.failure_modes


def test_adapters_are_self_contained() -> None:
    """运行时不依赖其它本地仓库。"""

    assert "this repository" in UpstreamCheckoutAdapter().capabilities().license
    assert "this repository" in UpstreamAccountAdapter().capabilities().license
    assert "this repository" in UpstreamPaymentAdapter().capabilities().license


def test_fake_checkout_standard_is_priced() -> None:
    fake = FakeCheckoutAdapter()
    req = CheckoutRequest(
        session="fake",
        plan=Plan.PLUS,
        billing_country="US",
        billing_mode=BillingMode.STANDARD,
    )
    res = asyncio.run(fake.create_checkout(req))
    assert res.currency == "USD"
    assert str(res.amount) == "20.00"
    assert res.checkout_url.endswith("/fake/1")
    assert res.billing and res.billing["country"] == "US"
    assert res.billing["postal_code"]


def test_fake_checkout_team_uses_seats_and_hosted_url() -> None:
    fake = FakeCheckoutAdapter()
    req = CheckoutRequest(
        session="fake",
        plan=Plan.TEAM,
        billing_country="JP",
        billing_mode=BillingMode.STANDARD,
        seat_quantity=8,
        promo_code="TEAM50",
        workspace_name="Desk",
    )
    res = asyncio.run(fake.create_checkout(req))
    assert res.currency == "JPY"
    assert str(res.amount) == "200"
    assert res.checkout_kind == "hosted"
    assert res.publishable_key == "pk_test_fake"
    assert "pay.openai.com/c/pay/" in res.checkout_url
    assert fake.calls[0].seat_quantity == 8
    assert fake.calls[0].promo_code == "TEAM50"


def test_fake_checkout_promo_is_zero() -> None:
    fake = FakeCheckoutAdapter()
    req = CheckoutRequest(
        session="fake",
        plan=Plan.PLUS,
        billing_country="PH",
        billing_mode=BillingMode.PROMO,
    )
    res = asyncio.run(fake.create_checkout(req))
    assert res.currency == "PHP"
    assert str(res.amount) == "0.00"


def test_fake_payment_scripted_3ds_then_success() -> None:
    fake = FakePaymentAdapter(
        scripted=[PaymentStatus.REQUIRES_ACTION, PaymentStatus.SUCCEEDED]
    )
    req = PaymentRequest(checkout_url="u", country="US", payment_method_token="pm_fake")
    first = asyncio.run(fake.pay(req))
    assert first.status is PaymentStatus.REQUIRES_ACTION
    assert first.next_action and first.next_action["type"] == "3ds_redirect"
    second = asyncio.run(fake.pay(req))
    assert second.status is PaymentStatus.SUCCEEDED
    assert len(fake.calls) == 2


def test_upstream_checkout_fails_fast_without_session() -> None:
    req = CheckoutRequest(
        session="", plan=Plan.PLUS, billing_country="US", billing_mode=BillingMode.STANDARD
    )
    with pytest.raises(DomainError) as exc:
        asyncio.run(UpstreamCheckoutAdapter().create_checkout(req))
    assert exc.value.code is ErrorCode.INVALID_SESSION


def test_confirmation_token_keeps_browser_ctoken() -> None:
    class _Client:
        def create_confirmation_token(self, **kwargs):
            raise AssertionError("ctoken_ should not be converted")

    assert _confirmation_token(_Client(), publishable_key="pk", token="ctoken_abc") == "ctoken_abc"


def test_upstream_payment_fails_fast_without_session() -> None:
    req = PaymentRequest(checkout_url="u", country="US", payment_method_token="pm")
    with pytest.raises(DomainError) as exc:
        asyncio.run(UpstreamPaymentAdapter().pay(req))
    assert exc.value.code is ErrorCode.INVALID_SESSION


def test_fake_account_reports_inactive_when_configured() -> None:
    fake = FakeAccountAdapter(active=False)
    info = asyncio.run(fake.subscription_status("s"))
    assert info.active is False
    assert info.expires_at is None
    assert fake.sessions_seen == ["s"]


@pytest.mark.parametrize("plan", ["plus", "go", "prolite", "pro", "promax", "team", "business"])
@pytest.mark.parametrize("active", [True, False])
def test_real_account_adapter_recognizes_all_subscription_tiers(monkeypatch, plan, active):
    closed = []
    client = SimpleNamespace(subscription_payload=lambda: {
        "entitlement": {"subscription_plan": plan, "has_active_subscription": active},
    }, close=lambda: closed.append(True))
    monkeypatch.setattr("gss_account.adapter.ChatGPTClient.from_raw_session", lambda *args, **kwargs: client)
    info = asyncio.run(UpstreamAccountAdapter().subscription_status("fixture-session"))
    assert info.active is active
    assert info.raw_kind == plan
    assert closed == [True]
