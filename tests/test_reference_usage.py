"""Offline attribution across specs, unselected provider attempts and immutable selections."""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.bailian_video import VideoConfig, VideoError, build_request
from pianchang.collab_store import CollabStore
from pianchang.reference_usage import query_reference_usage
from pianchang.story_spec import digest_json
from trace_system import Tracer, event, span, trace, use_context
from pianchang.video_agent import VideoRunner


class OfflineProvider:
    def __init__(self, outcome):
        self.outcome = outcome

    def submit(self, payload):
        wire = json.dumps(payload,ensure_ascii=False,allow_nan=False).encode("utf8")
        with span("offline.reference-usage.transport", kind="test"):
            event("http.request", {"method":"POST", "url":"https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis",
                                  "body":payload, "wire_body_sha256":hashlib.sha256(wire).hexdigest()})
            if self.outcome == "accepted":
                event("http.response", {"http_status":200,"body":{"output":{"task_id":"offline-task"}}})
            if self.outcome == "rejected":
                event("http.response", {"http_status":400, "body_request_id":"offline-rejected",
                                        "body":{"request_id":"offline-rejected"}})
        if self.outcome == "rejected":
            raise VideoError("offline fixture provider rejected", http_status=400, request_id="offline-rejected")
        if self.outcome == "unknown":
            raise VideoError("offline fixture connection lost")
        return {"output":{"task_id":"offline-task", "task_status":"PENDING"}, "request_id":"offline-result"}


class ReferenceUsageTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.scope = use_context(None, tracer=Tracer(self.root / "offline-traces"))
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.store = CollabStore(self.root / "outputs/collaborations/assets.sqlite3")
        self.jobs = self.root / "jobs"
        def chunk(name, content):
            return struct.pack(">I",len(content)) + name + content + struct.pack(">I",zlib.crc32(name+content))
        self.data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR",struct.pack(">IIBBBBB",240,240,8,2,0,0,0))
                     + chunk(b"IDAT",zlib.compress(b"\0"*(240*(1+240*3)))) + chunk(b"IEND",b""))
        self.content = {"subject_id":"char:lin", "purpose":"portrait", "media_sha256":hashlib.sha256(self.data).hexdigest(),
                        "schema_version":"reference-media/1", "mime_type":"image/png", "bytes":len(self.data)}
        reference = self.store.publish_asset("portrait/lin", self.content, kind="reference_media",
                                             story_id="story-A", producer="offline-assets")
        self.pin = {k:reference[k] for k in ("asset_id","revision_id","sha256")}
        self.pin.update({k:self.content[k] for k in ("subject_id","purpose","media_sha256")})

    def publish_spec(self, *, parent_revision=None, pins=None):
        refs = [self.pin] if pins is None else pins
        return self.store.publish_asset("project", {"shots":[{"id":"S003","required_refs":refs}]}, kind="production_spec",
            story_id="story-A",producer="offline-assets", parent_revision=parent_revision,
            dependencies=[{"asset_id":p["asset_id"],"revision_id":p["revision_id"],"role":"required_reference",
                           "source_id":"S003"} for p in refs])

    def submit(self, *, outcome="accepted", story="story-A", source_story="story-A"):
        config = VideoConfig(model="wan2.7-r2v", api_key="unit-reference-query-placeholder-key", output_dir=self.jobs)
        resolved = dict(self.pin, provider_url="data:image/png;base64,"+base64.b64encode(self.data).decode("ascii"),
                        bytes=len(self.data), width=240, height=240, mime_type="image/png")
        body = build_request(config, "offline prompt", resolved_refs=[resolved])
        runner = VideoRunner(config, client=OfflineProvider(outcome))
        return runner.submit(body, {"story_id":story, "reference_story_id":source_story, "shot_id":"S003",
                                    "required_refs":[self.pin]}, new=True, resolved_refs=[resolved])

    def query(self):
        return query_reference_usage(self.store, self.pin["asset_id"], self.pin["revision_id"], jobs_dir=self.jobs)

    def test_published_but_never_run_and_exact_revision(self):
        self.publish_spec()
        first = self.query()
        self.assertEqual([(s["shot_id"],s["source"]["revision_id"],s["edge_recorded"]) for s in first["required_shots"]],
                         [("S003","r1",True)])
        self.assertFalse(first["candidates"])
        self.assertEqual(first["accepted_count"],0)
        self.publish_spec(parent_revision="r1",pins=[])
        second = self.store.publish_asset("portrait/lin",{**self.content,"media_sha256":"a"*64},
            kind="reference_media",producer="offline-assets",story_id="story-A",parent_revision="r1")
        new = query_reference_usage(self.store,second["asset_id"],"r2",jobs_dir=self.jobs)
        self.assertFalse(new["required_shots"])
        self.assertEqual(self.query()["required_shots"], first["required_shots"])
        with self.assertRaises(KeyError):
            query_reference_usage(self.store,second["asset_id"],"r2",jobs_dir=self.jobs,story_id="story-B")

    def test_unselected_and_unknown_never_equate_to_acceptance(self):
        self.submit()
        for outcome in ("rejected","unknown"):
            with self.assertRaises(VideoError):
                self.submit(outcome=outcome)
        legacy = self.jobs / "legacy"
        legacy.mkdir()
        (legacy / "job.json").write_text(json.dumps({"schema_version":"bailian-video-job/1",
            "metadata":{"shot_id":"S003","story_id":"story-A"},"task_id":"no-receipt"}),encoding="utf8")
        data = self.query()
        states = {c["evidence_status"]:c for c in data["candidates"] + data["unverified_candidates"]}
        self.assertEqual(data["submitted_count"],2)
        self.assertEqual(data["accepted_count"],1)
        self.assertEqual(set(states),{"ACCEPTED","SUBMISSION_UNKNOWN","PROVIDER_REJECTED","LEGACY_UNKNOWN"})
        self.assertFalse(states["SUBMISSION_UNKNOWN"]["submitted_refs"])
        self.assertEqual(states["ACCEPTED"]["http_evidence"]["status"],"MATCHED_RESPONSE")
        self.assertEqual(states["PROVIDER_REJECTED"]["http_evidence"]["status"],"MATCHED_RESPONSE")
        self.assertEqual(states["SUBMISSION_UNKNOWN"]["http_evidence"]["status"],"REQUEST_ONLY")
        self.assertEqual(states["ACCEPTED"]["selected_in"],[])
        self.assertEqual({r["evidence_status"] for r in data["unverified_candidates"]},{"LEGACY_UNKNOWN","SUBMISSION_UNKNOWN"})
        visible = json.dumps(data)
        self.assertNotIn("data:image",visible)
        self.assertNotIn("offline prompt",visible)

    def test_tampered_receipt_and_other_stories(self):
        row = self.submit()
        journal = Path(row["manifest_path"])
        saved = json.loads(journal.read_text(encoding="utf8"))
        saved["reference_receipt"]["submitted_refs"][0]["locator_sha256"] = "d"*64
        journal.write_text(json.dumps(saved),encoding="utf8")
        self.submit(story="foreign",source_story="foreign")
        data = self.query()
        self.assertFalse(data["candidates"])
        self.assertEqual([r["evidence_status"] for r in data["unverified_candidates"]],["INVALID_RECEIPT"])
        external = self.submit(story="commissioned",source_story="story-A")
        self.assertEqual(self.query()["candidates"][0]["candidate_id"],Path(external["manifest_path"]).parent.name)

    def test_journal_wire_hash_contradicted_by_independent_provider_trace(self):
        row = self.submit()
        path = Path(row["manifest_path"])
        saved = json.loads(path.read_text(encoding="utf8"))
        saved["reference_receipt"]["wire_body_sha256"] = "f"*64
        path.write_text(json.dumps(saved),encoding="utf8")
        report = self.query()
        self.assertEqual(report["accepted_count"],0)
        self.assertEqual(report["unverified_candidates"][0]["http_evidence"]["status"],"CONFLICT")

    def test_unknown_receipt_validation_preserves_uncertainty(self):
        self.submit()
        with patch("pianchang.video_agent.verify_reference_receipt",return_value={"status":"UNKNOWN", "checks":[]}):
            data = self.query()
        self.assertEqual((data["accepted_count"],data["submitted_count"]),(0,0))
        item, = data["unverified_candidates"]
        self.assertEqual((item["evidence_status"],item["receipt_validation"]),("RECEIPT_UNCONFIRMED","UNKNOWN"))

    def test_accepted_journal_without_correlated_transport_response_is_not_counted(self):
        result = self.submit()
        path = Path(result["manifest_path"])
        job = json.loads(path.read_text(encoding="utf8"))
        receipt = job["reference_receipt"]
        for expected in ("NO_TRANSPORT_RECORD","REQUEST_ONLY","RESPONSE_UNCORRELATED"):
            with self.subTest(expected=expected), trace("offline.unconfirmed_delivery",kind="test") as root:
                with span("video.submit"):
                    with span("video.references.check_request") as checked:
                        checked.set_output({**receipt, "status":"PREPARED", "submitted_refs":[]})
                    if expected != "NO_TRANSPORT_RECORD":
                        with span("offline.provider",kind="test"):
                            event("http.request", {"method":"POST", "url":job["base_url"]+"/services/aigc/video-generation/video-synthesis",
                                                  "body":job["request"], "wire_body_sha256":receipt["wire_body_sha256"]})
                            if expected == "RESPONSE_UNCORRELATED":
                                event("http.response", {"http_status":200,"body":{"output":{"task_id":"different-task"}}})
                receipt["trace_id"] = root.trace_id
                path.write_text(json.dumps(job),encoding="utf8")
            report = self.query()
            self.assertEqual((report["submitted_count"],report["accepted_count"]),(0,0))
            self.assertEqual(report["candidates"],[])
            entry, = report["unverified_candidates"]
            self.assertEqual((entry["submitted_refs"],entry["evidence_status"]),( [],"TRANSPORT_UNCONFIRMED"))
            self.assertEqual(entry["http_evidence"]["status"],expected)

    def test_http_response_needs_preflight_from_same_submission_to_attribute_asset_version(self):
        result = self.submit()
        path = Path(result["manifest_path"])
        job = json.loads(path.read_text(encoding="utf8"))
        receipt = job["reference_receipt"]
        for misplaced in ("absent","sibling","after_post"):
            with self.subTest(misplaced=misplaced), trace("offline.missing_associated_preflight", kind="test") as root:
                if misplaced == "sibling":
                    with span("video.submit"), span("video.references.check_request") as preflight:
                        preflight.set_output({**receipt,"status":"PREPARED","submitted_refs":[]})
                with span("video.submit"):
                    with span("offline.provider",kind="test"):
                        event("http.request", {"method":"POST", "url":job["base_url"]+"/services/aigc/video-generation/video-synthesis",
                                              "body":job["request"], "wire_body_sha256":receipt["wire_body_sha256"]})
                        event("http.response", {"http_status":200, "body":{"output":{"task_id":job["task_id"]}}})
                    if misplaced == "after_post":
                        with span("video.references.check_request") as preflight:
                            preflight.set_output({**receipt,"status":"PREPARED","submitted_refs":[]})
                receipt["trace_id"] = root.trace_id
                path.write_text(json.dumps(job),encoding="utf8")
            report = self.query()
            self.assertEqual((report["submitted_count"],report["accepted_count"]),(0,0))
            entry = report["unverified_candidates"][0]
            self.assertEqual(entry["receipt_validation"],"PASS")
            self.assertEqual(entry["http_evidence"]["status"],"BINDING_UNCONFIRMED")

    def test_new_version_cannot_be_attributed_by_rewriting_receipt_for_identical_bytes(self):
        result = self.submit()
        path = Path(result["manifest_path"])
        self.store.publish_asset("portrait/lin",self.content,kind="reference_media",producer="offline-assets",
                                 story_id="story-A",parent_revision="r1")
        job = json.loads(path.read_text(encoding="utf8"))
        for group in (job["metadata"]["required_refs"], *[job["reference_receipt"][key] for key in
                         ("required_refs","prepared_refs","submitted_refs")]):
            group[0]["revision_id"] = "r2"
        job["request_sha256"] = digest_json({k:job[k] for k in ("base_url","request","metadata")})
        path.write_text(json.dumps(job),encoding="utf8")
        report = query_reference_usage(self.store,"portrait/lin","r2",jobs_dir=self.jobs)
        self.assertEqual(report["accepted_count"],0)
        self.assertEqual(report["unverified_candidates"][0]["evidence_status"],"INVALID_RECEIPT")

    def test_same_original_candidate_can_be_selected_in_multiple_production_revisions(self):
        result = self.submit()
        manifest = Path(result["manifest_path"])
        job = json.loads(manifest.read_text(encoding="utf8"))
        media_hash = hashlib.sha256(b"offline clip for selection indexing").hexdigest()
        job.update(status="SUCCEEDED",download_sha256=media_hash)
        manifest.write_text(json.dumps(job),encoding="utf8")
        clip = {"shot_id":"S003","task_id":job["task_id"],"media_sha256":media_hash,
                "manifest_path":str(manifest),"operation":"generated"}
        package = {"project":{"shots":[]},"shots":{"S003":clip}}
        kwargs = {"kind":"production","producer":"offline-video","story_id":"story-A"}
        self.store.publish_asset("production",package,**kwargs)
        clip["operation"] = "reused"
        self.store.publish_asset("production",package,parent_revision="r1",**kwargs)
        data = self.query()
        self.assertEqual(data["accepted_count"],1)
        selected = data["candidates"][0]["selected_in"]
        self.assertEqual([(row["production"]["revision_id"],row["operation"]) for row in selected],
                         [("r1","generated"),("r2","reused")])

    def test_archived_dependency_survives_missing_selected_journal_without_claiming_it_is_proof(self):
        media = self.store.publish_asset("shot",{"task_id":"offline-old","shot_id":"S003"},kind="shot_media",
            story_id="story-A",producer="offline-video",
            dependencies=[{"asset_id":"portrait/lin","revision_id":"r1","role":"submitted_reference","source_id":"S003"}])
        self.store.publish_asset("production", {"project":{"shots":[]},"shots":{"S003":{
            "task_id":"offline-old","media_sha256":"a"*64,"manifest_path":str(self.jobs/"missing"/"job.json"),
            "operation":"generated"}},"media_refs":{"S003":{k:media[k] for k in ("asset_id","revision_id","sha256")}}},
            kind="production",producer="offline-video",story_id="story-A")
        data = self.query()
        self.assertEqual(data["archived_media_dependencies"][0]["media"]["asset_id"],"shot")
        self.assertEqual(data["submitted_count"],0)
        self.assertEqual(data["unverified_candidates"][0]["evidence_status"],"JOURNAL_UNAVAILABLE")

    def test_consuming_story_can_follow_two_hop_selection_without_seeing_other_consumers(self):
        def publish_external(story):
            job = self.submit(story=story,source_story="story-A")
            manifest = Path(job["manifest_path"])
            saved = json.loads(manifest.read_text(encoding="utf8"))
            media_sha = hashlib.sha256(story.encode()).hexdigest()
            saved.update(status="SUCCEEDED",download_sha256=media_sha)
            manifest.write_text(json.dumps(saved),encoding="utf8")
            clip = {"shot_id":"S003","manifest_path":str(manifest),"task_id":job["task_id"],"media_sha256":media_sha,"operation":"generated"}
            common = {"story_id":story,"producer":"offline-video"}
            media = self.store.publish_asset(story+"/media",clip,kind="shot_media",**common,
                dependencies=[{"asset_id":"portrait/lin","revision_id":"r1","role":"submitted_reference","source_id":"S003"}])
            media_ref = {k:media[k] for k in ("asset_id","revision_id","sha256")}
            unrelated = {"shot_id":"UNRELATED", "task_id":"private-other-task", "media_sha256":"e"*64,
                         "manifest_path":str(self.jobs/(story+"-unrelated")/"job.json")}
            package = {"project":{"shots":[]},"shots":{"S003":clip, "UNRELATED":unrelated},"media_refs":{"S003":media_ref}}
            self.store.publish_asset(story+"/production",package,kind="production",**common,
                dependencies=[{"asset_id":media["asset_id"],"revision_id":"r1","role":"selected_media"}])
            return manifest.parent.name
        candidate_b = publish_external("story-B")
        candidate_c = publish_external("story-C")
        all_uses = self.query()
        self.assertEqual({row["candidate_id"] for row in all_uses["candidates"]},{candidate_b,candidate_c})
        for consumer,candidate in (("story-B", candidate_b),("story-C",candidate_c)):
            view = query_reference_usage(self.store,"portrait/lin","r1",jobs_dir=self.jobs,story_id=consumer)
            row, = view["candidates"]
            self.assertEqual(row["candidate_id"],candidate)
            self.assertEqual(row["selected_in"][0]["production"]["asset_id"],consumer+"/production")
            self.assertEqual(view["accepted_count"],1)
            self.assertEqual(view["unverified_candidates"],[])
        with self.assertRaises(KeyError):
            query_reference_usage(self.store,"portrait/lin","r1",jobs_dir=self.jobs,story_id="unrelated")

    def test_readonly_http_endpoint_accepts_pinned_revision_and_fixed_workspace_directory(self):
        from trace_system import get_tracer
        from pianchang.web_server import create_server
        self.store.publish_asset("story-B/spec",{"shot_id":"S003","required_refs":[self.pin]},kind="shot_reference_spec",
            producer="offline-video",story_id="story-B",
            dependencies=[{"asset_id":"portrait/lin","revision_id":"r1","role":"required_reference","source_id":"S003"}])
        service = SimpleNamespace(root=self.root,tracer=get_tracer(),video_config=SimpleNamespace(output_dir=self.jobs))
        server = create_server(service, port=0)
        server.daemon_threads = False  # wait for HTTP Trace commits before Windows removes the fixture DB
        worker = Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address,timeout=15)
            connection.request("GET","/api/reference-usage?asset_id=portrait%2Flin&revision_id=r1&story_id=story-A")
            response = connection.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(json.loads(response.read())["reference"],self.pin)
            connection.request("GET","/api/reference-usage?asset_id=portrait%2Flin&revision_id=r1&story_id=story-B")
            response = connection.getresponse()
            self.assertEqual(response.status,200)
            foreign = json.loads(response.read())
            self.assertEqual((foreign["story_id"],foreign["query_story_id"]),("story-A","story-B"))
            self.assertEqual([r["shot_id"] for r in foreign["required_shots"]],["S003"])
            connection.request("GET","/api/reference-usage?asset_id=portrait%2Flin&revision_id=r1&story_id=unknown-story")
            response = connection.getresponse()
            self.assertEqual(response.status,404)
            response.read()
            connection.request("GET","/api/reference-usage?asset_id=portrait%2Flin&revision_id=r1&jobs_dir=/tmp")
            response = connection.getresponse()
            self.assertEqual(response.status,400)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            worker.join(15)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
