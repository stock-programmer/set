"""Local workbench facade over the real story/video/acceptance tools.

The browser receives whitelisted data, never configuration secrets or arbitrary
filesystem access. Background operations are serialized and journaled; a restart
does not replay an uncertain paid request.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import traceback
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import replace
from functools import wraps
from pathlib import Path

from pianchang.acceptance import create_contract, file_sha256, observe, run_checks
from pianchang.bailian_observer import load_observer_config
from pianchang.bailian_text import BailianTextClient, load_text_config
from pianchang.bailian_video import build_request, config_for_references, load_config
from pianchang.media_tools import MediaError, find_ffmpeg, find_ffprobe, inspect_media
from pianchang.model_routes import load_routes
from pianchang.story_agent import StoryRunner, script_markdown
from pianchang.story_spec import digest_json, script_structure, validate_compilation, validate_structure_constraints
from pianchang.video_agent import VideoRunner, compile_project_previews, compile_shot
from trace_system import TraceError, current_context, event, get_tracer, register_secret, traced, use_context

ROOT = Path(__file__).resolve().parents[1]
STORY_COUNT_FIELDS = {"episodes": "episodes", "scenes": "scenes_per_episode", "beats": "beats_per_scene"}
# Official 2.7 output dimensions differ from 2.6 for 4:3 and 3:4.
# https://help.aliyun.com/zh/model-studio/text-to-video-api-reference
SIZES_27 = {
    "720P": {"16:9": "1280*720", "9:16": "720*1280", "1:1": "960*960", "4:3": "1104*832", "3:4": "832*1104"},
    "1080P": {"16:9": "1920*1080", "9:16": "1080*1920", "1:1": "1440*1440", "4:3": "1648*1248", "3:4": "1248*1648"},
}


def story_constraints(body):
    return {canonical: body[field] for field, canonical in STORY_COUNT_FIELDS.items() if field in body}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    from pianchang.atomic_files import replace_with_retry
    replace_with_retry(temporary, path)


def now():
    return datetime.now(timezone.utc).isoformat()


def project_id_for_path(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:16]


def service_tool(name):
    """Bind public service calls to their store even outside an HTTP request."""
    def decorate(function):
        instrumented = traced(name, kind="tool")(function)
        @wraps(instrumented)
        def wrapper(self, *args, **kwargs):
            parent = current_context()
            if parent and parent.get("store_id") == self.tracer.store_id:
                return instrumented(self, *args, **kwargs)
            with use_context(None, tracer=self.tracer):
                return instrumented(self, *args, **kwargs)
        return wrapper
    return decorate


class AppService:
    def __init__(self, root=ROOT, state_dir=None):
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir or self.root / "outputs" / "web").resolve()
        self.secrets = set()
        self.tracer = get_tracer(os.environ.get("AGENT_TRACE_DIR") or self.root / "outputs" / "traces")
        self._load_configs()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._process_lock = (self.state_dir / "server.lock").open("a+b")
        self._process_lock.seek(0, 2)
        if not self._process_lock.tell():
            self._process_lock.write(b"0")
            self._process_lock.flush()
        self._process_lock.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self._process_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._process_lock.close()
            raise ValueError("此工程的工作台已在运行，请打开已有地址。") from None
        self.lock = threading.RLock()
        self._asset_project_locks = {}
        self._asset_failure_history = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drama-workbench")
        self.script_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drama-script")
        self.asset_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drama-asset")
        self.library_asset_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drama-library-asset")
        self._asset_futures = {}
        self.assets = {}
        self.asset_scopes = {}
        self.projects = {}
        state = self.state_dir / "state.json"
        self.state = read_json(state) if state.exists() else {"jobs": [], "selections": {}, "reviews": {}, "exports": {}}
        for job in self.state["jobs"]:
            if job["status"] in {"queued", "running"}:
                context = job.get("_trace_context")
                if context:
                    with use_context(context, tracer=self.tracer):
                        event("task.interrupted_detected", {"job_id": job["id"], "previous_status": job["status"],
                              "reason": "Exclusive workbench process lock acquired after previous process ended; no work replayed."}, kind="state")
                    self.tracer.end_span(context, status="UNKNOWN", error={"type": "ProcessInterrupted",
                                         "message": "服务重启时检测到未结束任务；实际中断时间未知。"})
                job.update(status="interrupted", message="服务已重启；保留任务记录，未自动重新提交。已有视频任务请点继续查询。", updated_at=now())
        self._save()

    def _load_configs(self):
        env = self.root / ".env"
        routes = load_routes(env)
        video = load_config(env, allow_missing=True)
        text = load_text_config(env, allow_missing=True, routes=routes)
        vision = load_observer_config(env, allow_missing=True, routes=routes)
        keys = routes.secrets() | {c.api_key for c in (video, text, vision) if c.api_key}
        for secret in keys:
            register_secret(secret)
            self.tracer.register_secret(secret)
        self.secrets = self.secrets | keys
        self.model_routes, self.video_config, self.text_config, self.vision_config = routes, video, text, vision

    def sanitize(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
        if isinstance(value, list):
            return [self.sanitize(x) for x in value]
        if isinstance(value, dict):
            return {k: self.sanitize(v) for k, v in value.items()}
        return value

    def _save(self):
        write_json(self.state_dir / "state.json", self.sanitize(self.state))

    def _asset_write_lock(self, pid):
        with self.lock:
            return self._asset_project_locks.setdefault(pid, threading.RLock())

    def close(self, wait=True):
        if hasattr(self,"_pi_sessions"):self._pi_sessions.close(wait=wait)
        self.script_executor.shutdown(wait=wait, cancel_futures=not wait)
        self.executor.shutdown(wait=wait, cancel_futures=not wait)
        self.asset_executor.shutdown(wait=wait, cancel_futures=not wait)
        self.library_asset_executor.shutdown(wait=wait, cancel_futures=not wait)
        # During process teardown, running workers keep the file lock until exit.
        if wait:
            self._process_lock.close()

    def _inside(self, path, roots):
        path = Path(path).resolve()
        if not any(path.is_relative_to(Path(root).resolve()) for root in roots):
            raise ValueError("素材不在已登记的工程输出目录内。")
        return path

    def _hash(self, path):
        return file_sha256(Path(path).resolve())

    def asset(self, path, expected_sha256=None, *, project_id=None, candidate_binding=None):
        path = self._inside(path, [self.root / "outputs", self.state_dir, self.video_config.output_dir])
        if not path.is_file() or path.suffix.lower() not in {".mp4", ".wav", ".jpg", ".png", ".json", ".md"} or any(part.startswith(".") for part in path.relative_to(path.anchor).parts):
            raise ValueError("不支持公开此文件。")
        sha = self._hash(path)
        if expected_sha256 and sha != expected_sha256:
            raise ValueError("文件与已登记版本的指纹不符。")
        scope = "/"+project_id if project_id and path.suffix.lower()==".mp4" else ""
        if candidate_binding:
            scope += "/candidate/" + digest_json(candidate_binding)
        token = hashlib.sha256((str(path) + sha + scope).encode()).hexdigest()[:32]
        with self.lock:
            self.assets[token] = (path, sha)
            if path.suffix.lower()==".mp4":self.asset_scopes[token]=(project_id, candidate_binding)
        return "/assets/" + token

    def resolve_asset(self, token):
        with self.lock:
            registered = self.assets.get(token)
        if not registered:
            raise KeyError("素材不存在。")
        path, expected = registered
        self._inside(path, [self.root / "outputs", self.state_dir, self.video_config.output_dir])
        if not path.is_file() or self._hash(path) != expected:
            raise KeyError("素材已变更，请刷新工程。")
        if path.suffix.lower()==".mp4":
            from pianchang.preview_access import require_preview,PreviewBlocked
            pid, binding = self.asset_scopes.get(token, (None, None))
            if not pid:raise PreviewBlocked({"ready":False,"reason":"此视频尚未绑定当前已验收工程，不能人工预览。"})
            if binding:
                self._check_candidate_preview(pid, binding, path, expected)
            else:
                require_preview(self,pid,path=path,sha256=expected)
        return path

    @traced("preview.candidate.verify", kind="validation", capture_inputs=False, capture_output=False)
    def _check_candidate_preview(self, pid, binding, path, media_sha256):
        _, project = self._project(pid)
        video_root = Path(self.video_config.output_dir).resolve()
        if path.name != "video.mp4" or path.parent.parent != video_root:
            raise KeyError("候选素材不在镜头输出目录内。")
        manifest = read_json(path.with_name("job.json"))
        metadata = manifest.get("metadata") or {}
        expected = {"project": digest_json(project), "shot": binding["shot"], "manifest": binding["manifest"],
                    "shot_in_project": True, "status": "SUCCEEDED", "media": media_sha256}
        actual = {"project": metadata.get("project_sha256"), "shot": metadata.get("shot_id"),
                  "manifest": digest_json(manifest), "shot_in_project": any(s["id"] == binding["shot"] for s in project["shots"]),
                  "status": manifest.get("status"), "media": manifest.get("download_sha256")}
        event("preview.candidate.binding", {"path": str(path), "expected": expected, "actual": actual,
              "status": "PASS" if actual == expected else "FAIL"}, kind="validation")
        if actual != expected:
            raise KeyError("镜头候选或工程版本已变更，请刷新。")

    def _discover(self):
        paths = [(self.root / "fixtures" / "story_project.json", "内置三镜头示例")]
        final = self.root / "outputs" / "stories" / "last-key-step2-final" / "project.json"
        paths.append((final, "已有创作工程"))
        paths.extend((p, "Web 创作工程") for p in sorted((self.state_dir / "stories").glob("*/project.json")))
        records = {}
        for path, source in paths:
            if path.is_file():
                pid = project_id_for_path(path)
                records[pid] = {"path": path.resolve(), "source": source}
        with self.lock:
            self.projects = records
        return records

    @service_tool("project.read")
    def _project(self, pid):
        records = self._discover()
        if pid not in records:
            raise KeyError("工程不存在。")
        path = records[pid]["path"]
        return path, read_json(path)

    def _candidates(self, project, project_id=None):
        result = []
        fingerprint = digest_json(project)
        for path in sorted(Path(self.video_config.output_dir).glob("*/job.json")):
            try:
                job = read_json(path)
                if job.get("metadata", {}).get("project_sha256") != fingerprint:
                    continue
                video = path.parent / "video.mp4"
                valid_video = video.is_file() and job.get("download_sha256") == self._hash(video)
                result.append({"id": path.parent.name, "task_id": job.get("task_id"),
                               "shot_id": job["metadata"].get("shot_id"), "status": job.get("status"),
                               "created_at": job.get("created_at"),
                               "duration_s": job.get("request", {}).get("parameters", {}).get("duration"),
                               "video_url": self.asset(video, job["download_sha256"], project_id=project_id,
                                   candidate_binding={"shot":job["metadata"]["shot_id"], "manifest":digest_json(job)})
                                   if project_id and valid_video and job.get("status") == "SUCCEEDED" else None,
                               "media_available":valid_video,
                               "error": self.sanitize(job.get("last_error")),
                               "_path": path, "_job": job, "_video": video if valid_video else None})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(result, key=lambda c: c.get("created_at") or "")

    @staticmethod
    def _public_candidate(candidate):
        return {k: v for k, v in candidate.items() if not k.startswith("_")}

    def _chosen(self, pid, shot_id, candidates, explicit=None):
        available = [c for c in candidates if c["shot_id"] == shot_id]
        chosen = explicit or self.state["selections"].get(pid, {}).get(shot_id)
        if chosen:
            found = next((c for c in available if c["id"] == chosen), None)
            if found:
                return found
            if explicit:
                raise ValueError("候选不属于这个镜头或工程版本。")
        return next((c for c in reversed(available) if c["_video"]), available[-1] if available else None)

    def _report(self, project, candidate):
        if not candidate or not candidate["_video"]:
            return None
        review_key = digest_json([digest_json(project), candidate["id"]])
        with self.lock:
            entry = self.state["reviews"].get(review_key)
        possibilities = [entry] if entry else []
        # Existing evidence is imported by content and job binding, not shot ID alone.
        possibilities.append({"report": str(self.root / "outputs" / "acceptance" / "S002" / "report.json"),
                              "inspection": str(self.root / "outputs" / "acceptance" / "S002" / "inspection" / "inspection.json")})
        for record in possibilities:
            try:
                report_path = self._inside(record["report"], [self.root / "outputs", self.state_dir])
                report = read_json(report_path)
                if (report.get("project_sha256") != digest_json(project)
                        or report.get("media_sha256") != self._hash(candidate["_video"])
                        or report.get("generation_job_sha256") != digest_json(candidate["_job"])):
                    continue
                inspection = read_json(record["inspection"])
                if digest_json(inspection) != report["inspection_sha256"]:
                    continue
                frames = []
                for frame in inspection["frames"]:
                    if self._hash(frame["path"]) != frame["sha256"]:
                        raise ValueError("帧证据已变更")
                    frames.append({"id": frame["id"], "url": self.asset(frame["path"]),
                                   "timestamp_s": frame["time_s"]})
                checks = [{**r, "id": r["criterion_id"], "expected": r.get("expectation", "")} for r in report["results"]]
                return {**report, "checks": checks,
                        "summary": {s.lower(): sum(r["status"] == s for r in checks) for s in ("PASS", "FAIL", "UNKNOWN")},
                        "report_url": self.asset(report_path), "frames": frames}
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    def bootstrap(self, *, include_jobs=True):
        self._load_configs()
        # Resolve the progress URL from the same sidebar read. Running jobs
        # take precedence over the last recorded commission for that project.
        video_jobs = {}
        with self.lock:
            for job in reversed(self.state["jobs"]):
                pid = job.get("project_id")
                if job["kind"] != "studio_video" or not pid:
                    continue
                active = job["status"] in {"queued", "running"}
                if not active and not job.get("message_id"):
                    continue
                previous = video_jobs.get(pid)
                if previous and (not active or previous["status"] in {"queued", "running"}):
                    continue
                video_jobs[pid] = {key: job[key] for key in
                                   ("project_id", "id", "kind", "status", "run_id", "plan_id", "message_id", "message", "updated_at")
                                   if key in job}
        from pianchang.studio_live_progress import last_shot_records
        for pid, job in video_jobs.items():
            if job.get("run_id") and job.get("plan_id") and job.get("message_id"):
                recorded = last_shot_records(self.root, pid, job)
                if recorded is not None:
                    job["recorded_progress"] = recorded
        projects = []
        for pid, record in self._discover().items():
            project = read_json(record["path"])
            # The sidebar already reads these documents. Keep a small, read-only
            # excerpt ready for navigation, without compiling prompts, opening
            # sessions or inferring anything about available media/permissions.
            script = (project.get("script_source") or {}).get("script") or {}
            entities = []
            for eid, entity in project.get("entities", {}).items():
                canonical = entity.get("canonical") or {}
                field = {"character": "identity", "scene": "layout"}.get(entity.get("kind"), "appearance")
                entities.append({"id": eid, "name": canonical.get("name") or eid,
                                 "kind": entity.get("kind"), "description": canonical.get(field) or ""})
            opening = {
                "logline": script.get("logline", ""),
                "video_job": video_jobs.get(pid),
                "entities": entities,
                "shots": [{k: shot.get(k) for k in ("id", "description", "duration_s", "entity_ids", "scene_id")}
                          for shot in project.get("shots", [])],
            }
            projects.append({"id": pid, "title": project.get("title", "未命名"),
                             "shot_count": len(opening["shots"]), "source": record["source"], "opening": opening})
        try:
            find_ffmpeg()
            find_ffprobe()
            ffmpeg_ready = True
        except Exception:
            ffmpeg_ready = False
        routes = self.model_routes.describe()
        return self.sanitize({"config": {"key_configured": any(r["key_configured"] for r in routes.values()),
                                         "model_routes": routes,
                                         "text_model": routes["script.decision"]["model"],
                                         "video_model": routes["video.text_to_video"]["model"],
                                         "reference_video_model": routes["video.reference_to_video"]["model"],
                                         "vision_model": self.vision_config.model, "ffmpeg_ready": ffmpeg_ready,
                                         "local_only": True,"workspace_snapshot":True,"video_detail":True}, "projects": projects,
                              "story_drafts": self.story_drafts(),
                              **({"jobs":self.list_jobs()} if include_jobs else {"jobs_pending":True})})

    def _story_directory(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise KeyError("故事记录不存在。")
        return self._inside(self.state_dir / "stories" / job_id, [self.state_dir / "stories"])

    def _story_json(self, directory, name):
        try:
            path = self._inside(directory / name, [directory])
            value = read_json(path)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def _story_record(self, job):
        directory = self._story_directory(job["id"])
        saved = job.get("story_input")
        if not isinstance(saved, dict):
            saved = self._story_json(directory, "run.json").get("inputs", {})
        if not isinstance(saved, dict):
            saved = {}
        inputs = {"idea": saved.get("idea", "")}
        for field, canonical in STORY_COUNT_FIELDS.items():
            if field in saved or canonical in saved:
                inputs[field] = saved.get(field, saved.get(canonical))
        idea = inputs["idea"] if isinstance(inputs["idea"], str) else ""
        project_path = directory / "project.json"
        pid = (job.get("result") or {}).get("project_id")
        if not pid and project_path.is_file():
            pid = project_id_for_path(project_path)
        return {"id": job["id"], "kind": job["kind"], "title": idea[:60] or "未完成的故事 " + job["id"][:8],
                "idea": idea, "inputs": inputs, "status": job["status"],
                "created_at": job.get("created_at"), "updated_at": job.get("updated_at"),
                "error": job.get("error"), "message": job.get("message"),
                "trace_id": job.get("trace_id"), "project_id": pid, "repair_from": job.get("repair_from")}

    @service_tool("story.repair_source.read")
    def _story_repair_source(self, job_id):
        directory = self._story_directory(job_id)
        job = next((j for j in self.list_jobs() if j["id"] == job_id and j["kind"] == "story"), None)
        if job is None:
            raise KeyError("待修复的故事记录不存在。")
        if job["status"] not in {"failed", "interrupted"}:
            raise ValueError("只有失败或中断的故事可从已保存草稿启动修复。")
        for path in directory.glob("*-call.json"):
            call = self._story_json(directory, path.name)
            if call.get("status") != "COMPLETED":
                raise ValueError("原任务有未确认的模型调用；请先核对 Trace，不会自动重放。")
        draft = self._story_json(directory, "draft-candidate.json")
        if not draft:
            response = self._story_json(directory, "draft-call.json").get("response", {})
            draft = response.get("data") if isinstance(response, dict) else None
        if not isinstance(draft, dict) or not draft:
            raise ValueError("原任务没有已完成且可读取的模型草稿，不能执行局部修复。")
        inputs = self._story_record(job)["inputs"]
        if not isinstance(inputs.get("idea"), str) or not inputs["idea"].strip():
            raise ValueError("无法恢复原始创意；不能更改创作约束后自动修复。")
        validate_structure_constraints(story_constraints(inputs))
        return {"inputs": inputs, "draft": draft, "source": {"job_id": job_id,
                "trace_id": job.get("trace_id"), "draft_sha256": digest_json(draft)}}

    @service_tool("story.drafts.list")
    def story_drafts(self):
        records = []
        with self.lock:
            jobs=copy.deepcopy([j for j in reversed(self.state["jobs"]) if j.get("kind") in {"story","studio_script"}])
        for job in jobs:
            record = self._story_record(job)
            if not record["project_id"]:
                records.append(record)
        return self.sanitize(records)

    @service_tool("story.draft.read")
    def story_draft_detail(self, job_id):
        directory = self._story_directory(job_id)
        job = next((j for j in self.list_jobs() if j["id"] == job_id and j["kind"] in {"story", "studio_script"}), None)
        if job is None:
            raise KeyError("故事记录不存在。")
        record = self._story_record(job)
        call = self._story_json(directory, "draft-call.json")
        response = call.get("response")
        draft = (self._story_json(directory, "draft-candidate.json")
                 or (response.get("data") if isinstance(response, dict) else None))
        validation = (self._story_json(directory, "validation-report.json")
                      or self._story_json(directory, "draft-validation.json") or None)
        readable = self._inside(directory / "script.md", [directory])
        try:
            script_text = readable.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            script_text = ""
        artifacts = []
        script_ref = job.get("script_ref")
        names = ["run.json", "draft-call.json", "draft-seed.json", "draft-candidate.json",
                 "draft-validation.json", "script.json", "script.md", "validation-report.json"]
        sources = {}
        if job["kind"] == "studio_script":
            agent_dir = self.root / "outputs" / "collaborations" / "runs" / job_id / "script-r1"
            agent_script = self._story_json(agent_dir, "published-script.json")
            receipt = self._story_json(agent_dir, "publication.json")
            if agent_script and receipt.get("sha256") == digest_json(agent_script):
                script_ref = {k: receipt[k] for k in ("asset_id", "revision_id", "sha256")}
            draft = agent_script or self._story_json(agent_dir, "candidate.json") or None
            if agent_script:
                script_text = script_markdown(agent_script)
            validation = validation or self._story_json(agent_dir, "script-validation.json") or None
            for name in ("published-script.json", "published-script.md", "script-validation.json", "publication.json", "candidate.json"):
                names.append(name)
                sources[name] = agent_dir / name
            pi_dir=self.root/"outputs/pi/workspaces"/("story-"+job_id)/"script"
            pi_script=self._story_json(pi_dir,"published-script.json")
            pi_receipt=self._story_json(pi_dir,"publication.json")
            if pi_script and pi_receipt.get("sha256")==digest_json(pi_script):
                draft=pi_script;script_text=script_markdown(pi_script)
                script_ref={k:pi_receipt[k] for k in ("asset_id","revision_id","sha256")}
                validation=self._story_json(pi_dir,"script-validation.json") or validation
                for name in ("published-script.json","published-script.md","script-validation.json","publication.json","candidate.json"):
                    sources[name]=pi_dir/name
        names += [p.name for p in sorted(directory.glob("draft-*.json"))
                  if re.fullmatch(r"draft-(?:\d{2}-(?:candidate|report)|repair-\d{2}-call)\.json", p.name)]
        for name in names:
            path = sources.get(name) or self._inside(directory / name, [directory])
            if path.is_file():
                # Keep source evidence intact; downloads are separately labelled,
                # content-addressed redacted copies, not raw files bypassing SDK redaction.
                try:
                    source = read_json(path) if path.suffix == ".json" else path.read_text(encoding="utf-8-sig")
                    safe = self.tracer.sanitize(self.sanitize(source))
                    content = json.dumps(safe, ensure_ascii=False, indent=2) if path.suffix == ".json" else safe
                    payload = content.encode("utf-8")
                    digest = hashlib.sha256(payload).hexdigest()
                    public = self.state_dir / "story-draft-previews" / job_id / digest / name
                    if not public.is_file() or public.read_bytes() != payload:
                        public.parent.mkdir(parents=True, exist_ok=True)
                        temporary = public.with_name(name + "." + uuid.uuid4().hex + ".tmp")
                        temporary.write_bytes(payload)
                        temporary.replace(public)
                    artifacts.append({"name": name, "url": self.asset(public), "redacted": True})
                except (OSError, ValueError, UnicodeError):
                    continue
        return self.sanitize({**record, "job": job, "script_text": script_text, "script_structure": script_structure(draft),
                              "draft": draft, "validation": validation, "artifacts": artifacts,
                              "ready_for_generation": False,
                              "script_ref": script_ref,
                              "content_status": "published_script" if script_ref else "unvalidated_draft"})

    def project_detail(self, pid):
        path, project = self._project(pid)
        validation, previews = compile_project_previews(project)
        script = project.get("script_source", {}).get("script")
        readable = path.parent / "script.md"
        script_text = readable.read_text(encoding="utf-8-sig") if readable.exists() else script_markdown(script) if script else ""
        candidates = self._candidates(project,pid)
        shots = []
        for shot in project["shots"]:
            current = self._chosen(pid, shot["id"], candidates)
            compiled = previews.get(shot["id"], {"prompt": "", "metadata": {}})
            entities = project["entities"]
            shots.append({"id": shot["id"], "description": shot["description"], "duration_s": shot["duration_s"],
                          "scene_name": entities[shot["scene_id"]]["canonical"].get("name", shot["scene_id"]),
                          "characters": [entities[e]["canonical"].get("name", e) for e in shot["entity_ids"] if entities[e]["kind"] == "character"],
                          "dependencies": shot.get("continuity_from", []), "prompt": compiled["prompt"],
                          "state_before": compiled["metadata"].get("state_before", {}),
                          "state_after": compiled["metadata"].get("state_after", {}),
                          "candidates": [self._public_candidate(c) for c in candidates if c["shot_id"] == shot["id"]],
                          "selected_candidate_id": current["id"] if current else None,
                          "report": self._report(project, current)})
        export = None
        with self.lock:
            saved_export = self.state["exports"].get(pid)
        if saved_export:
            try:
                selected = [self._chosen(pid, s["id"], candidates) for s in project["shots"]]
                binding = [[c["shot_id"], c["id"], c["_job"].get("download_sha256")] for c in selected if c]
                if saved_export["binding"] == binding and self._hash(saved_export["preview_path"]) == saved_export["preview_sha256"]:
                    export = {"video_url": self.asset(saved_export["preview_path"],project_id=pid), "status": "PREVIEW_ONLY",
                              "accepted": False, "total_duration_s": saved_export["total_duration_s"],
                              "manifest_url": self.asset(saved_export["manifest_path"])}
            except (KeyError, OSError, ValueError):
                pass
        from pianchang.preview_access import preview_status
        gate=preview_status(self,pid)
        if not gate["ready"]:export=None
        elif gate.get("preview"):
            approved=gate["preview"]
            export={"video_url":self.asset(approved["preview_path"],approved["preview_sha256"],project_id=pid),
                "status":"READY_FOR_PREVIEW","media_accepted":True,"total_duration_s":approved["total_duration_s"]}
        return self.sanitize({"id": pid, "title": project["title"], "script_text": script_text, "script_structure": script_structure(script),
                              "logline": (script or {}).get("logline", ""), "validation": validation,
                              "shots": shots, "export": export,"preview_gate":{k:v for k,v in gate.items() if k!="allowed_media"}})

    def list_jobs(self, *, detail_project=None):
        with self.lock:
            jobs = copy.deepcopy([{k: v for k, v in j.items() if not k.startswith("_")} for j in reversed(self.state["jobs"])])
        for index,job in enumerate(jobs):
            if not job.get("project_id"):
                source_id=job.get("story_job_id") or (job["id"] if job.get("kind") in {"story","studio_script"} else None)
                if isinstance(source_id,str) and re.fullmatch(r"[a-f0-9]{32}",source_id):
                    source_path=self.state_dir/"stories"/source_id/"project.json"
                    if source_path.is_file():job["project_id"]=project_id_for_path(source_path)
            if detail_project is not None and job.get("project_id")!=detail_project:
                jobs[index]={k:v for k,v in job.items() if k in {"id","kind","project_id","story_job_id","status","created_at","updated_at","trace_id"}}
                continue
            diagnostics = self._asset_job_diagnostics(job)
            if diagnostics is not None:
                job["asset_diagnostics"] = diagnostics
        return self.sanitize(jobs)

    def _asset_job_diagnostics(self, job):
        from pianchang.asset_dispatch import ASSET_ACTIONS
        from pianchang.asset_failure_history import read_asset_job_history
        if job.get("kind") not in ASSET_ACTIONS or job.get("status") not in {"failed", "interrupted"} or not job.get("trace_id"):
            return None
        recorded = (job.get("result") or {}).get("asset_diagnostics")
        if recorded is not None:
            return recorded
        key = (job["id"], job["trace_id"])
        with self.lock:
            cached = self._asset_failure_history.get(key)
        if cached is None:
            with use_context(current_context() if (current_context() or {}).get("store_id") == self.tracer.store_id else None,
                             tracer=self.tracer):
                cached = read_asset_job_history(self, job)
            with self.lock:
                self._asset_failure_history[key] = cached
        return cached

    @service_tool("studio.assets.trace_failure_summary")
    def asset_diagnostics_for_trace(self, trace_id):
        with self.lock:
            job = next((dict(j) for j in self.state["jobs"] if j.get("trace_id") == trace_id), None)
        return self._asset_job_diagnostics(job) if job else None

    @service_tool("project.select_candidate")
    def select(self, pid, body):
        context = current_context()
        with self.lock:
            if any(j["kind"] == "agent" and j["project_id"] == pid and j["status"] in {"queued", "running"}
                   and j.get("trace_id") != (context or {}).get("trace_id") for j in self.state["jobs"]):
                raise ValueError("此故事的 Agent 正在执行，请先停止后续动作或等任务结束再手动选用候选。")
        _, project = self._project(pid)
        candidate = self._chosen(pid, body.get("shot_id"), self._candidates(project), body.get("candidate_id"))
        if not body.get("candidate_id") or not candidate or not candidate["_video"]:
            raise ValueError("只能选用此镜头已下载且哈希有效的候选。")
        with self.lock:
            self.state["selections"].setdefault(pid, {})[body["shot_id"]] = candidate["id"]
            self._save()
        return self.project_detail(pid)

    @service_tool("task.enqueue")
    def submit(self, action, pid, body, idempotency_key=None, *, library_backfill=False):
        if action=="agent":
            from pianchang.video_execution import StandaloneAgentRetired
            raise StandaloneAgentRetired(pid)
        if action=="studio_video":
            from pi_bridge.service import get_sessions
            return get_sessions(self).submit_production(pid,body,lambda:self._submit_task(action,pid,body,idempotency_key,library_backfill=library_backfill))
        return self._submit_task(action,pid,body,idempotency_key,library_backfill=library_backfill)

    def _submit_task(self, action, pid, body, idempotency_key=None, *, library_backfill=False):
        if action=="export":
            from pianchang.preview_access import require_preview
            require_preview(self,pid)
        from pianchang.asset_dispatch import ASSET_ACTIONS, validate_asset_order
        if action not in {"story", "generate", "resume", "check", "export",
                          "studio_script", "studio_video", "studio_review"} | ASSET_ACTIONS:
            raise ValueError("未知操作。")
        if not isinstance(body, dict):
            raise ValueError("请求必须为 JSON 对象。")
        if idempotency_key is not None and (not re.fullmatch(r"[\w-]{1,128}", idempotency_key)):
            raise ValueError("请求幂等标识无效。")
        project = None
        request_signature = digest_json([action, pid, body])
        # Generated reference requests acquire a parent-version pin during validation.
        # Look up the original client request first, including after that parent advanced.
        if action in ASSET_ACTIONS and idempotency_key:
            with self.lock:
                previous = next((j for j in self.state["jobs"] if j.get("_idempotency_key") == idempotency_key), None)
                if previous:
                    if previous.get("_request_signature") != request_signature:
                        raise ValueError("同一请求标识不能用于不同操作。")
                    event("task.reused", {"job_id": previous["id"], "trace_id": previous.get("trace_id"),
                                         "reason": "idempotency_key"}, kind="cache")
                    return next(j for j in self.list_jobs() if j["id"] == previous["id"])
        if action in {"studio_script", "studio_video", "studio_review"} | ASSET_ACTIONS:
            if action in ASSET_ACTIONS:
                body = validate_asset_order(self, pid, action, body)
            else:
                from pianchang.studio_jobs import validate_submission
                validate_submission(action, body)
            if action != "studio_script":
                _, project = self._project(pid)
        elif action == "story":
            if "repair_from" in body:
                if set(body) != {"repair_from"}:
                    raise ValueError("修复必须沿用原创意和数量约束，不能覆盖参数。")
                saved = self._story_repair_source(body["repair_from"])
                body = {**saved["inputs"], "repair_from": body["repair_from"], "repair_source": saved["source"]}
            elif "repair_source" in body:
                raise ValueError("修复来源由服务端读取，不能自行指定。")
            if not isinstance(body.get("idea"), str) or not 1 <= len(body["idea"].strip()) <= 6000:
                raise ValueError("请输入 1～6000 字符的创意。")
            validate_structure_constraints(story_constraints(body))
        else:
            _, project = self._project(pid)
            if action in {"generate", "check"} and body.get("shot_id") not in {s["id"] for s in project["shots"]}:
                raise ValueError("镜头不存在。")
            if body.get("prompt") is not None and (not isinstance(body["prompt"], str) or len(body["prompt"]) > 12000):
                raise ValueError("提示词应为不超过 12000 字符的文本。")
            for flag in ("new_candidate", "with_model"):
                if flag in body and type(body[flag]) is not bool:
                    raise ValueError(flag + " 必须为布尔值。")
        signature = digest_json([action, pid, body])
        if action in ASSET_ACTIONS - {"asset_production"}:
            scope = [pid, action, body["subject_id"], body["purpose"], body.get("asset_id")]
        elif action == "asset_production":
            scope = [pid, action]
        else:
            scope = [pid, signature if action == "studio_script" else action if action in
                     {"story", "agent", "studio_video"} else body.get("shot_id") or body.get("candidate_id") or "export"]
        with self.lock:
            for previous in self.state["jobs"]:
                if idempotency_key and previous.get("_idempotency_key") == idempotency_key:
                    if previous["_signature"] != signature:
                        raise ValueError("同一请求标识不能用于不同操作。")
                    event("task.reused", {"job_id": previous["id"], "trace_id": previous.get("trace_id"), "reason": "idempotency_key"}, kind="cache")
                    return next(j for j in self.list_jobs() if j["id"] == previous["id"])
                if previous["status"] in {"queued", "running"} and previous.get("_scope") == scope:
                    if previous["_signature"] == signature:
                        event("task.reused", {"job_id": previous["id"], "trace_id": previous.get("trace_id"), "reason": "same_active_request"}, kind="cache")
                        return next(j for j in self.list_jobs() if j["id"] == previous["id"])
                    raise ValueError("此镜头已有任务，请等任务结束后再操作。")
                if (previous["status"] in {"queued", "running"} and pid is not None
                        and previous["project_id"] == pid and (action in {"agent", "studio_video"}
                        or previous["kind"] in {"agent", "studio_video"})):
                    raise ValueError("此故事已有制作任务，请等任务结束后再启动其他操作。")
            job = {"id": uuid.uuid4().hex, "kind": action, "project_id": pid, "shot_id": body.get("shot_id"),
                   "status": "queued", "message": "已进入本地任务队列", "created_at": now(), "updated_at": now(),
                   "result": None, "error": None, "_signature": signature, "_scope": scope,
                   "_idempotency_key": idempotency_key}
            if action in ASSET_ACTIONS:
                job["_request_signature"] = request_signature
                job.update(asset_subject_id=body.get("subject_id"), asset_purpose=body.get("purpose"))
            if action in {"story", "studio_script"}:
                job["story_input"] = {"idea": body.get("idea") or body.get("script", {}).get("title"),
                                      **{field: body[field] for field in STORY_COUNT_FIELDS if field in body}}
                if body.get("repair_from"):
                    job["repair_from"] = body["repair_from"]
            if action == "studio_video":
                if "plan_id" in body:
                    job["plan_id"] = body["plan_id"]
                    job.update(_autonomy_enabled=True,_autonomy_body=copy.deepcopy(body))
                    if "proposal_id" in body:
                        job["proposal_id"] = body["proposal_id"]
                else:
                    job["max_video_generations"] = body["max_video_generations"]
            caller = current_context()
            links = [{"trace_id": caller["trace_id"], "span_id": caller["span_id"], "relation": "submitted_by"}] if caller else []
            if body.get("repair_source", {}).get("trace_id"):
                links.append({"trace_id": body["repair_source"]["trace_id"], "relation": "repairs"})
            task_span = self.tracer.start_span("agent.task." + action, kind="task", new_trace=True,
                       inputs={"action": action, "project_id": pid, "body": body},
                       attributes={"job_id": job["id"], "project_id": pid, "shot_id": body.get("shot_id")}, links=links)
            job.update(trace_id=task_span.trace_id, _trace_context=task_span.context)
            executor = self.script_executor if action == "studio_script" else self.executor
            predecessor = None
            if action in ASSET_ACTIONS:
                # A foreground order for another story can start immediately. Keep
                # orders for the same story in their existing queue until it drains.
                use_library = library_backfill or any(
                    j.get("asset_pool") == "library" and j["project_id"] == pid and j["status"] in {"queued", "running"}
                    for j in self.state["jobs"])
                job["asset_pool"] = "library" if use_library else "foreground"
                executor = self.library_asset_executor if use_library else self.asset_executor
                if use_library:
                    predecessor = self._asset_futures.get(pid)
            task_span.event("task.queued", {"job_id": job["id"], "request_signature": signature}, kind="state")
            self.state["jobs"].append(job)
            self._save()
            try:
                args = (job, copy.deepcopy(body), digest_json(project) if project else None)
                future = executor.submit(self._execute, *args, predecessor) if predecessor else executor.submit(self._execute, *args)
                if action in ASSET_ACTIONS:
                    self._asset_futures[pid] = (job["id"], future)
            except Exception as exc:
                self._update(job, status="failed", error=str(exc), message="任务未能进入执行队列。")
                raise
            event("task.linked", {"job_id": job["id"], "trace_id": task_span.trace_id}, kind="link")
            return next(j for j in self.list_jobs() if j["id"] == job["id"])

    @service_tool("agent.cancel_requested")
    def cancel_agent(self, job_id):
        with self.lock:
            job = next((j for j in self.state["jobs"] if j["id"] == job_id and j["kind"] == "agent"), None)
            if job is None:
                raise KeyError("Agent 任务不存在。")
            if job["status"] in {"queued", "running"}:
                self._update(job, cancel_requested=True, message="已请求停止后续动作；当前模型或工具调用返回后停止，不撤销已提交的云端任务。")
            return next(j for j in self.list_jobs() if j["id"] == job_id)

    def _update(self, job, **fields):
        with self.lock:
            context = job.get("_trace_context")
            if context:
                with use_context(context, tracer=self.tracer):
                    event("task.transition_requested", {"job_id": job["id"], "previous_status": job["status"], "changes": fields}, kind="state")
            job.update(self.sanitize(fields), updated_at=now())
            self._save()
            if context:
                with use_context(context, tracer=self.tracer):
                    event("task.updated", {"job_id": job["id"], "changes": fields, "state_persisted": True}, kind="state")
                if fields.get("status") in {"succeeded", "failed", "interrupted"}:
                    self.tracer.end_span(context, status={"succeeded": "OK", "failed": "ERROR", "interrupted": "UNKNOWN"}[fields["status"]],
                                         output=fields.get("result"), error={"message": fields["error"]} if fields.get("error") else None)

    def _execute(self, job, body, project_hash, predecessor=None):
        try:
            with use_context(job.get("_trace_context"), tracer=self.tracer):
                if predecessor:
                    previous_id, future = predecessor
                    event("asset.order.predecessor", {"job_id": previous_id}, kind="state")
                    try:
                        future.exception()
                    except CancelledError:
                        event("asset.order.predecessor_cancelled", {"job_id": previous_id}, kind="state")
                self._execute_steps(job, body, project_hash)
                if job.get("kind")=="studio_video" and hasattr(self,"_pi_sessions"):
                    self._pi_sessions.release_production(job)
                if (job.get("_autonomy_enabled") and job.get("status") in {"failed","interrupted"}
                        and not job.get("trace_incomplete")):
                    from pianchang.video_autonomy import enqueue_recovery
                    self.executor.submit(enqueue_recovery,self)
        except TraceError as exc:
            # The trace store is unavailable: never proceed with further tools or
            # automatically retry a paid call. Preserve an explicit local failure.
            with self.lock:
                job.update(status="failed", updated_at=now(), message="Trace 写入失败，已停止后续工具调用。",
                           error=self.sanitize(str(exc)), trace_incomplete=True)
                self._save()

    def _execute_steps(self, job, body, project_hash):
        self._update(job, status="running", message="正在准备工具与输入")
        try:
            self._load_configs()
            action, pid = job["kind"], job["project_id"]
            if action == "studio_script":
                from pianchang.studio_jobs import run_script_job
                result = run_script_job(self, job, body)
                if result.get("backend")=="pi" and not result.get("task_complete"):
                    self._update(job,status="interrupted",message=result["summary"],result=result)
                    return
            elif action == "story":
                result = self._build_story(job, body)
            else:
                project_path, project = self._project(pid)
                if digest_json(project) != project_hash:
                    raise ValueError("排队期间工程版本已改变，请刷新后重试。")
                if action == "studio_video":
                    from pianchang.studio_jobs import run_video_job
                    from pi_bridge.service import get_sessions
                    sessions=get_sessions(self)
                    conversation=sessions.resolve({"project":pid},"video")
                    with sessions.binding(conversation,job):result = run_video_job(self, job, body)
                    if result.get("action_required"):
                        self._update(job, status="interrupted", message=result["summary"], result=result)
                        return
                elif action == "studio_review":
                    from pianchang.studio_recovery import run_review_job
                    result = run_review_job(self, job, body)
                elif action in {"studio_reference", "asset_production", "asset_inspect", "asset_regenerate"}:
                    from pianchang.asset_dispatch import run_asset_order
                    result = run_asset_order(self, job, body)
                    if result.get("needs_attention"):
                        self._update(job, status="failed", result=result, message=result["summary"], error=result["summary"])
                        return
                elif action == "agent":
                    self._run_agent(job,body,project_hash)  # retired; never starts a model
                elif action == "generate":
                    from pianchang.studio_jobs import check_legacy_video_scope
                    check_legacy_video_scope(self, pid, body["shot_id"])
                    compiled = compile_shot(project, body["shot_id"], prompt_override=body.get("prompt") or None)
                    metadata = compiled["metadata"]
                    config = config_for_references(self.video_config, metadata.get("required_refs", []))
                    resolved = []
                    if metadata.get("required_refs"):
                        from pianchang.reference_transport import prepare_workbench_request
                        payload, resolved = prepare_workbench_request(config, compiled, project, self.root)
                    else:
                        payload = build_request(config, compiled["prompt"], duration=compiled["duration"])
                    metadata.update(project_path=str(project_path), generated_duration_s=payload["parameters"]["duration"],
                                    duration_differs_from_target=metadata["target_duration_s"] != payload["parameters"]["duration"])
                    runner = VideoRunner(config)
                    self._update(job, message="正在提交视频任务；相同输入会复用已有候选")
                    submitted = runner.submit(payload, metadata, new=body.get("new_candidate", False),
                                              **({"resolved_refs": resolved} if resolved else {}))
                    self._update(job, message="云端视频生成中，任务 ID：" + submitted["task_id"])
                    generated = runner.resume(submitted["manifest_path"])
                    result = self._generated(pid, body["shot_id"], generated)
                elif action == "resume":
                    candidate = next((c for c in self._candidates(project) if c["id"] == body.get("candidate_id")), None)
                    if not candidate:
                        raise ValueError("找不到此工程的视频候选。")
                    self._update(job, message="正在查询已有百炼任务，未重新提交生成")
                    generated = VideoRunner(self.video_config).resume(candidate["_path"])
                    result = self._generated(pid, candidate["shot_id"], generated)
                elif action == "check":
                    result = self._check(job, project, body)
                else:
                    result = self._export(job, project)
            self._update(job, status="succeeded", message=result.get("summary") or "操作完成；制作验收状态请查看镜头报告。", result=result)
        except TraceError:
            raise
        except Exception as exc:
            event("task.exception", {"type": type(exc).__name__, "message": str(exc), "stack": traceback.format_exc()}, kind="error")
            self._update(job, status="failed", message="操作未完成，已有结果和任务记录已保留。", error=self.sanitize(str(exc))[:3000])

    @service_tool("agent.execute")
    def _run_agent(self, job, body, project_hash):
        from pianchang.video_execution import StandaloneAgentRetired
        raise StandaloneAgentRetired(job["project_id"])

    def _build_story(self, job, body):
        directory = self.state_dir / "stories" / job["id"]
        self._update(job, message="正在调用百炼编写剧本、编译分镜并检查来源与剧情状态")
        extra = {}
        if body.get("repair_from"):
            saved = self._story_repair_source(body["repair_from"])
            if saved["source"] != body["repair_source"] or saved["inputs"] != job["story_input"]:
                raise ValueError("排队期间原草稿或约束已改变；已停止修复，请查看原记录。")
            extra = {"initial_draft": saved["draft"], "repair_source": saved["source"]}
        def progress(update):
            self._update(job, story_progress=update, message=update.get("summary", "正在检查与修复剧本"))
        summary = StoryRunner(self.text_config, progress=progress).build(body["idea"], directory,
                   max_repairs=2, **{field: body[field] for field in STORY_COUNT_FIELDS if field in body}, **extra)
        if not summary.get("ready_for_generation"):
            raise ValueError("剧本已保存，但制作规格经过修复后仍未通过。检查报告：" + str(directory / "validation-report.json"))
        project_path = Path(summary["project_path"]).resolve()
        pid = next(pid for pid, record in self._discover().items() if record["path"] == project_path)
        from pianchang.asset_dispatch import schedule_published_assets
        asset_job = schedule_published_assets(self, directory, pid, job)
        return {"project_id": pid, "ready_for_generation": True, "semantic_fidelity_pass": None,
                "asset_job": asset_job,
                "draft_attempts": summary.get("draft_attempts"), "draft_repair_count": summary.get("draft_repair_count"),
                "compile_attempts": summary.get("compile_attempts"), "repair_from": body.get("repair_from")}

    def _generated(self, pid, shot_id, generated):
        cid = Path(generated["manifest_path"]).parent.name
        with self.lock:
            self.state["selections"].setdefault(pid, {})[shot_id] = cid
            self._save()
        return {"project_id": pid, "shot_id": shot_id, "candidate_id": cid,
                "video_url": self.asset(generated["local_video_path"]), "task_id": generated["task_id"]}

    def _check(self, job, project, body):
        pid, shot_id = job["project_id"], body["shot_id"]
        candidate = self._chosen(pid, shot_id, self._candidates(project), body.get("candidate_id"))
        if not candidate or not candidate["_video"]:
            raise ValueError("请先生成并下载此镜头的视频。")
        parameters = candidate["_job"]["request"]["parameters"]
        size = parameters.get("size")
        model_route = (candidate["_job"].get("metadata") or {}).get("model_route") or {}
        if model_route.get("adapter") == "litellm":
            from pianchang.sdk_video import sdk_expected_size
            size = sdk_expected_size(candidate["_job"])
        if size is None:
            try:
                if candidate["_job"]["request"]["model"] not in {
                        "wan2.7-t2v", "wan2.7-t2v-2026-06-12", "wan2.7-r2v", "wan2.7-r2v-2026-06-12"}:
                    raise KeyError("model")
                size = SIZES_27[parameters["resolution"]][parameters["ratio"]]
            except KeyError:
                raise ValueError("当前验收尚未定义此模型画幅的固定分辨率，请先补充适配。") from None
        width, height = map(int, size.split("*"))
        contract = create_contract(project, shot_id, width=width, height=height)
        folder = self.state_dir / "reviews" / digest_json([contract, candidate["_job"]["download_sha256"]])[:24]
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "contract.json", contract)
        self._update(job, message="正在完整解码、读取媒体信息并提取带时间戳的证据帧")
        attempt = folder / "attempts" / job["id"]
        pointer = folder / "current-inspection.json"
        inspection = None
        if pointer.exists():
            try:
                cached = self._inside(read_json(pointer)["directory"], [folder])
                inspection = inspect_media(candidate["_video"], cached, interval_s=1, max_frames=16)
            except (MediaError, OSError, ValueError, KeyError):
                pass  # Preserve incomplete/changed evidence and use a new attempt.
        if inspection is None:
            inspection = inspect_media(candidate["_video"], attempt / "inspection", interval_s=1, max_frames=16)
        inspection_path = Path(inspection["manifest_path"])
        write_json(pointer, {"directory": str(inspection_path.parent)})
        observation_path = inspection_path.parent.parent / "observations.json"
        observations = read_json(observation_path) if observation_path.exists() else None
        if body.get("with_model") and observations is None:
            self._update(job, message="正在调用百炼视觉模型观察抽样帧；不能据此证明全程一致")
            observations = observe(inspection, contract, config=self.vision_config)
            write_json(observation_path, observations)
        report = run_checks(project, contract, inspection, observations, job=candidate["_job"])
        report_path = folder / ("report-" + digest_json(report)[:16] + ".json")
        write_json(report_path, report)
        with self.lock:
            self.state["reviews"][digest_json([digest_json(project), candidate["id"]])] = {
                "report": str(report_path), "inspection": str(inspection_path)}
            self._save()
        return {"project_id": pid, "shot_id": shot_id, "acceptance_status": report["status"],
                "task_complete": report["task_complete"], "report_url": self.asset(report_path)}

    def _export(self, job, project):
        from pianchang.preview_access import require_preview
        pid = job["project_id"]
        gate=require_preview(self,pid)
        result=gate["preview"]
        # The production Agent already rendered and verified this exact accepted
        # selection. An old export endpoint cannot assemble a different selection.
        binding=gate["production_ref"]
        with self.lock:
            self.state["exports"][pid] = {**result, "binding": binding,"production_ref":binding}
            self._save()
        return {"project_id": pid, "video_url": self.asset(result["preview_path"],result["preview_sha256"],project_id=pid),
                "status": "READY_FOR_PREVIEW", "media_accepted":True,"human_confirmed":False,
                "production_ref":binding,"total_duration_s":result["total_duration_s"]}
