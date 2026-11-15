"""Single-attempt LiteLLM calls with evidence at the serialized HTTP boundary."""
import hashlib
import json
import os
import ssl
from importlib.metadata import PackageNotFoundError, version
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request

from pianchang.bailian_video import _trace_http_request, _trace_http_response
from pianchang.model_routes import SDK_VERSION
from trace_system import TraceError, current_context, event, register_secret, traced


class SDKSetupError(RuntimeError):
    network_dispatched = False


def _upstream_transport():
    import httpx
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_default_certs()
    return httpx.HTTPTransport(verify=tls, retries=0, trust_env=False)


class _Wire:
    def __init__(self, route, operation, timeout, before_send, task_id):
        self.upstream = _upstream_transport()
        self.origin = urlsplit(route.base_url)
        self.operation = operation
        self.task_id = task_id
        self.timeout = timeout
        self.before_send = before_send
        self.receipt = {}
        self.media_url = None
        self.failure = None
        self.sent = 0

    def close(self):
        self.upstream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def handle_request(self, req):
        if self.failure is not None:
            raise self.failure
        try:
            return self._send(req)
        except Exception as exc:
            # SDKs can wrap transport exceptions. Preserve the original TraceError.
            self.failure = exc
            raise

    def _send(self, req):
        import httpx
        url = str(req.url)
        target = urlsplit(url)
        download = (self.operation == "video_content" and self.sent == 1 and url == self.media_url
                    and self.receipt.get("provider_task_id") == self.task_id)
        same_origin = (target.scheme, target.hostname, target.port or 443) == (
            self.origin.scheme, self.origin.hostname, self.origin.port or 443)
        method = "GET" if self.operation in {"video_status", "video_content"} else "POST"
        if (req.method != method or target.scheme != "https" or target.username is not None
                or not (same_origin or download) or (self.sent and not download)):
            raise ValueError("SDK 请求偏离已配置的供应商、操作或单次提交约束")
        # The OpenAI client may inherit these account headers from the environment.
        # Our route identifies the account exclusively through its configured key.
        for header in ("openai-organization", "openai-project"):
            req.headers.pop(header, None)
        if download:
            for header in ("authorization", "proxy-authorization", "api-key", "x-api-key", "cookie"):
                req.headers.pop(header, None)
        data = req.read() if req.method == "POST" else None
        _trace_http_request(Request(url, method=method, data=data, headers=dict(req.headers)), self.timeout)
        digest = hashlib.sha256(data).hexdigest() if data is not None else None
        if self.before_send:
            self.before_send(method, url, json.loads(data) if data is not None else None, digest)
        self.sent += 1
        if data is not None:
            self.receipt.update(trace_context=current_context(), wire_body_sha256=digest)
        try:
            response = self.upstream.handle_request(req)
            try:
                limit = 512 * 1024**2 if download else 32 * 1024**2
                encoded = response.headers.get("content-encoding", "identity").lower() != "identity"
                wire_digest = None
                source = response
                if encoded and not response.is_stream_consumed:
                    wire_digest = hashlib.sha256()
                    def encoded_chunks():
                        for chunk in response.iter_raw():
                            wire_digest.update(chunk)
                            yield chunk
                    source = httpx.Response(response.status_code, headers=response.headers, content=encoded_chunks())
                buf = bytearray()
                for part in source.iter_bytes():
                    if len(buf) + len(part) > limit:
                        raise ValueError("供应商响应超过本次操作允许的大小")
                    buf.extend(part)
                raw = bytes(buf)
                captured_digest = None
                if wire_digest is not None:
                    captured_digest = wire_digest.hexdigest()
                elif not encoded:
                    captured_digest = hashlib.sha256(raw).hexdigest()
            finally:
                response.close()
        except Exception as exc:
            event("http.error", {"error_type": type(exc).__name__}, kind="http")
            raise
        if download:
            event("http.response", {"http_status": response.status_code, "captured_bytes": len(raw),
                  "captured_wire_sha256": captured_digest, "media_sha256": hashlib.sha256(raw).hexdigest()}, kind="http")
            length = response.headers.get("content-length")
            if length is not None and not response.headers.get("content-encoding"):
                if not length.isdecimal() or int(length) != len(raw):
                    raise ValueError("视频下载长度与供应商响应头不一致")
        else:
            self.receipt["http_status"] = response.status_code
            self.receipt["provider_request_id"] = (response.headers.get("x-request-id")
                                                   or response.headers.get("x-tt-logid"))
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeError):
                obj = None
            if isinstance(obj, dict):
                if self.operation == "image_generation" and isinstance(obj.get("data"), list):
                    for image in obj["data"]:
                        image_url = image.get("url") if isinstance(image, dict) else None
                        if isinstance(image_url, str) and "?" in image_url:
                            register_secret(image_url)
                content = obj.get("content")
                if isinstance(content, dict) and isinstance(content.get("video_url"), str):
                    media_url = content["video_url"]
                    if "?" in media_url:
                        register_secret(media_url)
                    try:
                        self.media_url = str(httpx.URL(media_url))
                    except (ValueError, httpx.InvalidURL):
                        self.media_url = None
                    if self.media_url and "?" in self.media_url:
                        register_secret(self.media_url)
                if not self.receipt["provider_request_id"]:
                    self.receipt["provider_request_id"] = obj.get("request_id")
                if self.operation.startswith("video_"):
                    self.receipt["provider_task_id"] = obj.get("id")
            _trace_http_response(SimpleNamespace(status=response.status_code, headers=response.headers,
                                                 captured_wire_sha256=captured_digest), raw, limit)
        # iter_bytes() decoded the content, so rebuild without the wire encoding.
        headers = [(k, v) for k, v in response.headers.multi_items() if k not in {"content-length", "content-encoding"}]
        return httpx.Response(response.status_code, headers=headers, content=raw, request=req)


@traced("models.sdk.call", kind="model", capture_inputs=False, capture_output=False)
def call_sdk(route, operation, *, timeout, before_send=None, **kwargs):
    if not route.api_key:
        raise SDKSetupError(f"模型连接 {route.connection} 缺少 API Key，请检查配置中的 api_key_env")
    register_secret(route.api_key)
    event("model.route", {"operation": operation, "route": route.snapshot()}, kind="model")
    try:
        installed = version("litellm")
    except PackageNotFoundError:
        installed = None
    if installed != SDK_VERSION:
        raise SDKSetupError(f'请在启动片场的 Python 环境中运行 python -m pip install "litellm=={SDK_VERSION}"')
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    try:
        import httpx
        import litellm
        from openai import OpenAI
        from litellm.llms.custom_httpx.http_handler import HTTPHandler
    except ImportError as exc:
        raise SDKSetupError("无法载入 LiteLLM 或其平台依赖，请检查启动片场的 Python 环境") from exc

    options = dict(api_key=route.api_key, api_base=route.base_url, timeout=timeout)
    wire = _Wire(route, operation, timeout, before_send, kwargs.get("video_id"))
    with httpx.Client(transport=wire, trust_env=False, follow_redirects=False, timeout=timeout,
                      headers={"accept-encoding": "identity"}) as http:
        if operation in {"completion", "image_generation"} and route.sdk_provider == "openai":
            options.update(model=route.sdk_model, client=OpenAI(api_key=route.api_key, base_url=route.base_url,
                           max_retries=0, http_client=http), max_retries=0)
            if operation == "completion":
                options["num_retries"] = 0
        elif operation in {"video_generation", "video_status", "video_content"} and route.sdk_provider == "volcengine":
            options.update(client=HTTPHandler(client=http, timeout=timeout, follow_redirects=False))
            if operation == "video_generation":
                options["model"] = route.sdk_model
            else:
                options["custom_llm_provider"] = route.sdk_provider
        else:
            raise SDKSetupError("SDK 协议尚未适配此模型操作")
        try:
            result = getattr(litellm, operation)(**options, **kwargs)
        except Exception as exc:
            if isinstance(wire.failure, TraceError):
                raise wire.failure from None
            if wire.sent == 0 and not isinstance(exc, TraceError):
                (wire.failure or exc).network_dispatched = False
            if wire.failure is not None:
                raise wire.failure from None
            exc.wire_receipt = wire.receipt.copy()
            raise
        if wire.failure is not None:
            raise wire.failure
        if not wire.sent:
            raise RuntimeError("模型调用缺少实际 HTTP 传输证据")
        if operation == "video_content":
            if wire.sent != 2 or not isinstance(result, bytes):
                raise RuntimeError("无法核对原任务和实际视频下载回执")
            return result
        normalized = result.model_dump()
        normalized.update(_wire_receipt=wire.receipt, _provider_request_id=wire.receipt.get("provider_request_id"),
                          _provider_task_id=wire.receipt.get("provider_task_id"))
        return normalized
