"""Team prepare -> pm -> confirm -> challenge -> server GET poll, without network."""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_core.vault import vault
from gss_contracts import OrderStatus, SubscriptionInfo
from gss_core.stripe_protocol import PaymentOutcome, StripeProtocol

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/team_hosted_success.json").read_text())
BILLING = {"name": "Fixture", "line1": "Fixture Street", "city": "Fixture City", "state": "OR", "postal_code": "00000", "country": "US"}


@pytest.fixture
def setup_team(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    store.clear()
    client = TestClient(create_app())
    r = client.post("/api/orders", json={"plan": "team", "billing_country": "EG", "seat_quantity": 2,
                                       "session": "fixture-session"})
    assert r.status_code == 201
    order_id = r.json()["id"]
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    calls = []
    class FakeClient:
        def close(self): pass
        def cancel_subscription_renewal(self, *, expected_plan):
            assert expected_plan == "team"
            return {"will_renew": False, "already_cancelled": False}
    class Hosted:
        def __init__(self, client): pass
        def prepare(self, **kw):
            calls.append(("prepare", kw))
            return {"context_id": "fixture-context", "session_id": kw["session_id"], "publishable_key": kw["key"],
                    "amount_minor": 115000, "currency": "egp", "eid": "NA", "billing": kw["billing"],
                    "prepared_at": time.time(), "checkout_config_id": "fixture-config-updated",
                    "elements_session_id": "elements_fixture", "client_session_id": "client_fixture",
                    "guid": "fixture-guid", "muid": "fixture-muid", "sid": "fixture-sid", "passive_captcha": {}}
        def poll(self, **kw):
            calls.append(("GET-poll", kw))
            return FIXTURE["poll"]["record"]["response"]["fields"]
    monkeypatch.setattr("gss_api.routers.orders._team_client", lambda *a: FakeClient())
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Hosted)
    return client, order_id, calls


def test_prepare_requotes_before_pm_and_keeps_egp(setup_team):
    client, order_id, calls = setup_team
    r = client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING, "browser_timezone": "UTC"})
    assert r.status_code == 200, r.text
    assert r.json()["order"]["currency"] == "EGP"
    assert r.json()["order"]["amount"] == "1150"
    assert r.json()["order"]["billing_country"] == "EG"
    assert r.json()["context"]["checkout_config_id"] == "fixture-config-updated"
    assert "billing" not in r.json()["context"]
    assert calls[0][1]["billing"]["country"] == "US"
    assert store.get_order(order_id).status is OrderStatus.AWAITING_PAYMENT


def test_poll_top_level_success_delivers_and_clears_secrets(setup_team):
    client, order_id, calls = setup_team
    store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
    r = client.post(f"/api/orders/{order_id}/team/poll")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "delivered"
    assert r.json()["renewal"]["status"] == "cancelled"
    assert vault.get(order_id) is None
    assert calls[0][0] == "GET-poll"
    # Duplicate poll after delivery must not require secrets or issue another upstream call.
    assert client.post(f"/api/orders/{order_id}/team/poll").json()["status"] == "delivered"
    assert len(calls) == 1


def test_team_summary_retains_subscription_dates_after_cancellation(setup_team, monkeypatch):
    client, order_id, _ = setup_team

    class SubscriptionClient:
        last_subscription = None

        def cancel_subscription_renewal(self, *, expected_plan):
            assert expected_plan == "team"
            self.last_subscription = SubscriptionInfo(
                active=True, raw_kind="business",
                started_at=datetime(2026, 11, 16, 0, 49, 43, tzinfo=timezone.utc),
                expires_at=datetime(2026, 12, 16, 6, 49, 43, tzinfo=timezone.utc),
            )
            return {"will_renew": False, "already_cancelled": False}

        def close(self):
            pass

    monkeypatch.setattr("gss_api.routers.orders._team_client", lambda *args: SubscriptionClient())
    store.view(order_id).session_email = "team-account@example.invalid"
    store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
    result = client.post(f"/api/orders/{order_id}/team/poll")
    assert result.status_code == 200, result.text
    subscription = result.json()["subscription"]
    assert subscription["started_at"] == "2026-11-16T00:49:43+00:00"
    assert subscription["expires_at"] == "2026-12-16T06:49:43+00:00"
    assert result.json()["session_email"] == "team-account@example.invalid"
    assert vault.get(order_id) is None
    assert client.get(f"/api/orders/{order_id}").json()["subscription"] == subscription


def test_team_prepare_poll_and_cancellation_share_rotated_credentials(setup_team, monkeypatch):
    from gss_api.routers import orders

    client, order_id, _ = setup_team
    original_hosted = orders.TeamHostedClient
    received = []

    class StatefulClient:
        access_token = "fixture-access"
        extra_cookies = {}
        def __init__(self, cookie):
            self.session_token = cookie
        def close(self):
            pass
        def cancel_subscription_renewal(self, *, expected_plan):
            assert expected_plan == "team" and self.session_token == "poll-cookie"
            return {"will_renew": False, "already_cancelled": False}

    def factory(secrets, country=""):
        try:
            cookie = json.loads(secrets.session)["sessionToken"]
        except ValueError:
            cookie = "original-cookie"
        received.append(cookie)
        return StatefulClient(cookie)

    class Hosted(original_hosted):
        def __init__(self, upstream):
            self.upstream = upstream
        def prepare(self, **kwargs):
            self.upstream.session_token = "prepared-cookie"
            return super().prepare(**kwargs)
        def poll(self, **kwargs):
            self.upstream.session_token = "poll-cookie"
            return super().poll(**kwargs)

    monkeypatch.setattr(orders, "_team_client", factory)
    monkeypatch.setattr(orders, "TeamHostedClient", Hosted)
    assert client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING}).status_code == 200
    assert json.loads(vault.get(order_id).session)["sessionToken"] == "prepared-cookie"
    store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
    result = client.post(f"/api/orders/{order_id}/team/poll")
    assert result.status_code == 200, result.text
    assert result.json()["payment_confirmed"] is True
    assert result.json()["renewal"]["will_renew"] is False
    assert received == ["original-cookie", "prepared-cookie", "poll-cookie"]


def test_team_protocol_reuses_the_checkout_key_and_supports_direct_retry(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    view = store.view(order_id)
    view.checkout_url = "https://pay.openai.com/c/pay/cs_test_fixture"
    secrets = vault.get(order_id)
    secrets.proxy = "http://fixed.invalid:3000"
    routes = []

    def pay(engine, checkout_id):
        assert checkout_id == "cs_test_fixture"
        assert engine.publishable_key == view.publishable_key
        assert engine.allow_proxy_retry is False
        routes.append(engine.proxy)
        state = "card_declined" if len(routes) == 1 else "paid"
        return PaymentOutcome(state=state, intent_status="succeeded" if state == "paid" else "requires_payment_method")

    monkeypatch.setattr(StripeProtocol, "pay", pay)
    payload = {"payment_method_id": "pm_fixture", "billing": BILLING}
    failed = client.post(f"/api/orders/{order_id}/pay-protocol", json=payload)
    assert failed.status_code == 200, failed.text
    assert failed.json()["order"]["status"] == "awaiting_payment"
    assert failed.json()["payment_confirmed"] is False
    paid = client.post(f"/api/orders/{order_id}/pay-protocol", json={**payload, "stripe_direct": True})
    assert paid.status_code == 200, paid.text
    assert paid.json()["order"]["status"] == "delivered"
    assert paid.json()["payment_confirmed"] is True
    assert paid.json()["order"]["renewal"]["will_renew"] is False
    assert routes == ["http://fixed.invalid:3000", None]


def test_team_protocol_transport_failure_is_queried_without_payment_replay(setup_team, monkeypatch):
    client, order_id, calls = setup_team
    store.view(order_id).checkout_url = "https://pay.openai.com/c/pay/cs_test_fixture"
    attempts = []

    def pay(engine, checkout_id):
        attempts.append(checkout_id)
        raise RuntimeError("SSL EOF after payment submission")

    monkeypatch.setattr(StripeProtocol, "pay", pay)
    payload = {"payment_method_id": "pm_fixture", "billing": BILLING}
    response = client.post(f"/api/orders/{order_id}/pay-protocol", json=payload)
    assert response.status_code == 502
    assert store.get_order(order_id).status is OrderStatus.UNKNOWN
    assert client.post(f"/api/orders/{order_id}/pay-protocol", json=payload).status_code == 409
    polled = client.post(f"/api/orders/{order_id}/team/poll")
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "delivered"
    assert polled.json()["renewal"]["will_renew"] is False
    assert attempts == ["cs_test_fixture"]
    assert calls[0][0] == "GET-poll"


def test_team_protocol_requotes_billing_and_requires_the_updated_amount(setup_team, monkeypatch):
    from gss_api.routers import orders
    client, order_id, _ = setup_team
    store.view(order_id).checkout_url = "https://pay.openai.com/c/pay/cs_test_fixture"
    calls = []

    class Hosted:
        def __init__(self, upstream):
            pass
        def quote(self, **kwargs):
            assert kwargs["billing"]["country"] == "US"
            calls.append("quote")
            return {"currency": "egp", "amount_minor": 115000}

    def pay(engine, checkout_id):
        calls.append("pay")
        return PaymentOutcome(state="paid", intent_status="succeeded")

    monkeypatch.setattr(orders, "TeamHostedClient", Hosted)
    monkeypatch.setattr(StripeProtocol, "pay", pay)
    payload = {"payment_method_id": "pm_fixture", "billing": BILLING,
               "expected_amount": "50", "expected_currency": "EGP"}
    changed = client.post(f"/api/orders/{order_id}/pay-protocol", json=payload)
    assert changed.status_code == 409, changed.text
    assert store.get_order(order_id).status is OrderStatus.AWAITING_PAYMENT
    assert store.view(order_id).amount == "1150"
    assert calls == ["quote"]
    confirmed = client.post(f"/api/orders/{order_id}/pay-protocol", json={**payload, "expected_amount": "1150"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["order"]["status"] == "delivered"
    assert calls == ["quote", "quote", "pay"]


def test_team_protocol_finishes_after_the_browser_request_disconnects(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    from gss_api.routers import orders
    store.view(order_id).checkout_url = "https://pay.openai.com/c/pay/cs_test_fixture"
    entered, release = Event(), Event()

    def pay(engine, checkout_id):
        entered.set()
        assert release.wait(timeout=5)
        return PaymentOutcome(state="paid", intent_status="succeeded")

    monkeypatch.setattr(StripeProtocol, "pay", pay)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as api:
            request = asyncio.create_task(api.post(f"/api/orders/{order_id}/pay-protocol", json={
                "payment_method_id": "pm_fixture", "billing": BILLING,
            }))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                operation = orders._protocol_payment_tasks[order_id]
                assert not operation.done()
                pending = await api.post(f"/api/orders/{order_id}/team/poll")
                assert pending.json()["status"] == "paying"
                cleared = await api.request("DELETE", "/api/memory-orders", json={"order_ids": [order_id]})
                assert cleared.json()["skipped"][0]["id"] == order_id
                release.set()
                await operation
                completed = (await api.get(f"/api/orders/{order_id}")).json()
                assert completed["status"] == "delivered"
                assert completed["renewal"]["will_renew"] is False
                assert order_id not in orders._protocol_payment_tasks
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(exercise())


def test_client_paid_flag_never_overrides_team_poll(setup_team, monkeypatch):
    client, order_id, calls = setup_team
    class Pending:
        def __init__(self, client): pass
        def poll(self, **kw): return {"state": "active", "payment_object_status": "processing"}
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Pending)
    r = client.post(f"/api/orders/{order_id}/verify?paid=true")
    assert r.status_code == 200
    assert r.json()["status"] != "delivered"


def test_missing_context_cannot_submit_confirm(setup_team):
    client, order_id, _ = setup_team
    r = client.post(f"/api/orders/{order_id}/pay", json={"payment_method_token": "pm_fixture", "billing": BILLING})
    assert r.status_code == 409
    assert store.get_order(order_id).status is OrderStatus.AWAITING_PAYMENT


def test_mutated_billing_requires_prepare_again(setup_team):
    client, order_id, _ = setup_team
    client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})
    r = client.post(f"/api/orders/{order_id}/pay", json={"payment_method_token": "pm_fixture", "team_context_id": "fixture-context",
                                                     "billing": {**BILLING, "city": "Changed"}})
    assert r.status_code == 409


def test_duplicate_confirm_is_rejected_after_submission(setup_team):
    client, order_id, _ = setup_team
    store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
    r = client.post(f"/api/orders/{order_id}/pay", json={"payment_method_token": "pm_fixture"})
    assert r.status_code == 409


def test_team_successful_confirm_is_idempotent_and_does_not_verify_subscription(setup_team, monkeypatch):
    from gss_contracts import PaymentResult, PaymentStatus
    from gss_api.runtime import build_pipeline
    client, order_id, _ = setup_team
    client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})
    # Use a captured-result stub only for the mutation; subscription must not be queried for Team success.
    class Payment:
        async def pay(self, request):
            assert request.team_context["checkout_config_id"] == "fixture-config-updated"
            assert request.passive_captcha_token == "fixture-captcha"
            return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref="cs_test_fixture")
    class Account:
        async def subscription_status(self, session):
            pytest.fail("Team charge success must not wait on ChatGPT subscription")
    from gss_orchestrator.pipeline import OrderPipeline, PipelineDeps
    pipeline = OrderPipeline(PipelineDeps(checkout=None, payment=Payment(), account=Account()))
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: pipeline)
    body = {"payment_method_token": "pm_fixture", "team_context_id": "fixture-context", "billing": BILLING,
            "passive_captcha_token": "fixture-captcha"}
    r = client.post(f"/api/orders/{order_id}/pay", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "delivered"
    assert client.post(f"/api/orders/{order_id}/pay", json=body).json()["status"] == "delivered"


def test_team_challenge_then_pending_poll_preserves_secret(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    view = store.view(order_id)
    view.next_action = {"type": "use_stripe_sdk", "client_secret": "pi_fixture_secret_fixture"}
    store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
    class Pending:
        def __init__(self, client): pass
        def poll(self, **kw): return {"payment_object_status": "requires_action", "state": "active"}
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Pending)
    r = client.post(f"/api/orders/{order_id}/team/poll")
    assert r.status_code == 200
    assert r.json()["status"] == "requires_action"
    assert r.json()["next_action"]["client_secret"] == "pi_fixture_secret_fixture"


def test_quote_failure_keeps_order_awaiting_and_reports_no_payment(setup_team, monkeypatch):
    from gss_contracts.errors import DomainError, ErrorCode
    client, order_id, _ = setup_team
    class FailedQuote:
        def __init__(self, client): pass
        def quote(self, **kw):
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "Stripe 初始化结账失败：订单代理连接超时；本步骤未提交付款确认")
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", FailedQuote)
    r = client.post(f"/api/orders/{order_id}/team/quote", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_payment"
    assert "未提交付款确认" in r.json()["quote_error"]


def test_quote_amount_does_not_require_full_prepare(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    class QuoteOnly:
        def __init__(self, client): pass
        def quote(self, **kw): return {"currency": "egp", "amount_minor": 115000}
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", QuoteOnly)
    r = client.post(f"/api/orders/{order_id}/team/quote", json={"billing": BILLING})
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == "1150" and r.json()["currency"] == "EGP"
    assert r.json()["status"] == "awaiting_payment"


def test_partial_prepare_quote_survives_later_error(setup_team, monkeypatch):
    from gss_contracts.errors import DomainError, ErrorCode
    client, order_id, _ = setup_team
    class LaterFailure:
        def __init__(self, client): pass
        def prepare(self, on_quote, **kw):
            on_quote({"currency": "egp", "amount_minor": 115000})
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "组件初始化失败，本步骤未提交付款确认")
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", LaterFailure)
    r = client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})
    assert r.status_code == 400
    got = client.get(f"/api/orders/{order_id}").json()
    assert got["amount"] == "1150" and got["status"] == "awaiting_payment"
    assert "未提交付款确认" in got["last_error"]


def test_server_side_card_is_tokenized_then_confirmed(setup_team, monkeypatch):
    from gss_contracts import PaymentResult, PaymentStatus
    from gss_orchestrator.pipeline import OrderPipeline, PipelineDeps
    client, order_id, calls = setup_team
    assert client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING}).status_code == 200
    tokenized = []

    class Hosted:
        def __init__(self, client): pass
        def tokenize(self, **kw):
            tokenized.append(kw)
            return "pm_server_fixture"

    class Payment:
        async def pay(self, request):
            assert request.payment_method_token == "pm_server_fixture"
            return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref="cs_test_fixture")

    class Account:
        async def subscription_status(self, session):
            raise AssertionError("Team success must not wait on subscription")

    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Hosted)
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline",
                        lambda: OrderPipeline(PipelineDeps(checkout=None, payment=Payment(), account=Account())))
    body = {"billing": BILLING, "team_context_id": "fixture-context",
            "card": {"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2030"}}  # release-check: ignore
    r = client.post(f"/api/orders/{order_id}/pay", json=body)
    assert r.status_code == 200, r.text
    assert tokenized and tokenized[0]["publishable_key"].startswith("pk_")
    assert tokenized[0]["card"]["number"] == "4242424242424242"  # release-check: ignore
    assert "4242424242424242" not in r.text and "123" not in r.text  # release-check: ignore


def test_card_and_payment_method_are_mutually_exclusive(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})
    body = {"billing": BILLING, "team_context_id": "fixture-context", "payment_method_token": "pm_fixture",
            "card": {"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2030"}}  # release-check: ignore
    assert client.post(f"/api/orders/{order_id}/pay", json=body).status_code == 400


def test_server_card_decline_keeps_order_awaiting_payment(setup_team, monkeypatch):
    from gss_contracts.errors import DomainError, ErrorCode
    client, order_id, _ = setup_team
    client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})

    class Hosted:
        def __init__(self, client): pass
        def tokenize(self, **kw):
            raise DomainError(ErrorCode.PAYMENT_DECLINED, "Your card was declined (card_declined)")

    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Hosted)
    body = {"billing": BILLING, "team_context_id": "fixture-context",
            # Public Stripe decline-test PAN, not a real card.
            "card": {"number": "4000000000000002", "cvc": "123", "exp_month": "12", "exp_year": "2030"}}  # release-check: ignore
    r = client.post(f"/api/orders/{order_id}/pay", json=body)
    assert r.status_code >= 400
    assert store.get_order(order_id).status is not OrderStatus.PAYING
