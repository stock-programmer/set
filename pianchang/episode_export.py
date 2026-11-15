"""Normalize and hard-cut selected local shots into an unaccepted preview.

No model APIs are called. Input videos are never modified. FFmpeg's concat
demuxer receives only controlled temporary filenames, not user-supplied text.
"""
from __future__ import annotations

from trace_system import event, span, traced

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from pianchang.media_tools import MediaError, find_ffmpeg, find_ffprobe


PROFILE = {"width": 720, "height": 1280, "fps": 30, "video_codec": "h264",
           "pixel_format": "yuv420p", "audio_codec": "aac", "sample_rate": 48000,
           "audio_channels": 2, "edit": "hard_cuts", "version": 1}


class ExportError(MediaError):
    """Invalid preview inputs or a failed local export; never an acceptance result."""


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _run(arguments, *, timeout=60):
    arguments = [str(arg) for arg in arguments]
    with span("export.process", kind="subprocess", inputs={"argv": arguments, "timeout_s": timeout}) as operation:
        try:
            result = subprocess.run(arguments, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            operation.event("process.timeout", {"timeout_s": exc.timeout,
                            "stdout": exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout,
                            "stderr": exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr})
            raise ExportError("本地视频导出工具运行超时，未接受为成功预览片。") from None
        except OSError as exc:
            raise ExportError(f"无法执行视频导出工具：{exc}") from None
        operation.set_output({"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise ExportError("本地视频导出失败：" + (result.stderr.strip() or f"退出码 {result.returncode}")[-4000:])
        return result


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _probe(ffprobe, path):
    result = _run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path])
    try:
        data = json.loads(result.stdout)
        streams = data["streams"]
        container = data["format"]
        if not isinstance(streams, list) or not all(isinstance(s, dict) for s in streams):
            raise ValueError("invalid streams")
        if not isinstance(container, dict) or "mp4" not in container.get("format_name", "").split(","):
            raise ValueError("not an MP4 container")
        video = next(s for s in streams if s.get("codec_type") == "video")
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        duration = _positive(video.get("duration")) or _positive(container.get("duration"))
        if duration is None or type(video.get("width")) is not int or type(video.get("height")) is not int:
            raise ValueError("invalid video metadata")
        if video["width"] <= 0 or video["height"] <= 0:
            raise ValueError("invalid video dimensions")
    except (ValueError, TypeError, KeyError, StopIteration, AttributeError):
        raise ExportError("素材必须是包含有效视频流和时长的 MP4 文件。") from None
    return {"video": video, "audio": audio, "duration_s": duration,
            "container_duration_s": _positive(container.get("duration")) or duration}


def _inputs(clips):
    if not isinstance(clips, list) or not clips:
        raise ExportError("请选择至少一个镜头，并明确提供镜头顺序。")
    selected = []
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict) or not isinstance(clip.get("shot_id"), str) or not clip["shot_id"].strip():
            raise ExportError(f"第 {index + 1} 个素材缺少有效 shot_id。")
        expected = clip.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise ExportError(f"镜头 {clip['shot_id']} 缺少有效 SHA-256。")
        try:
            path = Path(clip["path"]).expanduser().resolve()
        except (TypeError, KeyError, ValueError):
            raise ExportError(f"镜头 {clip['shot_id']} 缺少有效本地路径。") from None
        if not path.is_file() or path.stat().st_size == 0:
            raise ExportError(f"镜头 {clip['shot_id']} 的本地视频不存在或为空。")
        if _hash(path) != expected.lower():
            raise ExportError(f"镜头 {clip['shot_id']} 的视频哈希与所选素材不一致。")
        selected.append({"order": index + 1, "shot_id": clip["shot_id"],
                         "path": str(path), "sha256": expected.lower()})
    return selected


def _cached(directory, input_hash):
    path = directory / "export.json"
    preview = directory / "preview.mp4"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != "episode-preview/1"
                or manifest.get("input_sha256") != input_hash or manifest.get("status") != "PREVIEW_ONLY"
                or manifest.get("accepted") is not False or manifest.get("profile") != PROFILE
                or manifest.get("preview_path") != str(preview) or not preview.is_file()
                or manifest.get("preview_sha256") != _hash(preview)):
            raise ValueError("incompatible or damaged preview")
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise ExportError("导出目录非空且没有相同输入的完整预览缓存；请使用新目录，已有文件不会覆盖。") from None


def _write_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".export-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        from pianchang.atomic_files import replace_with_retry
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _transcode(ffmpeg, clip, info, target):
    # Quantize each shot to the common 30 fps timeline, at most half a frame off.
    frame_count = max(1, int(math.floor(info["duration_s"] * PROFILE["fps"] + 0.5)))
    duration = frame_count / PROFILE["fps"]
    args = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-err_detect", "explode",
            "-i", clip["path"]]
    audio_policy = "first_source_audio_stream" if info["audio"] else "generated_silence"
    if not info["audio"]:
        args.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
    video_filter = ("setpts=PTS-STARTPTS,fps=30:start_time=0,"
                    "scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1,"
                    "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
                    f"tpad=stop_mode=clone:stop_duration={duration:.9f}")
    args.extend(["-map", "0:v:0", "-map", "0:a:0" if info["audio"] else "1:a:0",
                 "-vf", video_filter, "-af", "aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo,apad",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-bf", "0",
                 "-pix_fmt", "yuv420p", "-frames:v", str(frame_count),
                 # PCM intermediates avoid per-shot AAC encoder-delay overlap.
                 # AAC is encoded once after concatenating the common timeline.
                 "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                 "-t", f"{duration:.9f}", "-video_track_timescale", "15360", "-movflags", "+faststart",
                 "-n", target])
    _run(args, timeout=max(60, min(1800, duration * 15 + 30)))
    return {**clip, "duration_s": info["duration_s"], "preview_duration_s": duration,
            "preview_frame_count": frame_count, "source_width": info["video"]["width"],
            "source_height": info["video"]["height"], "source_audio_stream_count": len(info["audio"]),
            "audio_policy": audio_policy}


def _verify(ffmpeg, ffprobe, preview, expected_duration, clip_count):
    metadata = _probe(ffprobe, preview)
    video, audio = metadata["video"], metadata["audio"]
    try:
        fps = float(Fraction(video.get("avg_frame_rate", "0/1")))
    except (ValueError, TypeError, ZeroDivisionError):
        fps = 0
    if (video["width"] != 720 or video["height"] != 1280 or video.get("codec_name") != "h264"
            or video.get("pix_fmt") != "yuv420p" or not math.isclose(fps, 30, abs_tol=0.01)
            or len(audio) != 1 or audio[0].get("codec_name") != "aac"
            or str(audio[0].get("sample_rate")) != "48000" or audio[0].get("channels") != 2):
        raise ExportError("预览片未达到 720×1280、30fps、H.264/yuv420p、AAC双声道48kHz 的导出规格。")
    # AAC packet boundaries can extend the container by a few hundredths of a second.
    tolerance = 0.1 + clip_count * 0.05
    actual = metadata["container_duration_s"]
    if abs(actual - expected_duration) > tolerance:
        raise ExportError(f"预览片时长 {actual:.3f}s 与镜头总时长 {expected_duration:.3f}s 不符。")
    decoded = _run([ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror",
                    "-err_detect", "explode", "-i", preview, "-map", "0:v", "-map", "0:a",
                    "-f", "null", "-"], timeout=max(60, min(1800, actual * 8 + 30)))
    if decoded.stderr.strip():
        raise ExportError("预览片完整解码存在错误：" + decoded.stderr.strip()[-4000:])
    return ({"duration_s": actual, "video_duration_s": metadata["duration_s"],
             "width": 720, "height": 1280, "fps": fps, "video_codec": "h264",
             "pixel_format": "yuv420p", "audio_codec": "aac", "audio_sample_rate": 48000,
             "audio_channels": 2},
            {"format": "PASS", "decode": "PASS", "duration_error_s": actual - expected_duration,
             "duration_tolerance_s": tolerance})


@traced("media.export_preview", kind="tool")
def export_preview(clips, output_dir):
    """Export selected clips in list order; return and persist an unaccepted preview.

    This is local hard-cut assembly only. Passing encoding/decoding checks does
    not accept the shots, their continuity, or the episode's visual/audio quality.
    """
    selected = _inputs(clips)
    input_hash = _json_hash({"clips": selected, "profile": PROFILE})
    directory = Path(output_dir).expanduser().resolve()
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        cached = _cached(directory, input_hash)
        event("cache.hit", {"manifest_path": str(directory / "export.json"), "input_sha256": input_hash})
        return cached
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    source_info = [_probe(ffprobe, clip["path"]) for clip in selected]
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".export.lock"
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except FileExistsError:
        raise ExportError("该目录正在导出预览片，不能并发写入。") from None
    try:
        with tempfile.TemporaryDirectory(prefix=".preview-", dir=directory) as temporary:
            staging = Path(temporary).resolve()
            if not staging.is_relative_to(directory):
                raise ExportError("临时导出路径超出了指定输出目录。")
            records = []
            concat_lines = ["ffconcat version 1.0"]
            for index, (clip, info) in enumerate(zip(selected, source_info)):
                filename = f"clip-{index:05}.mov"
                record = _transcode(ffmpeg, clip, info, staging / filename)
                records.append(record)
                # Only generated portable filenames enter the concat file.
                concat_lines.extend([f"file '{filename}'", f"duration {record['preview_duration_s']:.9f}"])
            concat_file = staging / "clips.ffconcat"
            concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
            temporary_preview = staging / "preview.mp4"
            expected = sum(r["preview_duration_s"] for r in records)
            _run([ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-f", "concat",
                  "-safe", "0", "-i", concat_file, "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                  "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                  "-af", "aresample=48000:async=1:first_pts=0", "-t", f"{expected:.9f}",
                  "-movflags", "+faststart", "-n", temporary_preview],
                 timeout=max(60, min(1800, expected * 4 + 30)))
            metadata, validation = _verify(ffmpeg, ffprobe, temporary_preview, expected, len(records))
            for clip in selected:
                if _hash(clip["path"]) != clip["sha256"]:
                    raise ExportError("导出过程中源素材发生变化；未发布预览片。")
            preview_hash = _hash(temporary_preview)
            preview_bytes = temporary_preview.stat().st_size
            preview = directory / "preview.mp4"
            from pianchang.atomic_files import replace_with_retry
            replace_with_retry(temporary_preview, preview)
            manifest = {
                "schema_version": "episode-preview/1", "status": "PREVIEW_ONLY", "accepted": False,
                "scope": "hard-cut preview only; shots and episode have not been accepted",
                "input_sha256": input_hash, "profile": dict(PROFILE), "clips": records,
                "source_total_duration_s": sum(r["duration_s"] for r in records),
                "expected_duration_s": expected, "total_duration_s": metadata["duration_s"],
                "preview_path": str(preview), "preview_sha256": preview_hash, "preview_bytes": preview_bytes,
                "metadata": metadata, "validation": validation,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "manifest_path": str(directory / "export.json"),
            }
            _write_json(directory / "export.json", manifest)
            event("artifact.preview_written", {"manifest_path": str(directory / "export.json"),
                  "preview_path": str(preview), "preview_sha256": preview_hash,
                  "preview_bytes": preview_bytes, "clips": records}, kind="artifact")
            return manifest
    finally:
        lock.unlink(missing_ok=True)
