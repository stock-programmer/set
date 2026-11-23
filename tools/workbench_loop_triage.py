"""Send the recorded failure to Codex for the watcher's next recovery decision."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from trace_system import current_context, event, traced
from tools.workbench_loop_observer import original_failure_evidence


FINDINGS = ("missing_selected_asset", "provider_receipt_unknown", "version_mismatch",
            "voice_connection_failed_before_send","voice_connection_failed_delivery_unknown","voice_observer_missing_input",
            "planner_connection_failed_before_send", "local_code_failure", "authorization_required", "insufficient_evidence")
NEXT_CHECKS = ("check_current_spec", "reconcile_original_receipt", "check_selected_version",
               "inspect_local_regression", "check_commission", "await_new_evidence",
               "check_voice_connection","check_original_voice_recovery","reinspect_original_audio",
               "check_planner_connection","resume_verified_commission","repair_and_verify_local_fault")
RESULT_SCHEMA = {
    "type":"object","additionalProperties":False,
    "properties":{"finding":{"type":"string","enum":list(FINDINGS)},
                  "next_check":{"type":"string","enum":list(NEXT_CHECKS)}},
    "required":["finding","next_check"],
}


@traced("workbench_loop.triage_failure",kind="tool",capture_inputs=False)
def triage_failure(root, job, evidence, *, timeout_s=240):
    """Summarize the next step. Execution and completion have independent receipts."""
    context=current_context() or {}
    receipt={"job_id":job.get("id"),"trace_id":context.get("trace_id")}
    cli=shutil.which("codex.exe" if os.name=="nt" else "codex")
    if not cli:
        return {**receipt,"status":"unavailable","reason":"codex_unavailable"}
    details={"job_id":receipt["job_id"],"job_kind":job.get("kind"),"job_status":job.get("status"),
             "commission_recorded":bool(job.get("commission_id") or job.get("message_id"))}
    if evidence.get("reason")=="original_job_changed":
        # The payload is whitelisted by the Trace reader: enum codes and repository-relative frames only.
        details.update(original_failure_evidence(root,job))
    for key in ("reason","status","related_job_id","source_revision","error_type","trace_id","code"):
        value=evidence.get(key)
        if isinstance(value,str) and re.fullmatch(r"[a-zA-Z0-9_./-]{1,96}",value):
            details[key]=value
    for key in ("receipt_unknown","missing_count","original_missing_count","current_shot_count","safe_to_continue"):
        if type(evidence.get(key)) in (bool,int):
            details[key]=evidence[key]
    fault=evidence.get("fault") or {}
    if fault.get("code") and re.fullmatch(r"[A-Za-z0-9_]{1,80}",str(fault["code"])):
        details["local_fault"]={"code":fault["code"]}
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,64}",str(fault.get("error_type",""))):
            details["local_fault"]["error_type"]=fault["error_type"]
        details["local_fault"]["frames"]=[{"file":frame["file"],"line":frame["line"]}
            for frame in (fault.get("frames") or []) if isinstance(frame,dict)
            and re.fullmatch(r"(?:pianchang|pi_bridge|tools|web)/(?:[\w-]+/)*[\w-]+\.(?:py|js)",str(frame.get("file","")))
            and type(frame.get("line")) is int and frame["line"]>0][-4:]
    failure=evidence.get("transport_failure") or {}
    kind=failure.get("request_kind")
    if kind in {"audio","model"}:
        attempt={"request_kind":kind}
        for key,values in (("error_type",{"ConnectError","ConnectTimeout","ProxyError","ReadTimeout","WriteTimeout",
                                "WriteError","ReadError","RemoteProtocolError","TransportError"}),
                           ("dispatch",{"unknown","not_sent"}),("proxy_mode",{"proxy","direct"}),
                           ("phase",{"unknown","before_send","connect_or_tls","connect_tcp","start_tls","send_request_headers",
                               "send_request_body","receive_response_headers","receive_response_body"}),
                           ("proof",{"legacy_httpx_connect_before_origin_write"})):
            if failure.get(key) in values:
                attempt[key]=failure[key]
        for key in ("trace_id","span_id","commission_id","run_id"):
            if re.fullmatch(r"[a-f0-9]{32}",str(failure.get(key))):
                attempt[key]=failure[key]
        for key in ("request_seq","error_seq","end_seq","errno","winerror"):
            if type(failure.get(key)) is int:
                attempt[key]=failure[key]
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}",str(failure.get("ssl_reason"))):
            attempt["ssl_reason"]=failure["ssl_reason"]
        details["original_audio_attempt" if kind=="audio" else "original_planner_attempt"]=attempt
        connectivity=evidence.get("connectivity") or {}
        if connectivity.get("status") in {"http_response","connection_failed","original_route_unavailable","reachable","retry_later"}:
            probe={"status":connectivity["status"]}
            if type(connectivity.get("http_status")) is int:
                probe["http_status"]=connectivity["http_status"]
            details["current_voice_connection" if kind=="audio" else "current_planner_connection"]=probe
        if kind=="model":
            for key in ("remaining_video_generations","remaining_model_calls"):
                if type(evidence.get(key)) is int:
                    details[key]=evidence[key]
    review=evidence.get("voice_review") or {}
    if review.get("observer_claimed_missing_input") is True:
        details["observer_claimed_missing_input"]=True
        for key in ("provider_audio_tokens","encoded_bytes","request_seq","response_seq"):
            if type(review.get(key)) is int:
                details[key]=review[key]
        details["original_audio_first"]=review.get("input_format")=="audio_first"
        details["corrected_voice_format"]=review.get("corrected_input_format")=="voice-audition/input-audio-last/1"
    prompt=("你正在参与片场工作台的自动修复循环。请根据原始故障和当前复查证据，给出故障归因以及推进原任务的下一步。"
            "有可定位的本地代码故障时选择 repair_and_verify_local_fault，修复执行器将接收源码和失败检查。"
            "dispatch=not_sent 表示这一个原始请求在发送前已失败。规划请求未发出时先检查原线路，"
            "若线路已通且冻结委托的 safe_to_continue=true，选择 resume_verified_commission。"
            "dispatch=unknown 时核对原回执。当前线路探测只说明现在的网络状态。"
            "供应商统计音频输入而听审声称没有音频时，应针对同一份媒体核查听审。"
            "排查建议交给旁路执行，修复和原业务的验证结果由执行器记录，业务再次失败会带入新的证据。\n"
            +json.dumps(details,ensure_ascii=False))
    try:
        with tempfile.TemporaryDirectory(prefix="pianchang-triage-") as directory:
            workspace=Path(directory)
            schema=workspace/"schema.json"
            result=workspace/"result.json"
            schema.write_text(json.dumps(RESULT_SCHEMA),encoding="utf-8")
            argv=[cli,"exec","--ignore-user-config","--ephemeral","--sandbox","read-only",
                  "--skip-git-repo-check","--disable","shell_tool","--disable","unified_exec",
                  "--disable","apps","--disable","plugins","-c",'web_search="disabled"',
                  "-c",'approval_policy="never"',"--output-schema",str(schema),
                  "--output-last-message",str(result),"-"]
            event("workbench_loop.triage.request",{"argv":argv,"prompt":prompt,"schema":RESULT_SCHEMA},kind="subprocess")
            child=subprocess.run(argv,input=prompt.encode(),cwd=workspace,stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,timeout=timeout_s,
                                 creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            raw=result.read_text(encoding="utf-8-sig") if result.is_file() else None
            event("workbench_loop.triage.response",{"exit":child.returncode,"result":raw},kind="subprocess")
            if child.returncode or not raw:
                return {**receipt,"status":"unavailable","reason":"codex_no_receipt"}
            answer=json.loads(raw)
            if not isinstance(answer,dict) or answer.get("finding") not in FINDINGS or answer.get("next_check") not in NEXT_CHECKS:
                raise ValueError("invalid diagnostic receipt")
            return {**receipt,"status":"reported","finding":answer["finding"],"next_check":answer["next_check"]}
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        event("workbench_loop.triage.error",{"type":type(exc).__name__},kind="error")
        return {**receipt,"status":"unavailable","reason":type(exc).__name__}
