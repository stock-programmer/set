"""Existing-task status lookups use an isolated offline provider and an independent trace."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pianchang.app_service import write_json
from pianchang.bailian_video import VideoConfig
from tools.studio_progress_observe import observe_existing_task
from trace_system import Tracer, use_context


class OfflineReader:
    def __init__(self, root):
        self.config = VideoConfig(api_key="offline-inactive", output_dir=root / "outputs/videos")
        self.calls = []
        self.status = "SUCCEEDED"
        self.task = "task-001"

    def get_task(self, task):
        self.calls.append(("GET",task))
        return {"output":{"task_id":self.task,"task_status":self.status,
                          "video_url":"https://example.test/a?Signature=never-show-this"}}

    def download(self, url, path):
        self.calls.append(("download", path))
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b"offline movie")


class ObservationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.reader=OfflineReader(self.root)
        self.ids=["1"*16,"2"*32,"S005","task-001","3"*24]
        project, job, sid, task, candidate = self.ids
        write_json(self.root/"outputs/web/state.json",{"jobs":[{"id":job,"kind":"studio_video","project_id":project,
             "message_id":"order","story_id":"story","run_id":"run"}]})
        dbpath=self.root/"outputs/collaborations/assets.sqlite3"
        dbpath.parent.mkdir(parents=True)
        db=sqlite3.connect(dbpath)
        db.execute("CREATE TABLE messages(story_id,correlation_id,payload,message_id,message_type,recipient)")
        script={"asset_id":"script"}
        order={"shooting_plan":{"project_id":project,"plan_id":"plan","new_shots":[sid],"retake_shots":[]},
               "script":script,"template":{"sha256":"spec"}}
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",("story","run",json.dumps(order),"order","production.requested","video"))
        db.commit()
        db.close()
        self.manifest=self.reader.config.output_dir/candidate/"job.json"
        write_json(self.manifest,{"metadata":{"script_asset_ref":script,"shooting_plan_id":"plan","project_sha256":"spec",
                "shot_id":sid},"provider":"aliyun-bailian","base_url":self.reader.config.base_url,"task_id":task,
                "provider_response":{"output":{"task_id":task}}})
        ctx=use_context(None,tracer=Tracer(self.root/"offline-traces"))
        ctx.__enter__()
        self.addCleanup(ctx.__exit__,None,None,None)

    def read(self):
        return observe_existing_task(self.root,*self.ids,client=self.reader)

    def test_success_keeps_writer_bytes_and_reuses_hash_bound_observation(self):
        before=self.manifest.read_bytes()
        receipt=self.read()
        self.assertEqual(receipt["provider_status"],"SUCCEEDED")
        self.assertIsNotNone(receipt["media_sha256"])
        self.assertNotIn("never-show-this",json.dumps(receipt))
        self.assertEqual(receipt,self.read())
        self.assertEqual([name for name,_ in self.reader.calls],["GET","download"])
        self.assertEqual(before,self.manifest.read_bytes())
        self.assertFalse((self.manifest.parent/"video.mp4").exists())

    def test_mismatched_task_never_exposes_media_and_request_for_wrong_shot_stops_before_network(self):
        self.reader.task="alien-task"
        with self.assertRaises(ValueError):
            self.read()
        self.ids[2]="S006"
        with self.assertRaises(ValueError):
            self.read()
        self.assertEqual(self.reader.calls,[("GET","task-001")])

    def test_running_status_is_only_a_timestamped_observation(self):
        self.reader.status="RUNNING"
        receipt=self.read()
        self.assertEqual(receipt["provider_status"],"RUNNING")
        self.assertIsNone(receipt["media_file"])
        self.assertEqual(self.reader.calls,[("GET","task-001")])
