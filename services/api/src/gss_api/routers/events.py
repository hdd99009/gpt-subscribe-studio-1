"""SSE (Server-Sent Events) 实时订单状态推送。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from gss_contracts.models import OrderStatus
from ..store import store

router = APIRouter(prefix="/api/events", tags=["events"])
logger = logging.getLogger("gss.api.events")

# 全局订阅管理器：order_id -> set of asyncio.Queue
_subscribers: dict[str, set[asyncio.Queue[dict]]] = {}
_lock = asyncio.Lock()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def notify_order_update(order_id: str, status: OrderStatus, data: dict | None = None) -> None:
    """通知所有订阅该订单的客户端。"""
    async with _lock:
        queues = _subscribers.get(order_id, set())
        if not queues:
            return
        
        event = {
            "order_id": order_id,
            "status": status.value,
            "timestamp": time.time(),
            **(data or {}),
        }
        
        # 发送到所有订阅者队列
        dead_queues = set()
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 客户端消费太慢，丢弃该订阅
                dead_queues.add(q)
        
        # 清理失效队列
        for q in dead_queues:
            queues.discard(q)


async def _subscribe(order_id: str) -> asyncio.Queue[dict]:
    """订阅一个订单的更新事件。"""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
    async with _lock:
        if order_id not in _subscribers:
            _subscribers[order_id] = set()
        _subscribers[order_id].add(q)
    return q


async def close_order_streams(order_id: str) -> None:
    async with _lock:
        queues = _subscribers.pop(order_id, set())
        for queue in queues:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait({"type": "deleted", "order_id": order_id,
                              "status": "deleted", "timestamp": time.time()})


async def _unsubscribe(order_id: str, q: asyncio.Queue[dict]) -> None:
    """取消订阅。"""
    async with _lock:
        if order_id in _subscribers:
            _subscribers[order_id].discard(q)
            if not _subscribers[order_id]:
                del _subscribers[order_id]


async def _event_stream(order_id: str, initial_status: str) -> AsyncGenerator[str, None]:
    """生成 SSE 事件流。"""
    q = await _subscribe(order_id)
    
    try:
        if store.get_order(order_id) is None:
            yield _sse({"type": "deleted", "order_id": order_id,
                        "status": "deleted", "timestamp": time.time()})
            return
        # 发送初始状态
        yield _sse({"type": "init", "status": initial_status})
        
        while True:
            try:
                # 等待事件，60 秒超时发送心跳
                event = await asyncio.wait_for(q.get(), timeout=60.0)
                yield _sse(event)
                if event.get("type") == "deleted":
                    return
            except asyncio.TimeoutError:
                # 心跳：保持连接活跃
                yield f": heartbeat {time.time()}\n\n"
    except asyncio.CancelledError:
        logger.info(f"SSE stream cancelled for order {order_id}")
    finally:
        await _unsubscribe(order_id, q)


@router.get("/orders/{order_id}/stream")
async def stream_order_updates(order_id: str, request: Request) -> StreamingResponse:
    """订阅订单状态更新的 SSE 流。
    
    客户端示例：
    ```javascript
    const events = new EventSource('/api/events/orders/{order_id}/stream');
    events.addEventListener('message', (e) => {
      const data = JSON.parse(e.data);
      console.log('Order update:', data);
    });
    ```
    """
    # 先查询当前订单状态
    order = store.get_order(order_id)
    if not order:
        # 订单不存在，返回一个立即结束的流
        async def not_found():
            yield _sse({"error": "Order not found"})
        return StreamingResponse(
            not_found(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
            },
        )
    
    logger.info(f"Client subscribed to order {order_id}, current status: {order.status.value}")
    
    return StreamingResponse(
        _event_stream(order_id, order.status.value),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
