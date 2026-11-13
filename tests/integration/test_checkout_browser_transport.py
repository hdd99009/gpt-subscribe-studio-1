"""Exercise the actual Node SDK and curl bridge against a local checkout fixture."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import threading
from urllib.parse import urlsplit

import pytest

from gss_core import chatgpt, sentinel


@pytest.mark.skipif(not shutil.which("node"), reason="Node runtime not installed")
def test_real_protocol_transports_share_identity_and_measure_ping(monkeypatch):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            path = urlsplit(self.path).path
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append({"method": self.command, "path": path, "headers": dict(self.headers), "body": body})
            if path == "/backend-api/sentinel/req":
                payload = {"token": "fixture-challenge", "expire_after": 540,
                           "turnstile": {"required": False}, "proofofwork": {"required": False},
                           "so": {"required": False}}
            elif path == "/backend-api/sentinel/ping":
                payload = {"status": "ok"}
            else:
                payload = {"checkout_session_id": "oaics_fixture", "publishable_key": "pk_test_fixture",
                           "processor_entity": "openai_llc", "amount_total": 110000, "currency": "php"}
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if path.endswith("/sentinel/ping"):
                for key, value in {"s-cf-edge-msec": "11", "s-cf-origin-ttfb-msec": "37", "s-cf-tcp-rtt-msec": "4"}.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(chatgpt, "APP_BASE", origin)
    monkeypatch.setattr(chatgpt, "CHECKOUT_PAGE", origin + "/settings/billing?source=chatgpt_web_merged")
    monkeypatch.setenv("GSS_DEVICE_OS", "windows")
    monkeypatch.delenv("SENTINEL_CURL_DAEMON_SOCKET", raising=False)
    monkeypatch.delenv("SENTINEL_DAEMON_SOCKET", raising=False)
    monkeypatch.setenv("SENTINEL_NODE", shutil.which("node"))
    original = sentinel._run_bridge
    bridge_inputs = []
    def local_bridge(payload, timeout, env):
        data = json.loads(payload)
        data["sentinelOrigin"] = origin
        bridge_inputs.append(data)
        return original(json.dumps(data).encode(), timeout, env)
    monkeypatch.setattr(sentinel, "_run_bridge", local_bridge)
    client = chatgpt.ChatGPTClient(access_token="fixture-access", account_id="fixture-account", device_id="fixture-device")
    try:
        created = client.create_checkout(plan="plus", country="PH")
        assert created.currency == "PHP" and str(created.amount) == "1100.00"
        assert [(call["method"], call["path"]) for call in calls] == [
            ("GET", "/settings/billing"), ("POST", "/backend-api/sentinel/req"),
            ("POST", "/backend-api/sentinel/ping"), ("POST", "/backend-api/payments/checkout"),
            ("GET", "/backend-api/payments/checkout/openai_llc/oaics_fixture"),
            ("POST", "/backend-api/payments/checkout/taxes"),
        ]
        normalized = [{key.lower(): value for key, value in call["headers"].items()} for call in calls]
        assert len({headers["user-agent"] for headers in normalized}) == 1
        assert all('v="150"' in headers["sec-ch-ua"] for headers in normalized)
        assert all(headers["sec-ch-ua-platform"] == '"Windows"' for headers in normalized)
        assert bridge_inputs[0]["impersonate"] == "chrome150"
        timing = json.loads(normalized[3]["oai-telemetry"])
        assert timing[2:7] == [11, 37, 4, 1, 0]
        assert timing[1] >= 0 and timing[7] >= 0
        assert normalized[3]["referer"] == chatgpt.CHECKOUT_PAGE
        posted = json.loads(calls[3]["body"])
        assert posted["cancel_url"] == chatgpt.CHECKOUT_PAGE + "#pricing"
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
