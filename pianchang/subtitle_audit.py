"""Independent, cached subtitle transcription after an unusable whole-video observation.

Frames prove the visible text at their timestamps; they never prove continuous
acting, identity, audio, or the absence of text elsewhere in a video.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib import request

from pianchang.bailian_observer import BailianObserver, _frame_part
from pianchang.bailian_text import TextError
from pianchang.story_spec import digest_json
from trace_system import TraceError, register_secret, traced

VERSION="subtitle-frame-transcription/1"
PROMPT="""你只抄录图片中实际可见的叙事字幕，不进行验收、比较或推理。不抄建筑牌匾和水印。
输入不提供目标字幕，禁止猜测或补写；多帧为同一字幕时只抄一次，保留实际标点。
只输出JSON：{"observed_text":"实际文字；没有或看不清则空字符串","readable":true}。
看不清或没有字幕时readable为false。不要输出reason、解释、分析或其他文字。
"""


class SubtitleFrameObserver(BailianObserver):
    @traced("content.model.transcribe_subtitle_frames",kind="model",capture_inputs=False,capture_output=False)
    def transcribe(self,frames):
        register_secret(self.config.api_key)
        if not self.config.api_key.strip():raise TextError("没有可用的文字读取模型凭证，未发送请求。")
        parts=[{"type":"text","text":"请按指定JSON结构抄录以下实际图片中的叙事字幕；不提供要比对的目标文字。"}]
        for frame in frames:parts.extend(_frame_part(frame)[1])
        payload={"model":self.config.model,"messages":[{"role":"system","content":PROMPT},{"role":"user","content":parts}],
                 "stream":False,"max_tokens":320,"temperature":0,"enable_thinking":False,"response_format":{"type":"json_object"}}
        req=request.Request(self.config.base_url+"/chat/completions",data=json.dumps(payload,ensure_ascii=False).encode("utf8"),
                            headers={"Authorization":"Bearer "+self.config.api_key,"Content-Type":"application/json"},method="POST")
        body,header=self._post(req)
        if body.get("error") or body.get("code"):raise self._provider_failure(body,request_id=header)
        choices=body.get("choices") or []
        if len(choices)!=1 or choices[0].get("finish_reason")!="stop":raise TextError("字幕图片读取未完整结束，不能接受截断文字。")
        message=choices[0].get("message") or {}
        if message.get("refusal"):raise TextError("字幕图片读取被模型拒绝。")
        return {"data":json.loads(message["content"]),"model":body.get("model") or self.config.model,
                "request_id":body.get("id") or header,"usage":body.get("usage"),"input_modality":"images"}


@traced("content.prepare_subtitle_frames",kind="tool",capture_inputs=False)
def prepare_subtitle_frames(clip,video,directory):
    from pianchang.content_review import _asset,_check,_ffmpeg,_read,_write
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    source=_asset(clip["media_path"])
    _check("subtitle.frames.current_source",clip["media_sha256"],source["sha256"])
    duration=video["duration_s"]
    _check("subtitle.frames.duration",True,duration>.05)
    times=sorted({round(max(0,duration-.25),3),round(max(0,duration-.10),3)})
    binding={"version":VERSION,"source":source,"times_s":times,"scale":"original_pixel_dimensions"}
    for path in [directory/"frames.json",*sorted(directory.glob("recovery-*/frames.json"))]:
        if path.is_file():
            saved=_read(path);_check("subtitle.frames.cached_binding",binding,saved["binding"])
            for frame in saved["frames"]:_check("subtitle.frames.cached_hash",frame["sha256"],_asset(frame["path"])["sha256"])
            return {**saved,"manifest":_asset(path)}
    if any(directory.glob("F*.jpg")):
        for attempt in range(1,4):
            candidate=directory/("recovery-"+str(attempt))
            if not any(candidate.glob("F*.jpg")):
                directory=candidate;directory.mkdir(parents=True,exist_ok=True);break
        else:raise ValueError("字幕证据抽帧连续中断，三个部分产物均已保留；未发送新的模型请求。")
    path=directory/"frames.json"
    frames=[]
    for index,timestamp in enumerate(times,1):
        target=directory/("F%03d.jpg"%index)
        if target.exists():raise ValueError("字幕抽帧曾中断且缺少完整来源清单，保留未确认文件："+str(target))
        _ffmpeg(["-noautorotate","-ss",str(timestamp),"-i",source["path"],"-map","0:v:0","-frames:v","1","-q:v","2",str(target)])
        frames.append({**_asset(target),"id":"F%03d"%index,"time_s":timestamp})
    saved={"binding":binding,"frames":frames};_write(path,saved)
    return {**saved,"manifest":_asset(path)}


@traced("content.audit_subtitle_frames",kind="validation",capture_inputs=False,capture_output=False)
def audit_subtitle_frames(config,clip,video,criterion,context,directory,previous,*,client=None):
    from pianchang.content_review import _asset,_check,_read,_write
    from pianchang.video_collab_agent import _one_model_call
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    eligible=any(row["criterion_id"]==criterion["criterion_id"] and row["status"]=="UNKNOWN" and
                 row.get("execution_status") in {"evidence_invalid","execution_error"} for row in previous["rows"])
    if not eligible:return previous
    try:frames=prepare_subtitle_frames(clip,video,directory/"input")
    except TraceError:raise
    except (ValueError,KeyError,TypeError,OSError) as exc:
        reason="原始分辨率字幕证据准备失败："+str(exc)+"；原检查和部分产物保留，未发送文字读取模型请求。"
        row={"criterion_id":criterion["criterion_id"],"status":"UNKNOWN","execution_status":"execution_error",
             "reason":reason,"method":"original_resolution_subtitle_frames","frame_audit_version":VERSION,
             "evidence":[],"limitations":[reason],"expected_text":criterion["expected_text"]}
        receipt={"version":VERSION,"source_type":"subtitle_frame_transcription","input_modality":"images","execution_status":"execution_error",
                 "source_video_context_only":True,"video":video,"references":[],"frames":[],"frame_manifest":None,
                 "context":copy.deepcopy(context),"criteria":[criterion],"rows":[row],"response":None,
                 "fallback_from":{"path":previous["receipt_path"],"sha256":previous["receipt_sha256"]}}
        target=directory/("preparation-error-"+digest_json(receipt)[:12]+".json")
        if not target.is_file():_write(target,receipt)
        return {**receipt,"receipt_path":str(target),"receipt_sha256":digest_json(receipt)}
    # One last-frame input makes the transcription's actual timestamp unambiguous,
    # and avoids fragile model-generated citation metadata for this static check.
    model_frames=frames["frames"][-1:]
    fingerprint={"version":VERSION,"model":config.model,"endpoint":config.base_url,"prompt_sha256":digest_json(PROMPT),
                 "source_video":video,"frames":model_frames,"frame_manifest":frames["manifest"],"references":[]}
    folder=directory/digest_json(fingerprint)[:12];folder.mkdir(parents=True,exist_ok=True)
    path=folder/"call.json"
    receipt_path=folder/("receipt-"+digest_json({"criterion":criterion,"context":context,"previous":previous["receipt_sha256"]})[:12]+".json")
    if receipt_path.is_file():
        saved=_read(receipt_path);_check("subtitle.frame_audit.cached_request",digest_json(fingerprint),saved["request_sha256"])
        _check("subtitle.frame_audit.cached_requirement",[criterion],saved["criteria"])
        return {**saved,"receipt_path":str(receipt_path),"receipt_sha256":digest_json(saved)}
    response=None;error=None
    try:
        observer=client or SubtitleFrameObserver(config)
        response=_one_model_call(path,fingerprint,lambda:observer.transcribe(model_frames))
        _check("subtitle.frame_audit.response_object",True,isinstance(response,dict) and isinstance(response.get("data"),dict))
        data=response["data"];text=data.get("observed_text")
        _check("subtitle.frame_audit.text_structure",True,isinstance(text,str) and len(text)<=160 and type(data.get("readable")) is bool)
        readable=data["readable"] and bool(text)
        matches=readable and text==criterion["expected_text"]
        rendering=context.get("subtitle_rendering")
        wrong_rendering=bool(rendering and rendering["text"]!=criterion["expected_text"])
        status="FAIL" if wrong_rendering else "PASS" if matches else "UNKNOWN"
        reason=("原始分辨率字幕帧实际读取到“"+text+"”，与固定字幕逐字一致。" if matches else
                "已改用原始分辨率字幕帧读取，但识别到的文字“"+(text or "空")+"”尚不能证明该字幕满足要求；这些帧不能证明其他时段没有字幕。")
        if wrong_rendering:reason="实际烧录输入与固定字幕不符，不能用图片读取的文字覆盖该独立失败证据。"
        evidence=[{**_asset(f["path"]),"type":"subtitle_frame","frame_id":f["id"],"time_s":f["time_s"],
                   "observation":"实际图片字幕读取："+text} for f in model_frames]
        if wrong_rendering:evidence.append({**_asset(rendering["receipt"]["path"]),"type":"render_receipt","observation":"实际烧录文字："+rendering["text"]})
    except TraceError:raise
    except (ValueError,KeyError,TypeError,OSError,TextError) as exc:
        error=str(exc);status="UNKNOWN";text=None;evidence=[]
        reason="已尝试独立的原始分辨率图片文字读取，但未取得有效结果："+error+"；保留原检查及调用记录，未冒认通过。"
    row={"criterion_id":criterion["criterion_id"],"status":status,"reason":reason,"method":"original_resolution_subtitle_frames",
         "execution_status":"execution_error" if error else "inconclusive" if status=="UNKNOWN" else "executed",
         "observed_text":text,"expected_text":criterion["expected_text"],"evidence":evidence,"limitations":[reason] if status=="UNKNOWN" else [],
         "frame_audit_version":VERSION,"decision_source":"deterministic_comparison_of_received_transcription",
         "provider_request_id":(response or {}).get("request_id"),"model":(response or {}).get("model"),
         "human_confirmation":"pending","sampling_scope":"visible_subtitle_at_cited_original_resolution_frames_only"}
    if context.get("subtitle_rendering"):row["rendered_text"]=context["subtitle_rendering"]["text"]
    receipt={"version":VERSION,"source_type":"subtitle_frame_transcription","input_modality":"images","execution_status":"execution_error" if error else "executed",
             "source_video_context_only":True,"video":video,"references":[],"frames":model_frames,"frame_manifest":frames["manifest"],
             "context":copy.deepcopy(context),"criteria":[criterion],"rows":[row],"response":response,
             "call_path":str(path) if response else None,"call_sha256":digest_json(_read(path)) if response else None,
             "request_sha256":digest_json(fingerprint),"fallback_from":{"path":previous["receipt_path"],"sha256":previous["receipt_sha256"]}}
    _write(receipt_path,receipt)
    return {**receipt,"receipt_path":str(receipt_path),"receipt_sha256":digest_json(receipt)}
