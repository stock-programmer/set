"""Recheck old picture-only beat verdicts against their own frozen film and soundtrack."""
import re
from pathlib import Path

from pianchang.acceptance import file_sha256
from pianchang.content_review import (NORMALIZATION_VERSION, STATE_AUDIT_VERSION, VERSION, _asset, _read, _write,
                                      publish_content_report, review_creative_sound, unaffected_observations, validate_content_evidence)
from pianchang.creative_sound import POLICY, legacy_audio_checks, review_scope
from pianchang.story_spec import digest_json
from trace_system import event, traced


def sidecar_commission(root, pid, cid, original_job):
    """The watched original job and the accepted native continuation identify this commission."""
    if not root:
        return None
    folder = Path(root) / "outputs/workbench-loop"
    watch = folder / "watch/state.json"
    if not watch.is_file():
        return None
    state = _read(watch)
    record = state.get("events", {}).get(state.get("latest", {}).get(original_job), {})
    run_id = record.get("run_id")
    if (record.get("job_id") != original_job or record.get("kind") != "studio_video"
            or not re.fullmatch("[0-9a-f]{32}", str(run_id))):
        return None
    path = folder / run_id / "run.json"
    run = _read(path) if path.is_file() else {}
    if (run.get("id") == run_id and run.get("role") == "video" and run.get("project_id") == pid
            and run.get("commission_id") == cid and run.get("origin_job_id") == original_job
            and original_job in run.get("submitted", [])):
        return run
    return None


def _current_edges(work, reports, clips, project):
    edges = {}
    shots = {s["id"]:s for s in project["shots"]}
    order = list(shots)
    pairs = set(zip(order,order[1:]))
    for _, refs, _ in reports.values():
        for ref in refs:
            path = Path(ref["path"]).resolve()
            if not path.is_relative_to((work/"cv2").resolve()):
                continue
            data = _read(path)
            context = data.get("context") or {}
            pair = (context.get("left"),context.get("right"))
            if pair not in pairs:
                continue
            if digest_json(data) != ref["sha256"]:
                raise ValueError("原衔接回执已变更。")
            segments = context.get("source_segments") or []
            sources = {s["shot_id"]:s["source"] for s in segments}
            if ([s["shot_id"] for s in segments] != list(pair)
                    or context.get("left_shot") != shots[pair[0]] or context.get("right_shot") != shots[pair[1]]
                    or (context.get("cut_review_scope") or {}).get("source_segments") != segments
                    or any(s not in clips or sources[s]["sha256"] != clips[s]["media_sha256"]
                    or Path(sources[s]["path"]).resolve() != Path(clips[s]["media_path"]).resolve() for s in pair)):
                continue
            descriptor,preview = context["export_manifest"],context["preview"]
            manifest = _read(descriptor["path"])
            if (file_sha256(descriptor["path"]) != descriptor["sha256"] or manifest["preview_sha256"] != preview["sha256"]
                    or preview["sha256"] != data["video"]["binding"]["source"]["sha256"]
                    or [s["shot_id"] for s in manifest["clips"]] != list(pair)
                    or context["cut_s"] != manifest["clips"][0]["preview_duration_s"]
                    or any(c["sha256"] != s["segment"]["sha256"] for c,s in zip(manifest["clips"],segments))):
                continue
            cid = "continuity:" + "->".join(pair)
            if len(data["rows"]) == 1 and data["rows"][0]["criterion_id"] == cid:
                edges.setdefault(cid,{})[ref["sha256"]] = ref
    return edges


def _replace_stale_edges(evidence, clips, current):
    replacements = {}
    stale = {s for s,v in evidence.get("selected_media",{}).items() if s in clips
             and (v["sha256"] != clips[s]["media_sha256"] or Path(v["path"]).resolve() != Path(clips[s]["media_path"]).resolve())}
    if not stale:
        return replacements
    for row in evidence["rows"]:
        cid = row["criterion_id"]
        if cid.startswith("continuity:") and stale.intersection(cid[len("continuity:"):].split("->")):
            refs = current.get(cid,{})
            if len(refs) != 1:
                return None
            replacements[cid] = next(iter(refs.values()))
    return replacements


def _completed_sound_receipt(work, timing, timing_ref, scopes, routes):
    def route(request):
        return [request.get("model"), request.get("endpoint"), request.get("account","")]

    matches = []
    for path in (work/"creative-sound").glob("*/receipt-p4.json"):
        result = _read(path)
        context = result.get("context") or {}
        call_path = path.with_name("call.json")
        if (result.get("execution_status") != "executed" or result.get("normalization_version") != NORMALIZATION_VERSION
                or result.get("video") != timing["video"] or context.get("visual_timing_receipt") != timing_ref
                or context.get("creative_audio_scope") != scopes or not result.get("call_path")
                or Path(result["call_path"]).resolve() != call_path.resolve() or not call_path.is_file()):
            continue
        call = _read(call_path)
        request = call.get("request") or {}
        if (call.get("status") == "completed" and digest_json(call) == result.get("call_sha256")
                and call.get("response") == result.get("response") and call.get("input_sha256") == digest_json(request)
                and result.get("request_sha256") == call["input_sha256"] and path.parent.name == call["input_sha256"][:12]
                and [route(request),route(request.get("audio_review") or {})] == routes
                and {r["criterion_id"] for r in result["rows"] if r.get("creative_audio_policy") == POLICY} == set(scopes)):
            matches.append({"path":str(path.resolve()),"sha256":digest_json(result)})
    if len(matches) > 1:
        raise ValueError("当前候选有多个未区分的视听回执，保留原报告。")
    return next(iter(matches),None)


def legacy_sound_scope(service, view, snapshot, owner, work, reports, remaining, *, excluding=None):
    from pianchang.shot_approval import active_production
    if (not snapshot.get("retake_stop") or not view.get("script_ref") or not view.get("spec")
            or view.get("pending_script_ref") or snapshot.get("unknown_shots")
            or snapshot.get("failed_shots") or view.get("script_ref") != snapshot.get("script_ref")
            or view.get("spec") != snapshot.get("spec_ref")):
        return None
    cid, pid = snapshot["request_id"], view["project_id"]
    with service.lock:
        if active_production([j for j in service.state["jobs"] if j["id"] != excluding], pid, cid):
            return None
    driver = sidecar_commission(getattr(service,"root",None), pid, cid, snapshot["job_id"])
    if not driver or (owner["kind"] == "pi_video" and owner["id"] not in driver["submitted"]):
        return None
    config = service.vision_config
    audio = getattr(config,"continuity_config",None)
    if not audio or audio.adapter not in {"openai","bailian"}:
        return None
    routes = [[c.model,c.base_url,getattr(c,"route_account","")] for c in (config,audio)]
    recorded = _read(work / "reports.json")
    clips = {sid:item["clip"] for sid,item in snapshot["available"].items()}
    current_edges = _current_edges(work,reports,clips,view["project"])
    selected, criteria = {}, {}
    for shot in view["project"]["shots"]:
        sid = shot["id"]
        if sid not in reports:
            continue
        report_sha, refs, _ = reports[sid]
        report = _read(recorded[sid]["report_path"])
        required = legacy_audio_checks(shot,report)
        if not required:
            continue
        clip = snapshot["available"][sid]["clip"]
        existing = _read(Path(recorded[sid]["report_path"]).parent/"content.json")
        edges = _replace_stale_edges(existing,clips,current_edges)
        if edges is None:
            continue
        for ref in refs:
            path = Path(ref["path"]).resolve()
            if (not path.is_relative_to((work/"cv2"/sid).resolve()) or path.parent.parent.name != "model"):
                continue
            receipt = _read(path)
            video = receipt["video"]
            source = video["binding"]["source"]
            call_path = Path(receipt.get("call_path") or "").resolve()
            call = _read(call_path) if call_path == path.with_name("call.json") and call_path.is_file() else {}
            if (digest_json(receipt) != ref["sha256"] or receipt.get("execution_status") != "executed"
                    or receipt["context"].get("shot") != shot or source["sha256"] != clip["media_sha256"]
                    or Path(source["path"]).resolve() != Path(clip["media_path"]).resolve()
                    or call.get("status") != "completed" or not receipt.get("response")
                    or call.get("response") != receipt["response"] or digest_json(call) != receipt.get("call_sha256")):
                continue
            scopes = review_scope(shot,receipt["rows"],video["duration_s"])
            if set(scopes) != required:
                continue
            cached = _completed_sound_receipt(path.parent.parent.parent,receipt,ref,scopes,routes)
            if not cached:
                if remaining < 2 or not config.api_key or not audio.api_key:
                    break
                remaining -= 2
            selected[sid] = {"report":report_sha,"timing":ref,"media_sha256":clip["media_sha256"],"edges":edges,
                             "cached_receipt":cached}
            criteria[sid] = sorted(required)
            break
    if not selected:
        return None
    identity = ["legacy-sound-review/2",POLICY,VERSION,NORMALIZATION_VERSION,STATE_AUDIT_VERSION,
                cid,snapshot["sha256"],routes,selected,criteria]
    needs_route = any(item["cached_receipt"] is None for item in selected.values())
    return {"token":digest_json([owner["id"],identity]),"key":"review-legacy-audio-"+digest_json(identity),
            "allowed":{},"legacy_audio":selected,"criteria":criteria,
            "urls":sorted({c.base_url.rstrip("/")+"/chat/completions" for c in (config,audio)}) if needs_route else []}


@traced("studio.review.legacy_sound", kind="task", capture_inputs=False, capture_output=False)
def apply_legacy_sound(workspace, scope):
    """Append only the missing beat observations, retaining every unrelated prior result."""
    completed = 0
    for sid, bound in scope["legacy_audio"].items():
        prior = workspace.reports[sid]
        folder = Path(prior["report_path"]).parent
        previous = _read(prior["report_path"])
        contract, evidence = _read(folder/"contract.json"), _read(folder/"content.json")
        inspection = _read(prior["inspection_path"])
        clip = workspace.shots[sid]
        ref = bound["timing"]
        timing = _read(ref["path"])
        if (digest_json(previous) != bound["report"] or digest_json(evidence) != previous["content_evidence_sha256"]
                or ref not in evidence["receipts"] or digest_json(timing) != ref["sha256"]
                or clip["media_sha256"] != bound["media_sha256"]):
            raise ValueError("当前素材或对应的旧观察已变动，须重新核对同一委托。")
        validate_content_evidence(workspace.project,contract,inspection,evidence)
        shot = next(s for s in workspace.project["shots"] if s["id"] == sid)
        scopes = review_scope(shot,timing["rows"],timing["video"]["duration_s"])
        if sorted(scopes) != scope["criteria"][sid]:
            raise ValueError("原分镜与选定听审范围不符。")
        work = Path(ref["path"]).resolve().parent.parent.parent
        original = {**timing,"receipt_path":ref["path"],"receipt_sha256":ref["sha256"]}
        if bound["edges"]:
            old_refs, old_criteria, current_rows = [], {}, {}
            for old in evidence["receipts"]:
                previous_data = _read(old["path"])
                context = previous_data.get("context") or {}
                cid = f"continuity:{context.get('left')}->{context.get('right')}"
                if cid not in bound["edges"]:
                    old_refs.append(old)
                else:
                    old_criteria[cid] = previous_data["criteria"]
            for cid,new in bound["edges"].items():
                data = _read(new["path"])
                if (digest_json(data) != new["sha256"] or [r["criterion_id"] for r in data["rows"]] != [cid]
                        or old_criteria.get(cid) != data["criteria"]):
                    raise ValueError("当前邻镜的正式回执已变更。")
                current_rows[cid] = data["rows"][0]
                old_refs.append(new)
            evidence["receipts"] = old_refs
            evidence["rows"] = [current_rows.get(r["criterion_id"],r) for r in evidence["rows"]]
        if "selected_media" in evidence:
            evidence["selected_media"] = {s:_asset(c["media_path"]) for s,c in workspace.shots.items()}
        validate_content_evidence(workspace.project,contract,inspection,evidence)
        result = review_creative_sound(workspace,shot,clip,work,original,timing["criteria"],scopes,
                                       cached_receipt=bound.get("cached_receipt"))
        replacements = {r["criterion_id"]:r for r in result["rows"]}
        if set(replacements) != set(scopes):
            raise ValueError("新观察未返回原分镜的完整分项。")
        evidence["rows"] = [replacements.get(r["criterion_id"],r) for r in evidence["rows"]]
        evidence["receipts"].append({"path":result["receipt_path"],"sha256":result["receipt_sha256"]})
        index = workspace.directory / "reports.json"
        if _read(index)[sid]["report_sha256"] != bound["report"]:
            raise ValueError("复验期间已有更新的选用报告，保留这次观察供原委托核对。")
        publish_content_report(workspace,sid,contract,inspection,evidence,work)
        updated = _read(workspace.reports[sid]["report_path"])
        affected = set(replacements) | set(bound["edges"])
        if unaffected_observations(previous,affected) != unaffected_observations(updated,affected):
            raise ValueError("本次复验涉及范围外的验收变化，已保留原选用报告。")
        saved = _read(index)
        if saved[sid]["report_sha256"] != bound["report"]:
            raise ValueError("选用报告已变更，复验凭据已单独保存。")
        event("studio.review.legacy_sound_result", {"shot_id":sid,"media_sha256":bound["media_sha256"],"policy":POLICY,
              "criteria":scope["criteria"][sid],"timing_receipt":ref,"current_edge_receipts":bound["edges"],
              "previous_report_sha256":bound["report"],"report_sha256":workspace.reports[sid]["report_sha256"]},
              kind="artifact")
        _write(index,{**saved,sid:workspace.reports[sid]})
        completed += int(result["execution_status"] == "executed")
    return completed
