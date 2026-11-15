"""Local media metadata, full decoding and frames with measured source PTS.

These checks do not judge the visual content, acting, continuity, or dialogue.
FFmpeg references: https://ffmpeg.org/ffmpeg.html and
https://ffmpeg.org/ffprobe.html . No shell commands or model APIs are used.
"""
from __future__ import annotations

from trace_system import event, span, traced

import bisect
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COVERAGE_NOTE = "sparse frames do not prove complete content coverage"


class MediaError(Exception):
    """The requested local inspection cannot be performed or safely reused."""


def _find_tool(name, variable):
    override = os.environ.get(variable)
    if override is not None:
        path = Path(override).expanduser().resolve()
        if not override.strip() or not path.is_file():
            raise MediaError(f"{variable} 没有指向可执行文件。")
        return path
    suffix = ".exe" if os.name == "nt" else ""
    local = ROOT / ".tools" / "ffmpeg" / "bin" / (name + suffix)
    if local.is_file():
        return local.resolve()
    found = shutil.which(name)
    if found:
        return Path(found).resolve()
    raise MediaError(f"未找到 {name}；请配置 {variable} 或放入项目 .tools/ffmpeg/bin。未伪造媒体检查。")


def find_ffmpeg():
    return _find_tool("ffmpeg", "FFMPEG_PATH")


def find_ffprobe():
    return _find_tool("ffprobe", "FFPROBE_PATH")


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(arguments, *, timeout):
    arguments = [str(arg) for arg in arguments]
    with span("media.process", kind="subprocess", inputs={"argv": arguments, "timeout_s": timeout}) as operation:
        try:
            result = subprocess.run(arguments, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            operation.event("process.timeout", {"timeout_s": exc.timeout,
                            "stdout": exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout,
                            "stderr": exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr})
            raise
        except OSError as exc:
            raise MediaError(f"无法执行媒体工具：{type(exc).__name__}: {exc}") from None
        operation.set_output({"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            operation.set_status("ERROR", "媒体工具返回非零退出码")
        return result


def _error(result):
    return (result.stderr.strip() or f"工具退出码 {result.returncode}")[-4000:]


def _probe(tool, arguments, *, timeout=60):
    try:
        result = _run([tool, "-v", "error", *arguments, "-of", "json"], timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError("ffprobe 检查超时，未接受为有效媒体信息。") from None
    if result.returncode != 0:
        raise MediaError("ffprobe 无法读取媒体：" + _error(result))
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise MediaError("ffprobe 未返回有效 JSON。") from None
    if not isinstance(data, dict):
        raise MediaError("ffprobe 返回了非对象 JSON。")
    return data


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _metadata(probe):
    streams = probe.get("streams")
    if not isinstance(streams, list) or not all(isinstance(s, dict) for s in streams):
        raise MediaError("ffprobe 未返回有效的流信息。")
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("输入没有视频流。")
    duration = _number(video.get("duration"))
    if duration is None or duration <= 0:
        container = probe.get("format")
        duration = _number(container.get("duration")) if isinstance(container, dict) else None
    if duration is None or duration <= 0:
        raise MediaError("无法确定有限正数的视频时长。")
    width, height = video.get("width"), video.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise MediaError("视频分辨率无效。")
    return {"duration_s": duration, "width": width, "height": height,
            "video_codec": video.get("codec_name"),
            "audio_stream_count": sum(s.get("codec_type") == "audio" for s in streams)}


def _frame_index(probe):
    frames = probe.get("frames")
    if not isinstance(frames, list) or not frames:
        raise MediaError("ffprobe 没有返回可抽取的视频帧。")
    indexed = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise MediaError("ffprobe 帧结构无效。")
        pts = _number(frame.get("best_effort_timestamp_time"))
        if pts is None:
            pts = _number(frame.get("pts_time"))
        if pts is None:
            raise MediaError("视频帧缺少可靠的 PTS，不能把请求时间冒充实际帧时间。")
        if indexed and pts < indexed[-1]["pts_time_s"]:
            raise MediaError("视频帧 PTS 非单调，无法建立可靠的时间索引。")
        indexed.append({"source_frame_index": index, "pts_time_s": pts})
    origin = indexed[0]["pts_time_s"]
    for frame in indexed:
        frame["time_s"] = round(frame["pts_time_s"] - origin, 9)
    return indexed


def _sample(indexed, interval_s, max_frames):
    last = indexed[-1]["time_s"]
    quotient = last / interval_s
    grid_count = math.floor(quotient) + 1 if math.isfinite(quotient) else None
    grid_end = (grid_count - 1) * interval_s if grid_count is not None else None
    needs_last = grid_end is None or not math.isclose(grid_end, last, abs_tol=1e-9)
    candidate_count = grid_count + int(needs_last) if grid_count is not None else None
    capped = candidate_count is None or candidate_count > max_frames
    count = max_frames if grid_count is None else min(grid_count, max_frames)
    requested = [round(i * interval_s, 9) for i in range(count)]
    if needs_last:
        if len(requested) < max_frames:
            requested.append(last)
        elif max_frames > 1:
            requested[-1] = last
    elif capped and max_frames > 1:
        requested[-1] = last
    times = [f["time_s"] for f in indexed]
    selected, seen = [], set()
    for target in requested:
        right = bisect.bisect_left(times, target)
        candidates = [i for i in (right - 1, right) if 0 <= i < len(times)]
        index = min(candidates, key=lambda i: (abs(times[i] - target), i))
        if index not in seen:
            selected.append({**indexed[index], "requested_time_s": target})
            seen.add(index)
    return selected, {"interval_s": interval_s, "max_frames": max_frames,
                      "requested_times_s": requested, "candidate_sample_count": candidate_count,
                      "capped": capped, "source_frame_count": len(indexed),
                      "source_start_pts_time_s": indexed[0]["pts_time_s"],
                      "extracted_times_s": [], "extraction_errors": [],
                      "coverage_note": COVERAGE_NOTE,
                      "timestamp_basis": "actual ffprobe video frame PTS minus first video frame PTS",
                      "limit_note": ("sample limit omits interval samples; last frame retained when cap >= 2"
                                     if capped else "all requested interval samples plus the last frame")}


def _write_manifest(path, manifest):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".inspection-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(manifest, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        from pianchang.atomic_files import replace_with_retry
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cached(directory, media, digest, size, interval_s, max_frames):
    path = directory / "inspection.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        valid = (isinstance(manifest, dict) and manifest.get("schema_version") == "media-inspection/1"
                 and manifest.get("media_path") == str(media) and manifest.get("media_sha256") == digest
                 and manifest.get("bytes") == size and isinstance(manifest.get("sampling"), dict)
                 and manifest["sampling"].get("interval_s") == interval_s
                 and manifest["sampling"].get("max_frames") == max_frames
                 and isinstance(manifest.get("frames"), list) and isinstance(manifest.get("decode"), dict)
                 and manifest["decode"].get("status") in ("PASS", "FAIL", "UNKNOWN"))
        if not valid:
            raise ValueError("mismatched manifest")
        frames = manifest["frames"]
        sampling = manifest["sampling"]
        if (len(frames) > max_frames
                or sampling.get("extracted_times_s") != [f["time_s"] for f in frames]
                or len({f["id"] for f in frames}) != len(frames)
                or (sampling.get("extraction_complete") is True and not frames)):
            raise ValueError("inconsistent frame inventory")
        for frame in manifest["frames"]:
            image = Path(frame["path"]).resolve()
            if not image.is_relative_to(directory) or not image.is_file() or _sha256(image) != frame["sha256"]:
                raise ValueError("changed frame")
        return manifest
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise MediaError("输出目录非空且不是同一媒体/参数的完整缓存，或帧哈希已改变；请使用新的输出目录。") from None


@traced("media.inspect", kind="tool")
def inspect_media(media_path, output_dir, *, interval_s=1.0, max_frames=24):
    """Inspect a local video; return and atomically save inspection.json.

    Each JPEG is selected by decoded frame index, not a nominal seek timestamp.
    Existing output is reused only for the same source hash/parameters with all
    frame hashes intact; an incompatible/nonempty directory is never overwritten.
    """
    if type(interval_s) not in (int, float) or not math.isfinite(interval_s) or interval_s <= 0:
        raise MediaError("interval_s 必须是有限正数。")
    if type(max_frames) is not int or not 1 <= max_frames <= 100:
        raise MediaError("max_frames 必须是 1 到 100 的整数。")
    interval_s = float(interval_s)
    media = Path(media_path).expanduser().resolve()
    if not media.is_file() or media.stat().st_size == 0:
        raise MediaError("媒体文件不存在或为空。")
    size, digest = media.stat().st_size, _sha256(media)
    directory = Path(output_dir).expanduser().resolve()
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        cached = _cached(directory, media, digest, size, interval_s, max_frames)
        event("cache.hit", {"manifest_path": str(directory / "inspection.json"), "media_sha256": digest})
        return cached
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".inspection.lock"
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except FileExistsError:
        raise MediaError("输出目录正在进行媒体检查，不能并发写入。") from None
    try:
        versions = {}
        for name, executable in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
            try:
                version = _run([executable, "-version"], timeout=15)
            except subprocess.TimeoutExpired:
                raise MediaError(f"{name} 版本查询超时。") from None
            if version.returncode or not version.stdout.strip():
                raise MediaError(f"{name} 无法运行：" + _error(version))
            versions[name] = version.stdout.splitlines()[0]
        metadata = _metadata(_probe(ffprobe, ["-show_format", "-show_streams", str(media)]))
        timeout = max(60, min(300, metadata["duration_s"] * 4 + 30))
        decode_args = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror",
                       "-err_detect", "explode", "-i", media, "-map", "0:v", "-map", "0:a?",
                       "-f", "null", "-"]
        try:
            decoded = _run(decode_args, timeout=timeout)
            passed = decoded.returncode == 0 and not decoded.stderr.strip()
            decode = {"status": "PASS" if passed else "FAIL", "error": None if passed else _error(decoded)}
        except subprocess.TimeoutExpired:
            decode = {"status": "UNKNOWN", "error": "全文件解码超时，不能宣称解码通过。"}
        indexed = _frame_index(_probe(ffprobe, ["-select_streams", "v:0", "-show_frames",
                                               "-show_entries", "frame=best_effort_timestamp_time,pts_time",
                                               str(media)], timeout=timeout))
        selected, sampling = _sample(indexed, interval_s, max_frames)
        frames = []
        for index, frame in enumerate(selected):
            identifier = f"F{index:03}"
            image = directory / (identifier + ".jpg")
            args = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-i", media,
                    "-map", "0:v:0", "-vf", f"select=eq(n\\,{frame['source_frame_index']})",
                    "-fps_mode", "passthrough", "-frames:v", "1", "-q:v", "2", "-update", "1", "-n", image]
            try:
                extracted = _run(args, timeout=timeout)
                if extracted.returncode or not image.is_file() or image.stat().st_size < 4:
                    raise MediaError(_error(extracted))
                with image.open("rb") as stream:
                    if stream.read(2) != b"\xff\xd8":
                        raise MediaError("抽帧输出没有有效 JPEG 文件头。")
                    stream.seek(-2, os.SEEK_END)
                    if stream.read(2) != b"\xff\xd9":
                        raise MediaError("抽帧输出缺少 JPEG 结束标记。")
                frames.append({"id": identifier, **frame, "path": str(image), "sha256": _sha256(image)})
            except (MediaError, subprocess.TimeoutExpired, OSError) as exc:
                image.unlink(missing_ok=True)
                sampling["extraction_errors"].append({**frame, "error": str(exc) or "抽帧失败"})
        sampling["extracted_times_s"] = [f["time_s"] for f in frames]
        sampling["extraction_complete"] = not sampling["extraction_errors"] and len(frames) == len(selected)
        if media.stat().st_size != size or _sha256(media) != digest:
            raise MediaError("检查过程中源视频发生变化；未发布绑定错误的检查清单。")
        status = decode["status"]
        if status == "PASS" and not sampling["extraction_complete"]:
            status = "UNKNOWN"
        manifest = {"schema_version": "media-inspection/1", "media_path": str(media),
                    "media_sha256": digest, "bytes": size, "metadata": metadata,
                    "decode": decode, "frames": frames, "sampling": sampling,
                    "inspection_status": status, "inspected_at": datetime.now(timezone.utc).isoformat(),
                    "tool_versions": versions, "manifest_path": str(directory / "inspection.json")}
        _write_manifest(directory / "inspection.json", manifest)
        event("artifact.inspection_written", {"manifest_path": str(directory / "inspection.json"),
              "media_sha256": digest, "frames": frames}, kind="artifact")
        return manifest
    finally:
        lock.unlink(missing_ok=True)
