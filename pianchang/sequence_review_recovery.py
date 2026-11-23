"""Publish an adjacent-cut result from two completed observations of the selected clips."""
from pathlib import Path

from pianchang.content_review import (_read, _write, publish_content_report, review_video,
                                      unaffected_observations, validate_content_evidence)
from pianchang.legacy_sound_review import _current_edges
from pianchang.story_spec import digest_json
from trace_system import event, traced


def completed_parts(path, request_sha):
    if path.name != "receipt-p4.json" or path.parent.name != request_sha[:12] or path.parent.parent.name != "model":
        return None
    parts = {}
    audio = None
    for name in ("audio-call.json", "visual-call.json"):
        source = path.with_name(name)
        if not source.is_file():
            return None
        part = _read(source)
        if (part.get("status") != "completed" or not part.get("response")
                or digest_json(part["request"]) != part.get("input_sha256")):
            return None
        if name == "audio-call.json":
            if part["input_sha256"] != request_sha:
                return None
            audio = part
        elif part["request"] != {**audio["request"], "audio_sha256":digest_json(audio["response"])}:
            return None
        parts[name] = digest_json(part)
    return parts


def sequence_assembly_scope(service, view, snapshot, owner, work, reports, remaining, *, excluding=None):
    # review_retry has already validated the original commission, current reports, and idle owner.
    candidates = {key for _, _, unresolved in reports.values() for key in unresolved if key.startswith("continuity:")}
    if not candidates:
        return None
    clips = {sid:entry["clip"] for sid,entry in snapshot["available"].items()}
    edges = _current_edges(work, reports, clips, view["project"])
    for criterion in sorted(candidates):
        refs = edges.get(criterion, {})
        if len(refs) != 1:
            continue
        ref = next(iter(refs.values()))
        path = Path(ref["path"]).resolve()
        receipt = _read(path)
        if receipt["execution_status"] not in {"execution_error", "partial"}:
            continue
        parts = completed_parts(path, receipt["request_sha256"])
        targets = {sid:sha for sid,(sha,refs,unresolved) in reports.items() if criterion in unresolved and ref in refs}
        if not parts or not targets:
            continue
        bound = {"criterion":criterion, "receipt":ref, "parts":parts, "reports":targets}
        key = "review-sequence-assembly-" + digest_json([snapshot["request_id"], snapshot["sha256"], bound])
        return {"sequence_assembly":bound, "key":key, "token":digest_json([owner["id"], key]),
                "criteria":{sid:[criterion] for sid in targets}, "urls":[], "allowed":{}}
    return None


@traced("studio.review.assemble_sequence", kind="task", capture_inputs=False, capture_output=False)
def apply_sequence_assembly(workspace, scope):
    bound = scope["sequence_assembly"]
    criterion, ref = bound["criterion"], bound["receipt"]
    path = Path(ref["path"]).resolve()
    old = _read(path)
    if (not path.is_relative_to((workspace.directory / "cv2").resolve()) or digest_json(old) != ref["sha256"]
            or [row["criterion_id"] for row in old["criteria"]] != [criterion]
            or completed_parts(path, old["request_sha256"]) != bound["parts"]):
        raise ValueError("相邻片段的原始回执已更新，保留原报告。")
    # assemble mode checks the complete request fingerprint and permits only local cache reads.
    model_dir = path.parent.parent
    merged = review_video(workspace.vision_config, old["video"], old["references"], old["criteria"], old["context"],
                          model_dir, audiovisual=True, bound_cut={"mode":"assemble", "folder":str(model_dir),
                                                                  "receipt":ref, "parts":bound["parts"]})
    if merged["execution_status"] != "executed" or [row["criterion_id"] for row in merged["rows"]] != [criterion]:
        raise ValueError("现有视听回执仍不足以合并，保留原报告。")
    index = workspace.directory / "reports.json"
    for sid, sha in bound["reports"].items():
        current = workspace.reports[sid]
        folder = Path(current["report_path"]).parent
        previous = _read(current["report_path"])
        evidence = _read(folder / "content.json")
        contract = _read(folder / "contract.json")
        inspection = _read(current["inspection_path"])
        if (current["report_sha256"] != sha or digest_json(previous) != sha
                or digest_json(evidence) != previous["content_evidence_sha256"] or evidence["receipts"].count(ref) != 1
                or sum(r["criterion_id"] == criterion for r in evidence["rows"]) != 1):
            raise ValueError("当前影片或所选衔接报告已变更。")
        evidence["rows"] = [merged["rows"][0] if row["criterion_id"] == criterion else row for row in evidence["rows"]]
        evidence["receipts"] = [{"path":merged["receipt_path"], "sha256":merged["receipt_sha256"]} if r == ref else r
                                for r in evidence["receipts"]]
        validate_content_evidence(workspace.project, contract, inspection, evidence)
        publish_content_report(workspace, sid, contract, inspection, evidence, Path(current["content_work_path"]))
        updated = _read(workspace.reports[sid]["report_path"])
        if unaffected_observations(previous, {criterion}) != unaffected_observations(updated, {criterion}):
            raise ValueError("本地合并改变了其他验收依据，保留原选用报告。")
    current_index = _read(index)
    if any(current_index[sid]["report_sha256"] != sha for sid,sha in bound["reports"].items()):
        raise ValueError("相邻镜头验收期间报告已更新。")
    event("studio.review.sequence_assembled", {"criterion":criterion, "old_receipt":ref["sha256"],
          "receipt":merged["receipt_sha256"], "reports":{sid:workspace.reports[sid]["report_sha256"]
          for sid in bound["reports"]}, "new_model_call":False}, kind="artifact")
    _write(index, {**current_index, **{sid:workspace.reports[sid] for sid in bound["reports"]}})
    return 1
