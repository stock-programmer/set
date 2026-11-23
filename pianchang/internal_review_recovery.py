"""Append only authorized within-shot cut observations to the selected film's report."""
from pathlib import Path

from pianchang.content_review import (_read, _write, publish_content_report, review_internal_cut,
                                      unaffected_observations, validate_content_evidence)
from pianchang.story_spec import digest_json
from trace_system import event, traced


@traced("studio.review.internal_cuts",kind="task",capture_inputs=False,capture_output=False)
def apply_internal_cuts(workspace,scope):
    index = workspace.directory/"reports.json"
    shots = {shot["id"]:shot for shot in workspace.project["shots"]}
    selected = {sid:(clip["media_sha256"],Path(clip["media_path"]).resolve()) for sid,clip in workspace.shots.items()}
    completed = 0
    for sid,bound in scope["internal_cuts"].items():
        prior = workspace.reports[sid]
        previous = _read(prior["report_path"])
        folder = Path(prior["report_path"]).parent
        evidence = _read(folder/"content.json")
        contract = _read(folder/"contract.json")
        inspection = _read(prior["inspection_path"])
        clip = workspace.shots[sid]
        timing_ref = bound["timing"]
        timing_path = Path(timing_ref["path"]).resolve()
        timing = _read(timing_path)
        work = timing_path.parent.parent.parent
        source = timing["video"]["binding"]["source"]
        expected = {r["criterion_id"]:r for r in contract["requirements"] if r["kind"] == "internal_cut"}
        rows = {r["criterion_id"]:r for r in previous["results"]}
        affected = set(bound["criteria"])
        if (prior["report_sha256"] != bound["report"] or _read(index)[sid]["report_sha256"] != bound["report"]
                or digest_json(previous) != bound["report"] or digest_json(evidence) != previous["content_evidence_sha256"]
                or digest_json(timing) != timing_ref["sha256"] or timing_ref not in evidence["receipts"]
                or (source["sha256"],Path(source["path"]).resolve()) != selected[sid]
                or {other:(asset["sha256"],Path(asset["path"]).resolve()) for other,asset in evidence["selected_media"].items()} != selected
                or not work.is_relative_to((workspace.directory/"cv2"/sid).resolve())
                or timing_path.parent.parent.name != "model" or timing["context"]["shot"] != shots[sid]
                or not affected or affected != set(scope["criteria"][sid]) or not affected <= expected.keys()
                or any(not rows[cid]["required"] or rows[cid]["status"] != "UNKNOWN" for cid in affected)):
            raise ValueError("选用影片、报告或段内切点范围已更新，保留原观察。")
        validate_content_evidence(workspace.project,contract,inspection,evidence)
        replacements = {}
        old_refs = []
        for ref in evidence["receipts"]:
            data = _read(ref["path"])
            pair = data.get("context",{}).get("creative_pair")
            cid = "internal:"+"->".join(pair) if pair else None
            if cid not in affected:
                old_refs.append(ref)
                continue
            allowed = bound["criteria"][cid]
            if (allowed["mode"] == "new" or Path(ref["path"]).resolve().parent.parent != Path(allowed["folder"]).resolve()
                    or [r["criterion_id"] for r in data["rows"]] != [cid]):
                raise ValueError("段内切点已有其他执行回执，保留原记录。")
        for cid,cut in bound["criteria"].items():
            result = review_internal_cut(workspace,shots[sid],clip,work,timing,expected[cid],bound_cut=cut)
            if not result or [r["criterion_id"] for r in result["rows"]] != [cid]:
                raise ValueError("当前两侧的片内时段或验收回执不完整。")
            replacements[cid] = result["rows"][0]
            old_refs.append({"path":result["receipt_path"],"sha256":result["receipt_sha256"]})
            completed += int(result["execution_status"] == "executed")
        evidence["rows"] = [r for r in evidence["rows"] if r["criterion_id"] not in affected]+list(replacements.values())
        evidence["receipts"] = old_refs
        validate_content_evidence(workspace.project,contract,inspection,evidence)
        publish_content_report(workspace,sid,contract,inspection,evidence,work)
        updated = _read(workspace.reports[sid]["report_path"])
        if unaffected_observations(previous,affected) != unaffected_observations(updated,affected):
            raise ValueError("段内复验发现范围外的验收依据变化，保留原报告。")
        saved = _read(index)
        if saved[sid]["report_sha256"] != bound["report"]:
            raise ValueError("验收期间选用报告已更新，已保存切点观察。")
        event("studio.review.internal_cut_result",{"shot_id":sid,"media_sha256":clip["media_sha256"],
              "criteria":sorted(affected),"previous_report_sha256":bound["report"],
              "report_sha256":workspace.reports[sid]["report_sha256"]},kind="artifact")
        _write(index,{**saved,sid:workspace.reports[sid]})
    return completed
