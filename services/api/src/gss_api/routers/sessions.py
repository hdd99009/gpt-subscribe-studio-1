"""Read-only account inspection before creating a checkout."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient, _PLAN_TYPE
from gss_core.config import use_fake_adapters
from gss_core.proxies import configured_proxy
from gss_core.saved_cards import session_snapshot
from gss_core.session_token import email_from_access_token, parse_session_bundle

from ..schemas import ErrorOut
from .orders import _from_domain

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionCheckRequest(BaseModel):
    session: str = Field(min_length=1, repr=False)


class SessionCheckOut(BaseModel):
    valid: bool = True
    email: str | None = None
    plan_type: str
    active: bool
    expires_at: str | None = None
    will_renew: bool | None = None
    session: str = Field(repr=False)


def _inspect_session(raw: str) -> SessionCheckOut:
    bundle = parse_session_bundle(raw)
    client = ChatGPTClient.from_raw_session(raw, proxy=configured_proxy("checkout"))
    client.timeout = 15.0
    try:
        if not client.access_token and not client.refresh_session():
            raise DomainError(ErrorCode.INVALID_SESSION, "无法从当前会话取得访问令牌")
        payload = client.subscription_payload(allow_missing=True)
        entitlement = payload.get("entitlement") or {}
        subscription = payload.get("subscription") or {}
        legacy_plan = payload.get("plan") or {}
        plan = str(
            payload.get("plan_type") or entitlement.get("subscription_plan")
            or subscription.get("plan") or legacy_plan.get("type") or ""
        ).lower()
        if not plan:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "查询未返回可核实的会员状态，请稍后重试")
        plan = plan.removeprefix("chatgpt").removesuffix("plan")
        plan = _PLAN_TYPE.get(plan, plan)
        plan = {
            "prolite": "pro_lite", "promax": "pro_max",
        }.get(plan, plan)
        active = payload.get("is_active")
        if not isinstance(active, bool):
            active = entitlement.get("has_active_subscription")
        if not isinstance(active, bool):
            active = (
                plan in _PLAN_TYPE or plan in {"business", "enterprise"}
                or subscription.get("status") in {"active", "trialing", "paid"}
            )
        expires = (
            entitlement.get("expires_at") or subscription.get("expires_at")
            or payload.get("expires_at") or payload.get("active_until")
        )
        will_renew = payload.get("will_renew")
        if will_renew is None:
            will_renew = (payload.get("last_active_subscription") or {}).get("will_renew")
        email = email_from_access_token(client.access_token) or bundle.email
        snapshot = json.loads(session_snapshot(client))
        snapshot["email"] = email
        return SessionCheckOut(
            email=email or None, plan_type=plan, active=active,
            expires_at=expires if isinstance(expires, str) else None,
            will_renew=will_renew if isinstance(will_renew, bool) else None,
            session=json.dumps(snapshot),
        )
    finally:
        client.close()


@router.post("/check", response_model=SessionCheckOut)
async def check_session(payload: SessionCheckRequest) -> SessionCheckOut:
    if use_fake_adapters():
        raise HTTPException(409, "离线模式无法检测真实 Session，请启用真实适配器")
    try:
        return await asyncio.to_thread(_inspect_session, payload.session.strip())
    except ValueError as exc:
        raise HTTPException(401, detail=ErrorOut(code=str(ErrorCode.INVALID_SESSION), message=str(exc)).model_dump())
    except DomainError as exc:
        raise _from_domain(exc)
