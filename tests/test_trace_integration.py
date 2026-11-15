"""Offline trace contracts across the real workbench, compiler and HTTP server."""
import base64
import copy
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.app_service import AppService
from pianchang.bailian_text import TextConfig
from pianchang.story_agent import StoryRunner
from pianchang.story_spec import StoryError
from trace_system import TraceError, Tracer, current_context, event, span, use_context
from pianchang.web_server import create_server
from tests.test_story_agent import FakeClient, draft_from_script
from tests.test_reference_assets import offline_png

ROOT = Path(__file__).resolve().parents[1]


class TraceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "traces"
        self.environment = patch.dict(os.environ, {"AGENT_TRACE_DIR": str(self.directory)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.tracer = Tracer(self.directory)

    def service(self):
        (self.root / ".env").write_text("", encoding="utf-8")
        service = AppService(self.root)
        self.addCleanup(service.close)
        return service

    def server(self, service):
        server = create_server(service, port=0)
        # server_close must join request handlers before disposing their temporary trace store.
        server.daemon_threads = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(stop)
        return server.server_address[1]

    def request(self, port, method, target, body=None, extra_headers=None):
        headers = {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
        headers.update(extra_headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            connection.request(method, target, body=None if body is None else json.dumps(body), headers=headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), json.loads(payload)
        finally:
            connection.close()

    def wait_job(self, service, jid):
        deadline = time.monotonic() + 60  # Full offline background work; page timing is tested separately.
        while time.monotonic() < deadline:
            job = next(j for j in service.list_jobs() if j["id"] == jid)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(.02)
        self.fail("Offline job did not finish")

    def test_workspace_snapshot_contains_real_story_scoped_data_without_dispatch(self):
        service=self.service();fixture=self.root/"fixtures/story_project.json"
        fixture.parent.mkdir(parents=True);fixture.write_bytes((ROOT/"fixtures/story_project.json").read_bytes())
        port=self.server(service)
        with patch("urllib.request.OpenerDirector.open",side_effect=AssertionError("Offline read fixture forbids provider requests")),patch.object(service,"submit",side_effect=AssertionError("Snapshot must not enqueue work")):
            status,headers,data=self.request(port,"GET","/api/workspace")
            self.assertEqual(status,200,data)
            self.assertEqual(data["project"]["id"],data["studio"]["project_id"])
            self.assertFalse(data["studio"]["preview_gate"]["ready"])
            self.assertEqual(data["jobs"],[])
            self.assertEqual(set(data["conversations"]),{"script","video"})
            for role,conversation in data["conversations"].items():
                self.assertEqual(conversation["session"]["story_id"],data["studio"]["story_id"])
                self.assertEqual(conversation["session"]["role"],role)
                self.assertEqual(conversation["messages"],[])
            self.assertTrue(service.tracer.verify(headers["X-Trace-Id"])["ok"])
            self.assertEqual(service.state["jobs"],[])

            # Force a new projection with the existing sessions. It should read
            # their actual messages without resolving the same project twice.
            sessions=service._pi_sessions
            sessions.repo.append(data["conversations"]["video"]["session"]["id"],"user","offline workspace message")
            with patch.object(sessions,"resolve",side_effect=AssertionError("Workspace already matched this session")), \
                 patch.object(sessions.repo,"ensure",side_effect=AssertionError("Existing sessions are linked")):
                status,headers,updated=self.request(port,"GET","/api/workspace?project="+data["project"]["id"])
            self.assertEqual(status,200,updated)
            self.assertEqual([m["text"] for m in updated["conversations"]["video"]["messages"]],["offline workspace message"])
            self.assertTrue(service.tracer.verify(headers["X-Trace-Id"])["ok"])
            with patch("pianchang.workspace_read._build_workspace",side_effect=AssertionError("Unchanged closed gate should be reusable")):
                status,headers,reused=self.request(port,"GET","/api/workspace?project="+data["project"]["id"])
            self.assertEqual(status,200,reused)
            self.assertEqual(updated,reused)
            records=service.tracer.get_events(headers["X-Trace-Id"])
            self.assertIn("workbench.snapshot.reused",[record["name"] for record in records])
            recorded=next(record for record in records if record["name"]=="http.response.body")
            self.assertEqual(service.tracer.resolve(recorded["data"])["body"],reused)
            self.assertTrue(service.tracer.verify(headers["X-Trace-Id"])["ok"])

    def test_studio_http_reference_goes_from_script_to_shot_without_leaking_into_trace_or_other_stories(self):
        service = self.service()
        first = self.root / "fixtures" / "story_project.json"
        first.parent.mkdir()
        project = json.loads((ROOT / "fixtures" / first.name).read_text(encoding="utf-8"))
        first.write_text(json.dumps(project), encoding="utf-8")
        other = service.state_dir / "stories" / "offline-b" / "project.json"
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps({**project, "offline_fixture": "separate project"}), encoding="utf-8")
        ids = {r["path"]: pid for pid, r in service._discover().items()}
        port = self.server(service)
        route = "/api/projects/" + ids[first] + "/studio"
        pixels = offline_png((12, 0, 0))
        encoded = base64.b64encode(pixels).decode()
        def offline_observer(service, brief, criteria, files):
            self.assertIn("candidate", files)
            return ([{"criterion_id": k, "verdict": "PASS", "reason": "offline synthetic red sample",
                      "frame_ids": ["candidate"]} for k in criteria], {"model": "offline-test"})
        with patch("pianchang.asset_review._observe_images", side_effect=offline_observer):
            status, headers, result = self.request(port, "POST", route + "/references", {
                "subject_id": "char:lin", "purpose": "portrait", "data_uri": "data:image/png;base64," + encoded})
            self.assertEqual(status, 201, result)
            self.assertEqual(self.wait_job(service, result["job"]["id"])["status"], "succeeded")
        self.assertEqual(status, 201, result)
        receipt = result["pin"]
        trace = json.dumps(self.tracer.export_trace(headers["X-Trace-Id"]))
        self.assertNotIn(encoded, trace)
        self.assertIn("captured_wire_sha256", trace)
        status, _, binding = self.request(port, "POST", route + "/bindings", {
            "asset_id": receipt["asset_id"], "revision_id": receipt["revision_id"], "shot_id": "S002", "spec_parent_revision": None})
        self.assertEqual(status, 400, binding)
        status, _, binding = self.request(port, "POST", route + "/asset-approve",
            {k: receipt[k] for k in ("asset_id", "revision_id", "sha256")})
        self.assertEqual(status, 200, binding)
        self.assertIn("S002", binding["shot_ids"])
        status, _, flow = self.request(port, "GET", route)
        self.assertEqual(status, 200, flow)
        self.assertEqual(binding["spec"], flow["assets"]["spec"])
        self.assertEqual([s["id"] for s in flow["stages"]], ["script", "assets", "video", "review"])
        self.assertEqual(flow["productions"], [])
        shot = next(s for s in flow["assets"]["shots"] if s["id"] == "S002")
        self.assertEqual(shot["required_refs"], [receipt])
        entity = next(e for e in flow["assets"]["entities"] if e["id"] == "char:lin")
        self.assertIn("S002", entity["shot_ids"])
        media_url = entity["references"][0]["media_url"]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", media_url, headers={"Range": "bytes=0-7"})
            response = conn.getresponse()
            self.assertEqual((response.status, response.getheader("Content-Type"), response.read()), (206, "image/png", pixels[:8]))
        finally:
            conn.close()
        status, _, separate = self.request(port, "GET", "/api/projects/" + ids[other] + "/studio")
        self.assertEqual(status, 200)
        self.assertNotEqual(separate["story_id"], flow["story_id"])
        self.assertFalse(any(e["references"] for e in separate["assets"]["entities"]))

    def wait_trace_end(self, tracer, trace_id):
        # Receiving the response does not join the server thread: its final
        # span.end transaction happens after socket delivery. UNKNOWN is valid
        # until that event exists; wait for evidence, not a fixed sleep.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            detail = tracer.get_trace(trace_id)
            if detail["end_time"] is not None:
                return detail
            time.sleep(.02)
        self.fail("Request trace did not record its end")

    def test_exact_count_failure_contains_context_actual_expected_and_error_chain(self):
        script = json.loads((ROOT / "fixtures" / "story_script.json").read_text(encoding="utf-8"))
        draft = draft_from_script(script)
        draft["episodes"][0]["scenes"][0]["beats"].pop()
        client = FakeClient(draft)
        with self.assertRaisesRegex(StoryError, "要求 3，实际 2"):
            with self.tracer.trace("offline.count_failure", kind="task", attributes={"test_only": True}) as root:
                StoryRunner(TextConfig(api_key="sk-trace-offline-secret"), client).build(script["idea"], self.root / "story", beats=3, max_repairs=0)
        detail = self.tracer.get_trace(root.trace_id)
        self.assertEqual(detail["status"], "ERROR")
        count_span = next(s for s in detail["spans"] if s["name"] == "story.validate_counts")
        result = self.tracer.resolve(count_span["output"])
        self.assertEqual(result["status"], "FAIL")
        failed = next(c for c in result["checks"] if c["status"] == "FAIL")
        self.assertEqual(failed, {"path": "episodes[0].scenes[0].beats", "expected": 3, "actual": 2, "status": "FAIL"})
        saved = json.loads((self.root / "story" / "draft-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, result)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse((self.root / "story" / "project.json").exists())
        self.assertTrue(self.tracer.verify(root.trace_id)["ok"])
        self.assertNotIn("sk-trace-offline-secret", json.dumps(self.tracer.export_trace(root.trace_id)))

    def test_http_request_and_background_task_have_linked_independent_traces(self):
        service = self.service()
        port = self.server(service)
        def execute(job, body):
            with span("offline.echo_tool", kind="tool", inputs=body) as tool:
                tool.set_output({"idea": body["idea"]})
            return {"test_only": True}
        with patch.object(service, "_build_story", side_effect=execute):
            status, headers, response = self.request(port, "POST", "/api/projects", {"idea": "offline test"}, {"Idempotency-Key": "trace-link-test"})
            self.assertEqual(status, 202)
            task = self.wait_job(service, response["job"]["id"])
        self.assertEqual(task["status"], "succeeded")
        self.assertNotEqual(task["trace_id"], headers["X-Trace-Id"])
        task_trace = service.tracer.get_trace(task["trace_id"])
        self.assertEqual(task_trace["status"], "OK")
        self.assertEqual(task_trace["links"][0]["trace_id"], headers["X-Trace-Id"])
        self.assertIn("offline.echo_tool", [s["name"] for s in task_trace["spans"]])
        names = [e["name"] for e in task_trace["events"]]
        self.assertIn("task.queued", names)
        self.assertIn("task.updated", names)
        self.assertTrue(service.tracer.verify(task["trace_id"])["ok"])

    def test_reused_job_records_link_without_repeating_task(self):
        service = self.service()
        with patch.object(service, "_build_story", return_value={"test_only": True}) as build:
            first = service.submit("story", None, {"idea": "once"}, "same-key")
            self.wait_job(service, first["id"])
            with use_context(None, tracer=service.tracer):
                second = service.submit("story", None, {"idea": "once"}, "same-key")
        self.assertEqual(first["trace_id"], second["trace_id"])
        self.assertEqual(build.call_count, 1)
        self.assertEqual(len(service.tracer.list_traces(kind="task")), 1)
        self.assertTrue(any(e["name"] == "task.reused" for t in service.tracer.list_traces()
                            for e in service.tracer.get_events(t["trace_id"])))

    def test_trace_storage_failure_prevents_any_queued_model_work(self):
        service = self.service()
        with patch.object(service.tracer, "start_span", side_effect=TraceError("disk offline")), patch.object(service.executor, "submit") as queued:
            with self.assertRaises(TraceError):
                service.submit("story", None, {"idea": "must not execute"})
        queued.assert_not_called()
        self.assertEqual(service.list_jobs(), [])

    def test_http_trace_query_is_inspectable_without_recursive_payload_capture(self):
        service = self.service()
        with service.tracer.trace("fixture", kind="task") as example:
            event("test.fixture", {"value": "data"})
        port = self.server(service)
        status, headers, body = self.request(port, "GET", "/api/traces/" + example.trace_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["trace_id"], example.trace_id)
        query = self.wait_trace_end(service.tracer, headers["X-Trace-Id"])
        response = next(e for e in query["events"] if e["name"] == "http.response.body")
        data = service.tracer.resolve(response["data"])
        self.assertEqual(data["body_omitted_reason"], "trace_query_response_avoids_recursive_capture")
        self.assertNotIn("body", data)

    def test_rejected_request_has_error_trace_and_no_task(self):
        service = self.service()
        port = self.server(service)
        status, headers, body = self.request(port, "POST", "/api/projects", {"idea": "blocked"}, {"Origin": "http://evil.invalid"})
        self.assertEqual(status, 403)
        self.assertEqual(service.list_jobs(), [])
        trace = self.wait_trace_end(service.tracer, headers["X-Trace-Id"])
        self.assertEqual(trace["status"], "ERROR")

    def test_legacy_job_is_not_backfilled_with_fabricated_trace(self):
        state_dir = self.root / "outputs" / "web"
        state_dir.mkdir(parents=True)
        old = {"jobs": [{"id": "old-job", "status": "failed", "kind": "story"}], "selections": {}, "reviews": {}, "exports": {}}
        (state_dir / "state.json").write_text(json.dumps(old), encoding="utf-8")
        service = self.service()
        self.assertNotIn("trace_id", service.list_jobs()[0])
        self.assertEqual(service.tracer.list_traces(kind="task"), [])

    def test_event_http_pagination_uses_trace_sequence_across_global_gaps(self):
        service = self.service()
        with service.tracer.trace("first", kind="task") as first:
            first.event("one")
            with service.tracer.trace("interleaved", kind="task"):
                event("other")
            first.event("two")
            first.event("three")
        port = self.server(service)
        after, found = 0, []
        for _ in range(10):
            status, headers, page = self.request(port, "GET", f"/api/traces/{first.trace_id}/events?limit=2&after={after}")
            self.assertEqual(status, 200)
            found.extend(page["events"])
            after = page["next_after"]
            if not page["has_more"]:
                break
        expected = service.tracer.get_events(first.trace_id)
        self.assertEqual([e["event_id"] for e in found], [e["event_id"] for e in expected])

    def test_http_exact_span_pages_exclude_siblings_children_and_other_traces(self):
        service = self.service()
        with service.tracer.trace("offline batch") as root:
            with service.tracer.span("inspection") as selected:
                selected.event("before child")
                with service.tracer.span("child") as child:
                    child.event("child evidence")
                selected.event("after child")
            with service.tracer.span("sibling") as sibling:
                sibling.event("unrelated")
        with service.tracer.trace("offline other order") as foreign:
            foreign.event("separate evidence")
        port = self.server(service)
        endpoint = f"/api/traces/{root.trace_id}/events"
        after, found = 0, []
        for _ in range(10):
            status, _, page = self.request(port, "GET", f"{endpoint}?span={selected.span_id}&limit=2&after={after}")
            self.assertEqual(status, 200, page)
            found.extend(page["events"])
            self.assertTrue(page["events"])
            self.assertEqual(page["next_after"], page["events"][-1]["trace_seq"])
            after = page["next_after"]
            if not page["has_more"]:
                break
        self.assertEqual([e["name"] for e in found], ["inspection","before child","after child","inspection"])
        self.assertEqual({e["span_id"] for e in found}, {selected.span_id})
        self.assertEqual(len({e["event_id"] for e in found}), 4)
        status, _, page = self.request(port, "GET", f"{endpoint}?span={foreign.span_id}")
        self.assertEqual(status, 200, page)
        self.assertEqual(page, {"events":[], "has_more":False, "next_after":0})
        status, _, _ = self.request(port, "GET", f"{endpoint}?span=invalid")
        self.assertEqual(status, 400)

    def test_registered_production_boundary_contract(self):
        from tools.check_trace_contract import check_contract
        self.assertEqual(check_contract()["issues"], [])

    def test_failed_terminal_state_write_cannot_leave_successful_task_trace(self):
        service = self.service()
        with patch.object(service, "_execute"):
            submitted = service.submit("story", None, {"idea": "offline persistence failure"})
        job = next(j for j in service.state["jobs"] if j["id"] == submitted["id"])
        with patch.object(service, "_save", side_effect=OSError("disk write failed")):
            with self.assertRaises(OSError):
                service._update(job, status="succeeded", result={"test_only": True})
        self.assertIsNone(service.tracer.get_trace(job["trace_id"])["end_time"])
        service._update(job, status="failed", error="disk write failed")
        self.assertEqual(service.tracer.get_trace(job["trace_id"])["status"], "ERROR")


if __name__ == "__main__":
    unittest.main()
