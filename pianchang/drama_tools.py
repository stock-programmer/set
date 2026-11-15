"""Project-scoped tools exposed to the decision loop; no implicit job queueing.

The model chooses which tool runs. The adapter binds every action to the original
project version and verifies completion from actual media/report evidence.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path

from pianchang.agent_loop import Tool
from pianchang.bailian_video import build_request, config_for_references
from pianchang.media_tools import inspect_media as inspect_media_file
from pianchang.story_agent import query_context
from pianchang.story_spec import digest_json, validate_compilation
from trace_system import event, traced
from pianchang.video_agent import VideoRunner, compile_shot


def _schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


_SHOT = {"type": "string", "minLength": 1, "maxLength": 128}
_CANDIDATE = {"type": "string", "minLength": 1, "maxLength": 128}
_PROMPT = {"type": "string", "minLength": 1, "maxLength": 12000}


class DramaTools:
    def __init__(self, service, job, body, project_hash):
        if not re.fullmatch(r"[a-f0-9]{32}", job["id"]):
            raise ValueError("Agent 任务 ID 无效。")
        self.service, self.job = service, job
        self.pid = job["project_id"]
        self.project_hash = project_hash
        self.mode = body.get("mode", "inspect")
        if self.mode not in {"inspect", "preview", "accepted"}:
            raise ValueError("Agent 交付范围无效。")
        if type(body.get("with_model", False)) is not bool:
            raise ValueError("with_model 必须为布尔值。")
        self.allow_vision = body.get("with_model", False)
        self.directory = Path(service.state_dir) / "agents" / job["id"] / "tools"
        self.overrides = {}
        self.checked = {}
        self.exported_binding = None
        self.exported_sha256 = None
        self._project()
        self.tools = [
            Tool("read_project", "读取当前冻结剧本、制作规格、镜头及素材状态；只允许当前工程。", _schema(), self.read_project),
            Tool("query_story_state", "查询指定镜头的剧情起止状态、来源事件与设定。", _schema({"shot_id": _SHOT}, ["shot_id"]), self.query_story_state),
            Tool("patch_prompt", "保存本次运行的镜头生成提示修订，不修改冻结剧本或验收规则。下次生成使用该修订。", _schema({"shot_id": _SHOT, "prompt": _PROMPT}, ["shot_id", "prompt"]), self.patch_prompt, effect="write"),
            Tool("generate_video", "提交镜头视频生成并查询一次，可能计费且消耗一次生成额度。仅产生候选，不修改显式选用关系；完成后需调用 select_candidate 显式选用。PENDING/RUNNING 时使用返回的 candidate_id 调用 resume_video，不能重复新建。new_candidate=true 只在确需新的修复候选时使用。", _schema({"shot_id": _SHOT, "prompt": _PROMPT, "new_candidate": {"type": "boolean"}}, ["shot_id"]), self.generate_video, effect="video"),
            Tool("resume_video", "查询已有候选的云端任务一次，成功后下载；不会重新提交生成，也不修改显式选用关系。下载后需调用 select_candidate 显式选用。尚在运行时如实保留待处理状态。", _schema({"shot_id": _SHOT, "candidate_id": _CANDIDATE}, ["shot_id", "candidate_id"]), self.resume_video, effect="write"),
            Tool("inspect_media", "完整解码并抽取带时间戳的证据帧，返回元数据与图片引用；文本决策模型不能仅凭图片引用声称看过画面。", _schema({"shot_id": _SHOT, "candidate_id": _CANDIDATE}, ["shot_id"]), self.inspect_media, effect="write"),
            Tool("check_shot", "检查一个已下载候选并返回逐项 PASS/FAIL/UNKNOWN 与证据。with_model=true 会调用视觉模型，只有用户开启视觉观察时允许。", _schema({"shot_id": _SHOT, "candidate_id": _CANDIDATE, "with_model": {"type": "boolean"}}, ["shot_id"]), self.check_shot, effect="write"),
            Tool("select_candidate", "选用当前工程同一镜头中已有、下载完整且哈希有效的候选。选用不代表验收通过。", _schema({"shot_id": _SHOT, "candidate_id": _CANDIDATE}, ["shot_id", "candidate_id"]), self.select_candidate, effect="write"),
            Tool("export_preview", "取得当前已发布且全部必过项通过的预览；缺少、失败、未知、过期证据均不能导出或播放。", _schema(), self.export_preview, effect="write"),
        ]
        if self.mode == "inspect":
            allowed = {"read_project", "query_story_state", "inspect_media", "check_shot"}
            self.tools = [tool for tool in self.tools if tool.name in allowed]

    def _require_production(self):
        if self.mode == "inspect":
            raise ValueError("仅检查模式不能生成、下载新素材、修改提示词或选用关系，也不能导出；请启动相应制作范围的新任务。")

    def _project(self):
        path, project = self.service._project(self.pid)
        actual = digest_json(project)
        if actual != self.project_hash:
            event("agent.project_version_changed", {"project_id": self.pid,
                  "expected": self.project_hash, "actual": actual}, kind="validation")
            raise ValueError("工程版本在 Agent 运行期间发生变化；请基于当前版本启动新任务。")
        return path, project

    @staticmethod
    def _shot(project, shot_id):
        shot = next((s for s in project["shots"] if s["id"] == shot_id), None)
        if shot is None:
            raise ValueError("镜头不属于当前工程。")
        return shot

    def _candidate(self, project, shot_id, candidate_id=None, *, require_video=True):
        self._shot(project, shot_id)
        candidate = self.service._chosen(self.pid, shot_id, self.service._candidates(project), candidate_id)
        if not candidate or candidate["shot_id"] != shot_id:
            raise ValueError("当前镜头没有可用候选；请先生成或查询已有任务。")
        if candidate_id and candidate["id"] != candidate_id:
            raise ValueError("候选不属于当前工程或镜头。")
        if require_video and (not candidate.get("_video") or
                              self.service._hash(candidate["_video"]) != candidate["_job"].get("download_sha256")):
            raise ValueError("候选尚未下载完成或视频哈希已改变。")
        return candidate

    def _persist(self):
        data = self.service.tracer.sanitize({"project_id": self.pid, "project_sha256": self.project_hash,
                                            "prompt_overrides": self.overrides, "checked": self.checked,
                                            "exported_binding": self.exported_binding,
                                            "exported_sha256": self.exported_sha256})
        event("agent.tool_state_write", data, kind="artifact")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / (".state-" + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.directory / "state.json")

    @staticmethod
    def _report_summary(report):
        if not report:
            return None
        checks = report.get("checks", report.get("results", []))
        return {"status": report.get("status"), "task_complete": report.get("task_complete") is True,
                "unresolved": [{"criterion_id": c.get("criterion_id", c.get("id")), "status": c.get("status")}
                               for c in checks if c.get("status") != "PASS"],
                "report_url": report.get("report_url")}

    def _selection_info(self, shot_id, candidates):
        candidate = self.service._chosen(self.pid, shot_id, candidates)
        with self.service.lock:
            configured = self.service.state["selections"].get(self.pid, {}).get(shot_id)
        explicit = bool(candidate and configured == candidate["id"])
        return {"selected_candidate_id": candidate["id"] if candidate else None,
                "selection_source": "explicit" if explicit else "default_candidate" if candidate else "none",
                "explicit_candidate_id": candidate["id"] if explicit else None,
                "default_candidate": candidate["id"] if candidate and not explicit else None}

    @traced("drama.tools.snapshot", kind="tool")
    def snapshot(self):
        _, project = self._project()
        candidates = self.service._candidates(project)
        shots = []
        for shot in project["shots"]:
            candidate = self.service._chosen(self.pid, shot["id"], candidates)
            report = self.service._report(project, candidate) if candidate else None
            shots.append({"shot_id": shot["id"], "description": shot["description"],
                          **self._selection_info(shot["id"], candidates),
                          "candidates": [self.service._public_candidate(c) for c in candidates if c["shot_id"] == shot["id"]],
                          "report": self._report_summary(report), "prompt_override": self.overrides.get(shot["id"])})
        return {"project_id": self.pid, "project_sha256": self.project_hash, "title": project["title"],
                "mode": self.mode, "visual_observation_authorized": self.allow_vision, "shots": shots,
                "completion_scope": "inspect=完成所有镜头检查并如实报告问题；preview=有效选用素材的预览；accepted=镜头必过检查全部通过及有效预览。整片创作审看单独保留。",
                "limitations": ["有固定参考的镜头须用支持该用途的模型；实际提交的素材以候选任务的参考回执为准。",
                                "抽样帧不足以证明全程视觉或音频一致性。"]}

    @traced("drama.tools.read_project", kind="tool")
    def read_project(self):
        _, project = self._project()
        return {"project": copy.deepcopy(project), "validation": validate_compilation(project), "state": self.snapshot()}

    @traced("drama.tools.query_story_state", kind="tool")
    def query_story_state(self, shot_id):
        _, project = self._project()
        self._shot(project, shot_id)
        return query_context(project, shot_id)

    @traced("drama.tools.patch_prompt", kind="tool")
    def patch_prompt(self, shot_id, prompt):
        self._require_production()
        _, project = self._project()
        self._shot(project, shot_id)
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 12000:
            raise ValueError("提示词必须为 1～12000 字符。")
        self.overrides[shot_id] = prompt
        self._persist()
        return {"shot_id": shot_id, "prompt": prompt, "scope": "this_run_generation_only", "frozen_project_changed": False}

    def _generation_result(self, project, shot_id, generated):
        self._project()
        cid = Path(generated["manifest_path"]).parent.name
        result = {"project_id": self.pid, "shot_id": shot_id, "candidate_id": cid,
                  "task_id": generated.get("task_id"), "status": generated.get("status"),
                  "media_accepted": False, "selection_changed": False,
                  **self._selection_info(shot_id, self.service._candidates(project))}
        if generated.get("status") == "SUCCEEDED" and generated.get("local_video_path"):
            candidate = self._candidate(project, shot_id, cid)
            result["video_url"] = self.service.asset(candidate["_video"])
            result["media_sha256"] = candidate["_job"]["download_sha256"]
            result["next_action"] = "Candidate is ready. Use select_candidate to explicitly choose it; this tool did not change the explicit selection. default_candidate is only the workbench fallback, not an explicit choice."
        else:
            result["next_action"] = "resume_video with this candidate_id; no new submission required"
        return result

    @traced("drama.tools.generate_video", kind="tool")
    def generate_video(self, shot_id, prompt=None, new_candidate=False):
        self._require_production()
        path, project = self._project()
        self._shot(project, shot_id)
        if type(new_candidate) is not bool:
            raise ValueError("new_candidate 必须为布尔值。")
        if prompt is not None and (not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 12000):
            raise ValueError("提示词必须为 1～12000 字符。")
        pending = [c for c in self.service._candidates(project) if c["shot_id"] == shot_id
                   and c.get("status") in {"SUBMITTING", "SUBMISSION_UNKNOWN", "PENDING", "RUNNING", "UNKNOWN"}]
        if pending:
            if new_candidate or len(pending) != 1 or not pending[0].get("task_id"):
                raise ValueError("镜头已有未结束或提交结果未确认的任务；必须先查询或人工核对，不能再次生成。")
            candidate = pending[0]
            return {"project_id": self.pid, "shot_id": shot_id, "candidate_id": candidate["id"],
                    "task_id": candidate["task_id"], "status": candidate["status"],
                    "submission_performed": False, "media_accepted": False, "selection_changed": False,
                    **self._selection_info(shot_id, self.service._candidates(project)),
                    "next_action": "resume_video with this candidate_id; a pending task already exists"}
        override = prompt if prompt is not None else self.overrides.get(shot_id)
        compiled = compile_shot(project, shot_id, prompt_override=override)
        metadata = compiled["metadata"]
        config = config_for_references(self.service.video_config, metadata.get("required_refs", []))
        resolved = []
        if metadata.get("required_refs"):
            from pianchang.reference_transport import prepare_workbench_request
            payload, resolved = prepare_workbench_request(config, compiled, project, self.service.root)
        else:
            payload = build_request(config, compiled["prompt"], duration=compiled["duration"])
        metadata.update(project_path=str(path), generated_duration_s=payload["parameters"]["duration"],
                        duration_differs_from_target=metadata["target_duration_s"] != payload["parameters"]["duration"])
        runner = VideoRunner(config)
        submitted = runner.submit(payload, metadata, new=new_candidate, **({"resolved_refs": resolved} if resolved else {}))
        generated = runner.resume(submitted["manifest_path"], wait=False)
        return self._generation_result(project, shot_id, generated)

    @traced("drama.tools.resume_video", kind="tool")
    def resume_video(self, shot_id, candidate_id):
        self._require_production()
        _, project = self._project()
        candidate = self._candidate(project, shot_id, candidate_id, require_video=False)
        generated = VideoRunner(self.service.video_config).resume(candidate["_path"], wait=False)
        return self._generation_result(project, shot_id, generated)

    @traced("drama.tools.inspect_media", kind="tool")
    def inspect_media(self, shot_id, candidate_id=None):
        _, project = self._project()
        candidate = self._candidate(project, shot_id, candidate_id)
        evidence = self.directory / "inspections" / uuid.uuid4().hex
        inspection = inspect_media_file(candidate["_video"], evidence, interval_s=1, max_frames=16)
        self._project()
        return {"shot_id": shot_id, "candidate_id": candidate["id"],
                "inspection_status": inspection["inspection_status"], "media_sha256": inspection["media_sha256"],
                "metadata": inspection["metadata"], "decode": inspection["decode"], "sampling": inspection["sampling"],
                "frames": [{"id": f["id"], "timestamp_s": f["time_s"], "sha256": f["sha256"],
                            "url": self.service.asset(f["path"])} for f in inspection["frames"]],
                "manifest_url": self.service.asset(inspection["manifest_path"]),
                "observation_scope": "Metadata and frame references only; text controller has not visually inspected these images. Use authorized check_shot(with_model=true) for sampled visual observations."}

    @traced("drama.tools.check_shot", kind="tool")
    def check_shot(self, shot_id, candidate_id=None, with_model=None):
        _, project = self._project()
        if with_model is None:
            with_model = self.allow_vision
        if type(with_model) is not bool or (with_model and not self.allow_vision):
            raise ValueError("本次任务没有授权付费视觉观察；不能由模型扩大权限。")
        candidate = self._candidate(project, shot_id, candidate_id)
        result = self.service._check(self.job, project, {"shot_id": shot_id, "candidate_id": candidate["id"], "with_model": with_model})
        self._project()
        fresh = self._candidate(project, shot_id, candidate["id"])
        report = self.service._report(project, fresh)
        if report is None:
            raise ValueError("检查结果缺少与当前素材绑定的可验证报告。")
        self.checked[shot_id] = {"candidate_id": fresh["id"], "media_sha256": fresh["_job"]["download_sha256"],
                                "generation_job_sha256": digest_json(fresh["_job"]), "report_url": report.get("report_url")}
        self._persist()
        return {**result, "candidate_id": fresh["id"], "report": report,
                "media_accepted": report.get("task_complete") is True}

    @traced("drama.tools.select_candidate", kind="tool")
    def select_candidate(self, shot_id, candidate_id):
        self._require_production()
        _, project = self._project()
        candidate = self._candidate(project, shot_id, candidate_id)
        self.service.select(self.pid, {"shot_id": shot_id, "candidate_id": candidate["id"]})
        self._project()
        return {"project_id": self.pid, "shot_id": shot_id, "selected_candidate_id": candidate["id"],
                "selection_source": "explicit", "explicit_candidate_id": candidate["id"],
                "default_candidate": None, "selection_is_not_acceptance": True}

    def _binding(self, project):
        candidates = self.service._candidates(project)
        binding = []
        for shot in project["shots"]:
            selected = self.service._chosen(self.pid, shot["id"], candidates)
            if not selected or not selected.get("_video"):
                return None
            if self.service._hash(selected["_video"]) != selected["_job"].get("download_sha256"):
                return None
            binding.append([shot["id"], selected["id"], selected["_job"]["download_sha256"]])
        return binding

    @traced("drama.tools.export_preview", kind="tool")
    def export_preview(self):
        self._require_production()
        from pianchang.preview_access import require_preview
        gate=require_preview(self.service,self.pid)
        _, project = self._project()
        result = self.service._export(self.job, project)
        self._project()
        with self.service.lock:
            saved = copy.deepcopy(self.service.state["exports"].get(self.pid))
        gate=require_preview(self.service,self.pid)
        binding=gate["production_ref"]
        if not saved or saved.get("binding") != binding or self.service._hash(saved["preview_path"]) != saved.get("preview_sha256"):
            raise ValueError("预览导出未绑定当前选用素材，或文件哈希不符。")
        self.exported_binding = binding
        self.exported_sha256 = saved["preview_sha256"]
        self._persist()
        return {**result, "media_accepted": True, "creative_review": "not_performed"}

    @traced("drama.tools.finish_check", kind="validation")
    def finish_check(self):
        _, project = self._project()
        candidates = self.service._candidates(project)
        checks = []
        for shot in project["shots"]:
            candidate = self.service._chosen(self.pid, shot["id"], candidates)
            report = self.service._report(project, candidate) if candidate else None
            recorded = self.checked.get(shot["id"], {})
            checked_now = bool(candidate and report and candidate.get("_video")
                               and self.service._hash(candidate["_video"]) == candidate["_job"].get("download_sha256")
                               and recorded.get("candidate_id") == candidate["id"]
                               and recorded.get("media_sha256") == candidate["_job"].get("download_sha256")
                               and recorded.get("generation_job_sha256") == digest_json(candidate["_job"]))
            checks.append({"shot_id": shot["id"], "candidate_id": candidate["id"] if candidate else None,
                           **self._selection_info(shot["id"], candidates),
                           "checked_this_run": checked_now, "acceptance_status": report.get("status") if report else "UNKNOWN",
                           "task_complete": bool(report and report.get("task_complete") is True),
                           "report": self._report_summary(report)})
        from pianchang.preview_access import preview_status
        gate=preview_status(self.service,self.pid) if self.mode!="inspect" else {"ready":False,"production_ref":None}
        binding=gate["production_ref"]
        with self.service.lock:
            saved = copy.deepcopy(self.service.state["exports"].get(self.pid))
        preview_valid = False
        try:
            preview_valid = bool(gate["ready"] and saved and binding and self.exported_binding == binding == saved.get("binding")
                                 and self.exported_sha256 == saved.get("preview_sha256")
                                 and self.service._hash(saved["preview_path"]) == saved["preview_sha256"])
        except (OSError, KeyError, ValueError):
            pass
        checked_all = bool(checks) and all(c["checked_this_run"] for c in checks)
        accepted_all = gate["ready"]
        complete = {"inspect": checked_all, "preview": preview_valid, "accepted": accepted_all and preview_valid}[self.mode]
        result = {"complete": complete, "mode": self.mode, "project_id": self.pid,
                  "project_sha256": self.project_hash, "shots": checks, "preview_valid": preview_valid,
                  "media_accepted": accepted_all, "creative_review": "not_performed",
                  "completion_scope": "Completes only the selected mode. inspect may complete with FAIL/UNKNOWN evidence; it does not approve media. accepted requires current shot checks plus valid preview, not human creative approval."}
        if self.mode == "inspect":
            result["required_next"] = ["check_shot:" + c["shot_id"] for c in checks if not c["checked_this_run"]]
        elif self.mode == "preview":
            result["required_next"] = [] if preview_valid else ["Generate/resume/select missing valid clips, then export_preview for the current selection."]
        else:
            result["required_next"] = ["Resolve FAIL/UNKNOWN mandatory checks and check current selected clips; export_preview. If evidence/tool capability is insufficient, finish needs_input."] if not complete else []
        unresolved = sum(c["acceptance_status"] != "PASS" for c in checks)
        if not complete:
            result["summary"] = "任务尚未满足选定交付范围；请查看 required_next 和各镜头检查证据。"
        elif self.mode == "inspect":
            result["summary"] = f"已完成全部 {len(checks)} 个当前选用镜头的检查；{unresolved} 个镜头仍为失败或未知。检查任务完成不代表影片验收通过。"
        elif self.mode == "preview":
            result["summary"] = "当前已发布版本的全部必过项与文件绑定已核对通过，可人工预览；人工确认尚未进行。"
        else:
            result["summary"] = "当前选用镜头的必过检查全部通过，且预览绑定和文件哈希有效；整片创作审看仍未执行。"
        event("validation.agent_completion", result, kind="validation")
        return result


@traced("drama.tools.build", kind="tool", capture_output=False)
def build_drama_tools(service, job, body, project_hash):
    return DramaTools(service, job, body, project_hash)
