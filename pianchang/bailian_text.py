"""Structured JSON generation through the configured text route.

The native Bailian path uses the standard library. Other accounts can use the
LiteLLM transport. Both paths validate one complete JSON object per request.

References checked 2026-11-04:
https://help.aliyun.com/zh/model-studio/qwen-structured-output
https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
"""
from __future__ import annotations

from trace_system import TraceError, event, register_secret, traced
from pianchang.bailian_video import _trace_http_request, _trace_http_response

import http.client
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib import error, parse, request

from pianchang.bailian_video import VideoError, _NoRedirect, _read_env

if TYPE_CHECKING:
    from pianchang.model_routes import ModelRoutes

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class TextError(Exception):
    """A safe-to-display error, with provider tracing information when available."""

    def __init__(self, message, *, code=None, request_id=None, http_status=None, usage=None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.http_status = http_status
        self.usage = usage


def _base_url(value, *, expected_path="/compatible-mode/v1", name="BAILIAN_TEXT_BASE_URL"):
    try:
        url = parse.urlsplit(value)
        valid = (url.scheme == "https" and bool(url.hostname) and not url.username
                 and not url.password and not url.query and not url.fragment
                 and url.path.rstrip("/") == expected_path
                 and not any(c.isspace() for c in value))
        _ = url.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise TextError(f"{name} 必须是以 {expected_path} 结尾的 HTTPS 地址，不能包含凭证或查询参数。")
    return value.rstrip("/")


@dataclass(frozen=True)
class TextSettings:
    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = "qwen-plus"
    max_tokens: int = 8192
    request_timeout: float = 180.0
    env_file: Path | None = field(default=None, repr=False, compare=False)
    routes: ModelRoutes | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class TextConfig(TextSettings):

    def __post_init__(self):
        if not isinstance(self.api_key, str) or any(c in self.api_key for c in "\r\n"):
            raise TextError("DASHSCOPE_API_KEY 必须是单行字符串。")
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "base_url", _base_url(self.base_url))
        if not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", self.model):
            raise TextError("BAILIAN_TEXT_MODEL 必须是有效的模型名称。")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise TextError("BAILIAN_TEXT_MAX_TOKENS 必须是正整数。")
        if (type(self.request_timeout) not in (int, float)
                or not math.isfinite(self.request_timeout) or self.request_timeout <= 0):
            raise TextError("BAILIAN_TEXT_REQUEST_TIMEOUT 必须是有限正数。")


def load_text_config(env_file=None, *, allow_missing=False, routes=None):
    """Load .env independently of video-model settings; process variables win.

    qwen-plus and qwen-plus-2025-12-01 support the selected JSON/non-thinking
    protocol. Other configured models must support those options in the user's
    region; accepting a model name here does not establish service availability.
    """
    path = Path(env_file).resolve() if env_file is not None else ROOT / ".env"
    try:
        values = _read_env(path) if path.exists() or (env_file is not None and not allow_missing) else {}
    except VideoError as exc:
        raise TextError(str(exc)) from None
    values.update(os.environ)
    if "BAILIAN_TEXT_BASE_URL" in values:
        endpoint = values["BAILIAN_TEXT_BASE_URL"].strip()
    elif "DASHSCOPE_BASE_URL" in values:
        video_endpoint = _base_url(values["DASHSCOPE_BASE_URL"].strip(),
                                   expected_path="/api/v1", name="DASHSCOPE_BASE_URL")
        parts = parse.urlsplit(video_endpoint)
        endpoint = parse.urlunsplit((parts.scheme, parts.netloc, "/compatible-mode/v1", "", ""))
    else:
        endpoint = DEFAULT_BASE_URL
    try:
        max_tokens = int(values.get("BAILIAN_TEXT_MAX_TOKENS", "8192").strip())
    except ValueError:
        raise TextError("BAILIAN_TEXT_MAX_TOKENS 必须是正整数。") from None
    try:
        timeout = float(values.get("BAILIAN_TEXT_REQUEST_TIMEOUT", "180").strip())
    except ValueError:
        raise TextError("BAILIAN_TEXT_REQUEST_TIMEOUT 必须是有限正数。") from None
    return TextConfig(
        env_file=path, routes=routes,
        api_key=values.get("DASHSCOPE_API_KEY", "").strip(),
        base_url=endpoint,
        model=values.get("BAILIAN_TEXT_MODEL", "qwen-plus").strip(),
        max_tokens=max_tokens,
        request_timeout=timeout,
    )


def configured_text_routes(config):
    """Standalone text commands remain independent of media settings without a routes file."""
    if getattr(config, "routes", None) is not None:
        return config.routes
    env_file = getattr(config, "env_file", None)
    if env_file is not None and (env_file.parent / "config" / "models.json").is_file():
        from pianchang.model_routes import load_routes
        return load_routes(env_file)
    return None


def text_client_for(client, purpose):
    """Route production helpers while leaving explicitly injected clients untouched."""
    if not isinstance(client, BailianTextClient):
        return client
    routes = client.routes or configured_text_routes(client.base_config)
    if routes and client.purpose == purpose:
        return client
    return BailianTextClient(client.base_config, purpose=purpose, routes=routes) if routes else client


def routed_cache_identity(client):
    if not isinstance(client, BailianTextClient) or client.route is None:
        return None
    route = client.route
    if (route.adapter == "bailian" and route.connection == "bailian.chat"
            and route.model_id == client.base_config.model):
        return None
    return route.snapshot()


def _messages(messages):
    if not isinstance(messages, list) or not messages:
        raise TextError("messages 必须是非空消息列表。")
    normalized = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise TextError(f"messages[{index}] 必须仅包含 role 和文本 content。")
        if message["role"] not in ("system", "user", "assistant"):
            raise TextError(f"messages[{index}].role 必须是 system、user 或 assistant。")
        content = message["content"]
        if not isinstance(content, str) or not content.strip():
            raise TextError(f"messages[{index}].content 必须是非空字符串。")
        normalized.append({"role": message["role"], "content": content})
    if not any(m["role"] == "user" for m in normalized):
        raise TextError("messages 必须包含 user 消息。")
    if not any(m["role"] in ("system", "user") and "json" in m["content"].lower() for m in normalized):
        raise TextError("JSON Object 模式要求 system 或 user 消息明确包含 JSON 输出指导。")
    return normalized


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


class BailianTextClient:
    """A single non-streaming, non-thinking JSON completion per method call."""

    def __init__(self, config, *, purpose=None, routes=None):
        self.base_config = config
        if purpose is not None and routes is None:
            routes = configured_text_routes(config)
        self.purpose, self.routes = purpose, routes
        self.route = routes.resolve(purpose) if routes is not None and purpose is not None else None
        if self.route is not None:
            route = self.route
            settings = TextConfig if route.adapter == "bailian" else TextSettings
            config = settings(api_key=route.api_key, base_url=route.base_url, model=route.model_id,
                              max_tokens=config.max_tokens, request_timeout=config.request_timeout,
                              env_file=getattr(config, "env_file", None), routes=routes)
        self.config = config
        register_secret(config.api_key)
        if self.route is None or self.route.adapter == "bailian":
            from pianchang.bailian_video import _verified_https_handler
            self._opener = request.build_opener(_NoRedirect(),_verified_https_handler())

    def _redact(self, value):
        if isinstance(value, dict):
            return {self._redact(k): self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if not isinstance(value, str):
            return value
        if self.config.api_key:
            value = value.replace(self.config.api_key, "[REDACTED]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)

    def _failure(self, message, *, body=None, code=None, http_status=None, request_id=None):
        body = body if isinstance(body, dict) else {}
        request_id = request_id or body.get("id") or body.get("request_id")
        request_id = self._redact(str(request_id)) if request_id else None
        usage = self._redact(body.get("usage"))
        safe = self._redact(str(message))
        if http_status is not None:
            safe += f" (HTTP {http_status})"
        if request_id:
            safe += f" [request_id={request_id}]"
        return TextError(safe, code=self._redact(str(code)) if code else None,
                         request_id=request_id, http_status=http_status, usage=usage)

    def _provider_failure(self, body, *, http_status=None, request_id=None):
        body = body if isinstance(body, dict) else {}
        details = body.get("error")
        details = details if isinstance(details, dict) else body
        code = details.get("code") or details.get("type") or "APIError"
        provider = self.route.provider if self.route is not None else "百炼"
        message = details.get("message") or "文本 API 请求失败"
        return self._failure(f"{provider} 文本 API 错误：{code}: {message}", body=body, code=code,
                             http_status=http_status, request_id=request_id)

    @traced("bailian.text.complete_json", kind="model")
    def complete_json(self, messages):
        messages = _messages(messages)
        key = self.config.api_key
        if not key or key.lower() in ("your_api_key", "your-api-key", "sk-xxxx", "replace_me"):
            if self.route is None:
                raise TextError("尚未配置 API Key：请填写 .env 的 DASHSCOPE_API_KEY，或设置同名环境变量。")
            raise TextError("所选文本模型尚未配置有效的 API Key。")
        if self.route is not None and self.route.adapter == "litellm":
            from pianchang.sdk_transport import call_sdk
            try:
                body = call_sdk(self.route, "completion", messages=messages,
                                response_format={"type": "json_object"}, stream=False,
                                max_tokens=self.config.max_tokens, timeout=self.config.request_timeout)
            except TraceError:
                raise
            except Exception as exc:
                receipt = getattr(exc, "wire_receipt", {})
                raise self._failure(str(exc), code=type(exc).__name__, http_status=receipt.get("http_status"),
                                    request_id=receipt.get("provider_request_id")) from None
            return self._decode_completion(body, body.get("_provider_request_id"))
        payload = {
            "model": self.config.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "stream": False,
            "max_tokens": self.config.max_tokens,
        }
        req = request.Request(
            self.config.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Accept": "application/json"}, method="POST")
        header_request_id = None
        _trace_http_request(req, self.config.request_timeout)
        try:
            with self._opener.open(req, timeout=self.config.request_timeout) as response:
                header_request_id = response.headers.get("x-request-id")
                raw = response.read(8 * 1024 * 1024 + 1)
                _trace_http_response(response, raw, 8 * 1024 * 1024)
        except error.HTTPError as exc:
            http_status = exc.code
            header_request_id = exc.headers.get("x-request-id") if exc.headers else None
            try:
                raw_error = exc.read(65537)
                _trace_http_response(exc, raw_error, 65536)
                body = json.loads(raw_error.decode("utf-8"))
            except (ValueError, OSError, http.client.HTTPException):
                body = {}
            finally:
                exc.close()
            if 300 <= http_status < 400:
                body = {"error": {"code": "UnexpectedRedirect",
                                   "message": "API 返回重定向，已阻止转发凭证；请检查 BAILIAN_TEXT_BASE_URL。"}}
            raise self._provider_failure(body, http_status=http_status,
                                         request_id=header_request_id) from None
        except (error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            cause = getattr(exc, "reason", exc)
            event("http.transport_error", {"endpoint":self.config.base_url + "/chat/completions",
                  "exception_type":type(exc).__name__, "cause_type":type(cause).__name__,
                  "errno":getattr(cause,"errno",None), "filename":getattr(cause,"filename",None),
                  "message":str(exc), "response_request_id":header_request_id,
                  "provider_outcome":"unknown"}, kind="error")
            raise self._failure(f"百炼文本请求失败：{exc}。未自动重试；服务端可能已处理并计费。",
                                 request_id=header_request_id) from None
        if len(raw) > 8 * 1024 * 1024:
            raise self._failure("百炼文本 API 响应超过大小限制，未作为有效结果接受。",
                                 request_id=header_request_id)
        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, UnicodeError):
            raise self._failure("百炼文本 API 未返回有效 JSON。", request_id=header_request_id) from None
        if not isinstance(body, dict):
            raise self._failure("百炼文本 API 返回了非对象 JSON。", request_id=header_request_id)
        return self._decode_completion(body, header_request_id)

    def _decode_completion(self, body, header_request_id):
        key = self.config.api_key
        if body.get("error") or body.get("code"):
            raise self._provider_failure(body, request_id=header_request_id)
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise self._failure("文本模型响应必须包含一个有效 choice。", body=body,
                                 request_id=header_request_id)
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise self._failure("文本模型响应缺少 message。", body=body, request_id=header_request_id)
        if message.get("refusal"):
            raise self._failure("模型拒绝了本次生成请求，未接受为剧本结果。", body=body,
                                 code="ModelRefusal", request_id=header_request_id)
        reason = choice.get("finish_reason")
        if reason == "length":
            raise self._failure("模型输出达到 max_tokens 上限而被截断；请缩小任务或提高输出上限，未自动重试。",
                                 body=body, code="OutputTruncated", request_id=header_request_id)
        if reason != "stop":
            raise self._failure(f"模型没有正常完成输出（finish_reason={reason}），未接受为剧本结果。",
                                 body=body, code="IncompleteOutput", request_id=header_request_id)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise self._failure("模型返回了空文本，未生成 JSON 对象。", body=body,
                                 request_id=header_request_id)
        if key in content:
            raise self._failure("模型响应意外包含敏感凭证，已拒绝返回该内容。", body=body,
                                 request_id=header_request_id)
        try:
            result = json.loads(content, parse_constant=_reject_constant)
        except (ValueError, UnicodeError):
            raise self._failure("模型输出不是合法 JSON；未截取、修补或自动重试。", body=body,
                                 code="InvalidJSON", request_id=header_request_id) from None
        if not isinstance(result, dict):
            raise self._failure("模型输出必须是 JSON 对象，不能是数组、字符串或空值。", body=body,
                                 code="InvalidJSONObject", request_id=header_request_id)
        if key in json.dumps(result, ensure_ascii=False):
            # JSON unicode escapes must not bypass the credential guard above.
            raise self._failure("模型响应意外包含敏感凭证，已拒绝返回该内容。", body=body,
                                 request_id=header_request_id)
        return {
            "data": result,
            "request_id": self._redact(body.get("id") or body.get("request_id") or header_request_id),
            "model": self._redact(body.get("model")),
            "usage": self._redact(body.get("usage")),
            "finish_reason": reason,
        }
