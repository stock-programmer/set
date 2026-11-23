"""Compile, submit, resume and download shots with the configured video model.

Native Bailian calls use the standard library. SDK routes require the optional
LiteLLM dependency. Real generation is billable; --dry-run is offline.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

from pianchang.bailian_video import (BailianVideoClient, PIN_FIELDS, REFERENCE_MODELS, VideoError, build_request, config_for_references,
                           load_config, reference_pin, reference_prompt_bindings, reference_slots,
                           validate_reference_capabilities, validate_reference_media)
from pianchang.production import validate
from trace_system import TraceError, current_context, event, get_tracer, register_secret, traced


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wire_hash(payload):
    # Match the exact encoding used by BailianVideoClient._request.
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _locator_digest(value):
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    # SDK stores only the hash of the full URI when persisting inline media.
    if isinstance(value, dict) and value.get("omitted") == "inline_media" and value.get("hash_basis") == "original_data_uri_utf8":
        return value.get("sha256")
    return None


def _comparison(path, expected, actual):
    return {"path": path, "expected": expected, "actual": actual, "status": "PASS" if expected == actual else "FAIL"}


def _decoded_rows(payload, pins, *, sdk=False):
    """Read exact media slots from the body, including the subject a voice is attached to."""
    inputs = payload["input"]
    media = inputs.get("media", [])
    slots = reference_slots(pins)
    checks = [_comparison("input.media.count", len(slots), len(media) if isinstance(media, list) else None)]
    rows = []
    ark_slot = 1
    if not isinstance(media, list) or len(media) != len(slots):
        return rows, checks
    for i, (image, voice) in enumerate(slots):
        fields = media[i]
        base = f"input.media[{i}]"
        checks.append(_comparison(base+".fields", sorted(["type","url"] + (["reference_voice"] if voice else [])),
                                  sorted(fields) if isinstance(fields, dict) else None))
        if not isinstance(fields, dict):
            continue
        checks.append(_comparison(base+".type","reference_image",fields.get("type")))
        for key, pin in (("url",image),("reference_voice",voice)):
            if pin:
                locator_path = base+"."+key
                if sdk:
                    modality = "image_url" if key == "url" else "audio_url"
                    locator_path = f"content[{ark_slot}].{modality}.url"
                    ark_slot += 1
                rows.append({**reference_pin(pin), "payload_path":locator_path,
                             "locator_sha256":_locator_digest(fields.get(key))})
    return rows, checks


@traced("video.references.check_request", kind="validation", capture_inputs=False)
def check_reference_request(config, payload, required_refs, resolved_refs, *, metadata=None):
    """Check the final provider payload immediately before the POST; never infer pins from a URL name."""
    if "dry_run" in payload or any(ref.get("dry_run") for ref in resolved_refs):
        raise VideoError("dry_run 计划不能提交。")
    clean = [reference_pin(p) for p in required_refs]
    expected_purpose = "video.reference_to_video" if clean else "video.text_to_video"
    if config.route is not None and config.route_purpose != expected_purpose:
        raise VideoError("提交素材与已选择的视频模型用途不符。")
    from pianchang.reference_inputs import effective_reference_pins
    effective=effective_reference_pins(metadata or {"required_refs":clean},config)
    validate_reference_capabilities(config, effective)
    validate_reference_media(config, resolved_refs)
    resolved = [reference_pin(p) for p in resolved_refs]
    rows, checks = _decoded_rows(payload, effective, sdk=config.sdk_video)
    checks.extend([_comparison("model", config.model, payload.get("model")),
                   _comparison("effective_refs", effective, resolved)])
    inputs = payload["input"]
    if clean:
        checks.append(_comparison("input.fields", [], sorted(set(inputs) - {"media","prompt","negative_prompt"})))
        checks.append(_comparison("input.prompt.subject_bindings", True,
                                  inputs.get("prompt","").endswith(reference_prompt_bindings(effective, sdk=config.sdk_video))))
    else:
        checks.append(_comparison("input.unbound_media_fields", [], sorted(set(inputs) - {"prompt","negative_prompt","audio_url"})))
    lookups = {(r["subject_id"],r["purpose"]):r for r in resolved_refs}
    for row in rows:
        item = lookups.get((row["subject_id"],row["purpose"]),{})
        checks.append(_comparison(row["payload_path"]+".locator_sha256",
                                  _locator_digest(item.get("provider_url")), row["locator_sha256"]))
    uploads = []
    for item in resolved_refs:
        if item["purpose"] != "voice" or config.sdk_video:
            continue
        from pianchang.reference_transport import verify_voice_upload
        upload = item["upload_receipt"]
        proof = verify_voice_upload(upload)
        checks.extend({**check, "path": item["subject_id"] + "." + check["path"]} for check in proof["checks"])
        uploads.append({
            **{key: upload.get(key) for key in ("provider_url", "media_sha256", "bytes", "trace_id", "span_id", "request_id")},
            "asset_id": item["asset_id"], "revision_id": item["revision_id"],
            "locator_sha256": _locator_digest(item["provider_url"])
        })
    report = {"status":"PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL","checks":checks}
    event("video.reference_preflight", report, kind="validation")
    if report["status"] != "PASS":
        problems = [c["path"] for c in checks if c["status"] != "PASS"]
        raise VideoError("实际请求与固定参考素材不符："+"、".join(problems))
    return {"schema_version":"video-reference-receipt/2" if (metadata or {}).get("reference_bundle") else "video-reference-receipt/1", "status":"PREPARED",
            "trace_id":(current_context() or {}).get("trace_id"), "wire_body_sha256":None if config.sdk_video else _wire_hash(payload),
            "required_refs":clean, "prepared_refs":rows,"submitted_refs":[],
            **({"reference_bundle":metadata["reference_bundle"],"effective_refs":effective} if (metadata or {}).get("reference_bundle") else {}),
            "voice_uploads":uploads,
            "unversioned_inputs":["input.audio_url"] if "audio_url" in inputs else []}


@traced("video.references.verify_receipt", kind="validation", capture_inputs=False)
def verify_reference_receipt(job):
    with get_tracer().read_batch():
        return _verify_reference_receipt(job)


def _verify_reference_receipt(job):
    """Verify a persisted receipt against its sanitized job; Trace retains the independent HTTP wire digest."""
    try:
        receipt = job["reference_receipt"]
        required = [reference_pin(p) for p in job["metadata"].get("required_refs", [])]
        from pianchang.reference_inputs import effective_reference_pins
        from pianchang.bailian_video import VideoConfig
        route = job["metadata"].get("model_route") or {}
        sdk = route.get("adapter") == "litellm"
        effective=effective_reference_pins(job["metadata"],VideoConfig(model=job["request"]["model"]) if job["metadata"].get("reference_bundle") and not route else None)
        rows, checks = _decoded_rows(job["request"], effective, sdk=sdk)
        status = receipt["status"]
        checks.extend([
            _comparison("receipt.schema", "video-reference-receipt/2" if job["metadata"].get("reference_bundle") else "video-reference-receipt/1", receipt.get("schema_version")),
            _comparison("receipt.status.valid",True,status in {"PREPARED","SUBMISSION_UNKNOWN","ACCEPTED","PROVIDER_REJECTED"}),
            _comparison("job.request_sha256", job["request_sha256"], _digest({k:job[k] for k in ("base_url","request","metadata")})),
            _comparison("receipt.required_refs", required, receipt.get("required_refs")),
            _comparison("receipt.prepared_refs", rows, receipt.get("prepared_refs")),
            _comparison("receipt.submitted_refs", rows if status in {"ACCEPTED","PROVIDER_REJECTED"} else [], receipt.get("submitted_refs")),
            _comparison("receipt.wire_sha256.format",True,bool(re.fullmatch(r"[a-f0-9]{64}",str(receipt.get("wire_body_sha256"))))
                        or (sdk and status in {"PREPARED","SUBMISSION_UNKNOWN"} and receipt.get("wire_body_sha256") is None)),
            _comparison("receipt.unversioned_inputs", ["input.audio_url"] if "audio_url" in job["request"]["input"] else [],
                        receipt.get("unversioned_inputs")),
            _comparison("receipt.trace_id.format", True, bool(re.fullmatch(r"[a-f0-9]{32}",str(receipt.get("trace_id"))))),
        ])
        if job["metadata"].get("reference_bundle"):
            checks.extend([_comparison("receipt.reference_bundle",job["metadata"]["reference_bundle"],receipt.get("reference_bundle")),
                           _comparison("receipt.effective_refs",effective,receipt.get("effective_refs"))])
        if status == "ACCEPTED":
            checks.append(_comparison("receipt.task_id", job.get("task_id"), receipt.get("task_id")))
            checks.append(_comparison("receipt.task_id.present", True, bool(receipt.get("task_id"))))
            if sdk:
                checks.append(_comparison("receipt.provider_task_id", job.get("task_id"), receipt.get("provider_task_id")))
                checks.append(_comparison("job.provider_task_id", job.get("task_id"), job.get("provider_task_id")))
        elif status == "PROVIDER_REJECTED":
            checks.append(_comparison("receipt.rejection_status_valid", True, receipt.get("http_status") in {400,401,403,404,413,415,422,429}))
        else:
            checks.append(_comparison("receipt.unconfirmed_task_id", None, job.get("task_id")))
        if required:
            supported = (route.get("sdk_provider") == "volcengine" and route.get("model_id") == job["request"]["model"]) if sdk else job["request"]["model"] in REFERENCE_MODELS
            checks.append(_comparison("receipt.model",True,supported))
            checks.append(_comparison("input.prompt.subject_bindings",True,
                                      job["request"]["input"]["prompt"].endswith(reference_prompt_bindings(effective, sdk=sdk))))
        allowed_inputs = {"media", "prompt", "negative_prompt"} if required else {"prompt", "negative_prompt", "audio_url"}
        checks.append(_comparison("input.extra_fields", [], sorted(set(job["request"]["input"]) - allowed_inputs)))
        upload_keys = ("asset_id", "revision_id", "media_sha256", "locator_sha256")
        expected_uploads = [{k:r[k] for k in upload_keys} for r in rows if r["purpose"] == "voice" and not sdk]
        uploads = receipt.get("voice_uploads", [])
        actual_uploads = [{k:r.get(k) for k in upload_keys} for r in uploads]
        checks.append(_comparison("receipt.voice_uploads", sorted(expected_uploads, key=lambda r:r["asset_id"]),
                                  sorted(actual_uploads, key=lambda r:r["asset_id"])))
        if uploads:
            from pianchang.reference_transport import verify_voice_upload
            for upload in uploads:
                locator = upload.get("provider_url")
                if locator is None:
                    checks.append({"path":"receipt.voice_upload.locator", "expected":upload.get("locator_sha256"),
                                   "actual":None, "status":"UNKNOWN"})
                else:
                    checks.append(_comparison("receipt.voice_upload.locator", upload.get("locator_sha256"),
                                              _locator_digest(locator)))
                checks.extend(verify_voice_upload(upload)["checks"])
    except (KeyError, ValueError, TypeError, AttributeError, OSError, VideoError) as exc:
        checks = [_comparison("receipt.structure", "valid", type(exc).__name__)]
    statuses = {check["status"] for check in checks}
    return {"status":"FAIL" if "FAIL" in statuses else ("UNKNOWN" if "UNKNOWN" in statuses else "PASS"), "checks":checks}


@traced("video.compile_shot", kind="tool")
def compile_shot(project, shot_id, *, prompt_override=None):
    """Compile a validated declared shot; this does not verify generated media."""
    if isinstance(project, dict) and "script_source" in project:
        from pianchang.story_spec import validate_compilation
        source_report = validate_compilation(project)
        if not source_report["ready_for_generation"]:
            errors = [f"{i['code']}: {i.get('path', '')}" for i in source_report["issues"]
                      if i.get("severity", "error") == "error"]
            raise VideoError("剧本来源或制作规格检查未通过：" + "; ".join(errors))
    report = validate(project)
    if not report["spec_pass"]:
        errors = [f"{i['code']}: {i['path']}" for i in report["issues"]
                  if i["severity"] == "error"]
        raise VideoError("制作规格检查未通过：" + "; ".join(errors))
    shot = next((s for s in project["shots"] if s["id"] == shot_id), None)
    if shot is None:
        raise VideoError(f"未知镜头：{shot_id}")
    state = next(t for t in report["state_trace"] if t["shot_id"] == shot_id)
    return _render_shot(project, shot, state, prompt_override)


@traced("video.project_preview", kind="tool", capture_inputs=False, capture_output=False)
def compile_project_previews(project):
    """Build all workbench previews from the same validated snapshot."""
    from pianchang.story_spec import validate_compilation
    validation = validate_compilation(project)
    ready = validation["ready_for_generation"] if "script_source" in project else validation["spec_pass"]
    previews = {}
    if ready:
        states = {row["shot_id"]: row for row in validation["state_trace"]}
        for shot in project["shots"]:
            if isinstance(shot.get("description"), str) and shot["description"].strip():
                previews[shot["id"]] = _render_shot(project, shot, states[shot["id"]], None)
    event("video.project_preview.compiled", {"project_sha256": _digest(project), "shot_ids": list(previews)})
    return validation, previews


def _render_shot(project, shot, trace, prompt_override):
    shot_id = shot["id"]
    if prompt_override is None and (not isinstance(shot.get("description"), str)
                                    or not shot["description"].strip()):
        raise VideoError(f"镜头 {shot_id} 缺少 description，请补充动作描述或显式提供提示词。")
    entity_ids = list(dict.fromkeys([shot["scene_id"], *shot["entity_ids"]]))
    relevant = set(entity_ids)
    before = {k: v for k, v in trace["before"].items() if k.split(".", 1)[0] in relevant}
    changes = {k: v for k, v in trace["after"].items()
               if k in before and before[k] != v}
    canonical = {eid: project["entities"][eid]["canonical"] for eid in entity_ids}
    event_by_id = {e["id"]: e for e in project["events"]}
    events = [event_by_id[eid]["reason"] for eid in shot["event_ids"]]
    prompt = prompt_override if prompt_override is not None else "\n".join([
        ("按下列分镜顺序生成一段有切镜的完整短剧视频。保持人物、服装、道具和空间关系稳定。"
         if len(shot.get("creative_shots", [])) > 1 else
         "生成单镜头短剧视频。保持人物外观、服装、道具和空间关系稳定。"),
        f"本镜头：{shot.get('description', '')}",
        "人物与场景设定：" + _json(canonical),
        "镜头开始状态（ID对应上述设定）：" + _json(before),
        "本镜头事件：" + ("；".join(events) or "无状态变更，按镜头描述表演。"),
        "结束时的变化：" + _json(changes),
        "结束状态要求：" + _json(shot["expected_after"]),
        "未变化的状态保持不变；房间方位是世界坐标，不等于画面左右。",
    ])
    duration = shot["duration_s"]
    if type(duration) is float and duration.is_integer():
        duration = int(duration)
    compiled = {
        "prompt": prompt,
        "duration": duration,
        "metadata": {
            "project_title": project.get("title"),
            "project_sha256": _digest(project),
            "shot_id": shot_id,
            "input_versions": shot["input_versions"],
            "continuity_from": shot["continuity_from"],
            "target_duration_s": shot["duration_s"],
            "state_before": trace["before"],
            "state_after": trace["after"],
            "spec_pass": True,
            "prompt_overridden": prompt_override is not None,
            "reference_media_used": False,
        },
    }
    if "script_source" in project:
        compiled["metadata"].update(script_sha256=project["script_source"]["sha256"],
                                    source_refs=shot["source_refs"],
                                    episode_id=shot["episode_id"],
                                    script_scene_id=shot["script_scene_id"],
                                    semantic_fidelity_pass=None)
    if shot.get("required_refs"):
        compiled["metadata"].pop("reference_media_used", None)
        compiled["metadata"]["required_refs"] = copy.deepcopy(shot["required_refs"])
    return compiled


def config_for_manifest(config, saved):
    metadata = saved.get("metadata", {})
    snapshot = metadata.get("model_route")
    if snapshot is not None or saved.get("schema_version") == "provider-video-job/2":
        purpose = "video.reference_to_video" if metadata.get("required_refs") else "video.text_to_video"
        if (not isinstance(snapshot, dict) or saved.get("request_sha256") != _digest(
                {k:saved[k] for k in ("base_url","request","metadata")})
                or metadata.get("model_purpose", purpose) != purpose
                or snapshot.get("base_url") != saved.get("base_url")
                or snapshot.get("model_id") != saved.get("request", {}).get("model")
                or snapshot.get("provider") != saved.get("provider")):
            raise VideoError("视频任务的冻结路由与提交记录不符。")
        if config.route is None or config.route.snapshot() != snapshot:
            return config_for_references(config, metadata.get("required_refs", []), snapshot=snapshot)
    elif config.route is not None:
        return load_config(config.env_file, allow_missing=True)
    return config


def _submission_intent(payload, metadata, effective, *, sdk):
    if metadata.get("execution_key"):
        return _digest({"execution_key":metadata["execution_key"]})
    inputs = payload["input"]
    prompt = inputs["prompt"]
    if effective:
        prompt = prompt.removesuffix(reference_prompt_bindings(effective, sdk=sdk))
    return _digest({"prompt":prompt, "negative_prompt":inputs.get("negative_prompt"), "audio_url":inputs.get("audio_url"),
                    "duration":payload["parameters"]["duration"], "seed":payload["parameters"].get("seed"),
                    "metadata":{k:v for k,v in metadata.items() if k not in {"model_route","model_purpose"}}})


def verified_local_failure(job, manifest):
    if job.get("status") != "SUBMISSION_NOT_SENT" or job.get("task_id"):
        return False
    context = job.get("not_sent_context") or {}
    if not context.get("trace_id") or not context.get("span_id"):
        return False
    tracer = get_tracer()
    proof = {"manifest_path":str(Path(manifest).resolve()), "request_sha256":job["request_sha256"],
             "candidate_key":job["candidate_key"], "network_dispatched":False}
    return any(tracer.resolve(e["data"]) == proof for e in tracer.get_events(context["trace_id"],
               span_id=context["span_id"], names=["video.task_not_dispatched"]))


def submission_rejection(job, manifest):
    """Read a definitive negative submission receipt. A missing task ID alone proves nothing."""
    from pianchang.reference_usage import verify_reference_exchange
    receipt = job.get("reference_receipt") or {}
    if (job.get("status") != "SUBMISSION_REJECTED" or job.get("task_id")
            or receipt.get("status") != "PROVIDER_REJECTED" or verify_reference_receipt(job)["status"] != "PASS"):
        return None
    wire = verify_reference_exchange(job, receipt, receipt["trace_id"])
    if wire["status"] != "MATCHED_RESPONSE":
        return None
    error = wire.get("provider_error") or {}
    detail = " ".join(error.values()) or str(job.get("last_error") or "")[:800]
    slots = set(re.findall(r"\bcontent\[\d+\]", detail))
    references = [{k: row[k] for k in ("asset_id", "revision_id", "purpose", "payload_path")}
                  for row in receipt["submitted_refs"] if row["payload_path"].split(".", 1)[0] in slots]
    provider = job.get("provider") or (job["metadata"].get("model_route") or {}).get("provider") or "aliyun-bailian"
    reason = f"{provider} 已拒绝视频提交（HTTP {receipt['http_status']}）：{detail}"
    if error.get("code") == "InputImageSensitiveContentDetected.PrivacyInformation":
        reason += " 请选用符合供应商要求的参考图，并重新确认使用新素材的拍摄计划。"
    return {"kind":"submission_rejected", "terminal_failure":True, "reason":reason,
            "provider":provider, "model":job["request"]["model"], "http_status":receipt["http_status"],
            "provider_request_id":receipt.get("provider_request_id"), "provider_error":error, "references":references,
            "shot_id":job.get("metadata", {}).get("shot_id"), "manifest_path":str(manifest),
            "manifest_sha256":_digest(job), "execution_key":job.get("metadata", {}).get("execution_key")}


def bound_submission_rejection(job, manifest, *, directory, shot_id, repair_id, script_ref, dependency_sha256):
    from pianchang.story_spec import digest_json
    expected_key = digest_json({"order":str(directory), "shot_id":shot_id, "repair_id":repair_id})
    meta = job.get("metadata") or {}
    if (meta.get("execution_key") != expected_key or meta.get("shot_id") != shot_id
            or meta.get("script_asset_ref") != script_ref or meta.get("dependency_sha256") != dependency_sha256):
        return None
    rejection = submission_rejection(job, manifest)
    return {**rejection, "repair_id":repair_id} if rejection else None


def _reserve_submission(directory, name, payload, metadata, effective, config):
    """Serialize creation for the same creative input across model/connection changes."""
    key = _submission_intent(payload, metadata, effective, sdk=config.sdk_video)
    with closing(sqlite3.connect(directory / ".video-submissions.sqlite3")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS claims(intent TEXT PRIMARY KEY, candidate TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] == 0:
            for manifest in directory.glob("*/job.json"):
                try:
                    old = json.loads(manifest.read_text(encoding="utf-8-sig"))
                    if (old.get("schema_version") != "bailian-video-job/1" or old.get("task_id")
                            or old.get("status") == "SUBMISSION_REJECTED"):
                        continue
                    meta = old["metadata"]
                    refs = (old.get("reference_receipt") or {}).get("effective_refs", meta.get("required_refs", []))
                    old_key = _submission_intent(old["request"], meta, refs, sdk=False)
                    db.execute("INSERT OR IGNORE INTO claims VALUES (?,?)", (old_key,manifest.parent.name))
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
            db.execute("PRAGMA user_version=1")
        row = db.execute("SELECT candidate FROM claims WHERE intent=?", (key,)).fetchone()
        if row:
            prior = row[0]
            if not re.fullmatch(r"[a-f0-9]{24}(?:-[a-f0-9]{12})?", prior):
                raise VideoError("原始提交占位记录损坏，须先核对。")
            prior_path = directory / prior / "job.json"
            previous = json.loads(prior_path.read_text(encoding="utf-8-sig")) if prior_path.is_file() else {}
            if (not previous.get("task_id") and previous.get("status") != "SUBMISSION_REJECTED"
                    and not verified_local_failure(previous, prior_path)):
                raise VideoError(f"同一输入已有结果未知的提交，不能自动再次提交：{prior_path}")
        db.execute("INSERT OR REPLACE INTO claims VALUES (?,?)", (key,name))


class VideoRunner:
    """Persistent task lifecycle. Identical input reuses its existing task.

    A failed or ambiguous POST is never automatically retried. Once a task ID
    exists, resume only performs GETs and downloads. Manifests contain no API key.
    """

    def __init__(self, config, client=None, *, sleep=time.sleep, monotonic=time.monotonic):
        register_secret(config.api_key)
        self.config = config
        from pianchang.sdk_video import SDKVideoClient
        self.client = client or (SDKVideoClient(config) if config.sdk_video else BailianVideoClient(config))
        self.injected_client = client
        self.sleep = sleep
        self.monotonic = monotonic

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self.config.api_key, "[REDACTED]") if self.config.api_key else value
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def _save(self, path, job):
        job["updated_at"] = _now()
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".job-", suffix=".tmp", delete=False) as handle:
                temp_path = Path(handle.name)
                json.dump(get_tracer().sanitize(self._redact(job)), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            from pianchang.atomic_files import replace_with_retry
            replace_with_retry(temp_path, path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _load(self, path):
        path = Path(path).resolve()
        job = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(job, dict) or job.get("schema_version") not in {"bailian-video-job/1", "provider-video-job/2"}:
            raise VideoError("不是受支持的视频任务清单。")
        if job.get("base_url") != self.config.base_url.rstrip("/"):
            raise VideoError("任务地域/Endpoint 与当前配置不同，请使用提交任务时的 DASHSCOPE_BASE_URL。")
        if job.get("metadata", {}).get("required_refs") and verify_reference_receipt(job)["status"] != "PASS":
            raise VideoError("已有任务的固定参考与实际提交回执不符，保留记录待核实。")
        return path, job

    @staticmethod
    def _result(path, job):
        return {**job, "manifest_path": str(path)}

    @traced("video.submit", kind="tool")
    def submit(self, payload, metadata=None, *, new=False, resolved_refs=()):
        """Save request before POST and task ID immediately afterwards."""
        if not self.config.legacy_submission and self.config.route is None and self.config.env_file is not None:
            from pianchang.model_routes import load_routes
            pins = (metadata or {}).get("required_refs", [])
            purpose = "video.reference_to_video" if pins else "video.text_to_video"
            snapshot = load_routes(self.config.env_file).resolve(purpose).snapshot()
            selected = config_for_references(self.config, pins, snapshot=snapshot)
            runner = VideoRunner(selected, self.injected_client, sleep=self.sleep, monotonic=self.monotonic)
            return runner._submit(payload, metadata, new=new, resolved_refs=resolved_refs)
        return self._submit(payload, metadata, new=new, resolved_refs=resolved_refs)

    def _submit(self, payload, metadata, *, new, resolved_refs):
        if not self.config.api_key.strip():
            raise VideoError("请填写当前视频账户配置的 API key；--dry-run 无需 Key。")
        metadata = copy.deepcopy(metadata or {})
        payload = copy.deepcopy(payload)
        required = metadata.get("required_refs", [])
        if self.config.route is not None:
            metadata["model_route"] = self.config.route.snapshot()
            metadata["model_purpose"] = self.config.route_purpose
        receipt = check_reference_request(self.config, payload, required, resolved_refs, metadata=metadata)
        from pianchang.reference_inputs import effective_reference_pins
        effective=effective_reference_pins(metadata,self.config)
        if required:
            # Actual inclusion and provider acceptance are recorded separately in the receipt.
            metadata.pop("reference_media_used", None)
        signature = {"base_url": self.config.base_url.rstrip("/"),
                     "request": get_tracer().sanitize(payload), "metadata": get_tracer().sanitize(metadata)}
        fingerprint = _digest(signature)
        if required:
            logical = copy.deepcopy(signature)
            logical["request"]["input"]["media"] = [
                {"type":"reference_image", "url":reference_pin(image),
                 **({"reference_voice":reference_pin(voice)} if voice else {})}
                for image,voice in reference_slots(effective)]
            candidate_key = _digest(logical)
        else:
            candidate_key = fingerprint
        # Pre-gateway jobs did not include the connection in their signature. Reuse
        # the original slot (including an uncertain POST) on the same Bailian account.
        if not new and self.config.route and self.config.route.connection == "bailian.media":
            legacy = copy.deepcopy(logical if required else signature)
            legacy["metadata"].pop("model_route", None)
            legacy["metadata"].pop("model_purpose", None)
            legacy_key = _digest(legacy)
            original = Path(self.config.output_dir) / legacy_key[:24] / "job.json"
            if original.exists():
                _, old = self._load(original)
                if (old.get("schema_version") != "bailian-video-job/1"
                        or old.get("candidate_key", old.get("request_sha256")) != legacy_key
                        or not old.get("task_id") or required and (old.get("reference_receipt") or {}).get("status") != "ACCEPTED"):
                    raise VideoError(f"迁移前的候选尚未核实，不能自动再次提交：{original}")
                event("cache.hit", {"manifest_path":str(original), "provider_task_id":old["task_id"],
                                    "network_submission_performed":False}, kind="cache")
                return self._result(original, old)
        name = candidate_key[:24] + ("-" + uuid.uuid4().hex[:12] if new else "")
        directory = Path(self.config.output_dir).resolve() / name
        path = directory / "job.json"
        while self.config.sdk_video and path.is_file() and verified_local_failure(json.loads(path.read_text(encoding="utf-8-sig")), path):
            # Keep the failed manifest and use a reproducible slot for the corrected attempt.
            name = candidate_key[:24] + "-" + _digest({"local_attempt":name})[:12]
            directory = directory.parent / name
            path = directory / "job.json"
        if self.config.route is not None and not path.exists():
            directory.parent.mkdir(parents=True, exist_ok=True)
            _reserve_submission(directory.parent, name, payload, metadata, effective, self.config)
        directory.mkdir(parents=True, exist_ok=True)
        job = {
            "schema_version": "provider-video-job/2" if self.config.route else "bailian-video-job/1",
            "provider": self.config.route.provider if self.config.route else "aliyun-bailian",
            **signature,
            "request_sha256": fingerprint,
            "candidate_key": candidate_key,
            "reference_receipt": receipt,
            "created_at": _now(),
            "status": "SUBMITTING",
            "task_id": None,
            "media_generated": False,
            "visual_pass": None,
            "audio_pass": None,
            "usage": None,
            "cost": None,
        }
        try:
            # Exclusive creation prevents two workers submitting the same request.
            with path.open("x", encoding="utf-8") as handle:
                json.dump(get_tracer().sanitize(self._redact(job)), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            path, existing = self._load(path)
            if not existing.get("task_id"):
                rejected = submission_rejection(existing, path)
                if rejected:
                    raise VideoError(rejected["reason"] + " 已保留原回执：" + str(path))
                raise VideoError(f"已有提交记录但尚无任务 ID，不能自动再次提交：{path}。"
                                 "请先核对原供应商的任务记录。")
            if required:
                check = verify_reference_receipt(existing)
                if (existing.get("candidate_key") != candidate_key or check["status"] != "PASS"
                        or existing["reference_receipt"]["status"] != "ACCEPTED"):
                    raise VideoError("已有同一候选记录，但其参考回执无法核实；请检查原始请求。")
            event("cache.hit", {"manifest_path": str(path), "provider_task_id": existing["task_id"],
                                "network_submission_performed": False}, kind="cache")
            return self._result(path, existing)
        try:
            receipt["status"] = "SUBMISSION_UNKNOWN"
            self._save(path, job)
            if self.config.sdk_video:
                def record_wire(wire_sha256):
                    receipt["wire_body_sha256"] = wire_sha256
                    self._save(path, job)
                response = self._redact(self.client.submit(payload, record_wire=record_wire))
            else:
                response = self._redact(self.client.submit(payload))
            output = response.get("output", {})
            if not isinstance(output, dict) or not isinstance(output.get("task_id"), str) or not output["task_id"]:
                raise VideoError("提交返回缺少 task_id；请核对原供应商的任务记录，勿直接重复提交。")
            job.update(task_id=output["task_id"], status=output.get("task_status", "PENDING"),
                       provider_response=response, request_id=response.get("request_id"))
            receipt.update(status="ACCEPTED", submitted_refs=receipt["prepared_refs"],
                           task_id=output["task_id"], provider_request_id=response.get("request_id"))
            if self.config.sdk_video:
                receipt.update(wire_body_sha256=response["wire_receipt"]["wire_body_sha256"],
                               provider_task_id=response["provider_task_id"])
                job["provider_task_id"] = response["provider_task_id"]
                job["sdk_expected_size"] = response.get("expected_size")
            self._save(path, job)
            event("video.task_submitted", {"manifest_path": str(path), "provider_task_id": job["task_id"],
                                          "provider_status": job["status"]}, kind="state")
        except TraceError:
            # Preserve the existing submission journal; stop without masking a
            # trace failure or attempting another provider request.
            raise
        except (Exception, KeyboardInterrupt) as exc:
            # The provider may have accepted a POST even if its response was lost.
            job["status"] = "SUBMISSION_UNKNOWN" if not job.get("task_id") else job["status"]
            if self.config.sdk_video and getattr(exc,"network_dispatched",None) is False:
                event("video.task_not_dispatched", {"manifest_path":str(path), "request_sha256":fingerprint, "candidate_key":candidate_key,
                      "network_dispatched":False}, kind="state")
                job.update(status="SUBMISSION_NOT_SENT", not_sent_context=current_context())
                receipt["status"] = "PREPARED"
            elif isinstance(exc,VideoError) and exc.http_status in {400,401,403,404,413,415,422,429}:
                receipt.update(status="PROVIDER_REJECTED", submitted_refs=receipt["prepared_refs"],
                               http_status=exc.http_status, provider_request_id=exc.request_id)
                job["status"] = "SUBMISSION_REJECTED"
            job["last_error"] = self._redact(str(exc)) or "操作中断"
            self._save(path, job)
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise VideoError(f"{job['last_error']}；任务记录：{path}") from exc
        return self._result(path, job)

    @traced("video.resume", kind="tool")
    def resume(self, manifest_path, *, wait=True, download=True):
        """Refresh an existing task and optionally wait and download its video."""
        saved = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
        restored = config_for_manifest(self.config, saved)
        if restored is not self.config:
            return VideoRunner(restored, self.injected_client, sleep=self.sleep, monotonic=self.monotonic).resume(
                manifest_path, wait=wait, download=download)
        path, job = self._load(manifest_path)
        task_id = job.get("task_id")
        if not task_id:
            if verified_local_failure(job, path):
                raise VideoError("原始记录已证明请求未发出，请修复本地配置后重新执行生成。")
            raise VideoError("记录中没有 task_id；请在原供应商处核对提交结果，不能自动重提。")
        video_path = path.parent / "video.mp4"
        if (download and job.get("download_sha256") and video_path.is_file()
                and _file_digest(video_path) == job["download_sha256"]):
            try:
                if self.config.sdk_video:
                    from pianchang.bailian_video import validate_mp4_header
                    with video_path.open("rb") as handle:
                        validate_mp4_header(handle.read(12), video_path.stat().st_size)
            except VideoError:
                event("video.cached_download_invalid", {"manifest_path":str(path),"media_sha256":job["download_sha256"]},kind="validation")
                for field in ("download_sha256","download_bytes","local_video_path"):
                    job.pop(field,None)
                self._save(path,job)
            else:
                event("cache.hit", {"manifest_path": str(path), "media_sha256": job["download_sha256"],
                                    "network_request_performed": False}, kind="cache")
                return self._result(path, job)
        deadline = self.monotonic() + self.config.wait_timeout
        while True:
            try:
                response = self._redact(self.client.get_task(task_id))
                output = response.get("output", {})
                if not isinstance(output, dict) or not isinstance(output.get("task_status"), str):
                    raise VideoError("查询返回缺少 output.task_status。")
                if output.get("task_id", task_id) != task_id:
                    raise VideoError("查询返回的 task_id 与本地任务不一致。")
                status = output["task_status"]
                event("video.task_polled", {"provider_task_id": task_id, "status": status}, kind="state")
                job.update(status=status, provider_response=response,
                           request_id=response.get("request_id"), usage=response.get("usage"))
                job.pop("last_error", None)
                self._save(path, job)
                if status == "SUCCEEDED":
                    url = output.get("video_url")
                    if not self.config.sdk_video and (not isinstance(url, str) or not url):
                        raise VideoError("任务成功但没有 video_url，已保留响应供排查。")
                    if url:
                        job["video_url"] = url
                    job["media_generated"] = True
                    if download:
                        saved = (self.client.download_task(task_id, video_path) if self.config.sdk_video
                                 else self.client.download(url, video_path))
                        job["local_video_path"] = str(saved.resolve())
                        job["download_sha256"] = _file_digest(saved)
                        job["download_bytes"] = saved.stat().st_size
                    self._save(path, job)
                    return self._result(path, job)
                if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                    raise VideoError(f"视频任务 {task_id} 状态 {status}："
                                     f"{output.get('code', '')} {output.get('message', '')}".strip())
                if status not in {"PENDING", "RUNNING"}:
                    raise VideoError(f"未识别的任务状态 {status}；已保留 task_id，未重提。")
                if not wait:
                    return self._result(path, job)
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise VideoError(f"等待超时，云端任务可能仍在运行。请使用 resume 继续查询：{path}")
                self.sleep(min(self.config.poll_interval, remaining))
            except (VideoError, OSError, ValueError) as exc:
                job["last_error"] = self._redact(str(exc))
                self._save(path, job)
                raise VideoError(f"{job['last_error']}；任务记录：{path}") from exc


def generate_video(config, prompt, *, duration=None, negative_prompt=None, seed=None,
                   audio_url=None, metadata=None, wait=True, new=False, resolved_refs=()):
    """Agent-facing callable: validated request -> persistent task -> local MP4."""
    config = config_for_references(config, (metadata or {}).get("required_refs", []))
    payload = build_request(config, prompt, duration=duration, negative_prompt=negative_prompt,
                            seed=seed, audio_url=audio_url, resolved_refs=resolved_refs)
    runner = VideoRunner(config)
    job = (runner.submit(payload, metadata, new=new, resolved_refs=resolved_refs) if resolved_refs
           else runner.submit(payload, metadata, new=new))
    return runner.resume(job["manifest_path"]) if wait else job


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="默认读取代码目录的 .env，系统环境变量优先")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="显示配置和 Key 是否已填写，不显示 Key 内容")
    generate = sub.add_parser("generate", help="生成单个视频或单个镜头；真实请求按所选供应商规则计费")
    generate.add_argument("--prompt", help="文本提示词；与 --project 同用时覆盖自动编译的提示词")
    generate.add_argument("--prompt-file", type=Path, help="UTF-8 提示词文件")
    generate.add_argument("--project", type=Path, help="制作工程 JSON")
    generate.add_argument("--shot", help="镜头 ID，例如 S001")
    generate.add_argument("--duration", type=int, help="显式覆盖生成时长，2–15 秒整数")
    generate.add_argument("--negative-prompt")
    generate.add_argument("--seed", type=int)
    generate.add_argument("--audio-url")
    generate.add_argument("--reference-store", type=Path, help="镜头参考资产所在的协作 SQLite 库")
    generate.add_argument("--reference-story-id", help="参考资产所属故事 ID")
    generate.add_argument("--dry-run", action="store_true", help="仅编译并打印请求，无需 Key、不联网")
    generate.add_argument("--no-wait", action="store_true", help="保存 task_id 后返回")
    generate.add_argument("--new", action="store_true", help="明确创建新候选，发生新的生成费用")
    for name, help_text in (("status", "查询一次已有任务"), ("resume", "继续等待并下载已有任务")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("manifest", type=Path, help="生成命令输出的 job.json 路径")
    return parser


@traced("cli.video_agent", kind="cli")
def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.env_file)
        if args.command == "config":
            # Explicit allowlist avoids accidental disclosure from dataclass/asdict.
            result = {"api_key_configured": bool(config.api_key),
                      "base_url": config.base_url, "model": config.model, "reference_model": config.reference_model,
                      "resolution": config.resolution, "ratio": config.ratio,
                      "default_duration_s": config.duration,
                      "output_dir": str(config.output_dir),
                      "poll_interval_s": config.poll_interval,
                      "wait_timeout_s": config.wait_timeout}
            if config.env_file is not None:
                from pianchang.model_routes import load_routes
                registry = load_routes(config.env_file)
                plain = registry.resolve("video.text_to_video")
                refs = registry.resolve("video.reference_to_video")
                result.update(api_key_configured=bool(plain.api_key), base_url=plain.base_url, model=plain.model_id,
                              reference_model=refs.model_id, video_routes={"video.text_to_video":plain.snapshot(),
                                                                        "video.reference_to_video":refs.snapshot()})
        elif args.command in {"status", "resume"}:
            result = VideoRunner(config).resume(args.manifest, wait=args.command == "resume",
                                                download=args.command == "resume")
        else:
            if args.prompt is not None and args.prompt_file is not None:
                raise VideoError("--prompt 和 --prompt-file 只能使用一个。")
            prompt = (args.prompt_file.read_text(encoding="utf-8-sig").strip()
                      if args.prompt_file else args.prompt)
            if args.project:
                if not args.shot:
                    raise VideoError("--project 必须同时指定 --shot。")
                project = json.loads(args.project.read_text(encoding="utf-8-sig"))
                compiled = compile_shot(project, args.shot, prompt_override=prompt)
                prompt, metadata = compiled["prompt"], compiled["metadata"]
                duration = args.duration if args.duration is not None else compiled["duration"]
                metadata["project_path"] = str(args.project.resolve())
            else:
                if args.shot:
                    raise VideoError("--shot 必须同时指定 --project。")
                if prompt is None:
                    raise VideoError("请提供 --prompt、--prompt-file 或 --project 与 --shot。")
                duration, metadata = args.duration, {}
            config = config_for_references(config, metadata.get("required_refs", []))
            options = {"duration":duration,"negative_prompt":args.negative_prompt,
                       "seed":args.seed,"audio_url":args.audio_url}
            resolved_refs = []
            if args.reference_store or args.reference_story_id or metadata.get("required_refs"):
                if not args.project or not args.reference_story_id or not args.reference_store or not args.reference_store.is_file():
                    raise VideoError("使用固定参考的镜头需要 --project、已有 --reference-store 和 --reference-story-id。")
                from pianchang.collab_store import CollabStore
                from pianchang.reference_transport import prepare_shot_request
                if project.get("reference_story_id", args.reference_story_id) != args.reference_story_id:
                    raise VideoError("所选参考故事与制作规格不符。")
                metadata.update(story_id=args.reference_story_id, reference_story_id=args.reference_story_id)
                payload, resolved_refs = prepare_shot_request(config, compiled, store=CollabStore(args.reference_store),
                    story_id=args.reference_story_id, request_options=options, dry_run=args.dry_run)
            else:
                payload = build_request(config, prompt, **options)
            generated_duration = payload["parameters"]["duration"]
            metadata["generated_duration_s"] = generated_duration
            metadata["duration_differs_from_target"] = (
                metadata.get("target_duration_s", generated_duration) != generated_duration)
            if args.dry_run:
                result = {"dry_run": True, "base_url": config.base_url,
                          "request": payload, "metadata": metadata,
                          "media_generated": False, "visual_pass": None, "audio_pass": None}
                if config.route:
                    result["model_route"] = config.route.snapshot()
            else:
                runner = VideoRunner(config)
                job = (runner.submit(payload, metadata, new=args.new, resolved_refs=resolved_refs) if resolved_refs
                       else runner.submit(payload, metadata, new=args.new))
                print(f"任务 ID：{job['task_id']}\n任务记录：{job['manifest_path']}",
                      file=sys.stderr, flush=True)
                result = job if args.no_wait else runner.resume(job["manifest_path"])
        print(json.dumps(get_tracer().sanitize(result), ensure_ascii=False, indent=2))
        return 0
    except (VideoError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断本地等待；已保存的云端任务不会取消，请用 resume 继续。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    # Windows redirected stdout otherwise uses the local ANSI code page, which
    # corrupts Chinese JSON when consumed by an agent expecting UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
