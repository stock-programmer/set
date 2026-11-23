"""Correlate an immutable audition and its original observer request before re-inspection."""
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from pianchang.model_routes import load_routes
from pianchang.story_spec import digest_json
from trace_system import TraceError,traced


FORMAT = "voice-audition/input-audio-last/1"
_NO_INPUT = re.compile(r"no (?:audio|sound) (?:sample )?(?:was |is )?(?:provided|supplied|available)|"
                       r"(?:not|never) (?:receive[d]?|provided with) (?:an? |the |any )?audio|"
                       r"未(?:收到|提供)(?:任何|实际|可听的|可听)?音频|没有(?:收到|提供|可听|可用)(?:的|实际)?音频",re.I)

def _route_digest(routes):
    route=routes.resolve("voice.review")
    if route.provider=="openai" and route.adapter=="openai":
        return digest_json([route.base_url.rstrip("/")+"/chat/completions",route.model_id])
    return None


def review_evidence(root, job, item, unit, catalog):
    if (job.get("kind") not in {"asset_production","asset_regenerate"} or item.get("purpose")!="voice"
            or item.get("stage")!="review" or item.get("verdict")!="UNKNOWN" or not unit
            or job.get("cancel_requested") or job.get("trace_incomplete") or unit.get("selected_pin")):
        return None
    pin, report_ref = item.get("candidate"),item.get("review_ref")
    if not pin or not report_ref or unit.get("candidate")!=pin or unit.get("job_id")!=job["id"]:
        return None
    try:
        return _review_evidence(Path(root),job,item,pin,report_ref,catalog)
    except TraceError:
        raise
    except (OSError,KeyError,ValueError,TypeError,IndexError,sqlite3.Error):
        return None


def _review_evidence(root,job,item,pin,report_ref,catalog):
    trace, span = item["trace_id"], item["span_id"]
    if trace!=job["trace_id"] or not all(re.fullmatch("[0-9a-f]{32}",s) for s in (trace,span)):
        return None
    store=Path(catalog["store_path"])
    with closing(sqlite3.connect(store.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        raw=db.execute("SELECT content FROM assets WHERE story_id=? AND asset_id=? AND revision_id=? AND sha256=?",
                       (catalog["story_id"],report_ref["asset_id"],report_ref["revision_id"],report_ref["sha256"])).fetchone()
    if not raw:
        return None
    report=json.loads(raw[0])
    if (digest_json(report)!=report_ref["sha256"] or report["pin"]!=pin or report["candidate"]!=pin
            or report["span_id"]!=span or report["trace_id"]!=trace or report["verdict"]!="UNKNOWN"
            or report["provider"].get("evidence_scope")!="candidate_audio"
            or report["provider"].get("media_sha256")!=pin["media_sha256"]):
        return None
    sql="""SELECT e.trace_seq,e.span_id,e.name,e.data FROM events e
           JOIN events http ON http.trace_id=e.trace_id AND http.span_id=e.span_id
               AND http.kind='span.start' AND http.name='bailian.vision.http'
           JOIN events listen ON listen.trace_id=http.trace_id AND listen.span_id=http.parent_span_id
               AND listen.kind='span.start' AND listen.name='asset.voice.listen'
           WHERE e.trace_id=? AND listen.parent_span_id=? AND e.name IN ('http.request','http.response','http.error')
           ORDER BY e.trace_seq"""
    with closing(sqlite3.connect((root/"outputs/traces/trace.sqlite3").resolve().as_uri()+"?mode=ro",uri=True)) as db:
        events=db.execute(sql,(trace,span)).fetchall()
    if len(events)!=2 or [row[2] for row in events]!=["http.request","http.response"] or events[0][1]!=events[1][1]:
        return None
    req,resp=[json.loads(row[3]) for row in events]
    if req["method"]!="POST" or resp["http_status"]!=200 or resp.get("body_truncated"):
        return None
    [msg]=req["body"]["messages"]
    parts=msg["content"]
    if msg["role"]!="user" or [p["type"] for p in parts]!=["input_audio","text"]:
        return None
    audio=parts[0]["input_audio"]
    data=audio["data"]
    if audio["format"]!="wav" or not isinstance(data,dict) or data.get("omitted")!="inline_media":
        return None
    digest=pin["media_sha256"]
    if not re.fullmatch("[0-9a-f]{64}",digest):
        return None
    path=store.parent/"reference-blobs"/digest[:2]/digest
    if not 0<path.stat().st_size<=8*1024*1024:
        return None
    raw=path.read_bytes()
    encoded=base64.b64encode(raw)
    if (hashlib.sha256(raw).hexdigest()!=digest or data["encoded_bytes"]!=len(encoded) or
            data["sha256"]!=hashlib.sha256(encoded).hexdigest()):
        return None
    response=resp["body"]
    tokens=response["usage"]["prompt_tokens_details"]["audio_tokens"]
    [choice]=response["choices"]
    obs=json.loads(choice["message"]["content"])["observations"]
    if (type(tokens) is not int or tokens<=0 or response["id"]!=report["provider"]["request_id"]
            or tokens!=report["provider"]["usage"]["prompt_tokens_details"]["audio_tokens"]
            or choice.get("finish_reason")!="stop" or not obs or any(o["verdict"]!="UNKNOWN" for o in obs)
            or not any(_NO_INPUT.search(o.get("reason","")) for o in obs)):
        return None
    from pianchang import asset_voice
    route_digest=digest_json([req["url"],req["body"]["model"]])
    current=_route_digest(load_routes(root/".env"))==route_digest
    return {"input_format":"audio_first","corrected_input_format":getattr(asset_voice,"OPENAI_LISTEN_FORMAT",None) if current else None,
            "route_digest":route_digest,
            "trace_id":trace,"span_id":events[0][1],"request_seq":events[0][0],"response_seq":events[1][0],
            "encoded_bytes":len(encoded),"provider_audio_tokens":tokens,"http_status":200,
            "candidate":pin,"review_ref":report_ref,"observer_claimed_missing_input":True}


@traced("studio.assets.verify_voice_inspection",kind="validation",capture_inputs=False,capture_output=False)
def verify_inspection(service,pid,job_id,pin):
    from tools.workbench_loop_dependency import recheck_dependency
    if not re.fullmatch("[a-f0-9]{32}",str(job_id)):
        raise ValueError("语音原自检任务无效。")
    with service.lock:
        job=next((j.copy() for j in service.state["jobs"] if j["id"]==job_id and j.get("project_id")==pid),None)
    report=(recheck_dependency(service.root,job) or {}).get("voice_review") if job else None
    if (not report or report["corrected_input_format"]!=FORMAT or report["candidate"]!=pin
            or _route_digest(service.model_routes)!=report["route_digest"]):
        raise ValueError("这份语音候选与修正后的听审请求尚未核实。")
