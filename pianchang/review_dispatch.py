"""Observe urllib's first origin write and reopen only the matching unsent review."""
import hashlib
import http.client
import os
import re
import ssl
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib import request

from trace_system import current_context, event, get_tracer


class ReviewNotDispatched(ValueError):
    """The next review request did not reach the provider."""

    def __init__(self, message, *, wire=None):
        super().__init__(message)
        self.wire = wire


_active_call = ContextVar("active_review_call", default=None)
_scope = ContextVar("review_retry_scope", default=None)


class ReviewOutOfScope(ReviewNotDispatched):
    """This scoped recheck may read this cache but cannot start the call."""


@contextmanager
def retry_review_scope(allowed):
    """Restrict this worker to exact call paths and the independently selected attempt IDs."""
    if any(not Path(path).is_absolute() or not re.fullmatch("[a-f0-9]{32}", str(attempt))
           for path, attempt in allowed.items()):
        raise ValueError("复验请求必须引用实际调用路径和原尝试标识。")
    approved = {str(Path(path).resolve()): attempt for path, attempt in allowed.items()}
    token = _scope.set(approved)
    try:
        yield
    finally:
        _scope.reset(token)


def require_unscoped_call():
    if _scope.get() is not None:
        raise ReviewOutOfScope("本次复验仅补做原委托已核实的观察。")


class ReviewDispatch:
    def __init__(self, req):
        self.url = req.full_url
        self.connections = 0
        self.origin_write = False
        self.failure = None
        self.binding = None
        self.wire = None
        active = _active_call.get()
        if active is None:
            require_unscoped_call()
        else:
            from pianchang.video_collab_agent import _write
            path, record, previous_wire = active
            if "wire" in record:
                raise ValueError("同一观察已绑定一次 HTTP 请求，无法再次发送。")
            ctx = current_context()
            self.binding = {**record["attempt"], "method": req.get_method(), "url": self.url,
                            "wire_body_sha256": hashlib.sha256(req.data).hexdigest()}
            if previous_wire and any(previous_wire.get(k) != self.binding[k] for k in ("method", "url", "wire_body_sha256")):
                raise ReviewNotDispatched("本次实际观察请求与确证未发送的原请求不一致，等待核对。")
            self.wire = {"trace_id": ctx["trace_id"], "span_id": ctx["span_id"], **self.binding}
            record["wire"] = self.wire
            _write(path, record)
            event("review.http_bound", self.binding, kind="http")
        req._review_dispatch = self

    def error(self, exc):
        reason = getattr(exc, "reason", exc)
        unsent = self.connections == 1 and not self.origin_write and self.failure is not None and self.failure is reason
        phase = ("start_tls" if isinstance(reason, ssl.SSLError) else "https_connect") if unsent else "unknown"
        event("http.error", {"dispatch": "not_sent" if unsent else "unknown", "phase": phase,
              "error_type": type(reason).__name__, "connections": self.connections,
              "origin_write_started": self.origin_write, "review": self.binding}, kind="http")
        return unsent


class _ReviewHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, dispatch, **kwargs):
        super().__init__(host, **kwargs)
        self.dispatch = dispatch
        self.connecting = False

    def connect(self):
        self.dispatch.connections += 1
        event("http.connect_started", {"connection": self.dispatch.connections}, kind="http")
        self.connecting = True
        try:
            # HTTPS proxy CONNECT is part of setup. Its bytes cannot contain the origin POST.
            super().connect()
        except (OSError, http.client.HTTPException) as exc:
            self.dispatch.failure = exc
            raise
        finally:
            self.connecting = False

    def send(self, data):
        if not self.connecting:
            if self.sock is None and self.auto_open:
                self.connect()
            if not self.dispatch.origin_write:
                self.dispatch.origin_write = True
                event("http.origin_write", {"connection": self.dispatch.connections}, kind="http")
        return super().send(data)


class ReviewHTTPSHandler(request.HTTPSHandler):
    def https_open(self, req):
        dispatch = getattr(req, "_review_dispatch", None)
        if dispatch is None or dispatch.url != req.full_url or req.get_method() != "POST":
            return super().https_open(req)
        def connection(host, **kwargs):
            return _ReviewHTTPSConnection(host, dispatch=dispatch, **kwargs)
        return self.do_open(connection, req, context=self._context)


def _verified_unsent(record, *, tracer=None):
    from pianchang.story_spec import digest_json
    wire = record.get("wire") or {}
    attempt = record.get("attempt") or {}
    inputs = record.get("request")
    if (record.get("status") != "submitted_or_unknown" or not isinstance(inputs, dict)
            or record.get("input_sha256") != digest_json(inputs) or attempt.get("input_sha256") != record["input_sha256"]
            or not attempt.get("id") or not wire.get("trace_id") or not wire.get("span_id")
            or not all(wire.get(k) == v for k, v in attempt.items())):
        return False
    tracer = tracer or get_tracer()
    rows = tracer.get_events(wire["trace_id"], span_id=wire["span_id"], limit=8)
    if [(r["kind"], r["name"]) for r in rows] != [
        ("span.start", "bailian.vision.http"), ("http", "review.http_bound"), ("http", "http.request"),
        ("http", "http.connect_started"), ("http", "http.error"), ("span.end", "bailian.vision.http")
    ]:
        return False
    binding, req, connect, error, ended = (tracer.resolve(r["data"]) for r in rows[1:])
    route = inputs.get("audio_review", {}) if attempt.get("purpose") == "continuity.review" else inputs
    return (binding == {k: v for k, v in wire.items() if k not in {"trace_id", "span_id"}}
            and all(binding.get(k) == req.get(k) for k in ("method", "url", "wire_body_sha256"))
            and req["method"] == "POST" and req["url"].startswith("https://")
            and req["url"] == str(route.get("endpoint")) + "/chat/completions"
            and isinstance(route.get("model"), str) and isinstance(req.get("body"), dict)
            and req["body"].get("model") == route["model"]
            and connect == {"connection": 1} and error.get("review") == binding
            and error.get("dispatch") == "not_sent" and error.get("phase") in {"start_tls", "https_connect"}
            and error.get("connections") == 1 and error.get("origin_write_started") is False
            and ended.get("status") == "ERROR"
            and (tracer.resolve(ended.get("error")) or {}).get("type") == "ReviewNotDispatched")


@contextmanager
def _claim(path):
    # Covers both the production loop and the official review worker. Released on process death.
    with path.with_suffix(".lock").open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ReviewNotDispatched("同一观察正在原任务中处理，等待其回执。") from exc
        yield


def _allows_companion(allowed, path, partner, commission_id, *, tracer=None):
    from pianchang.story_spec import digest_json
    from pianchang.video_collab_agent import _read
    if not partner or not commission_id or path.exists():
        return False
    first, inputs = partner
    selected = allowed.get(str(first.resolve()))
    if (not selected or path.parent != first.parent or {path.name, first.name} != {"audio-call.json", "visual-call.json"}
            or not first.is_file()):
        return False
    completed = _read(first)
    wire = completed.get("wire") or {}
    attempt = completed.get("attempt") or {}
    if (completed.get("status") != "completed" or completed.get("retry_of") != selected
            or attempt.get("commission_id") != commission_id or not wire.get("span_id") or wire.get("id") != attempt.get("id")
            or completed.get("input_sha256") != digest_json(inputs)):
        return False
    archive = first.with_name(first.stem + "-" + selected[:12] + ".json")
    original = _read(archive) if archive.is_file() else {}
    linked = ((original.get("attempt") or {}).get("id") == selected and original.get("input_sha256") == completed["input_sha256"]
            and original["attempt"].get("call_path") == str(first.resolve())
            and original["attempt"].get("commission_id") == commission_id and _verified_unsent(original, tracer=tracer)
            and all(wire.get(k) == original["wire"].get(k) for k in ("url", "method", "wire_body_sha256")))
    if not linked:
        return False
    # A new paid companion also needs the SDK receipt of the completed first observation.
    tracer = tracer or get_tracer()
    rows = tracer.get_events(wire["trace_id"], span_id=wire["span_id"], limit=8)
    if [(r["kind"], r["name"]) for r in rows] != [
        ("span.start", "bailian.vision.http"), ("http", "review.http_bound"), ("http", "http.request"),
        ("http", "http.connect_started"), ("http", "http.origin_write"), ("http", "http.response"), ("span.end", "bailian.vision.http")
    ]:
        return False
    bound = tracer.resolve(rows[1]["data"])
    status = tracer.resolve(rows[5]["data"]).get("http_status")
    return (bound == {k: v for k, v in wire.items() if k not in {"trace_id", "span_id"}}
            and type(status) is int and 200 <= status < 300 and tracer.resolve(rows[-1]["data"]).get("status") == "OK")


def one_review_call(path, inputs, purpose, call, *, available, reserve, reserve_first=True, partner=None):
    from pianchang.story_spec import digest_json
    from pianchang.video_collab_agent import _read, _write
    fingerprint = digest_json(inputs)
    identity = {"input_sha256": fingerprint, "call_path": str(path.resolve()), "purpose": purpose}
    with _claim(path):
        previous = _read(path) if path.exists() else None
        if previous:
            if previous.get("input_sha256") != fingerprint or previous.get("request") != get_tracer().sanitize(inputs):
                require_unscoped_call()
                raise ValueError("观察缓存与原始输入不一致。")
            if previous["status"] == "completed":
                event("video.collab.cached_model_result", {"path": str(path), "input_sha256": fingerprint,
                      "new_network_request": False}, kind="cache")
                return previous["response"]
        scope = _scope.get()
        if scope is not None:
            if previous:
                claimed = (previous.get("attempt") or {}).get("id")
                allowed = bool(claimed and claimed == scope.get(identity["call_path"]))
            else:
                allowed = _allows_companion(scope, path, partner, getattr(reserve, "commission_id", None))
            if not allowed:
                raise ReviewOutOfScope("此观察超出本次已核实的复验范围。")
        if previous:
            attempt = previous.get("attempt") or {}
            if (previous["status"] != "submitted_or_unknown" or not attempt.get("id")
                    or not all(attempt.get(k) == v for k, v in identity.items()) or not _verified_unsent(previous)):
                require_unscoped_call()
                raise ValueError("已有未确认的模型调用，已停止重复调用：" + str(path))
            if not attempt.get("commission_id") or attempt["commission_id"] != getattr(reserve, "commission_id", None):
                raise ReviewNotDispatched("原观察确证未发送，等待原委托及其模型额度。", wire=previous["wire"])
        if not available and (reserve_first or previous):
            raise ReviewNotDispatched("该验收所用模型连接缺少 API Key。", wire=previous and previous["wire"])
        if reserve is not None and (previous or reserve_first):
            try:
                reserve(purpose)
            except ValueError as exc:
                raise ReviewNotDispatched(str(exc), wire=previous and previous["wire"]) from exc
        if previous:
            # Keep the old attempt and its independent Trace after replacing this cache entry.
            archive = path.with_name(path.stem + "-" + previous["attempt"]["id"][:12] + ".json")
            if not archive.exists():
                _write(archive, previous)
            elif _read(archive) != previous:
                raise ValueError("历史观察凭据不一致。")
            event("review.retry_proved_unsent", {"wire": previous["wire"], "archive": str(archive)}, kind="cache")
        record = {"request": inputs, "input_sha256": fingerprint, "status": "submitted_or_unknown",
                  "attempt": {**identity, "id": uuid.uuid4().hex, "commission_id": getattr(reserve, "commission_id", None)}}
        if previous:
            record["retry_of"] = previous["attempt"]["id"]
        _write(path, record)
        token = _active_call.set((path, record, previous and previous["wire"]))
        try:
            response = call()
        except ReviewNotDispatched:
            if previous and "wire" not in record:
                _write(path, previous)
            raise
        finally:
            _active_call.reset(token)
        _write(path, {**record, "status": "completed", "response": response})
        return response
