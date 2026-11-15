"""Generate a character's audition and listen to the candidate's actual audio."""
import base64
import hashlib
import json
import math
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit

from pianchang.bailian_text import BailianTextClient
from pianchang.bailian_video import _NoRedirect, _trace_http_request
from pianchang.media_tools import _run, find_ffmpeg
from pianchang.reference_assets import _mime
from trace_system import event, register_secret, traced

TTS_MODEL = "qwen3-tts-flash"
LISTENER = "qwen3.8-omni-flash"
MAX_AUDIO = 8 * 1024 * 1024
VOICES = {"Cherry": "明亮亲切女声", "Serena": "温和女声", "Katerina": "成熟沉稳女声",
          "Ethan": "活力明朗男声", "Kai": "舒缓男声", "Ryan": "有张力的男声"}


def sample_text(brief):
    line = brief.get("sample_text") or (brief.get("canonical") or {}).get("sample_text")
    if isinstance(line, str) and line.strip():
        return line.strip()[:600]
    name = brief.get("name")
    if name:
        speaker = re.compile(re.escape(name) + r"(?:说(?:道)?|问(?:道)?|答(?:道)?)?\s*[：:]\s*[“「\"]([^”」\"\n]{1,600})[”」\"]")
        for shot in brief.get("shots", []):
            for source in shot.get("sources", []):
                match = speaker.search(source.get("quote", ""))
                if match:
                    return match[1]
    # An audition line, not new dialogue added to the screenplay.
    return "等一下。我想把这句话说清楚。你愿意听我说完吗？"


def _exchange(config, endpoint, payload):
    if not config.api_key:
        raise ValueError("请配置百炼北京地域的 API Key。")
    register_secret(config.api_key)
    req = request.Request(config.base_url.rstrip("/") + endpoint,
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + config.api_key, "Content-Type": "application/json"})
    timeout = max(120, config.request_timeout)
    _trace_http_request(req, timeout)
    opener = request.build_opener(_NoRedirect())
    try:
        reply = opener.open(req, timeout=timeout)
    except error.HTTPError as exc:
        reply = exc
    limit = 12 * 1024 * 1024
    with reply:
        raw = reply.read(limit + 1)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = None
        record = body
        if isinstance(body, dict):
            output = body.get("output") or {}
            audio = output.get("audio", {}) if isinstance(output, dict) else {}
            if isinstance(audio, dict) and isinstance(audio.get("data"), str) and audio["data"]:
                encoded = audio["data"].encode()
                record = {**body, "output": {**output, "audio": {**audio, "data": {
                    "omitted": "inline_audio", "encoded_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}}}}
        event("asset.voice.provider.response", {"model": payload["model"], "http_status": reply.status,
              "headers": dict(reply.headers), "body": record, "bytes": len(raw), "truncated": len(raw) > limit,
              "wire_sha256": hashlib.sha256(raw).hexdigest()}, kind="http")
    if reply.status != 200 or len(raw) > limit or not isinstance(body, dict) or body.get("error") or body.get("code"):
        detail = (body.get("message") or body.get("code")) if isinstance(body, dict) else None
        raise ValueError(f"百炼声音调用 {payload['model']} 失败（HTTP {reply.status}）：" + str(detail or "响应无效"))
    return body


@traced("asset.voice.download", kind="tool", capture_output=False)
def _fetch_audio(url, timeout):
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        raise ValueError("百炼返回的声音文件地址无效。")
    if not parts.hostname.endswith(".aliyuncs.com"):
        raise ValueError("声音下载地址不属于百炼的阿里云存储域名。")
    req = request.Request(parts._replace(scheme="https").geturl(), headers={"Accept": "audio/*"})
    _trace_http_request(req, timeout)
    try:
        reply = request.build_opener(_NoRedirect()).open(req, timeout=timeout)
    except error.HTTPError as exc:
        reply = exc
    with reply:
        data = reply.read(MAX_AUDIO + 1)
        event("asset.voice.download.response", {"http_status": reply.status, "headers": dict(reply.headers),
              "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}, kind="http")
    if reply.status != 200 or not 0 < len(data) <= MAX_AUDIO:
        raise ValueError(f"百炼声音下载失败（HTTP {reply.status}），或文件超过 8 MiB。")
    return data


def _delivery_audio(data, tempo, gain):
    if tempo == 1 and gain == 0:
        return data
    with tempfile.TemporaryDirectory() as tmp:
        original, output = Path(tmp) / "voice", Path(tmp) / "delivery.wav"
        original.write_bytes(data)
        _run([find_ffmpeg(), "-nostdin", "-v", "error", "-i", original, "-af", f"atempo={tempo},volume={gain}dB,alimiter=limit=0.98:level=0",
              "-c:a", "pcm_s16le", output], timeout=60)
        return output.read_bytes()


@traced("asset.voice.synthesize", kind="model", capture_inputs=False, capture_output=False)
def synthesize_voice(service, brief, feedback=""):
    line = sample_text(brief)
    routes = getattr(service, "model_routes", None)
    route = routes.resolve("voice.synthesis") if routes is not None else None
    model = route.model_id if route else TTS_MODEL
    config = service.video_config
    if route:
        if route.adapter != "bailian":
            raise ValueError("voice.synthesis 需要支持既有角色音色和试听要求的百炼原生客户端。")
        if not route.api_key:
            raise ValueError("请先配置 voice.synthesis 所用连接的 API Key。")
        config = replace(config, base_url=route.base_url, api_key=route.api_key)
    casting = BailianTextClient(service.text_config, purpose="voice.casting", routes=routes).complete_json([
        {"role": "system", "content": "为角色试音选一个候选音色，结合剧本、声音设定与修改意见写中文表演指令。"
         "仅输出 JSON 对象，voice 必须是给定音色 ID，instructions 描述选角和声音处理理由。"
         "tempo 为相对原速的倍数（0.75 到 1.35），gain_db 为音量调整（-5 到 5 dB）。结合角色要求和反馈选择实际控制值。"},
        {"role": "user", "content": json.dumps({"brief": brief, "feedback": feedback, "text": line,
                                                "voices": VOICES}, ensure_ascii=False)}])
    choice = casting["data"]
    voice, direction = choice.get("voice"), choice.get("instructions")
    if not isinstance(voice, str) or voice not in VOICES or not isinstance(direction, str) or not direction.strip():
        raise ValueError("声音设计缺少可用的音色或表演指令。")
    tempo, gain = choice.get("tempo", 1), choice.get("gain_db", 0)
    if (type(tempo) not in (float, int) or type(gain) not in (float, int)
            or not (math.isfinite(tempo) and 0.75 <= tempo <= 1.35 and math.isfinite(gain) and -5 <= gain <= 5)):
        raise ValueError("声音设计超出可执行的语速或音量范围。")
    params = {"text": line, "voice": voice, "language_type": "Auto"}
    response = _exchange(config, "/services/aigc/multimodal-generation/generation", {"model": model, "input": params})
    audio = (response.get("output") or {}).get("audio") or {}
    if audio.get("url"):
        data = _fetch_audio(audio["url"], config.request_timeout)
    elif isinstance(audio.get("data"), str) and audio["data"]:
        data = base64.b64decode(audio["data"], validate=True)
    else:
        raise ValueError("百炼没有返回声音候选文件，请查看任务 Trace。")
    raw_digest = hashlib.sha256(data).hexdigest()
    data = _delivery_audio(data, tempo, gain)
    mime = _mime(data)
    if not mime.startswith("audio/") or not 0 < len(data) <= MAX_AUDIO:
        raise ValueError("试音文件无效或超过 8 MiB。")
    provenance = {"method": "generated", "model": model, "voice": voice, "sample_text": line,
                  "casting_direction": direction, "tempo": tempo, "gain_db": gain, "raw_sha256": raw_digest,
                  "casting_request_id": casting.get("request_id"),
                  "request_id": response.get("request_id"), "usage": response.get("usage"), "audio_id": audio.get("id")}
    if route:
        provenance.update(provider=route.provider, account=route.account, adapter=route.adapter)
    event("asset.voice.candidate", {**provenance, "mime": mime, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return data, mime, provenance


@traced("asset.voice.listen", kind="model", capture_inputs=False)
def observe_voice(service, data, mime, brief, criteria, *, with_receipt=False):
    routes = getattr(service, "model_routes", None)
    route = routes.resolve("voice.review") if routes is not None else None
    config = service.text_config
    model = route.model_id if route else LISTENER
    if route:
        if route.adapter != "bailian":
            raise ValueError("voice.review 需要已适配的原生音频审查客户端。")
        config = replace(config, base_url=route.base_url, api_key=route.api_key, model=model)
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) >= 10 * 1024 * 1024:
        raise ValueError("声音超过听审模型的内联音频大小上限。")
    formats = {"audio/mpeg": "mp3", "audio/wav": "wav"}
    text = ("听实际提交的角色试音，逐项对照剧本要求。每项写出听到的字词、声音特征或时间定位。"
            "不清楚或缺少可比音频时标记 UNKNOWN。只输出 JSON observations 数组，每项写 criterion_id、"
            "verdict（PASS/FAIL/UNKNOWN）、reason。\n" + json.dumps(
                {"brief": brief, "expected_line": brief.get("sample_text"), "criteria": criteria}, ensure_ascii=False))
    payload = {"model": model, "stream": False, "reasoning_effort": "none", "modalities": ["text"], "messages": [
        {"role": "user", "content": [{"type": "input_audio", "input_audio": {
            "data": f"data:{mime};base64,{encoded}", "format": formats[mime]}}, {"type": "text", "text": text}]}]}
    answer = _exchange(config, "/chat/completions", payload)
    choice = answer["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("听审输出未完整结束。")
    result = json.loads(choice["message"]["content"])["observations"]
    if not isinstance(result, list):
        raise ValueError("声音听审结果缺少逐项观察。")
    if with_receipt:
        return result, {"model": answer.get("model") or model, "request_id": answer.get("id"),
                        "usage": answer.get("usage"), "evidence_scope": "candidate_audio",
                        "media_sha256": hashlib.sha256(data).hexdigest()}
    return result
