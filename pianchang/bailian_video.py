"""Bailian / DashScope video transport. Python 3.10+, standard library only.

API references (checked 2026-11-04):
https://help.aliyun.com/zh/model-studio/legacy-wan-text-to-video-api-reference
https://help.aliyun.com/zh/model-studio/text-to-video-api-reference
https://help.aliyun.com/zh/model-studio/wan-video-to-video-api-reference

Task persistence and polling live in pianchang/video_agent.py. A submit is never retried
here: a transport timeout can happen after the service accepted a paid job.
"""
from __future__ import annotations

from trace_system import event, register_secret, traced

import base64
import binascii
import hashlib
import http.client
import json
import math
import os
import re
import ssl
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib import error, parse, request

if TYPE_CHECKING:
    from pianchang.model_routes import ModelRoute

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_MODELS = ("wan2.7-r2v", "wan2.7-r2v-2026-06-12")
SUPPORTED_MODELS = ("wan2.6-t2v", "wan2.7-t2v", "wan2.7-t2v-2026-06-12", *REFERENCE_MODELS)
PIN_FIELDS = ("asset_id", "revision_id", "sha256", "media_sha256", "subject_id", "purpose")
IMAGE_PURPOSES = {"portrait", "scene", "prop", "costume"}
RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")
SIZES_26 = {
    "720P": dict(zip(RATIOS, ("1280*720", "720*1280", "960*960", "1088*832", "832*1088"))),
    "1080P": dict(zip(RATIOS, ("1920*1080", "1080*1920", "1440*1440", "1632*1248", "1248*1632"))),
}


class VideoError(Exception):
    """A configuration, transport, or provider error, safe to show to the user."""

    def __init__(self, message, *, code=None, request_id=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.http_status = http_status


@dataclass(frozen=True)
class VideoConfig:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    model: str = "wan2.6-t2v"
    resolution: str = "720P"
    ratio: str = "9:16"
    duration: int = 5
    prompt_extend: bool = True
    watermark: bool = False
    poll_interval: float = 10.0
    wait_timeout: float = 900.0
    request_timeout: float = 60.0
    output_dir: Path = ROOT / "outputs" / "videos"
    reference_model: str | None = None
    env_file: Path | None = field(default=None, repr=False, compare=False)
    route: ModelRoute | None = field(default=None, repr=False)
    route_purpose: str | None = None
    legacy_submission: bool = False

    @property
    def sdk_video(self):
        return self.route is not None and self.route.adapter == "litellm"

    @property
    def max_reference_images(self):
        return 9 if self.sdk_video else 5

    def require_legacy_provider(self):
        if self.env_file is not None and (self.env_file.parent / "config/models.json").is_file():
            from pianchang.model_routes import load_routes
            load_routes(self.env_file).require_provider("aliyun-bailian")

    def __post_init__(self):
        if not isinstance(self.api_key, str) or any(c in self.api_key for c in "\r\n"):
            raise VideoError("DASHSCOPE_API_KEY 必须是单行字符串。")
        if self.route is not None and (self.route.model_id != self.model or self.route.base_url != self.base_url):
            raise VideoError("视频模型或端点与选定的用途路由不符。")
        if self.sdk_video:
            if self.route.sdk_provider != "volcengine":
                raise VideoError(f"当前未适配 {self.route.sdk_provider} 的视频协议与回执。")
        else:
            _validate_base_url(self.base_url)
        if not self.sdk_video and self.model not in SUPPORTED_MODELS:
            raise VideoError("尚未适配该视频模型；支持：" + ", ".join(SUPPORTED_MODELS))
        if self.reference_model is not None and self.reference_model not in REFERENCE_MODELS:
            raise VideoError("BAILIAN_REFERENCE_VIDEO_MODEL 必须是 " + " 或 ".join(REFERENCE_MODELS))
        if self.resolution not in SIZES_26:
            raise VideoError("BAILIAN_VIDEO_RESOLUTION 仅支持 720P 或 1080P。")
        if self.ratio not in RATIOS:
            raise VideoError("BAILIAN_VIDEO_RATIO 仅支持：" + ", ".join(RATIOS))
        _duration(self.duration)
        if type(self.prompt_extend) is not bool or type(self.watermark) is not bool:
            raise VideoError("prompt_extend 和 watermark 必须为布尔值。")
        for name in ("poll_interval", "wait_timeout", "request_timeout"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise VideoError(f"{name} 必须是有限正数。")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "output_dir", Path(self.output_dir))


def _validate_base_url(value):
    try:
        url = parse.urlsplit(value)
        valid = (url.scheme == "https" and bool(url.hostname) and not url.username
                 and not url.password and not url.query and not url.fragment
                 and url.path.rstrip("/") == "/api/v1"
                 and not any(c.isspace() for c in value))
        _ = url.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise VideoError("DASHSCOPE_BASE_URL 必须是以 /api/v1 结尾的 HTTPS 地址，不能包含凭证或查询参数。")


def _duration(value):
    if type(value) is not int or not 2 <= value <= 15:
        raise VideoError("视频生成时长必须是 2 到 15 秒之间的整数；请显式拆分或调整镜头。")
    return value


def _read_env(path):
    """Read a small dotenv subset without shell expansion or modifying os.environ."""
    values = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise VideoError(f"无法读取环境配置文件 {path}: {type(exc).__name__}") from None
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise VideoError(f"环境配置文件第 {number} 行无效，需要 NAME=value 格式。")
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            if end == -1 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#")):
                raise VideoError(f"环境配置文件第 {number} 行引号不匹配；不支持多行值。")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def load_config(env_file=None, *, allow_missing=False):
    """Load project .env, with process environment taking precedence.

    An explicitly supplied missing file is an error. The default .env may be
    absent when a deployment supplies all configuration via its environment.
    Relative output paths are resolved against the .env directory.
    """
    path = Path(env_file).resolve() if env_file is not None else ROOT / ".env"
    values = _read_env(path) if path.exists() or (env_file is not None and not allow_missing) else {}
    values.update(os.environ)

    def get(name, default):
        return values.get(name, default).strip()

    def integer(name, default):
        try:
            return int(get(name, str(default)))
        except ValueError:
            raise VideoError(f"{name} 必须是整数。") from None

    def number(name, default):
        try:
            return float(get(name, str(default)))
        except ValueError:
            raise VideoError(f"{name} 必须是数值。") from None

    def boolean(name, default):
        value = get(name, str(default)).lower()
        if value not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            raise VideoError(f"{name} 必须是 true 或 false。")
        return value in ("true", "1", "yes", "on")

    output_value = get("BAILIAN_VIDEO_OUTPUT_DIR", "outputs/videos")
    if not output_value:
        raise VideoError("BAILIAN_VIDEO_OUTPUT_DIR 不能为空。")
    output_dir = Path(output_value).expanduser()
    if not output_dir.is_absolute():
        output_dir = path.parent / output_dir
    return VideoConfig(
        api_key=get("DASHSCOPE_API_KEY", ""),
        base_url=get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"),
        model=get("BAILIAN_VIDEO_MODEL", "wan2.6-t2v"),
        reference_model=get("BAILIAN_REFERENCE_VIDEO_MODEL", "") or None,
        resolution=get("BAILIAN_VIDEO_RESOLUTION", "720P").upper(),
        ratio=get("BAILIAN_VIDEO_RATIO", "9:16"),
        duration=integer("BAILIAN_VIDEO_DURATION", 5),
        prompt_extend=boolean("BAILIAN_VIDEO_PROMPT_EXTEND", True),
        watermark=boolean("BAILIAN_VIDEO_WATERMARK", False),
        poll_interval=number("BAILIAN_VIDEO_POLL_INTERVAL", 10),
        wait_timeout=number("BAILIAN_VIDEO_WAIT_TIMEOUT", 900),
        request_timeout=number("BAILIAN_VIDEO_REQUEST_TIMEOUT", 60),
        output_dir=output_dir.resolve(), env_file=path,
    )


@traced("video.references.select_model", kind="validation", capture_inputs=False, capture_output=False)
def config_for_references(config, pins, *, snapshot=None, legacy=False):
    purpose = "video.reference_to_video" if pins else "video.text_to_video"
    if legacy:
        config.require_legacy_provider()
        if getattr(config, "route", None) is not None:
            if config.env_file is None:
                raise VideoError("历史制作委托需要原百炼连接配置，当前配置未指定其来源。")
            config = load_config(config.env_file, allow_missing=True)
        return replace(config, legacy_submission=True, model=(config.reference_model or config.model) if pins else config.model)
    if snapshot is None and not pins and getattr(config, "route", None) is None and getattr(config, "env_file", None) is None:
        return config
    if (snapshot is None and not pins and config.route is None
            and not (config.env_file.parent / "config" / "models.json").is_file()):
        return config
    if getattr(config, "route", None) is not None and config.route_purpose == purpose and snapshot is None and not legacy:
        return config
    if config.route is not None and config.env_file is None and snapshot is None:
        raise VideoError("当前视频配置已绑定其他用途，请提供路由配置重新选择模型。")
    if config.env_file is None and snapshot is None:
        chosen = replace(config, model=config.reference_model) if pins and config.reference_model else config
    else:
        from pianchang.model_routes import load_routes
        registry = load_routes(config.env_file)
        route = registry.restore(snapshot) if snapshot is not None else registry.resolve(purpose)
        chosen = replace(config, model=route.model_id, base_url=route.base_url,
                         api_key=route.api_key, route=route, route_purpose=purpose)
    event("video.model_selected", {"base_model": config.model, "configured_reference_model": config.reference_model,
          "reference_count": len(pins), "model": chosen.model}, kind="decision")
    return chosen


def config_for_shooting_plan(config, pins, plan):
    if plan is None:
        return config_for_references(config, pins)
    routes = plan.get("video_routes")
    if routes is None:
        return config_for_references(config, pins, legacy=True)
    purpose = "video.reference_to_video" if pins else "video.text_to_video"
    if purpose not in routes:
        raise VideoError("制作委托未确认当前镜头用途的模型。")
    return config_for_references(config, pins, snapshot=routes[purpose])


def _text(value, name, limit, *, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise VideoError(f"{name} 必须是非空字符串。")
    if len(value) > limit:
        raise VideoError(f"{name} 超过模型上限 {limit} 字符；请缩短输入，程序不会静默截断。")
    return value


def _media_url(value, name, schemes):
    try:
        url = parse.urlsplit(value)
        valid = (url.scheme in schemes and bool(url.hostname) and not url.username
                 and not url.password and not any(c.isspace() for c in value))
        _ = url.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise VideoError(f"{name} 必须是有效的 {'/'.join(schemes)} URL，不能使用本地路径或内嵌凭证。")
    return value


def _ref_check(path, expected, actual, *, details=None):
    valid = expected == actual
    event("video.reference_check", {"path": path, "expected": expected, "actual": actual,
                                    "status": "PASS" if valid else "FAIL", **({"details":details} if details else {})}, kind="validation")
    if not valid:
        raise VideoError(f"参考素材校验失败 {path}：要求 {expected}，实际 {actual}。" +
                         (f" 当前图片参考 {details['image_count']} 张，允许 {details['minimum']}–{details['maximum']} 张。" if details and "image_count" in details else ""))


def reference_pin(item):
    if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip() for k in PIN_FIELDS):
        raise VideoError("参考素材必须指定资产 ID、版本、两个哈希、主体和用途。")
    if any(not re.fullmatch(r"[a-f0-9]{64}", item[k]) for k in ("sha256", "media_sha256")):
        raise VideoError("参考素材版本与文件哈希必须为 SHA-256。")
    if not re.fullmatch(r"r[1-9][0-9]*", item["revision_id"]):
        raise VideoError("参考素材须固定到明确版本。")
    return {k: item[k] for k in PIN_FIELDS}


@traced("video.references.model_capabilities", kind="validation", capture_inputs=False)
def validate_reference_capabilities(config, pins):
    if not isinstance(pins, (list, tuple)):
        raise VideoError("required_refs 必须是列表。")
    refs = [reference_pin(ref) for ref in pins]
    if config.sdk_video and not refs:
        return
    if not config.sdk_video and config.model not in REFERENCE_MODELS:
        if refs:
            roles = sorted({r["purpose"] for r in refs})
            event("video.reference_check", {"path": f"model.{config.model}.reference_purposes",
                  "expected": roles, "actual": [], "status": "FAIL"}, kind="validation")
            raise VideoError(f"{config.model} 无法提交镜头要求的参考 {roles}；参考生视频可选 wan2.7-r2v。")
        return
    images, voices = {}, set()
    for item in refs:
        who, purpose = item["subject_id"], item["purpose"]
        if purpose == "voice":
            _ref_check(f"subject.{who}.single_voice", False, who in voices)
            voices.add(who)
        elif purpose in IMAGE_PURPOSES:
            _ref_check(f"subject.{who}.single_visual_reference", False, who in images)
            images[who] = purpose
        else:
            raise VideoError(f"当前模型未适配参考用途 {purpose}。")
    maximum = config.max_reference_images
    _ref_check(f"model.reference_image_count_in_1_to_{maximum}", True, 1 <= len(images) <= maximum,
               details={"image_count":len(images),"minimum":1,"maximum":maximum,"subjects":sorted(images)})
    for who in voices:
        _ref_check(f"subject.{who}.voice_requires_portrait", "portrait", images.get(who))
    if config.sdk_video and len(voices) > 3:
        raise VideoError("Seedance 每个镜头至多接受 3 个参考音频。")


def _inline_bytes(ref):
    value = ref.get("provider_url")
    prefix = "data:" + str(ref.get("mime_type")) + ";base64,"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise VideoError("参考图片和待上传的音色须来自经过固定的本地资产字节。")
    try:
        return base64.b64decode(value[len(prefix):], validate=True)
    except (ValueError, binascii.Error):
        raise VideoError("参考媒体的 Base64 内容无法解码。") from None


def _opaque_png(data):
    """Reject alpha channels and palette transparency, per the Wan image contract."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 33 or data[12:16] != b"IHDR":
        return False
    if data[25] in (4, 6):
        return False
    offset = 8
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk = data[offset + 4:offset + 8]
        offset += length + 12
        if offset > len(data) or chunk == b"tRNS":
            return False
        if chunk == b"IEND":
            return True
    return False


@traced("video.references.validate_media", kind="validation", capture_inputs=False)
def validate_reference_media(config, resolved_refs, *, pending_voice_upload=False):
    """Check immutable media bytes and Bailian's upload receipt when a voice uses OSS."""
    validate_reference_capabilities(config, resolved_refs)
    voice_duration = 0
    for item in resolved_refs:
        pin = reference_pin(item)
        name = f"{pin['subject_id']}.{pin['purpose']}"
        voice = pin["purpose"] == "voice"
        size = item.get("bytes")
        byte_limit = (15 if voice else 20)*1024*1024
        if config.sdk_video and not voice:
            byte_limit = 30*1024*1024 - 1
        _ref_check(name + ".supported_size", True, type(size) is int and 0 < size <= byte_limit)
        if voice:
            _ref_check(name + ".supported_audio_format", True, item.get("mime_type") in {"audio/wav","audio/mpeg"})
            seconds = item.get("duration_s")
            low, high = (2, 15) if config.sdk_video else (1, 10)
            _ref_check(name + f".duration_within_{low}_to_{high}s", True, type(seconds) in (float,int) and low <= seconds <= high)
            voice_duration += seconds
            if config.sdk_video:
                _ref_check("model.total_reference_audio_within_15s",True,voice_duration <= 15)
            if not pending_voice_upload and not config.sdk_video:
                receipt = item.get("upload_receipt") or {}
                _ref_check(name + ".upload_is_real", True, bool(receipt) and not receipt.get("dry_run")
                           and bool(re.fullmatch(r"[a-f0-9]{32}", str(receipt.get("trace_id", "")))))
                _ref_check(name + ".upload_bytes", size, receipt.get("bytes"))
                _ref_check(name + ".upload_sha256", pin["media_sha256"], receipt.get("media_sha256"))
                locator = _media_url(item.get("provider_url"), "音色临时地址", ("oss",))
                remote = parse.urlsplit(locator)
                _ref_check(name + ".dashscope_upload_object", True, remote.hostname == "dashscope-instant"
                           and bool(remote.path.strip("/")) and not remote.query and not remote.fragment)
                _ref_check(name + ".uploaded_locator", hashlib.sha256(locator.encode()).hexdigest(),
                           hashlib.sha256(str(receipt.get("provider_url","")).encode()).hexdigest())
                continue
        else:
            _ref_check(name + ".supported_image_format", True,
                       item.get("mime_type") in {"image/jpeg","image/png","image/webp","image/bmp"})
            w, h = item.get("width"), item.get("height")
            minimum, maximum, aspect = (300, 6000, 2.5) if config.sdk_video else (240, 8000, 8)
            _ref_check(name + ".supported_dimensions", True, type(w) is int and type(h) is int
                       and minimum <= w <= maximum and minimum <= h <= maximum and 1/aspect <= w/h <= aspect)
        raw = _inline_bytes(item)
        _ref_check(name + ".bytes", size, len(raw))
        _ref_check(name + ".media_sha256", pin["media_sha256"], hashlib.sha256(raw).hexdigest())
        if item["mime_type"] == "image/png" and not config.sdk_video:
            _ref_check(name + ".opaque_png", True, _opaque_png(raw))


def reference_slots(refs):
    """Wan counts images by their own modality and attaches timbre to that exact subject."""
    voices = {r["subject_id"]: r for r in refs if r["purpose"] == "voice"}
    return [(r, voices.get(r["subject_id"])) for r in refs if r["purpose"] != "voice"]


def reference_prompt_bindings(refs, *, sdk=False):
    slots = reference_slots(refs)
    bindings = "\n参考素材：" + "；".join(
        f"图{i}对应{image['subject_id']}（{image['purpose']}）" + ("，使用该主体的参考音色" if voice else "")
        for i, (image, voice) in enumerate(slots, 1))
    if sdk:
        voices = [voice for _, voice in slots if voice]
        bindings += "".join(f"；音频{i}是{voice['subject_id']}的参考音色" for i, voice in enumerate(voices, 1))
    return bindings


def build_request(config, prompt, *, duration=None, negative_prompt=None,
                  seed=None, image_url=None, audio_url=None, resolved_refs=(), reference_plan=False):
    """Compile the correct 2.6 or 2.7 HTTP payload; never silently drop inputs."""
    if image_url is not None:
        raise VideoError("此入口不接受未绑定的 image_url；请登记图片并在镜头 required_refs 中指定资产版本。")
    limit = 1500 if config.model == "wan2.6-t2v" else 5000
    validate_reference_capabilities(config, resolved_refs)
    if config.sdk_video and (audio_url is not None or negative_prompt is not None):
        raise VideoError("当前 SDK 视频协议没有适配独立驱动音频或 negative_prompt。")
    uses_references = bool(resolved_refs) if config.sdk_video else config.model in REFERENCE_MODELS
    if uses_references:
        if audio_url is not None:
            raise VideoError("参考生视频的角色音色须绑定对应主体，不能传独立驱动音频。")
        validate_reference_media(config, resolved_refs, pending_voice_upload=reference_plan)
        prompt = _text(prompt, "prompt", limit) + reference_prompt_bindings(resolved_refs, sdk=config.sdk_video)
    inputs = {"prompt": _text(prompt, "prompt", limit)}
    if uses_references:
        inputs["media"] = [{"type":"reference_image", "url":image["provider_url"],
                            **({"reference_voice":voice["provider_url"]} if voice else {})}
                           for image,voice in reference_slots(resolved_refs)]
    if negative_prompt is not None:
        inputs["negative_prompt"] = _text(negative_prompt, "negative_prompt", 500, allow_empty=True)
    if audio_url is not None:
        inputs["audio_url"] = _media_url(audio_url, "audio_url", ("https", "http", "oss"))
    parameters = {
        "duration": _duration(config.duration if duration is None else duration),
        "prompt_extend": config.prompt_extend,
        "watermark": config.watermark,
    }
    if config.sdk_video:
        parameters.pop("prompt_extend")
        if parameters["duration"] < 4:
            raise VideoError("当前 Seedance 视频配置支持 4 到 15 秒的镜头，请先调整镜头时长。")
    if config.model == "wan2.6-t2v":
        parameters["size"] = SIZES_26[config.resolution][config.ratio]
        if config.prompt_extend:
            parameters["shot_type"] = "single"
    else:
        parameters.update(resolution=config.resolution, ratio=config.ratio)
    if seed is not None:
        if type(seed) is not int or not 0 <= seed <= 2147483647:
            raise VideoError("seed 必须是 0 到 2147483647 之间的整数。")
        parameters["seed"] = seed
    payload = {"model": config.model, "input": inputs, "parameters": parameters}
    if config.sdk_video:
        from pianchang.sdk_video import ark_body
        wire_size = sum(len(chunk.encode("utf-8")) for chunk in json.JSONEncoder(
            ensure_ascii=False, separators=(",", ":")).iterencode(ark_body(payload)))
        if wire_size > 64*1024*1024:
            raise VideoError("当前固定参考的实际视频请求超过 Seedance 的 64 MB 请求体上限。")
    if reference_plan:
        return {"dry_run": True, "model": config.model, "parameters": parameters, "prompt": inputs["prompt"],
                "required_refs": [reference_pin(r) for r in resolved_refs],
                "pending_uploads": [reference_pin(r) for r in resolved_refs if r["purpose"] == "voice" and not config.sdk_video]}
    return payload


class _NoRedirect(request.HTTPRedirectHandler):
    """Never forward an authenticated API request to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _MediaRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _media_url(newurl, "视频重定向地址", ("https", "http"))
        event("http.redirect", {"from_url": req.full_url, "to_url": newurl,
              "http_status": code, "response_headers": dict(headers.items())}, kind="http")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _omit_encoded_media(body):
    """Replace OpenAI's bare base64 fields with evidence before persisting JSON."""
    import hashlib
    changed = False
    if not isinstance(body, dict):
        return False

    def items(key):
        value = body.get(key)
        return value if isinstance(value, list) else ()

    def omit(container, key):
        nonlocal changed
        value = container.get(key) if isinstance(container, dict) else None
        if isinstance(value, str) and value and not value.startswith("data:"):
            encoded = value.encode()
            container[key] = {"omitted": "inline_media", "encoded_bytes": len(encoded),
                              "sha256": hashlib.sha256(encoded).hexdigest()}
            changed = True

    for image in items("data"):
        omit(image, "b64_json")
    for choice in items("choices"):
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict):
            omit(message.get("audio"), "data")
    for message in items("messages"):
        parts = message.get("content") if isinstance(message, dict) else None
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict):
                    omit(part.get("input_audio"), "data")
    return changed


def _trace_http_request(req, timeout):
    """Record the actual request before transport; credentials are SDK-redacted."""
    import hashlib
    raw = req.data
    try:
        body = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (ValueError, UnicodeError):
        body = raw.decode("utf-8", errors="replace") if raw is not None else None
    _omit_encoded_media(body)
    event("http.request", {"method": req.get_method(), "url": req.full_url,
          "headers": dict(req.header_items()), "body": body,
          "wire_body_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
          "timeout_s": timeout}, kind="http")


def _trace_http_response(response, raw, limit):
    """Keep both unparsed text and JSON envelope, including invalid responses."""
    import hashlib
    text = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError):
        body = None
    if _omit_encoded_media(body):
        text = json.dumps(body, ensure_ascii=False)
    headers = dict(response.headers.items()) if response.headers else {}
    event("http.response", {"http_status": getattr(response, "status", getattr(response, "code", None)),
          "headers": headers, "header_request_id": response.headers.get("x-request-id") if response.headers else None,
          "body_id": body.get("id") if isinstance(body, dict) else None,
          "body_request_id": body.get("request_id") if isinstance(body, dict) else None,
          "body": body, "body_text": text, "captured_bytes": len(raw),
          "capture_limit_bytes": limit, "body_truncated": len(raw) > limit,
          "captured_wire_sha256": getattr(response, "captured_wire_sha256", hashlib.sha256(raw).hexdigest())}, kind="http")


def _verified_https_handler():
    # Keep hostname/certificate verification while avoiding mutable process-wide
    # default-context hooks (including inherited TLS key-log file callbacks).
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    from pianchang.review_dispatch import ReviewHTTPSHandler
    return ReviewHTTPSHandler(context=context)


def validate_mp4_header(prefix, byte_count):
    if byte_count < 12 or prefix[4:8] != b"ftyp":
        raise VideoError("视频下载为空或缺少 MP4 文件头；未保存为成片。")


class BailianVideoClient:
    """Single-attempt API methods. Polling/recovery belongs to the caller."""

    def __init__(self, config):
        self.config = config
        register_secret(config.api_key)
        self._api_opener = request.build_opener(_NoRedirect(),_verified_https_handler())
        self._media_opener = request.build_opener(_MediaRedirect(),_verified_https_handler())

    def _redact(self, value):
        text = str(value)
        if self.config.api_key:
            text = text.replace(self.config.api_key, "[REDACTED]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)

    def _provider_error(self, body, http_status=None):
        code = self._redact(body.get("code") or "APIError")
        request_id = self._redact(body.get("request_id") or "")
        message = self._redact(body.get("message") or "百炼 API 请求失败")
        details = f"百炼 API 错误：{code}: {message}"
        if http_status is not None:
            details += f" (HTTP {http_status})"
        if request_id:
            details += f" [request_id={request_id}]"
        return VideoError(details, code=code, request_id=request_id or None, http_status=http_status)

    @traced("bailian.video.http", kind="http")
    def _request(self, method, path, payload=None):
        self.config.require_legacy_provider()
        key = self.config.api_key
        if not key or key.lower() in ("your_api_key", "your-api-key", "sk-xxxx", "replace_me"):
            if self.config.route is not None and self.config.route.connection != "bailian.media":
                raise VideoError(f"模型连接 {self.config.route.connection} 缺少有效的 API Key，请检查 api_key_env。")
            raise VideoError("尚未配置 API Key：请在项目 .env 中填写 DASHSCOPE_API_KEY，或设置同名环境变量。")
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        data = None
        if payload is not None:
            try:
                data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                raise VideoError("视频请求必须可序列化为有效 JSON。") from None
            headers.update({"Content-Type": "application/json", "X-DashScope-Async": "enable"})
            inputs = payload.get("input", {})
            fields = [inputs.get("audio_url"), *(value for item in inputs.get("media", [])
                       for value in (item.get("url"), item.get("reference_voice")))]
            if any(isinstance(value, str) and value.startswith("oss://") for value in fields):
                headers["X-DashScope-OssResourceResolve"] = "enable"
        req = request.Request(self.config.base_url + path, data=data, headers=headers, method=method)
        _trace_http_request(req, self.config.request_timeout)
        try:
            with self._api_opener.open(req, timeout=self.config.request_timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                _trace_http_response(response, raw, 1024 * 1024)
        except error.HTTPError as exc:
            try:
                raw_error = exc.read(65537)
                _trace_http_response(exc, raw_error, 65536)
                body = json.loads(raw_error.decode("utf-8"))
            except (ValueError, OSError, http.client.HTTPException):
                body = {}
            finally:
                exc.close()
            if not isinstance(body, dict):
                body = {}
            if 300 <= exc.code < 400:
                body = {"code": "UnexpectedRedirect", "message": "API 返回重定向，已阻止转发凭证；请检查 DASHSCOPE_BASE_URL。"}
            raise self._provider_error(body, exc.code) from None
        except (error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            cause=getattr(exc,"reason",exc)
            event("http.transport_error",{"method":method,"endpoint":self.config.base_url+path,
                "exception_type":type(exc).__name__,"cause_type":type(cause).__name__,"errno":getattr(cause,"errno",None),
                "filename":getattr(cause,"filename",None),"message":str(exc),
                "provider_outcome":"unknown" if method=="POST" else "existing task unchanged"},kind="error")
            suffix = "提交结果可能未知，请勿自动重提以免重复计费。" if method == "POST" else "可以使用原 task_id 继续查询。"
            raise VideoError(f"百炼网络请求失败：{self._redact(exc)}。{suffix}",code="TransportError") from exc
        if len(raw) > 1024 * 1024:
            raise VideoError("百炼 API 响应过大，无法解析；请保留本地任务记录。")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise VideoError("百炼 API 未返回有效 JSON；请检查 Endpoint 并保留本地任务记录。") from None
        if not isinstance(body, dict):
            raise VideoError("百炼 API 返回了非对象 JSON，无法识别任务。")
        if body.get("code"):
            raise self._provider_error(body)
        return body

    @traced("bailian.video.submit", kind="model")
    def submit(self, payload):
        """Submit exactly once; success normally returns output.task_id."""
        return self._request("POST", "/services/aigc/video-generation/video-synthesis", payload)

    @traced("bailian.video.get_task", kind="tool")
    def get_task(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise VideoError("task_id 必须是服务端返回的任务 ID，不能包含路径或查询参数。")
        return self._request("GET", "/tasks/" + task_id)

    @traced("bailian.video.download", kind="tool")
    def download(self, video_url, destination):
        """Stream a result without Authorization, then atomically publish an MP4.

        Header/length checks detect empty responses and common error pages; they
        do not constitute playback, visual, audio, or duration verification.
        """
        _media_url(video_url, "video_url", ("https", "http"))
        destination = Path(destination)
        temporary = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            req = request.Request(video_url, headers={"Accept": "video/mp4, application/octet-stream"})
            _trace_http_request(req, self.config.request_timeout)
            with self._media_opener.open(req, timeout=self.config.request_timeout) as response:
                event("http.response.headers", {"http_status": getattr(response, "status", None),
                      "headers": dict(response.headers.items()), "body_policy": "streamed_binary_not_embedded"}, kind="http")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type.startswith("text/") or content_type in ("application/json", "application/xml"):
                    _trace_http_response(response, response.read(65537), 65536)
                    raise VideoError("视频下载返回了文本或错误页面，未保存为 MP4。")
                length = response.headers.get("Content-Length")
                expected = int(length) if length is not None else None
                import hashlib
                digest = hashlib.sha256()
                total, prefix = 0, b""
                with tempfile.NamedTemporaryFile(prefix=destination.name + ".", suffix=".part",
                                                 dir=destination.parent, delete=False) as output:
                    temporary = Path(output.name)
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        if len(prefix) < 12:
                            prefix = (prefix + chunk)[:12]
                        output.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                validate_mp4_header(prefix, total)
                if expected is not None and total != expected:
                    raise VideoError("视频下载长度不完整；可以使用同一任务记录重试下载。")
            from pianchang.atomic_files import replace_with_retry
            replace_with_retry(temporary, destination)
            temporary = None
            event("artifact.downloaded", {"path": str(destination.resolve()), "bytes": total,
                  "sha256": digest.hexdigest(), "content_type": content_type,
                  "expected_bytes": expected}, kind="artifact")
            return destination
        except VideoError:
            raise
        except error.HTTPError as exc:
            try:
                _trace_http_response(exc, exc.read(65537), 65536)
            finally:
                exc.close()
            raise VideoError(f"视频下载失败：{self._redact(exc)}；可继续原任务，无需重新生成。") from None
        except (error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            raise VideoError(f"视频下载失败：{self._redact(exc)}；可继续原任务，无需重新生成。") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
