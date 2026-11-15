"""Read-only verification of the two-revision red-carpet commission against its actual evidence."""
from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path

from pianchang.acceptance import file_sha256
from pianchang.collab_store import CollabStore
from pianchang.collaboration_workflow import HOME_DIR, SOURCE, ref
from pianchang.story_agent import _write_json
from pianchang.story_spec import digest_json, index_sources, validate_compilation
from trace_system import TraceError, current_context, get_tracer, traced
from pianchang.video_collab_agent import analyze_impact


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


class ReadCatalog(CollabStore):
    """Use the store's checksum and edge readers on a single SQLite read-only snapshot."""
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def _db(self, write=False):
        if write:
            raise ValueError("审计连接只允许读取。")
        yield self.connection


def _changed_beat(before, after, beat_id):
    """Allow exactly one text replacement and one screenplay version increment."""
    expected = copy.deepcopy(before)
    expected["script_version"] += 1
    target = index_sources(after)[beat_id]
    if target["kind"] != "beat" or target["text"] == index_sources(before)[beat_id]["text"]:
        return False
    for episode in expected["episodes"]:
        for scene in episode["scenes"]:
            for beat in scene["beats"]:
                if beat["id"] == beat_id:
                    beat["text"] = target["text"]
    return expected == after


def _feedback_verified(events, resolve):
    """Check the next request following every real tool result, not just the saved final history."""
    count = 0
    pending = []
    for event in events:
        if event["name"] == "agent.loop.tool_result":
            pending.append(resolve(event["data"]))
        if event["name"] == "agent.loop.model_requested" and pending:
            content = resolve(event["data"])["messages"]
            observed = []
            for message in content:
                if message["role"] != "user" or not message["content"].lstrip().startswith("{"):
                    continue
                try:
                    value = json.loads(message["content"])
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("type") == "tool_result":
                    observed.append(value)
            if not all(result in observed for result in pending):
                return 0
            count += len(pending)
            pending.clear()
    return 0 if pending else count


class Audit:
    def __init__(self, store, folder, index, beat, source):
        self.store, self.folder, self.index = store, folder, index
        self.story = "story-" + folder.name
        self.beat, self.source = beat, Path(source)
        self.checks, self.trace_results, self.media = [], [], {}
        self.chain = []
        self.proposals = []
        self.submitted = {}
        self.tracer = get_tracer()

    def check(self, path, expected, actual):
        self.checks.append({"path": path, "expected": expected, "actual": actual,
                            "status": "PASS" if expected == actual else "FAIL"})

    def get(self, suffix, revision):
        return self.store.get_asset(self.story + "/" + suffix, revision)

    def trace_evidence(self, identifier, label, *, require_success=True):
        export = self.tracer.export_trace(identifier)
        record = export["trace"]
        integrity = export["integrity"]
        self.check(label + ".trace_integrity", True, integrity["ok"])
        self.check(label + ".trace_open_spans", 0, record["open_span_count"])
        if require_success:
            self.check(label + ".trace_status", "OK", record["status"])
        summary = {"name": record["name"], "stage": label, "trace_id": identifier,
                   "integrity": integrity["ok"], "open_spans": record["open_span_count"],
                   "checked_events": integrity["checked_events"], "trace_status": record["status"]}
        summary["edge_review_spans"] = [
            {"span_id": span["span_id"], "output_sha256": digest_json(self.tracer.resolve(span["output"]))}
            for span in record["spans"] if span["name"] == "video.collab.edge_reviews" and span["status"] == "OK"
        ]
        self.trace_results.append(summary)
        if record["name"] == "video.reconcile_local_delivery":
            network = [s["name"] for s in record["spans"] if s["name"].startswith(("bailian.", "agent.loop.decide"))]
            self.check(label + ".恢复无模型或视频调用", [], network)
            summary["restored_from_trace_ids"] = [link["trace_id"] for link in record["links"]]
            summary["network_calls"] = len(network)
        if record["name"] not in {"agent.task.video", "agent.task.script"}:
            return
        spans = record["spans"]
        decides = [span for span in spans if span["name"] == "agent.loop.decide"]
        model_ids = []
        resolve = self.tracer.resolve
        for decide in decides:
            model = next(s for s in spans if s["parent_span_id"] == decide["span_id"]
                         and s["name"] == "bailian.text.complete_json")
            req = resolve(next(e for e in model["events"] if e["name"] == "http.request")["data"])
            res = resolve(next(e for e in model["events"] if e["name"] == "http.response")["data"])
            output = resolve(model["output"])
            self.check(label + ".actual_model_input", digest_json(resolve(decide["inputs"])["messages"]),
                       digest_json(req["body"]["messages"]))
            if model["status"] != "OK":
                summary.setdefault("provider_errors", []).append({"http_status": res["http_status"],
                    "code": res.get("body", {}).get("code") or (res.get("body", {}).get("error") or {}).get("code"),
                    "error": (model.get("error") or {}).get("message")})
                if require_success:
                    self.check(label + ".model_http_success", True, 200 <= res["http_status"] < 300)
                continue
            self.check(label + ".model_http_success", True, 200 <= res["http_status"] < 300)
            self.check(label + ".provider_request_id", True,
                       bool(output["request_id"]) and output["request_id"] in
                       [res.get("body_id"), res.get("body_request_id"), res.get("header_request_id")])
            model_ids.append(output["request_id"])
        self.check(label + ".real_model_decisions", True, bool(model_ids))
        feedback = _feedback_verified(record["events"], resolve)
        self.check(label + ".tool_feedback_next_request", True, feedback > 0)
        summary.update(model_request_ids=model_ids, feedback_steps=feedback)
        if record["name"] == "agent.task.video":
            submits = []
            for submission in (s for s in spans if s["name"] == "bailian.video.submit" and s["status"] == "OK"):
                task_id = resolve(submission["output"])["output"]["task_id"]
                http = next(s for s in spans if s["parent_span_id"] == submission["span_id"] and s["name"] == "bailian.video.http")
                returned = resolve(next(e for e in http["events"] if e["name"] == "http.response")["data"])
                self.check(label + ".供应商HTTP提交结果", [True, task_id],
                           [200 <= returned["http_status"] < 300, returned["body"]["output"]["task_id"]])
                submits.append(task_id)
            summary["submitted_video_task_ids"] = sorted(submits)
            self.submitted[identifier] = sorted(submits)

    def traces(self):
        # A successful continuation delivers a new linked result; the old root keeps its error.
        self.trace_evidence(self.index["trace_id"], "协作总任务", require_success=False)
        if self.index.get("continuation_trace_id"):
            self.trace_evidence(self.index["continuation_trace_id"], "后续协作任务")
        self.check("协作状态", "completed", self.index["status"])
        for stage in self.index["stages"]:
            key = stage["name"] + "." + stage["revision"]
            excepted = stage["status"] in {"blocked", "failed", "error"}
            self.trace_evidence(stage["trace_id"], key, require_success=not excepted)
        for rev in ("r1", "r2"):
            names = sorted(r["name"] for r in self.trace_results
                           if r["stage"].endswith("." + rev) and r["name"] in {"agent.task.script", "agent.task.video"})
            self.check(rev + ".独立Agent根", ["agent.task.script", "agent.task.video"], names)

    def scripts(self):
        template = self.get("source-project", "r1")
        original = _read(self.source)
        self.check("原始工程.内容哈希", digest_json(original), template["sha256"])
        digital_source = _read(self.source.with_name("script.json"))
        self.source_hashes = {"project_file_sha256": file_sha256(self.source),
                              "script_file_sha256": file_sha256(self.source.with_name("script.json")),
                              "project_content_sha256": template["sha256"]}
        self.check("原始剧本.工程来源绑定", digest_json(digital_source), original["script_source"]["sha256"])
        first = self.get("script", "r1")
        self.check("r1.保留冻结剧本", digest_json(digital_source), first["sha256"])
        self.script_results = {}
        self.check_script_package(first)
        second = self.get("script", "r2")
        self.check("r2.父版本", "r1", second["parent_revision"])
        self.check("r2.仅目标节拍修改", True, _changed_beat(first["content"], second["content"], self.beat))
        self.changed = {"beat_id": self.beat, "before": index_sources(first["content"])[self.beat]["text"],
                        "after": index_sources(second["content"])[self.beat]["text"]}
        self.check_script_package(second)

    def check_script_package(self, asset):
        rev = asset["revision_id"]
        folder = self.folder / ("script-" + rev)
        self.check(rev + ".发布目录剧本", asset["sha256"], digest_json(_read(folder / "published-script.json")))
        self.check(rev + ".发布目录检查", "PASS", _read(folder / "script-validation.json")["status"])
        self.script_results[rev] = ref(asset)

    def message_receipts(self, message, actor, expected, rev):
        statuses = [r["status"] for r in self.store.receipts(message["message_id"]) if r["agent"] == actor]
        if expected is not None:
            self.check(rev + "." + message["message_type"] + ".回执", expected, statuses)
        self.chain.append({"revision": rev, "type": message["message_type"], "message_id": message["message_id"],
                           "cause": message["causation_id"], "receipts": statuses, "trace_id": message["trace_id"]})

    def messages(self, rev):
        messages = self.store.messages(self.folder.name)
        script = self.get("script", rev)
        publication = self.store.get_message(script["event_id"])
        request, = [m for m in messages if m["message_type"] == "production.requested"
                    and m["payload"]["script"] == ref(script)]
        self.check(rev + ".发布事件类型", "script.revision_published", publication["message_type"])
        if rev == "r1":
            self.check(rev + ".发布原因", None, publication["causation_id"])
        else:
            cause = self.store.get_message(publication["causation_id"])
            self.check(rev + ".改稿因果", True, cause["message_type"] in {"production.result","script.change_requested"})
            if cause["message_type"] == "script.change_requested":
                self.message_receipts(cause, "script", ["received","accepted","completed"], rev)
                cause = self.store.get_message(cause["causation_id"])
            self.check(rev + ".首版结果反馈", ref(self.get("production","r1")), cause["payload"]["production"])
        self.check(rev + ".委托来源", publication["message_id"], request["causation_id"])
        self.message_receipts(publication, "video", ["received","completed"], rev)
        result, = [m for m in messages if m["message_type"] == "production.result"
                   and m["causation_id"] == request["message_id"]]
        if "production" not in result["payload"]:
            self.message_receipts(request, "video", ["received","accepted","unknown"], rev)
            self.message_receipts(result, "script", None, rev)
            recovery, = [m for m in messages if m["message_type"] == "production.recovery_requested"
                         and m["causation_id"] == result["message_id"]]
            self.check(rev + ".恢复委托无云端额度", 0, recovery["payload"]["max_video_generations"])
            self.check(rev + ".恢复来源委托", request["message_id"], recovery["payload"]["original_request_id"])
            self.message_receipts(recovery, "video", ["received","accepted","completed"], rev)
            result, = [m for m in messages if m["message_type"] == "production.result"
                       and m["causation_id"] == recovery["message_id"]]
            self.check(rev + ".恢复结果新调用", 0, result["payload"]["new_cloud_calls"])
        else:
            self.message_receipts(request, "video", ["received","accepted","completed"], rev)
        production = self.get("production", rev)
        self.check(rev + ".结果绑定", ref(production), result["payload"]["production"])
        self.check(rev + ".结果剧本", ref(script), result["payload"]["script"])
        expected = ["received","completed"] if self.index["status"] == "completed" else None
        self.message_receipts(result, "script", expected, rev)

    def change_request(self):
        for msg in self.store.messages(self.folder.name, message_type="script.change_requested"):
            ref_data = msg["payload"]["proposal"]
            plan = self.store.get_asset(ref_data["asset_id"], ref_data["revision_id"])
            self.check("拟改.资产哈希", ref_data["sha256"], plan["sha256"])
            self.check("拟改.授权节拍", self.beat, plan["content"]["beat"])
            self.proposals.append({"message_id": msg["message_id"], "cause": msg["causation_id"],
                "delivery": msg["deliveries"].get("script"), "proposal": plan["content"]})

    def _checked_edges(self, rev, data, impact, producer_trace_id):
        rows = data["edge_reviews"]
        pairs = [[r["from"], r["to"]] for r in rows]
        self.check(rev + ".衔接实际检查范围", sorted(impact["recheck_edges"]), sorted(pairs))
        spans = [s for trace in self.trace_results if trace["trace_id"] == producer_trace_id
                 for s in trace["edge_review_spans"] if s["output_sha256"] == digest_json(rows)]
        self.check(rev + ".衔接结果绑定实际执行Trace", True, bool(spans))
        project = data["project"]
        states = {s["shot_id"]: s for s in validate_compilation(project)["state_trace"]}
        order = [s["id"] for s in project["shots"]]
        edges = []
        for row in rows:
            left, right = row["from"], row["to"]
            key = f"{rev}.{left}→{right}"
            adjacent = (left, right) in zip(order, order[1:])
            after, before = states[left]["after"], states[right]["before"]
            status = ("PASS" if after == before else "FAIL") if adjacent else "NOT_ADJACENT"
            self.check(key + ".选用媒体", [data["shots"][s]["media_sha256"] for s in (left, right)], row["media"])
            self.check(key + ".目标版本", data["script_ref"], row["script_ref"])
            self.check(key + ".导出绑定", data["preview"]["preview_path"], row["preview"])
            self.check(key + ".状态比较", {"path":f"{left}.after/{right}.before", "expected":after,
                       "actual":before, "status":status}, row["state_checks"])
            edges.append({"from":left, "to":right, "story_state":status, "media_sha256":row["media"],
                          "audiovisual_continuity":row["audiovisual_continuity"], "trace_id":producer_trace_id,
                          "span_ids":[span["span_id"] for span in spans]})
        return edges

    def media_version(self, rev, previous=None):
        script, asset = self.get("script", rev), self.get("production", rev)
        data = asset["content"]
        self.check(rev + ".成片来源", ref(script), data["script_ref"])
        self.check(rev + ".制作工程剧本", script["sha256"], digest_json(data["project"]["script_source"]["script"]))
        base = previous["content"] if previous else None
        impact = analyze_impact(data["project"], base["project"] if base else None)
        self.check(rev + ".独立依赖分析", digest_json(impact), digest_json(data["impact"]))
        if base:
            self.check(rev + ".变更来源", [self.beat], impact["changed_sources"])
            self.check(rev + ".局部更新", True, bool(impact["regenerate"] and impact["reuse"]))
        rows, new_task_ids = {}, []
        package_deps = {("target_script", script["asset_id"], rev)}
        for sid, clip in data["shots"].items():
            target = rev + "." + sid
            op = "reused" if sid in impact["reuse"] else "generated"
            self.check(target + ".操作", op, clip["operation"])
            self.check(target + ".素材文件", clip["media_sha256"], file_sha256(clip["media_path"]))
            job = _read(clip["manifest_path"])
            self.check(target + ".供应商任务记录", clip["generation_job_sha256"], digest_json(job))
            task_id = clip["task_id"]
            self.check(target + ".供应商任务ID", [task_id, task_id, "SUCCEEDED", "aliyun-bailian"],
                       [job["task_id"], job["provider_response"]["output"]["task_id"], job["status"], job["provider"]])
            self.check(target + ".下载绑定", clip["media_sha256"], job["download_sha256"])
            self.check(target + ".生成输入依赖", impact["shots"][sid]["target"]["sha256"], job["metadata"]["dependency_sha256"])
            media_ref, check_ref = data["media_refs"][sid], data["check_refs"][sid]
            media = self.store.get_asset(media_ref["asset_id"], media_ref["revision_id"])
            check = self.store.get_asset(check_ref["asset_id"], check_ref["revision_id"])
            self.check(target + ".素材资产", media_ref, ref(media))
            self.check(target + ".素材绑定", clip["media_sha256"], media["content"]["media_sha256"])
            origin = clip["origin_script_ref"]
            source_ids = sorted(impact["shots"][sid]["target"]["source_ids"])
            actual_deps = sorted(d.get("source_id") for d in media["dependencies"] if d["asset_id"] == origin["asset_id"]
                                 and d["revision_id"] == origin["revision_id"] and d["role"] == "script_source")
            self.check(target + ".素材来源对象依赖", source_ids, actual_deps)
            self.check(target + ".验收资产", check_ref, ref(check))
            evidence = data["reports"][sid]
            self.check(target + ".验收内容", digest_json(evidence), digest_json(check["content"]))
            package_deps.update({("selected_media", media_ref["asset_id"], media_ref["revision_id"]),
                                 ("verification", check_ref["asset_id"], check_ref["revision_id"])})
            report = _read(evidence["report_path"])
            self.check(target + ".报告文件", evidence["report_sha256"], digest_json(report))
            self.check(target + ".验收绑定", [clip["media_sha256"], digest_json(data["project"]), sid],
                       [report["media_sha256"], report["project_sha256"], report["shot_id"]])
            dep_set = {(d["role"], d["asset_id"], d["revision_id"]) for d in check["dependencies"]}
            self.check(target + ".验收依赖", True, ("examined_media", media_ref["asset_id"], media_ref["revision_id"]) in dep_set
                       and ("target_script", script["asset_id"], rev) in dep_set)
            self.check(target + ".素材来源版本", clip["origin_script_ref"], job["metadata"]["script_asset_ref"])
            if op == "reused":
                old = base["shots"][sid]
                self.check(target + ".复用原件", [base["media_refs"][sid], old["task_id"], old["media_sha256"]],
                           [media_ref, task_id, clip["media_sha256"]])
            else:
                new_task_ids.append(task_id)
                self.check(target + ".生成来源", ref(script), clip["origin_script_ref"])
                if base:
                    self.check(target + ".新任务及新视频", True, task_id != base["shots"][sid]["task_id"]
                               and clip["media_sha256"] != base["shots"][sid]["media_sha256"])
            rows[sid] = {"operation": op, "provider_task_id": task_id, "media_sha256": clip["media_sha256"],
                         "report_status": report["status"], "unresolved": evidence["unresolved"]}
        actual_package_deps = {(d["role"], d["asset_id"], d["revision_id"]) for d in asset["dependencies"]}
        self.check(rev + ".制作包依赖", sorted(package_deps), sorted(actual_package_deps))
        video_stage, = [s for s in self.index["stages"] if s["revision"] == rev and s["trace_id"] in self.submitted]
        self.check(rev + ".真实视频提交ID", sorted(new_task_ids), self.submitted[video_stage["trace_id"]])
        preview = data["preview"]
        self.check(rev + ".预览文件", preview["preview_sha256"], file_sha256(preview["preview_path"]))
        export = _read(preview["manifest_path"])
        binding = [(s["id"], rows[s["id"]]["media_sha256"]) for s in data["project"]["shots"]]
        self.check(rev + ".预览时间线", binding, [(c["shot_id"],c["sha256"]) for c in export["clips"]])
        self.check(rev + ".预览清单", ["PREVIEW_ONLY", preview["preview_sha256"], False],
                   [export["status"], export["preview_sha256"], export["accepted"]])
        accepted = all(_read(r["report_path"])["task_complete"] for r in data["reports"].values())
        self.check(rev + ".媒体验收结论", accepted, data["media_accepted"])
        edges = self._checked_edges(rev, data, impact, asset["trace_id"])
        self.media[rev] = {"production_ref": ref(asset), "regenerated": impact["regenerate"], "reused": impact["reuse"],
                           "changed_sources": impact["changed_sources"], "recheck_edges": impact["recheck_edges"],
                           "edge_reviews": edges, "shots": rows, "preview": preview["preview_path"], "preview_sha256": preview["preview_sha256"],
                           "media_accepted": accepted}
        recovery = data.get("recovery")
        if recovery:
            trace_record, = [r for r in self.trace_results if r["trace_id"] == recovery["recovery_trace_id"]]
            source_trace = recovery["source_run_trace_id"]
            self.check(rev + ".恢复根与原执行关联", True,
                       trace_record["name"] == "video.reconcile_local_delivery" and
                       source_trace in trace_record["restored_from_trace_ids"] and source_trace in self.submitted)
            self.check(rev + ".本地恢复无云端调用", [0,0,0],
                       [recovery["model_calls"], recovery["video_submissions"], trace_record["network_calls"]])
            history = Path(recovery["source_directory"])
            for filename, sha in recovery["source_files"].items():
                self.check(rev + ".原执行记录." + filename, sha, digest_json(_read(history / filename)))
            self.media[rev]["recovery"] = {"source_trace_id": source_trace, "recovery_trace_id": trace_record["trace_id"],
                "source_loop_status": recovery["source_loop_status"], "new_model_calls": recovery["model_calls"],
                "new_video_submissions": recovery["video_submissions"],
                "verified_prior_reports": recovery["verified_prior_reports"], "new_reports": recovery["new_reports"]}
        return asset


@traced("collaboration.audit", kind="validation")
def audit_collaboration(directory, run_id, *, beat="B003", source=SOURCE):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or not re.fullmatch(r"B[0-9]{3}", beat):
        raise ValueError("协作 ID 须为 32 位十六进制，节拍 ID 须采用 B003 格式。")
    directory = Path(directory).resolve()
    folder = directory / "runs" / run_id
    index = _read(folder / "overview.json")
    with closing(sqlite3.connect((directory / "assets.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        audit = Audit(ReadCatalog(db), folder, index, beat, source)
        for label, operation in (("Trace", audit.traces), ("剧本", audit.scripts), ("改稿委托", audit.change_request),
                                 ("首版通信", lambda: audit.messages("r1")), ("改稿通信", lambda: audit.messages("r2")),
                                 ("首次制作", lambda: audit.media_version("r1")),
                                 ("改稿制作", lambda: audit.media_version("r2", audit.get("production","r1")))):
            try:
                operation()
            except TraceError:
                raise
            except (KeyError, ValueError, OSError, TypeError, IndexError, StopIteration, sqlite3.Error) as exc:
                audit.check(label + ".证据可读", True, f"{type(exc).__name__}: {exc}")
        failures = [row for row in audit.checks if row["status"] != "PASS"]
        versions = audit.media.keys()
        delivered = all(rev in versions for rev in ("r1", "r2"))
        report = {"status": "FAIL" if failures else "PASS", "run_id": run_id, "workflow_trace_id": index["trace_id"],
                  "completion_status": "COMPLETE" if delivered and index["status"] == "completed" and not failures else "INCOMPLETE",
                  "workflow_status": index["status"],
                  "deliverables": {rev: ("AVAILABLE" if rev in versions else "MISSING") for rev in ("r1", "r2")},
                  "audit_trace_id": current_context()["trace_id"], "checks_total": len(audit.checks), "failures": failures,
                  "source": getattr(audit, "source_hashes", {}), "beat": getattr(audit, "changed", {}),
                  "published_scripts": getattr(audit, "script_results", {}), "change_requests": audit.proposals,
                  "messages": audit.chain, "traces": audit.trace_results, "media": audit.media,
                  "checks": audit.checks}
    path = folder / "evidence.json"
    if path.is_file():
        old = _read(path)
        _write_json(folder / ("evidence-" + digest_json(old) + ".json"), old)
    _write_json(path, report)
    return report


@traced("cli.collab_audit", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description="离线核对两个 Agent、已发布版本、媒体与 Trace 的协作证据。")
    parser.add_argument("--directory", type=Path, default=HOME_DIR)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--beat", default="B003")
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args(argv)
    try:
        report = audit_collaboration(args.directory, args.run_id, beat=args.beat, source=args.source)
    except TraceError:
        raise
    except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"验收": "FAIL", "原因": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"证据验收": report["status"], "跨版本交付": report["completion_status"],
                      "版本产物": report["deliverables"], "检查项": report["checks_total"], "失败项": report["failures"],
                      "证据文件": str(args.directory.resolve() / "runs" / args.run_id / "evidence.json"),
                      "审计Trace": report["audit_trace_id"]}, ensure_ascii=False, indent=2))
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
