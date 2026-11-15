"""Standalone tracing contract tests; no model/network/media services are used."""
import asyncio
import contextvars
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from trace_system import (TraceError, Tracer, current_context, event, get_tracer,
                          register_secret, traced, use_context)

ROOT = Path(__file__).resolve().parents[1]


@traced("sdk-test.add", kind="tool")
def add(a, b):
    event("addition.checked", {"a": a, "b": b}, kind="validation")
    return a + b


class TraceSystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tracer = Tracer(self.temp.name)

    def test_nested_spans_outputs_and_order_are_durable(self):
        with self.tracer.trace("task", inputs={"a": 2}) as task:
            self.assertEqual(current_context(), task.context)
            self.assertEqual(add(2, 3), 5)
            task.set_output({"sum": 5})
        data = Tracer(self.temp.name).get_trace(task.trace_id)
        self.assertEqual(data["status"], "OK")
        self.assertEqual(data["span_count"], 2)
        child = data["spans"][1]
        self.assertEqual(child["parent_span_id"], task.span_id)
        self.assertEqual(child["inputs"], {"a": 2, "b": 3})
        self.assertEqual(child["output"], 5)
        self.assertEqual(child["attributes"]["module"], __name__)
        self.assertEqual(len(child["attributes"]["source_sha256"]), 64)
        self.assertTrue(add.__trace_instrumented__)
        self.assertGreaterEqual(child["duration_ms"], 0)
        self.assertEqual([e["trace_seq"] for e in data["events"]], list(range(1, 6)))
        self.assertTrue(all(e["timestamp"].endswith("+00:00") for e in data["events"]))
        self.assertTrue(self.tracer.verify(task.trace_id)["ok"])
        self.assertIsNone(current_context())

    def test_exception_is_recorded_and_propagated_without_credentials(self):
        secret = "credential-offline-27836"
        self.tracer.register_secret(secret)
        with self.assertRaisesRegex(ValueError, "credential"):
            with self.tracer.trace("failure") as task:
                raise ValueError("bad credential " + secret)
        data = self.tracer.get_trace(task.trace_id)
        self.assertEqual(data["status"], "ERROR")
        error = data["spans"][0]["error"]
        self.assertEqual(error["type"], "ValueError")
        self.assertIn("ValueError", error["stack"])
        self.assertNotIn(secret, json.dumps(self.tracer.export_trace(task.trace_id)))

    def test_large_payload_is_content_addressed_and_exported(self):
        tracer = Tracer(self.temp.name, inline_bytes=256)
        payload = {"prompt": "真实长上下文" * 500}
        with tracer.trace("large", inputs=payload) as task:
            task.event("response", {"content": "回答" * 600})
            task.set_output(payload)
        detail = tracer.get_trace(task.trace_id)
        ref = detail["spans"][0]["inputs"]
        self.assertIn("$artifact", ref)
        self.assertEqual(tracer.resolve(ref), payload)
        export = tracer.export_trace(task.trace_id)
        self.assertGreater(len(export["artifacts"]), 0)
        self.assertTrue(export["integrity"]["ok"])

    def test_problem_spans_include_later_required_findings_even_when_execution_succeeded(self):
        with self.tracer.trace("offline navigation") as task:
            with self.tracer.span("completed preparation") as clean:
                clean.event("ok", {"status":"PASS"}, kind="validation")
                clean.event("optional comparison", {"status":"UNKNOWN", "required":False}, kind="validation")
            with self.tracer.span("review the first item") as first:
                first.event("uncertain first check", {"status":"UNKNOWN", "required":True}, kind="validation")
                first.event("second check failed", {"status":"FAIL"}, kind="validation")
            with self.tracer.span("review another item") as second:
                second.event("insufficient evidence", {"status":"UNKNOWN"}, kind="validation")
        first_page = self.tracer.get_trace(task.trace_id, limit=1)
        self.assertTrue(first_page["events_has_more"])
        self.assertEqual(first_page["status"], "OK")
        self.assertEqual([e["kind"] for e in first_page["events"]], ["span.start"])
        self.assertEqual(first_page["problem_spans"], [
            {"span_id":first.span_id, "status":"FAIL", "count":2},
            {"span_id":second.span_id, "status":"UNKNOWN", "count":1}])
        self.assertEqual(self.tracer.get_trace(task.trace_id, after=first_page["next_after"], limit=1)["problem_spans"],
                         first_page["problem_spans"])

    def test_problem_index_resolves_only_verified_redacted_validation_artifacts(self):
        tracer = Tracer(self.temp.name, inline_bytes=256)
        private = "offline-credential-for-review"
        tracer.register_secret(private)
        with tracer.trace("offline checks in artifacts") as task:
            with tracer.span("optional only") as optional:
                optional.event("optional failure", {"required":False, "status":"FAIL", "actual":"x"*900}, kind="validation")
            with tracer.span("required") as selected:
                selected.event("required evidence", {"required":True, "status":"UNKNOWN",
                    "actual": private + ("长证据" * 300)}, kind="validation")
        detail = tracer.get_trace(task.trace_id, limit=1)
        self.assertEqual(detail["problem_spans"], [{"span_id":selected.span_id, "status":"UNKNOWN", "count":1}])
        stored = next(e for e in tracer.get_events(task.trace_id) if e["name"]=="required evidence")["data"]
        self.assertIn("$artifact", stored)
        self.assertNotIn(private, json.dumps(tracer.resolve(stored)))
        self.assertTrue(tracer.verify(task.trace_id)["ok"])
        with closing(sqlite3.connect(tracer.path)) as db:
            db.execute("UPDATE artifacts SET content=? WHERE sha256=?", ('{"status":"PASS"}', stored["$artifact"]))
            db.commit()
        with self.assertRaises(TraceError):
            tracer.get_trace(task.trace_id, limit=1)
        self.assertFalse(tracer.verify(task.trace_id)["ok"])

    def test_redaction_before_storage_covers_json_strings_urls_media_and_headers(self):
        registered = "arbitrary-provider-credential-993"
        register_secret(registered)
        nested_secret = "hidden-raw-json-value"
        payload = {"Authorization": "Bearer token-a", "Cookie": "session=abc",
                   "api_key": "unregistered-key", "prompt_tokens": 12,
                   "body_text": json.dumps({"api_key": nested_secret, "image": "data:image/png;base64,AAAA", "normal": "hello"}),
                   "url": "https://user:pass@example.test/video?signature=signedsecret&token=t123&id=42",
                   "binary": b"never-store-binary-content", "text": registered + " sk-synthetic-key-sufficient-long",
                   "plain_text": "api_key=plain-secret-123 password: another-plain-secret",
                   "headers_text": "Set-Cookie: session=secret-cookie\nAuthorization: Bearer unregistered-value"}
        with self.tracer.trace("redaction") as task:
            task.event("http.response", payload, kind="http")
        export = self.tracer.export_trace(task.trace_id)
        encoded = json.dumps(export, ensure_ascii=False)
        for forbidden in (registered, nested_secret, "unregistered-key", "signedsecret", "t123", "user:pass", "AAAA", "never-store-binary-content", "secret-cookie", "unregistered-value", "plain-secret-123", "another-plain-secret"):
            self.assertNotIn(forbidden, encoded)
        self.assertIn("prompt_tokens", encoded)
        self.assertIn("inline_media", encoded)
        self.assertIn("REDACTED", encoded)
        # SQLite source contains no plaintext secrets, including its WAL.
        for path in Path(self.temp.name).glob("trace.sqlite3*"):
            contents = path.read_bytes()
            self.assertNotIn(registered.encode(), contents)
            self.assertNotIn(nested_secret.encode(), contents)

    def test_json_raw_text_is_preserved_when_no_redaction_is_required(self):
        text = '{ "choices": [\n  {"message": {"content":"hello"}}] }'
        self.assertEqual(self.tracer.sanitize(text), text)

    def test_open_span_is_unknown_and_recovery_appends_instead_of_overwriting(self):
        task = self.tracer.start_span("detached", kind="task", new_trace=True)
        original = self.tracer.get_events(task.trace_id)
        other = Tracer(self.temp.name)
        before = other.get_trace(task.trace_id)
        self.assertEqual(before["status"], "UNKNOWN")
        self.assertIsNone(before["end_time"])
        self.assertFalse(before["spans"][0]["end_recorded"])
        first_end = other.end_span(task.context, status="UNKNOWN", error={"reason": "supervisor confirmed previous worker interrupted"})
        repeated = other.end_span(task.context, status="OK", output={"false_success": True})
        self.assertEqual(first_end, repeated)
        after = other.get_events(task.trace_id)
        self.assertEqual(len(after), 2)
        self.assertEqual(after[0], original[0])
        self.assertEqual(other.get_trace(task.trace_id)["status"], "UNKNOWN")

    def test_thread_propagation_uses_explicit_context_and_isolated_roots(self):
        task = self.tracer.start_span("queued", kind="task", new_trace=True)
        def worker(index):
            with use_context(task.context, tracer=self.tracer):
                return add(index, 1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            result = list(pool.map(worker, range(12)))
        self.tracer.end_span(task.context, output=result)
        detail = self.tracer.get_trace(task.trace_id)
        self.assertEqual(detail["span_count"], 13)
        self.assertTrue(all(s["parent_span_id"] == task.span_id for s in detail["spans"][1:]))
        self.assertTrue(self.tracer.verify(task.trace_id)["ok"])
        self.assertIsNone(current_context())

    def test_none_context_binds_custom_store_and_context_copy_works(self):
        with use_context(None, tracer=self.tracer):
            self.assertIs(get_tracer(), self.tracer)
            self.assertIsNone(current_context())
            add(7, 9)
        self.assertEqual(len(self.tracer.list_traces()), 1)
        with self.tracer.trace("copy") as task:
            copied = contextvars.copy_context()
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(copied.run, add, 1, 1).result(), 2)
        self.assertEqual(self.tracer.get_trace(task.trace_id)["span_count"], 2)

    def test_store_mismatch_and_unknown_parent_are_rejected(self):
        task = self.tracer.start_span("root")
        with tempfile.TemporaryDirectory() as other_dir:
            other = Tracer(other_dir)
            with self.assertRaises(TraceError):
                with use_context(task.context, tracer=other):
                    pass
        unknown = {**task.context, "span_id": "f" * 32}
        with self.assertRaises(TraceError):
            self.tracer.start_span("child", parent=unknown)
        with self.assertRaises(TraceError):
            self.tracer.end_span(unknown)

    def test_async_function_records_actual_awaited_result_and_exception(self):
        @traced("async.test")
        async def async_call(fail=False):
            await asyncio.sleep(0)
            if fail:
                raise RuntimeError("async failure")
            return {"awaited": True}
        async def run():
            with self.tracer.trace("async-root") as task:
                result = await async_call()
                try:
                    await async_call(True)
                except RuntimeError:
                    pass
            return task.trace_id, result
        trace_id, result = asyncio.run(run())
        self.assertEqual(result, {"awaited": True})
        detail = self.tracer.get_trace(trace_id)
        self.assertEqual(detail["spans"][1]["output"], result)
        self.assertEqual(detail["spans"][2]["status"], "ERROR")
        self.assertEqual(detail["status"], "ERROR")

    def test_filters_and_pagination_do_not_silently_discard_events(self):
        with self.tracer.trace("needles", kind="task", attributes={"project": "test-project"}) as task:
            for index in range(8):
                task.event("iteration", {"i": index})
        with self.tracer.trace("http-poll", kind="http"):
            pass
        self.assertEqual(len(self.tracer.list_traces(kind="task", status="OK", search="needle")), 1)
        self.assertEqual(len(self.tracer.list_traces(offset=1, limit=1)), 1)
        page = self.tracer.get_trace(task.trace_id, limit=3)
        self.assertTrue(page["events_has_more"])
        collected = page["events"][:]
        while page["events_has_more"]:
            page = self.tracer.get_trace(task.trace_id, after=page["next_after"], limit=3)
            collected.extend(page["events"])
        self.assertEqual(len(collected), 10)
        self.assertEqual(len(self.tracer.export_trace(task.trace_id)["trace"]["events"]), 10)

    def test_span_pages_keep_exact_named_events_and_payloads(self):
        with self.tracer.trace("offline.span.pages") as task:
            with self.tracer.read_batch(), self.tracer.span("worker") as child:
                for i in range(7):
                    child.event("receipt", {"sequence": i, "evidence": "offline"*1200})
                    task.event("receipt", {"other": i})
                    child.event("other", {})
        all_rows = self.tracer.get_events(task.trace_id)
        expected = [r for r in all_rows if r["span_id"]==child.span_id and r["name"]=="receipt"]
        pages = []
        while True:
            page = self.tracer.get_events(task.trace_id, after=pages[-1]["trace_seq"] if pages else 0,
                                          limit=2, span_id=child.span_id, names=["receipt"])
            if not page:
                break
            pages.extend(page)
        self.assertEqual(pages, expected)
        self.assertEqual(len(pages), 7)
        self.assertEqual(self.tracer.get_events(task.trace_id, span_id=child.span_id),
                         [r for r in all_rows if r["span_id"]==child.span_id])
        self.assertEqual(self.tracer.get_events(task.trace_id, span_id=child.span_id, names=[]), [])

    def test_tampering_events_or_artifacts_is_detected(self):
        tracer = Tracer(self.temp.name, inline_bytes=256)
        with tracer.trace("integrity", inputs={"long": "value" * 300}) as task:
            task.event("one", {"valid": True})
        with closing(sqlite3.connect(tracer.path)) as db:
            db.execute("UPDATE events SET name='tampered' WHERE kind='event'")
            db.commit()
        report = tracer.verify(task.trace_id)
        self.assertFalse(report["ok"])
        self.assertIn("event_hash_mismatch", [i["issue"] for i in report["issues"]])
        with closing(sqlite3.connect(tracer.path)) as db:
            db.execute("UPDATE artifacts SET content='{}'")
            db.commit()
        self.assertIn("artifact_missing_or_corrupt", [i["issue"] for i in tracer.verify(task.trace_id)["issues"]])

    def test_invalid_ids_and_traversal_cannot_read_files(self):
        for candidate in ("../.env", ".env", "f" * 63, "f" * 65, "E:/private.json"):
            with self.assertRaises(KeyError):
                self.tracer.get_artifact(candidate)
        with self.assertRaises(KeyError):
            self.tracer.get_trace("../.env")
        with self.assertRaises(KeyError):
            self.tracer.get_trace("f" * 32)
        with self.assertRaises(TraceError):
            with use_context({"trace_id": 12, "span_id": [], "store_id": self.tracer.store_id}, tracer=self.tracer):
                pass

    def test_corrupt_event_json_returns_failed_integrity_report(self):
        with self.tracer.trace("corrupt-json") as task:
            pass
        with closing(sqlite3.connect(self.tracer.path)) as db:
            db.execute("UPDATE events SET data='{not-json' WHERE trace_id=?", (task.trace_id,))
            db.commit()
        report = self.tracer.verify(task.trace_id)
        self.assertFalse(report["ok"])
        self.assertEqual(report["issues"][0]["issue"], "event_json_corrupt")

    def test_before_send_recording_failure_prevents_decorated_operation(self):
        effects = []
        @traced("billable.fake")
        def billable():
            effects.append("called")
        with use_context(None, tracer=self.tracer), patch.object(self.tracer, "start_span", side_effect=TraceError("disk unavailable")):
            with self.assertRaises(TraceError):
                billable()
        self.assertEqual(effects, [])

    def test_objects_do_not_leak_repr_or_attributes(self):
        class Config:
            api_key = "do-not-inspect-attribute"
            def __repr__(self):
                return "do-not-call-repr"
        value = self.tracer.sanitize({"self": Config()})
        self.assertIn("python_type", value["self"])
        self.assertNotIn("do-not", json.dumps(value))

    def test_circular_input_is_rejected_without_partial_span(self):
        circular = {}
        circular["nested"] = circular
        with self.assertRaises(TraceError):
            self.tracer.start_span("bad", inputs=circular)
        self.assertEqual(self.tracer.list_traces(), [])

    def test_separate_program_and_concurrent_processes_use_same_store(self):
        task = self.tracer.start_span("process-root", new_trace=True)
        script = """import json,sys
from trace_system import Tracer,use_context,event
t=Tracer(sys.argv[1])
with use_context(json.loads(sys.argv[2]),tracer=t):
    with t.span('external-script') as s:
        for i in range(10): event('external.event', {'i':i})
"""
        environment = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
        processes = [subprocess.Popen([sys.executable, "-c", script, self.temp.name, json.dumps(task.context)],
                                      cwd=self.temp.name, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for _ in range(2)]
        for process in processes:
            out, err = process.communicate(timeout=40)
            self.assertEqual(process.returncode, 0, err.decode())
        self.tracer.end_span(task.context)
        self.assertEqual(self.tracer.get_trace(task.trace_id)["event_count"], 26)
        self.assertTrue(self.tracer.verify(task.trace_id)["ok"])

    def test_cli_export_and_verify_are_independent_of_application(self):
        with self.tracer.trace("cli") as task:
            task.event("fact", {"offline": True})
        environment = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
        args = [sys.executable, "-m", "trace_system", "--directory", self.temp.name]
        output = Path(self.temp.name) / "export.json"
        result = subprocess.run(args + ["export", task.trace_id, "--output", str(output)], cwd=self.temp.name,
                                env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["trace"]["trace_id"], task.trace_id)
        result = subprocess.run(args + ["verify", task.trace_id], cwd=self.temp.name, env=environment,
                                capture_output=True, text=True, encoding="utf-8", timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_cli_return_and_system_exit_status_reflect_exit_code(self):
        @traced("cli.return", kind="cli")
        def command(argv=None):
            return 2
        with use_context(None, tracer=self.tracer), patch.object(sys, "argv", ["test-cli", "--api-key", "hidden-cli-token"]):
            self.assertEqual(command(["input.json"]), 2)
        summary = self.tracer.list_traces()[0]
        detail = self.tracer.get_trace(summary["trace_id"])
        self.assertEqual(detail["status"], "ERROR")
        self.assertEqual(detail["spans"][0]["output"]["exit_code"], 2)
        self.assertEqual(detail["spans"][0]["inputs"]["argv"], ["input.json"])
        self.assertNotIn("hidden-cli-token", json.dumps(detail))
        for code, expected in ((None, "OK"), (0, "OK"), (3, "ERROR")):
            @traced("cli.exit", kind="cli")
            def exiting():
                raise SystemExit(code)
            with use_context(None, tracer=self.tracer), self.assertRaises(SystemExit):
                exiting()
            summary = self.tracer.list_traces()[0]
            self.assertEqual(summary["status"], expected)

    def test_header_pairs_and_relative_signed_urls_are_redacted(self):
        value = {"headers": [["Authorization", "Basic bm9uX3JlZ2lzdGVyZWRfc2VjcmV0"],
                              ["Cookie", "session=plain-cookie-value"], ["Content-Type", "application/json"]],
                 "url": "/video.mp4?Signature=private-sig-value&safe=keep",
                 "callback": "callback?token=plain-token-value&next=home",
                 "error": "HTTP 401: Authorization: Basic dXNlcjpwYXNz",
                 "xml": "<Error><Signature>plain-signature</Signature><Token>plain-token</Token></Error>"}
        with self.tracer.trace("headers-and-urls", inputs=value) as task:
            pass
        result = self.tracer.export_trace(task.trace_id)
        encoded = json.dumps(result)
        for secret in ("bm9uX3JlZ2lzdGVyZWRfc2VjcmV0", "plain-cookie-value", "private-sig-value", "plain-token-value", "dXNlcjpwYXNz", "plain-signature", "plain-token"):
            self.assertNotIn(secret, encoded)
        self.assertIn("application/json", encoded)
        self.assertIn("safe=keep", encoded)

    def test_user_artifact_shape_is_literal_but_internal_large_refs_still_work(self):
        tracer = Tracer(self.temp.name, inline_bytes=256)
        literal = {"$artifact": "a" * 64, "bytes": 10, "media_type": "application/json"}
        inputs = {"literal": literal, "large": "large-content" * 200}
        with tracer.trace("literal-and-large", inputs=inputs) as task:
            task.event("literal-event", literal)
            task.set_output(inputs)
        detail = tracer.get_trace(task.trace_id)
        self.assertIn("$artifact", detail["spans"][0]["inputs"])
        expanded = tracer.resolve(detail["spans"][0]["inputs"])
        self.assertEqual(expanded["literal"]["$literal_reason"], "user_artifact_shape")
        self.assertEqual(tracer.resolve(expanded["literal"]), literal)
        self.assertTrue(tracer.verify(task.trace_id)["ok"])
        exported = tracer.export_trace(task.trace_id)
        self.assertNotIn("a" * 64, exported["artifacts"])
        self.assertGreater(len(exported["artifacts"]), 0)

    def test_data_uri_dictionary_key_is_a_safe_string_not_unhashable(self):
        value = self.tracer.sanitize({"data:image/png;base64,AAAA": "ordinary-value"})
        self.assertEqual(len(value), 1)
        key = next(iter(value))
        self.assertIsInstance(key, str)
        self.assertNotIn("AAAA", key)
        self.assertIn("inline_media", key)
        with self.tracer.trace("odd-key", inputs={"data:image/png;base64,AAAA": "ordinary-value"}) as task:
            pass
        self.assertTrue(self.tracer.verify(task.trace_id)["ok"])

    def test_trace_listing_prefilters_kind_name_id_and_attributes_in_sql(self):
        for index in range(12):
            with self.tracer.trace("GET /poll", kind="http", attributes={"request": index}):
                pass
        with self.tracer.trace("Ä task", kind="task", attributes={"project_id": "specific-project"}) as task:
            pass
        with patch.object(self.tracer, "_trace_summary", wraps=self.tracer._trace_summary) as lookup:
            found = self.tracer.list_traces(kind="task")
            self.assertEqual(len(found), 1)
            self.assertEqual(lookup.call_count, 1)
        for search in ("ä task", "specific-project", task.trace_id):
            with patch.object(self.tracer, "_trace_summary", wraps=self.tracer._trace_summary) as lookup:
                self.assertEqual(len(self.tracer.list_traces(search=search)), 1)
                self.assertEqual(lookup.call_count, 1)

    def test_trace_listing_keeps_artifact_backed_root_metadata_candidates(self):
        tracer = Tracer(self.temp.name, inline_bytes=256)
        with tracer.trace("large-root", kind="task", attributes={"note": "prefix " * 200 + "find-this-marker"}) as task:
            pass
        self.assertEqual(tracer.list_traces(kind="task", search="find-this-marker")[0]["trace_id"], task.trace_id)

    def test_trace_listing_does_not_load_child_payloads_or_event_pages(self):
        tracer = Tracer(self.temp.name, inline_bytes=256)
        with tracer.trace("offline-list-metadata", kind="task") as task:
            for index in range(6):
                with tracer.span("model-child", kind="model", inputs={"prompt": "context" * 300},
                                 attributes={"child_note": "metadata" * 200}) as child:
                    child.set_output({"response": "result" * 400})
                    child.event("large-response", {"body": "result" * 500})
        detail = tracer.get_trace(task.trace_id)
        unused_artifacts = {e["data"]["$artifact"] for e in tracer._all_events(task.trace_id)
                            if e["kind"] == "span.start" and e["parent_span_id"] is not None and "$artifact" in e["data"]}
        for child in detail["spans"][1:]:
            for field in ("inputs", "output", "attributes"):
                if isinstance(child[field], dict) and "$artifact" in child[field]:
                    unused_artifacts.add(child[field]["$artifact"])
        with patch.object(tracer, "get_trace", side_effect=AssertionError("list must not materialize a trace")), \
             patch.object(tracer, "get_events", side_effect=AssertionError("list must not load event pages")), \
             patch.object(tracer, "get_artifact", wraps=tracer.get_artifact) as lookup:
            summaries = tracer.list_traces(kind="task")
        self.assertEqual(summaries[0]["span_count"], 7)
        self.assertEqual(summaries[0]["event_count"], detail["event_count"])
        self.assertTrue(unused_artifacts)
        self.assertFalse(unused_artifacts.intersection(call.args[0] for call in lookup.call_args_list))
        self.assertTrue(tracer.verify(task.trace_id)["ok"])

    def test_trace_listing_summary_matches_full_detail_for_errors_unknowns_and_pagination(self):
        tracer = Tracer(self.temp.name, inline_bytes=256)
        with tracer.trace("completed-ok"):
            pass
        root = tracer.start_span("closed-root-open-child", kind="task", new_trace=True)
        tracer.start_span("still-open", parent=root.context)
        tracer.end_span(root.context, status="OK")
        root = tracer.start_span("error-wins-over-open", kind="task", new_trace=True)
        child = tracer.start_span("failed", parent=root.context)
        tracer.end_span(child.context, status="ERROR", output={"body": "x" * 1000}, error={"message": "y" * 1000})
        tracer.start_span("also-open", parent=root.context)
        tracer.end_span(root.context, status="OK")
        unknown = tracer.start_span("explicit-unknown", kind="task", new_trace=True)
        tracer.end_span(unknown.context, status="UNKNOWN", output={"body": "x" * 1000}, error={"message": "y" * 1000})
        tracer.start_span("open-root", kind="task", new_trace=True)
        summaries = tracer.list_traces()
        for summary in summaries:
            detail = tracer.get_trace(summary["trace_id"])
            self.assertEqual(summary, {k: v for k, v in detail.items()
                                      if k not in {"spans", "problem_spans", "events", "events_has_more", "next_after"}})
        self.assertEqual([s["name"] for s in tracer.list_traces(status="ERROR")], ["error-wins-over-open"])
        self.assertEqual(len(tracer.list_traces(status="UNKNOWN")), 3)
        self.assertEqual(tracer.list_traces(limit=2, offset=1), summaries[1:3])
        self.assertEqual(tracer.list_traces(status="UNKNOWN", limit=1, offset=1),
                         [s for s in summaries if s["status"] == "UNKNOWN"][1:2])

    def test_trace_listing_still_checks_integrity_of_packed_status_metadata(self):
        tracer = Tracer(self.temp.name, inline_bytes=256)
        root = tracer.start_span("packed-end", kind="task", new_trace=True)
        ending = tracer.end_span(root.context, status="ERROR", output={"body": "x" * 1000}, error={"message": "y" * 1000})
        self.assertIn("$artifact", ending["data"])
        with tracer._db() as db:
            db.execute("UPDATE artifacts SET content=? WHERE sha256=?", ('{"status":"OK"}', ending["data"]["$artifact"]))
        with self.assertRaisesRegex(TraceError, "integrity"):
            tracer.list_traces()

    def test_large_plain_provider_text_does_not_trigger_quadratic_url_scan(self):
        # A captured response can contain one very long token. Bound this in a
        # subprocess so a future pathological regex fails rather than hangs CI.
        script = """import sys
from trace_system import Tracer
t=Tracer(sys.argv[1])
text='x'*262144
assert t.sanitize({'body_text':text})['body_text']==text
print('bounded large-text sanitization passed')
"""
        result = subprocess.run([sys.executable, "-c", script, self.temp.name], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
