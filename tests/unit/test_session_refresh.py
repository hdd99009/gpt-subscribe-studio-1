"""session 刷新对齐：用账单国出口请求 /api/auth/session，换取与本次 IP 同批次的凭证。

结账地区由出口代理决定，session 本身不绑定国家；刷新是风控缓解措施，
必须软失败（不阻断结账/支付）。
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from gss_checkout import UpstreamCheckoutAdapter
from gss_contracts import BillingMode, CheckoutRequest, PaymentRequest, Plan
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.session_token import SESSION_COOKIE_CHUNK_SIZE, SESSION_COOKIE_NAME
from gss_payment.adapter import UpstreamPaymentAdapter

from test_chatgpt_protocol import _jwt


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


class _SessionHttp:
    """可编程的 /api/auth/session 桩。"""

    def __init__(self, *, payload: dict | None = None, status: int = 200, raises: bool = False) -> None:
        self.headers: dict = {}
        self.cookies: dict = {"oai-sc": "sc-from-refresh"}
        self.posts: list = []
        self.gets: list = []
        self.get_headers: list = []
        self.get_params: list = []
        self._payload = payload or {}
        self._status = status
        self._raises = raises

    def get(self, url: str, **kwargs):
        if self._raises:
            raise RuntimeError("network down")
        self.gets.append(url)
        self.get_headers.append(kwargs.get("headers", {}))
        self.get_params.append(kwargs.get("params"))
        return _FakeResponse(status_code=self._status, payload=self._payload)

    def post(self, url: str, **kwargs):
        self.posts.append({"url": url})
        return _FakeResponse(payload={})


def _client_with_session_token() -> ChatGPTClient:
    client = ChatGPTClient.from_raw_session(_jwt("acct-refresh"))
    client.session_token = "sess-token"
    return client


def test_refresh_session_updates_token_and_cookies() -> None:
    client = _client_with_session_token()
    client._http = _SessionHttp(payload={"accessToken": _jwt("acct-refreshed")})

    assert client.refresh_session() is True
    assert client.access_token == _jwt("acct-refreshed")
    assert client.extra_cookies["oai-sc"] == "sc-from-refresh"
    assert any("/api/auth/session" in url for url in client._http.gets)


def test_refresh_session_soft_fails_on_network_error() -> None:
    client = _client_with_session_token()
    original = client.access_token
    client._http = _SessionHttp(raises=True)

    assert client.refresh_session() is False
    assert client.access_token == original


def test_refresh_session_soft_fails_on_upstream_error() -> None:
    client = _client_with_session_token()
    original = client.access_token
    client._http = _SessionHttp(status=500)

    assert client.refresh_session() is False
    assert client.access_token == original


def test_refresh_session_ignores_expired_session_token() -> None:
    """session-token 失效时上游返回 200 + 空 accessToken，不能拿空 token 覆盖。"""

    client = _client_with_session_token()
    original = client.access_token
    client._http = _SessionHttp(payload={})

    assert client.refresh_session() is False
    assert client.access_token == original


def test_refresh_session_without_session_token_is_noop() -> None:
    client = ChatGPTClient.from_raw_session(_jwt("acct-refresh"))
    client._http = _SessionHttp(payload={"accessToken": _jwt("acct-other")})

    assert client.refresh_session() is False
    assert client.access_token == _jwt("acct-refresh")
    assert client._http.gets == []


def test_refresh_keeps_the_session_token_returned_in_the_payload() -> None:
    client = _client_with_session_token()
    client._http = _SessionHttp(payload={"accessToken": _jwt("acct-refresh"), "sessionToken": "rotated-session"})
    assert client.refresh_session() is True
    assert client.session_token == "rotated-session"
    assert client.http.cookies[SESSION_COOKIE_NAME] == "rotated-session"
    assert client.http.get_headers[0]["Authorization"] is None
    assert client.http.get_headers[0]["chatgpt-account-id"] is None


def test_expired_token_recovery_matches_browser_request() -> None:
    def token(issued_at: int) -> str:
        payload = {"iat": issued_at, "https://api.openai.com/auth": {"chatgpt_account_id": "acct-refresh"}}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJub25lIn0.{encoded}.fixture"

    client = _client_with_session_token()
    client.access_token = token(100)
    renewed = token(200)
    client._http = _SessionHttp(payload={"accessToken": renewed, "sessionToken": "renewed-cookie"})

    assert client.refresh_session(failed_path="/backend-api/subscriptions") is True
    assert client.http.get_params == [{
        "refresh": "true", "reason": "token_expired", "method": "GET", "path": "/backend-api/subscriptions",
    }]
    headers = client.http.get_headers[0]
    assert headers["Authorization"] is None
    assert headers["chatgpt-account-id"] is None
    assert headers["x-openai-failed-access-token-iat"] == "100"
    assert headers["x-codex-auth-recovery-document"] == client.oai_session_id
    assert headers["x-codex-auth-recovery-attempt"]
    assert headers["Cache-Control"] == headers["Pragma"] == "no-cache"
    assert client.access_token == renewed
    assert client.session_token == "renewed-cookie"


def test_recovery_rejects_the_same_failed_access_token() -> None:
    client = _client_with_session_token()
    original = client.access_token
    client._http = _SessionHttp(payload={"accessToken": original})
    assert client.refresh_session(failed_path="/backend-api/subscriptions") is False
    assert client.access_token == original


def test_recovery_rejects_a_different_account_without_replacing_cookies() -> None:
    client = _client_with_session_token()
    original = client.access_token
    client._http = _SessionHttp(payload={"accessToken": _jwt("acct-other"), "sessionToken": "foreign-cookie"})
    assert client.refresh_session(failed_path="/backend-api/subscriptions") is False
    assert client.access_token == original
    assert client.session_token == "sess-token"


def test_routine_refresh_accepts_an_unchanged_valid_token() -> None:
    client = _client_with_session_token()
    client._http = _SessionHttp(payload={"accessToken": client.access_token})
    assert client.refresh_session() is True
    assert client.http.get_params == [None]
    assert "x-openai-failed-access-token-iat" not in client.http.get_headers[0]


@pytest.mark.parametrize("new_token", ["short-session", "a" * SESSION_COOKIE_CHUNK_SIZE + "b" * 614])
def test_response_cookie_rotation_replaces_old_cookie_forms(new_token: str) -> None:
    from curl_cffi.requests import Cookies
    from types import SimpleNamespace
    from gss_core.session_token import session_cookie_values

    client = _client_with_session_token()
    client.extra_cookies = {SESSION_COOKIE_NAME: "old-base", SESSION_COOKIE_NAME + ".2": "old-tail"}
    client._http = _SessionHttp()
    client._http.cookies = Cookies(client.extra_cookies)
    response = SimpleNamespace(cookies=Cookies(session_cookie_values(new_token)))
    client._merge_cookies(response, session_token="payload-session")
    assert client.session_token == new_token
    assert client.extra_cookies == session_cookie_values(new_token)
    assert client.http.cookies.get_dict() == session_cookie_values(new_token)
    assert client._cookie_header() == "; ".join(
        f"{name}={value}" for name, value in {**session_cookie_values(new_token), "oai-did": client.device_id}.items()
    )


class _StubCheckoutClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def refresh_session(self) -> bool:
        self.calls.append("refresh")
        return True

    def create_checkout(self, **kwargs):
        self.calls.append("create")
        from gss_core.chatgpt import CheckoutSession

        return CheckoutSession(
            checkout_session_id="cs_test_1",
            publishable_key="pk_live_test",
            account_id="acct-stub",
            checkout_url="https://chatgpt.com/checkout/openai_llc/cs_test_1",
            currency="USD",
        )

    def close(self) -> None:
        self.calls.append("close")


def test_checkout_adapter_refreshes_session_before_create(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubCheckoutClient()

    class _Factory:
        @staticmethod
        def from_raw_session(raw: str, proxy=None) -> _StubCheckoutClient:
            return stub

    monkeypatch.setattr("gss_checkout.adapter.ChatGPTClient", _Factory)

    result = asyncio.run(
        UpstreamCheckoutAdapter().create_checkout(
            CheckoutRequest(
                session="sess",
                plan=Plan.PLUS,
                billing_country="US",
                billing_mode=BillingMode.STANDARD,
            )
        )
    )

    assert result.checkout_session_id == "cs_test_1"
    # 刷新必须发生在创建结账之前；刷新失败也不阻断结账。
    assert stub.calls.index("refresh") < stub.calls.index("create")


class _StubPaymentClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def refresh_session(self) -> bool:
        self.calls.append("refresh")
        return True

    def warmup(self, *, page_url: str = "") -> None:
        self.calls.append("warmup")

    def confirm_checkout(self, **kwargs):
        self.calls.append("confirm")
        raise DomainError(ErrorCode.PAYMENT_DECLINED, "declined on purpose")


def test_payment_adapter_refreshes_session_before_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubPaymentClient()

    class _Factory:
        @staticmethod
        def from_raw_session(raw: str, proxy=None) -> _StubPaymentClient:
            return stub

    monkeypatch.setattr("gss_payment.adapter.ChatGPTClient", _Factory)

    with pytest.raises(DomainError):
        asyncio.run(
            UpstreamPaymentAdapter().pay(
                PaymentRequest(
                    checkout_url="https://chatgpt.com/checkout/openai_llc/cs_test_1",
                    country="US",
                    payment_method_token="ctoken_1",
                    session="sess",
                    checkout_session_id="cs_test_1",
                    publishable_key="pk_live_test",
                    plan="plus",
                )
            )
        )

    assert stub.calls.index("refresh") < stub.calls.index("warmup") < stub.calls.index("confirm")
