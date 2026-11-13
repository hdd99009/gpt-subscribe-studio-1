from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
import httpx
import pytest

from gss_api.main import create_app
from gss_core.http_debug import begin_exchange, debug_store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GSS_PROXY_CONFIG", str(tmp_path / "proxies.json"))
    return TestClient(create_app(), raise_server_exceptions=False)


def start(client):
    response = client.put("/api/settings/debug", json={"enabled": True})
    assert response.status_code == 200
    return response.json()["capture_id"]


def entries(client, capture_id):
    response = client.get(f"/api/settings/debug/captures/{capture_id}/har")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()["log"]["entries"]


def test_disabled_by_default_and_switching_creates_distinct_captures(client):
    assert client.get("/api/settings/debug").json()["enabled"] is False
    client.get("/api/health")
    assert not debug_store().directory.exists()
    first = start(client)
    assert start(client) == first
    client.get("/api/health")
    assert len(entries(client, first)) == 1
    assert client.put("/api/settings/debug", json={"enabled": False}).status_code == 200
    client.get("/api/health")
    assert len(entries(client, first)) == 1
    second = start(client)
    assert first != second
    assert debug_store().status()["capture_id"] == second


def test_reinitialization_with_a_temporarily_replaced_curl_method_does_not_stack_httpx_hooks(client, monkeypatch):
    from curl_cffi.requests import Session
    from gss_core.http_debug import install_transport_capture
    original_sync = httpx.Client._send_single_request
    original_async = httpx.AsyncClient._send_single_request
    monkeypatch.setattr(Session, "request", lambda *args, **kwargs: None)
    install_transport_capture()
    create_app()
    assert httpx.Client._send_single_request is original_sync
    assert httpx.AsyncClient._send_single_request is original_async


def test_business_api_and_upstream_are_correlated_with_full_untruncated_payloads(client):
    token = "fixture-access-credential"
    large_body = "x" * 250000

    @client.app.post("/api/debug-fixture")
    async def fixture(request: Request):
        posted = await request.json()
        def send():
            with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
                401, json={"error": {"code": "token_revoked"}, "body": large_body},
                headers={"set-cookie": "fixture=renewed-value"},
            )), trust_env=False) as upstream:
                return upstream.post("https://upstream.invalid/checkout?country=PH", json=posted,
                                     headers={"Authorization": "Bearer " + token, "Cookie": "fixture=original-value"}).json()
        return await asyncio.to_thread(send)

    capture = start(client)
    response = client.post("/api/debug-fixture", json={"session": "fixture-session", "card": {"number": "fixture-card", "cvc": "fixture-code"}})
    assert response.status_code == 200 and response.json()["body"] == large_body
    captured = entries(client, capture)
    assert len(captured) == 2
    inbound = next(item for item in captured if item["_direction"] == "inbound")
    upstream = next(item for item in captured if item["_direction"] == "upstream")
    assert upstream["_parentRequestId"] == inbound["_id"]
    assert upstream["response"]["status"] == 401
    assert json.loads(upstream["response"]["content"]["text"])["body"] == large_body
    assert token in json.dumps(upstream["request"]["headers"])
    assert "original-value" in json.dumps(upstream["request"]["headers"])
    assert "renewed-value" in json.dumps(upstream["response"]["headers"])
    assert "fixture-session" in inbound["request"]["postData"]["text"]
    assert "fixture-card" in upstream["request"]["postData"]["text"]
    assert all(item["_state"] == "complete" for item in captured)


def test_httpx_redirects_and_binary_body_are_preserved(client):
    def route(request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/binary"})
        return httpx.Response(200, content=b"\xff\x00\xfe", headers={"content-type": "application/octet-stream"})
    capture = start(client)
    with httpx.Client(transport=httpx.MockTransport(route), trust_env=False) as upstream:
        response = upstream.get("https://upstream.invalid/redirect", follow_redirects=True)
    assert response.content == b"\xff\x00\xfe"
    captured = entries(client, capture)
    assert [item["response"]["status"] for item in captured] == [302, 200]
    content = captured[1]["response"]["content"]
    assert content["encoding"] == "base64" and base64.b64decode(content["text"]) == response.content


def test_network_errors_keep_original_exception_and_are_recorded(client):
    def timeout(request):
        raise httpx.ReadTimeout("fixture-timeout", request=request)
    capture = start(client)
    with httpx.Client(transport=httpx.MockTransport(timeout), trust_env=False) as upstream:
        with pytest.raises(httpx.ReadTimeout, match="fixture-timeout"):
            upstream.get("https://upstream.invalid/timeout")
    captured = entries(client, capture)
    assert len(captured) == 1
    assert captured[0]["_error"]["type"] == "ReadTimeout"
    assert captured[0]["response"]["status"] == 0


def test_async_httpx_stream_is_not_consumed_before_the_caller_reads(client):
    class Body(httpx.AsyncByteStream):
        reads = 0
        async def __aiter__(self):
            self.reads += 1
            yield b"first"
            yield b"second"
    capture = start(client)
    body = Body()
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=body)),
                                    trust_env=False) as upstream:
            async with upstream.stream("GET", "https://upstream.invalid/stream") as response:
                assert body.reads == 0
                assert await response.aread() == b"firstsecond"
    asyncio.run(run())
    captured = entries(client, capture)
    assert len(captured) == 1 and captured[0]["response"]["content"]["text"] == "firstsecond"


def test_asgi_sse_is_captured_in_chunks_and_forwarded_immediately(client):
    async def run():
        first_sent = asyncio.Event()
        chunks = []
        async def generate():
            yield b"data: first\n\n"
            await asyncio.wait_for(first_sent.wait(), timeout=1)
            yield b"data: second\n\n"
        async def fixture(scope, receive, send):
            await StreamingResponse(generate(), media_type="text/event-stream")(scope, receive, send)
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"])
                first_sent.set()
        from gss_core.http_debug import HttpDebugMiddleware
        scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET", "scheme": "http",
                 "path": "/api/events/fixture", "query_string": b"", "headers": [],
                 "server": ("localhost", 80), "http_version": "1.1"}
        await HttpDebugMiddleware(fixture)(scope, receive, send)
        assert chunks == [b"data: first\n\n", b"data: second\n\n"]
    capture = start(client)
    asyncio.run(run())
    captured = entries(client, capture)
    assert len(captured) == 1 and captured[0]["response"]["content"]["text"] == "data: first\n\ndata: second\n\n"


def test_global_network_error_response_is_also_captured(client):
    @client.app.get("/api/debug-network-error")
    def fixture():
        raise httpx.ConnectError("fixture-connect-failure")
    capture = start(client)
    response = client.get("/api/debug-network-error")
    assert response.status_code == 503
    captured = entries(client, capture)
    assert captured[0]["response"]["status"] == 503
    assert captured[0]["response"]["content"]["text"] == response.text


def test_concurrent_requests_and_inflight_completion_after_disable(client):
    capture = start(client)
    def send(index):
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=str(index))),
                          trust_env=False) as upstream:
            return upstream.get(f"https://upstream.invalid/{index}").text
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(send, range(12))) == [str(i) for i in range(12)]
    exchange = begin_exchange(method="GET", url="https://upstream.invalid/inflight", headers=[], body=b"",
                              transport="fixture", direction="upstream")
    client.put("/api/settings/debug", json={"enabled": False})
    exchange.start_response(200, [])
    exchange.chunk(b"finished-after-disable")
    exchange.finish()
    captured = entries(client, capture)
    assert len(captured) == 13 and len({item["_id"] for item in captured}) == 13
    assert all(item["_state"] == "complete" for item in captured)


def test_capture_write_failure_does_not_change_business_result(client, monkeypatch):
    start(client)
    def no_space(*args, **kwargs):
        raise OSError("fixture-disk-full")
    monkeypatch.setattr("gss_core.http_debug._atomic_json", no_space)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="business-ok")),
                      trust_env=False) as upstream:
        assert upstream.get("https://upstream.invalid/ok").text == "business-ok"
    assert "OSError" in debug_store().last_error


def test_bad_settings_and_capture_paths_are_rejected(client):
    assert client.put("/api/settings/debug", json={"enabled": "false"}).status_code == 422
    assert client.get("/api/settings/debug/captures/invalid/har").status_code == 400
    assert client.get("/api/settings/debug/captures/20261113T010203000000Z-12345678/har").status_code == 404


def test_sentinel_bridge_input_output_and_capture_context_are_exported(client, monkeypatch):
    from gss_core.sentinel import mint
    def bridge(payload, timeout, env):
        args = json.loads(payload)
        assert args["debugCapture"]["parentRequestId"]
        return SimpleNamespace(returncode=0, stderr=b"fixture-bridge-diagnostic", stdout=json.dumps({
            "main": json.dumps({"p": "fixture-proof", "t": "fixture-turnstile", "c": "fixture-challenge"}),
            "so": json.dumps({"so": "fixture-observer"}), "hasT": True, "hasSo": True,
        }).encode())
    monkeypatch.setattr("gss_core.sentinel._run_bridge", bridge)
    @client.app.get("/api/debug-sentinel")
    def fixture():
        return {"minted": bool(mint(flow="fixture", device_id="fixture-device", user_agent="fixture-agent"))}
    capture = start(client)
    assert client.get("/api/debug-sentinel").json() == {"minted": True}
    log = client.get(f"/api/settings/debug/captures/{capture}/har").json()["log"]
    assert len(log["_sentinelBridges"]) == 1
    bridge = log["_sentinelBridges"][0]
    assert bridge["_parentRequestId"] == log["entries"][0]["_id"]
    assert bridge["input"]["deviceId"] == "fixture-device"
    assert "fixture-turnstile" in bridge["stdout"] and "fixture-observer" in bridge["stdout"]
    assert bridge["stderr"] == "fixture-bridge-diagnostic"
    assert bridge["state"] == "complete"


def test_curl_transport_captures_cookie_form_headers_and_response(client):
    from curl_cffi.requests import Session
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "renewed=fixture-cookie; Path=/")
            self.end_headers()
            self.wfile.write(json.dumps({"posted": body.decode(), "cookie": self.headers.get("Cookie")}).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    capture = start(client)
    try:
        with Session(impersonate="chrome150", trust_env=False) as upstream:
            upstream.cookies.set("fixture", "original-cookie", domain="127.0.0.1")
            response = upstream.post(f"http://127.0.0.1:{server.server_port}/form?country=PH",
                                     data={"fixture": "full-form-value"}, headers={"X-Fixture": "request-header"})
            assert response.status_code == 402
            assert "original-cookie" in response.json()["cookie"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    captured = entries(client, capture)
    assert len(captured) == 1 and captured[0]["_transport"] == "curl_cffi"
    assert captured[0]["request"]["postData"]["text"] == "fixture=full-form-value"
    assert "original-cookie" in json.dumps(captured[0]["request"]["headers"])
    assert captured[0]["response"]["status"] == 402
    assert "renewed=fixture-cookie" in json.dumps(captured[0]["response"]["headers"])
    assert json.loads(captured[0]["response"]["content"]["text"])["posted"] == "fixture=full-form-value"
