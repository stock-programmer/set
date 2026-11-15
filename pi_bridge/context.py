"""Bounded model observations; full tool receipts remain in shared Trace."""
from pianchang.story_spec import digest_json
from trace_system import traced


def reference_issue(item, *, all_checks=False):
    review=item.get("review") or {}
    return {"subject_id":item.get("subject_id"),"purpose":item.get("purpose"),
        "status":item.get("status"),"candidate":item.get("candidate"),
        "selected_pin":item.get("selected_pin"),"verdict":review.get("verdict"),
        "observation_error":review.get("observation_error"),
        "checks":[{k:c.get(k) for k in ("path","required","status","expected","actual","frame_ids")}
            for c in review.get("checks",[]) if all_checks or c.get("required") and c.get("status")!="PASS"],
        "review_ref":review.get("review_ref"),"trace_id":review.get("trace_id"),
        "attempts":[{k:a.get(k) for k in ("verdict","review_ref","trace_id")} for a in item.get("attempts",[])]}


@traced("pi.context.reference_source_script",kind="validation",capture_inputs=False)
def script_origin_check(item,script_ref):
    origin=(item.get("review") or {}).get("brief",{}).get("script_ref")
    known=bool(origin and script_ref and all(origin.get(k) and script_ref.get(k) for k in ("asset_id","revision_id","sha256")))
    matches=known and all(origin[k]==script_ref[k] for k in ("asset_id","revision_id","sha256"))
    return {"path":"reference."+str(item.get("subject_id"))+"."+str(item.get("purpose"))+".source_script",
        "expected":script_ref,"actual":origin,"status":"PASS" if matches else "FAIL" if known else "UNKNOWN"}


@traced("pi.context.video_observation",kind="validation",capture_inputs=False)
def video_observation(value):
    """Preserve current counts and each blocker without repeating full scripts/VLM briefs."""
    work=value.get("asset_work") or {}
    script=value.get("script") or {}
    items=work.get("items",[])
    return {"observation_id":digest_json(value),"authority":"fresh read_video_state tool result; supersedes historical progress summaries",
        "full_receipt":"pi.tool.response in shared Trace and this run's run.json; observation_id hashes the complete result",
        "project_id":value.get("project_id"),"story_id":value.get("story_id"),"title":value.get("title"),
        "script":{k:script.get(k) for k in ("script_ref","structure")},
        "reference_version_policy":"Revision IDs are local to each asset. A candidate r2 and script r1 are compatible when source_script matches; differing local revision numbers are not evidence of mismatch.",
        "asset_work":{"counts":work.get("counts"),"total":len(items),
            "current_items":[{"subject_id":i.get("subject_id"),"purpose":i.get("purpose"),"status":i.get("status"),
                "selected_pin":i.get("selected_pin")} for i in items],
            "issues":[{**reference_issue(i),"source_script_validation":script_origin_check(i,script.get("script_ref")),
                "interpretation":"An observation_error means the checking pipeline failed; it does not prove absent media or poor voice quality. Keep the verdict unresolved with the exact error."
                    if (i.get("review") or {}).get("observation_error") else "Use actual failed checks; do not infer a version mismatch from different asset revision numbers."}
                for i in items if i.get("status")!="approved"],
            "jobs":[{k:j.get(k) for k in ("id","status","message","trace_id")} for j in work.get("jobs",[])]},
        "video_plan":value.get("video_plan"),"video_plan_error":value.get("video_plan_error"),"video_execution":value.get("video_execution"),
        "incomplete_production":value.get("incomplete_production"),"production_review":value.get("production_review"),
        "preview_gate":value.get("preview_gate"),"handoff":value.get("handoff")}


@traced("pi.context.reference_observation",kind="validation",capture_inputs=False)
def reference_observation(item):
    return {"observation_id":digest_json(item),**reference_issue(item,all_checks=True)}
