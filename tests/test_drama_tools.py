"""Offline adapter contracts; fake media/provider results are explicitly fixtures."""
import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.drama_tools import build_drama_tools
from pianchang.story_spec import digest_json
from trace_system import get_tracer, use_context

ROOT = Path(__file__).resolve().parents[1]


class FakeService:
    def __init__(self, root, project):
        self.root = root
        self.state_dir = root / "web"
        self.tracer = get_tracer(root / "traces")
        self.lock = threading.RLock()
        self.state = {"selections": {}, "exports": {}}
        self.project = copy.deepcopy(project)
        self.candidates = []
        self.reports = {}
        self.check_status = "UNKNOWN"
        self.check_calls = []
        self.video_config = SimpleNamespace(output_dir=root / "videos")
        self.path = root / "project.json"

    def _project(self, pid):
        if pid != "project-one":
            raise KeyError(pid)
        return self.path, copy.deepcopy(self.project)

    def _hash(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def asset(self, path):
        return "/assets/" + hashlib.sha256(str(path).encode()).hexdigest()[:32]

    def _candidates(self, project):
        return list(self.candidates)

    @staticmethod
    def _public_candidate(candidate):
        return {k: v for k, v in candidate.items() if not k.startswith("_")}

    def _chosen(self, pid, shot_id, candidates, explicit=None):
        items = [c for c in candidates if c["shot_id"] == shot_id]
        cid = explicit or self.state["selections"].get(pid, {}).get(shot_id)
        if cid:
            candidate = next((c for c in items if c["id"] == cid), None)
            if candidate:
                return candidate
            if explicit:
                raise ValueError("Wrong candidate scope")
        return items[-1] if items else None

    def add_candidate(self, shot_id, cid=None, status="SUCCEEDED"):
        cid = cid or ("candidate-" + shot_id)
        directory = self.root / "videos" / cid
        directory.mkdir(parents=True)
        video = directory / "video.mp4"
        video.write_bytes(b"explicit offline fake media fixture: " + cid.encode())
        job = {"download_sha256": self._hash(video), "metadata": {"shot_id": shot_id, "project_sha256": digest_json(self.project)}, "test_only": True}
        candidate = {"id": cid, "shot_id": shot_id, "task_id": "offline-task-" + cid, "status": status,
                     "_video": video if status == "SUCCEEDED" else None, "_path": directory / "job.json", "_job": job}
        self.candidates.append(candidate)
        return candidate

    def _check(self, job, project, body):
        self.check_calls.append(copy.deepcopy(body))
        candidate = self._chosen(job["project_id"], body["shot_id"], self.candidates, body["candidate_id"])
        self.reports[candidate["id"]] = {"status": self.check_status,
            "task_complete": self.check_status == "PASS", "report_url": "/assets/offline-report-" + candidate["id"],
            "generation_job_sha256": digest_json(candidate["_job"]), "media_sha256": candidate["_job"]["download_sha256"],
            "checks": [{"criterion_id": "offline_content_check", "status": self.check_status}], "test_only": True}
        return {"project_id": job["project_id"], "shot_id": body["shot_id"], "acceptance_status": self.check_status}

    def _report(self, project, candidate):
        if not candidate or not candidate.get("_video"):
            return None
        report = self.reports.get(candidate["id"])
        if report and (report["generation_job_sha256"] != digest_json(candidate["_job"]) or report["media_sha256"] != self._hash(candidate["_video"])):
            return None
        return copy.deepcopy(report)

    def select(self, pid, body):
        self.state["selections"].setdefault(pid, {})[body["shot_id"]] = body["candidate_id"]
        return {"id": pid}

    def _export(self, job, project):
        binding = []
        for shot in project["shots"]:
            candidate = self._chosen(job["project_id"], shot["id"], self.candidates)
            if not candidate or not candidate["_video"]:
                raise ValueError("missing clip")
            binding.append([shot["id"], candidate["id"], candidate["_job"]["download_sha256"]])
        preview = self.root / "offline-preview.mp4"
        preview.write_bytes(json.dumps(binding).encode())
        self.state["exports"][job["project_id"]] = {"binding": binding, "preview_path": str(preview), "preview_sha256": self._hash(preview)}
        return {"project_id": job["project_id"], "video_url": self.asset(preview), "status": "PREVIEW_ONLY", "accepted": False, "test_only": True}

    def _generated(self, pid, shot_id, generated):
        cid = Path(generated["manifest_path"]).parent.name
        self.state["selections"].setdefault(pid, {})[shot_id] = cid
        return {"project_id": pid, "shot_id": shot_id, "candidate_id": cid,
                "video_url": self.asset(generated["local_video_path"]), "task_id": generated["task_id"]}


class DramaToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"AGENT_TRACE_DIR": str(self.root / "traces")})
        self.env.start()
        self.addCleanup(self.env.stop)
        project = json.loads((ROOT / "fixtures" / "story_project.json").read_text(encoding="utf-8"))
        self.service = FakeService(self.root, project)
        self.job = {"id": "a" * 32, "project_id": "project-one"}
        self.fingerprint = digest_json(project)

    def adapter(self, mode="inspect", with_model=False):
        with use_context(None, tracer=self.service.tracer):
            return build_drama_tools(self.service, self.job, {"mode": mode, "with_model": with_model}, self.fingerprint)

    def fill(self):
        return [self.service.add_candidate(s["id"]) for s in self.service.project["shots"]]

    def check_all(self, adapter):
        for shot in self.service.project["shots"]:
            adapter.check_shot(shot["id"])

    def test_candidate_cannot_cross_shot_or_project_scope(self):
        self.service.add_candidate("S001")
        adapter = self.adapter(mode="preview")
        with self.assertRaises(ValueError):
            adapter.select_candidate("S002", "candidate-S001")
        with self.assertRaises(ValueError):
            adapter.resume_video("S001", "../foreign-project")
        self.assertEqual(self.service.state["selections"], {})

    def test_project_version_change_prevents_any_generation(self):
        adapter = self.adapter(mode="preview")
        self.service.project["title"] = "changed independently"
        with patch("pianchang.drama_tools.VideoRunner") as runner:
            with self.assertRaisesRegex(ValueError, "版本"):
                adapter.generate_video("S001")
        runner.assert_not_called()

    def test_prompt_patch_is_run_scoped_and_used_by_generation(self):
        adapter = self.adapter(mode="preview")
        adapter.patch_prompt("S001", "explicit offline revised prompt")
        compiled = {"prompt": "revised", "duration": 5, "metadata": {"target_duration_s": 5}}
        pending = {"manifest_path": str(self.root / "videos" / "new-one" / "job.json"), "task_id": "offline-cloud", "status": "PENDING"}
        with patch("pianchang.drama_tools.compile_shot", return_value=compiled) as compile_call, patch("pianchang.drama_tools.build_request", return_value={"parameters": {"duration": 5}}), patch("pianchang.drama_tools.VideoRunner") as runner:
            runner.return_value.submit.return_value = pending
            runner.return_value.resume.return_value = pending
            result = adapter.generate_video("S001")
        self.assertEqual(digest_json(self.service.project), self.fingerprint)
        self.assertEqual(compile_call.call_args.kwargs["prompt_override"], "explicit offline revised prompt")
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(self.service.state["selections"], {})
        self.assertTrue((adapter.directory / "state.json").exists())

    def test_unconfirmed_or_pending_submission_cannot_be_regenerated(self):
        candidate = self.service.add_candidate("S001", status="PENDING")
        adapter = self.adapter(mode="preview")
        with patch("pianchang.drama_tools.VideoRunner") as runner:
            result = adapter.generate_video("S001")
            self.assertFalse(result["submission_performed"])
            self.assertEqual(result["candidate_id"], candidate["id"])
            with self.assertRaisesRegex(ValueError, "不能再次生成"):
                adapter.generate_video("S001", new_candidate=True)
            candidate["task_id"] = None
            candidate["status"] = "SUBMISSION_UNKNOWN"
            with self.assertRaises(ValueError):
                adapter.generate_video("S001")
        runner.assert_not_called()

    def test_finished_generation_and_resume_preserve_existing_explicit_selection(self):
        old = self.service.add_candidate("S001", "old-approved-choice")
        self.service.select("project-one", {"shot_id": "S001", "candidate_id": old["id"]})
        new = self.service.add_candidate("S001", "new-repair-candidate")
        adapter = self.adapter(mode="preview")
        generated = {"manifest_path": str(new["_path"]), "local_video_path": str(new["_video"]),
                     "task_id": new["task_id"], "status": "SUCCEEDED"}
        compiled = {"prompt": "offline repair", "duration": 5, "metadata": {"target_duration_s": 5}}
        with patch("pianchang.drama_tools.compile_shot", return_value=compiled), patch("pianchang.drama_tools.build_request", return_value={"parameters": {"duration": 5}}), patch("pianchang.drama_tools.VideoRunner") as runner, patch.object(self.service, "_generated") as legacy_select:
            runner.return_value.submit.return_value = generated
            runner.return_value.resume.return_value = generated
            first = adapter.generate_video("S001", new_candidate=True)
            resumed = adapter.resume_video("S001", new["id"])
        legacy_select.assert_not_called()
        self.assertEqual(self.service.state["selections"]["project-one"]["S001"], old["id"])
        for result in (first, resumed):
            self.assertEqual(result["candidate_id"], new["id"])
            self.assertEqual(result["selected_candidate_id"], old["id"])
            self.assertEqual(result["selection_source"], "explicit")
            self.assertFalse(result["selection_changed"])
            self.assertEqual(result["video_url"], self.service.asset(new["_video"]))
            self.assertIn("select_candidate", result["next_action"])

    def test_snapshot_and_result_identify_default_fallback_as_not_explicit(self):
        candidate = self.service.add_candidate("S001")
        adapter = self.adapter(mode="preview")
        shot = next(s for s in adapter.snapshot()["shots"] if s["shot_id"] == "S001")
        self.assertEqual(shot["selected_candidate_id"], candidate["id"])
        self.assertEqual(shot["selection_source"], "default_candidate")
        self.assertEqual(shot["default_candidate"], candidate["id"])
        self.assertIsNone(shot["explicit_candidate_id"])
        generated = {"manifest_path": str(candidate["_path"]), "local_video_path": str(candidate["_video"]),
                     "task_id": candidate["task_id"], "status": "SUCCEEDED"}
        with patch("pianchang.drama_tools.VideoRunner") as runner:
            runner.return_value.resume.return_value = generated
            result = adapter.resume_video("S001", candidate["id"])
        self.assertEqual(result["selection_source"], "default_candidate")
        self.assertIsNone(result["explicit_candidate_id"])
        self.assertEqual(self.service.state["selections"], {})
        adapter.select_candidate("S001", candidate["id"])
        updated = next(s for s in adapter.snapshot()["shots"] if s["shot_id"] == "S001")
        self.assertEqual(updated["selection_source"], "explicit")
        self.assertIsNone(updated["default_candidate"])

    def test_visual_model_permission_cannot_be_expanded_by_tool_args(self):
        self.fill()
        adapter = self.adapter(with_model=False)
        with self.assertRaisesRegex(ValueError, "没有授权"):
            adapter.check_shot("S001", with_model=True)
        self.assertEqual(self.service.check_calls, [])

    def test_inspection_completion_requires_all_current_candidates_checked_this_run(self):
        self.fill()
        adapter = self.adapter()
        self.assertFalse(adapter.finish_check()["complete"])
        adapter.check_shot("S001")
        self.assertFalse(adapter.finish_check()["complete"])
        self.check_all(adapter)
        report = adapter.finish_check()
        self.assertTrue(report["complete"])
        self.assertFalse(report["media_accepted"])
        self.assertIn("不代表影片验收通过", report["summary"])

    def test_candidate_replacement_or_file_change_invalidates_checked_result(self):
        candidates = self.fill()
        adapter = self.adapter()
        self.check_all(adapter)
        self.assertTrue(adapter.finish_check()["complete"])
        replacement = self.service.add_candidate("S001", "replacement")
        self.service.select("project-one", {"shot_id": "S001", "candidate_id": replacement["id"]})
        self.assertFalse(adapter.finish_check()["complete"])
        adapter.check_shot("S001")
        self.assertTrue(adapter.finish_check()["complete"])
        replacement["_video"].write_bytes(b"changed offline fixture")
        self.assertFalse(adapter.finish_check()["complete"])

    def test_existing_reports_do_not_count_as_checks_performed_by_this_run(self):
        self.fill()
        first = self.adapter()
        self.check_all(first)
        fresh = self.adapter()
        self.assertFalse(fresh.finish_check()["complete"])

    def test_preview_requires_published_gate_before_any_export_effect(self):
        from pianchang.preview_access import PreviewBlocked
        self.fill();adapter=self.adapter(mode="preview")
        gate={"ready":False,"production_ref":None,"reason":"offline: mandatory checks unresolved"}
        with patch("pianchang.preview_access.preview_status",return_value=gate),patch.object(self.service,"_export") as exporter:
            self.assertFalse(adapter.finish_check()["complete"])
            with self.assertRaises(PreviewBlocked):adapter.export_preview()
            exporter.assert_not_called()

    def test_local_pass_reports_do_not_replace_published_acceptance(self):
        from pianchang.preview_access import PreviewBlocked
        self.fill();adapter=self.adapter(mode="accepted")
        self.service.check_status="PASS";self.check_all(adapter)
        gate={"ready":False,"production_ref":None,"reason":"offline: no fully accepted delivery"}
        with patch("pianchang.preview_access.preview_status",return_value=gate):
            self.assertFalse(adapter.finish_check()["complete"])
            with self.assertRaises(PreviewBlocked):adapter.export_preview()
            self.assertEqual(adapter.finish_check()["creative_review"],"not_performed")

    def test_inspection_returns_frame_references_not_visual_claim_or_base64(self):
        candidate = self.service.add_candidate("S001")
        adapter = self.adapter()
        fixture = {"inspection_status": "PASS", "media_sha256": candidate["_job"]["download_sha256"],
                   "metadata": {"duration_s": 5}, "decode": {"status": "PASS"}, "sampling": {"coverage_note": "offline sampled fixture"},
                   "frames": [{"id": "F001", "time_s": 1.0, "sha256": "f" * 64, "path": str(self.root / "frame.jpg")}],
                   "manifest_path": str(self.root / "inspection.json")}
        with patch("pianchang.drama_tools.inspect_media_file", return_value=fixture):
            result = adapter.inspect_media("S001")
        self.assertTrue(result["frames"][0]["url"].startswith("/assets/"))
        self.assertNotIn("data:", json.dumps(result))
        self.assertIn("has not visually inspected", result["observation_scope"])
        self.assertFalse(adapter.finish_check()["complete"])

    def test_tool_catalog_marks_only_generate_as_billable_video_action(self):
        adapter = self.adapter(mode="preview")
        self.assertEqual([t.name for t in adapter.tools if t.effect == "video"], ["generate_video"])
        self.assertTrue(all(t.parameters["additionalProperties"] is False for t in adapter.tools))
        self.assertTrue(all(getattr(t.execute, "__trace_instrumented__", False) for t in adapter.tools))
        with self.service.tracer.trace("offline adapter parent") as root:
            adapter.snapshot()
        detail = self.service.tracer.get_trace(root.trace_id)
        self.assertTrue(any(s["name"] == "drama.tools.snapshot" and s["parent_span_id"] == root.span_id for s in detail["spans"]))

    def test_inspect_mode_exposes_only_read_and_check_tools_and_guards_direct_writes(self):
        adapter = self.adapter()
        self.assertEqual({tool.name for tool in adapter.tools}, {"read_project", "query_story_state", "inspect_media", "check_shot"})
        with patch("pianchang.drama_tools.VideoRunner") as runner:
            for method, args in [(adapter.generate_video, ("S001",)), (adapter.resume_video, ("S001", "candidate-S001")),
                                 (adapter.patch_prompt, ("S001", "rewrite")), (adapter.select_candidate, ("S001", "candidate-S001")),
                                 (adapter.export_preview, ())]:
                with self.subTest(method=method.__name__):
                    with self.assertRaisesRegex(ValueError, "仅检查模式"):
                        method(*args)
        runner.assert_not_called()
        self.assertEqual(self.service.state, {"selections": {}, "exports": {}})
        self.assertEqual(adapter.overrides, {})


if __name__ == "__main__":
    unittest.main()
