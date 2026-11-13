"""Opt-in, local HTTP capture for business APIs and upstream transports."""

from __future__ import annotations

import base64
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache, wraps
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request as CookieRequest
import uuid

logger = logging.getLogger("gss.http_debug")
request_id: ContextVar[str] = ContextVar("http_debug_request_id", default="")
_CAPTURE_ID = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{8}\Z")
_install_lock = threading.Lock()
_installed = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _headers(value: Any) -> list[dict]:
    if value is None:
        return []
    items = value.multi_items() if hasattr(value, "multi_items") else value.items()
    return [{"name": str(key), "value": str(item)} for key, item in items if item is not None]


def _header(headers: list[dict], name: str) -> str:
    return next((item["value"] for item in headers if item["name"].lower() == name), "")


def _content(body: bytes, mime_type: str) -> dict:
    content = {"size": len(body), "mimeType": mime_type}
    try:
        content["text"] = body.decode("utf-8")
    except UnicodeDecodeError:
        content.update(text=base64.b64encode(body).decode("ascii"), encoding="base64")
    return content


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class DebugStore:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self._lock = threading.RLock()
        self.last_error = ""

    def _state(self) -> dict:
        path = self.directory / "settings.json"
        return _read_json(path) if path.is_file() else {"enabled": False, "capture_id": ""}

    def failure(self, error: Exception) -> None:
        self.last_error = f"调试日志写入失败（{type(error).__name__}），请检查磁盘空间和目录权限"
        logger.warning("http_debug_write_failed type=%s", type(error).__name__)

    def set_enabled(self, enabled: bool) -> dict:
        with self._lock:
            state = self._state()
            self.directory.mkdir(parents=True, exist_ok=True)
            if enabled and not state.get("enabled"):
                capture_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
                directory = self.directory / capture_id
                directory.mkdir()
                _atomic_json(directory / "capture.json", {"id": capture_id, "started_at": _now()})
                state["capture_id"] = capture_id
            state["enabled"] = enabled
            _atomic_json(self.directory / "settings.json", state)
            self.last_error = ""
            return self.status()

    def capture_path(self, capture_id: str) -> Path:
        if not _CAPTURE_ID.fullmatch(capture_id):
            raise ValueError("调试记录编号格式不正确")
        directory = self.directory / capture_id
        if directory.is_symlink() or not (directory / "capture.json").is_file():
            raise FileNotFoundError("调试记录不存在")
        return directory

    def target(self) -> Path | None:
        try:
            with self._lock:
                state = self._state()
                return self.capture_path(state["capture_id"]) if state.get("enabled") else None
        except Exception as error:
            self.failure(error)
            return None

    def status(self) -> dict:
        with self._lock:
            state = self._state()
            captures = []
            for directory in sorted(self.directory.glob("*"), reverse=True):
                if not _CAPTURE_ID.fullmatch(directory.name) or not directory.is_dir():
                    continue
                capture = _read_json(directory / "capture.json")
                entries = [_read_json(path) for path in directory.glob("exchange-*.json")]
                capture.update(
                    count=len(entries),
                    pending=sum(item.get("_state") in {"pending", "streaming"} for item in entries),
                    errors=sum(bool(item.get("_error")) or item["response"]["status"] >= 400 for item in entries),
                    size_bytes=sum(path.stat().st_size for path in directory.iterdir() if path.is_file()),
                )
                captures.append(capture)
            return {"enabled": bool(state.get("enabled")), "capture_id": state.get("capture_id", ""),
                    "directory": str(self.directory), "captures": captures, "last_error": self.last_error}

    def har(self, capture_id: str) -> dict:
        directory = self.capture_path(capture_id)
        entries = []
        for path in directory.glob("exchange-*.json"):
            entry = _read_json(path)
            body_path = path.with_suffix(".body")
            if body_path.is_file():
                body = body_path.read_bytes()
                mime = _header(entry["response"]["headers"], "content-type")
                entry["response"]["content"] = _content(body, mime)
                entry["response"]["bodySize"] = len(body)
            entries.append(entry)
        entries.sort(key=lambda item: (item["startedDateTime"], item["_id"]))
        return {"log": {"version": "1.2", "creator": {"name": "gpt-subscribe-studio", "version": "0.1.0"},
                        "entries": entries,
                        "_sentinelBridges": [_read_json(path) for path in directory.glob("bridge-*.json")]}}


@lru_cache(maxsize=8)
def _store(directory: str) -> DebugStore:
    return DebugStore(Path(directory))


def debug_store() -> DebugStore:
    directory = Path(os.environ.get("GSS_DATA_DIR", "runtime")) / "http-debug"
    return _store(str(directory.resolve()))


def sentinel_debug_context() -> dict | None:
    directory = debug_store().target()
    if directory is None:
        return None
    return {"directory": str(directory), "parentRequestId": request_id.get(), "id": uuid.uuid4().hex,
            "startedDateTime": _now(), "startedMonotonic": time.monotonic()}


def capture_sentinel(context: dict | None, payload: bytes, *, process=None, error=None) -> None:
    if context is None:
        return
    try:
        value = {"startedDateTime": context["startedDateTime"], "input": json.loads(payload),
                 "_parentRequestId": context["parentRequestId"], "state": "pending",
                 "time": round((time.monotonic() - context["startedMonotonic"]) * 1000, 3)}
        if process is not None:
            value.update(state="complete", returncode=process.returncode,
                         stdout=(process.stdout or b"").decode("utf-8", "replace"),
                         stderr=(process.stderr or b"").decode("utf-8", "replace"))
        if error is not None:
            value.update(state="error", error={"type": type(error).__name__, "message": str(error)},
                         stdout=(getattr(error, "output", None) or b"").decode("utf-8", "replace"),
                         stderr=(getattr(error, "stderr", None) or b"").decode("utf-8", "replace"))
        _atomic_json(Path(context["directory"]) / f"bridge-{context['id']}.json", value)
    except Exception as capture_error:
        debug_store().failure(capture_error)


class Exchange:
    """Keep response chunks on disk so live SSE streams retain bounded memory."""

    def __init__(self, store: DebugStore, directory: Path, *, method: str, url: str,
                 headers: list[dict], body: bytes, transport: str, direction: str, proxy: Any = ""):
        self.store = store
        self.started = time.monotonic()
        self.finished = False
        self.size = 0
        identifier = uuid.uuid4().hex
        self.path = directory / f"exchange-{identifier}.json"
        request = {"method": method.upper(), "url": url, "httpVersion": "",
                   "headers": headers, "cookies": [], "queryString": [
                       {"name": key, "value": value} for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
                   ], "headersSize": -1, "bodySize": len(body)}
        if body:
            content = _content(body, _header(headers, "content-type"))
            request["postData"] = {"mimeType": content["mimeType"], "text": content["text"]}
            if "encoding" in content:
                request["postData"]["encoding"] = content["encoding"]
        self.entry = {
            "startedDateTime": _now(), "time": 0, "request": request,
            "response": {"status": 0, "statusText": "", "httpVersion": "", "headers": [], "cookies": [],
                         "content": {"size": 0, "mimeType": "", "text": ""}, "redirectURL": "",
                         "headersSize": -1, "bodySize": 0},
            "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
            "_id": identifier, "_parentRequestId": request_id.get(), "_direction": direction,
            "_transport": transport, "_proxy": proxy or "direct", "_state": "pending",
        }
        self.save()

    def save(self) -> None:
        try:
            _atomic_json(self.path, self.entry)
        except Exception as error:
            self.store.failure(error)

    def start_response(self, status: int, headers: list[dict], *, http_version: str = "") -> None:
        response = self.entry["response"]
        response.update(status=int(status), headers=headers, httpVersion=http_version,
                        redirectURL=_header(headers, "location"))
        self.entry["_state"] = "streaming"
        self.save()

    def chunk(self, value: bytes) -> None:
        if not value:
            return
        try:
            with self.path.with_suffix(".body").open("ab") as stream:
                stream.write(value)
            self.size += len(value)
        except Exception as error:
            self.store.failure(error)

    def finish(self, error: BaseException | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        elapsed = round((time.monotonic() - self.started) * 1000, 3)
        self.entry.update(time=elapsed, _state="error" if error else "complete")
        self.entry["timings"]["wait"] = elapsed
        self.entry["response"]["bodySize"] = self.size
        self.entry["response"]["content"]["size"] = self.size
        if error:
            self.entry["_error"] = {"type": type(error).__name__, "message": str(error)}
        self.save()


def begin_exchange(**values: Any) -> Exchange | None:
    store = debug_store()
    directory = store.target()
    if directory is None:
        return None
    try:
        return Exchange(store, directory, **values)
    except Exception as error:
        store.failure(error)
        return None


def _curl_request(session: Any, method: str, url: str, values: dict) -> Exchange | None:
    if debug_store().target() is None:
        return None
    from curl_cffi.requests.cookies import Cookies
    from curl_cffi.requests.headers import Headers

    headers = Headers(session.headers)
    for key, value in (values.get("headers") or {}).items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    full_url = str(url)
    params = values.get("params")
    if params:
        query = params if isinstance(params, str) else urlencode(params, doseq=True)
        full_url += ("&" if "?" in full_url else "?") + query
    cookies = Cookies(session.cookies)
    cookies.update(values.get("cookies") or {})
    cookie_request = CookieRequest(full_url)
    cookies.jar.add_cookie_header(cookie_request)
    if cookie_request.has_header("Cookie") and "cookie" not in headers:
        headers["cookie"] = cookie_request.get_header("Cookie")
    data = values.get("data")
    if values.get("json") is not None:
        body = json.dumps(values["json"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers.setdefault("content-type", "application/json")
    elif isinstance(data, bytes):
        body = data
    elif isinstance(data, str):
        body = data.encode("utf-8")
    elif data is not None:
        body = urlencode(data, doseq=True).encode("utf-8")
        headers.setdefault("content-type", "application/x-www-form-urlencoded")
    else:
        body = b""
    proxies = values.get("proxies") or session.proxies
    proxy = values.get("proxy") or (proxies or {}).get(urlsplit(full_url).scheme, "")
    return begin_exchange(method=method, url=full_url, headers=_headers(headers), body=body,
                          transport="curl_cffi", direction="upstream", proxy=proxy)


def install_transport_capture() -> None:
    """Install once. Disabled capture leaves request/response consumption untouched."""
    global _installed
    import httpx
    from curl_cffi.requests import Session

    with _install_lock:
        if _installed:
            return
        curl_request = Session.request

        @wraps(curl_request)
        def captured_curl(self, method, url, *args, **kwargs):
            exchange = None
            try:
                exchange = _curl_request(self, method, url, kwargs)
            except Exception as error:
                debug_store().failure(error)
            try:
                response = curl_request(self, method, url, *args, **kwargs)
            except BaseException as error:
                if exchange:
                    exchange.finish(error)
                raise
            if exchange:
                try:
                    exchange.start_response(response.status_code, _headers(response.headers),
                                            http_version=str(response.http_version))
                    outgoing = getattr(response, "request", None)
                    if outgoing is not None:
                        exchange.entry["request"]["_transportHeaders"] = _headers(outgoing.headers)
                    exchange.entry["response"]["_finalUrl"] = str(response.url)
                    if kwargs.get("stream"):
                        original_iter = response.iter_content

                        def chunks(*chunk_args, **chunk_kwargs):
                            try:
                                for chunk in original_iter(*chunk_args, **chunk_kwargs):
                                    exchange.chunk(chunk)
                                    yield chunk
                            except BaseException as error:
                                exchange.finish(error)
                                raise
                            else:
                                exchange.finish()
                        response.iter_content = chunks
                    else:
                        exchange.chunk(response.content)
                        exchange.finish()
                except Exception as error:
                    debug_store().failure(error)
            return response

        Session.request = captured_curl

        class SyncCapture(httpx.SyncByteStream):
            def __init__(self, stream, exchange):
                self.stream, self.exchange = stream, exchange

            def __iter__(self):
                try:
                    for chunk in self.stream:
                        self.exchange.chunk(chunk)
                        yield chunk
                except BaseException as error:
                    self.exchange.finish(error)
                    raise
                else:
                    self.exchange.finish()

            def close(self):
                try:
                    self.stream.close()
                finally:
                    if not self.exchange.finished:
                        self.exchange.finish(RuntimeError("response_closed_before_body_completed"))

        class AsyncCapture(httpx.AsyncByteStream):
            def __init__(self, stream, exchange):
                self.stream, self.exchange = stream, exchange

            async def __aiter__(self):
                try:
                    async for chunk in self.stream:
                        self.exchange.chunk(chunk)
                        yield chunk
                except BaseException as error:
                    self.exchange.finish(error)
                    raise
                else:
                    self.exchange.finish()

            async def aclose(self):
                try:
                    await self.stream.aclose()
                finally:
                    if not self.exchange.finished:
                        self.exchange.finish(RuntimeError("response_closed_before_body_completed"))

        def prepare_httpx_exchange(client, request):
            if type(client).__module__ == "starlette.testclient" or isinstance(client._transport, httpx.ASGITransport):
                return None
            if debug_store().target() is None:
                return None
            try:
                body = request.content
            except httpx.RequestNotRead:
                body = b""
            return begin_exchange(method=request.method, url=str(request.url), headers=_headers(request.headers),
                                  body=body, transport="httpx", direction="upstream",
                                  proxy=getattr(client, "_gss_debug_proxy", "environment"))

        def httpx_exchange(client, request):
            try:
                return prepare_httpx_exchange(client, request)
            except Exception as error:
                debug_store().failure(error)
                return None

        def save_httpx(response, exchange, stream_type):
            try:
                exchange.entry["request"]["httpVersion"] = response.http_version
                exchange.start_response(response.status_code, _headers(response.headers), http_version=response.http_version)
                if response.is_stream_consumed:
                    exchange.chunk(response.content)
                    exchange.finish()
                else:
                    response.stream = stream_type(response.stream, exchange)
            except Exception as error:
                debug_store().failure(error)

        sync_send = httpx.Client._send_single_request
        async_send = httpx.AsyncClient._send_single_request

        @wraps(sync_send)
        def captured_send(self, request):
            exchange = httpx_exchange(self, request)
            try:
                response = sync_send(self, request)
            except BaseException as error:
                if exchange:
                    exchange.finish(error)
                raise
            if exchange:
                save_httpx(response, exchange, SyncCapture)
            return response

        @wraps(async_send)
        async def captured_async_send(self, request):
            exchange = httpx_exchange(self, request)
            try:
                response = await async_send(self, request)
            except BaseException as error:
                if exchange:
                    exchange.finish(error)
                raise
            if exchange:
                save_httpx(response, exchange, AsyncCapture)
            return response

        httpx.Client._send_single_request = captured_send
        httpx.AsyncClient._send_single_request = captured_async_send

        def capture_init(original):
            @wraps(original)
            def initialize(self, *args, **kwargs):
                original(self, *args, **kwargs)
                self._gss_debug_proxy = str(kwargs.get("proxy") or ("environment" if kwargs.get("trust_env", True) else "direct"))
            return initialize

        httpx.Client.__init__ = capture_init(httpx.Client.__init__)
        httpx.AsyncClient.__init__ = capture_init(httpx.AsyncClient.__init__)
        _installed = True


class HttpDebugMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or path.startswith("/api/settings/debug"):
            return await self.app(scope, receive, send)
        directory = debug_store().target()
        if directory is None:
            return await self.app(scope, receive, send)
        from starlette.datastructures import URL
        exchange = begin_exchange(method=scope["method"], url=str(URL(scope=scope)),
                                  headers=[{"name": key.decode("latin-1"), "value": value.decode("latin-1")}
                                           for key, value in scope["headers"]],
                                  body=b"", transport="asgi", direction="inbound")
        if exchange is None:
            return await self.app(scope, receive, send)
        scope["gss.http_debug.exchange"] = exchange
        context = request_id.set(exchange.entry["_id"])
        body = bytearray()

        async def captured_receive():
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if not message.get("more_body"):
                    content = _content(bytes(body), _header(exchange.entry["request"]["headers"], "content-type"))
                    exchange.entry["request"].update(bodySize=len(body), postData={
                        "mimeType": content["mimeType"], "text": content["text"],
                        **({"encoding": content["encoding"]} if "encoding" in content else {}),
                    })
                    exchange.save()
            return message

        async def captured_send(message):
            if message["type"] == "http.response.start":
                exchange.start_response(message["status"], [
                    {"name": key.decode("latin-1"), "value": value.decode("latin-1")}
                    for key, value in message.get("headers", [])
                ], http_version="HTTP/" + scope.get("http_version", "1.1"))
            elif message["type"] == "http.response.body":
                exchange.chunk(message.get("body", b""))
                if not message.get("more_body"):
                    exchange.finish()
            await send(message)

        try:
            await self.app(scope, captured_receive, captured_send)
        except BaseException as error:
            exchange.finish(error)
            raise
        finally:
            if not exchange.finished:
                exchange.finish(RuntimeError("request_disconnected_before_response_completed"))
            request_id.reset(context)


def capture_error_response(scope: dict, response: Any, error: Exception) -> None:
    exchange = scope.get("gss.http_debug.exchange")
    if exchange is None or exchange.entry["response"]["status"]:
        return
    exchange.start_response(response.status_code, _headers(response.headers))
    exchange.chunk(response.body)
    exchange.finished = False
    exchange.finish(error)
