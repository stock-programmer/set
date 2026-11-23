"""Project friendly asset diagnostics from the events of a single finished order."""
from pianchang.asset_diagnostics import asset_diagnostic, asset_failure_summary
from trace_system import traced


_NAMES = ("studio.assets.unit.transition", "asset.reference.review", "asset.reference.loop", "collab.assets.publish")


def legacy_model_error(issue):
    return (isinstance(issue, dict) and issue.get("stage") == "unknown" and
            "404 - The model or endpoint " in (issue.get("reason") or ""))


def project_recorded_failures(service, job, recorded):
    # Old summaries clipped the provider error mid-sentence. Use the original
    # order's full error, even when a later order has replaced its work item.
    with service.lock:
        work = service.state.get("asset_work", {}).get(job["project_id"], {}).get("items", {})
        rows = {(row["subject_id"], row["purpose"]): dict(row) for row in work.values()
                if row.get("job_id") == job["id"] and row.get("trace_id") == job["trace_id"]}
    history = None
    projected = []
    for issue in recorded["items"]:
        if legacy_model_error(issue) and issue.get("trace_id") == job["trace_id"]:
            key = (issue.get("subject_id"), issue.get("purpose"))
            row = rows.get(key)
            if row and row.get("status") == "needs_attention" and row.get("candidate") == issue.get("candidate"):
                diagnosis = asset_diagnostic(row)
            else:
                if history is None:
                    history = {(x["subject_id"], x["purpose"]): x
                               for x in read_asset_job_history(service, job)["items"]}
                diagnosis = history.get(key)
            if diagnosis and diagnosis.get("code") == "image_model_access":
                issue = {**issue, **{k: diagnosis[k] for k in ("code", "model_id", "reason", "action", "stage", "verdict")}}
        projected.append(issue)
    return {**recorded, "summary": asset_failure_summary(recorded.get("summary", ""), projected), "items": projected}


def _order_events(tracer, trace_id):
    after = 0
    while True:
        page = tracer.get_events(trace_id, after=after, limit=250, names=_NAMES)
        yield from page
        if len(page) < 250:
            return
        after = page[-1]["trace_seq"]


@traced("studio.assets.read_failure_history", kind="tool", capture_inputs=False, capture_output=False)
def read_asset_job_history(service, job):
    """Read the recorded final state and committed review from that task, including old jobs."""
    tracer, trace_id = service.tracer, job["trace_id"]
    reviews, loops, units = {}, {}, {}
    for record in _order_events(tracer, trace_id):
        name, kind, sid = record["name"], record["kind"], record["span_id"]
        if name == "studio.assets.unit.transition" and kind == "state":
            row = tracer.resolve(record["data"])
            if row.get("job_id") == job["id"]:
                units[row["subject_id"], row["purpose"]] = (row, sid)
        elif name in ("asset.reference.review", "asset.reference.loop") and kind == "span.start":
            inputs = tracer.resolve(tracer.resolve(record["data"]).get("inputs")) or {}
            if inputs.get("pid") != job["project_id"]:
                continue
            if name == "asset.reference.review":
                pin = inputs.get("pin")
                if isinstance(pin, dict):
                    reviews[sid] = {"pin": pin, "trace_id": trace_id, "span_id": sid}
            else:
                body = inputs.get("body") or {}
                loops[body.get("subject_id"), body.get("purpose")] = sid
        elif name == "collab.assets.publish" and kind == "span.end" and record["parent_span_id"] in reviews:
            attempt = reviews[record["parent_span_id"]]
            end = tracer.resolve(record["data"])
            if end.get("status") != "OK":
                continue
            publication = tracer.resolve(end.get("output")) or {}
            report = publication.get("content") or {}
            if (publication.get("kind") == "reference_review" and report.get("trace_id") == trace_id
                    and report.get("candidate") == attempt["pin"]):
                attempt["review"] = {**report, "review_ref": {k: publication[k] for k in ("asset_id", "revision_id", "sha256")}}
    issues = []
    for key, (row, transition_span) in units.items():
        if row["status"] != "needs_attention":
            continue
        attempts = [a for a in reviews.values() if (a["pin"].get("subject_id"), a["pin"].get("purpose")) == key]
        matching = next((a for a in reversed(attempts) if a.get("review") and a["pin"] == row.get("candidate")), {})
        # An error recorded after inspection belongs to the operation, not to its earlier successful review.
        span_id = (loops.get(key) if row.get("error") else matching.get("span_id")) or transition_span
        issue = asset_diagnostic({**row, "review": matching.get("review"), "attempts": attempts},
                                 trace_id=trace_id, span_id=span_id)
        if issue:
            issues.append(issue)
    summary = (job.get("result") or {}).get("summary") or job.get("error") or ""
    return {"project_id": job["project_id"], "summary": asset_failure_summary(summary, issues),
            "items": issues}
