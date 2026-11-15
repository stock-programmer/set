"""Version-bound shot acceptance: technical checks, sampled evidence, and gaps.

PASS is scoped to the stated criterion. Sparse images never prove continuous
action, audio, reference identity, or neighboring-shot continuity.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from pathlib import Path

from pianchang.bailian_text import TextError
from pianchang.production import validate
from pianchang.story_spec import digest_json, validate_compilation
from trace_system import traced


class AcceptanceError(ValueError):
    pass


@traced("acceptance.required_subtitle",kind="validation")
def required_subtitle(shot):
    match=re.search(r"字幕(?:浮现|出现|显示)?\s*[:：]\s*(.+)",shot.get("description",""))
    if not match:return None
    text=match.group(1).strip()
    if text[:1] in {'“','「','"'}:
        closing={'“':'”','「':'」','"':'"'}[text[0]]
        end=text.find(closing,1)
        if end<0:raise AcceptanceError("字幕原文的引号未闭合，不能猜测待渲染文字。")
        text=text[1:end]
    if not text or len(text)>160 or '\n' in text:
        raise AcceptanceError("字幕原文不明确或超出本地排版范围，须保留为具体规格阻碍。")
    return {"criterion_id":"script_subtitle","kind":"on_screen_text","required":True,
            "expected_text":text,"expectation":"实际成片必须显示剧本原文字幕，逐字核对，不能以留待后期为由豁免："+text}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if digest_json(_read(path)) == digest_json(value):
            return
        raise AcceptanceError(f"输出文件已存在且内容不同，请使用新路径保留旧证据：{path}")
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _validated_project(project):
    report = validate_compilation(project) if "script_source" in project else validate(project)
    if not report.get("ready_for_generation", report.get("spec_pass", False)):
        raise AcceptanceError("工程规格/来源检查未通过，不能据此建立或通过验收。")
    return report


@traced("acceptance.regression_plan", kind="validation")
def regression_plan(project, shot_id):
    """Conservative review scope; it never claims that these checks were run."""
    _validated_project(project)
    shots = project["shots"]
    if shot_id not in {s["id"] for s in shots}:
        raise AcceptanceError(f"未知镜头：{shot_id}")
    affected = {shot_id}
    while True:
        following = {s["id"] for s in shots if set(s["continuity_from"]) & affected}
        if following <= affected:
            break
        affected |= following
    edges = []
    for shot in shots:
        for previous in shot["continuity_from"]:
            if previous in affected or shot["id"] in affected:
                edges.append([previous, shot["id"]])
    context_ids = affected | {s for edge in edges for s in edge}
    return {"changed_shot_id": shot_id,
            "shot_checks": [s["id"] for s in shots if s["id"] in affected],
            "context_shot_ids": [s["id"] for s in shots if s["id"] in context_ids],
            "continuity_edges": edges,
            "basis": "declared continuity_from; no edited timeline exists yet",
            "checks_executed": False}


@traced("acceptance.create_contract", kind="tool")
def create_contract(project, shot_id, *, width=720, height=1280):
    report = _validated_project(project)
    shot = next((s for s in project["shots"] if s["id"] == shot_id), None)
    if shot is None:
        raise AcceptanceError(f"未知镜头：{shot_id}")
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise AcceptanceError("目标分辨率必须为正整数。")
    trace = next(s for s in report["state_trace"] if s["shot_id"] == shot_id)
    entities = {eid: project["entities"][eid] for eid in [shot["scene_id"], *shot["entity_ids"]]}
    def criterion(cid, kind, expectation, *, required=True, **extra):
        return {"criterion_id": cid, "kind": kind, "required": required,
                "expectation": expectation, **extra}
    props = {eid: e["canonical"] for eid, e in entities.items() if e["kind"] == "prop"}
    costumes = {eid: e["canonical"] for eid, e in entities.items() if e["kind"] == "costume"}
    requirements = [
        criterion("file_decode", "technical", "整个视频文件可严格解码", check="decode"),
        criterion("duration", "technical", "时长符合固定目标", check="duration",
                  expected=shot["duration_s"], tolerance_s=0.25),
        criterion("resolution", "technical", "分辨率符合固定目标", check="resolution", width=width, height=height),
        criterion("character_identity", "reference_comparison", "角色与已批准参考一致；文字外貌不能替代身份参考"),
        criterion("scene_geometry", "reference_and_camera", "场景布局与参考、世界坐标及机位一致"),
        criterion("action_sequence", "continuous_video", "连续画面确实表现镜头动作，不能仅凭首尾推断：" + shot["description"]),
        criterion("start_end_state", "continuous_video", "实际开始与结束状态符合预期，且能确认相关角色与道具"),
    ]
    subtitle=required_subtitle(shot)
    if subtitle:requirements.append(subtitle)
    if shot.get("required_refs"):
        requirements.append(criterion(
            "reference_submission", "reference_submission",
            "本镜头固定的参考版本出现在实际提交的请求中，供应商任务响应与被检查的媒体文件相符。",
            required_refs=copy.deepcopy(shot["required_refs"])))
    if props:
        requirements.append(criterion("sampled_prop_appearance", "sampled_visual",
                            "只判断引用帧中可见道具的外观是否符合文字设定，遮挡/看不清则UNKNOWN：" + json.dumps(props, ensure_ascii=False)))
    if costumes:
        requirements.append(criterion("sampled_costume_appearance", "sampled_visual",
                            "只判断引用帧中可见服装是否符合文字设定，不能据此判断全程不变：" + json.dumps(costumes, ensure_ascii=False)))
    plan = regression_plan(project, shot_id)
    for before, after in plan["continuity_edges"]:
        requirements.append(criterion(f"continuity:{before}->{after}", "sequence_review",
                                      f"复查 {before} 与 {after} 的动作、道具、场景和声音衔接",
                                      shot_ids=[before, after]))
    return {"schema_version": "shot-acceptance/1", "project_sha256": digest_json(project),
            "shot_id": shot_id, "shot_sha256": digest_json(shot),
            "scope": "shot_and_declared_regression_dependencies",
            "input_versions": shot["input_versions"],
            "entity_snapshots": entities,
            "expected_state_before": trace["before"], "expected_state_after": trace["after"],
            "requirements": requirements, "regression": plan,
            "creative_review": "UNKNOWN", "audio_content_review": "UNKNOWN"}


def validate_contract(contract):
    if not isinstance(contract, dict) or contract.get("schema_version") != "shot-acceptance/1":
        raise AcceptanceError("不支持的验收契约。")
    requirements = contract.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise AcceptanceError("验收条件不能为空。")
    seen = set()
    for item in requirements:
        if not isinstance(item, dict):
            raise AcceptanceError("验收条件必须为对象。")
        cid = item.get("criterion_id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise AcceptanceError("验收条件 ID 必须非空且唯一。")
        seen.add(cid)
        if type(item.get("required")) is not bool or not isinstance(item.get("kind"), str):
            raise AcceptanceError("验收条件需要明确 required 和 kind。")
        if not isinstance(item.get("expectation"), str) or not item["expectation"].strip():
            raise AcceptanceError("验收预期不能为空。")
        if item.get("kind") == "technical" and item.get("check") == "duration":
            for field in ("expected", "tolerance_s"):
                value = item.get(field)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise AcceptanceError("时长和容差须为有限非负数。")
    if not any(item["required"] for item in requirements):
        raise AcceptanceError("至少需要一个必过验收项。")


def verify_inspection(inspection):
    if not isinstance(inspection, dict) or inspection.get("schema_version") != "media-inspection/1":
        raise AcceptanceError("不支持的媒体观察清单。")
    if file_sha256(inspection["media_path"]) != inspection["media_sha256"]:
        raise AcceptanceError("实际视频与观察记录的哈希不符，旧证据失效。")
    frames = inspection.get("frames")
    if not isinstance(frames, list):
        raise AcceptanceError("抽帧清单无效。")
    ids = set()
    last_time = -1.0
    for frame in frames:
        if not isinstance(frame, dict):
            raise AcceptanceError("帧记录必须为对象。")
        fid, moment = frame.get("id"), frame.get("time_s")
        if not isinstance(fid, str) or fid in ids:
            raise AcceptanceError("帧 ID 无效或重复。")
        if type(moment) not in (int, float) or not math.isfinite(moment) or moment < last_time or moment < 0:
            raise AcceptanceError("帧时间戳无效或乱序。")
        duration = inspection.get("metadata", {}).get("duration_s")
        if type(duration) in (int, float) and moment > duration + 0.001:
            raise AcceptanceError("帧时间戳超出视频范围。")
        if file_sha256(frame["path"]) != frame["sha256"]:
            raise AcceptanceError(f"帧 {fid} 被替换，观察证据失效。")
        ids.add(fid)
        last_time = moment
    return frames


@traced("acceptance.observe", kind="tool")
def observe(inspection, contract, *, config=None, client=None):
    """Store a model's sampled observations; this function never accepts a shot."""
    from pianchang.bailian_observer import BailianObserver, load_observer_config
    validate_contract(contract)
    frames = verify_inspection(inspection)
    if not frames:
        raise AcceptanceError("没有可观察的图片帧。")
    config = config or load_observer_config()
    client = client or BailianObserver(config)
    criteria = [r for r in contract["requirements"] if r["kind"] == "sampled_visual"]
    if not criteria:
        raise AcceptanceError("契约没有可由静帧模型辅助观察的项目。")
    result = client.complete_observations(frames, criteria)
    return {"schema_version": "sampled-observations/1", "method": "vl_on_timestamped_still_frames",
            "contract_sha256": digest_json(contract), "inspection_sha256": digest_json(inspection),
            "media_sha256": inspection["media_sha256"], "capabilities": ["sampled_visual"],
            "response": result,
            "limitations": ["No continuous motion, audio or calibrated identity verification.",
                            "Model observations may be wrong; PASS is scoped to cited still frames."]}


@traced("acceptance.reference_submission", kind="validation", capture_inputs=False)
def _reference_result(project, contract, requirement, inspection, job, reference_job, media_lineage=None):
    from pianchang.video_agent import verify_reference_receipt
    from pianchang.reference_usage import verify_reference_exchange

    source = reference_job if reference_job is not None else job
    evidence = []
    def compare(path, expected, actual, *, inconclusive=False):
        status = "UNKNOWN" if actual is None or inconclusive else ("PASS" if actual == expected else "FAIL")
        evidence.append({"path": path, "expected": expected, "actual": actual, "status": status})

    if not isinstance(source, dict):
        compare("origin_job", "bailian-video-job/1", None if source is None else type(source).__name__)
    else:
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        pins = requirement["required_refs"]
        compare("origin_job.schema_version", True, source.get("schema_version") in {"bailian-video-job/1", "provider-video-job/2"})
        compare("origin_job.shot_id", contract["shot_id"], metadata.get("shot_id"))
        if media_lineage:
            from pianchang.video_repair import validate_lineage
            validate_lineage(media_lineage, output_path=inspection["media_path"], output_sha256=inspection["media_sha256"], job=source)
            compare("origin_job.derived_media_lineage", True, True)
            evidence.append({"media_lineage": media_lineage, "original_download_sha256": source.get("download_sha256")})
        else:
            compare("origin_job.media_sha256", inspection["media_sha256"], source.get("download_sha256"))
        compare("origin_job.required_refs", pins, metadata.get("required_refs"))
        if project.get("reference_story_id") is not None:
            compare("origin_job.reference_story_id", project["reference_story_id"], metadata.get("reference_story_id"))
        if job is not None:
            compare("origin_job.project_sha256", contract["project_sha256"], metadata.get("project_sha256"))
            compare("origin_job.input_versions", contract["input_versions"], metadata.get("input_versions"))
            if isinstance(job, dict):
                compare("origin_job.same_candidate_request", job.get("request_sha256"), source.get("request_sha256"))
                compare("origin_job.same_candidate_task", job.get("task_id"), source.get("task_id"))
        state = source.get("status")
        compare("origin_job.status", "SUCCEEDED", state, inconclusive=state in {
            "SUBMISSION_UNKNOWN", "SUBMITTING", "PENDING", "RUNNING", "UNKNOWN"})
        receipt = source.get("reference_receipt")
        if receipt is None or receipt == {}:
            compare("reference_receipt", "accepted_request_receipt", None)
        else:
            validation = verify_reference_receipt(source)
            evidence.extend(validation["checks"])
            if validation["status"] == "PASS":
                status = receipt["status"]
                compare("reference_receipt.provider_status", "ACCEPTED", status,
                        inconclusive=status in {"PREPARED","SUBMISSION_UNKNOWN"})
                if status == "ACCEPTED":
                    exchange = verify_reference_exchange(source, receipt, receipt["trace_id"])
                    wire = exchange["status"]
                    compare("reference_receipt.http_exchange", "MATCHED_RESPONSE", wire,
                            inconclusive=wire not in {"MATCHED_RESPONSE","CONFLICT"})
                    evidence.append({"trace_id": receipt["trace_id"], "task_id": receipt["task_id"],
                                     "wire_body_sha256": receipt["wire_body_sha256"], "exchange": exchange,
                                     "submitted_refs": receipt["submitted_refs"]})
    statuses = [item.get("status") for item in evidence]
    verdict = "FAIL" if "FAIL" in statuses else ("UNKNOWN" if "UNKNOWN" in statuses else "PASS")
    reasons = {"PASS":"已核对实际请求中的参考版本、供应商任务响应与视频文件。",
               "FAIL":"目标镜头、实际请求回执或媒体绑定存在冲突。",
               "UNKNOWN":"缺少完整的提交、供应商确认或候选绑定证据。"}
    return {"status": verdict, "reason": reasons[verdict], "evidence": evidence}


@traced("acceptance.run_checks", kind="validation")
def run_checks(project, contract, inspection, observations=None, *, job=None, reference_job=None, content_evidence=None, media_lineage=None):
    """Evaluate every frozen criterion; missing/insufficient evidence stays UNKNOWN."""
    validate_contract(contract)
    if not isinstance(inspection, dict):
        raise AcceptanceError("媒体观察清单必须为对象。")
    integrity = []
    try:
        _validated_project(project)
        if digest_json(project) != contract["project_sha256"]:
            integrity.append("工程版本已变化，契约需要重新建立。")
        shot = next((s for s in project["shots"] if s["id"] == contract["shot_id"]), None)
        if shot is None or digest_json(shot) != contract["shot_sha256"]:
            integrity.append("镜头规格与契约不匹配。")
        # This policy has mandatory content/regression gates. Deleting them or
        # changing required/kind cannot turn an incomplete shot into a success.
        by_id = {r["criterion_id"]: r for r in contract["requirements"]}
        resolution = by_id.get("resolution", {})
        baseline = create_contract(project, contract["shot_id"],
                                   width=resolution.get("width", 720), height=resolution.get("height", 1280))
        for requirement in baseline["requirements"]:
            if digest_json(by_id.get(requirement["criterion_id"])) != digest_json(requirement):
                integrity.append(f"固定验收项被删除或降低标准：{requirement['criterion_id']}。")
        for field in ("scope", "input_versions", "entity_snapshots", "expected_state_before",
                      "expected_state_after", "regression"):
            if digest_json(contract.get(field)) != digest_json(baseline[field]):
                integrity.append(f"契约的 {field} 与当前工程/验收范围不匹配。")
        frames = verify_inspection(inspection)
    except (AcceptanceError, OSError, KeyError, TypeError, ValueError) as exc:
        integrity.append(str(exc))
        frames = []
    if observations is not None:
        if not isinstance(observations, dict) or any((
            observations.get("schema_version") != "sampled-observations/1",
            observations.get("contract_sha256") != digest_json(contract),
            observations.get("inspection_sha256") != digest_json(inspection),
            observations.get("media_sha256") != inspection.get("media_sha256"),
            observations.get("method") != "vl_on_timestamped_still_frames",
            observations.get("capabilities") != ["sampled_visual"],
        )):
            integrity.append("模型观察来自其他契约、素材或抽帧版本，不能复用。")
    content_results = {}
    if content_evidence is not None and not integrity:
        try:
            from pianchang.content_review import validate_content_evidence
            content_results = validate_content_evidence(project, contract, inspection, content_evidence)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            integrity.append("内容验收证据不完整或不匹配：" + str(exc))
    candidate_status = "UNKNOWN"
    candidate_reason = "未提供生成任务记录，尚不能确认该素材属于目标镜头。"
    if job is not None:
        try:
            identity = {k: job[k] for k in ("base_url", "request", "metadata")}
            media_match = job.get("download_sha256") == inspection.get("media_sha256")
            if media_lineage:
                from pianchang.video_repair import validate_lineage
                validate_lineage(media_lineage, output_path=inspection["media_path"], output_sha256=inspection["media_sha256"], job=job)
                media_match = True
            valid = (job.get("schema_version") in {"bailian-video-job/1", "provider-video-job/2"}
                     and job.get("status") == "SUCCEEDED"
                     and job.get("request_sha256") == digest_json(identity)
                     and media_match
                     and job["metadata"].get("project_sha256") == contract["project_sha256"]
                     and job["metadata"].get("shot_id") == contract["shot_id"]
                     and job["metadata"].get("input_versions") == contract["input_versions"])
            candidate_status = "PASS" if valid else "FAIL"
            candidate_reason = "已绑定生成任务、工程版本、镜头输入和下载素材哈希。" if valid else "生成任务与当前工程、镜头或实际视频不匹配。"
        except (KeyError, TypeError, AttributeError):
            candidate_status, candidate_reason = "FAIL", "生成任务记录无效。"
    results = [{"criterion_id": "evidence_integrity", "required": True,
                "status": "FAIL" if integrity else "PASS", "method": "sha256_and_spec",
                "reason": "；".join(integrity) if integrity else "工程、素材及证据文件哈希已核对。", "evidence": []}]
    results.append({"criterion_id": "candidate_binding", "required": True, "status": candidate_status,
                    "method": "generation_job", "reason": candidate_reason,
                    "evidence": [{"task_id": job.get("task_id")}] if isinstance(job, dict) else []})
    candidates = {}
    malformed_observations = False
    if observations is not None and not integrity:
        try:
            items = observations["response"]["data"]["observations"]
            if not isinstance(items, list):
                raise ValueError("observations must be a list")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("criterion_id"), str):
                    raise ValueError("invalid observation")
                candidates.setdefault(item["criterion_id"], []).append(item)
        except (KeyError, TypeError, ValueError):
            malformed_observations = True
    frame_map = {f["id"]: f for f in frames}
    metadata = inspection.get("metadata", {})
    for requirement in contract["requirements"]:
        cid, kind = requirement["criterion_id"], requirement["kind"]
        result = {"criterion_id": cid, "required": requirement["required"], "status": "UNKNOWN",
                  "expectation": requirement["expectation"], "method": kind, "reason": "缺少所需证据。", "evidence": []}
        if integrity:
            result["reason"] = "证据完整性检查失败，不能接受已有判断。"
        elif kind == "reference_submission":
            result.update(_reference_result(project, contract, requirement, inspection, job, reference_job, media_lineage))
        elif kind == "technical":
            check = requirement.get("check")
            if check == "decode":
                decode = inspection.get("decode", {})
                result["status"] = decode.get("status") if decode.get("status") in {"PASS", "FAIL"} else "UNKNOWN"
                result["reason"] = decode.get("error") or "完整解码结果；不代表内容已经审看。"
                result["evidence"] = [{"media_sha256": inspection["media_sha256"], "decode": decode}]
            elif check == "duration":
                actual = metadata.get("duration_s")
                if type(actual) in (int, float) and math.isfinite(actual):
                    result["status"] = "PASS" if abs(actual - requirement["expected"]) <= requirement["tolerance_s"] else "FAIL"
                    result["reason"] = f"实际 {actual} 秒，预期 {requirement['expected']} 秒，容差 {requirement['tolerance_s']} 秒。"
                    result["evidence"] = [{"actual_duration_s": actual}]
            elif check == "resolution":
                if metadata.get("width") and metadata.get("height"):
                    result["status"] = "PASS" if (metadata["width"], metadata["height"]) == (requirement.get("width"), requirement.get("height")) else "FAIL"
                    result["reason"] = f"实际分辨率 {metadata['width']}×{metadata['height']}。"
                    result["evidence"] = [{"width": metadata["width"], "height": metadata["height"]}]
        elif cid in content_results:
            result.update(content_results[cid])
        elif kind == "sampled_visual":
            items = candidates.get(cid, [])
            if malformed_observations or len(items) != 1:
                result["reason"] = "缺少唯一、有效的该项模型观察。"
            else:
                item = items[0]
                ids = item.get("frame_ids")
                if (item.get("verdict") not in {"PASS", "FAIL", "UNKNOWN"}
                        or not isinstance(ids, list) or not ids
                        or any(not isinstance(fid, str) or fid not in frame_map for fid in ids)
                        or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
                    result["reason"] = "观察缺少有效帧引用或判断依据。"
                else:
                    result.update(status=item["verdict"], reason=item["reason"],
                                  evidence=[{k: frame_map[fid][k] for k in ("id", "time_s", "path", "sha256")} for fid in ids],
                                  review_scope="cited_still_frames_only", creative_review="UNKNOWN")
        else:
            reasons = {
                "reference_comparison": "尚无已批准角色参考及校准后的身份比较证据；文字外貌相似不等于同一角色。",
                "reference_and_camera": "缺少场景参考与机位验证，静帧不能确认世界坐标布局。",
                "continuous_video": "稀疏帧不能证明连续动作或完整起止过程，需要连续片段观察。",
                "sequence_review": "尚无绑定前后镜头候选的组合预览与衔接审看证据。",
                "audio_content": "尚无真实音轨转写、说话人和时序证据。",
                "on_screen_text": "待检查实际视频中的字幕并逐字比较剧本原文，尚未取得文字观察。",
            }
            if "reference_submission" in by_id:
                reasons["reference_comparison"] = "需要比较指定参考与成片中的实际角色身份。"
                reasons["reference_and_camera"] = "需要结合实际画面、参考场景及机位验证空间布局。"
            result["reason"] = reasons.get(kind, "该验收能力尚未实现，不能默认通过。")
            result["execution_status"] = "not_run"
            result["reason"] = "此份报告未执行对应内容检查。" + result["reason"]
        results.append(result)
    required = [r for r in results if r["required"]]
    status = "FAIL" if any(r["status"] == "FAIL" for r in required) else (
        "UNKNOWN" if any(r["status"] != "PASS" for r in required) else "PASS")
    return {"schema_version": "shot-acceptance-report/1", "scope": contract["scope"],
            "shot_id": contract["shot_id"], "status": status, "task_complete": status == "PASS",
            "project_sha256": digest_json(project), "contract_sha256": digest_json(contract),
            "inspection_sha256": digest_json(inspection), "media_sha256": inspection.get("media_sha256"),
            "observations_sha256": digest_json(observations) if observations is not None else None,
            "generation_job_sha256": digest_json(job) if job is not None else None,
            **({"media_lineage": media_lineage} if media_lineage else {}),
            **({"reference_job_sha256": digest_json(reference_job)} if reference_job is not None
               and any(r["criterion_id"] == "reference_submission" for r in contract["requirements"]) else {}),
            "results": results, "regression": contract["regression"],
            "creative_review": "UNKNOWN", "audio_content_review": "UNKNOWN",
            "policy": "Any required FAIL or UNKNOWN blocks completion; stopping is not acceptance."}


@traced("cli.acceptance", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("contract", "regression"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--project", type=Path, required=True)
        cmd.add_argument("--shot", required=True)
        if command == "contract":
            cmd.add_argument("--output", type=Path, required=True)
            cmd.add_argument("--width", type=int, default=720)
            cmd.add_argument("--height", type=int, default=1280)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("media", type=Path)
    inspect.add_argument("--output", type=Path, required=True)
    inspect.add_argument("--interval", type=float, default=1.0)
    inspect.add_argument("--max-frames", type=int, default=24)
    for command in ("observe", "check"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--contract", type=Path, required=True)
        cmd.add_argument("--inspection", type=Path, required=True)
        cmd.add_argument("--output", type=Path, required=True)
        if command == "check":
            cmd.add_argument("--project", type=Path, required=True)
            cmd.add_argument("--observations", type=Path)
            cmd.add_argument("--job", type=Path, help="生成任务 job.json，用于绑定目标镜头和素材")
            cmd.add_argument("--reference-job", type=Path, help="复用候选的原始生成任务，用于核实最初参考提交")
    args = parser.parse_args(argv)
    try:
        if args.command == "contract":
            result = create_contract(_read(args.project), args.shot, width=args.width, height=args.height)
        elif args.command == "regression":
            result = regression_plan(_read(args.project), args.shot)
        elif args.command == "inspect":
            from pianchang.media_tools import MediaError, inspect_media
            try:
                result = inspect_media(args.media, args.output, interval_s=args.interval, max_frames=args.max_frames)
            except MediaError as exc:
                raise AcceptanceError(str(exc)) from None
        elif args.command == "observe":
            if args.output.exists():
                raise AcceptanceError("观察输出已存在；请复用已有观察或选择新路径，避免重复计费。")
            result = observe(_read(args.inspection), _read(args.contract))
        else:
            result = run_checks(_read(args.project), _read(args.contract), _read(args.inspection),
                                _read(args.observations) if args.observations else None,
                                job=_read(args.job) if args.job else None,
                                reference_job=_read(args.reference_job) if args.reference_job else None)
        if args.command not in {"inspect", "regression"}:
            _write_new(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "check":
            return {"PASS": 0, "FAIL": 1, "UNKNOWN": 3}[result["status"]]
        return 0
    except (TextError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
