"""Evidence-bound repair tools for the video agent; original contracts stay fixed."""
from __future__ import annotations

import copy
import itertools
import json
import math
import os
import sys
from pathlib import Path

from pianchang.agent_loop import Tool
from pianchang.acceptance import file_sha256
from pianchang.story_spec import digest_json
from pianchang.studio_live_progress import local_failure_before_generation
from trace_system import TraceError, current_context, event, traced

POLICY_VERSION = "acceptance-loop/1"
PRESERVE_CRITERIA = {"character_identity","scene_geometry","action_sequence","file_decode","reference_submission","start_end_state","script_subtitle"}
SELECTION_POLICY = "mandatory-regression/2"


@traced("video.repair.preserved_passes",kind="validation",capture_inputs=False)
def preserved_passes(clip, summary, selected):
    """Compare passed cuts only when their other endpoint is still the selected media."""
    from pianchang.video_collab_agent import _read
    report=_read(summary["report_path"])
    if digest_json(report)!=summary["report_sha256"] or report["media_sha256"]!=clip["media_sha256"]:
        raise ValueError("候选验收与实际素材绑定不符。")
    passed={r["criterion_id"] for r in report["results"] if r.get("required") and
            r["criterion_id"] in PRESERVE_CRITERIA and r["status"]=="PASS"}
    evidence_path=Path(summary["report_path"]).parent/"content.json"
    if report.get("content_review_version") and evidence_path.is_file():
        evidence=_read(evidence_path)
        if digest_json(evidence)!=report["content_evidence_sha256"]:
            raise ValueError("相邻衔接的内容证据绑定不符。")
        bindings=evidence["selected_media"]
        sid=report["shot_id"]
        for row in report["results"]:
            cid=row["criterion_id"]
            if not row.get("required") or row["status"]!="PASS" or not cid.startswith("continuity:"):continue
            endpoints=cid.split(":",1)[1].split("->")
            if sid not in endpoints:continue
            others=[other for other in endpoints if other!=sid]
            current=all(other in bindings and other in selected and
                bindings[other]["sha256"]==selected[other]["media_sha256"] for other in others)
            event("video.repair.cut_comparison_binding",{"path":cid+".other_endpoint_is_current","expected":True,
                "actual":current,"status":"PASS" if current else "UNKNOWN",
                "reason":"当前邻镜头不变，可比较历史通过项。" if current else "邻镜头已改变，历史通过不能用于当前组合。"},kind="validation")
            if current:passed.add(cid)
    return sorted(passed)


@traced("video.repair.verified_selection_plan",kind="validation",capture_inputs=False)
def verified_selection_plan(project,clips,reports,history):
    """Find a better *already reviewed* combination, without spending or inventing cut verdicts."""
    from pianchang.video_collab_agent import _read
    empty={"changes":{},"known_pass_gain":0}
    order=[s["id"] for s in (project or {}).get("shots",[]) if s["id"] in clips and s["id"] in reports]
    if not order:return empty
    project_hash=digest_json(project);pools={};edges={};seen_reports=set();current={}
    def observe(sid,clip,summary):
        report=_read(summary["report_path"])
        if not report.get("content_review_version"):return None
        valid=(digest_json(report)==summary["report_sha256"] and report["media_sha256"]==clip["media_sha256"]
               and report["project_sha256"]==project_hash and report["shot_id"]==sid)
        if not _check("selection."+sid+".report_binding",True,valid):raise ValueError("组合选片的实际验收绑定不符："+sid)
        required={r["criterion_id"] for r in report["results"] if r.get("required") and not r["criterion_id"].startswith("continuity:")}
        passed={r["criterion_id"] for r in report["results"] if r.get("required") and r["status"]=="PASS" and not r["criterion_id"].startswith("continuity:")}
        if summary["report_sha256"] not in seen_reports:
            seen_reports.add(summary["report_sha256"])
            content=_read(Path(summary["report_path"]).parent/"content.json")
            if not _check("selection."+sid+".content_hash",report["content_evidence_sha256"],digest_json(content)):
                raise ValueError("组合选片内容证据已改变。")
            for row in report["results"]:
                if not row.get("required") or not row["criterion_id"].startswith("continuity:"):continue
                left,right=row["criterion_id"].split(":",1)[1].split("->")
                bound=content["selected_media"]
                if left in bound and right in bound:
                    key=(left,right,bound[left]["sha256"],bound[right]["sha256"])
                    edges.setdefault(key,set()).add(row["status"])
        return {"clip":clip,"report":summary,"passed":passed,"required":required}
    for sid in order:
        value=observe(sid,clips[sid],reports[sid])
        if value is None:return empty
        current[sid]=value;pools[sid]={clips[sid]["media_sha256"]:value}
    for item in history:
        sid=item["shot_id"]
        if sid not in pools:continue
        for ck,rk in [("source_clip","source_report"),("result_clip","result_report")]:
            clip,summary=item.get(ck),item.get(rk)
            if not clip or not summary:continue
            value=observe(sid,clip,summary)
            if value is None or not current[sid]["required"]<=value["required"] or not current[sid]["passed"]<=value["passed"]:continue
            sha=clip["media_sha256"]
            if sha==clips[sid]["media_sha256"]:continue
            if sha not in pools[sid] or len(value["passed"])>len(pools[sid][sha]["passed"]):pools[sid][sha]=value
    pairs=[(a["id"],b["id"]) for a,b in zip(project["shots"],project["shots"][1:]) if a["id"] in current and b["id"] in current]
    def cut(pair,selection):
        left,right=pair
        values=edges.get((left,right,selection[left]["clip"]["media_sha256"],selection[right]["clip"]["media_sha256"]))
        # Conflicting historical observations cannot be promoted to PASS.
        return None if not values else "FAIL" if "FAIL" in values else "UNKNOWN" if "UNKNOWN" in values else "PASS"
    protected=[pair for pair in pairs if cut(pair,current)=="PASS"]
    def score(selection):
        return sum(len(value["passed"]) for value in selection.values())+sum(cut(pair,selection)=="PASS" for pair in pairs)
    baseline=score(current);best=current;best_score=baseline
    choices=[list(pools[sid].values()) for sid in order]
    count=math.prod(len(values) for values in choices)
    if count<=20000:
        candidates=(dict(zip(order,values)) for values in itertools.product(*choices))
        search="all_existing_combinations"
    else:
        def neighborhoods():
            for left,right in pairs:
                for a,b in itertools.product(pools[left].values(),pools[right].values()):yield {**current,left:a,right:b}
        candidates=neighborhoods();search="adjacent_pair_neighborhoods"
    for candidate in candidates:
        if any(cut(pair,candidate) is None for pair in pairs):continue
        if any(cut(pair,candidate)!="PASS" for pair in protected):continue
        value=score(candidate)
        if value>best_score:
            best,best_score=candidate,value
        elif value==best_score and value>baseline:
            changed=lambda selection:sum(selection[s]["clip"]["media_sha256"]!=clips[s]["media_sha256"] for s in order)
            if changed(candidate)<changed(best):best=candidate
    changes={sid:{k:copy.deepcopy(best[sid][k]) for k in ["clip","report"]} for sid in order
             if best[sid]["clip"]["media_sha256"]!=clips[sid]["media_sha256"]}
    _check("selection.required_coverage_preserved",True,all(current[s]["required"]<=best[s]["required"] for s in order))
    _check("selection.current_passes_preserved",True,all(current[s]["passed"]<=best[s]["passed"] for s in order) and all(cut(pair,best)=="PASS" for pair in protected))
    _check("selection.recorded_cuts_only",True,all(cut(pair,best) is not None for pair in pairs))
    _check("selection.changed_only_for_known_improvement",True,not changes or best_score>baseline)
    return {"changes":changes,"known_pass_gain":best_score-baseline,"search":search,"candidate_combinations":count,
            "existing_passes_preserved":True,"new_video_posts":0,"new_cut_verdicts_invented":False}


def _check(path, expected, actual):
    valid = expected == actual
    event("video.repair.check", {"path":path, "expected":expected, "actual":actual,
          "status":"PASS" if valid else "FAIL"}, kind="validation")
    return valid


def _subtitle_font():
    if sys.platform == "darwin":
        system_fonts = Path("/System/Library/Fonts")
        candidates = [system_fonts / "PingFang.ttc", system_fonts / "STHeiti Medium.ttc",
                      system_fonts / "STHeiti Light.ttc", system_fonts / "Supplemental/Songti.ttc"]
        for folder in (Path.home() / "Library/Fonts", Path("/Library/Fonts")):
            candidates.extend(folder / name for name in ("NotoSansCJKsc-Regular.otf", "SourceHanSansSC-Regular.otf"))
        hint = "请在字体册中安装中文字体，或将 NotoSansCJKsc-Regular.otf 放入 ~/Library/Fonts。"
    else:
        candidates = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"]
        hint = "请检查系统是否已安装微软雅黑。"
    font = next((path for path in candidates if path.is_file() and os.access(path, os.R_OK)), None)
    if not _check("local_edit.subtitle_font_readable", True, font is not None):
        raise ValueError("缺少可读取的中文字幕字体。" + hint)
    return font


def retake_limit(policy, shot_id):
    """The current ceiling also applies to previously frozen orders."""
    if shot_id in policy.get("previously_retried_shots", ()):
        return 0
    return min(1, policy.get("max_repairs_per_shot", 1))


def authorized_video_count(plan):
    policy = plan.get("acceptance_loop") or {}
    first_pass = len(plan["new_shots"]) + len(plan["retake_shots"])
    total = first_pass + sum(retake_limit(policy, sid) for sid in policy.get("shot_ids", ()))
    cap = plan.get("generation_request_limit", total)
    if type(cap) is not int or cap < first_pass:
        raise ValueError("生成任务数量超过了本次委托的总请求上限。")
    return min(total, cap)


def failed_retake_issues(history):
    summary = history["result_report"]
    report = json.loads(Path(summary["report_path"]).read_bytes())
    if (digest_json(report) != summary["report_sha256"]
            or report["media_sha256"] != history["result_clip"]["media_sha256"]):
        raise ValueError("重拍验收报告与实际候选不符。")
    return [{k: row[k] for k in ("criterion_id", "status", "reason")}
            for row in report["results"] if row.get("required") and row["status"] != "PASS"]


@traced("video.repair.policy", kind="validation", capture_inputs=False)
def acceptance_policy(shot_ids, *, repairs_per_shot=1, previously_retried_shots=()):
    if not _check("repair_policy.repairs_per_shot", True, type(repairs_per_shot) is int and 0 <= repairs_per_shot <= 1):
        raise ValueError("每镜头自动重拍上限须为 0 或 1。")
    policy = {"version": POLICY_VERSION, "shot_ids": list(shot_ids),
            "autonomy_version": "video-autonomy/1",
            "max_repairs_per_shot": repairs_per_shot, "max_local_edits_per_shot": 0,
            "completion_requires": "all_required_checks_pass"}
    carried = sorted(set(previously_retried_shots).intersection(policy["shot_ids"]))
    if carried:
        policy["previously_retried_shots"] = carried
    return policy


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or []}


def _planner_result(result):
    from pianchang.video_collab_agent import _model_result
    return _model_result(result)


class VideoRepair:
    def __init__(self, workspace):
        self.w = workspace
        self.plans = {}
        self.history = []
        self.local_edits = {}
        sid = {"type": "string", "minLength": 1, "maxLength": 128}
        self.tools = [
            Tool("read_acceptance", "读取当前选用视频的必过失败/待补证据项、原始要求、实际观察及参考。修复前先读取。",
                 _schema({"shot_id": sid}), self.read),
            Tool("prepare_shot_repair", "为实际失败项准备定向修复提示，绑定当前候选和验收报告。保留原剧本、参考和所有必过条件。",
                 _schema({"shot_id": sid, "criterion_ids": {"type": "array", "minItems": 1, "maxItems": 30, "items": sid},
                          "instruction": {"type": "string", "minLength": 10, "maxLength": 3500},
                          "reason": {"type": "string", "minLength": 1, "maxLength": 1500}},
                         ["shot_id", "criterion_ids", "instruction", "reason"]), self.prepare, effect="write"),
            Tool("regenerate_shot", "执行已绑定证据的定向重拍，保留旧候选，自动验收新候选并复查相邻衔接。仅在自动修复额度内提交。",
                 _schema({"shot_id": sid, "repair_id": sid}, ["shot_id", "repair_id"]), self.regenerate, effect="video", preflight=self.preflight, context_result=_planner_result),
            Tool("edit_shot_asset", "用本地视频工具将指定时间后的背景切黑并可加剧本原文字幕，保持时长，保留原素材并重新验收。不能修复缺失的表演动作。",
                 _schema({"shot_id": sid, "repair_id": sid,
                          "black_from_s": {"type": "number", "minimum": 0.1, "maximum": 60},
                          "subtitle": {"type": "string", "maxLength": 160}},
                         ["shot_id", "repair_id", "black_from_s", "subtitle"]), self.edit, effect="write",context_result=_planner_result)]

    def _paths(self):
        from pianchang.video_collab_agent import _write
        _write(self.w.directory / "repair-history.json", {"version": POLICY_VERSION,
               "history": self.history, "plans": self.plans})

    def retake_spent(self, shot_id):
        policy = (self.w.shooting_plan or {}).get("acceptance_loop") or {}
        inherited = (self.w.partial or {}).get("previously_retried_shots", ())
        return shot_id in inherited or retake_limit(policy, shot_id) == 0 or any(
            h["shot_id"] == shot_id and h.get("strategy") == "prompt_and_locked_references"
            and h.get("status") != "local_failure_before_post" for h in self.history)

    def require_retake_available(self, shot_id):
        if self.retake_spent(shot_id):
            raise ValueError(f"{shot_id} 的自动重拍额度已用完，已停止重拍，等待人工审看。")

    def report_retake_failure(self, history):
        if history.get("strategy") != "prompt_and_locked_references" or not history.get("result_report"):
            return
        summary = history["result_report"]
        if history.get("retake_failure", {}).get("report_sha256") == summary["report_sha256"]:
            return
        issues = failed_retake_issues(history)
        if not issues:
            return
        lines = [f'{row["criterion_id"]} [{row["status"]}] {row["reason"]}' for row in issues]
        message = "\n".join([f'{history["shot_id"]} 重拍后验收仍未通过，已停止自动重拍。', *lines])
        failure = {"report_sha256": summary["report_sha256"], "issues": issues, "summary": message}
        event("video.repair.retake_limit_reached", {"shot_id": history["shot_id"], **failure}, kind="decision")
        history["retake_failure"] = failure
        self._paths()
        print(message, file=sys.stderr, flush=True)
        self.w._progress(message)

    def _rows(self):
        from pianchang.video_collab_agent import _read
        from pianchang.creative_sound import effective_results
        rows = []
        for sid, summary in self.w.reports.items():
            report = _read(summary["report_path"])
            if digest_json(report) != summary["report_sha256"] or report["media_sha256"] != self.w.shots[sid]["media_sha256"]:
                raise ValueError("验收报告与当前候选不符：" + sid)
            shot = next(s for s in self.w.project["shots"] if s["id"] == sid)
            for row in effective_results(shot, report):
                if row.get("required") and row["status"] != "PASS":
                    rows.append({"shot_id": sid, "report_sha256": summary["report_sha256"], **row})
        return rows

    @traced("video.repair.read", kind="tool", capture_inputs=False)
    def read(self, shot_id=None):
        from pianchang.video_collab_agent import _objects, _read
        rows = [r for r in self._rows() if shot_id is None or r["shot_id"] == shot_id]
        issues = []
        for row in rows:
            item = {k: row[k] for k in ("shot_id", "report_sha256", "criterion_id", "status", "reason",
                    "expectation", "execution_status", "limitations") if k in row}
            item["report_path"] = self.w.reports[row["shot_id"]]["report_path"]
            item["time_ranges"] = [{k: e[k] for k in ("type", "start_s", "end_s", "observation") if k in e}
                                   for e in row.get("evidence", []) if e.get("type") in {"video", "audio"}]
            if shot_id is not None:
                item["state_checks"] = row.get("state_checks", [])
                item["components"] = row.get("components", [])
            issues.append(item)
        prior_attempts = []
        for sid, clip in self.w.shots.items():
            if not clip.get("repair_id") or shot_id is not None and sid != shot_id:
                continue
            manifest = _read(clip["manifest_path"])
            if not _check("repair.prior_manifest_sha256", clip["generation_job_sha256"], digest_json(manifest)):
                raise ValueError("之前的修复提交记录已改变，不能据此继续重拍。")
            attempt = manifest.get("metadata", {}).get("acceptance_repair")
            if attempt:
                prior_attempts.append({"shot_id": sid, "task_id": clip["task_id"],
                                       "current_media_sha256": clip["media_sha256"], **attempt})
        return {"stage": "repairing" if rows else "checking", "issues": issues,
                "shots": [{"shot_id": sid, "shot": _objects(self.w.project, "shots")[sid],
                           "media_sha256": clip["media_sha256"], "media_path": clip["media_path"],
                           "approved_refs": self.w._required(sid)} for sid, clip in self.w.shots.items() if shot_id is None or sid == shot_id],
                "missing_shots": [s["id"] for s in self.w.project["shots"] if s["id"] not in self.w.shots],
                "repair_plans": [{k: p[k] for k in ("repair_id", "shot_id", "criterion_ids", "reason")} for p in self.plans.values()],
                "history": [{k: h[k] for k in ("repair_id", "shot_id", "strategy", "status")} for h in self.history],
                "prior_attempts": prior_attempts,
                "provider_failures": self.w.failed_shots,
                "rule": "UNKNOWN不算通过。画外/认知状态或规格矛盾须具体说明，可用已有改稿协商工具请求澄清；不得删条件或凭表情补证。"}

    @traced("video.repair.prepare", kind="tool", capture_inputs=False)
    def prepare(self, shot_id, criterion_ids, instruction, reason):
        from pianchang.video_collab_agent import _write
        self.require_retake_available(shot_id)
        failed = self.w.failed_shots.get(shot_id)
        if (shot_id not in self.w.shots and not failed) or len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("修复必须绑定一个已就绪候选及不重复的验收项。")
        rows = [r for r in self._rows() if r["shot_id"] == shot_id and r["criterion_id"] in criterion_ids]
        if failed and not self.w.shots.get(shot_id) and criterion_ids == ["provider_generation"]:
            from pianchang.video_collab_agent import _read
            job = _read(failed["manifest_path"])
            if digest_json(job) != failed["manifest_sha256"] or job["status"] not in {"FAILED", "CANCELED", "CANCELLED"}:
                raise ValueError("供应商最终失败状态无法核实，不得重拍。")
            rows = [{"shot_id": shot_id, "criterion_id": "provider_generation", "status": "FAIL", "reason": failed["reason"]}]
        if not _check("repair.unresolved_criteria", sorted(criterion_ids), sorted(r["criterion_id"] for r in rows)):
            raise ValueError("只能针对当前报告中尚未通过的实际验收项修复。")
        if any(r["criterion_id"] in {"evidence_integrity", "candidate_binding", "reference_submission"}
               or r.get("execution_status") in {"blocked_missing_media", "blocked_missing_cut_window",
                                                "execution_error", "evidence_invalid", "not_run"}
               or r.get("creative_audio_policy") and r["status"] != "FAIL"
               for r in rows):
            raise ValueError("请先补齐缺失镜头或恢复检查/素材绑定；不能以重拍掩盖证据或执行错误。")
        plan = {"shot_id": shot_id, "candidate_sha256": self.w.shots.get(shot_id, {}).get("media_sha256"),
                "report_sha256": self.w.reports.get(shot_id, {}).get("report_sha256"), "criterion_ids": criterion_ids,
                "instruction": instruction, "reason": reason, "issues": rows,
                "project_sha256": digest_json(self.w.project), "trace_context": current_context()}
        repair_id = digest_json(plan)
        self.plans[repair_id] = {**plan, "repair_id": repair_id}
        _write(self.w.directory / "repair-plans" / (repair_id + ".json"), self.plans[repair_id])
        self._paths()
        event("video.repair.planned", self.plans[repair_id], kind="decision")
        return {k: self.plans[repair_id][k] for k in
                ("repair_id", "shot_id", "candidate_sha256", "report_sha256", "criterion_ids", "instruction", "reason")}

    def _plan(self, shot_id, repair_id):
        plan = self.plans.get(repair_id)
        if not plan or plan["shot_id"] != shot_id:
            raise ValueError("修复计划不存在或镜头不匹配。")
        binding = {"candidate_sha256":self.w.shots.get(shot_id, {}).get("media_sha256"),
                   "report_sha256":self.w.reports.get(shot_id, {}).get("report_sha256"),
                   "project_sha256":digest_json(self.w.project)}
        if not all([_check("repair."+k, plan[k], actual) for k, actual in binding.items()]):
            raise ValueError("候选或验收已改变，须读取当前证据后重新准备修复。")
        if self.w.reports.get(shot_id):
            from pianchang.creative_sound import legacy_audio_checks
            from pianchang.video_collab_agent import _read
            shot = next(s for s in self.w.project["shots"] if s["id"] == shot_id)
            report = _read(self.w.reports[shot_id]["report_path"])
            if legacy_audio_checks(shot, report).intersection(plan["criterion_ids"]):
                raise ValueError("旧修复依据缺少对应分镜的原声检查，须先重验原素材。")
        for history in self.history:
            if history["repair_id"] != repair_id:
                continue
            paths = history.get("manifest_paths") or []
            from pianchang.video_agent import verified_local_failure
            from pianchang.video_collab_agent import _read
            if (history.get("status") != "local_failure_before_post" or not paths
                    or not all(verified_local_failure(_read(path), path) for path in paths)):
                raise ValueError("这份修复已执行或结果未确认，不得重复提交。")
        return plan

    def _start(self, plan, strategy):
        from pianchang.video_collab_agent import _write
        source = copy.deepcopy(self.w.shots.get(plan["shot_id"]))
        history = {"repair_id": plan["repair_id"], "shot_id": plan["shot_id"], "strategy": strategy,
                   "source_clip": source, "source_report": copy.deepcopy(self.w.reports.get(plan["shot_id"])),
                   "status": "started_or_unknown", "trace_context": current_context()}
        self.history.append(history)
        self._paths()  # Persist before any paid call or tool side effect.
        if source:
            _write(self.w.directory / "candidates" / plan["shot_id"] / (source["media_sha256"] + ".json"), source)
        return history

    @traced("video.repair.preflight", kind="validation", capture_inputs=False)
    def preflight(self, shot_id, repair_id):
        self.require_retake_available(shot_id)
        self.w.require_reference_inputs(shot_id)
        from pianchang.video_agent import compile_shot
        from pianchang.bailian_video import build_request
        from pianchang.reference_transport import prepare_shot_request
        plan = self._plan(shot_id, repair_id)
        self.validate_generation_strategy(shot_id, plan["instruction"])
        compiled = compile_shot(self.w.project, shot_id)
        compiled["prompt"] += "\n本次定向修复（原剧本、角色参考、动作和起止条件全部保留）：\n" + plan["instruction"]
        pins = self.w._required(shot_id)
        config = self.w.video_config_for(shot_id)
        if pins:
            prepared=self.w.reference_inputs(shot_id).read(shot_id)
            if prepared["selected"].get("bundle"):
                compiled["metadata"].update(reference_bundle=prepared["selected"]["bundle"],
                    reference_story_id=self.w.project.get("reference_story_id") or self.w.story_id)
            prepare_shot_request(config, compiled, store=self.w.reference_store,
                story_id=self.w.project.get("reference_story_id") or self.w.story_id, dry_run=True)
        else:
            build_request(config, compiled["prompt"], duration=compiled["duration"])
        return {"valid": True, "new_network_request": False}

    @traced("video.repair.validate_generation_strategy", kind="validation", capture_inputs=False)
    def validate_generation_strategy(self, shot_id, instruction):
        """A failed candidate cannot trigger an identical paid repair strategy."""
        from pianchang.video_collab_agent import _read
        if getattr(self.w,"autonomy",None) and shot_id in self.w.autonomy.blocked:
            raise ValueError("此镜头有未确认的操作，禁止再次付费提交；其他镜头仍可继续。")
        if getattr(self.w,"autonomy",None):
            for attempt in self.w.autonomy.attempts(shot_id):
                if "".join(attempt["instruction"].split())=="".join(instruction.split()):
                    raise ValueError("此方案已在本次或继承候选中实际尝试；包含未被选中的失败候选，不能原样再次提交付费重拍。")
        clip = self.w.shots.get(shot_id)
        if not clip or not clip.get("repair_id"):
            return {"applies": False, "reason": "no previous repaired candidate"}
        manifest = _read(clip["manifest_path"])
        if not _check("repair.strategy.source_manifest", clip["generation_job_sha256"], digest_json(manifest)):
            raise ValueError("上次修复的提交记录不符，不能继续付费重拍。")
        previous = manifest.get("metadata", {}).get("acceptance_repair") or {}
        if not previous.get("instruction"):
            return {"applies": False, "reason": "previous candidate was not produced by prompt repair"}
        previous_refs = sorted(digest_json(p) for p in manifest.get("metadata", {}).get("required_refs", []))
        current_refs = sorted(digest_json(p) for p in self.w._required(shot_id))
        changed_prompt = "".join(instruction.split()) != "".join(previous["instruction"].split())
        changed_assets = previous_refs != current_refs
        changed = changed_prompt or changed_assets
        if not _check("repair.generation_strategy_changed", True, changed):
            raise ValueError("当前候选已经实际尝试过相同修复指令和相同参考，仍未通过验收。"
                             "请改变动作设计、时间分配、调度或资产，或换用本地修改工具；不能原样再次提交付费重拍。")
        return {"applies": True, "changed_prompt": changed_prompt, "changed_assets": changed_assets,
                "previous_repair_id": previous.get("repair_id")}

    def _complete(self, history):
        sid = history["shot_id"]
        self.w.preview = None
        self.w.reports.pop(sid, None)
        self.w.review_shot(sid)
        history.update(status="reviewed", result_clip=copy.deepcopy(self.w.shots[sid]),
                       result_report=copy.deepcopy(self.w.reports[sid]))
        self._paths()
        event("video.repair.reviewed", history, kind="artifact")
        selection=self.assess_candidate(history)
        return {"repair_id": history["repair_id"], "shot_id": sid, "report": self.w.reports[sid],
                "acceptance": self.w.acceptance_gate(),"status":selection["status"],"reason":selection["reason"]}

    @traced("video.repair.assess_candidate",kind="validation",capture_inputs=False)
    def assess_candidate(self, history):
        """Never silently replace already-passed core content with a regressing candidate."""
        from pianchang.video_collab_agent import _read,_write,_validated_media
        self.report_retake_failure(history)
        if not getattr(self.w,"autonomy",None):
            return {"status":"candidate_reviewed","reason":"沿用原非自主任务的候选策略。"}
        source,old_summary=history.get("source_clip"),history.get("source_report")
        result,new_summary=history.get("result_clip"),history.get("result_report")
        if not source or not old_summary or not result or not new_summary:
            return {"status":"candidate_reviewed","reason":"没有可比较的完整前后验收，保留当前实际结果。"}
        sid=history["shot_id"]
        if self.w.shots.get(sid,{}).get("media_sha256")!=result["media_sha256"]:
            return {"status":"candidate_not_current","reason":"该候选不是当前选用版本，未改动选片。"}
        old,new=_read(old_summary["report_path"]),_read(new_summary["report_path"])
        for label,summary,report,clip in [("source",old_summary,old,source),("result",new_summary,new,result)]:
            if digest_json(report)!=summary["report_sha256"] or report["media_sha256"]!=clip["media_sha256"]:
                raise ValueError("候选选择的"+label+"验收绑定不符。")
        previous=set(preserved_passes(source,old_summary,self.w.shots))
        actual=set(preserved_passes(result,new_summary,self.w.shots))
        regressed=previous-actual
        decision={"status":"kept_previous_candidate" if regressed else "candidate_reviewed",
                  "policy_version":SELECTION_POLICY,
                  "reason":"新候选损坏已通过的关键内容，已保留原候选："+"、".join(sorted(regressed)) if regressed else "没有已通过的关键内容退步。",
                  "source_media_sha256":source["media_sha256"],"evaluated_media_sha256":result["media_sha256"],
                  "regressed_criteria":sorted(regressed),"old_report_sha256":old_summary["report_sha256"],
                  "new_report_sha256":new_summary["report_sha256"]}
        event("video.repair.candidate_comparison",decision,kind="validation")
        history["selection"]=decision
        if regressed:
            _validated_media(source,require_origin=True)
            self.w.shots[sid]=copy.deepcopy(source);self.w.reports[sid]=copy.deepcopy(old_summary)
            self.w.preview=None
            _write(self.w.directory/"clips.json",self.w.shots)
            _write(self.w.directory/"reports.json",self.w.reports)
            # Changed selection invalidates adjacent bindings. Existing receipts are hash-checked and reused.
            self.w.review_shot(sid)
            history["status"]="reviewed_rejected_regression"
        self._paths()
        return decision

    @traced("video.repair.recover_best_candidates",kind="validation",capture_inputs=False)
    def recover_best_candidates(self):
        """Recover pre-existing chains too: several worse candidates may have been selected before restart."""
        from pianchang.video_collab_agent import _read,_write,_validated_media
        restored=[]
        def passed(clip,summary):
            report=_read(summary["report_path"])
            if (digest_json(report)!=summary["report_sha256"] or report["media_sha256"]!=clip["media_sha256"]
                    or report["project_sha256"]!=digest_json(self.w.project)):
                raise ValueError("历史候选与当前冻结工程的验收不匹配。")
            return set(preserved_passes(clip,summary,self.w.shots))
        for sid in list(self.w.shots):
            if sid not in self.w.reports:continue
            current=self.w.shots[sid];summary=self.w.reports[sid]
            baseline=passed(current,summary);better=[]
            for h in self.history:
                if h["shot_id"]!=sid:continue
                for clip_key,report_key in [("source_clip","source_report"),("result_clip","result_report")]:
                    clip,report=h.get(clip_key),h.get(report_key)
                    if not clip or not report:continue
                    values=passed(clip,report)
                    if values>baseline:better.append((len(values),clip,report,values))
            if not better:continue
            _,clip,report,values=max(better,key=lambda item:item[0])
            _validated_media(clip,require_origin=True)
            if clip["dependency_sha256"]!=self.w.impact["shots"][sid]["target"]["sha256"]:
                raise ValueError("历史候选的依赖与当前冻结镜头不符。")
            proof={"shot_id":sid,"replaced_media_sha256":current["media_sha256"],"selected_media_sha256":clip["media_sha256"],
                   "old_passed":sorted(baseline),"restored_passed":sorted(values),"source_report_sha256":report["report_sha256"],
                   "failed_reports_retained":True}
            event("video.repair.recovered_candidate_selection",proof,kind="validation")
            self.w.shots[sid]=copy.deepcopy(clip);self.w.reports[sid]=copy.deepcopy(report);self.w.preview=None
            _write(self.w.directory/"clips.json",self.w.shots);_write(self.w.directory/"reports.json",self.w.reports)
            self.w.review_shot(sid)
            restored.append(proof)
        joint=verified_selection_plan(self.w.project,self.w.shots,self.w.reports,self.history)
        if joint["changes"]:
            for sid,value in joint["changes"].items():
                _validated_media(value["clip"],require_origin=True)
                if value["clip"]["dependency_sha256"]!=self.w.impact["shots"][sid]["target"]["sha256"]:
                    raise ValueError("组合候选与冻结镜头依赖不符。")
            proof={"shot_id":"+".join(joint["changes"]),"joint_selection":True,
                   "before":{sid:self.w.shots[sid]["media_sha256"] for sid in joint["changes"]},
                   "after":{sid:value["clip"]["media_sha256"] for sid,value in joint["changes"].items()},
                   "known_pass_gain":joint["known_pass_gain"],"failed_reports_retained":True,"new_video_posts":0}
            event("video.repair.verified_joint_selection",proof,kind="validation")
            for sid,value in joint["changes"].items():
                self.w.shots[sid]=value["clip"];self.w.reports[sid]=value["report"]
            self.w.preview=None
            _write(self.w.directory/"clips.json",self.w.shots);_write(self.w.directory/"reports.json",self.w.reports)
            self.w.review_shot(next(iter(joint["changes"])))
            restored.append(proof)
        return restored

    @traced("video.repair.recover_pending_evaluations",kind="tool",capture_inputs=False)
    def recover_pending_evaluations(self):
        """A generated candidate with technical checks only still needs actual content review."""
        from pianchang.video_collab_agent import _read,_write,_validated_media
        if self.w.vision_config is None or self.w.reference_store is None:return []
        outcomes=[]
        for history in self.history:
            clip,summary=history.get("result_clip"),history.get("result_report")
            if not clip or not summary or history.get("status")=="blocked_evaluation":continue
            report=_read(summary["report_path"])
            if report.get("content_review_version"):continue
            if digest_json(report)!=summary["report_sha256"] or report["media_sha256"]!=clip["media_sha256"]:
                raise ValueError("待恢复的候选验收绑定不符。")
            sid=history["shot_id"]
            before_clips,before_reports=copy.deepcopy(self.w.shots),copy.deepcopy(self.w.reports)
            history.setdefault("prior_evaluations",[]).append({"report":copy.deepcopy(summary),
                "status":history["status"],"selection":copy.deepcopy(history.get("selection"))})
            _validated_media(clip,require_origin=True)
            self.w.shots[sid]=copy.deepcopy(clip);self.w.reports[sid]=copy.deepcopy(summary)
            self.w.defer_content_review=False
            _write(self.w.directory/"clips.json",self.w.shots);_write(self.w.directory/"reports.json",self.w.reports)
            try:
                self.w.review_shot(sid)
                updated=self.w.reports[sid]
                if not _read(updated["report_path"]).get("content_review_version"):
                    raise ValueError("候选仍未得到实际内容验收，不能把技术检查当成内容结论。")
                history.update(status="reviewed",result_report=copy.deepcopy(updated),result_clip=copy.deepcopy(self.w.shots[sid]))
                self._paths()
                selection=self.assess_candidate(history)
                outcomes.append({"shot_id":sid,"status":"missing_content_review_completed",
                    "evaluated_media_sha256":clip["media_sha256"],"report_sha256":updated["report_sha256"],"selection":selection})
            except TraceError:raise
            except Exception as exc:
                self.w.shots,self.w.reports=before_clips,before_reports
                blocker={"reason":"候选内容验收恢复失败："+str(exc),"media_sha256":clip["media_sha256"],
                         "original_technical_report":summary,"kind":"content_evaluation_error"}
                self.w.autonomy.blocked[sid]=blocker
                history.update(status="blocked_evaluation",evaluation_error=blocker)
                _write(self.w.directory/"clips.json",self.w.shots);_write(self.w.directory/"reports.json",self.w.reports)
                outcomes.append({"shot_id":sid,"status":"blocked_evaluation","evidence":blocker})
            self._paths()
        return outcomes

    @traced("video.repair.regenerate", kind="tool", capture_inputs=False)
    def regenerate(self, shot_id, repair_id):
        plan = self._plan(shot_id, repair_id)
        self.require_retake_available(shot_id)
        self.validate_generation_strategy(shot_id, plan["instruction"])
        history = self._start(plan, "prompt_and_locked_references")
        result = self.w.generate(shot_id, repair=plan)
        if result.get("status") in {"FAILED", "CANCELED", "CANCELLED"}:
            history.update(status="provider_failed", failure=result)
            self._paths()
            return result
        # generate() already rechecks the new candidate and affected edges.
        history.update(status="reviewed", result_clip=copy.deepcopy(self.w.shots[shot_id]),
                       result_report=copy.deepcopy(self.w.reports[shot_id]))
        self._paths()
        selection=self.assess_candidate(history)
        return {"shot_id": shot_id, "repair_id": repair_id, "report": self.w.reports[shot_id],
                "acceptance": self.w.acceptance_gate(),"status":selection["status"],"reason":selection["reason"]}

    @traced("video.repair.edit_asset", kind="tool", capture_inputs=False)
    def edit(self, shot_id, repair_id, black_from_s, subtitle):
        from pianchang.acceptance import required_subtitle
        from pianchang.video_collab_agent import _read, _write, _validated_media
        from pianchang.media_tools import _probe, _run, find_ffmpeg, find_ffprobe
        plan = self._plan(shot_id, repair_id)
        required=required_subtitle(next(shot for shot in self.w.project["shots"] if shot["id"]==shot_id))
        # The approved shot can supply sentence-ending punctuation that the raw
        # beat omits. Only that exact bound subtitle is allowed this presentation
        # difference; its words must still occur verbatim in the frozen script.
        approved_terminal=(required and subtitle==required["expected_text"] and subtitle.endswith("。")
                           and subtitle[:-1] and subtitle[:-1] in str(self.w.script))
        if not math.isfinite(black_from_s) or (subtitle and subtitle not in str(self.w.script) and not approved_terminal):
            raise ValueError("字幕文字须来自固定剧本，句末标点只允许已批准镜头中的原样形式，切黑时间须有效。")
        if required and subtitle!=required["expected_text"]:
            raise ValueError("本镜头的必过字幕必须完整烧录，不能省略文字或标点："+required["expected_text"])
        if self.retake_spent(shot_id) or (self.w.shooting_plan or {}).get("acceptance_loop"):
            raise ValueError("本次自动验收最多重拍一次，不追加本地修改和复验。")
        limit = 2
        if self.local_edits.get(shot_id, 0) >= limit:
            raise ValueError("本镜头本地修复次数已到上限，任务仍未完成。")
        source = self.w.shots[shot_id]
        _validated_media(source, require_origin=True)
        operation={"name":"black_tail","start_s":black_from_s,"subtitle":subtitle}
        for prior in self.history:
            if (prior["shot_id"]!=shot_id or prior["strategy"]!="local_black_tail" or
                    (prior.get("source_clip") or {}).get("media_sha256")!=source["media_sha256"]):continue
            reference=(prior.get("result_clip") or {}).get("media_lineage")
            if not reference:continue
            lineage=_read(reference["path"])
            if digest_json(lineage)!=reference["sha256"]:raise ValueError("历史本地修改谱系已改变。")
            if lineage["operation"]==operation:
                event("video.repair.repeated_local_strategy",{"path":"local_edit.operation_changed","expected":True,
                    "actual":False,"status":"FAIL","prior_repair_id":prior["repair_id"],"operation":operation},kind="validation")
                raise ValueError("同一素材已实际尝试相同切黑时间和字幕，不能原样重复；须结合失败证据改变本地修改策略。")
        probe = _probe(find_ffprobe(), ["-show_streams", "-show_format", source["media_path"]])
        if not 0 < black_from_s < float(probe["format"]["duration"]):
            raise ValueError("切黑时间必须位于实际镜头时长内。")
        font = _subtitle_font() if subtitle else None
        history = self._start(plan, "local_black_tail")
        self.local_edits[shot_id] = self.local_edits.get(shot_id, 0) + 1
        folder = self.w.directory / "edited-candidates" / repair_id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "video.mp4"
        vf = f"drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='gte(t,{black_from_s})'"
        if font is not None:
            text = folder / "subtitle.txt"
            text.write_text(subtitle, encoding="utf-8")
            escape = lambda p: str(p.resolve()).replace("\\", "/").replace(":", "\\:")
            vf += (f",drawtext=fontfile='{escape(font)}':textfile='{escape(text)}':expansion=none:"
                   f"fontcolor=white:fontsize=h/35:x=(w-tw)/2:y=(h-th)/2:enable='gte(t,{black_from_s})'")
        argv = [find_ffmpeg(), "-nostdin", "-hide_banner", "-v", "error", "-n", "-i", source["media_path"],
                "-vf", vf, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-crf", "18", "-c:a", "copy", target]
        result = _run(argv, timeout=180)
        if result.returncode:
            raise ValueError("本地视频修改失败：" + result.stderr[-1500:])
        lineage = {"schema_version": "video-media-lineage/1", "source_clip": copy.deepcopy(source),
                   "operation": {"name": "black_tail", "start_s": black_from_s, "subtitle": subtitle},
                   "output_path": str(target), "output_sha256": file_sha256(target),
                   "argv": list(map(str, argv)), "trace_context": current_context(), "repair_id": repair_id}
        receipt = folder / "lineage.json"
        _write(receipt, lineage)
        clip = {**source, "operation": "edited", "media_path": str(target), "media_sha256": lineage["output_sha256"],
                "media_lineage": {"path": str(receipt), "sha256": digest_json(_read(receipt))}, "repair_id": repair_id}
        _validated_media(clip, require_origin=True)
        self.w.shots[shot_id] = clip
        _write(self.w.directory / "clips.json", self.w.shots)
        return self._complete(history)


@traced("video.repair.validate_lineage", kind="validation", capture_inputs=False)
def validate_lineage(lineage_ref, *, output_path, output_sha256, job=None, depth=0):
    from pianchang.video_collab_agent import _read, _validated_media
    if depth > 6:
        raise ValueError("素材修改来源链超过上限。")
    lineage = _read(lineage_ref["path"])
    source = lineage["source_clip"]
    checks = {"receipt_hash": digest_json(lineage) == lineage_ref["sha256"],
              "schema": lineage.get("schema_version") == "video-media-lineage/1",
              "output_path": Path(lineage["output_path"]).resolve() == Path(output_path).resolve(),
              "output_hash": lineage["output_sha256"] == output_sha256 == file_sha256(output_path),
              "operation": lineage.get("operation", {}).get("name") == "black_tail"}
    event("video.repair.lineage_checked", {"checks": [{"path": k, "expected": True, "actual": v,
          "status": "PASS" if v else "FAIL"} for k, v in checks.items()]}, kind="validation")
    if not all(checks.values()):
        raise ValueError("修改后的素材与来源记录不符。")
    if source.get("media_lineage"):
        validate_lineage(source["media_lineage"], output_path=source["media_path"], output_sha256=source["media_sha256"], depth=depth+1)
    else:
        _validated_media(source, require_origin=True)
    origin = _read(source["manifest_path"])
    if job is not None and digest_json(job) != digest_json(origin):
        raise ValueError("修改来源与生成任务不符。")
    return origin
