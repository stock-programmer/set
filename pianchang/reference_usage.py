"""Read-only reverse lookup of exact reference revisions in shot specs and provider receipts."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from pianchang.collab_store import CollabStore
from pianchang.story_spec import digest_json
from trace_system import event, get_tracer, traced


_PIN_FIELDS = ("asset_id", "revision_id", "sha256", "media_sha256", "subject_id", "purpose")
_SHOT_SOURCE_KINDS = ("production_spec", "input_project", "production", "shot_reference_spec")


def _identity(asset):
    return {k: asset[k] for k in ("asset_id", "revision_id", "sha256")}


def _pin(pin):
    return {k: pin.get(k) for k in _PIN_FIELDS}


def _matches(pins, reference):
    if not isinstance(pins, list):
        return []
    return [_pin(p) for p in pins if isinstance(p, dict) and p.get("asset_id") == reference["asset_id"]
            and p.get("revision_id") == reference["revision_id"]]


def _snapshot(store, asset_id, revision_id, story_id):
    path = Path(store.path if isinstance(store, CollabStore) else store).resolve()
    if not path.is_file():
        raise FileNotFoundError("协作资产库不存在。")
    if not isinstance(revision_id, str) or not re.fullmatch(r"r[1-9][0-9]*", revision_id):
        raise ValueError("必须指定不可变的参考版本，例如 r1。")
    # One read transaction holds all the publication records and typed edges at the same instant.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM assets WHERE asset_id=? AND revision_id=?", (asset_id, revision_id)).fetchone()
        if not row:
            raise KeyError("找不到此故事的参考版本。")
        target = CollabStore._asset(db, row)
        if target["kind"] != "reference_media":
            if story_id is not None and story_id != target["story_id"]:
                raise KeyError("找不到此故事的参考版本。")
            raise ValueError("指定版本须是图像或声音参考资产。")
        direct_media = {(r["asset_id"], r["revision_id"]) for r in db.execute("""SELECT d.asset_id,d.revision_id
            FROM dependencies d JOIN assets a USING(asset_id,revision_id) WHERE d.dep_asset_id=? AND d.dep_revision_id=?
            AND d.role IN ('submitted_reference','derived_reference') AND a.kind='shot_media'""", (asset_id, revision_id))}
        # Only the explicit published edges authorize another production to view the reference.
        rows = db.execute("""WITH direct AS (
              SELECT d.asset_id,d.revision_id,owner.kind FROM dependencies d JOIN assets owner USING(asset_id,revision_id)
              WHERE d.dep_asset_id=? AND d.dep_revision_id=? AND (
                  (d.role IN ('submitted_reference','derived_reference') AND owner.kind='shot_media') OR
                  (d.role='required_reference' AND owner.kind IN ('shot_reference_spec','production_spec','input_project','production')))
            ), linked AS (
              SELECT asset_id,revision_id FROM direct UNION
              SELECT s.asset_id,s.revision_id FROM dependencies s JOIN direct d
                ON s.dep_asset_id=d.asset_id AND s.dep_revision_id=d.revision_id
                JOIN assets consumer ON consumer.asset_id=s.asset_id AND consumer.revision_id=s.revision_id
                WHERE consumer.kind='production' AND (
                    (s.role='selected_media' AND d.kind='shot_media') OR
                    (s.role='reference_spec' AND d.kind IN ('shot_reference_spec','production_spec')))
            )
            SELECT a.* FROM assets a WHERE (a.story_id=? OR EXISTS (
                 SELECT 1 FROM linked l WHERE a.asset_id=l.asset_id AND a.revision_id=l.revision_id))
              AND (? IS NULL OR a.story_id=?)
              AND a.kind IN ('production_spec','input_project','production','shot_reference_spec','script','shot_media')
            ORDER BY a.asset_id,a.seq""", (asset_id, revision_id, target["story_id"], story_id, story_id))
        sources = [CollabStore._asset(db, source) for source in rows]
        if story_id is not None and target["story_id"] != story_id and not sources:
            raise KeyError("找不到此故事的参考版本。")
    return target, sources, direct_media


def _shot_requirements(asset, reference, requirements, project_hashes):
    if asset["kind"] not in _SHOT_SOURCE_KINDS:
        return
    content = asset["content"]
    if asset["kind"] == "shot_reference_spec":
        shots = [content]
        fingerprint = content.get("project_sha256")
    else:
        project = content["project"] if asset["kind"] == "production" else content
        shots = project.get("shots", [])
        fingerprint = digest_json(project)
    if fingerprint:
        project_hashes[fingerprint] = asset["story_id"]
    for shot in shots:
        sid = shot.get("shot_id") or shot.get("id")
        pins = _matches(shot.get("required_refs", []), reference)
        if not pins:
            continue
        edge_recorded = any(dep["role"] == "required_reference" and dep["asset_id"] == reference["asset_id"]
                            and dep["revision_id"] == reference["revision_id"] and dep.get("source_id") == sid
                            for dep in asset["dependencies"])
        requirements.append({"shot_id": sid, "source": _identity(asset), "source_kind": asset["kind"],
                             "project_sha256": fingerprint, "required_refs": pins, "edge_recorded": edge_recorded,
                             "integrity": "PASS" if all(p == reference for p in pins) else "FAIL",
                             "trace_id": asset["trace_id"]})


def _selections(asset, selections, reference, owner_story, direct_media):
    if asset["kind"] != "production":
        return
    package = asset["content"]
    declared = {s["id"] for s in package["project"].get("shots", []) if _matches(s.get("required_refs"), reference)}
    for sid, clip in package.get("shots", {}).items():
        if not clip.get("manifest_path"):
            continue
        media_ref = (package.get("media_refs") or {}).get(sid) or {}
        media_key = (media_ref.get("asset_id"), media_ref.get("revision_id"))
        if asset["story_id"] != owner_story and sid not in declared and media_key not in direct_media:
            continue
        path = Path(clip["manifest_path"]).resolve()
        selections.setdefault(path, []).append({"production": _identity(asset), "shot_id": sid,
            "task_id": clip.get("task_id"), "media_sha256": clip.get("media_sha256"),
            "media_ref": package.get("media_refs", {}).get(sid), "operation": clip.get("operation"),
            "trace_id": asset["trace_id"]})


def _belongs(job, story_id, scripts, project_hashes, selections, reference, query_story):
    metadata = job.get("metadata") or {}
    reference_scope = metadata.get("reference_story_id")
    if reference_scope is not None and reference_scope != story_id:
        return False
    declared = metadata.get("story_id")
    if declared is not None:
        if query_story is not None and declared != query_story:
            return bool(selections)
        return bool(selections) or declared == story_id or (reference_scope == story_id and bool(_matches(metadata.get("required_refs"), reference)))
    script = metadata.get("script_asset_ref") or {}
    if script:
        return bool(selections) or (script.get("asset_id"), script.get("revision_id"), script.get("sha256")) in scripts
    return bool(selections) or project_hashes.get(metadata.get("project_sha256")) == story_id


def _http_exchanges(trace_id, exchange_cache):
    if trace_id in exchange_cache:
        return exchange_cache[trace_id]
    calls, parents, submissions, preflight_spans, prepared = {}, {}, set(), set(), []
    after = 0
    tracer = get_tracer()
    # Read transport/preflight records, not every creative-model transcript in a
    # long production trace. Parent headers are loaded below if a wrapper is absent.
    names = ["video.submit", "video.references.check_request", "bailian.video.submit",
             "bailian.video.http", "http.request", "http.response"]
    while True:
        events = tracer.get_events(trace_id, after=after, limit=1000, names=names)
        for entry in events:
            sid = entry["span_id"]
            if entry["kind"] == "span.start":
                parents[sid] = entry["parent_span_id"]
                if entry["name"] == "video.submit":
                    submissions.add(sid)
                if entry["name"] == "video.references.check_request":
                    preflight_spans.add(sid)
            elif entry["kind"] == "span.end" and sid in preflight_spans:
                value = tracer.resolve(entry["data"])
                receipt = tracer.resolve(value.get("output"))
                if value.get("status") == "OK" and isinstance(receipt,dict) and receipt.get("status") == "PREPARED":
                    prepared.append((sid, entry["trace_seq"], receipt))
            if entry["name"] not in {"http.request", "http.response"}:
                continue
            data = tracer.resolve(entry["data"])
            if entry["name"] == "http.request":
                if data.get("method") == "POST" and str(data.get("url", "")).endswith(("/services/aigc/video-generation/video-synthesis", "/api/v3/contents/generations/tasks")):
                    calls[sid] = {"request":data, "request_seq":entry["trace_seq"]}
            elif sid in calls:
                calls[sid]["response"] = data
        if len(events) < 1000:
            break
        after = events[-1]["trace_seq"]
    def submission(span_id):
        visited = set()
        while span_id is not None and span_id not in submissions:
            if span_id in visited:
                return None
            visited.add(span_id)
            if span_id not in parents:
                headers = tracer.get_events(trace_id, span_id=span_id, limit=1)
                if not headers or headers[0]["kind"] != "span.start":
                    return None
                header = headers[0]
                parents[span_id] = header["parent_span_id"]
                if header["name"] == "video.submit":
                    submissions.add(span_id)
                    break
            span_id = parents.get(span_id)
        return span_id
    for sid, call in calls.items():
        run = submission(sid)
        call["preflight"] = [receipt for origin,seq,receipt in prepared
                             if run is not None and seq < call["request_seq"] and submission(origin) == run]
    exchange_cache[trace_id] = calls
    return calls


@traced("reference.usage.verify_exchange", kind="validation", capture_inputs=False)
def verify_reference_exchange(job, receipt, trace_id, exchange_cache=None):
    with get_tracer().read_batch():
        return _verify_reference_exchange(job,receipt,trace_id,exchange_cache)


def _verify_reference_exchange(job, receipt, trace_id, exchange_cache=None):
    """Compare a checked local receipt with the original submission's independent HTTP Trace."""
    if not isinstance(trace_id,str) or not re.fullmatch(r"[a-f0-9]{32}", trace_id):
        return {"status":"NO_TRANSPORT_RECORD"}
    if exchange_cache is None:
        exchange_cache = {}
    expected_wire = receipt["wire_body_sha256"]
    matches = []
    unconfirmed_binding = None
    pinned = bool(receipt.get("required_refs") or receipt.get("prepared_refs") or job.get("metadata", {}).get("required_refs"))
    route = job.get("metadata", {}).get("model_route") or {}
    sdk = route.get("adapter") == "litellm"
    if sdk:
        from pianchang.sdk_video import ark_body, ark_create_url
        expected_url = ark_create_url(job["base_url"])
        expected_body = ark_body(job["request"])
    else:
        expected_url = job["base_url"].rstrip("/") + "/services/aigc/video-generation/video-synthesis"
        expected_body = job.get("request")
    for sid, call in _http_exchanges(trace_id, exchange_cache).items():
        req = call["request"]
        if req.get("url") != expected_url:
            continue
        resp = call.get("response", {})
        body = resp.get("body") or {}
        task_id = (body.get("output") or {}).get("task_id") if isinstance(body,dict) else None
        if sdk:
            task_id = body.get("id") if isinstance(body, dict) else None
            same_task = bool(task_id and task_id == job.get("provider_task_id") == receipt.get("provider_task_id"))
        else:
            same_task = bool(task_id and task_id == job.get("task_id"))
        same_body = req.get("body") == expected_body
        same_wire = bool(expected_wire and req.get("wire_body_sha256") == expected_wire)
        provider_id = receipt.get("provider_request_id")
        same_rejection = (receipt["status"] == "PROVIDER_REJECTED" and provider_id
                          and provider_id in (resp.get("body_request_id"), resp.get("header_request_id")))
        identified = same_task or same_rejection
        if not (identified or same_wire):
            continue
        info = {"span_id":sid, "http_status":resp.get("http_status")}
        plans = call["preflight"]
        keys = ("required_refs","prepared_refs") if sdk else ("wire_body_sha256","required_refs","prepared_refs")
        plan_matches = any(all(plan.get(k) == receipt.get(k) for k in keys) for plan in plans)
        if not (same_body and same_wire) or (plans and not plan_matches):
            if identified:
                return {"status":"CONFLICT", **info}
            # Distinct candidates can use the same locator bytes and model payload.
            continue
        if (same_task and receipt["status"] == "ACCEPTED" and resp.get("http_status") not in range(200,300)
                or same_rejection and resp.get("http_status") != receipt.get("http_status")):
            return {"status":"CONFLICT", **info}
        if pinned and not plan_matches:
            # A wire digest identifies bytes, not the revision the owner intended to use.
            unconfirmed_binding = {"status":"BINDING_UNCONFIRMED", **info}
            continue
        if same_task and receipt["status"] == "ACCEPTED":
            return {"status":"MATCHED_RESPONSE", **info}
        elif same_rejection and resp.get("http_status") == receipt.get("http_status"):
            return {"status":"MATCHED_RESPONSE", **info}
        elif not resp:
            matches.append({"status":"REQUEST_ONLY", **info})
        else:
            # Multiple submissions can have identical wire bytes; match the receipt's task/request separately.
            matches.append({"status":"RESPONSE_UNCORRELATED", **info})
    return unconfirmed_binding or (matches[0] if matches else {"status":"NO_TRANSPORT_RECORD"})


def _candidate(job, candidate_id, reference, selections, exchange_cache):
    meta = job.get("metadata") or {}
    receipt = job.get("reference_receipt")
    source = meta.get("script_asset_ref")
    result = {"candidate_id": candidate_id, "shot_id": meta.get("shot_id"),
              "script_ref": {k: source.get(k) for k in ("asset_id","revision_id","sha256")} if isinstance(source,dict) else None,
              "project_sha256": meta.get("project_sha256"), "task_id": job.get("task_id"), "job_status": job.get("status"),
              "selected_in": selections, "required_refs": _matches(meta.get("required_refs"), reference),
              "submitted_refs": [], "evidence_status": "LEGACY_UNKNOWN", "trace_id": None}
    if receipt is None:
        return result
    from pianchang.video_agent import verify_reference_receipt
    check = verify_reference_receipt(job)
    tid = receipt.get("trace_id") if isinstance(receipt,dict) else None
    result["trace_id"] = tid if isinstance(tid, str) and re.fullmatch(r"[a-f0-9]{32}", tid) else None
    if check["status"] == "UNKNOWN":
        result.update(evidence_status="RECEIPT_UNCONFIRMED", receipt_validation="UNKNOWN")
        return result
    if not isinstance(receipt, dict) or check["status"] != "PASS":
        result.update(evidence_status="INVALID_RECEIPT", receipt_validation="FAIL")
        return result
    result["receipt_validation"] = "PASS"
    requirements = _matches(receipt.get("required_refs"), reference)
    if requirements:
        result["required_refs"] = requirements
    submitted = _matches(receipt.get("submitted_refs"), reference)
    prepared = _matches(receipt.get("prepared_refs"), reference)
    if not (result["required_refs"] or prepared or submitted):
        return None
    if any(pin != reference for pin in [*result["required_refs"], *prepared, *submitted]):
        result.update(evidence_status="INVALID_PIN", receipt_validation="FAIL")
        return result
    status = receipt["status"]
    wire = verify_reference_exchange(job, receipt, result["trace_id"], exchange_cache)
    result["http_evidence"] = wire
    if wire["status"] == "CONFLICT":
        result.update(evidence_status="INVALID_RECEIPT", receipt_validation="FAIL")
        return result
    if wire["status"] != "MATCHED_RESPONSE":
        result["receipt_status"] = status
        result["evidence_status"] = (wire["status"] if wire["status"] == "BINDING_UNCONFIRMED" else
                                     status if status in {"PREPARED","SUBMISSION_UNKNOWN"} else "TRANSPORT_UNCONFIRMED")
        return result
    result["evidence_status"] = status
    if status in {"ACCEPTED", "PROVIDER_REJECTED"}:
        result["submitted_refs"] = submitted
        if receipt.get("reference_bundle"):
            from pianchang.reference_inputs import verify_reference_bundle
            bundle=verify_reference_bundle(receipt["reference_bundle"],source_refs=meta["required_refs"],story_id=meta["reference_story_id"],
                project_sha256=meta["project_sha256"],shot_id=meta["shot_id"])
            result["derived_submitted_refs"]=[s["provider_ref"] for s in bundle["slots"] if reference in s["source_refs"] and s["provider_ref"]!=reference]
            result["reference_bundle"]=receipt["reference_bundle"]
    return result


@traced("reference.usage.query", kind="tool", capture_inputs=False)
def query_reference_usage(store, asset_id, revision_id, *, jobs_dir, story_id=None):
    """Attribute only exact pinned, checked references. Legacy jobs retain their uncertainty."""
    target, sources, direct_media = _snapshot(store, asset_id, revision_id, story_id)
    reference = {**_identity(target), **{k: target["content"].get(k)
        for k in ("media_sha256", "subject_id", "purpose")}}
    requirements, selected, project_hashes = [], {}, {}
    scripts = {(a["asset_id"], a["revision_id"], a["sha256"]) for a in sources if a["kind"] == "script"}
    for source in sources:
        _shot_requirements(source, reference, requirements, project_hashes)
        _selections(source, selected, reference, target["story_id"], direct_media)
    root = Path(jobs_dir).resolve()
    jobs = sorted({p.resolve() for p in root.glob("*/job.json") if p.resolve().is_relative_to(root)})
    results, unknown = [], []
    seen_selections, exchange_cache = set(), {}
    unreadable = 0
    for path in jobs:
        try:
            job = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            unreadable += 1
            continue
        selections = selected.get(path, [])
        if (not isinstance(job, dict) or not isinstance(job.get("metadata", {}), dict) or job.get("schema_version") not in {"bailian-video-job/1", "provider-video-job/2"}
                or not _belongs(job, target["story_id"], scripts, project_hashes, selections, reference, story_id)):
            continue
        confirmed = [row for row in selections if row["task_id"] and row["task_id"] == job.get("task_id")
                     and row["shot_id"] == job.get("metadata", {}).get("shot_id") and row["media_sha256"] == job.get("download_sha256")]
        if len(confirmed) == len(selections):
            seen_selections.add(path)
        item = _candidate(job, path.parent.name, reference, confirmed, exchange_cache)
        if item is None:
            continue
        if item.get("http_evidence", {}).get("status") == "MATCHED_RESPONSE":
            results.append(item)
        else:
            unknown.append(item)
    for path in selected.keys() - seen_selections:
        unknown.append({"candidate_id": path.parent.name, "shot_id": selected[path][0]["shot_id"],
                        "evidence_status": "JOURNAL_UNAVAILABLE", "selected_in": selected[path],
                        "required_refs": [], "submitted_refs": [], "trace_id": None})
    # Archived dependency edges remain useful when a candidate journal is lost.
    # A catalog edge alone does not establish that a provider received the file.
    archived = []
    for source in sources:
        if source["kind"] != "shot_media":
            continue
        edge = next((d for d in source["dependencies"] if d["role"] == "submitted_reference"
                     and d["asset_id"] == asset_id and d["revision_id"] == revision_id), None)
        if edge:
            archived.append({"media": _identity(source), "shot_id": edge.get("source_id"),
                             "task_id": source["content"].get("task_id"), "trace_id": source["trace_id"]})
    report = {"schema_version": "reference-usage/1", "story_id": target["story_id"], "query_story_id":story_id, "reference": reference,
              "required_shots": requirements, "candidates": results, "unverified_candidates": unknown,
              "archived_media_dependencies": archived,
              "coverage": {"journal_directory_exists": root.is_dir(), "unreadable_journals": unreadable},
              "submitted_count": sum(bool(c["submitted_refs"]) for c in results),
              "derived_submitted_count":sum(bool(c.get("derived_submitted_refs")) for c in results),
              "derived_accepted_count":sum(bool(c.get("derived_submitted_refs")) and c["evidence_status"]=="ACCEPTED" for c in results),
              "accepted_count": sum(bool(c["submitted_refs"]) and c["evidence_status"] == "ACCEPTED" for c in results)}
    event("reference.usage.result", {"asset_id": asset_id, "revision_id": revision_id,
        "required_records": len(requirements), "confirmed_submissions": report["submitted_count"],
        "accepted": report["accepted_count"], "unverified_candidates": len(unknown)})
    return report


@traced("cli.reference.usage", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description="按资产 ID 和确切版本查镜头要求、候选提交和供应商接受记录。")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--revision-id", required=True)
    parser.add_argument("--jobs-dir", type=Path, required=True, help="此制作使用的视频任务输出目录")
    parser.add_argument("--story-id", help="仅查询指定故事")
    args = parser.parse_args(argv)
    report = query_reference_usage(args.store, args.asset_id, args.revision_id, jobs_dir=args.jobs_dir, story_id=args.story_id)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
