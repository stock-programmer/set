"""Offline integration. Real assets, manifests, FFmpeg, asset store and Trace; simulated cloud transports."""
import base64
import hashlib
import json
import struct
import subprocess
import tempfile
import unittest
import wave
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from pianchang.bailian_video import BailianVideoClient, VideoConfig, VideoError, config_for_references
from pianchang.collaboration_workflow import Collaboration, ref
from pianchang.media_tools import find_ffmpeg
from pianchang.reference_assets import import_reference, bind_project
from pianchang.reference_transport import prepare_shot_request
from pianchang.reference_usage import query_reference_usage
from tests.test_bailian_video import Response
from tests.test_video_collab_agent import OfflineDecisionClient
from trace_system import Tracer, use_context
from pianchang.video_agent import VideoRunner, compile_shot
from pianchang.video_collab_agent import run_video_agent


def portrait_fixture(shade):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    header = struct.pack(">IIBBBBB", 240, 240, 8, 2, 0, 0, 0)
    pixels = (b"\0" + bytes([shade, 80, 25]) * 240) * 240
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


class ReferenceIntegration(unittest.TestCase):
    def test_requirement_can_be_revised_after_an_attempt_without_a_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            tracer = Tracer(folder / "offline-traces")
            with use_context(None,tracer=tracer), tracer.trace("offline.unfinished_reference_attempt",kind="test"):
                session = Collaboration(folder / "collaborations","2"*32)
                store = session.store
                project = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json").read_text(encoding="utf8"))
                script = store.publish_asset("script",project["script_source"]["script"],kind="script",producer="script",
                    story_id=session.story,correlation_id=session.run_id,topic="script.revision_published")
                file = folder / "actor.png"
                prior_spec = None
                records = []
                for n in (1,2):
                    file.write_bytes(portrait_fixture(n))
                    pin = import_reference(store,story_id=session.story,asset_id="actor",subject_id="char:chen",purpose="portrait",
                                           path=file,parent_revision="r1" if n == 2 else None)
                    spec = bind_project(store,prior_spec["content"] if prior_spec else project,story_id=session.story,
                        shot_id="S003",pins=[pin],spec_asset_id="spec",parent_revision="r1" if n == 2 else None)
                    order = session.commission(script,spec,None,1)
                    records.append(session.lock_shot_references(spec["content"],script,None,order)["S003"])
                    prior_spec = spec
                self.assertEqual([record["revision_id"] for record in records],["r1","r2"])
                self.assertEqual(store.get_asset(records[0]["asset_id"],"r1")["content"]["required_refs"][0]["revision_id"],"r1")

    def test_mixed_project_tracks_reference_only_update_through_real_entrypoints(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            tracer = Tracer(folder / "offline-traces")
            with use_context(None, tracer=tracer), tracer.trace("offline.reference_integration", kind="test"):
                self._run(folder, tracer)

    def _run(self, folder, tracer):
        cfg = VideoConfig(api_key="offline-test-key", reference_model="wan2.7-r2v", output_dir=folder / "jobs")
        session = Collaboration(folder / "collaborations", "1"*32)
        session.write(id=session.run_id,title="离线参考更新",status="running",stages=[],productions=[])
        store = session.store
        store.subscribe("video", "script.revision_published", session.story)
        project = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json").read_text(encoding="utf8"))
        script = store.publish_asset("script",project["script_source"]["script"],kind="script",producer="script",
                                     story_id=session.story,correlation_id=session.run_id,topic="script.revision_published")
        portrait = folder / "portrait.png"
        portrait.write_bytes(portrait_fixture(30))
        register = dict(story_id=session.story, subject_id="char:chen")
        first = import_reference(store, asset_id="portrait", purpose="portrait", path=portrait, **register)
        voice_file = folder / "voice.wav"
        with wave.open(str(voice_file), "wb") as stream:
            stream.setparams((1,2,16000,0,"NONE","")); stream.writeframes(bytes(32000))
        voice = import_reference(store, asset_id="voice", purpose="voice", path=voice_file, **register)
        spec = bind_project(store, project, story_id=session.story, shot_id="S003", pins=[first,voice],spec_asset_id="spec")
        compiled = compile_shot(spec["content"], "S003")
        selected_model = config_for_references(cfg, [first, voice])
        with patch("pianchang.reference_transport.upload_voice", side_effect=AssertionError("Unexpected cloud upload")):
            dry, _ = prepare_shot_request(selected_model,compiled,store=store,story_id=session.story,dry_run=True)
            self.assertEqual(dry["pending_uploads"], [voice])
            with self.assertRaises(VideoError):
                prepare_shot_request(selected_model,compiled,store=store,story_id=session.story,request_options={"duration":16})
        diagnostic = folder / "offline.mp4"
        subprocess.run([find_ffmpeg(),"-loglevel","error","-f","lavfi","-i","color=blue:s=180x240:d=0.5",
                        "-c:v","libx264",str(diagnostic)],check=True)
        original_bytes = diagnostic.read_bytes()
        requests, transfers = [], []

        def response(value, *, raw=False):
            r = Response(value if raw else json.dumps(value).encode())
            r.status = 200
            return r

        def cloud(req, **_):
            if req.get_method() == "POST":
                payload = json.loads(req.data)
                requests.append(payload)
                return response({"output":{"task_id":"offline-task-"+str(len(requests)),"task_status":"PENDING"}})
            task_id = req.full_url.rsplit("/",1)[-1]
            return response({"output":{"task_id":task_id,"task_status":"SUCCEEDED",
                                       "video_url":"https://offline.invalid/" + task_id}})

        def storage(req, **_):
            if req.get_method() == "GET":
                return response({"data":{"policy":"offline-private-policy", "signature":"offline-private-signature",
                    "oss_access_key_id":"offline-private-access", "upload_host":"https://offline.oss-cn-beijing.aliyuncs.com",
                    "upload_dir":"dashscope-instant/offline/"+str(len(transfers)),
                    "x_oss_object_acl":"private","x_oss_forbid_overwrite":"true"}})
            self.assertIn(voice_file.read_bytes(), req.data)
            transfers.append(req.full_url)
            return response(b"",raw=True)

        def runner(config):
            client = BailianVideoClient(config)
            client._api_opener = Mock()
            client._api_opener.open.side_effect = cloud
            client._media_opener = Mock()
            def download(req, **_):
                identity = req.full_url.rsplit("/",1)[-1].encode()
                return response(original_bytes + struct.pack(">I",8+len(identity)) + b"free" + identity,raw=True)
            client._media_opener.open.side_effect = download
            return VideoRunner(config, client)

        def make(spec_asset, prior):
            order = session.commission(script,spec_asset,prior,3 if prior is None else 1)
            with patch("pianchang.collaboration_workflow.load_text_config",return_value=None), \
                 patch("pianchang.collaboration_workflow.load_config",return_value=cfg), \
                 patch("pianchang.collaboration_workflow.load_observer_config",return_value=None), \
                 patch("pianchang.video_collab_agent.BailianTextClient",return_value=OfflineDecisionClient()), \
                 patch("pianchang.video_collab_agent.VideoWorkspace.acceptance_gate",return_value={
                     "passed":True,"issues":[],"scope":"explicit_offline_mock_transport_only"}):
                delivery, result = session.worker(order["message_id"])
            self.assertEqual(result["payload"]["production"],ref(delivery))
            return delivery

        oss = Mock()
        oss.open.side_effect = storage
        with patch("pianchang.reference_transport.request.build_opener",return_value=oss), patch("pianchang.video_collab_agent.VideoRunner",side_effect=runner):
            r1 = make(spec,None)
            portrait.write_bytes(portrait_fixture(75))
            second = import_reference(store,asset_id="portrait",purpose="portrait",path=portrait,parent_revision="r1",**register)
            spec2 = bind_project(store,spec["content"],story_id=session.story,shot_id="S003",pins=[second],
                                 spec_asset_id="spec",parent_revision="r1")
            r2 = make(spec2,r1)
        self.assertEqual([p["model"] for p in requests],["wan2.6-t2v","wan2.6-t2v","wan2.7-r2v","wan2.7-r2v"])
        self.assertEqual(len(transfers),2)
        for pin,payload in zip((first,second),requests[2:]):
            media = payload["input"]["media"][0]
            self.assertEqual(hashlib.sha256(base64.b64decode(media["url"].split(",")[1])).hexdigest(),pin["media_sha256"])
            self.assertTrue(media["reference_voice"].endswith(voice["media_sha256"]+".wav"))
            query = query_reference_usage(store,"portrait",pin["revision_id"],jobs_dir=cfg.output_dir)
            self.assertEqual((query["submitted_count"],query["accepted_count"]), (1,1))
        self.assertEqual(r2["content"]["impact"]["regenerate"],["S003"])
        self.assertEqual(r2["content"]["impact"]["reuse"],["S001","S002"])
        self.assertEqual(r2["content"]["impact"]["changed_sources"],[])
        self.assertEqual(r2["content"]["impact"]["shots"]["S003"]["changed_components"],["required_refs"])
        self.assertEqual(r2["content"]["media_refs"]["S001"],r1["content"]["media_refs"]["S001"])
        self.assertEqual(r2["content"]["script_ref"],r1["content"]["script_ref"])
        receipt = r2["content"]["shots"]["S003"]["reference_receipt"]
        self.assertEqual(receipt["status"],"ACCEPTED")
        report_path = Path(r2["content"]["reports"]["S003"]["report_path"])
        results = json.loads(report_path.read_text(encoding="utf8"))["results"]
        self.assertEqual(next(row["status"] for row in results if row["criterion_id"] == "reference_submission"), "PASS")
        exported = json.dumps(tracer.export_trace(receipt["trace_id"]))
        for secret in ("offline-private-policy","offline-private-signature","offline-private-access"):
            self.assertNotIn(secret,exported)
        with self.assertRaises(VideoError), patch("pianchang.reference_transport.upload_voice",side_effect=AssertionError("unexpected upload")):
            run_video_agent(script=script["content"],script_ref=ref(script),directory=folder / "unsupported",text_config=None,
                video_config=VideoConfig(model="wan2.6-t2v"),client=Mock(),template_project=spec2["content"],reference_store=store)
