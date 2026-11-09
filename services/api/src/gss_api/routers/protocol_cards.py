"""Single-order server-side card setup, checkout refresh, and payment protocol."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from decimal import Decimal, InvalidOperation
from secrets import token_bytes
from threading import Lock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gss_contracts import BillingMode, OrderStatus, Plan
from gss_contracts.errors import ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.config import use_fake_adapters
from gss_core.checkout_protocol import run_checkout_protocol
from gss_core.plus_har_protocol import har_engine_enabled
from gss_core.chatgpt import _processor_from_checkout_url
from gss_core.vault import vault
from gss_core.saved_cards import session_snapshot

from ..schemas import BillingIn, CardIn
from ..store import store
from .events import notify_order_update
from .orders import _cancel_renewal, _error, _mark_delivered, _team_locks, _to_out

router = APIRouter(prefix="/api/orders", tags=["card-protocol"])
logger = logging.getLogger("gss.card-protocol")
_settlement_tasks: dict[str, asyncio.Task] = {}
_operation_tasks: set[asyncio.Task] = set()


class BindProtocolRequest(BaseModel):
    card: CardIn = Field(repr=False)
    billing: BillingIn
    expected_amount: str = Field(min_length=1, max_length=40)
    expected_currency: str = Field(min_length=3, max_length=3)
    stripe_direct: bool = False


def _run(secrets, context, spec, *, card=None, billing=None, read_only=False):
    guard = context["operation_lock"]
    if not guard.acquire(blocking=False):
        return {"state": "pending", "stage": "payment" if context.get("app_confirmation_started") else "checkout",
                "message": "原协议请求仍在处理，请查询协议状态", "intent_status": "processing"}
    try:
        client = ChatGPTClient.from_raw_session(secrets.session, proxy=secrets.proxy or None)
        client.timeout = 45.0
        client.stripe_direct = bool(context.get("stripe_direct", False))
        try:
            try:
                result = run_checkout_protocol(client, context, spec, card=card, billing=billing, read_only=read_only)
            except Exception:
                if not context.get("payment_confirmed"):
                    raise
                result = {"state": "pending", "stage": "payment", "intent_status": "succeeded",
                          "message": "付款已确认，正在核验订阅和自动续费"}
            confirmed = context.get("payment_confirmed") or (result.get("stage") == "payment" and (
                result.get("state") == "paid" or (
                    har_engine_enabled() and result.get("state") == "pending" and result.get("intent_status") == "succeeded"
                )
            ))
            if confirmed:
                context["payment_confirmed"] = True
                secrets.account_id = context.get("account_id") or client.account_id or secrets.account_id
                order = store.get_order(str(spec.get("order_id") or ""))
                if order and order.status not in {OrderStatus.ACTIVE, OrderStatus.DELIVERED}:
                    view = store.view(order.id)
                    view.payment_confirmed = True
                    view.renewal = {"status": "pending", "message": "付款已确认，正在核验订阅并关闭自动续费"}
                    store.update_status(order.id, OrderStatus.VERIFYING)
                    logger.info("protocol_payment_confirmed id=%s", order.id)
                context["renewal"] = _cancel_renewal(secrets, "plus", client=client)
                if context["renewal"]["status"] == "cancelled":
                    result = {**result, "state": "paid", "stage": "payment", "intent_status": "succeeded",
                              "message": "付款已确认，Plus 订阅已生效，自动续费已关闭"}
                    result.pop("code", None)
            return result
        finally:
            secrets.session = session_snapshot(client)
            client.close()
    finally:
        guard.release()


def _cached(order_id: str) -> dict | None:
    order = store.get_order(order_id)
    if order is None or order.plan is not Plan.PLUS:
        raise HTTPException(404, "Plus 订单不存在")
    view = store.view(order_id)
    if order.status in {OrderStatus.ACTIVE, OrderStatus.DELIVERED, OrderStatus.FAILED}:
        result = dict(view.protocol_saved_result or {})
        if not result:
            raise HTTPException(409, "订单已有最终结果，请查询原订单")
        if result.get("state") != "paid" and view.payment_confirmed and (view.renewal or {}).get("status") == "cancelled":
            result.update(state="paid", stage="payment", intent_status="succeeded",
                          message="付款已确认，自动续费已关闭", code=None)
        return {**result, "order": _to_out(order)}
    return None


def _context(order_id: str, *, start: bool):
    order = store.get_order(order_id)
    if order is None or order.plan is not Plan.PLUS:
        raise HTTPException(404, "Plus 订单不存在")
    secrets = vault.get(order_id)
    if secrets is None:
        raise _error(401, ErrorCode.SESSION_EXPIRED, "订单凭证已过期，请重新创建订单")
    context = secrets.saved_card_context
    if context and (not context.get("protocol_flow") or context.get("protocol_kind") != "checkout_v2"):
        raise HTTPException(409, "本订单已开始旧版绑卡流程，请核对原结果；新建订单才使用当前直付协议")
    if not context and start:
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            raise HTTPException(409, "订单已提交，请先查询状态")
        context = {"protocol_flow": True, "protocol_kind": "checkout_v2", "state": "preparing", "request_key": token_bytes(32), "operation_lock": Lock()}
        secrets.saved_card_context = context
    return order, secrets, store.view(order_id), context


def _spec(order, view, secrets, payload=None):
    return {
        "order_id": order.id,
        "account_id": secrets.account_id, "country": order.billing_country,
        "promo": order.mode.billing_mode is BillingMode.PROMO,
        "promo_code": view.promo_code or "", "promo_campaign_id": view.promo_campaign_id or "",
        "expected_amount": payload.expected_amount if payload else view.amount,
        "expected_currency": payload.expected_currency if payload else view.currency,
        "checkout_id": view.checkout_session_id or secrets.checkout_session_id,
        "checkout_url": view.checkout_url, "publishable_key": view.publishable_key or secrets.publishable_key,
        "amount": view.amount, "currency": view.currency,
        "customer_session_client_secret": view.customer_session_client_secret or "",
        "processor": _processor_from_checkout_url(view.checkout_url or ""),
        "confirm_return_url": view.confirm_return_url or "",
    }


async def _apply(order, secrets, view, context: dict, result: dict) -> dict:
    cached = _cached(order.id)
    if cached:
        return cached
    if context.get("quoted"):
        view.amount = str(context["amount"])
        view.currency = context["currency"]
        view.quote_error = None
        order.amount = context["amount"]
    if context.get("account_id"):
        secrets.account_id = context["account_id"]
    result = dict(result)
    if context.get("payment_confirmed"):
        view.payment_confirmed = True
        if result.get("state") != "paid":
            result.update(state="pending", stage="payment", intent_status="succeeded")
            if result.get("code") == str(ErrorCode.INVALID_SESSION):
                result["message"] = "付款已确认，账号会话已失效，订阅和自动续费结果待核实"
                context["renewal"] = {"status": "failed", "message": result["message"], "retryable": False}
    if context.get("renewal"):
        view.renewal = dict(context["renewal"])
    if (
        view.payment_confirmed
        and result.get("state") != "paid"
        and (view.renewal or {}).get("status") == "failed"
    ):
        result["message"] = view.renewal["message"]
    result["payment_confirmed"] = view.payment_confirmed
    result["stripe_direct"] = bool(context.get("stripe_direct", False))
    result["payment_method_id"] = context.get("payment_method_id")
    view.protocol_saved_result = result
    if result["stage"] == "checkout" and result["state"] == "pending":
        store.update_status(order.id, OrderStatus.AWAITING_PAYMENT)
    if result["stage"] == "payment":
        state = result["state"]
        if state == "paid":
            store.update_status(order.id, OrderStatus.PAYING)
            store.update_status(order.id, OrderStatus.VERIFYING)
            await _mark_delivered(order.id, renewal_checked=bool(context.get("renewal")))
        elif state == "card_declined":
            store.update_status(order.id, OrderStatus.FAILED)
        elif state == "challenge_required":
            store.update_status(order.id, OrderStatus.REQUIRES_ACTION)
        elif state == "unknown":
            store.update_status(order.id, OrderStatus.UNKNOWN)
        elif view.payment_confirmed:
            store.update_status(order.id, OrderStatus.VERIFYING)
        elif context.get("app_confirmation_started"):
            store.update_status(order.id, OrderStatus.PAYING)
    can_settle = view.payment_confirmed and result["state"] == "pending" and (
        not view.renewal or view.renewal.get("retryable", True)
    )
    if can_settle:
        view.renewal = {"status": "pending", "message": "付款已确认，正在核验订阅并关闭自动续费"}
        if order.id not in _settlement_tasks:
            _settlement_tasks[order.id] = asyncio.create_task(_settle_payment(order.id))
    logger.info("protocol_result id=%s stage=%s state=%s payment_confirmed=%s renewal=%s code=%s",
                order.id, result.get("stage"), result.get("state"), view.payment_confirmed,
                (view.renewal or {}).get("status"), result.get("code"))
    if result["state"] != "paid":
        view.last_error = result.get("message")
        view.next_action = result.get("next_action")
        await notify_order_update(order.id, order.status, {"protocol_stage": result["stage"], "protocol_state": result["state"]})
    return {**result, "order": _to_out(order)}


async def _run_and_apply(order, secrets, view, context, spec, **kwargs) -> dict:
    """Publish the result even when the originating HTTP request disconnects."""
    async def process():
        result = await asyncio.to_thread(_run, secrets, context, spec, **kwargs)
        return await _apply(order, secrets, view, context, result)

    task = asyncio.create_task(process())
    previous = context.get("operation_task")
    if previous is None or previous.done():
        context["operation_task"] = task
    _operation_tasks.add(task)
    task.add_done_callback(_operation_tasks.discard)
    return await asyncio.shield(task)


async def _settle_payment(order_id: str) -> None:
    try:
        for attempt in range(3):
            await asyncio.sleep(1)
            result = await get_bind_protocol_status(order_id)
            renewal = result["order"].renewal
            if result["state"] == "paid" or (renewal and renewal.status == "failed" and not renewal.retryable):
                return
    except HTTPException:
        pass
    except Exception as exc:
        logger.warning("protocol_settlement_stopped id=%s error_type=%s", order_id, type(exc).__name__)
    finally:
        _settlement_tasks.pop(order_id, None)
        order = store.get_order(order_id)
        if order and (store.view(order_id).renewal or {}).get("status") == "pending":
            secrets = vault.get(order_id)
            last = (secrets.saved_card_context or {}).get("renewal") if secrets else None
            store.view(order_id).renewal = last or {"status": "failed", "retryable": True,
                "message": "付款已确认，自动核验暂未完成，请重试关闭自动续费"}
            await notify_order_update(order_id, order.status, {"renewal": store.view(order_id).renewal})


@router.post("/{order_id}/bind-and-pay-protocol")
async def bind_and_pay_protocol(order_id: str, payload: BindProtocolRequest) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        cached = _cached(order_id)
        if cached:
            return cached
        if use_fake_adapters():
            raise HTTPException(409, "离线模式不会提交真实绑卡或付款请求")
        # Validate the quote before creating or mutating a binding context.
        order = store.get_order(order_id)
        view = store.view(order_id)
        try:
            amount = Decimal(payload.expected_amount)
            current_amount = Decimal(str(view.amount))
        except (InvalidOperation, ValueError):
            raise HTTPException(409, "尚未读到可确认的订单金额")
        if not amount.is_finite() or amount < 0 or amount != current_amount or payload.expected_currency.upper() != str(view.currency or "").upper():
            raise HTTPException(409, "订单报价已改变，请重新确认当前金额")
        billing = {key: value.strip() for key, value in payload.billing.model_dump().items()}
        billing["country"] = billing["country"].upper()
        order, secrets, view, context = _context(order_id, start=True)
        if context.get("request_fingerprint") and bool(context.get("stripe_direct", False)) != payload.stripe_direct:
            raise HTTPException(409, "本订单已确定 Stripe 连接方式，请查询原结果或重新创建订单")
        card = payload.card.model_dump()
        request = json.dumps({"card": card, "billing": billing}, sort_keys=True, separators=(",", ":")).encode()
        fingerprint = hmac.new(context["request_key"], request, hashlib.sha256).digest()
        previous = context.get("request_fingerprint")
        if previous and not hmac.compare_digest(previous, fingerprint):
            raise HTTPException(409, "本订单已开始处理另一份支付资料，请查询原结果或重新创建订单")
        context["request_fingerprint"] = fingerprint
        context["stripe_direct"] = payload.stripe_direct
        context["protocol_billing"] = billing
        secrets.billing = billing
        view.billing = dict(billing)
        view.card_last4 = payload.card.last4
        return await _run_and_apply(order, secrets, view, context, _spec(order, view, secrets, payload),
                                    card=card, billing=billing)


@router.get("/{order_id}/bind-and-pay-protocol")
async def get_bind_protocol_status(order_id: str) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        cached = _cached(order_id)
        if cached:
            return cached
        order, secrets, view, context = _context(order_id, start=False)
        if not context:
            return {"state": "not_started", "stage": "checkout", "message": "协议流程尚未开始", "order": _to_out(order)}
        if use_fake_adapters():
            raise HTTPException(409, "离线模式不会查询真实支付上下文")
        return await _run_and_apply(order, secrets, view, context, _spec(order, view, secrets), read_only=True)
