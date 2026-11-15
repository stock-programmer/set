"""Inspect an actual hard cut in a published collaboration preview, with offline media evidence."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path

from pianchang.acceptance import file_sha256
from pianchang.bailian_observer import BailianObserver, load_observer_config
from pianchang.bailian_text import TextError
from pianchang.collab_store import CollabStore
from pianchang.collaboration_workflow import HOME_DIR, Collaboration, causal, open_ref, ref
from pianchang.media_tools import MediaError, _probe, _run, find_ffmpeg, find_ffprobe
from pianchang.story_spec import digest_json, validate_compilation
from pianchang.story_agent import _write_json
from trace_system import TraceError, current_context, event, get_tracer, trace, traced


def _check(path, expected, actual):
    result = {"path": path, "expected": expected, "actual": actual,
              "status": "PASS" if expected == actual else "FAIL"}
    event("cut.validation", result, kind="validation")
    if result["status"] != "PASS":
        raise ValueError(f"切点复核失败：{path}")
    return result


def _ffmpeg(*args, level="error"):
    result = _run([find_ffmpeg(), "-nostdin", "-hide_banner", "-v", level, "-n", *args], timeout=120)
    if result.returncode:
        raise MediaError(result.stderr[-2000:])
    return result


def _asset(path):
    return {"path": str(path.resolve()), "sha256": file_sha256(path), "bytes": path.stat().st_size}


def _frame_times(preview):
    data = _probe(find_ffprobe(), ["-select_streams", "v:0", "-show_frames", "-show_entries",
                                  "frame=best_effort_timestamp_time", str(preview)])
    return [float(frame["best_effort_timestamp_time"]) for frame in data["frames"]]


def _audio_levels(preview, start, end):
    result = _ffmpeg("-i", preview, "-map", "0:a:0", "-vn", "-af",
                     f"atrim=start={start:.9f}:end={end:.9f},volumedetect",
                     "-f", "null", "-", level="info")
    values = {}
    for metric in ("mean_volume", "max_volume"):
        match = re.search(r"\b" + metric + r":\s*(-?(?:[\d.]+|inf)) dB", result.stderr)
        if not match:
            raise MediaError("指定音频区间缺少电平测量：" + metric)
        values[metric + "_dbfs"] = float(match[1]) if match[1] != "-inf" else "-inf"
    return {"from_s": start, "to_s": end, **values}


def _criteria(production, report):
    selected = {s["id"]:s for s in production["project"]["shots"]}
    left, right = [selected[s["id"]] for s in report["shots"]]
    cut_context = {"left_scene":left["scene_id"],"right_scene":right["scene_id"],
                   "left_shot":left["description"],"right_shot":right["description"]}
    measures = {
        "costume":"两张可比较画面中的人物服装、发型或手持物外观能否衔接？分别描述每一帧可见细节；不判断身份。",
        "space":"两帧可辨认的房间、镜子、家具或门的位置与给定机位和空间要求相容吗？反打构图可以变化。",
        "prop_state":"仅检查两帧确实可见的手和道具（如捧花、头纱、手包）的静态位置与状态是否与剪切处的剧本状态相容。",
    }
    return [{"criterion_id":key,"description":desc + " 要给跨帧结论必须看清两侧；一侧看不清应报UNKNOWN。",
             "context":cut_context,"expected_state":report["state_check"]["expected"] if key=="prop_state" else None}
            for key,desc in measures.items()]


@traced("collaboration.cut.visual_observations", kind="validation", capture_inputs=False)
def observe_cut(client, production, report, cache_dir):
    frames = [{"id":fid,"time_s":frame["preview_time_s"],"sha256":frame["sha256"],"path":frame["path"]}
              for fid,frame in zip(("left_last","right_first"),report["frames"])]
    criteria = _criteria(production,report)
    key = {"schema":"cut-static-pair/1","preview":report["preview_sha256"],"model":client.config.model,
           "endpoint":client.config.base_url,"frames":[{k:f[k] for k in ("id","time_s","sha256")} for f in frames],
           "criteria":criteria}
    if getattr(client.config, "route_account", ""):
        key["account"] = client.config.route_account
    cache_dir.mkdir(parents=True,exist_ok=True)
    path = cache_dir / (digest_json(key) + ".json")
    try:
        with path.open("x",encoding="utf8") as stream:
            json.dump({"status":"submitted_or_unknown","request":key,"trace_id":current_context()["trace_id"]},
                      stream,ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        # A process may still be saving the claim. A missing result never authorizes another POST.
        try:
            saved = json.loads(path.read_text(encoding="utf8"))
        except (OSError,ValueError):
            saved = {}
        event("cut.observation_cache", {"path":path,"status":saved.get("status","unknown"),"new_request":False})
        if saved.get("status") == "completed" and saved.get("request") == key:
            return {**saved["observation"],"cached":True}
        return {"status":"UNKNOWN","reason":"相同观察已有调用凭据，未取得确认结果。","cached":True,
                "previous_trace_id":saved.get("trace_id")}
    try:
        event("cut.observation_inputs",key,kind="observation")
        response = client.complete_observations(frames,criteria)
        rows = response["data"]["observations"]
        expected = sorted(c["criterion_id"] for c in criteria)
        _check("visual.observed_criteria",expected,sorted(row["criterion_id"] for row in rows))
        frame_index = {f["id"]:f for f in frames}
        labels = []
        for row in rows:
            ids = row["frame_ids"]
            _check("visual.evidence_ids",True,len(ids)==len(set(ids)) and set(ids)<=frame_index.keys())
            verdict = row["verdict"] if set(ids) == frame_index.keys() else "UNKNOWN"
            labels.append({**row,"verdict":verdict,"model_verdict":row["verdict"],
                           "preview_times_s":[frame_index[f]["time_s"] for f in ids]})
        observation = {"status":"OBSERVED","rows":labels,"model":response.get("model"),
                       "provider_request_id":response.get("request_id"),"scope":"two_static_boundary_frames"}
        _write_json(path,{"status":"completed","request":key,"observation":observation,"trace_id":current_context()["trace_id"]})
        return observation
    except TraceError:
        raise
    except (TextError,ValueError,OSError,KeyError,RuntimeError) as exc:
        reason = get_tracer().sanitize(str(exc))
        event("cut.observation_unavailable",{"reason":reason},kind="error")
        return {"status":"UNKNOWN","reason":reason,"model":client.config.model,"scope":"two_static_boundary_frames"}


@traced("collaboration.cut.inspect", kind="validation", capture_inputs=False)
def inspect_cut(production, left, right, directory):
    """Read only verified inputs. Return measurements; semantic continuity remains a review decision."""
    preview_ref = production["preview"]
    preview = Path(preview_ref["preview_path"])
    manifest = json.loads(Path(preview_ref["manifest_path"]).read_text(encoding="utf-8-sig"))
    _check("preview.export_manifest", preview_ref["export_manifest_sha256"], digest_json(manifest))
    _check("preview.file", preview_ref["preview_sha256"], file_sha256(preview))
    _check("preview.export_file", preview_ref["preview_sha256"], manifest["preview_sha256"])
    _check("preview.script", production["script_ref"], preview_ref["script_ref"])
    clips = manifest["clips"]
    order = [item["shot_id"] for item in clips]
    _check("timeline.order", [s["id"] for s in production["project"]["shots"]], order)
    _check("timeline.adjacent", True, left in order and right in order and order.index(left) + 1 == order.index(right))
    for sid, item in zip(order, clips):
        shot = production["shots"][sid]
        _check(f"timeline.{sid}.script", production["script_ref"], shot["script_ref"])
        _check(f"timeline.{sid}.input", shot["media_sha256"], item["sha256"])
        _check(f"timeline.{sid}.media", item["sha256"], file_sha256(Path(item["path"])))
    left_index = order.index(left)
    left_clip, right_clip = clips[left_index:left_index + 2]
    fps = int(manifest["profile"]["fps"])
    frame_counts = [c["preview_frame_count"] for c in clips]
    _check("timeline.frame_counts", True, fps > 0 and all(type(count) is int and count > 0 for count in frame_counts))
    cut_frame = sum(frame_counts[:left_index + 1])
    times = _frame_times(preview)
    _check("preview.decoded_frame_count", sum(frame_counts), len(times))
    _check("preview.cut_timestamp_within_one_millisecond", True,
           all(abs(times[i] - i / fps) < .001 for i in (cut_frame - 1, cut_frame)))
    first = cut_frame - min(fps, frame_counts[left_index])
    stop = cut_frame + min(fps, frame_counts[left_index + 1])
    start_s = times[first]
    cut_s = times[cut_frame]
    end_s = times[stop] if stop < len(times) else stop / fps
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    evidence_frames = []
    # Use actual decoded frame indices, including the very last left frame and very first right frame.
    for sid, index, name in [(left, cut_frame - 1, "left-last"), (right, cut_frame, "right-first")]:
        path = directory / (name + ".png")
        _ffmpeg("-i", preview, "-an", "-vf", f"select='eq(n,{index})'", "-frames:v", "1", path)
        evidence_frames.append({"shot_id":sid, "preview_frame_index":index, "preview_time_s":times[index], **_asset(path)})
    window = directory / "cut-window.mp4"
    _ffmpeg("-i", preview, "-map", "0:v:0", "-map", "0:a:0",
            "-vf", f"trim=start_frame={first}:end_frame={stop},setpts=PTS-STARTPTS",
            "-af", f"atrim=start={start_s:.9f}:end={end_s:.9f},asetpts=PTS-STARTPTS",
            "-c:v","libx264","-preset","veryfast","-crf","18","-c:a","aac","-movflags","+faststart",window)
    _check("evidence.window_frames", stop - first, len(_frame_times(window)))
    probe = _probe(find_ffprobe(), ["-select_streams","a:0","-show_streams","-show_entries",
                                    "stream=codec_name,sample_rate,channels,duration",str(window)])
    _check("evidence.audio_stream",1,len(probe.get("streams",[])))
    audio = probe["streams"][0]
    _check("evidence.audio_duration",True, math.isclose(float(audio["duration"]),end_s - start_s,abs_tol=.06))
    _ffmpeg("-xerror","-i",window,"-map","0:v:0","-map","0:a:0","-f","null","-")
    _check("preview.hash_after_extraction",preview_ref["preview_sha256"],file_sha256(preview))
    states = {r["shot_id"]:r for r in validate_compilation(production["project"])["state_trace"]}
    after, before = states[left]["after"], states[right]["before"]
    state_check = {"path":f"{left}.after/{right}.before","expected":after,"actual":before,
                   "status":"PASS" if after == before else "FAIL"}
    event("cut.state_alignment",state_check,kind="validation")
    report = {"schema_version":"collaboration-cut-review/1","script_ref":production["script_ref"],
              "shots":[{"id":sid,"sha256":production["shots"][sid]["media_sha256"],"operation":production["shots"][sid]["operation"]}
                       for sid in (left,right)],"preview_sha256":preview_ref["preview_sha256"],
              "export_manifest_sha256":preview_ref["export_manifest_sha256"],"state_check":state_check,
              "cut":{"preview_time_s":cut_s,"frame_index":cut_frame,"fps":fps},
              "frames":evidence_frames,"window":{"from_s":start_s,"to_s":end_s,"cut_in_window_s":cut_s-start_s,**_asset(window)},
              "audio":{"stream":audio,"left":{**_audio_levels(preview,start_s,cut_s),"policy":left_clip["audio_policy"]},
                       "right":{**_audio_levels(preview,cut_s,end_s),"policy":right_clip["audio_policy"]}},
              "technical_status":"PASS","audiovisual_continuity":"UNKNOWN",
              "review_scope":"状态、相邻关系、实际帧、解码和分段音量已核对；人物动作是否连贯与听感需结合此区间审看。"}
    return report


def review_revision(directory, run_id, revision, left, right, *, vision=False):
    if not re.fullmatch("[0-9a-f]{32}", run_id) or not re.fullmatch("r[1-9][0-9]*",revision):
        raise ValueError("协作 ID 或版本无效")
    directory = Path(directory).resolve()
    if not (directory / "assets.sqlite3").is_file():
        raise ValueError("协作库不存在")
    store = CollabStore(directory / "assets.sqlite3")
    messages = [m for m in store.messages(run_id,message_type="production.result")
                if m["payload"].get("production") and m["payload"].get("script",{}).get("revision_id") == revision]
    if len(messages) != 1:
        raise ValueError("需唯一匹配到已交付的目标版本")
    message = messages[0]
    with trace("collaboration.review_cut",kind="task",links=[causal(message,"reviews_production")],
               inputs={"message_id":message["message_id"],"production":message["payload"]["production"],
                       "revision":revision,"left":left,"right":right,"vision":vision},attributes={"correlation_id":run_id}) as root:
        asset = open_ref(store,message["payload"]["production"])
        _check("asset.story", "story-" + run_id, asset["story_id"])
        _check("asset.kind","production",asset["kind"])
        _check("asset.script_binding",message["payload"]["script"],asset["content"]["script_ref"])
        open_ref(store,asset["content"]["script_ref"])
        output = directory / "runs" / run_id / "cut-reviews" / root.trace_id
        report = inspect_cut(asset["content"],left,right,output)
        if vision:
            try:
                client = BailianObserver(load_observer_config())
                report["visual_observations"] = observe_cut(client,asset["content"],report,output.parent / "observation-cache")
            except TextError as exc:
                report["visual_observations"] = {"status":"UNKNOWN","reason":get_tracer().sanitize(str(exc))}
        report.update(trace_id=root.trace_id,production=ref(asset),production_trace_id=message["trace_id"])
        path = output / "cut-review.json"
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf8")
        root.set_output({"report":_asset(path),**report})
    Collaboration(directory,run_id).stage(f"切点实片复查 {left}→{right}",report["trace_id"],"completed",revision,
        f"切点 {report['cut']['preview_time_s']:.3f} 秒；已提取前后画面、音轨和组合片段，完整视听衔接待审看")
    return report


@traced("cli.collaboration.cut_review",kind="cli")
def main(argv=None):
    parser=argparse.ArgumentParser(description="从指定版本成片核查相邻镜头的剪切位置与视听证据")
    parser.add_argument("--run-id",required=True)
    parser.add_argument("--revision",required=True)
    parser.add_argument("--left",required=True)
    parser.add_argument("--right",required=True)
    parser.add_argument("--directory",type=Path,default=HOME_DIR)
    parser.add_argument("--vision",action="store_true",help="用百炼视觉模型观察切点两侧实际帧，一次调用")
    args=parser.parse_args(argv)
    report=review_revision(args.directory,args.run_id,args.revision,args.left,args.right,vision=args.vision)
    print(json.dumps({k:report[k] for k in ("trace_id","cut","window","technical_status","audiovisual_continuity")},
                     ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
