"""Explicit offline HTTP flow: real FFmpeg files, mocked semantic verdicts, no paid APIs."""
import copy
import http.client as http_client
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.acceptance import create_contract,file_sha256
from pianchang.app_service import AppService,read_json,write_json
from pianchang.collaboration_workflow import ref
from pianchang.episode_export import export_preview
from pianchang.media_tools import find_ffmpeg,inspect_media
from pianchang.preview_access import preview_status,require_preview,PreviewBlocked
from pianchang.story_spec import digest_json
from pianchang.studio_assets import studio_project_for_video
from pianchang.studio_jobs import _session,_source_script,_handoff_spec
from trace_system import use_context
from pianchang.video_collab_agent import analyze_impact
from pianchang.web_server import create_server


class PreviewAccessTests(unittest.TestCase):
    def test_http_full_gate_flow_and_stale_addresses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with patch.dict(os.environ,{"AGENT_TRACE_DIR":str(root/"traces")}),\
                    patch("urllib.request.OpenerDirector.open",side_effect=AssertionError("Offline verification forbids provider requests")):
                (root/".env").write_text("# explicit offline verification\n",encoding="utf-8")
                project=read_json(Path(__file__).resolve().parents[1]/"fixtures/story_project.json")
                write_json(root/"fixtures/story_project.json",project)
                service=AppService(root)
                try:
                    with use_context(None,tracer=service.tracer),service.tracer.trace("test.preview.complete_http_flow",inputs={"label":"explicit offline; semantic verdicts are mocks"}) as task:
                        pid=next(iter(service._discover()));view=studio_project_for_video(service,pid)
                        session=_session(service,view["run_id"],project["title"],pid)
                        script=_source_script(session,view)
                        spec=_handoff_spec(session,script,view,{"project_sha256":digest_json(project)})
                        view=studio_project_for_video(service,pid);project=view["project"]
                        plan={"project_id":pid,"project_sha256":digest_json(project),"script_ref":view["script_ref"],
                              "new_shots":[s["id"] for s in project["shots"]],"retake_shots":[],"reuse_shots":[]}
                        plan["plan_id"]=digest_json(plan)
                        request=session.commission(script,spec,None,len(plan["new_shots"]),shooting_plan=plan)
                        session.store.claim("video",message_id=request["message_id"])
                        session.store.ack("video",request["message_id"],"accepted")
                        work=session.folder/("video-spec-"+request["message_id"])
                        write_json(work/"project.json",project)
                        commission_job={"id":"f"*32,"kind":"studio_video","project_id":pid,"status":"interrupted",
                                        "run_id":view["run_id"],"message_id":request["message_id"],"plan_id":plan["plan_id"]}
                        service.state["jobs"].append(commission_job)
                        service._save()
                        clips={};inspections={};impact=analyze_impact(project,None)
                        diagnostic=root/"outputs/offline-diagnostic.mp4";diagnostic.parent.mkdir(exist_ok=True)
                        subprocess.run([str(find_ffmpeg()),"-nostdin","-v","error","-f","lavfi","-i",
                            "color=c=blue:s=160x240:r=10:d=1","-c:v","libx264",str(diagnostic)],check=True)
                        for shot in project["shots"]:
                            sid=shot["id"];folder=root/"outputs/videos"/sid;folder.mkdir(parents=True)
                            media=folder/"video.mp4";media.write_bytes(diagnostic.read_bytes())
                            job={"base_url":"explicit-offline-mock","request":{},"metadata":{"shot_id":sid,"shooting_plan_id":plan["plan_id"],
                                "project_sha256":digest_json(project),"script_asset_ref":view["script_ref"],"dependency_sha256":impact["shots"][sid]["target"]["sha256"]}}
                            job.update(request_sha256=digest_json(job),status="SUCCEEDED",task_id="offline-"+sid,
                                       local_video_path=str(media),download_sha256=file_sha256(media))
                            write_json(folder/"job.json",job)
                            clips[sid]={"shot_id":sid,"operation":"generated","media_path":str(media),"media_sha256":file_sha256(media),"manifest_path":str(folder/"job.json"),
                                "generation_job_sha256":digest_json(job),"task_id":job["task_id"],"origin_script_ref":view["script_ref"],"script_ref":view["script_ref"],
                                "dependency_sha256":job["metadata"]["dependency_sha256"]}
                            inspections[sid]=inspect_media(media,session.folder/"offline-inspections"/sid,interval_s=1,max_frames=2)
                        write_json(work/"clips.json",clips)
                        preview=export_preview([{"shot_id":s["id"],"path":clips[s["id"]]["media_path"],"sha256":clips[s["id"]]["media_sha256"]} for s in project["shots"]],session.folder/"offline-preview")
                        preview["export_manifest_sha256"]=digest_json(read_json(preview["manifest_path"]))
                        prior=None;report_paths=[]
                        def publish(verdict,*,omit_required=False):
                            nonlocal prior
                            reports={}
                            for shot in project["shots"]:
                                sid=shot["id"];folder=work/"reviews"/(str(len(report_paths))+"-"+sid)
                                contract=create_contract(project,sid,width=160,height=240)
                                rows=[{"criterion_id":r["criterion_id"],"required":r["required"],"status":verdict if sid=="S001" else "PASS",
                                    "reason":"explicit offline mocked semantic verdict"} for r in contract["requirements"]]
                                if omit_required and sid=="S001":rows=[r for r in rows if r["criterion_id"]!="action_sequence"]
                                report={"schema_version":"explicit-offline-report/1","shot_id":sid,"project_sha256":digest_json(project),
                                    "media_sha256":clips[sid]["media_sha256"],"inspection_sha256":digest_json(inspections[sid]),
                                    "status":verdict if sid=="S001" else "PASS","task_complete":verdict=="PASS" or sid!="S001","results":rows}
                                write_json(folder/"contract.json",contract);write_json(folder/"report.json",report);report_paths.append(folder/"report.json")
                                reports[sid]={"shot_id":sid,"report_path":str(folder/"report.json"),"report_sha256":digest_json(report),"inspection_path":inspections[sid]["manifest_path"],
                                    "status":report["status"],"unresolved":[] if report["status"]=="PASS" else [r["criterion_id"] for r in rows if r["required"]]}
                            write_json(work/"reports.json",reports)
                            package={"project":project,"script_ref":view["script_ref"],"spec_ref":view["spec"],"shots":clips,"reports":reports,
                                "impact":impact,"media_refs":{},"preview":preview,"media_accepted":True,"label":"explicit offline mock verdicts"}
                            prior=session.store.publish_asset(view["story_id"]+"/production",package,kind="production",producer="video",story_id=view["story_id"],
                                parent_revision=prior["revision_id"] if prior else None,correlation_id=view["run_id"])
                            return prior
                        server=create_server(service,port=0);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                        port=server.server_address[1]
                        def http(method,path,body=None,headers=None):
                            connection=http_client.HTTPConnection("127.0.0.1",port,timeout=120)
                            extra={"Origin":f"http://127.0.0.1:{port}","Content-Type":"application/json",**(headers or {})}
                            connection.request(method,path,json.dumps(body) if body is not None else None,extra)
                            response=connection.getresponse();data=response.read();status=response.status;connection.close();return status,data
                        try:
                            html=http("GET","/")[1].decode();self.assertEqual(html.count('data-step="'),4)
                            progress_url=f"/api/projects/{pid}/studio/progress?job_id="+commission_job["id"]
                            def read_progress():
                                status,body=http("GET",progress_url)
                                self.assertEqual(status,200,body)
                                return json.loads(body)
                            progress=read_progress()
                            self.assertFalse(progress["preview_gate"]["ready"])
                            shot=next(s for s in progress["shots"] if s["shot_id"]=="S001")
                            self.assertEqual((shot["status"],shot["acceptance_status"]),("ready","PENDING"))
                            draft_url=shot["preview_url"]
                            sample=diagnostic.read_bytes()[:8]
                            self.assertEqual(http("GET",draft_url,headers={"Range":"bytes=0-7"}),(206,sample))
                            publish("FAIL");self.assertFalse(preview_status(service,pid)["ready"])
                            progress=read_progress()
                            self.assertFalse(progress["preview_gate"]["ready"])
                            self.assertEqual({r["shot_id"]:r["acceptance_status"] for r in progress["shots"]},
                                             {sid:"FAIL" if sid=="S001" else "PASS" for sid in clips})
                            self.assertEqual(http("GET",draft_url,headers={"Range":"bytes=0-7"}),(206,sample))
                            self.assertEqual(http("GET",draft_url.replace(shot["media_sha256"],"0"*64))[0],404)
                            clip_url=service.asset(clips["S001"]["media_path"],project_id=pid)
                            legacy_url=service.asset(preview["preview_path"])
                            for method,headers in [("GET",{}),("HEAD",{}),("GET",{"Range":"bytes=0-7"})]:
                                self.assertEqual(http(method,clip_url,headers=headers)[0],409)
                            self.assertEqual(http("GET",legacy_url)[0],409)
                            self.assertEqual(http("POST",f"/api/projects/{pid}/export",{})[0],409)
                            detail=json.loads(http("GET",f"/api/projects/{pid}")[1]);self.assertIsNone(detail["export"])
                            publish("UNKNOWN");self.assertFalse(preview_status(service,pid)["ready"])
                            publish("PASS",omit_required=True);self.assertFalse(preview_status(service,pid)["ready"])
                            package=publish("PASS");gate=require_preview(service,pid);self.assertTrue(gate["ready"])
                            detail=json.loads(http("GET",f"/api/projects/{pid}")[1]);self.assertTrue(detail["preview_gate"]["ready"])
                            url=detail["export"]["video_url"]
                            self.assertEqual(http("GET",url)[0],200)
                            self.assertEqual(http("HEAD",url)[0],200)
                            self.assertEqual(http("GET",url,headers={"Range":"bytes=0-7"})[0],206)
                            self.assertEqual(http("GET",clip_url)[0],200)
                            status,body=http("POST",f"/api/projects/{pid}/export",{});self.assertEqual(status,202)
                            jid=json.loads(body)["job"]["id"];deadline=time.monotonic()+120
                            while time.monotonic()<deadline:
                                job=next(j for j in service.list_jobs() if j["id"]==jid)
                                if job["status"] not in {"queued","running"}:break
                                time.sleep(.1)
                            self.assertEqual(job["status"],"succeeded",job)
                            report_path=Path(package["content"]["reports"]["S001"]["report_path"])
                            original=report_path.read_bytes();report_path.write_bytes(original+b" ")
                            # Formatting leaves canonical hashes unchanged; actual semantic tamper must revoke access.
                            changed=read_json(report_path);changed["results"][0]["status"]="FAIL";write_json(report_path,changed)
                            self.assertEqual(http("GET",url)[0],409);report_path.write_bytes(original)
                            spec=session.store.get_asset(view["spec"]["asset_id"],view["spec"]["revision_id"])
                            session.store.publish_asset(spec["asset_id"],spec["content"],kind=spec["kind"],producer=spec["producer"],parent_revision=spec["revision_id"],story_id=view["story_id"])
                            self.assertEqual(http("GET",url)[0],409,"Old URL cannot survive a newer spec revision")
                            self.assertTrue(service.tracer.verify(task.trace_id)["ok"])
                        finally:server.shutdown();server.server_close();worker.join(3)
                finally:service.close()


if __name__=="__main__":unittest.main()
