"""Resolve a server-owned purpose to one model, endpoint and account."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from pianchang.bailian_observer import load_observer_config
from pianchang.bailian_text import _base_url, load_text_config
from pianchang.bailian_video import ROOT, _read_env, _validate_base_url, load_config
from trace_system import register_secret


SDK_VERSION = "1.109.1"
SDK_PROTOCOLS = {
    "script.decision": "openai", "video.decision": "openai", "story.compile": "openai",
    "reference.prompt": "openai", "voice.casting": "openai", "video.repair_strategy": "openai",
    "reference.image": "openai", "voice.synthesis": "openai",
    "video.text_to_video": "volcengine", "video.reference_to_video": "volcengine",
}
OPENAI_REVIEW_PURPOSES = {"vision.review", "voice.review", "continuity.review"}
DEFAULT_ROUTES = {
    "script.decision": "bailian.text",
    "video.decision": "bailian.text",
    "story.compile": "bailian.text",
    "reference.prompt": "bailian.text",
    "voice.casting": "bailian.text",
    "video.repair_strategy": "bailian.text",
    "reference.image": "bailian.image",
    "video.text_to_video": "bailian.video",
    "video.reference_to_video": "bailian.reference_video",
    "vision.review": "bailian.vision",
    "continuity.review": "bailian.listener",
    "voice.synthesis": "bailian.tts",
    "voice.review": "bailian.listener",
}


@dataclass(frozen=True)
class ModelRoute:
    adapter: str
    provider: str
    account: str
    base_url: str
    model_id: str
    api_key: str = field(repr=False, compare=False)
    sdk_provider: str = ""
    connection: str = ""
    image_size: str | None = None
    image_response_format: str | None = None

    @property
    def sdk_model(self):
        return f"{self.sdk_provider}/{self.model_id}" if self.sdk_provider else self.model_id

    def snapshot(self):
        return {"adapter": self.adapter, "adapter_version": SDK_VERSION if self.adapter == "litellm" else "1",
                "provider": self.provider, "account": self.account, "base_url": self.base_url,
                "model_id": self.model_id, "sdk_provider": self.sdk_provider, "connection": self.connection,
                **({"image_size": self.image_size} if self.image_size is not None else {}),
                **({"image_response_format": self.image_response_format} if self.image_response_format is not None else {})}


class ModelRoutes:
    def __init__(self, models, routes, connections, env_names, disabled_providers=()):
        self.models = MappingProxyType(models.copy())
        self.routes = MappingProxyType(routes.copy())
        self._connections = MappingProxyType(connections.copy())
        self.credential_env_names = frozenset(env_names)
        self.disabled_providers = frozenset(disabled_providers)

    def require_provider(self, provider):
        if provider in self.disabled_providers:
            raise ValueError(f"模型供应商 {provider} 已在 config/models.json 中禁用。")

    def resolve(self, purpose):
        if purpose not in self.routes:
            raise ValueError(f"没有模型用途 {purpose}")
        return self.models[self.routes[purpose]]

    def restore(self, snapshot):
        """Resolve credentials for a frozen connection, independent of today's default model."""
        if not isinstance(snapshot, dict):
            raise ValueError("模型路由快照无效")
        connection = self._connections.get(snapshot.get("connection"))
        if connection is None or any(snapshot.get(k) != getattr(connection, k) for k in
                                     ("provider", "account", "base_url")):
            raise ValueError("冻结任务的供应商、账户或端点已不可用，请恢复原连接后查询")
        adapter = snapshot.get("adapter")
        self.require_provider(connection.provider)
        if adapter not in {"bailian", "litellm", "openai"}:
            raise ValueError("冻结任务使用未知的模型适配器")
        sdk = _name(snapshot.get("sdk_provider"), "sdk_provider") if adapter == "litellm" else ""
        route = replace(connection, adapter=adapter, model_id=_name(snapshot.get("model_id"), "model_id"),
                        sdk_provider=sdk, **_image_options(snapshot, adapter, sdk))
        if route.snapshot() != snapshot:
            raise ValueError("冻结任务的适配器或配置版本不匹配")
        return route

    def secrets(self):
        return {c.api_key for c in self._connections.values() if c.api_key}

    def describe(self):
        info = {}
        for purpose, alias in self.routes.items():
            route = self.models[alias]
            info[purpose] = {"provider": route.provider, "model": route.model_id, "adapter": route.adapter,
                             "key_configured": bool(route.api_key)}
        return info


def _name(value, field_name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}", value):
        raise ValueError(f"模型配置 {field_name} 无效")
    return value


def _fields(obj, required, optional=()):
    if not isinstance(obj, dict) or not set(required) <= obj.keys() or obj.keys() - set(required) - set(optional):
        raise ValueError("模型配置缺少必填字段或有未知字段，需要 " + ", ".join(required))
    return obj


def _image_options(model, adapter, sdk):
    options = {key: model[key] for key in ("image_size", "image_response_format") if key in model}
    if options and (adapter != "litellm" or sdk != "openai"):
        raise ValueError(f"{', '.join(options)} 只适用于 OpenAI 兼容 Images 接口。")
    if "image_size" in options:
        size = options["image_size"]
        if not isinstance(size, str) or not re.fullmatch(r"(?:auto|[1-9]\d?(?:\.\d)?K|[1-9]\d{0,4}x[1-9]\d{0,4})", size):
            raise ValueError("image_size 需要 Images 接口支持的尺寸，如 1024x1024、2048x2048 或 2K。")
    if "image_response_format" in options and options["image_response_format"] not in ("url", "b64_json"):
        raise ValueError("image_response_format 需要使用 url 或 b64_json。")
    return options


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"模型配置含重复字段 {key}")
        result[key] = value
    return result


def load_routes(env_file=None):
    path = Path(env_file).resolve() if env_file is not None else ROOT / ".env"
    cfg_path = path.parent / "config" / "models.json"
    text = load_text_config(env_file, allow_missing=True)
    video = load_config(env_file, allow_missing=True)
    vision = load_observer_config(env_file, resolve_routes=False, allow_missing=True)
    media = ModelRoute("bailian", "aliyun-bailian", "default", video.base_url, video.model,
                       video.api_key, connection="bailian.media")
    chat = replace(media, model_id=text.model, api_key=text.api_key, base_url=text.base_url,
                   connection="bailian.chat")
    models = {"bailian.text": chat, "bailian.video": media,
              "bailian.reference_video": replace(media, model_id=video.reference_model or video.model),
              "bailian.vision": replace(chat, model_id=vision.model),
              "bailian.image": replace(media, model_id="qwen-image-2.0"),
              "bailian.tts": replace(media, model_id="qwen3-tts-flash"),
              "bailian.listener": replace(chat, model_id="qwen3.8-omni-flash")}
    connections = {r.connection: r for r in (media, chat)}
    env_names = {"DASHSCOPE_API_KEY"}
    routes = dict(DEFAULT_ROUTES)
    disabled = []
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique)
        _fields(cfg, ("version",), ("extends", "connections", "models", "routes", "disabled_providers"))
        if type(cfg["version"]) is not int or cfg["version"] != 1 or cfg.get("extends", "bailian") != "bailian":
            raise ValueError("config/models.json 仅支持 version 1 与 extends bailian")
        for section in ("connections", "models", "routes"):
            if not isinstance(cfg.get(section, {}), dict):
                raise ValueError(f"模型配置 {section} 必须是对象")
        disabled = cfg.get("disabled_providers", [])
        if not isinstance(disabled, list):
            raise ValueError("模型配置 disabled_providers 必须是供应商名称列表")
        for provider in disabled:
            _name(provider, "disabled_providers")
        values = _read_env(path) if path.exists() else {}
        values.update(os.environ)
        for alias, c in cfg.get("connections", {}).items():
            _name(alias, "connection")
            _fields(c, ("provider", "account", "base_url", "api_key_env"))
            if alias in connections:
                raise ValueError(f"连接名称已保留：{alias}")
            url = c["base_url"]
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment or any(ch.isspace() for ch in url)):
                raise ValueError(f"连接 {alias} 需要不含认证信息和查询参数的 HTTPS Base URL")
            key_name = c["api_key_env"]
            if not isinstance(key_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key_name):
                raise ValueError(f"连接 {alias} 的 api_key_env 无效")
            env_names.add(key_name)
            key = values.get(key_name, "").strip()
            if "\n" in key or "\r" in key:
                raise ValueError(f"连接 {alias} 的密钥必须为单行")
            connections[alias] = ModelRoute("litellm", _name(c["provider"], "provider"), _name(c["account"], "account"),
                                            url.rstrip("/"), "", key, connection=alias)
        for alias, m in cfg.get("models", {}).items():
            _name(alias, "model")
            _fields(m, ("connection", "adapter", "model_id"), ("sdk_provider", "image_size", "image_response_format"))
            if alias in models:
                raise ValueError(f"模型别名已保留：{alias}")
            conn = connections.get(_name(m["connection"], "connection"))
            if conn is None:
                raise ValueError(f"模型 {alias} 引用的连接不存在")
            adapter = m["adapter"]
            if adapter == "litellm":
                sdk = _name(m.get("sdk_provider"), "sdk_provider")
            elif adapter == "bailian" and conn.provider == "aliyun-bailian" and "sdk_provider" not in m:
                sdk = ""
            elif adapter == "openai" and conn.provider == "openai" and "sdk_provider" not in m:
                sdk = ""
            else:
                raise ValueError(f"模型 {alias} 的 adapter 或 sdk_provider 无效")
            models[alias] = replace(conn, adapter=adapter, sdk_provider=sdk, model_id=_name(m["model_id"], "model_id"),
                                    **_image_options(m, adapter, sdk))
        for purpose, alias in cfg.get("routes", {}).items():
            if purpose not in routes or not isinstance(alias, str) or alias not in models:
                raise ValueError(f"模型用途 {purpose} 或其模型别名无效")
            model = models[alias]
            if purpose != "reference.image" and (model.image_size is not None or model.image_response_format is not None):
                raise ValueError(f"用途 {purpose} 不能使用带 image_size 或 image_response_format 的生图模型别名")
            if model.adapter == "litellm":
                if model.sdk_provider != SDK_PROTOCOLS.get(purpose):
                    raise ValueError(f"用途 {purpose} 未适配 SDK 协议 {model.sdk_provider}")
            elif model.adapter == "openai":
                if purpose not in OPENAI_REVIEW_PURPOSES:
                    raise ValueError(f"用途 {purpose} 未适配 OpenAI 原生审查接口")
            elif purpose in {"video.text_to_video", "video.reference_to_video", "voice.synthesis", "reference.image"}:
                _validate_base_url(model.base_url)
            else:
                _base_url(model.base_url)
            routes[purpose] = alias
    registry = ModelRoutes(models, routes, connections, env_names, disabled)
    for purpose, alias in routes.items():
        if models[alias].provider in registry.disabled_providers:
            raise ValueError(f"用途 {purpose} 仍指向已禁用的供应商 {models[alias].provider}，请配置可用模型。")
    for secret in registry.secrets():
        register_secret(secret)
    return registry
