"""Private per-run OpenAI-compatible proxy. Credentials never enter Pi."""
from __future__ import annotations

import json
import secrets
import ssl
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, error

from pianchang.bailian_text import BailianTextClient
from pianchang.bailian_video import _trace_http_request, _trace_http_response
from trace_system import TraceError, current_context, event, register_secret, traced, use_context


class ModelGateway:
    def __init__(self, config, tracer, before_call, *, exchange=None, route=None):
        if route is not None and route.adapter == "bailian":
            config = replace(config, api_key=route.api_key, base_url=route.base_url, model=route.model_id)
        self.config, self.tracer, self.before_call = config, tracer, before_call
        self.route = route
        self.model = route.model_id if route is not None else config.model
        self.api_key = route.api_key if route is not None else config.api_key
        self.context = current_context()
        self.token = secrets.token_hex(32)
        register_secret(self.token); tracer.register_secret(self.token)
        register_secret(self.api_key); tracer.register_secret(self.api_key)
        self.exchange_override = exchange
        self.failure = None
        self.failure_purpose = None
        self.calls = 0

    @traced("pi.model.exchange", kind="model", capture_inputs=False)
    def exchange(self, incoming, purpose="decision"):
        if not self.exchange_override and (not self.api_key or self.api_key.lower() in {"your_api_key","your-api-key","sk-xxxx","replace_me"}):
            raise ValueError("所选对话模型尚未配置有效的 API Key。")
        self.before_call(incoming,purpose=purpose)
        self.calls += 1
        payload = {k: v for k, v in incoming.items() if k in {
            "messages", "tools", "tool_choice", "temperature", "top_p", "parallel_tool_calls"}}
        payload.update(stream=False, max_tokens=self.config.max_tokens)
        if not isinstance(payload.get("messages"), list):
            raise ValueError("Pi model request has no messages")
        if self.route is None or self.route.adapter == "bailian":
            payload.update(model=self.model, enable_thinking=False)
        if self.exchange_override:
            payload["model"] = self.model
            event("pi.model.offline_request", {"payload": payload, "label": "explicit offline mock"}, kind="model")
            result = self.exchange_override(payload)
            event("pi.model.offline_response", {"response": result, "label": "explicit offline mock"}, kind="model")
            return result
        if self.route is not None and self.route.adapter == "litellm":
            from pianchang.sdk_transport import call_sdk
            body = call_sdk(self.route, "completion", timeout=self.config.request_timeout, **payload)
        else:
            body = self._native_exchange(payload)
        if body.get("error") or not body.get("choices"):
            raise ValueError("Pi model provider returned an error; see Trace")
        event("pi.model.receipt", {"model": self.model, "provider_id": body.get("id"),
              "request_id": body.get("_provider_request_id"),
              "usage": body.get("usage"), "finish_reason": body["choices"][0].get("finish_reason")}, kind="model")
        if body["choices"][0].get("finish_reason")=="length":raise ValueError("模型输出达到上限，实际回执已保留；没有把截断内容当作完整工具调用。")
        if body["choices"][0].get("message",{}).get("refusal"):raise ValueError("模型拒绝本次请求，实际拒绝回执见Trace。")
        return body

    def _native_exchange(self, payload):
        client = BailianTextClient(self.config)
        # Explicit verified TLS context avoids process-inherited TLS debug/key-log hooks.
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.load_default_certs()
        from pianchang.bailian_video import _NoRedirect
        client._opener=request.build_opener(_NoRedirect(),request.HTTPSHandler(context=context))
        req = request.Request(self.config.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.config.api_key, "Content-Type": "application/json"}, method="POST")
        _trace_http_request(req, self.config.request_timeout)
        try:
            with client._opener.open(req, timeout=self.config.request_timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                _trace_http_response(response, raw, 8 * 1024 * 1024)
        except error.HTTPError as exc:
            raw = exc.read(65537)
            _trace_http_response(exc, raw, 65536)
            try:details=json.loads(raw).get("error",{})
            except ValueError:details={}
            message=details.get("message","请查看Trace中的实际响应") if isinstance(details,dict) else str(details)
            code=details.get("code",details.get("type","")) if isinstance(details,dict) else ""
            raise ValueError("Pi 百炼调用失败，未自动重试：HTTP " + str(exc.code)+" "+str(code)+" "+str(message)[:2000]) from None
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Pi model response too large; outcome retained")
        return json.loads(raw)

    @traced("pi.model_gateway.start", kind="tool", capture_inputs=False, capture_output=False)
    def start(self):
        gateway = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                purpose="decision"
                try:
                    if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer " + gateway.token:
                        self.send_error(403); return
                    if gateway.failure is not None:
                        self.send_error(409,"Previous operation needs reconciliation; automatic retry is disabled");return
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 4 * 1024 * 1024: raise ValueError("Invalid model request size")
                    incoming = json.loads(self.rfile.read(size))
                    purpose=self.headers.get("X-Drama-Pi-Purpose","decision")
                    if purpose not in {"decision","compaction"}:raise ValueError("Invalid Pi request purpose")
                    with use_context(gateway.context, tracer=gateway.tracer):
                        body = gateway.exchange(incoming,purpose=purpose)
                    body=gateway.tracer.sanitize(body)
                    if incoming.get("stream"):
                        choice = body["choices"][0]
                        delta = dict(choice["message"])
                        delta.pop("reasoning_content", None)
                        for i, call in enumerate(delta.get("tool_calls") or []): call["index"] = i
                        base = {"id": body.get("id", ""), "object": "chat.completion.chunk", "model": body.get("model", gateway.model),
                                "created": body.get("created", 0)}
                        chunks = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                                  {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}],
                                   "usage": body.get("usage", {})}]
                        data = ("".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode("utf-8")
                        mime = "text/event-stream"
                    else: data, mime = json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json"
                    self.send_response(200); self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
                except Exception as exc:
                    gateway.failure = exc
                    gateway.failure_purpose = purpose
                    if not isinstance(exc, TraceError):
                        with use_context(gateway.context, tracer=gateway.tracer):
                            event("pi.model.failure", {"type": type(exc).__name__, "message": str(exc),
                                  "purpose":purpose,"outcome": "unknown unless provider receipt proves otherwise", "automatic_retry": False}, kind="error")
                    self.send_error(500, "Model request stopped; inspect project Trace")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads=True;self.server.timeout=0.25
        self.stop_event=threading.Event()
        def serve():
            while not self.stop_event.is_set():self.server.handle_request()
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        return "http://127.0.0.1:" + str(self.server.server_address[1]) + "/v1"

    @traced("pi.model_gateway.close", kind="tool", capture_inputs=False)
    def close(self):
        self._release()

    def _release(self):
        """Idempotent resource revocation, also used after a Trace failure."""
        if getattr(self,"_released",False):return
        if getattr(self,"stop_event",None):self.stop_event.set()
        if getattr(self,"thread",None):self.thread.join(timeout=3)
        if getattr(self,"server",None):self.server.server_close()
        self._released=True
