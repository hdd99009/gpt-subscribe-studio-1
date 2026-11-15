"""Team 托管（hosted）cs_ 会话的站内支付路径。

Team 长链也是 Stripe cs_ 会话：站内支付走同一套 payment_pages 确认，
但审批不经 chatgpt.com 后端 —— approve 被拒时必须降级轮询而不是报错。
"""

from __future__ import annotations

import asyncio

import pytest

from gss_contracts import PaymentRequest, PaymentStatus
from gss_contracts.errors import DomainError, ErrorCode
from gss_payment.adapter import UpstreamPaymentAdapter

_REQUIRES_APPROVAL = {
    "id": "cs_live_1",
    "status": "open",
    "submission_attempt": {"state": "requires_approval"},
}

_SETTLED = {
    "id": "cs_live_1",
    "status": "complete",
    "payment_object_status": "succeeded",
    "submission_attempt": {"state": "succeeded"},
    "payment_intent": {"id": "pi_1", "client_secret": "pi_1_secret_x", "status": "succeeded"},
}


class _StubChatGPTClient:
    language = "en-US"
    timezone = "America/New_York"
    timeout = 5.0

    def __init__(self, poll_payload: dict | None = None) -> None:
        self.calls: list[str] = []
        self.poll_payload = poll_payload

    def refresh_session(self) -> bool:
        self.calls.append("refresh")
        return True

    def close(self) -> None:
        self.calls.append("close")

    def warmup(self, *, page_url: str = "") -> None:
        self.calls.append("warmup")

    def confirm_checkout(self, **kwargs):
        self.calls.append("confirm")
        return dict(_REQUIRES_APPROVAL)

    def confirm_team_hosted_checkout(self, **kwargs):
        self.calls.append("team_confirm")
        self.team_confirm_args = kwargs
        return dict(_REQUIRES_APPROVAL)

    def finalize_stripe_custom_checkout(self, **kwargs):
        self.calls.append("finalize")
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "批准结账失败: 403")

    def poll_payment_page(self, **kwargs):
        self.calls.append("poll")
        return dict(self.poll_payload) if self.poll_payload else dict(_REQUIRES_APPROVAL)


def _stub_factory(monkeypatch: pytest.MonkeyPatch, client: _StubChatGPTClient) -> None:
    class _Factory:
        @staticmethod
        def from_raw_session(session: str, proxy: str | None = None) -> _StubChatGPTClient:
            return client

    monkeypatch.setattr("gss_payment.adapter.ChatGPTClient", _Factory)


def _pay(plan: str = "team"):
    return asyncio.run(
        UpstreamPaymentAdapter().pay(
            PaymentRequest(
                checkout_url="https://pay.openai.com/c/pay/cs_live_1",
                country="US",
                payment_method_token="ctoken_1",
                session="sess",
                checkout_session_id="cs_live_1",
                publishable_key="pk_live_test",
                plan=plan,
            )
        )
    )


def test_custom_cs_approve_rejected_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """非托管（plus custom）会话的 approve 失败必须原样抛错，不能吞掉。"""

    client = _StubChatGPTClient()
    _stub_factory(monkeypatch, client)

    with pytest.raises(DomainError):
        _pay(plan="plus")
    assert client.calls[-1] == "close"


# Dedicated Hosted pipeline and missing context tests are in test_hosted_protocol.py.
