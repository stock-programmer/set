"""Explicit offline read projections; these fixtures do not approve real media."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from trace_system import Tracer,use_context
from pianchang.workspace_cache import cached_workspace


class WorkspaceCacheTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        tracer=Tracer(self.root/"traces");self.enterContext(tracer.storage_session());self.enterContext(use_context(None,tracer=tracer))
        self.enterContext(tracer.trace("test.workspace_cache",inputs={"label":"explicit offline projection fixtures"}))
        self.pid="a"*16;source=self.root/"project.json";source.write_text('{}',encoding="utf-8")
        report=self.root/"outputs/report.json";report.parent.mkdir();report.write_text('first',encoding="utf-8");self.report=report
        self.service=SimpleNamespace(root=self.root,state_dir=self.root/"outputs/web",lock=threading.RLock(),assets={},
            video_config=SimpleNamespace(output_dir=self.root/"outputs/videos"),
            state={"jobs":[],"asset_work":{self.pid:{"status":"approved"}},"asset_auto_dispatch":{}},_discover=lambda:{self.pid:{"path":source}})
        self.calls=0

    def build(self,pid):
        self.calls+=1
        return {"studio":{"preview_gate":{"ready":False}},"evidence":{"report_path":str(self.report)},"label":"offline display only"}

    def test_unchanged_projection_is_copied_and_file_or_job_changes_invalidate_it(self):
        first=cached_workspace(self.service,self.pid,self.build)
        first["label"]="external mutation"
        self.assertEqual(cached_workspace(self.service,self.pid,self.build)["label"],"offline display only")
        self.assertEqual(self.calls,1)
        self.service.state["asset_auto_dispatch"]["b"*16]={"status":"idle"}
        cached_workspace(self.service,self.pid,self.build);self.assertEqual(self.calls,1)
        self.report.write_text('changed actual file',encoding="utf-8")
        cached_workspace(self.service,self.pid,self.build);self.assertEqual(self.calls,2)
        self.service.state["jobs"].append({"id":"job","kind":"pi_video","project_id":self.pid,"status":"running"})
        cached_workspace(self.service,self.pid,self.build);self.assertEqual(self.calls,3)

    def test_positive_authorization_is_never_cached(self):
        def accepted(pid):
            self.calls+=1
            return {"studio":{"preview_gate":{"ready":True}},"label":"explicit offline gate fixture; not an actual media verdict"}
        cached_workspace(self.service,self.pid,accepted);cached_workspace(self.service,self.pid,accepted)
        self.assertEqual(self.calls,2)
        self.assertNotIn(self.pid,self.service._workspace_reads)

    def test_sidebar_shot_records_invalidate_cached_workspace_across_projects(self):
        other = "b"*16
        job = {"id": "c"*32, "kind": "studio_video", "status": "running",
               "project_id": other, "run_id": "d"*32, "message_id": "e"*32}
        self.service.state["jobs"].append(job)
        work = self.root/"outputs/collaborations/runs"/job["run_id"]/("video-spec-" + job["message_id"])
        work.mkdir(parents=True)
        def build(pid):
            data = self.build(pid)
            data["projects"] = [{"id": other, "opening": {"video_job": dict(job)}}]
            return data
        cached_workspace(self.service, self.pid, build)
        (work/"clips.json").write_text("{}", encoding="utf-8")
        cached_workspace(self.service, self.pid, build)
        job["message"] = "updated without a clock change"
        cached_workspace(self.service, self.pid, build)
        self.assertEqual(self.calls, 3)

    def test_sidebar_project_addition_edit_and_removal_invalidate_open_workspace(self):
        records=self.service._discover()
        self.service._discover=lambda:records
        cached_workspace(self.service,self.pid,self.build)
        other=self.root/"second.json"
        other.write_text('{"title":"offline"}',encoding="utf-8")
        records["b"*16]={"path":other}
        cached_workspace(self.service,self.pid,self.build)
        other.write_text('{"title":"updated offline title"}',encoding="utf-8")
        cached_workspace(self.service,self.pid,self.build)
        del records["b"*16]
        cached_workspace(self.service,self.pid,self.build)
        self.assertEqual(self.calls,4)

    def test_conversation_changes_invalidate_only_their_own_story(self):
        from pi_bridge.repository import SessionRepository
        repo=SessionRepository(self.root/"outputs/pi")
        first=repo.ensure("offline-story-a","video");second=repo.ensure("offline-story-b","video")
        def build(pid):
            data=self.build(pid);data["studio"]["story_id"]="offline-story-a";return data
        cached_workspace(self.service,self.pid,build)
        repo.append(second["id"],"user","explicit offline other story")
        cached_workspace(self.service,self.pid,build);self.assertEqual(self.calls,1)
        repo.append(first["id"],"user","explicit offline current story")
        cached_workspace(self.service,self.pid,build);self.assertEqual(self.calls,2)

    def test_changing_inputs_during_build_are_not_marked_reusable(self):
        def changed(pid):
            self.service.state["asset_work"][pid]["status"]="changed during offline build"
            return self.build(pid)
        cached_workspace(self.service,self.pid,changed)
        self.assertNotIn(self.pid,self.service._workspace_reads)


if __name__=="__main__":unittest.main()
