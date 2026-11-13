"""Session inspection is read-only and keeps renewed credentials in the response."""
import base64
import json
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_core.chatgpt import ChatGPTClient


def access_token(email="account@example.test", issued_at=100):
    claims = {
        "iat": issued_at,
        "https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account"},
        "https://api.openai.com/profile": {"email": email},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{encoded}.fixture"


@pytest.fixture
def inspect_api(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.setattr("gss_api.routers.sessions.configured_proxy", lambda role: "http://fixture.invalid:8080")
    original = ChatGPTClient.from_raw_session
    store.clear()

    def setup(payloads):
        pending = deque(payloads)
        calls = []
        closed = []

        class Transport:
            def __init__(self):
                self.headers = {}
                self.cookies = {}

            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                payload, status = pending.popleft()
                return SimpleNamespace(status_code=status, json=lambda: payload)

            def post(self, *args, **kwargs):
                raise AssertionError("Session inspection must use GET requests")

            def close(self):
                closed.append(True)

        def build(raw, proxy=None):
            assert proxy == "http://fixture.invalid:8080"
            client = original(raw, proxy=proxy)
            client._http = Transport()
            return client

        monkeypatch.setattr(ChatGPTClient, "from_raw_session", staticmethod(build))
        return TestClient(create_app()), calls, closed

    yield setup
    store.clear()


def test_inspection_recovers_an_expired_token_and_returns_account_details(inspect_api):
    fresh = access_token("renewed@example.test", 200)
    client, calls, closed = inspect_api([
        ({"detail": {"code": "token_expired"}}, 401),
        ({"accessToken": fresh, "sessionToken": "renewed-cookie"}, 200),
        ({"plan_type": "plus", "is_active": True, "will_renew": False,
          "entitlement": {"expires_at": "2026-12-09T00:00:00+00:00"}}, 200),
    ])
    response = client.post("/api/sessions/check", json={"session": json.dumps({
        "accessToken": access_token(), "sessionToken": "original-cookie",
    })})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["valid"] is True and data["active"] is True
    assert data["email"] == "renewed@example.test" and data["plan_type"] == "plus"
    assert data["will_renew"] is False
    assert data["expires_at"] == "2026-12-09T00:00:00+00:00"
    snapshot = json.loads(data["session"])
    assert snapshot["accessToken"] == fresh and snapshot["sessionToken"] == "renewed-cookie"
    assert snapshot["email"] == data["email"]
    assert calls[1][1]["params"]["refresh"] == "true"
    assert len(calls) == 3 and closed == [True]
    assert store.orders == {}


@pytest.mark.parametrize("payload,plan,active", [
    ({"plan_type": "free", "is_active": False}, "free", False),
    ({"entitlement": {"subscription_plan": "chatgptgoplan", "has_active_subscription": True}}, "go", True),
    ({"plan_type": "chatgptproplan", "is_active": False}, "pro", False),
    ({"plan": {"type": "chatgptprolite"}, "last_active_subscription": {"will_renew": False}}, "pro_lite", True),
])
def test_inspection_supports_free_and_legacy_membership_shapes(inspect_api, payload, plan, active):
    client, calls, closed = inspect_api([(payload, 200)])
    response = client.post("/api/sessions/check", json={"session": access_token()})
    assert response.status_code == 200, response.text
    assert response.json()["plan_type"] == plan and response.json()["active"] is active
    assert response.json()["email"] == "account@example.test"
    assert len(calls) == 1 and closed == [True]


def test_rejected_session_does_not_claim_validity(inspect_api):
    client, calls, closed = inspect_api([({"detail": {"code": "token_expired"}}, 401)])
    response = client.post("/api/sessions/check", json={"session": access_token()})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_session"
    assert len(calls) == 1 and closed == [True]


def test_missing_subscription_is_a_valid_free_account_with_email(inspect_api):
    client, calls, closed = inspect_api([({"detail": "No subscription found for account"}, 404)])
    response = client.post("/api/sessions/check", json={"session": access_token()})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["valid"] is True and data["plan_type"] == "free" and data["active"] is False
    assert data["email"] == "account@example.test"
    assert data["expires_at"] is None and data["will_renew"] is None
    assert json.loads(data["session"])["email"] == data["email"]
    assert len(calls) == 1 and closed == [True] and store.orders == {}


def test_missing_subscription_after_token_recovery_uses_the_refreshed_email(inspect_api):
    fresh = access_token("renewed@example.test", 200)
    client, calls, closed = inspect_api([
        ({"detail": {"code": "token_expired"}}, 401),
        ({"accessToken": fresh, "sessionToken": "renewed-cookie"}, 200),
        ({"detail": "No subscription found for account"}, 404),
    ])
    response = client.post("/api/sessions/check", json={"session": json.dumps({
        "accessToken": access_token(), "sessionToken": "original-cookie",
    })})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["valid"] is True and data["plan_type"] == "free" and data["active"] is False
    assert data["email"] == "renewed@example.test"
    snapshot = json.loads(data["session"])
    assert snapshot["accessToken"] == fresh and snapshot["sessionToken"] == "renewed-cookie"
    assert len(calls) == 3 and closed == [True]


@pytest.mark.parametrize("payload,status", [
    ({"detail": "Not Found"}, 404),
    ({"detail": "No subscription found for account"}, 500),
    ({"detail": "No subscription found for account"}, 403),
])
def test_other_upstream_errors_do_not_claim_a_valid_free_account(inspect_api, payload, status):
    client, calls, closed = inspect_api([(payload, status)])
    response = client.post("/api/sessions/check", json={"session": access_token()})
    assert response.status_code != 200
    assert len(calls) == 1 and closed == [True]


def test_empty_membership_response_does_not_claim_validity(inspect_api):
    client, _, closed = inspect_api([({}, 200)])
    response = client.post("/api/sessions/check", json={"session": access_token()})
    assert response.status_code != 200
    assert closed == [True]


def test_malformed_session_is_rejected_before_any_upstream_request(inspect_api):
    client, calls, closed = inspect_api([])
    response = client.post("/api/sessions/check", json={"session": "{bad json"})
    assert response.status_code == 401 and calls == [] and closed == []


def test_offline_mode_does_not_make_a_real_session_check(inspect_api, monkeypatch):
    client, calls, _ = inspect_api([])
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    response = client.post("/api/sessions/check", json={"session": access_token()})
    assert response.status_code == 409 and calls == []
