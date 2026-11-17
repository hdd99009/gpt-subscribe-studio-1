"""订阅校验。交付前必须确认订阅已生效。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from gss_contracts import (
    AdapterCapabilities,
    PromoCampaignInfo,
    PromoEligibility,
    SubscriptionInfo,
)
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.proxies import configured_proxy
from gss_core.subscriptions import subscription_info_from_payload


class AccountAdapterBase:
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            source="self-contained chatgpt.com backend-api/subscriptions",
            license="MIT (this repository)",
            supports={
                "account_sources": ["customer_session"],
                "actions": ["subscription_status"],
            },
            failure_modes={
                "invalid_session": "session 无效 → 重新粘贴",
                "subscription_not_active": "订阅未生效 → 确认窗口内重试，不交付",
            },
        )


@dataclass
class UpstreamAccountAdapter(AccountAdapterBase):
    timeout: int = 30

    async def subscription_status(self, session: str) -> SubscriptionInfo:
        if not session:
            raise DomainError(ErrorCode.INVALID_SESSION, "缺少 session，无法查询订阅")
        client = ChatGPTClient.from_raw_session(session, proxy=configured_proxy("checkout"))
        client.timeout = float(self.timeout)
        try:
            payload = client.subscription_payload()
        finally:
            client.close()
        return subscription_info_from_payload(payload)

    async def promo_eligibility(self, session: str) -> PromoEligibility:
        if not session:
            raise DomainError(ErrorCode.INVALID_SESSION, "缺少 session，无法查询优惠资格")
        client = ChatGPTClient.from_raw_session(session, proxy=configured_proxy("checkout"))
        client.timeout = float(self.timeout)
        try:
            result = client.promo_eligibility()
        finally:
            client.close()
        campaigns = [
            PromoCampaignInfo(
                key=str(item.get("key") or ""),
                campaign_id=str(item.get("id") or ""),
                discount_percentage=item.get("discount_percentage"),
                duration_num_periods=item.get("duration_num_periods"),
                duration_period=item.get("duration_period"),
                title=item.get("title"),
                summary=item.get("summary"),
                promotion_type=item.get("promotion_type"),
            )
            for item in result.get("campaigns", [])
            if isinstance(item, dict) and item.get("id")
        ]
        return PromoEligibility(
            account_id=result.get("account_id") or None,
            current_plan_type=result.get("current_plan_type"),
            subscription_plan=result.get("subscription_plan"),
            is_free=bool(result.get("is_free")),
            plus_eligible=bool(result.get("plus_eligible")),
            campaigns=campaigns,
        )

    async def register_account(self) -> Dict[str, Any]:
        raise NotImplementedError("本工具只开通客户自己的账号，不代注册")


@dataclass
class FakeAccountAdapter(AccountAdapterBase):
    active: bool = True
    sessions_seen: Optional[list] = None

    def __post_init__(self) -> None:
        if self.sessions_seen is None:
            self.sessions_seen = []

    async def subscription_status(self, session: str) -> SubscriptionInfo:
        assert self.sessions_seen is not None
        self.sessions_seen.append(session)
        return SubscriptionInfo(
            active=self.active,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=30) if self.active else None),
            channel="web" if self.active else None,
            raw_kind="fake",
        )

    async def promo_eligibility(self, session: str) -> PromoEligibility:
        return PromoEligibility(
            account_id="fake-account",
            current_plan_type="free",
            subscription_plan="chatgptfreeplan",
            is_free=True,
            plus_eligible=True,
            campaigns=[
                PromoCampaignInfo(
                    key="plus",
                    campaign_id="plus-1-month-free",
                    discount_percentage=100,
                    duration_num_periods=1,
                    duration_period="month",
                    title="Try Plus free for 1 month",
                    promotion_type="1-month free trial",
                )
            ],
        )

    async def register_account(self) -> Dict[str, Any]:
        return {"session": "fake-session", "email": "fake@example.invalid"}


__all__ = ["AccountAdapterBase", "UpstreamAccountAdapter", "FakeAccountAdapter"]
