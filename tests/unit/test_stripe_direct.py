"""Direct payment transport ignores configured and environment proxies. Loopback GET only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from gss_core.chatgpt import ChatGPTClient
from gss_core.har_device import DeviceProfile
from gss_core.plus_har_protocol import PlusHarProtocol


@pytest.fixture
def local_http():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"local transport fixture")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("kind", ["client", "har"])
def test_stripe_direct_ignores_proxies_and_keeps_chatgpt_route(monkeypatch, local_http, kind):
    proxy = "http://127.0.0.1:9"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, proxy)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.delenv("GSS_HAR_CAPTURE", raising=False)

    if kind == "client":
        engine = ChatGPTClient(access_token="fixture", proxy=proxy, stripe_direct=True)
        stripe = engine.stripe_http
        web = engine.http
    else:
        engine = PlusHarProtocol(session="fixture", proxy=proxy, stripe_direct=True,
                                 device=DeviceProfile.random(seed="direct-fixture"), telemetry="off")
        stripe = engine._stripe_session()
        web = engine._web_session()
        assert engine._m_session().proxies == {"http": "", "https": ""}
    try:
        assert web.proxies == {"http": proxy, "https": proxy}
        assert stripe.proxies == {"http": "", "https": ""}
        response = stripe.get(local_http, timeout=2)
        assert response.status_code == 200
        assert response.text == "local transport fixture"
    finally:
        engine.close()


@pytest.mark.parametrize("kind", ["client", "har"])
def test_stripe_defaults_to_the_existing_proxy(kind):
    proxy = "http://127.0.0.1:9"
    if kind == "client":
        engine = ChatGPTClient(access_token="fixture", proxy=proxy)
        stripe = engine.stripe_http
    else:
        engine = PlusHarProtocol(session="fixture", proxy=proxy,
                                 device=DeviceProfile.random(seed="proxy-fixture"), telemetry="off")
        stripe = engine._stripe_session()
    try:
        assert stripe.proxies == {"http": proxy, "https": proxy}
    finally:
        engine.close()
