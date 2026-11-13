"""Memory order management with local fixtures and no upstream requests."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from threading import Lock

import httpx
import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import events, orders, protocol_cards
from gss_api.store import store
from gss_api.test_access import TestAccessMiddleware
from gss_contracts import Job, Order, OrderStatus, Plan
from gss_core.vault import OrderSecrets, vault


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    monkeypatch.setenv("GSS_PROXY_CONFIG", str(tmp_path / "proxies.json"))
    monkeypatch.setattr("gss_core.chatgpt.ChatGPTClient.from_raw_session",
                        lambda *args, **kwargs: pytest.fail("Memory management must not call an upstream"))
    store.clear()
    with TestClient(create_app()) as client:
        yield client
    for order_id in list(store.orders):
        vault.drop(order_id)
    store.clear()


def seed(order_id="fixture-order", *, status=OrderStatus.AWAITING_PAYMENT, minutes=0):
    order = Order(id=order_id, plan=Plan.GO, billing_country="PH", status=status,
                  created_at=datetime.now(timezone.utc) + timedelta(minutes=minutes))
    store.create_order(order, idempotency_key="fixture-key-" + order_id)
    store.add_job(Job(order_id=order_id))
    view = store.view(order_id)
    view.amount, view.currency = "8.00", "USD"
    view.card_last4 = "4242"
    view.customer_session_client_secret = "private-customer-session-fixture"
    view.next_action = {"client_secret": "private-intent-fixture"}
    view.billing = {"line1": "Private billing fixture"}
    vault.put(order_id, OrderSecrets(session=json.dumps({"accessToken": "private-access-fixture",
               "user": {"email": "memory@example.test"}}), proxy="private-proxy-fixture"))
    return order


def test_list_shows_current_orders_newest_first_and_projects_display_fields(api):
    old = seed("old", status=OrderStatus.DELIVERED, minutes=-1)
    new = seed("new")
    store.view(old.id).payment_confirmed = True
    store.view(old.id).renewal = {"status": "cancelled", "message": "自动续费已关闭", "will_renew": False}
    response = api.get("/api/memory-orders")
    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["id"] for item in items] == [new.id, old.id]
    assert items[0]["plan"] == "go" and items[0]["session_email"] == "memory@example.test"
    assert items[0]["card_last4"] == "4242" and items[0]["amount"] == "8.00"
    assert items[0]["created_at"] == new.created_at.isoformat()
    assert items[1]["renewal"]["status"] == "cancelled"
    assert items[1]["payment_confirmed"] is True
    for key in ("session", "billing", "next_action", "customer_session_client_secret", "proxy", "checkout_url"):
        assert key not in items[0]
    for private in ("private-access-fixture", "private-proxy-fixture", "private-customer-session-fixture",
                    "private-intent-fixture", "Private billing fixture"):
        assert private not in response.text


def test_clear_releases_order_view_jobs_credentials_and_idempotency(api):
    order = seed()
    response = api.request("DELETE", "/api/memory-orders", json={"order_ids": [order.id]})
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted_ids": [order.id], "missing_ids": [], "skipped": []}
    assert store.get_order(order.id) is None and order.id not in store.views
    assert not store.jobs_for_order(order.id) and vault.get(order.id) is None
    assert store.find_by_idempotency("fixture-key-" + order.id) is None
    assert api.get("/api/orders/" + order.id).status_code == 404
    assert api.get("/api/memory-orders").json() == []
    retry = api.request("DELETE", "/api/memory-orders", json={"order_ids": [order.id, order.id]})
    assert retry.json() == {"deleted_ids": [], "missing_ids": [order.id], "skipped": []}


def test_bulk_clear_only_removes_the_explicit_snapshot(api):
    first, second, later = seed("first"), seed("second"), seed("later")
    response = api.request("DELETE", "/api/memory-orders", json={"order_ids": [first.id, second.id, first.id]})
    assert response.json()["deleted_ids"] == [first.id, second.id]
    assert list(store.orders) == [later.id]
    assert vault.get(later.id) is not None
    for payload in ({}, {"order_ids": []}):
        assert api.request("DELETE", "/api/memory-orders", json=payload).status_code == 422
    assert list(store.orders) == [later.id]


@pytest.mark.parametrize("activity", ["request", "checkout", "renewal", "settlement", "protocol", "thread"])
def test_clear_skips_inflight_operations_and_allows_retry_after_completion(api, activity):
    busy, idle = seed("busy"), seed("idle")

    async def exercise():
        task = None
        guard = None
        registry = None
        if activity == "request":
            guard = asyncio.Lock()
            await guard.acquire()
            orders._team_locks[busy.id] = guard
        elif activity == "thread":
            guard = Lock()
            guard.acquire()
            vault.get(busy.id).saved_card_context = {"operation_lock": guard}
        else:
            task = asyncio.create_task(asyncio.Event().wait(), name=f"order:{busy.id}")
            if activity == "protocol":
                protocol_cards._operation_tasks.add(task)
                vault.drop(busy.id)  # Expired credentials must not hide a live payment worker.
            else:
                registry = {"checkout": orders._checkout_tasks, "renewal": orders._renewal_tasks,
                            "settlement": protocol_cards._settlement_tasks}[activity]
                registry[busy.id] = task
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                items = (await client.get("/api/memory-orders")).json()
                assert next(item for item in items if item["id"] == busy.id)["deletion_blocked_reason"]
                result = (await client.request("DELETE", "/api/memory-orders", json={"order_ids": [busy.id, idle.id]})).json()
                assert result["deleted_ids"] == [idle.id]
                assert result["skipped"][0]["id"] == busy.id
                assert store.get_order(busy.id) is not None
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                protocol_cards._operation_tasks.discard(task)
            if registry is not None:
                registry.pop(busy.id, None)
            if guard is not None:
                guard.release()
    asyncio.run(exercise())
    response = api.request("DELETE", "/api/memory-orders", json={"order_ids": [busy.id]})
    assert response.json()["deleted_ids"] == [busy.id]
    assert busy.id not in orders._team_locks


def test_checkout_entry_point_is_protected_until_creation_finishes(api, monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        from gss_checkout.adapter import FakeCheckoutAdapter
        original = FakeCheckoutAdapter.create_checkout

        async def checkout(self, request):
            entered.set()
            await release.wait()
            return await original(self, request)

        monkeypatch.setattr(FakeCheckoutAdapter, "create_checkout", checkout)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            pending = asyncio.create_task(client.post("/api/orders", json={"plan": "go", "billing_country": "PH",
                                                                         "session": '{"accessToken":"fixture"}'}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                items = (await client.get("/api/memory-orders")).json()
                order_id = items[0]["id"]
                assert items[0]["deletion_blocked_reason"]
                result = (await client.request("DELETE", "/api/memory-orders", json={"order_ids": [order_id]})).json()
                assert result["skipped"] and store.get_order(order_id) is not None
            finally:
                release.set()
                created = await pending
            assert created.status_code == 201
            assert order_id not in orders._checkout_tasks
            result = (await client.request("DELETE", "/api/memory-orders", json={"order_ids": [order_id]})).json()
            assert result["deleted_ids"] == [order_id]

    asyncio.run(exercise())


def test_clear_closes_existing_order_event_streams(api):
    order = seed()

    async def exercise():
        stream = events._event_stream(order.id, str(order.status))
        await anext(stream)
        queue = next(iter(events._subscribers[order.id]))
        for _ in range(queue.maxsize):
            queue.put_nowait({"status": "verifying"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            result = await client.request("DELETE", "/api/memory-orders", json={"order_ids": [order.id]})
            assert result.status_code == 200
        assert '"type": "deleted"' in await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert order.id not in events._subscribers
        late_stream = events._event_stream(order.id, "verifying")
        assert '"type": "deleted"' in await anext(late_stream)
        with pytest.raises(StopAsyncIteration):
            await anext(late_stream)
        assert order.id not in events._subscribers

    asyncio.run(exercise())


def test_memory_endpoints_use_existing_login_and_origin_protection(api, tmp_path):
    order = seed()
    credentials, login = tmp_path / "auth.json", tmp_path / "login.html"
    credentials.write_text(json.dumps({"password": "fixture-password"}))
    login.write_text("<html>{{ERROR_HIDDEN}}{{INVALID}}</html>")
    app = create_app()
    app.add_middleware(TestAccessMiddleware, credentials_file=str(credentials), login_file=str(login))
    with TestClient(app, base_url="https://memory.example.test") as client:
        assert client.get("/api/memory-orders").status_code == 401
        assert client.request("DELETE", "/api/memory-orders", json={"order_ids": [order.id]}).status_code == 401
        assert client.post("/login", data={"password": "fixture-password"}, follow_redirects=False).status_code == 303
        assert client.get("/api/memory-orders").status_code == 200
        response = client.request("DELETE", "/api/memory-orders", json={"order_ids": [order.id]},
                                  headers={"Origin": "https://another.example.test"})
        assert response.status_code == 403 and store.get_order(order.id) is not None
