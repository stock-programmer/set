"""Offline progress tests. No model, provider, production worker or HTTP server is started."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pianchang.story_spec import digest_json
from pianchang.studio_live_progress import last_shot_records, read_production_progress, resolve_production_clip
from trace_system import Tracer, use_context


def put(path, value):
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ProductionProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(use_context(None, tracer=Tracer(self.root / "offline-traces")))
        self.pid = "p"
        self.run, self.order, self.jid = "a"*32, "b"*32, "c"*32
        self.job = {"id":self.jid, "kind":"studio_video", "project_id":self.pid, "status":"running",
                    "run_id":self.run, "message_id":self.order, "plan_id":"d"*64, "trace_id":"e"*32}
        self.script = {"asset_id":"story-" + self.run + "/script","revision_id":"r1", "sha256":"0"*64}
        self.plan = {"project_id":self.pid, "plan_id":self.job["plan_id"],
                     "new_shots":["S01","S02","S03"],"reuse_shots":[],"retake_shots":[]}
        out = self.root / "outputs"
        collab = out / "collaborations"
        collab.mkdir(parents=True)
        self.work = collab / "runs" / self.run / ("video-spec-" + self.order)
        self.video = out / "videos"
        self.video.mkdir()
        self.db = collab / "assets.sqlite3"
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""CREATE TABLE messages(message_id,story_id,correlation_id,recipient,message_type,payload,created_at);
                CREATE TABLE deliveries(message_id,agent,status);
                CREATE TABLE receipts(message_id,agent,status,trace_context);
                CREATE TABLE assets(asset_id,revision_id,sha256,story_id,kind,content);""")
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", (self.order,"story-"+self.run,self.run,"video","production.requested",
                       json.dumps({"script":self.script, "shooting_plan":self.plan, "previous":None}),"2026-11-01T01:02:03+00:00"))
            db.execute("INSERT INTO deliveries VALUES(?,?,?)", (self.order,"video","accepted"))
            db.execute("INSERT INTO receipts VALUES(?,?,?,?)",(self.order,"video","accepted",json.dumps({"trace_id":"f"*32})))
            db.commit()
        put(self.work/"agent/run.json", {"reserved_shots":["S01","S02"],"status":"running"})

    def manifest(self, sid, status, plan_id=None):
        task = "task-" + sid
        folder = self.video / (sid + "-" + (plan_id or self.job["plan_id"])[:3])
        path = folder / "job.json"
        manifest = {"task_id":task, "status":status, "metadata":{"shot_id":sid, "script_asset_ref":self.script,
                    "dependency_sha256":"1"*64, "shooting_plan_id":plan_id or self.job["plan_id"]},
                    "provider_response":{"output":{"task_id":task, "task_status":status}}}
        if status == "SUCCEEDED":
            folder.mkdir(exist_ok=True)
            media = folder / "video.mp4"
            media.write_bytes(b"offline media bytes")
            sha = hashlib.sha256(media.read_bytes()).hexdigest()
            manifest.update(local_video_path=str(media),download_sha256=sha)
        put(path, manifest)
        return manifest, path

    def snapshot(self):
        return read_production_progress(self.root,self.pid,self.job,preview_url=lambda s,h:f"/{s}/{h}")

    def record_generated_shot(self, sid):
        manifest, path = self.manifest(sid,"SUCCEEDED")
        clip = {"shot_id":sid,"task_id":manifest["task_id"],"script_ref":self.script,"origin_script_ref":self.script,
                "operation":"generated","manifest_path":str(path),"generation_job_sha256":digest_json(manifest),
                "dependency_sha256":"1"*64,"media_path":manifest["local_video_path"],"media_sha256":manifest["download_sha256"]}
        put(self.work/"clips.json",{sid:clip})
        return clip

    def test_saved_shot_ledger_uses_exact_order_and_never_needs_media(self):
        self.plan.update(new_shots=["S01","S02"], reuse_shots=["S03"], script_ref=self.script)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE messages SET payload=?", (json.dumps({"script":self.script,"shooting_plan":self.plan}),))
            db.commit()
        clip={"operation":"generated","script_ref":self.script,"task_id":"offline-receipt",
              "media_sha256":"1"*64,"generation_job_sha256":"2"*64}
        put(self.work/"clips.json",{"S01":{**clip,"shot_id":"S01"}, "S03":{**clip,"shot_id":"S03","operation":"reused"}})
        put(self.work/"agent/run.json",{"shooting_plan":self.plan,"reserved_shots":["S02"]})
        result=last_shot_records(self.root,self.pid,self.job)
        self.assertEqual([s["status"] for s in result["shots"]],["generated","reserved","reused"])
        self.assertEqual((result["completed_count"],result["shot_count"]),(2,3))
        self.assertEqual(result["message_id"],self.order)
        self.assertTrue(result["recorded_at"].endswith("+00:00"))
        self.assertIsNone(last_shot_records(self.root,"other",self.job))
        self.assertIsNone(last_shot_records(self.root,self.pid,{**self.job,"plan_id":"e"*64}))
        put(self.work/"clips.json",{"S01":{**clip,"shot_id":"S01","script_ref":{"revision_id":"r99"}}})
        put(self.work/"agent/run.json",{"shooting_plan":{**self.plan,"plan_id":"e"*64},"reserved_shots":["S02"]})
        result=last_shot_records(self.root,self.pid,self.job)
        self.assertEqual([s["status"] for s in result["shots"]],["unknown","pending","pending"])
        self.assertEqual(result["completed_count"],0)
        self.plan["new_shots"].append("S02")
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE messages SET payload=?", (json.dumps({"script":self.script,"shooting_plan":self.plan}),))
            db.commit()
        self.assertIsNone(last_shot_records(self.root,self.pid,self.job))

    def test_live_finished_active_unstarted_and_preserved_previews_after_timeout(self):
        clip = self.record_generated_shot("S01")
        media_path, sha = Path(clip["media_path"]), clip["media_sha256"]
        self.manifest("S02","RUNNING")
        self.manifest("S02","SUCCEEDED",plan_id="9"*64)  # different plan cannot satisfy S02
        d = self.snapshot()
        self.assertEqual([s["status"] for s in d["shots"]],["ready","generating","pending"])
        self.assertEqual((d["completed"],d["total"],d["current_shot_id"],d["video_trace_id"]), (1,3,"S02","f"*32))
        self.job["status"] = "failed"
        d = self.snapshot()
        self.assertEqual([s["status"] for s in d["shots"]],["ready","unknown","not_started"])
        self.assertEqual(d["shots"][0]["acceptance_status"], "PENDING")
        self.assertEqual(d["shots"][0]["preview_url"], f"/S01/{sha}")
        self.assertEqual(resolve_production_clip(self.root,self.pid,self.job,"S01",sha), media_path)
        with self.assertRaises(KeyError):
            resolve_production_clip(self.root,"other",self.job,"S01",sha)
        with self.assertRaises(KeyError):
            resolve_production_clip(self.root,self.pid,self.job,"S02",sha)
        with self.assertRaises(KeyError):
            resolve_production_clip(self.root,self.pid,self.job,"S01","0"*64)
        media_path.write_bytes(b"changed")
        with self.assertRaises(KeyError):
            resolve_production_clip(self.root,self.pid,self.job,"S01",sha)
        self.assertEqual(self.snapshot()["completed"],0)

    def test_verdict_is_for_the_exact_clip_and_project(self):
        clip = self.record_generated_shot("S01")
        project = {"shots":[{"id":"S01"}], "label":"explicit offline test"}
        put(self.work/"project.json",project)
        report = {"shot_id":"S01","status":"PASS","project_sha256":digest_json(project),
                  "media_sha256":clip["media_sha256"],"generation_job_sha256":clip["generation_job_sha256"]}
        path = self.work/"reviews/S01/report.json"
        for field, value in (("shot_id","S02"),("media_sha256","0"*64),("project_sha256","0"*64),
                             ("generation_job_sha256","0"*64)):
            with self.subTest(field=field):
                stale = {**report, field:value}
                put(path,stale)
                put(self.work/"reports.json",{"S01":{"shot_id":"S01","report_path":str(path),
                                                      "report_sha256":digest_json(stale),"status":"PASS"}})
                row = self.snapshot()["shots"][0]
                self.assertEqual(row["acceptance_status"],"UNKNOWN")
                self.assertEqual(row["preview_url"],f"/S01/{clip['media_sha256']}")
        put(path,report)
        row = self.snapshot()["shots"][0]  # index still contains the old report digest
        self.assertEqual(row["acceptance_status"], "UNKNOWN")
        current = {"S01":{"sha256":clip["media_sha256"]}}
        for selected, expected in ((current,"PASS"),({**current,"S02":{"sha256":"9"*64}},"UNKNOWN")):
            with self.subTest(content_status=expected):
                evidence = {"selected_media":selected}
                content_report = {**report,"content_review_version":"explicit-offline/1",
                                  "content_evidence_sha256":digest_json(evidence)}
                put(path.parent/"content.json",evidence)
                put(path,content_report)
                put(self.work/"reports.json",{"S01":{"shot_id":"S01","report_path":str(path),
                                                      "report_sha256":digest_json(content_report),"status":"PASS"}})
                self.assertEqual(self.snapshot()["shots"][0]["acceptance_status"],expected)

    def test_candidate_from_another_commission_with_same_plan_is_not_previewable(self):
        clip = self.record_generated_shot("S01")
        manifest_path = Path(clip["manifest_path"])
        manifest = json.loads(manifest_path.read_bytes())
        for order, ready in ((self.work, True), (self.work.parent/("video-spec-"+"9"*32), False)):
            with self.subTest(order=order):
                manifest["metadata"]["execution_key"] = digest_json({"order":str(order),"shot_id":"S01","repair_id":None})
                put(manifest_path, manifest)
                put(self.work/"clips.json", {"S01":{**clip, "generation_job_sha256":digest_json(manifest)}})
                row = self.snapshot()["shots"][0]
                self.assertEqual(bool(row.get("preview_url")), ready)
                if not ready:
                    with self.assertRaises(KeyError):
                        resolve_production_clip(self.root,self.pid,self.job,"S01",clip["media_sha256"])

    def test_historical_retake_is_readable_only_with_its_original_receipt_and_bytes(self):
        from pianchang.studio_live_progress import _snapshot
        original = self.record_generated_shot("S01")
        self.plan["acceptance_loop"] = {"shot_ids":["S01"],"max_repairs_per_shot":1}
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE messages SET payload=?", (json.dumps({"script":self.script,"shooting_plan":self.plan}),))
            db.commit()
        rid = "9" * 64
        manifest = json.loads(Path(original["manifest_path"]).read_bytes())
        folder = self.video / "second-take"
        folder.mkdir()
        media = folder / "video.mp4"
        media.write_bytes(b"second successful video")
        sha = hashlib.sha256(media.read_bytes()).hexdigest()
        manifest.update(task_id="retake",provider_response={"output":{"task_id":"retake","task_status":"SUCCEEDED"}},
                        local_video_path=str(media),download_sha256=sha)
        manifest["metadata"].update(acceptance_repair={"repair_id":rid},execution_key=digest_json({
            "order":str(self.work),"shot_id":"S01","repair_id":rid}))
        path = folder / "job.json"
        put(path,manifest)
        retake = {**original,"repair_id":rid,"manifest_path":str(path),"task_id":"retake","media_path":str(media),
                  "media_sha256":sha, "generation_job_sha256":digest_json(manifest)}
        history = {"shot_id":"S01","repair_id":rid,"source_clip":original,"result_clip":retake,
                   "status":"reviewed_rejected_regression"}
        def write_history():
            put(self.work / "repair-history.json",{"history":[history]})
        def versions():
            data,_ = _snapshot(self.root,self.pid,self.job,None,lambda sid,digest:"/"+digest,
                               include_reviews=False,version_paths={})
            return data["shots"][0]["versions"]
        write_history()
        initial, second = versions()
        self.assertEqual([(v["kind"], v["selected"]) for v in (initial,second)], [("initial",True),("retake",False)])
        self.assertEqual(second["selection_reason"],"rejected_regression")
        self.assertEqual(resolve_production_clip(self.root,self.pid,self.job,"S01",sha),media)
        history["status"] = "blocked_evaluation"
        write_history()
        self.assertEqual(versions()[1]["preview_url"], "/"+sha)
        self.assertNotIn("report",versions()[1])
        for wrong_sid in ("S02","other"):
            with self.assertRaises(KeyError):
                resolve_production_clip(self.root,self.pid,self.job,wrong_sid,sha)
        media.write_bytes(b"changed historical video")
        with self.assertRaises(KeyError):
            resolve_production_clip(self.root,self.pid,self.job,"S01",sha)
        media.write_bytes(b"second successful video")
        manifest["provider_response"]["output"]["task_id"] = "other"
        put(path,manifest)
        retake["generation_job_sha256"] = digest_json(manifest)
        write_history()
        self.assertEqual([v["task_id"] for v in versions()], [original["task_id"]])
        manifest["provider_response"]["output"]["task_id"] = "retake"
        manifest["metadata"]["execution_key"] = digest_json({"order":"other","shot_id":"S01","repair_id":rid})
        put(path,manifest)
        retake["generation_job_sha256"] = digest_json(manifest)
        write_history()
        with self.assertRaises(KeyError):
            resolve_production_clip(self.root,self.pid,self.job,"S01",sha)

    def test_later_observation_requires_same_provider_task_and_original_manifest(self):
        manifest,_ = self.manifest("S02","RUNNING")
        self.job["status"] = "failed"
        task = manifest["task_id"]
        folder = self.root / "outputs/web/progress-observations" / self.jid / "S02" / task
        info = {"project_id":self.pid,"job_id":self.jid,"message_id":self.order,"plan_id":self.job["plan_id"],"shot_id":"S02",
                "task_id":task, "manifest_sha256":digest_json(manifest), "provider_status":"SUCCEEDED",
                "media_file":"video.mp4","media_sha256":hashlib.sha256(b"offline clip").hexdigest(),"trace_id":"8"*32,
                "provider_response":{"output":{"task_id":"different","task_status":"SUCCEEDED"}}}
        put(folder/"receipt.json",info)
        (folder/"video.mp4").write_bytes(b"offline clip")
        self.assertEqual(self.snapshot()["completed"],0)
        info["provider_response"]["output"]["task_id"] = task
        put(folder/"receipt.json",info)
        d = self.snapshot()
        self.assertEqual((d["completed"],d["phase"],d["shots"][1]["observation_trace_id"]),(1,"stopped","8"*32))
        self.assertEqual(d["shots"][1]["acceptance_status"], "PENDING")
        self.assertEqual(resolve_production_clip(self.root,self.pid,self.job,"S02",info["media_sha256"]), folder/"video.mp4")
        info["manifest_sha256"] = "0"*64
        put(folder/"receipt.json",info)
        self.assertEqual(self.snapshot()["completed"],0)
        with self.assertRaises(KeyError):
            resolve_production_clip(self.root,self.pid,self.job,"S02",info["media_sha256"])

    def test_another_plan_or_unlinked_task_does_not_read_commission_files(self):
        self.job.update(plan_id="0"*64)
        self.assertEqual(self.snapshot()["total"],0)
        self.job.update(message_id=None)
        self.assertEqual(self.snapshot()["shots"],[])
