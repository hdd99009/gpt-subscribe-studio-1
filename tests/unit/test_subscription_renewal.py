"""Renewal cancellation uses scripted transports and never contacts an account."""
from collections import deque
from types import SimpleNamespace

import pytest

from gss_contracts.errors import DomainError
from gss_core.chatgpt import APP_BASE, ChatGPTClient
from gss_core.saved_cards import session_snapshot
from gss_core.session_token import SESSION_COOKIE_CHUNK_SIZE, SESSION_COOKIE_NAME, session_cookie_values


@pytest.fixture
def client_factory(monkeypatch):
    monkeypatch.setattr("gss_core.chatgpt.time.sleep", lambda seconds: None)

    def create(payloads, *, post_error=None, post_status=200):
        remaining = deque(payloads)
        statuses = deque(post_status if isinstance(post_status, list) else [post_status])
        calls = []

        class Transport:
            headers = {}
            def __init__(self):
                self.cookies = {"__Secure-next-auth.session-token": "rotated-fixture-cookie"}

            def get(self, url, **kwargs):
                calls.append(("GET", url, kwargs))
                payload = remaining.popleft()
                if isinstance(payload, Exception):
                    raise payload
                status = 200
                if isinstance(payload, tuple):
                    payload, status = payload
                return SimpleNamespace(status_code=status, json=lambda: payload)

            def post(self, url, **kwargs):
                calls.append(("POST", url, kwargs))
                if post_error:
                    raise post_error
                status = statuses.popleft() if len(statuses) > 1 else statuses[0]
                return SimpleNamespace(status_code=status, json=lambda: {"returned_amount": None})

        client = ChatGPTClient(access_token="fixture-access", account_id="fixture-account", _http=Transport())
        return client, calls

    return create


def subscription(will_renew):
    return {"plan_type": "plus", "is_active": True, "will_renew": will_renew}


def test_cancels_once_and_requires_subscription_readback(client_factory):
    client, calls = client_factory([subscription(True), subscription(False)])
    result = client.cancel_subscription_renewal(expected_plan="plus")
    assert result == {"will_renew": False, "already_cancelled": False}
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]
    assert calls[1][1] == APP_BASE + "/backend-api/subscriptions/cancel"
    assert calls[1][2]["json"] == {"account_id": "fixture-account"}
    assert calls[2][2]["params"] == {"account_id": "fixture-account"}
    headers = calls[1][2]["headers"]
    assert headers["Authorization"] == "Bearer fixture-access"
    assert headers["chatgpt-account-id"] == "fixture-account"
    assert headers["Content-Type"] == "application/json"
    assert headers["originator"] == "Codex Browser"


def test_already_cancelled_is_a_read_only_success(client_factory):
    client, calls = client_factory([subscription(False)])
    assert client.cancel_subscription_renewal(expected_plan="plus")["already_cancelled"] is True
    assert len(calls) == 1 and calls[0][0] == "GET"


@pytest.mark.parametrize("payload", [
    {"plan_type": "team", "will_renew": True},
    {"plan_type": "plus"},
    {"plan_type": "plus", "will_renew": "false"},
])
def test_ambiguous_or_foreign_subscription_prevents_cancellation(client_factory, payload):
    client, calls = client_factory([payload])
    with pytest.raises(DomainError):
        client.cancel_subscription_renewal(expected_plan="plus")
    assert [call[0] for call in calls] == ["GET"]


def test_lost_response_can_still_be_confirmed_without_a_second_post(client_factory):
    client, calls = client_factory([subscription(True), subscription(False)], post_error=TimeoutError())
    assert client.cancel_subscription_renewal(expected_plan="plus")["will_renew"] is False
    assert [call[0] for call in calls].count("POST") == 1


def test_accepted_but_unverified_request_does_not_claim_cancellation(client_factory):
    client, calls = client_factory([subscription(True)] * 4)
    with pytest.raises(DomainError, match="尚未确认"):
        client.cancel_subscription_renewal(expected_plan="plus")
    assert [call[0] for call in calls] == ["GET", "POST", "GET", "GET", "GET"]


def test_followup_lookup_recovers_from_transient_errors(client_factory):
    client, calls = client_factory([subscription(True), TimeoutError(), subscription(False)])
    assert client.cancel_subscription_renewal(expected_plan="plus")["will_renew"] is False
    assert [call[0] for call in calls].count("POST") == 1


def test_older_subscription_shape_retains_renewal_information(client_factory):
    client, calls = client_factory([{
        "entitlement": {"subscription_plan": "chatgptplusplan", "has_active_subscription": True},
        "last_active_subscription": {"will_renew": False},
    }])
    assert client.cancel_subscription_renewal(expected_plan="plus")["already_cancelled"] is True
    assert len(calls) == 1


def test_subscription_query_refreshes_a_rejected_token_and_keeps_rotated_cookie(client_factory):
    client, calls = client_factory([({}, 401), {"accessToken": "fresh-fixture-access"}, subscription(False)])
    client.session_token = "original-fixture-cookie"
    assert client.subscription_payload()["will_renew"] is False
    assert [call[1] for call in calls] == [APP_BASE + "/backend-api/subscriptions",
        APP_BASE + "/api/auth/session", APP_BASE + "/backend-api/subscriptions"]
    assert client.access_token == "fresh-fixture-access"
    assert client.session_token == "rotated-fixture-cookie"
    assert client.http.headers["Authorization"] == "Bearer fresh-fixture-access"
    assert calls[1][2]["params"] == {
        "refresh": "true", "reason": "token_expired", "method": "GET", "path": "/backend-api/subscriptions",
    }


def test_rejected_cancellation_auth_is_refreshed_before_the_accepted_request(client_factory):
    client, calls = client_factory([subscription(True), {"accessToken": "fresh-fixture-access"}, subscription(False)],
                                   post_status=[401, 200])
    client.session_token = "original-fixture-cookie"
    assert client.cancel_subscription_renewal(expected_plan="plus")["will_renew"] is False
    assert [call[0] for call in calls] == ["GET", "POST", "GET", "POST", "GET"]
    assert calls[2][2]["params"] == {
        "refresh": "true", "reason": "token_expired", "method": "POST", "path": "/backend-api/subscriptions/cancel",
    }


def test_activation_delay_is_rechecked_before_cancelling_renewal(client_factory):
    client, calls = client_factory([{**subscription(True), "is_active": False}, subscription(True), subscription(False)])
    assert client.cancel_subscription_renewal(expected_plan="plus")["will_renew"] is False
    assert [call[0] for call in calls] == ["GET", "GET", "POST", "GET"]


def test_chunked_session_refresh_is_used_for_cancellation_and_readback(client_factory):
    original_session = "a" * SESSION_COOKIE_CHUNK_SIZE + "old-tail"
    renewed_session = "b" * SESSION_COOKIE_CHUNK_SIZE + "new-tail"
    client, calls = client_factory([
        ({}, 401),
        {"accessToken": "fresh-fixture-access", "sessionToken": renewed_session},
        subscription(True),
        {**subscription(False), "cancellation_outcome": "deactivate"},
    ])
    client.session_token = original_session
    client.extra_cookies = session_cookie_values(original_session)
    client.http.cookies = dict(client.extra_cookies)
    client.sentinel_token = "old-checkout-sentinel"
    client.sentinel_so = "old-checkout-so"

    assert client.cancel_subscription_renewal(expected_plan="plus") == {
        "will_renew": False, "already_cancelled": False,
    }
    assert [call[0] for call in calls] == ["GET", "GET", "GET", "POST", "GET"]
    for method, url, kwargs in calls[2:]:
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer fresh-fixture-access"
        cookies = dict(part.split("=", 1) for part in headers["Cookie"].split("; "))
        assert cookies == session_cookie_values(renewed_session)
        assert SESSION_COOKIE_NAME not in cookies
        assert not any("sentinel" in name.lower() for name in headers)
    restored = ChatGPTClient.from_raw_session(session_snapshot(client))
    assert restored.session_token == renewed_session
    assert restored.extra_cookies == session_cookie_values(renewed_session)


def test_unchanged_rejected_token_stops_before_cancellation(client_factory):
    client, calls = client_factory([
        ({"detail": {"code": "token_expired"}}, 401),
        {"accessToken": "fixture-access"},
    ])
    client.session_token = "fixture-session"
    with pytest.raises(DomainError):
        client.cancel_subscription_renewal(expected_plan="plus")
    assert [call[0] for call in calls] == ["GET", "GET"]
