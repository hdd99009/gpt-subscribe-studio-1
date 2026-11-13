import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from curl_cffi import CurlInfo
import pytest

from gss_core.chatgpt import CHECKOUT_PAGE, ChatGPTClient, _pick_profile
from gss_core.har_telemetry import sentinel_timing


def ping_response(**overrides):
    values = {
        "status_code": 200, "http_version": 3,
        "headers": {"s-cf-edge-msec": "11", "s-cf-origin-ttfb-msec": "37", "s-cf-tcp-rtt-msec": "4"},
        "infos": {CurlInfo.PRETRANSFER_TIME: 0.100, CurlInfo.STARTTRANSFER_TIME: 0.316},
        "cookies": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_timing_uses_the_same_fields_as_the_browser_har():
    assert json.loads(sentinel_timing(ping_response(), elapsed_ms=223)) == [1, 216, 11, 37, 4, 2, 0, 223]
    changed = ping_response(headers={"s-cf-edge-msec": "8", "s-cf-origin-ttfb-msec": "22", "s-cf-tcp-rtt-msec": "9"},
                            infos={CurlInfo.PRETRANSFER_TIME: 0.01, CurlInfo.STARTTRANSFER_TIME: 0.135})
    assert json.loads(sentinel_timing(changed, elapsed_ms=144)) == [1, 125, 8, 22, 9, 2, 0, 144]


def test_http3_timing_reads_quic_rtt():
    response = ping_response(http_version=30, headers={
        "s-cf-edge-msec": "11", "s-cf-origin-ttfb-msec": "37", "s-cf-quic-rtt-msec": "6", "s-cf-tcp-rtt-msec": "100",
    })
    assert json.loads(sentinel_timing(response, elapsed_ms=223))[4:6] == [6, 3]


@pytest.mark.parametrize("overrides", [
    {"status_code": 403}, {"headers": {}}, {"infos": {}},
    {"infos": {CurlInfo.PRETRANSFER_TIME: 0.316, CurlInfo.STARTTRANSFER_TIME: 0.1}},
    {"headers": {"s-cf-edge-msec": "NaN", "s-cf-origin-ttfb-msec": "37", "s-cf-tcp-rtt-msec": "4"}},
])
def test_incomplete_or_invalid_timing_uses_the_sdk_empty_value(overrides):
    assert sentinel_timing(ping_response(**overrides), elapsed_ms=223) == "[1,null]"


class Http:
    def __init__(self, calls, ping=None):
        self.calls = calls
        self.headers = {}
        self.cookies = {}
        self.ping = ping or ping_response()

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return SimpleNamespace(status_code=200, text="", cookies={}, json=lambda: {"amount_total": 110000, "currency": "php"})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/sentinel/ping"):
            if isinstance(self.ping, Exception):
                raise self.ping
            return self.ping
        return SimpleNamespace(status_code=200, json=lambda: {
            "checkout_session_id": "oaics_fixture", "publishable_key": "pk_test_fixture",
            "processor_entity": "openai_llc", "amount_total": 110000, "currency": "php",
        })


def checkout_client(monkeypatch, ping=None):
    calls = []
    def mint(**kwargs):
        calls.append(("MINT", kwargs["page_url"], kwargs))
        return {"p": "fixture-proof", "c": "fixture-challenge", "t": "fixture-turnstile",
                "id": kwargs["device_id"], "flow": kwargs["flow"]}
    monkeypatch.setattr("gss_core.chatgpt.mint", mint)
    client = ChatGPTClient(access_token="fixture-access", account_id="fixture-account", device_id="fixture-device")
    client._http = Http(calls, ping)
    return client, calls


def test_normal_checkout_uses_measured_ping_before_post_and_one_page_source(monkeypatch):
    client, calls = checkout_client(monkeypatch)
    created = client.create_checkout(plan="plus", country="PH")
    assert created.currency == "PHP"
    assert [(method, urlsplit(url).path) for method, url, _ in calls] == [
        ("GET", "/settings/billing"), ("MINT", "/settings/billing"),
        ("POST", "/backend-api/sentinel/ping"), ("POST", "/backend-api/payments/checkout"),
        ("GET", "/backend-api/payments/checkout/openai_llc/oaics_fixture"),
        ("POST", "/backend-api/payments/checkout/taxes"),
    ]
    assert calls[0][1] == calls[1][1] == CHECKOUT_PAGE
    posted = calls[3][2]
    assert posted["headers"]["Referer"] == CHECKOUT_PAGE
    assert posted["json"]["cancel_url"] == CHECKOUT_PAGE + "#pricing"
    assert posted["json"]["billing_details"] == {"country": "PH", "currency": "PHP"}
    timing = json.loads(posted["headers"]["oai-telemetry"])
    assert timing[:7] == [1, 216, 11, 37, 4, 2, 0]
    assert timing[7] >= 0
    identity = client._identity_headers()
    mint = calls[1][2]
    assert mint["user_agent"] == identity["User-Agent"]
    assert mint["client_hints"] == client.client_hints
    assert mint["impersonate"] == "chrome150"
    assert mint["language"] == identity["oai-language"] == "en-PH"
    assert calls[2][2]["headers"]["Authorization"] is None


@pytest.mark.parametrize("ping", [RuntimeError("fixture-timeout"), ping_response(status_code=503), ping_response(headers={})])
def test_ping_failure_does_not_block_checkout_or_reuse_previous_timing(monkeypatch, ping):
    client, calls = checkout_client(monkeypatch, ping)
    client.sentinel_telemetry = "[1,999,1,1,1,2,0,999]"
    assert client.create_checkout(plan="go", country="PH").checkout_session_id == "oaics_fixture"
    assert calls[3][2]["headers"]["oai-telemetry"] == "[1,null]"


def test_personal_campaign_keeps_its_query_on_the_billing_page(monkeypatch):
    client, calls = checkout_client(monkeypatch)
    client.create_checkout(plan="plus", country="PH", promo_campaign_id="fixture-promo")
    source = calls[0][1]
    assert urlsplit(source).path == "/settings/billing"
    assert parse_qs(urlsplit(source).query) == {"source": ["chatgpt_web_merged"], "promo_campaign": ["fixture-promo"]}
    assert calls[1][1] == source == calls[3][2]["headers"]["Referer"]


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_profile_os_and_full_client_hints_match_the_transport_major(monkeypatch, platform):
    monkeypatch.setenv("GSS_DEVICE_OS", platform)
    profile = _pick_profile("fixture-device")
    assert profile["key"] == platform and profile["build"].startswith("150.")
    client = ChatGPTClient(access_token="fixture", device_id="fixture-device")
    assert "Chrome/" + profile["build"] in client.ua
    assert client.client_hints["sec-ch-ua-full-version"] == '"' + profile["build"] + '"'
    assert 'v="150"' in client.client_hints["sec-ch-ua"]
    assert client.sentinel_profile["client_hints"] == client.client_hints
    assert client.sentinel_profile["impersonate"] == "chrome150"


def test_main_transport_collects_actual_curl_timing():
    client = ChatGPTClient(access_token="fixture", device_id="fixture-device")
    try:
        assert client.http.impersonate == "chrome150"
        assert CurlInfo.PRETRANSFER_TIME in client.http.curl_infos
        assert CurlInfo.STARTTRANSFER_TIME in client.http.curl_infos
    finally:
        client.close()
