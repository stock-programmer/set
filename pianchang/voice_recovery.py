"""Recover a single unsent character audition from its own frozen request."""
import json
import re

from pianchang.asset_dispatch import _basis, _matches_basis, asset_work_state
from trace_system import traced


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _span_data(tracer, call, names):
    rows = tracer.get_events(call["trace_id"], span_id=call["span_id"], names=names)
    _require(len(rows) == len(names) and {r["name"] for r in rows} == set(names),
             "原语音请求的事件不完整或不唯一。")
    return {r["name"]: tracer.resolve(r["data"]) for r in rows}


def _casting_recipe(tracer, calls):
    _require(len(calls) == 2, "不能唯一核实这次试音的选角和合成请求。")
    casting, audio = calls
    _require(casting["request_kind"] == "model" and casting.get("http_status") == 200
             and audio["request_kind"] == "audio" and casting["trace_id"] == audio["trace_id"]
             and casting["response_seq"] < audio["request_seq"], "原选角或试音顺序未核实。")
    cast = _span_data(tracer, casting, ["http.request", "http.response"])
    wire = _span_data(tracer, audio, ["model.route", "http.request"])
    messages = cast["http.request"]["body"]["messages"]
    [original] = [json.loads(m["content"]) for m in messages if m.get("role") == "user"]
    response = cast["http.response"]
    [choice] = response["body"]["choices"]
    _require(choice.get("finish_reason") == "stop" and not response.get("body_truncated"),
             "原选角没有完整回执。")
    design = json.loads(choice["message"]["content"])
    return original, {
        "voice": design["voice"], "instructions": design["instructions"],
        "tempo": design.get("tempo", 1), "gain_db": design.get("gain_db", 0),
        "casting_request_id": response.get("header_request_id"),
        "sample_text": original["text"], "route": wire["model.route"]["route"],
        "wire_body_sha256": wire["http.request"]["wire_body_sha256"],
    }


def _remaining_attempts(tracer, audio):
    span = audio["span_id"]
    for name in ("models.sdk.call", "asset.voice.synthesize", "studio.reference.generate"):
        events = tracer.get_events(audio["trace_id"], span_id=span, names=[name])
        [start] = [e for e in events if e["kind"] == "span.start"]
        span = start["parent_span_id"]
    events = tracer.get_events(audio["trace_id"], span_id=span, names=["asset.reference.next"])
    before = [e for e in events if e["trace_seq"] < audio["request_seq"]]
    _require(bool(before), "原资产尝试次数缺少记录。")
    count = tracer.resolve(before[-1]["data"])
    index, limit = count.get("attempt"), count.get("limit")
    _require(count.get("action") == "generate" and type(index) is int and type(limit) is int
             and 1 <= index <= limit <= 2, "原资产尝试次数无法核对。")
    return limit - index + 1


@traced("studio.assets.verify_unsent_voice", kind="validation", capture_inputs=False, capture_output=False)
def verified_voice_recovery(service, pid, source_id, catalog):
    from tools.workbench_loop_dependency import recheck_dependency

    _require(isinstance(source_id, str) and re.fullmatch(r"[0-9a-f]{32}", source_id),
             "原语音任务标识无效。")
    with service.lock:
        source = next((j.copy() for j in service.state["jobs"] if j["id"] == source_id), {})
    _require(source.get("kind") in {"asset_production", "asset_regenerate"}
             and source.get("project_id") == pid and source.get("status") == "failed"
             and not any(source.get(k) for k in ("_voice_recovery_from", "cancel_requested", "trace_incomplete")),
             "此任务不能作为原试音委托。")
    evidence = recheck_dependency(service.root, source) or {}
    _require(evidence.get("reason") == "voice_request_not_sent" and evidence.get("missing_count") == 1
             and evidence.get("receipt_unknown") is False
             and (evidence.get("transport_failure") or {}).get("dispatch") == "not_sent",
             "原语音请求尚无可复核的未发送证据。")
    original, recipe = _casting_recipe(service.tracer, evidence["provider_calls"])
    brief = original["brief"]
    subject = brief["subject_id"]
    work = asset_work_state(service, pid, catalog)
    units = [u for u in work["items"] if (u["subject_id"], u["purpose"]) == (subject, "voice")]
    _require(len(units) == 1 and brief["purpose"] == "voice", "原请求不属于当前声音素材。")
    unit = units[0]
    _require(not catalog.get("pending_script_ref") and unit.get("job_id") == source_id
             and unit["status"] == "needs_attention" and not unit.get("candidate") and not unit.get("selected_pin")
             and _matches_basis(service, catalog, unit.get("basis"))
             and unit.get("feedback", "") == brief.get("human_feedback", ""),
             "原声音委托已被替换，或已有候选需要核对。")
    return {"subject_id": subject, "purpose": "voice", "basis": _basis(catalog),
            "remaining_voice_attempts": _remaining_attempts(service.tracer, evidence["provider_calls"][-1]),
            "feedback": unit.get("feedback", ""), "resume_voice_from": source_id}, (brief, recipe)


def original_voice_recipe(brief, saved):
    # Recutting the same screenplay can change the shot list. The actual audition line,
    # voice instructions, style, character and published script still have to match.
    original, recipe = saved
    fields = ("subject_id", "purpose", "canonical", "entity_version", "source_refs", "script_ref",
              "script", "setup", "script_direction", "shared_style", "human_feedback", "sample_text")
    _require(all(brief.get(k) == original.get(k) for k in fields),
             "角色、试听要求或已发布剧本发生变化，原试音不可重放。")
    return recipe
