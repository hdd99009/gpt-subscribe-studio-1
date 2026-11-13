"""Inspect and release orders held by the current API process."""
from __future__ import annotations

from fastapi import APIRouter

from gss_core.redaction import redact_sensitive
from gss_core.vault import vault

from ..schemas import MemoryOrderOut, MemoryOrdersClearOut, MemoryOrdersClearRequest
from ..store import store
from . import orders, protocol_cards
from .events import close_order_streams

router = APIRouter(prefix="/api/memory-orders", tags=["memory-orders"])


def _blocked_reason(order_id: str) -> str | None:
    lock = orders._team_locks.get(order_id)
    if lock and lock.locked():
        return "订单请求正在执行，请稍后刷新再清除"
    tasks = (orders._checkout_tasks.get(order_id), orders._renewal_tasks.get(order_id),
             protocol_cards._settlement_tasks.get(order_id))
    if any(task is not None and not task.done() for task in tasks):
        return "正在创建结账、核验订阅或关闭续费，请稍后刷新再清除"
    if any(not task.done() and task.get_name() == f"order:{order_id}"
           for task in protocol_cards._operation_tasks):
        return "支付协议仍在处理，请稍后刷新再清除"
    secrets = vault.get(order_id)
    context = secrets.saved_card_context if secrets else None
    if context:
        guard = context.get("operation_lock")
        task = context.get("operation_task")
        if (guard and guard.locked()) or (task is not None and not task.done()):
            return "支付协议仍在处理，请稍后刷新再清除"
    return None


@router.get("", response_model=list[MemoryOrderOut])
async def list_memory_orders() -> list[MemoryOrderOut]:
    result = []
    for order in sorted(store.orders.values(), key=lambda item: item.created_at, reverse=True):
        snapshot = orders._to_out(order)
        fields = snapshot.model_dump(include=set(MemoryOrderOut.model_fields))
        fields["last_error"] = redact_sensitive(snapshot.last_error) if snapshot.last_error else None
        if fields.get("renewal"):
            fields["renewal"]["message"] = redact_sensitive(fields["renewal"]["message"])
        result.append(MemoryOrderOut(**fields, created_at=order.created_at.isoformat(),
                                     deletion_blocked_reason=_blocked_reason(order.id)))
    return result


@router.delete("", response_model=MemoryOrdersClearOut)
async def clear_memory_orders(payload: MemoryOrdersClearRequest) -> MemoryOrdersClearOut:
    deleted, missing, skipped = [], [], []
    # Check and remove each order without yielding, so another request cannot
    # begin an operation between the activity check and credential release.
    for order_id in dict.fromkeys(payload.order_ids):
        reason = _blocked_reason(order_id)
        if reason:
            skipped.append({"id": order_id, "reason": reason})
            continue
        removed = store.delete_order(order_id)
        vault.drop(order_id)
        orders._team_locks.pop(order_id, None)
        orders._checkout_tasks.pop(order_id, None)
        orders._renewal_tasks.pop(order_id, None)
        protocol_cards._settlement_tasks.pop(order_id, None)
        (deleted if removed else missing).append(order_id)
    for order_id in deleted:
        await close_order_streams(order_id)
    return MemoryOrdersClearOut(deleted_ids=deleted, missing_ids=missing, skipped=skipped)
