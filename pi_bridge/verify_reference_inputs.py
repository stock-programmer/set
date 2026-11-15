"""Live acceptance of reference preparation through the project's Pi conversation.

This is an explicitly live production command, not a unit test. It asks Pi once
to prepare real conditioning inputs, and never asks it to submit video generation.
"""
import argparse
import json
import time
import uuid
from pathlib import Path
from urllib.request import Request,urlopen

from trace_system import current_context,event,get_tracer,traced


def _http(path,body=None):
    base="http://127.0.0.1:8765"
    raw=json.dumps(body,ensure_ascii=False).encode("utf-8") if body is not None else None
    event("reference_input_verification.http_request",{"url":base+path,"body":body,"method":"POST" if body else "GET"},kind="http")
    with urlopen(Request(base+path,data=raw,headers={"Origin":base,"Content-Type":"application/json"}),timeout=45) as response:
        result=json.load(response)
        event("reference_input_verification.http_response",{"status":response.status,"trace_id":response.headers.get("X-Trace-Id"),
            "body":result},kind="http")
        return result


@traced("cli.pi.verify_reference_inputs",kind="cli",capture_inputs=False,capture_output=False)
def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",required=True);parser.add_argument("--message-id");args=parser.parse_args(argv)
    before=_http("/api/workspace?project="+args.project)
    cid=before["studio"]["video_execution"]["commission_id"]
    mid=args.message_id or uuid.uuid5(uuid.NAMESPACE_URL,"reference-inputs/1.prepare/"+args.project+"/"+str(cid)).hex
    text=("实现验收：请由你这个项目的 Pi 视频制作 Agent 自主处理当前冻结委托中 S001、S002、S003 的参考输入超限。"
          "先依据当前诊断，分别调用 prepare_reference_inputs 准备三个镜头的兼容输入；一个失败要换 balanced_boards 并继续其他镜头。"
          "独立视觉核验必须有真实来源证据，保留所有原始参考、人物音色、剧本、镜头数量、机位和验收标准。"
          "本轮范围仅为编排和核验参考输入：不要调用 continue_authorized_production，不要生成视频，不要发起改稿。"
          "若两个策略都受阻，如实说明具体证据与原因。完成后用 read_reference_inputs 核对各镜头实际状态，"
          "报告原图片数、提交槽位数和参考板审核结论；finish_task 使用 responded，本轮不代表成片通过。")
    submitted=_http("/api/pi/messages",{"scope":{"project":args.project},"role":"video","text":text,"mode":"work","message_id":mid})
    jid=(submitted.get("job") or {}).get("id") or submitted["message"].get("job_id")
    print(json.dumps({"submitted_job":jid,"message_id":mid,"session_id":submitted["session_id"]},ensure_ascii=False),flush=True)
    deadline=time.monotonic()+1500;previous=None
    while time.monotonic()<deadline:
        conversation=_http("/api/pi/sessions?project="+args.project+"&role=video")
        execution=conversation.get("execution") or {}
        if execution.get("id")==jid:
            progress=execution.get("agent_progress") or {};marker=(execution["status"],progress.get("step"),progress.get("type"),progress.get("toolName"))
            if marker!=previous:print(json.dumps({"status":execution["status"],"progress":progress,"message":execution.get("message","")[:150]},ensure_ascii=False),flush=True);previous=marker
            if execution["status"] not in {"queued","running"} and not conversation["session"].get("active_job"):break
        time.sleep(3)
    else:raise ValueError("项目 Pi 尚未结束，原任务仍保留；此命令不会重复投递。")
    after=_http("/api/workspace?project="+args.project)
    inputs=after["studio"]["video_execution"].get("reference_inputs",[])
    rows=[r for r in inputs if r["shot_id"] in {"S001","S002","S003"}]
    # Compile the exact real-source requests locally, without uploading voices or
    # POSTing a video task. This is explicitly a dry preflight, not cloud acceptance.
    from pianchang.collab_store import CollabStore
    from pianchang.bailian_video import load_config,config_for_references
    from pianchang.reference_transport import prepare_shot_request
    from pianchang.video_agent import compile_shot
    store=CollabStore(after["studio"]["assets"]["store_path"])
    spec=after["studio"]["assets"]["spec"];project=store.get_asset(spec["asset_id"],spec["revision_id"])["content"]
    config=load_config(Path(__file__).resolve().parent.parent/".env");preflights=[]
    for row in rows:
        if not row["ready"]:continue
        compiled=compile_shot(project,row["shot_id"])
        compiled["metadata"].update(reference_story_id=after["studio"]["story_id"],reference_bundle=row["bundle"])
        local,_=prepare_shot_request(config_for_references(config,compiled["metadata"]["required_refs"]),compiled,store=store,
                                     story_id=after["studio"]["story_id"],dry_run=True)
        preflights.append({"shot_id":row["shot_id"],"dry_run":local["dry_run"],"model":local["model"],
            "source_reference_count":len(compiled["metadata"]["required_refs"]),
            "effective_image_count":sum(p["purpose"]!="voice" for p in local["required_refs"]),
            "voice_count":len(local["pending_uploads"]),"status":"PASS"})
    checks={"three_shots_ready":len(rows)==3 and all(r["ready"] for r in rows),
        "within_five_slots":all(r["provider_image_count"] is not None and r["provider_image_count"]<=5 for r in rows),
        "real_request_local_preflight":len(preflights)==3 and all(p["status"]=="PASS" for p in preflights),
        "same_commission":cid==after["studio"]["video_execution"]["commission_id"],
        "same_script":before["studio"]["script"]["script_ref"]==after["studio"]["script"]["script_ref"],
        "same_spec":before["studio"]["assets"]["spec"]==after["studio"]["assets"]["spec"],
        "video_not_submitted":(before["studio"]["video_execution"].get("usage") or {}).get("reserved_video_generations",0)==(after["studio"]["video_execution"].get("usage") or {}).get("reserved_video_generations",0)}
    for name,value in checks.items():event("reference_input_verification.check",{"path":name,"expected":True,"actual":value,"status":"PASS" if value else "FAIL"},kind="validation")
    report={"scope":"live project Pi input orchestration, not video acceptance","project_id":args.project,"job_id":jid,"message_id":mid,
            "trace_id":execution.get("trace_id"),"session":conversation["session"],"execution":execution,"checks":checks,"inputs":rows,
            "preview_ready":after["studio"]["preview_gate"]["ready"],"commission_usage":after["studio"]["video_execution"].get("usage"),
            "local_dry_run_preflights":preflights,"cloud_video_acceptance_tested":False}
    path=Path(__file__).resolve().parent.parent/"outputs/web"/("reference-input-verification-"+jid+".json")
    if path.exists():path=path.with_stem(path.stem+"-"+current_context()["trace_id"][:8])
    from pianchang.reference_inputs import persist_inputs
    persist_inputs(path,report)
    print(json.dumps({"report":str(path),"checks":checks,"trace_id":report["trace_id"],"preview_ready":report["preview_ready"]},ensure_ascii=False),flush=True)
    return 0 if all(checks.values()) else 1


if __name__=="__main__":
    with get_tracer().storage_session():raise SystemExit(main())
