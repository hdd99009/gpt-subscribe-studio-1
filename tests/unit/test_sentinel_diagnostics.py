import json
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from gss_core.sentinel import last_failure, mint


def call():
    return mint(flow="fixture", device_id="fixture", user_agent="fixture")


@pytest.mark.parametrize("error,reason", [(FileNotFoundError(), "node_missing"),
                                         (subprocess.TimeoutExpired("node", 1), "node_timeout")])
def test_runtime_failures_are_distinguished(monkeypatch, error, reason):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr("gss_core.sentinel._run_bridge", run)
    assert call() == {}
    assert last_failure()["reason"] == reason


def test_bridge_failure_diagnostic_does_not_log_proxy_password_cookies_or_raw_output(monkeypatch, caplog):
    proxy = "socks5h://fixture-user:p%40ssword@proxy.example.invalid:32000"
    error = "proto2: request failed " + proxy + " cookie-secret p@ssword"
    output = {"error": error, "raw": "private-driver-content"}
    monkeypatch.setattr("gss_core.sentinel._run_bridge", lambda *args, **kwargs:
                        SimpleNamespace(stdout=json.dumps(output).encode(), stderr=b"", returncode=1))
    assert mint(flow="fixture", device_id="fixture", user_agent="fixture", proxy=proxy,
                cookie_header="fixture=cookie-secret") == {}
    assert last_failure()["reason"] == "bridge_error"
    for private in ("fixture-user", "p%40ssword", "p@ssword", "cookie-secret", "private-driver-content"):
        assert private not in caplog.text and private not in str(last_failure())
    assert "proto2" in caplog.text


def test_success_keeps_original_token_and_clears_failure(monkeypatch):
    token = {"p": "fixture-proof", "c": "fixture-challenge", "id": "fixture", "flow": "fixture"}
    output = {"main": json.dumps(token), "so": json.dumps({"so": "fixture-secondary"})}
    monkeypatch.setattr("gss_core.sentinel._run_bridge", lambda *args, **kwargs:
                        SimpleNamespace(stdout=json.dumps(output).encode(), stderr=b"", returncode=0))
    result = call()
    assert {key: result[key] for key in token} == token
    assert result["_so"] == {"so": "fixture-secondary"}
    assert last_failure() == {}


@pytest.mark.parametrize("reason", ["network_timeout", "network_error"])
def test_transport_failures_keep_their_reason(monkeypatch, reason):
    output = {"error": "proxy request failed", "reason": reason}
    monkeypatch.setattr("gss_core.sentinel._run_bridge", lambda *args:
                        SimpleNamespace(stdout=json.dumps(output).encode(), stderr=b"", returncode=0))
    assert call() == {}
    assert last_failure()["reason"] == reason


def test_overall_timeout_preserves_reported_network_timeout(monkeypatch):
    def run(*args):
        raise subprocess.TimeoutExpired("node", 90, stderr=b"sentinel_transport_failed reason=network_timeout timeout_ms=30000\n")
    monkeypatch.setattr("gss_core.sentinel._run_bridge", run)
    assert call() == {}
    assert last_failure()["reason"] == "network_timeout"


def test_network_deadline_has_margin_before_outer_timeout(monkeypatch):
    def run(payload, timeout_s, env):
        assert timeout_s == 30
        assert json.loads(payload)["requestTimeoutMs"] == 25000
        return SimpleNamespace(stdout=b'{"error":"fixture"}', stderr=b"", returncode=0)
    monkeypatch.setattr("gss_core.sentinel._run_bridge", run)
    mint(flow="fixture", device_id="fixture", user_agent="fixture", timeout_s=30)


@pytest.mark.skipif(not shutil.which("node"), reason="Node runtime not installed")
def test_overall_timeout_stops_node_descendants(monkeypatch, tmp_path):
    from gss_core import sentinel
    beat = tmp_path / "heartbeat"
    bridge = tmp_path / "hang.cjs"
    heartbeat_code = f"setInterval(()=>require('node:fs').appendFileSync({json.dumps(str(beat))},'x'),50)"
    bridge.write_text("const {spawn}=require('node:child_process');"
                      f"spawn(process.execPath,['-e',{json.dumps(heartbeat_code)}],"
                      "{stdio:'inherit',windowsHide:true});setInterval(()=>{},1000);", encoding="utf-8")
    monkeypatch.setattr(sentinel, "_BRIDGE", bridge)
    monkeypatch.setenv("SENTINEL_NODE", shutil.which("node"))
    with pytest.raises(subprocess.TimeoutExpired):
        sentinel._run_bridge(b"{}", 1.5, os.environ.copy())
    before = beat.stat().st_size
    time.sleep(0.25)
    assert beat.stat().st_size == before
