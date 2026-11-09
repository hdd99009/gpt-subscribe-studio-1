"""Team prepare -> pm -> confirm -> challenge -> server GET poll, without network."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_core.vault import vault
from gss_contracts import OrderStatus

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
