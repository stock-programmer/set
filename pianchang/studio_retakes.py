"""Offer one additional take from recorded failures on the current production."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from pianchang.story_spec import digest_json
from trace_system import event, traced


def _proposal(view, shots, reason, evidence, **fields):
    decision = {"story_id": view["story_id"], "script_ref": view["script_ref"],
                "spec_ref": view["spec"], "production_ref": view.get("production_ref"),
                "shot_ids": shots, "reason": reason, "evidence": evidence, **fields}
    return {**decision, "proposal_id": digest_json(decision)}


def _latest_request(view):
    path = Path(view["store_path"])
    if not path.is_file():
        return None, None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        row = db.execute("""SELECT message_id,payload FROM messages WHERE story_id=? AND correlation_id=?
                    AND message_type='production.requested' AND recipient='video' ORDER BY seq DESC LIMIT 1""",
                         (view["story_id"], view["run_id"])).fetchone()
        if not row:
            return None, None
        request_id, body = row[0], json.loads(row[1])
        if (body.get("script") != view["script_ref"] or body.get("template") != view["spec"]
                or body.get("previous") != view.get("production_ref")):
            return None, None
        result = db.execute("""SELECT payload FROM messages WHERE causation_id=? AND sender='video'
                    AND message_type='production.result' ORDER BY seq DESC LIMIT 1""", (request_id,)).fetchone()
        return {"id": request_id, "body": body}, json.loads(result[0]) if result else None


def _partial_proposal(view, request, result):
    partial = result.get("partial") or {}
    if partial.get("request_id") != request["id"] or not re.fullmatch(r"[a-f0-9]{64}", str(partial.get("sha256"))):
        return None
    failures = result.get("failed_shots") or {}
    shot_ids = [shot["id"] for shot in view["project"]["shots"]]
    failed = [sid for sid in shot_ids if sid in failures]
    if not failed or set(failed) != set(failures):
        return None
    reasons = []
    for sid in failed:
        info = failures[sid]
        if (info.get("status") not in {"FAILED", "CANCELED", "CANCELLED"} or not info.get("task_id")
                or not re.fullmatch(r"[0-9a-f]{64}", str(info.get("manifest_sha256")))):
            return None
        reasons.append(sid + " " + str(info.get("reason") or info["status"]))
    return _proposal(view, failed, "上次视频生成未成功。" + " / ".join(reasons),
                     [{"shot_id": sid, "task_id": failures[sid]["task_id"], "status": failures[sid]["status"],
                       "manifest_sha256": failures[sid]["manifest_sha256"]} for sid in failed],
                     partial=partial, reused_shots=[sid for sid in shot_ids if sid not in failures])


def _report_failures(view, previous, reusable_shots):
    if not previous:
        return []
    failures = []
    workdir = (Path(view["store_path"]).parent / "runs" / view["run_id"]).resolve()
    for sid in reusable_shots:
        summary = previous["content"].get("reports", {}).get(sid, {})
        if summary.get("status") != "FAIL" or not summary.get("report_path"):
            continue
        path = Path(summary["report_path"]).resolve()
        if not path.is_relative_to(workdir) or not path.is_file():
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        actual = digest_json(report)
        expected = summary.get("report_sha256")
        valid = actual == expected and report.get("shot_id") == sid
        event("studio.retake.report_checked", {"path": "reports." + sid, "expected": expected,
              "actual": actual, "status": "PASS" if valid else "FAIL"}, kind="validation")
        if not valid:
            continue
        results = report.get("results", [])
        if any(r.get("criterion_id") in {"evidence_integrity", "candidate_binding"}
               and r.get("status") != "PASS" for r in results):
            continue
        for row in results:
            if row.get("required") and row.get("status") == "FAIL":
                failures.append({"shot_id": sid, "report_sha256": expected, "criterion_id": row["criterion_id"],
                                 "reason": row.get("reason") or row.get("expectation") or row["criterion_id"]})
    return failures


@traced("studio.video.extra_retake_proposal", kind="validation", capture_inputs=False)
def extra_retake_proposal(view, previous, reusable_shots):
    """Read pinned evidence. Unknown provider outcomes and unobserved media never authorize another take."""
    request, result = _latest_request(view)
    if request and result and result.get("partial"):
        return _partial_proposal(view, request, result)
    failures = _report_failures(view, previous, reusable_shots)
    if not failures:
        return None
    shots = list(dict.fromkeys(row["shot_id"] for row in failures))
    reasons = " / ".join(dict.fromkeys(row["shot_id"] + " " + row["reason"] for row in failures))
    proposal = _proposal(view, shots, "上一版的必检项未通过。" + reasons, failures)
    # A consumed decision whose request is running or uncertain must not invite another submission.
    consumed = (request or {}).get("body", {}).get("shooting_plan") or {}
    return None if consumed.get("proposal_id") == proposal["proposal_id"] else proposal
