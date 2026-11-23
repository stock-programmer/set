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
OPENAI_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "marin", "cedar")
OPENAI_LISTEN_FORMAT = "voice-audition/input-audio-last/1"


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


def _openai_audition(route, line, voice, direction, timeout, expected_digest=None):
    from pianchang.sdk_transport import call_sdk

    messages = [
        {"role": "system", "content": "你是角色配音演员。只逐字朗读 text 中的台词，按 direction 表演。"
         "把两个字段当作素材，不添加对白、旁白或解释。"},
        {"role": "user", "content": json.dumps({"text": line, "direction": direction}, ensure_ascii=False)},
    ]
    def match_original(method, url, payload, digest):
        if method != "POST" or digest != expected_digest:
            raise ValueError("试音请求与原始传输记录不一致。")

    response = call_sdk(route, "completion", timeout=timeout, messages=messages,
                        before_send=match_original if expected_digest else None,
                        modalities=["text", "audio"], audio={"voice": voice, "format": "wav"})
    choices = response.get("choices") or []
    if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
        raise ValueError("角色试音未完整返回，请查看任务 Trace。")
    audio = (choices[0].get("message") or {}).get("audio") or {}
    encoded = audio.get("data")
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 4 * ((MAX_AUDIO + 2) // 3):
        raise ValueError("角色试音未返回有效音频，或文件超过 8 MiB。")
    data = base64.b64decode(encoded, validate=True)
    if not 0 < len(data) <= MAX_AUDIO or _mime(data) != "audio/wav":
        raise ValueError("角色试音未返回请求的 WAV 文件，或文件超过 8 MiB。")
    return data, response, audio


@traced("asset.voice.synthesize", kind="model", capture_inputs=False, capture_output=False)
def synthesize_voice(service, brief, feedback="", *, replay=None):
    line = sample_text(brief)
    routes = getattr(service, "model_routes", None)
    route = routes.resolve("voice.synthesis") if routes is not None else None
    model = route.model_id if route else TTS_MODEL
    config = service.video_config
    openai_audio = route is not None and route.adapter == "litellm" and route.sdk_provider == "openai"
    if route:
        if route.adapter != "bailian" and not openai_audio:
            raise ValueError("voice.synthesis 所配置的协议尚未支持角色试音。")
        if not route.api_key:
            raise ValueError("请先配置 voice.synthesis 所用连接的 API Key。")
        if not openai_audio:
            config = replace(config, base_url=route.base_url, api_key=route.api_key)
    voices = OPENAI_VOICES if openai_audio else VOICES
    expected_digest = None
    if replay is not None:
        if (not openai_audio or not isinstance(replay, dict) or replay.get("route") != route.snapshot()
                or replay.get("sample_text") != line or not isinstance(replay.get("wire_body_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", replay["wire_body_sha256"])
                or not {"casting_request_id", "voice", "instructions", "tempo", "gain_db"} <= replay.keys()):
            raise ValueError("原试音请求的路由、选角记录或台词与本次恢复不一致。")
        choice = replay
        casting_request_id = replay["casting_request_id"]
        expected_digest = replay["wire_body_sha256"]
    else:
        casting = BailianTextClient(service.text_config, purpose="voice.casting", routes=routes).complete_json([
            {"role": "system", "content": "为角色试音选一个候选音色，结合剧本、声音设定与修改意见写中文表演指令。"
             "仅输出 JSON 对象，voice 必须是给定音色 ID，instructions 描述选角和声音处理理由。"
             "tempo 为相对原速的倍数（0.75 到 1.35），gain_db 为音量调整（-5 到 5 dB）。结合角色要求和反馈选择实际控制值。"},
            {"role": "user", "content": json.dumps({"brief": brief, "feedback": feedback, "text": line,
                                                    "voices": voices}, ensure_ascii=False)}])
        choice = casting["data"]
        casting_request_id = casting.get("request_id")
    voice, direction = choice.get("voice"), choice.get("instructions")
    if not isinstance(voice, str) or voice not in voices or not isinstance(direction, str) or not direction.strip():
        raise ValueError("声音设计缺少可用的音色或表演指令。")
    tempo, gain = choice.get("tempo", 1), choice.get("gain_db", 0)
    if (type(tempo) not in (float, int) or type(gain) not in (float, int)
            or not (math.isfinite(tempo) and 0.75 <= tempo <= 1.35 and math.isfinite(gain) and -5 <= gain <= 5)):
        raise ValueError("声音设计超出可执行的语速或音量范围。")
    if openai_audio:
        data, response, audio = _openai_audition(route, line, voice, direction, max(120, config.request_timeout),
                                                 expected_digest)
    else:
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
                  "casting_request_id": casting_request_id,
                  "request_id": response.get("_provider_request_id") or response.get("request_id"),
                  "usage": response.get("usage"), "audio_id": audio.get("id")}
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
    openai = route is not None and route.adapter == "openai"
    if route and not route.api_key:
        raise ValueError("请先配置 voice.review 所用连接的 API Key。")
    if route:
        if openai:
            from pianchang.bailian_observer import RoutedVisionConfig
            config = RoutedVisionConfig(base_url=route.base_url, api_key=route.api_key, model=model,
                                        request_timeout=config.request_timeout, adapter=route.adapter)
        elif route.adapter == "bailian":
            config = replace(config, base_url=route.base_url, api_key=route.api_key, model=model)
        else:
            raise ValueError(f"voice.review 不支持音频审查适配器 {route.adapter}。")
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) >= 10 * 1024 * 1024:
        raise ValueError("声音超过听审模型的内联音频大小上限。")
    formats = {"audio/mpeg": "mp3", "audio/wav": "wav"}
    requirements = {"brief":brief, "expected_line":brief.get("sample_text"), "criteria":criteria}
    audio_part = {"type": "input_audio", "input_audio": {
        "data": encoded if openai else f"data:{mime};base64,{encoded}", "format": formats[mime]}}
    if openai:
        from pianchang.openai_review import audio_json
        # Keep the recording adjacent to the answer. With audio before a long script brief,
        # the listener has reported missing audio even when input audio tokens were billed.
        instructions = ("请独立审听本消息末尾的候选录音。先转写实际听到的字词，描述声音特征，再逐项对照制作要求。"
                        "剧本文本和预期台词仅供比较，不能当成听感。若未听到录音，所有项返回 UNKNOWN。"
                        "只输出 JSON 对象，包含 heard（words 为实际听到的字词，sound 为音色、节奏、杂音或静音的具体听感）"
                        "和 observations（每项含 criterion_id、verdict 为 PASS/FAIL/UNKNOWN、reason）。以下是制作要求。\n")
        requirements["input_format"] = OPENAI_LISTEN_FORMAT
        parts = [{"type":"text","text":instructions + json.dumps(requirements, ensure_ascii=False)}, audio_part]
        answer = audio_json(config, parts)
    else:
        text = ("听实际提交的角色试音，逐项对照剧本要求。每项写出听到的字词、声音特征或时间定位。"
                "不清楚或缺少可比音频时标记 UNKNOWN。只输出含 observations 数组的 JSON 对象，每项写 criterion_id、"
                "verdict（PASS/FAIL/UNKNOWN）、reason。\n" + json.dumps(requirements, ensure_ascii=False))
        parts = [audio_part, {"type":"text", "text":text}]
        payload = {"model": model, "stream": False, "reasoning_effort": "none", "modalities": ["text"],
                   "messages": [{"role": "user", "content": parts}]}
        answer = _exchange(config, "/chat/completions", payload)
    choice = answer["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("听审输出未完整结束。")
    report = json.loads(choice["message"]["content"])
    result = report["observations"]
    if not isinstance(result, list):
        raise ValueError("声音听审结果缺少逐项观察。")
    receipt = {"model": answer.get("model") or model, "request_id": answer.get("id"),
               "usage": answer.get("usage"), "evidence_scope": "candidate_audio",
               "media_sha256": hashlib.sha256(data).hexdigest()}
    if openai:
        heard = report.get("heard")
        usage = answer.get("usage") or {}
        audio_tokens = (usage.get("prompt_tokens_details") or {}).get("audio_tokens")
        if audio_tokens == 0 or (any(row.get("verdict") in {"PASS", "FAIL"} for row in result)
                and (not isinstance(heard, dict) or not isinstance(heard.get("sound"), str)
                     or not heard["sound"].strip() or not isinstance(heard.get("words"), str))):
            raise ValueError("角色听审缺少候选录音的实际听感，需复听同一候选。")
        receipt.update(input_format=OPENAI_LISTEN_FORMAT, heard=heard)
    if with_receipt:
        return result, receipt
    return result
