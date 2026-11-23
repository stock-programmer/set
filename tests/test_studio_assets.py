"""Offline workbench tests with synthetic PNGs and temporary collaboration/trace stores."""
import base64
from contextlib import closing
import hashlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

from pianchang.collab_store import CollabStore
from pianchang.model_routes import load_routes
from pianchang.story_spec import digest_json
from pianchang.studio_assets import (bind_studio_reference, generate_studio_reference, studio_asset_catalog, studio_project_for_video,
                           studio_reference_media, upload_studio_reference, validate_studio_reference_body)
from tests.test_reference_assets import offline_png
from trace_system import Tracer, use_context


class OfflineStudio:
    def __init__(self, root):
        self.root = root
        self.source = Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json"
        self.project = json.loads(self.source.read_text(encoding="utf8"))

    def _project(self, pid):
        project = self.project if pid == "project-a" else dict(self.project, title="另一个故事")
        source = self.root / "stories" / pid / "project.json"
        return source, project


class StudioAssetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.service = OfflineStudio(Path(folder.name))
        self.tracer = Tracer(Path(folder.name) / "offline-trace")
        context = use_context(None, tracer=self.tracer)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.pid = "project-a"

    def upload(self, color, parent=None):
        uri = "data:image/png;base64," + base64.b64encode(offline_png(color)).decode("ascii")
        return upload_studio_reference(self.service, self.pid, {
            "subject_id": "char:lin", "purpose": "portrait", "data_uri": uri, "parent_revision": parent})["pin"]

    def bind(self, pin, parent, **extra):
        # Scope these legacy binding/CAS tests to an explicitly simulated human-approved candidate.
        with patch("pianchang.asset_dispatch.check_reference_approval"):
            return bind_studio_reference(self.service, self.pid, {"shot_id": "S002", "asset_id": pin["asset_id"],
                "revision_id": pin["revision_id"], "spec_parent_revision": parent, **extra})

    def test_offline_versions_and_video_entry_use_same_pin(self):
        catalog = studio_asset_catalog(self.service, self.pid)
        lin = next(e for e in catalog["entities"] if e["id"] == "char:lin")
        self.assertTrue(lin["source_refs"])
        self.assertEqual(lin["references"], [])
        self.assertIn("S002", lin["shot_ids"])
        self.assertFalse(Path(catalog["store_path"]).exists())
        r1 = self.upload((12, 34, 56))
        spec1 = self.bind(r1, None)["spec"]
        self.assertEqual(studio_reference_media(self.service, self.pid, r1["asset_id"], "r1")[0].read_bytes(),
                         offline_png((12, 34, 56)))
        r2 = self.upload((34, 56, 78), parent="r1")
        with patch.object(CollabStore, "_asset", wraps=CollabStore._asset) as materialize:
            before = studio_project_for_video(self.service, self.pid)
            self.assertNotIn("reference_media", [call.args[1]["kind"] for call in materialize.call_args_list])
        self.assertEqual(before["project"]["shots"][1]["required_refs"], [r1])
        spec2 = self.bind(r2, "r1", spec_sha256=spec1["sha256"])["spec"]
        after = studio_project_for_video(self.service, self.pid, spec2["revision_id"])
        self.assertEqual(after["project"]["shots"][1]["required_refs"], [r2])
        self.assertEqual(after["spec"], spec2)
        self.assertEqual(studio_project_for_video(self.service, self.pid, "r1")["project"]["shots"][1]["required_refs"], [r1])
        catalog = studio_asset_catalog(self.service, self.pid)
        references = next(s["references"] for s in catalog["entities"] if s["id"] == "char:lin")
        self.assertEqual([r["revision_id"] for r in references], ["r2", "r1"])
        self.assertEqual(references[1]["dependencies"][0]["source_id"], "S002")
        self.assertEqual(spec2["sha256"], digest_json(after["project"]))
        self.assertNotIn("required_refs", self.service.project["shots"][1])

    def test_republished_cut_supersedes_bound_shots_without_rewriting_old_revision(self):
        import copy
        from pianchang.collaboration_workflow import ref
        from pianchang.studio_assets import sync_published_spec
        view=studio_project_for_video(self.service,self.pid)
        store=CollabStore(view["store_path"])
        source,original=self.service._project(self.pid)
        source.parent.mkdir(parents=True)
        story=view["story_id"]
        script=store.publish_asset(story+"/script",original["script_source"]["script"],
                                   kind="script",producer="script",story_id=story)
        old=store.publish_asset(story+"/source-project",original,kind="input_project",producer="commissioner",story_id=story)
        pin=self.upload((12,34,56))
        old_spec=self.bind(pin,None)["spec"]
        revised=copy.deepcopy(original)
        revised["shots"][1]["duration_s"]+=1
        new=store.publish_asset(old["asset_id"],revised,kind="input_project",producer="commissioner",
                                story_id=story,parent_revision="r1")
        metadata={"run_id":view["run_id"],"story_id":story,"project_id":self.pid,"script_ref":ref(script),"source_ref":ref(new)}
        source.write_text(json.dumps(revised),encoding="utf8")
        (source.parent/"studio.json").write_text(json.dumps(metadata),encoding="utf8")
        self.service.project=revised
        self.assertEqual(studio_project_for_video(self.service,self.pid)["project"],revised)
        sync_published_spec(self.service,self.pid,source)
        current=studio_project_for_video(self.service,self.pid)
        self.assertEqual(current["spec"]["revision_id"],"r2")
        self.assertEqual(current["project"]["shots"][1]["required_refs"],[pin])
        self.assertEqual(current["project"]["shots"][1]["duration_s"],revised["shots"][1]["duration_s"])
        self.assertEqual(studio_project_for_video(self.service,self.pid,"r1")["spec"],old_spec)
        sync_published_spec(self.service,self.pid,source)
        self.assertEqual(len(store.revisions(old_spec["asset_id"])),2)

    def test_recut_only_reuses_consistent_pins_from_all_contributing_beats(self):
        import copy
        from pianchang.studio_assets import _rebind_shared_pins
        project=self.service.project
        old=copy.deepcopy(project)
        shot=copy.deepcopy(old["shots"][1])
        target=shot["entity_ids"][0]
        pin={"subject_id":target,"purpose":"portrait","asset_id":"test-portrait","revision_id":"r1"}
        original=copy.deepcopy(old)
        first,second=old["shots"][:2]
        first["required_refs"]=[pin]
        second["required_refs"]=[pin]
        shot["source_refs"]=first["source_refs"]+second["source_refs"]
        recut={**project,"shots":[shot]}
        def carried():
            return _rebind_shared_pins(recut,old,original,"test-story")["shots"][0].get("required_refs",[])
        self.assertEqual(carried(),[pin])
        second["required_refs"]=[{**pin,"revision_id":"r2"}]
        self.assertEqual(carried(),[])
        second["required_refs"]=[pin]
        original["shots"][0]["required_refs"]=[pin]
        self.assertEqual(carried(),[])

    def test_offline_stale_spec_wrong_scope_and_unsafe_upload_are_rejected(self):
        pin = self.upload((12, 34, 56))
        self.bind(pin, None)
        with self.assertRaises(ValueError):
            self.bind(pin, None)
        with self.assertRaises(ValueError):
            self.bind(pin, "r1", spec_sha256="0" * 64)
        with self.assertRaises(ValueError):
            self.upload((34, 56, 78))
        with self.assertRaises(KeyError):
            studio_reference_media(self.service, "project-b", pin["asset_id"], "r1")
        with self.assertRaises(KeyError):
            bind_studio_reference(self.service, "project-b", {"asset_id": pin["asset_id"], "revision_id": "r1",
                "shot_id": "S002", "spec_parent_revision": None})
        for bad in ["C:/private/key", "file:///C:/private/key", "data:image/png;base64,!", "data:image/png;base64,YQ=="]:
            with self.assertRaises(ValueError):
                upload_studio_reference(self.service, self.pid, {"subject_id": "char:lin", "purpose": "portrait",
                    "data_uri": bad, "parent_revision": "r1"})

    def test_offline_collaboration_uses_published_version_and_story_id(self):
        catalog = studio_asset_catalog(self.service, self.pid)
        db = CollabStore(catalog["store_path"])
        run_id = "a" * 32
        story = "story-" + run_id
        db.publish_asset(story + "/source-project", self.service.project,
                         kind="input_project", producer="offline", story_id=story)
        project2 = dict(self.service.project, title="离线改稿")
        script = db.publish_asset(story + "/script", {"title": "离线改稿"}, kind="script", producer="offline", story_id=story)
        script_ref = {k: script[k] for k in ["asset_id", "revision_id", "sha256"]}
        db.publish_asset(story + "/production", {"project": project2, "script_ref": script_ref},
                         kind="production", producer="offline", story_id=story)
        view = studio_project_for_video(self.service, self.pid)
        self.assertEqual(view["run_id"], run_id)
        self.assertEqual(view["story_id"], story)
        self.assertEqual(view["project"]["title"], "离线改稿")
        self.assertEqual(view["script_ref"], script_ref)
        self.assertEqual(view["production_ref"]["revision_id"], "r1")
        self.assertIsNone(view["pending_script_ref"])
        db.publish_asset(story + "/script", {"title": "又一次离线改稿"}, kind="script", producer="offline", story_id=story,
                         parent_revision="r1")
        revision = studio_asset_catalog(self.service, self.pid)
        self.assertEqual(revision["script_ref"], script_ref)
        self.assertEqual(revision["pending_script_ref"]["revision_id"], "r2")
        other = studio_asset_catalog(self.service, "project-b")
        self.assertNotEqual(other["story_id"], story)

    def test_configured_sdk_image_keeps_receipt_and_validates_candidate_before_import(self):
        root = self.service.root
        env = root / ".env"
        env.write_text("OFFLINE_IMAGE_KEY=offline-image-secret\n", encoding="utf8")
        config = {"version": 1,
                  "connections": {"art": {"provider": "openai", "account": "studio",
                                          "api_key_env": "OFFLINE_IMAGE_KEY", "base_url": "https://image.invalid/v1"}},
                  "models": {"drawing": {"connection": "art", "adapter": "litellm", "sdk_provider": "openai",
                                         "model_id": "configured-drawing"}},
                  "routes": {"reference.image": "drawing"}}
        (root / "config").mkdir()
        (root / "config" / "models.json").write_text(json.dumps(config), encoding="utf8")
        self.service.model_routes = load_routes(env)
        self.service.video_config = SimpleNamespace(request_timeout=5)
        blob = offline_png((10, 20, 30))
        response = {"data": [{"b64_json": base64.b64encode(blob).decode("ascii")}],
                    "_provider_request_id": "req-art", "model": "returned-drawing", "usage": {"image_count": 1},
                    "_wire_receipt": {"wire_body_sha256": "a" * 64}}
        body = {"subject_id": "char:lin", "purpose": "portrait"}
        with patch("pianchang.sdk_transport.call_sdk", side_effect=[response, {"data": [{"b64_json": "!"}]}]) as sdk, \
                patch("pianchang.studio_assets._request_image", side_effect=AssertionError("unexpected native call")):
            pin = generate_studio_reference(self.service, self.pid, body)["pin"]
            self.assertEqual(sdk.call_args.args, (self.service.model_routes.resolve("reference.image"), "image_generation"))
            self.assertNotIn("n", sdk.call_args.kwargs)
            self.assertEqual(sdk.call_args.kwargs["size"], "1024x1024")
            self.assertIn("左眉有浅疤", sdk.call_args.kwargs["prompt"])
            with self.assertRaisesRegex(ValueError, "Base64"):
                generate_studio_reference(self.service, self.pid, {**body, "parent_revision": pin["revision_id"]})
            self.assertEqual(sdk.call_count, 2)
        catalog = studio_asset_catalog(self.service, self.pid)
        entry, = next(e["references"] for e in catalog["entities"] if e["id"] == "char:lin")
        self.assertEqual(studio_reference_media(self.service, self.pid, pin["asset_id"], pin["revision_id"])[0].read_bytes(), blob)
        origin = entry["origin"]
        self.assertEqual((origin["model"], origin["response_model"], origin["request_id"]),
                         ("configured-drawing", "returned-drawing", "req-art"))
        self.assertEqual((origin["provider"], origin["account"], origin["adapter"]), ("openai", "studio", "litellm"))
        self.assertEqual(origin["wire_receipt"]["wire_body_sha256"], "a" * 64)

    def _configure_ark_image(self, purpose="reference.image", **image_options):
        env = self.service.root / ".env"
        env.write_text("TEST_SEEDREAM_KEY=offline-seedream-key\n", encoding="utf8")
        endpoint = "https://ark.cn-beijing.volces.com/api/v3"
        config = {
            "version": 1,
            "connections": {"ark": {"provider": "volcengine-ark", "account": "studio-ark",
                                    "api_key_env": "TEST_SEEDREAM_KEY", "base_url": endpoint}},
            "models": {"drawing": {"connection": "ark", "adapter": "litellm", "sdk_provider": "openai",
                                   "model_id": "ep-offline", "image_size": "2048x2048", **image_options}},
            "routes": {purpose: "drawing"},
        }
        (self.service.root / "config").mkdir(exist_ok=True)
        (self.service.root / "config/models.json").write_text(json.dumps(config), encoding="utf8")
        self.service.model_routes = load_routes(env)
        self.service.video_config = SimpleNamespace(request_timeout=5)
        return endpoint

    def test_frozen_image_format_and_legacy_snapshot(self):
        self._configure_ark_image()
        legacy = self.service.model_routes.resolve("reference.image").snapshot()
        self.assertNotIn("image_response_format", legacy)
        self._configure_ark_image(image_response_format="b64_json")
        inline = self.service.model_routes.resolve("reference.image").snapshot()
        self._configure_ark_image(image_response_format="url")
        self.assertEqual(self.service.model_routes.restore(inline).image_response_format, "b64_json")
        self.assertIsNone(self.service.model_routes.restore(legacy).image_response_format)
        with self.assertRaisesRegex(ValueError, "image_response_format"):
            self._configure_ark_image(image_response_format="base64")
        with self.assertRaisesRegex(ValueError, "video.decision.*image_response_format"):
            self._configure_ark_image(purpose="video.decision", image_response_format="url")

    def test_image_model_refusal_requires_matching_sdk_receipt(self):
        from pianchang.studio_assets import _sdk_reference_image
        self._configure_ark_image()
        route = self.service.model_routes.resolve("reference.image")
        for status, code, rejected in ((404, "InvalidEndpointOrModel.NotFound", True),
                                       (404, "PathNotFound", False), (503, "InvalidEndpointOrModel.NotFound", False)):
            failure = ValueError("Offline provider error")
            failure.wire_receipt, failure.code = {"http_status": status}, code
            with self.subTest(status=status, code=code), patch("pianchang.sdk_transport.call_sdk", side_effect=failure):
                with self.assertRaises(ValueError) as caught:
                    _sdk_reference_image(route, "offline prompt", 1)
                self.assertIs(caught.exception, failure)
                self.assertEqual(getattr(failure, "rejected_image_model", None), "ep-offline" if rejected else None)

    @unittest.skipUnless(importlib.util.find_spec("litellm"), "optional LiteLLM SDK")
    def test_ark_inline_image_requires_only_the_original_post(self):
        import httpx

        endpoint = self._configure_ark_image(image_response_format="b64_json")
        blob = offline_png((27, 23, 42))
        replies = [{"b64_json": base64.b64encode(blob).decode("ascii")},
                   {"url": "https://fixture.invalid/unused?signature=offline"},
                   {"b64_json": "!"}]
        requests = []

        def respond(req):
            requests.append((req.method, str(req.url), json.loads(req.content)["response_format"]))
            return httpx.Response(200, headers={"x-tt-logid": f"inline-{len(requests)}"},
                                  json={"created": 1788888888, "data": [replies.pop(0)]})

        body = {"subject_id": "char:lin", "purpose": "portrait"}
        with patch("pianchang.sdk_transport._upstream_transport", return_value=httpx.MockTransport(respond)), \
                patch("pianchang.studio_assets._download_image", side_effect=AssertionError("unexpected CDN download")):
            pin = generate_studio_reference(self.service, self.pid, body)["pin"]
            for message in ("HTTP 200.*inline-2.*缺少.*b64_json", "HTTP 200.*inline-3.*Base64"):
                with self.assertRaisesRegex(ValueError, message):
                    generate_studio_reference(self.service, self.pid, {**body, "parent_revision": pin["revision_id"]})
        self.assertEqual(requests, [("POST", endpoint + "/images/generations", "b64_json")] * 3)
        path, _ = studio_reference_media(self.service, self.pid, pin["asset_id"], pin["revision_id"])
        self.assertEqual(path.read_bytes(), blob)
        asset, = next(e["references"] for e in studio_asset_catalog(self.service, self.pid)["entities"] if e["id"] == "char:lin")
        self.assertEqual(asset["origin"]["request_id"], "inline-1")

    @unittest.skipUnless(importlib.util.find_spec("litellm"), "optional LiteLLM SDK")
    def test_ark_image_route_preserves_provider_original_and_provenance(self):
        import httpx

        endpoint = self._configure_ark_image()
        returned_model = "doubao-seedream-5-0-flash-260915"
        image_url = "https://fixture.invalid/signed-source?signature=offline"
        blob = offline_png((17, 23, 42))
        sent = []

        def respond(req):
            sent.append(json.loads(req.content))
            self.assertEqual(req.method, "POST")
            self.assertEqual(str(req.url), endpoint + "/images/generations")
            self.assertEqual(req.headers["authorization"], "Bearer offline-seedream-key")
            return httpx.Response(200, headers={"x-tt-logid": "fixture-logid"}, json={
                "model": returned_model, "created": 1788888888, "data": [{"url": image_url}],
            })

        def download(req, timeout):
            self.assertEqual(req.full_url, image_url)
            self.assertFalse(req.has_header("Authorization"))
            reply = io.BytesIO(blob)
            reply.status = 200
            reply.headers = {"Content-Type": "image/png"}
            return reply

        with patch("pianchang.sdk_transport._upstream_transport", return_value=httpx.MockTransport(respond)), \
                patch("pianchang.studio_assets.request.build_opener", return_value=SimpleNamespace(open=download)):
            body = {"subject_id": "char:lin", "purpose": "portrait"}
            pin = generate_studio_reference(self.service, self.pid, body)["pin"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(set(sent[0]), {"model", "prompt", "size"})
        self.assertEqual((sent[0]["model"], sent[0]["size"]), ("ep-offline", "2048x2048"))
        path, _ = studio_reference_media(self.service, self.pid, pin["asset_id"], pin["revision_id"])
        self.assertEqual(path.read_bytes(), blob)
        self.assertEqual(pin["media_sha256"], hashlib.sha256(blob).hexdigest())
        catalog = studio_asset_catalog(self.service, self.pid)
        asset, = next(entity["references"] for entity in catalog["entities"] if entity["id"] == "char:lin")
        origin = asset["origin"]
        self.assertEqual((origin["provider"], origin["account"]), ("volcengine-ark", "studio-ark"))
        self.assertEqual((origin["model"], origin["response_model"]), ("ep-offline", returned_model))
        self.assertEqual((origin["request_id"], origin["provider_created_at"]), ("fixture-logid", 1788888888))
        with patch("pianchang.sdk_transport._upstream_transport", return_value=httpx.MockTransport(respond)), \
                patch("pianchang.studio_assets.request.build_opener",
                      return_value=SimpleNamespace(open=Mock(side_effect=URLError(image_url)))):
            with self.assertRaisesRegex(ValueError, "HTTP 200.*fixture-logid.*已生成.*下载.*失败") as failed:
                generate_studio_reference(self.service, self.pid, {**body, "parent_revision": pin["revision_id"]})
        self.assertNotIn("signature", str(failed.exception))
        self.assertEqual(len(sent), 2)

    def test_offline_image_request_receipt_and_origin(self):
        secret = "sk-offline-image-credential"
        self.service.video_config = SimpleNamespace(api_key=secret, base_url="https://offline.invalid/api/v1", request_timeout=5)
        blob = offline_png((12, 34, 56))
        payload = {"request_id": "offline-provider-image-id", "usage": {"image_count": 1}, "output": {
            "choices": [{"message": {"content": [{"image": "https://offline.invalid/image?Signature=offline-signature"}]}}]}}
        replies = [json.dumps(payload).encode(), blob]
        requests = []
        def offline_open(req, timeout):
            requests.append(req)
            result = io.BytesIO(replies.pop(0))
            result.status = 200
            result.headers = {"Content-Type": "application/json" if req.data else "image/png"}
            return result
        body = validate_studio_reference_body(self.service, self.pid, {"subject_id":"char:lin", "purpose":"portrait"})
        with patch("pianchang.studio_assets.request.build_opener", return_value=SimpleNamespace(open=offline_open)):
            made = generate_studio_reference(self.service, self.pid, body)
        self.assertEqual(len(requests), 2)
        self.assertIn("/multimodal-generation/generation", requests[0].full_url)
        self.assertNotIn("Authorization", dict(requests[1].header_items()))
        wire = json.loads(requests[0].data)
        self.assertEqual(wire["parameters"]["n"], 1)
        self.assertIn("左眉有浅疤", wire["input"]["messages"][0]["content"][0]["text"])
        catalog = studio_asset_catalog(self.service, self.pid)
        entry, = next(e["references"] for e in catalog["entities"] if e["id"] == "char:lin")
        self.assertEqual(entry["origin"]["request_id"], made["request_id"])
        self.assertEqual(entry["origin"]["entity_version"], 1)
        self.assertEqual(entry["origin"]["method"], "generated")
        with closing(sqlite3.connect(self.tracer.path)) as db:
            persisted = "\n".join(db.iterdump())
        self.assertNotIn(secret, persisted)
        self.assertNotIn("offline-signature", persisted)
        self.assertIn("offline-provider-image-id", persisted)
        with patch("pianchang.studio_assets._request_image", side_effect=AssertionError("unexpected second request")):
            with self.assertRaises(ValueError):
                generate_studio_reference(self.service, self.pid, body)
            with self.assertRaises(ValueError):
                generate_studio_reference(self.service, self.pid, {"subject_id":"char:lin", "purpose":"voice"})


if __name__ == "__main__":
    unittest.main()
