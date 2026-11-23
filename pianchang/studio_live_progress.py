"""Read a video commission's on-disk progress without acquiring its worker or contacting the provider."""
import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from pianchang.story_spec import digest_json
from trace_system import event, traced


_ID = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")
_ACTIVE = {"queued", "running"}
_TERMINAL_FAILURE = {"FAILED", "CANCELED", "CANCELLED"}


def _record_file(path):
    """Read one stable JSON checkpoint and return the time of that version."""
    try:
        stamp = _stamp(path)
        value = _read(path)
        if value and _stamp(path) == stamp:
            return value, datetime.fromtimestamp(stamp[3] / 1e9, timezone.utc)
    except (OSError, ValueError):
        pass
    return {}, None


@traced("studio.video.last_shot_records", kind="tool", capture_inputs=False, capture_output=False)
def last_shot_records(root, project_id, job):
    """Historical shot ledger for the sidebar, without inspecting or authorizing any media."""
    if not isinstance(job, dict) or job.get("kind") != "studio_video" or job.get("project_id") != project_id:
        return None
    jid, run, mid = (job.get(key) for key in ("id", "run_id", "message_id"))
    if not all(isinstance(v, str) and _ID.fullmatch(v) for v in (jid, run, mid)):
        return None
    store = Path(root) / "outputs/collaborations"
    database = store / "assets.sqlite3"
    if not database.is_file():
        return None
    try:
        with closing(sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True, timeout=0.1)) as db:
            row = db.execute("""SELECT m.created_at, json_extract(m.payload,'$.script'),
                    json_extract(m.payload,'$.shooting_plan') FROM messages m JOIN deliveries d USING(message_id)
                    WHERE m.message_id=? AND m.story_id=? AND m.correlation_id=? AND m.recipient='video'
                    AND m.message_type='production.requested' AND d.agent='video'""", (mid, "story-"+run, run)).fetchone()
        if not row:
            return None
        stamp = datetime.fromisoformat(row[0])
        if stamp.tzinfo is None:
            return None
        script, plan = json.loads(row[1]), json.loads(row[2])
        if not isinstance(script, dict) or not isinstance(plan, dict):
            return None
        if (plan.get("project_id") != project_id or not _SHA.fullmatch(str(job.get("plan_id")))
                or job["plan_id"] not in (plan.get("plan_id"), plan.get("base_plan_id"))
                or not script.get("asset_id") or not script.get("revision_id")
                or not _SHA.fullmatch(str(script.get("sha256"))) or plan.get("script_ref", script) != script):
            return None
        operations = {}
        for key, operation in (("new_shots","new"), ("retake_shots","retake"), ("reuse_shots","reuse")):
            ids = plan[key]
            if not isinstance(ids, list):
                return None
            for sid in ids:
                if not isinstance(sid, str) or not sid or sid in operations:
                    return None
                operations[sid] = operation
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        return None
    if not operations:
        return None

    work = store / "runs" / run / ("video-spec-"+mid)
    clips, clip_stamp = _record_file(work / "clips.json")
    shots = []
    for sid, operation in operations.items():
        clip = clips.get(sid)
        status = "pending"
        if clip is not None:
            status = "unknown"
            if (isinstance(clip,dict) and clip.get("shot_id")==sid and clip.get("script_ref")==script
                    and clip.get("task_id") and _SHA.fullmatch(str(clip.get("media_sha256")))
                    and _SHA.fullmatch(str(clip.get("generation_job_sha256")))):
                origin = clip.get("operation")
                if origin=="reused" and operation=="reuse":
                    status="reused"
                elif origin=="generated" and (operation!="reuse" or clip.get("repair_id")):
                    status="generated"
        shots.append({"shot_id":sid, "operation":operation, "status":status})
    if clip_stamp:
        stamp=max(stamp,clip_stamp)
    if any(shot["status"]=="pending" for shot in shots):
        saved, run_stamp = _record_file(work / "agent/run.json")
        if saved.get("shooting_plan")==plan and isinstance(saved.get("reserved_shots"),list):
            if run_stamp:
                stamp=max(stamp,run_stamp)
            reserved={v for v in saved["reserved_shots"] if isinstance(v,str)}
            for shot in shots:
                if shot["status"]=="pending" and shot["shot_id"] in reserved:
                    shot["status"]="reserved"
    completed=sum(shot["status"] in {"generated","reused"} for shot in shots)
    result={"project_id":project_id, "job_id":jid, "message_id":mid, "recorded_at":stamp.astimezone(timezone.utc).isoformat(),
            "completed_count":completed, "shot_count":len(shots), "shots":shots}
    event("studio.video.shot_record_scope", {"project_id":project_id, "job_id":jid, "path":"shooting_plan.project_id",
          "expected":project_id, "actual":plan["project_id"], "status":"PASS", "message_id":mid}, kind="validation")
    return result


def _read(path):
    # Some older writer files are rewritten in place. An incomplete read is retried by the next poll.
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _stamp(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


@lru_cache(maxsize=2048)
def _media_hash(path, stamp):
    with path.open("rb") as media:
        sha = hashlib.file_digest(media, "sha256").hexdigest()
    return sha if _stamp(path) == stamp else None


@lru_cache(maxsize=2048)
def _manifest(path, stamp):
    data = _read(path)
    if not data:
        return {}
    metadata = data.get("metadata") or {}
    return {"file": path, "hash": digest_json(data), "status": data.get("status"), "task_id": data.get("task_id"),
            "plan_id": metadata.get("shooting_plan_id"), "script": metadata.get("script_asset_ref"),
            "shot_id": metadata.get("shot_id"), "dependency": metadata.get("dependency_sha256"),
            "media": data.get("local_video_path"), "media_sha256": data.get("download_sha256"), "recovery": bool(data.get("recovery_source")),
            "repair_id": (metadata.get("acceptance_repair") or {}).get("repair_id"), "execution_key": metadata.get("execution_key"),
            "updated_at": data.get("updated_at"), "provider_status": (data.get("provider_response") or {}).get("output", {}).get("task_status"),
            "provider_task_id": (data.get("provider_response") or {}).get("output", {}).get("task_id")}


def _candidate(path, video_root):
    try:
        path = path.resolve()
        if path.name != "job.json" or path.parent.parent != video_root:
            return {}
        return _manifest(path, _stamp(path))
    except (OSError, ValueError):
        return {}


def _verified_file(path, expected, parent):
    if not path or not _SHA.fullmatch(str(expected)):
        return None
    try:
        path = Path(path).resolve()
        if path.name != "video.mp4" or path.parent != parent or not path.is_file():
            return None
        stamp = _stamp(path)
        if stamp[2] > 0 and _media_hash(path, stamp) == expected:
            return path
    except (OSError, ValueError):
        return None


def _matches_order(manifest, work, sid):
    # Older manifests omit the execution key. Their frozen plan and script
    # bindings are checked by the caller.
    return manifest.get("execution_key") is None or manifest["execution_key"] == digest_json({
        "order": str(work), "shot_id": sid, "repair_id": manifest.get("repair_id")})


def _verified_clip(clip, sid, source, order, plan, video_root, work):
    if (not isinstance(clip, dict) or clip.get("shot_id") != sid or clip.get("script_ref") != order["script"]
            or not clip.get("manifest_path") or not clip.get("media_path")):
        return None
    job = _candidate(Path(clip["manifest_path"]), video_root)
    if (not job or job["status"] != "SUCCEEDED" or job["shot_id"] != sid or job["script"] != clip.get("origin_script_ref")
            or job["hash"] != clip.get("generation_job_sha256") or not job["task_id"] or job["task_id"] != clip.get("task_id")
            or job["dependency"] != clip.get("dependency_sha256")):
        return None
    if clip.get("media_lineage"):
        try:
            path = Path(clip["media_path"]).resolve()
            # A prior commission's edit can only be played through an exact
            # reuse entry in this commission's frozen source.
            original = source.get(sid, {}) if sid in plan["reuse_shots"] else {}
            if clip.get("operation") == "reused":
                if any(original.get(k) != clip.get(k) for k in ("media_path", "media_sha256", "media_lineage", "task_id")):
                    return None
            elif clip.get("operation") != "edited" or path.parent.parent != work / "edited-candidates":
                return None
            elif job["hash"] != original.get("generation_job_sha256") and (
                    job["plan_id"] != plan["plan_id"] or job["script"] != order["script"] or not _matches_order(job, work, sid)):
                return None
            if path.name != "video.mp4" or not path.is_relative_to(work.parent) or path.parent.parent.name != "edited-candidates":
                return None
            from pianchang.video_collab_agent import _validated_media
            _validated_media(clip, require_origin=True)
            return path
        except (OSError, ValueError, KeyError):
            return None
    if job["media_sha256"] != clip.get("media_sha256") or job["media"] != clip.get("media_path"):
        return None
    if clip.get("operation") != "reused" and (job["script"] != order["script"] or not _matches_order(job, work, sid)):
        return None
    repair = (plan.get("acceptance_loop") and sid in plan["acceptance_loop"]["shot_ids"] and clip.get("repair_id")
              and clip["repair_id"] == job.get("repair_id") and job["plan_id"] == plan["plan_id"])
    if repair:
        pass
    elif sid in plan["reuse_shots"]:
        original = source.get(sid, {})
        if clip.get("operation") != "reused" or any(original.get(k) != clip.get(k) for k in
                ("task_id", "media_sha256", "generation_job_sha256", "dependency_sha256")):
            return None
    elif clip.get("operation") != "generated" or job["plan_id"] != plan["plan_id"]:
        return None
    return _verified_file(job["media"], job["media_sha256"], job["file"].parent)


def _order(root, run_id, message_id, pid, job):
    dbpath = root / "outputs" / "collaborations" / "assets.sqlite3"
    if not dbpath.is_file():
        return {}, {}, None, None
    with closing(sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("BEGIN")
        row = db.execute("""SELECT m.payload,d.status FROM messages m JOIN deliveries d USING(message_id)
            WHERE m.message_id=? AND m.story_id=? AND m.correlation_id=? AND m.recipient='video'
            AND m.message_type='production.requested' AND d.agent='video'""", (message_id, "story-"+run_id, run_id)).fetchone()
        if not row:
            return {}, {}, None, None
        order = json.loads(row[0])
        plan = order.get("shooting_plan") or {}
        if plan.get("project_id") != pid or job.get("plan_id") not in (plan.get("plan_id"), plan.get("base_plan_id")):
            return {}, {}, None, None
        trace = db.execute("SELECT trace_context FROM receipts WHERE message_id=? AND agent='video' AND status='accepted'",
                           (message_id,)).fetchone()
        previous = order.get("previous")
        reusable = {}
        if plan.get("reuse_shots") and previous and not plan.get("partial") and not plan.get("continuation"):
            row_asset = db.execute("""SELECT content FROM assets WHERE asset_id=? AND revision_id=? AND sha256=?
                                     AND story_id=? AND kind='production'""",
                                   (previous["asset_id"], previous["revision_id"], previous["sha256"], "story-"+run_id)).fetchone()
            if row_asset:
                reusable = json.loads(row_asset[0]).get("shots", {})
        return order, reusable, row[1], json.loads(trace[0]).get("trace_id") if trace else None


def _partial_source(folder, pointer):
    if not _ID.fullmatch(str(pointer.get("request_id"))):
        return {}
    checkpoint = _read(folder / ("video-spec-" + pointer["request_id"]) / "partial.json")
    return checkpoint.get("shots", {}) if checkpoint and digest_json(checkpoint) == pointer.get("sha256") else {}


def _observed_clip(root, data, sid, job):
    task = job.get("task_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(task)) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
        return None, {}
    base = (root / "outputs/web/progress-observations").resolve()
    folder = (base / data["job_id"] / sid / task).resolve()
    if not folder.is_relative_to(base):
        return None, {}
    info = _read(folder / "receipt.json")
    expected = {"project_id":data["project_id"], "job_id":data["job_id"], "message_id":data["message_id"],
                "plan_id":data["plan_id"], "shot_id":sid, "task_id":task, "manifest_sha256":job["hash"]}
    if any(info.get(k) != v for k,v in expected.items()):
        return None, {}
    output = (info.get("provider_response") or {}).get("output", {})
    if (output.get("task_id") != task or output.get("task_status") != info.get("provider_status")
            or not _ID.fullmatch(str(info.get("trace_id")))):
        return None, {}
    if info.get("provider_status") == "SUCCEEDED" and info.get("media_file") == "video.mp4":
        return _verified_file(folder / "video.mp4", info.get("media_sha256"), folder), info
    return None, info


@lru_cache(maxsize=32)
def _recent_paths(video_root, created_at, directory_version):
    try:
        cutoff = datetime.fromisoformat(created_at).timestamp() - 2
    except (TypeError, ValueError):
        cutoff = 0
    paths = []
    for entry in video_root.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime >= cutoff:
                paths.append(entry / "job.json")
        except FileNotFoundError:
            continue
    return paths


def _open_candidates(video_root, needed, plan, script, created_at, *, work=None):
    matches = {}
    if needed and video_root.is_dir():
        for path in _recent_paths(video_root, created_at, _stamp(video_root)):
            job = _candidate(path, video_root)
            if (not job.get("recovery") and job.get("shot_id") in needed and job.get("plan_id") == plan["plan_id"]
                    and job.get("script") == script and (work is None or _matches_order(job, work, job["shot_id"]))):
                matches.setdefault(job["shot_id"], []).append(job)
    return matches


def confirmed_video_reservations(output, work, plan, script, checkpoint, *, created_at=None, paid_clips=None):
    """Match each billed reservation to one successful receipt from this frozen order."""
    base = checkpoint["reserved_shots"]
    repairs = checkpoint["repair_reservations"]
    scheduled = set(plan["new_shots"] + plan["retake_shots"])
    policy = plan.get("acceptance_loop") or {}
    if (not isinstance(base, list) or not isinstance(repairs, dict)
            or len(base) != len(set(base)) or not set(base) <= scheduled
            or any(sid not in policy.get("shot_ids", []) or type(n) is not int
                   or not 0 < n <= policy["max_repairs_per_shot"] for sid, n in repairs.items())):
        return False
    expected = Counter((sid, False) for sid in base)
    expected.update({(sid, True): n for sid, n in repairs.items()})
    if sum(expected.values()) != checkpoint.get("video_generations"):
        return False
    output = Path(output).resolve()
    candidates = _open_candidates(output, scheduled | set(policy.get("shot_ids", [])), plan, script, created_at, work=work)
    actual = Counter()
    keys, tasks = set(), set()
    for sid, manifests in candidates.items():
        for item in manifests:
            key, task = item.get("execution_key"), item.get("task_id")
            response = (_read(item["file"]).get("provider_response") or {}).get("output") or {}
            if (not key or key in keys or not task or task in tasks or response.get("task_id") != task
                    or response.get("task_status") != "SUCCEEDED" or item.get("status") != "SUCCEEDED"):
                return False
            if paid_clips is not None:
                clip = paid_clips.get(key) or {}
                if (clip.get("task_id") != task or clip.get("shot_id") != sid or clip.get("script_ref") != script
                        or clip.get("manifest_path") != str(item["file"]) or clip.get("generation_job_sha256") != item["hash"]
                        or clip.get("media_path") != item["media"] or clip.get("media_sha256") != item["media_sha256"]
                        or not _verified_file(item["media"], item["media_sha256"], item["file"].parent)):
                    return False
            keys.add(key)
            tasks.add(task)
            actual[(sid, bool(item.get("repair_id")))] += 1
    return actual == expected and (paid_clips is None or keys == set(paid_clips))


@traced("video.repair.observe_local_pre_submit_failure", kind="validation", capture_inputs=False, capture_output=False)
def local_failure_before_generation(history, directory, *, saved_run=None):
    """Distinguish an observed local archive failure from an uncertain paid call."""
    if history.get("strategy") != "prompt_and_locked_references":
        return {"not_dispatched":False}
    path = Path(directory) / "agent/run.json"
    if saved_run is None:
        if not path.is_file():
            return {"not_dispatched":False}
        saved_run = json.loads(path.read_text(encoding="utf-8-sig"))
    run = saved_run
    if run.get("status") != "needs_input" or (run.get("result") or {}).get("reason") != "video_result_uncertain":
        return {"not_dispatched":False}
    results = []
    for msg in run.get("messages", []):
        if msg["role"] != "user":
            continue
        data = json.loads(msg["content"])
        if data.get("type") == "tool_result" and data.get("tool") == "regenerate_shot":
            results.append(data)
    error = (results[-1].get("error") or {}) if results else {}
    if error.get("type") != "FileNotFoundError":
        return {"not_dispatched":False}
    from trace_system import get_tracer
    tracer = get_tracer()
    context = run["trace_context"]
    lifecycle = tracer.get_events(context["trace_id"], span_id=context["span_id"])
    if not any(e["kind"] == "span.end" for e in lifecycle):
        return {"not_dispatched":False}
    generation = tracer.get_events(context["trace_id"], names=["video.collab.generate"], limit=1)
    proof = {"not_dispatched":not generation, "repair_id":history["repair_id"],
             "source_trace_id":context["trace_id"], "source_run_path":str(path), "source_run_sha256":digest_json(run),
             "generation_boundary_count":len(generation),"error":error}
    event("video.repair.pre_submit_failure_observed", {**proof, "path":"provider_generation_started",
          "expected":False,"actual":bool(generation),"status":"PASS" if not generation else "UNKNOWN"}, kind="validation")
    return proof


def _clip_acceptance(summary, row, project_sha256, job_sha256, outputs, selected):
    if summary is None:
        return "PENDING"
    sid = row["shot_id"]
    valid = False
    if isinstance(summary, dict) and isinstance(summary.get("report_path"), str):
        try:
            path = Path(summary["report_path"]).resolve()
            if path.name == "report.json" and path.is_relative_to(outputs):
                report = _read(path)
                valid = bool(report and project_sha256 and summary.get("shot_id") == sid == report.get("shot_id")
                    and report.get("project_sha256") == project_sha256
                    and report.get("media_sha256") == row["media_sha256"]
                    and digest_json(report) == summary.get("report_sha256")
                    and summary.get("status") == report.get("status") in ("PASS", "FAIL", "UNKNOWN")
                    and all(report.get(k) in (None, job_sha256) for k in ("generation_job_sha256", "reference_job_sha256")))
                if valid and report.get("content_review_version"):
                    evidence = _read(path.parent / "content.json")
                    media = evidence.get("selected_media")
                    valid = (digest_json(evidence) == report.get("content_evidence_sha256")
                        and isinstance(media, dict) and sid in media and all(
                            isinstance(asset, dict) and shot in selected and asset.get("sha256") == selected[shot]
                            for shot, asset in media.items()))
        except (OSError, ValueError):
            pass
    event("studio.video.shot_review_binding", {"path":sid + ".review_matches_candidate",
          "expected":True, "actual":valid, "status":"PASS" if valid else "FAIL",
          "media_sha256":row["media_sha256"]}, kind="validation")
    return summary["status"] if valid else "UNKNOWN"


def _version_report(summary, clip, project_sha, work):
    if not summary or not summary.get("report_path"):
        return {}
    path = Path(summary["report_path"]).resolve()
    if not path.is_relative_to(work.parent) or path.suffix != ".json" or not path.is_file():
        return {}
    report = _read(path)
    if (not report or digest_json(report) != summary.get("report_sha256") or report.get("shot_id") != clip["shot_id"]
            or report.get("media_sha256") != clip["media_sha256"] or report.get("project_sha256") != project_sha):
        return {}
    issues = [{k:r.get(k) for k in ("criterion_id","required","status","reason")}
              for r in report.get("results",[]) if r.get("required") and r.get("status") != "PASS"]
    return {"report_sha256":summary["report_sha256"], "report":{"status":report["status"],"results":issues}}


def _shot_versions(row, selected, selected_path, reports, history, source, order, video_root, work,
                   project_sha, preview_url, paths):
    sid = row["shot_id"]
    versions = {}
    def add(clip, path, summary, *, rejected=False, current=False):
        key = (clip["task_id"], clip["media_sha256"])
        kind = {"reused":"reuse", "edited":"edit"}.get(clip.get("operation"),
                     "retake" if clip.get("repair_id") else "initial")
        version = {"shot_id":sid, "task_id":key[0], "media_sha256":key[1], "kind":kind, "selected":current}
        if preview_url:
            version["preview_url"] = preview_url(sid, key[1])
            version.update(_version_report(summary, clip, project_sha, work))
        if rejected and not current:
            version["selection_reason"] = "rejected_regression"
        versions[key] = version
        paths[(sid,key[1])] = path

    for repair in history:
        if repair.get("shot_id") != sid:
            continue
        for name in ("source","result"):
            clip = repair.get(name+"_clip")
            if not isinstance(clip,dict) or not clip.get("manifest_path"):
                continue
            if name == "result" and (not repair.get("repair_id") or clip.get("repair_id") != repair["repair_id"]):
                continue
            path = _verified_clip(clip,sid,source,order,order["shooting_plan"],video_root,work)
            if not path:
                continue
            manifest = _candidate(Path(clip["manifest_path"]),video_root)
            if (manifest.get("provider_status") != "SUCCEEDED" or manifest.get("provider_task_id") != clip["task_id"]
                    or clip.get("operation") == "generated" and manifest.get("repair_id") != clip.get("repair_id")):
                continue
            add(clip,path,repair.get(name+"_report"),
                rejected=name=="result" and repair.get("status")=="reviewed_rejected_regression")
    if selected_path and row.get("task_id"):
        # Older interrupted downloads may have only an independently observed file.
        clip = selected or {k:row[k] for k in ("shot_id","task_id","media_sha256")}
        add(clip,selected_path,reports.get(sid),current=True)
    return list(versions.values())


def _snapshot(root, project_id, job, video_root, preview_url, *, include_reviews=True, version_paths=None):
    root = Path(root).resolve()
    video_root = Path(video_root or root / "outputs/videos").resolve()
    if not isinstance(job, dict) or job.get("project_id") != project_id or job.get("kind") != "studio_video" or not _ID.fullmatch(str(job.get("id"))):
        raise KeyError("找不到这个故事的视频制作任务。")
    run_id, message_id = job.get("run_id"), job.get("message_id")
    status = job["status"]
    data = {"project_id": project_id, "job_id": job["id"], "job_status": status, "trace_id": job.get("trace_id"),
            "video_trace_id": None, "phase": "preparing" if status in _ACTIVE else "stopped", "reason": job.get("error") or job.get("message"),
            "plan_id": job.get("plan_id"), "run_id": run_id, "message_id": message_id, "updated_at": job.get("updated_at"),
            "total": 0, "completed": 0, "current_shot_id": None, "shots": []}
    files = {}
    if not _ID.fullmatch(str(run_id)) or not _ID.fullmatch(str(message_id)):
        return data, files
    order, source, delivery_status, trace_id = _order(root, run_id, message_id, project_id, job)
    if not order:
        return data, files
    plan = order["shooting_plan"]
    data.update(plan_id=plan["plan_id"], video_trace_id=trace_id)
    folder = root / "outputs/collaborations/runs" / run_id
    work = folder / ("video-spec-" + message_id)
    overview = _read(folder / "overview.json")
    if overview.get("project_id") == project_id:
        data["project_title"] = overview.get("title")
    if plan.get("partial"):
        source = _partial_source(folder, plan["partial"])
    if plan.get("continuation"):
        from pianchang.studio_recovery import _continuation_checkpoint
        source = _continuation_checkpoint(root, run_id, plan["continuation"], order)["shots"]
    project = _read(work / "project.json")
    ops = {sid: op for key, op in (("new_shots", "new"), ("retake_shots","retake"), ("reuse_shots","reuse")) for sid in plan[key]}
    order_ids = [shot["id"] for shot in project.get("shots", []) if shot.get("id") in ops]
    ids = list(dict.fromkeys([*order_ids, *ops]))
    clips = _read(work / "clips.json")
    run = _read(work / "agent/run.json")
    reserved = set(run.get("reserved_shots") or [])
    matched = _open_candidates(video_root, reserved-set(clips), plan, order["script"], job.get("created_at"), work=work)
    reports = _read(work / "reports.json") if include_reviews or version_paths is not None and preview_url else {}
    project_sha256 = digest_json(project) if project and (reports or version_paths is not None and preview_url) else None
    job_hashes = {}
    active = status in _ACTIVE and delivery_status not in {"unknown","blocked","failed","completed"} and run.get("status") not in {
        "needs_input","budget_exhausted","cancelled","error","completed"}
    history = _read(work / "repair-history.json").get("history", [])
    phase = ("needs_approval" if (job.get("result") or {}).get("action_required") else "completed" if status == "succeeded"
             else "repairing" if active and history
             else "reviewing" if active and len(clips) == len(ids) else "generating" if active and reserved else "preparing" if active else "stopped")
    data.update(phase=phase, total=len(ids))
    data["repair_history"] = [{k: h.get(k) for k in ("shot_id", "repair_id", "strategy", "status")} for h in history]
    data["production_complete"] = status == "succeeded" and run.get("result", {}).get("task_complete") is True
    for sid in ids:
        row = {"shot_id":sid, "operation":ops[sid], "status":"pending" if active else "not_started", "acceptance_status":"PENDING"}
        data["shots"].append(row)
        clip = clips.get(sid) or ({**source[sid], "operation": "reused", "script_ref": order["script"]} if sid in source else None)
        if clip:
            path = _verified_clip(clip, sid, source, order, plan, video_root, work)
            if not path:
                row.update(status="unknown", reason="已记录的镜头文件或版本未能核对。")
            else:
                row.update(status="reused" if ops[sid] == "reuse" else "ready", media_sha256=clip["media_sha256"], task_id=clip["task_id"])
                files[sid] = path
        elif sid in reserved:
            candidates = matched.get(sid, [])
            row["status"] = "generating" if active else "unknown"
            if len(candidates) == 1:
                manifest = candidates[0]
                row.update(task_id=manifest["task_id"], provider_status=manifest["provider_status"], updated_at=manifest["updated_at"])
                path, observation = _observed_clip(root, data, sid, manifest)
                if observation:
                    row.update(independently_observed=True, observation_trace_id=observation.get("trace_id"),
                               provider_status=observation.get("provider_status"), updated_at=observation.get("observed_at") or row.get("updated_at"))
                if path:
                    files[sid] = path
                    row.update(status="ready", media_sha256=observation["media_sha256"])
                elif row["provider_status"] in _TERMINAL_FAILURE:
                    row.update(status="failed", reason="云端任务已确认失败或取消。")
                elif manifest["status"] == "SUCCEEDED":
                    path = _verified_file(manifest["media"], manifest["media_sha256"], manifest["file"].parent)
                    if path:
                        files[sid] = path
                        row.update(status="ready", media_sha256=manifest["media_sha256"])
                elif not active:
                    row["reason"] = "本地等待已结束，云端最终结果尚未确认。"
            elif len(candidates) > 1:
                row.update(status="unknown", reason="同一镜头有多个任务记录，需要核实其归属。")
            if row["status"] in {"generating","unknown"}:
                data["current_shot_id"] = sid
            if row.get("updated_at") and str(row["updated_at"]) > str(data["updated_at"] or ""):
                data["updated_at"] = row["updated_at"]
        if sid in files:
            data["completed"] += 1
            job_hashes[sid] = clip["generation_job_sha256"] if clip else manifest["hash"]
            if preview_url:
                row["preview_url"] = preview_url(sid, row["media_sha256"])
        pending = [h for h in history if h.get("shot_id") == sid and h.get("status") == "started_or_unknown"]
        if pending and not active:
            unresolved = []
            for repair in pending:
                proof = local_failure_before_generation(repair, work, saved_run=run)
                if proof["not_dispatched"]:
                    row["local_repair_error"] = proof["error"]["message"]
                    row["reason"] = "修复在本地归档阶段失败；生成函数未开始，旧视频保留，可在修正后继续。"
                else:
                    unresolved.append(repair)
            pending = unresolved
        if pending:
            row["repair_pending"] = True
            row["reason"] = "修复执行结果尚未全部确认，原候选保留，不会自动重复提交。"
        if version_paths is not None:
            row["versions"] = _shot_versions(row,clip,files.get(sid),reports,history,source,order,video_root,work,
                                            project_sha256,preview_url,version_paths)
    if include_reviews and reports:
        from pianchang.shot_approval import read_approvals
        approvals = read_approvals(work, project, clips, reports, project_id=project_id)
        selected = {r["shot_id"]: r["media_sha256"] for r in data["shots"] if r["shot_id"] in files}
        for row in data["shots"]:
            sid = row["shot_id"]
            if sid in files:
                row["acceptance_status"] = _clip_acceptance(reports.get(sid), row, project_sha256, job_hashes[sid], root / "outputs", selected)
                if sid in approvals and approvals[sid]["media_sha256"] == row["media_sha256"]:
                    row["human_approved"] = True
    later_clips = [r["shot_id"] for r in data["shots"] if r.get("independently_observed") and r["shot_id"] in files]
    if phase == "stopped" and later_clips:
        data["original_reason"] = data["reason"]
        data["reason"] = "原委托已停止。随后查询同一云端任务，" + "、".join(later_clips) + " 已下载，仍需自动验收与修复。"
    return data, files


@traced("studio.video.live_progress", kind="tool", capture_inputs=False)
def read_production_progress(root, project_id, job, *, video_root=None, preview_url=None):
    return _snapshot(root, project_id, job, video_root, preview_url)[0]


@traced("studio.video.live_clip", kind="validation", capture_inputs=False, capture_output=False)
def resolve_production_clip(root, project_id, job, shot_id, media_sha256, *, video_root=None,state_dir=None):
    versions = {}
    data, _ = _snapshot(root, project_id, job, video_root, None, include_reviews=False, version_paths=versions)
    path = versions.get((shot_id,media_sha256))
    actual = media_sha256 if path else next((r.get("media_sha256") for r in data["shots"] if r["shot_id"] == shot_id), None)
    valid = path is not None
    event("studio.video.live_clip_checked", {"project_id":project_id,"job_id":job["id"],"path":shot_id,
          "expected":media_sha256,"actual":actual,"status":"PASS" if valid else "FAIL"}, kind="validation")
    if not valid:
        raise KeyError("此镜头版本尚无可预览的文件。")
    return path
