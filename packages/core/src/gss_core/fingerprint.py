"""Stripe device fingerprint registration (m.stripe.com/6).

ChatGPT/Stripe 的 Elements 路径要求先注册设备指纹，拿到 guid/muid/sid，
后续 payment_methods / confirm 才能通过风控。缺失这三个 ID 时请求仍会发出，
但会被判定为无设备上下文。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import string
import time
import urllib.parse
import uuid
from typing import Any, Dict

logger = logging.getLogger("gss.fingerprint")

_FINGERPRINT_URL = "https://m.stripe.com/6"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_PLUGINS = (
    "PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf"
)
_CANVAS = [
    "0100100101111111101111101111111001110010110111110111111",
    "0100100101111111101111101111111001110010110111110111110",
    "0100100101111111101111101111111001110010110111110111101",
]
_AUDIO = [
    "d331ca493eb692cfcd19ae5db713ad4b",
    "a7c5f72e1b3d4e8f9c0d2a6b7e8f1c3d",
    "e4b8d6f2a0c3d5e7f9b1c3d5e7f9a0b2",
]


def _seg(n: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()


def _encode(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.b64encode(urllib.parse.quote(raw, safe="").encode()).decode()


def _local_ids() -> Dict[str, str]:
    def one() -> str:
        return str(uuid.uuid4()).replace("-", "") + uuid.uuid4().hex[:6]
    return {"guid": one(), "muid": one(), "sid": one()}


def register_device(session, *, timeout: float = 10.0, profile=None) -> Dict[str, str]:
    """Register a device fingerprint; falls back to local ids when unreachable."""
    if profile is not None:
        from .har_telemetry import StripeTelemetry, TelemetryContext
        telemetry = StripeTelemetry(session, TelemetryContext(device=profile, key="", currency="", amount=0))
        telemetry.register()
        return {"guid": profile.guid, "muid": profile.muid, "sid": profile.sid}
    ids = _local_ids()
    fp_id = uuid.uuid4().hex
    sw, sh, dpr = random.choice([(1920, 1080, 1), (1536, 864, 1.25), (2560, 1440, 1)])
    vh = sh - random.randint(40, 70)
    cpu = random.choice([4, 8, 12, 16])
    canvas_fp = random.choice(_CANVAS)
    audio_fp = random.choice(_AUDIO)

    def full(v2: int, with_ids: bool) -> Dict[str, Any]:
        s = [_seg() for _ in range(5)]
        return {
            "v2": v2, "id": fp_id, "t": round(random.uniform(3, 120), 1),
            "tag": "$npm_package_version", "src": "js",
            "a": {
                "a": {"v": "true", "t": 0}, "b": {"v": "true", "t": 0},
                "c": {"v": "en-US", "t": 0}, "d": {"v": "Win32", "t": 0},
                "e": {"v": _PLUGINS, "t": round(random.uniform(0, 0.5), 1)},
                "f": {"v": f"{sw}w_{vh}h_24d_{dpr}r", "t": 0},
                "g": {"v": str(cpu), "t": 0}, "h": {"v": "false", "t": 0},
                "i": {"v": "sessionStorage-enabled, localStorage-enabled", "t": round(random.uniform(0.5, 2), 1)},
                "j": {"v": canvas_fp, "t": round(random.uniform(5, 120), 1)},
                "k": {"v": "", "t": 0}, "l": {"v": _UA, "t": 0}, "m": {"v": "", "t": 0},
                "n": {"v": "false", "t": round(random.uniform(3, 50), 1)},
                "o": {"v": audio_fp, "t": round(random.uniform(20, 30), 1)},
            },
            "b": {
                "a": f"https://{s[0]}.{s[1]}.{s[2]}/",
                "b": f"https://{s[0]}.{s[2]}/{s[3]}/{s[4]}/{_seg()}",
                "c": _seg(),
                "d": ids["muid"] if with_ids else "NA",
                "e": ids["sid"] if with_ids else "NA",
                "f": False, "g": True, "h": True, "i": ["location"], "j": [],
                "n": round(random.uniform(800, 2000), 1),
                "u": "chatgpt.com", "v": "auth.openai.com",
                "w": f"{int(time.time() * 1000)}:{hashlib.sha256(os.urandom(32)).hexdigest()}",
            },
            "h": os.urandom(10).hex(),
        }

    def mouse(source: str) -> Dict[str, Any]:
        return {
            "muid": ids["muid"], "sid": ids["sid"],
            "url": f"https://{_seg()}.{_seg()}/{_seg()}/{_seg()}/{_seg()}",
            "source": source,
            "data": [random.randint(1, 8) for _ in range(10)],
        }

    headers = {
        "User-Agent": _UA, "Content-Type": "text/plain;charset=UTF-8", "Accept": "*/*",
        "Origin": "https://m.stripe.network", "Referer": "https://m.stripe.network/",
    }
    calls = [full(1, False), full(2, True),
             mouse("mouse-timings-10-v2"), mouse("mouse-timings-10")]
    for index, payload in enumerate(calls):
        try:
            response = session.post(_FINGERPRINT_URL, data=_encode(payload), headers=headers, timeout=timeout)
            if int(getattr(response, "status_code", 0) or 0) == 200:
                body = response.json()
                for key in ("guid", "muid", "sid"):
                    value = body.get(key)
                    if isinstance(value, str) and value.strip():
                        ids[key] = value.strip()
        except Exception as exc:  # network failure must not block checkout
            logger.warning("fingerprint_stage_failed index=%s type=%s", index + 1, type(exc).__name__)
    logger.info("fingerprint_registered has_guid=%s has_muid=%s has_sid=%s", bool(ids["guid"]), bool(ids["muid"]), bool(ids["sid"]))
    return ids


def random_ascii(n: int = 16) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))
