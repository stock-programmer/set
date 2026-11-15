"""Convert a frozen shot into the Ark video protocol used by LiteLLM."""
import hashlib
import os
import re
import tempfile
from pathlib import Path

from pianchang.bailian_video import VideoError, validate_mp4_header
from trace_system import TraceError, event, get_tracer


def ark_body(payload):
    inputs, params = payload["input"], payload["parameters"]
    content = [{"type": "text", "text": inputs["prompt"]}]
    for image in inputs.get("media", []):
        content.append({"type": "image_url", "role": "reference_image", "image_url": {"url": image["url"]}})
        if "reference_voice" in image:
            content.append({"type": "audio_url", "role": "reference_audio",
                            "audio_url": {"url": image["reference_voice"]}})
    return {"model": payload["model"], "content": content, "duration": params["duration"],
            "resolution": params["resolution"].lower(), "ratio": params["ratio"], "watermark": params["watermark"],
            **({"seed": params["seed"]} if "seed" in params else {})}


def ark_create_url(base_url):
    base = base_url.rstrip("/")
    if "/api/" in base:
        base = base.rsplit("/api/", 1)[0]
    elif base.endswith("/api") or base.endswith("/v1"):
        base = base.rsplit("/", 1)[0]
    return base + "/api/v3/contents/generations/tasks"


def sdk_expected_size(job):
    size = job.get("sdk_expected_size")
    if not isinstance(size, str) or not re.fullmatch(r"[1-9]\d{1,4}x[1-9]\d{1,4}", size):
        raise VideoError("SDK 提交回执缺少所请求画幅的目标分辨率，无法核验画幅。")
    return size.replace("x", "*")


class SDKVideoClient:
    def __init__(self, config):
        self.config = config

    def _call(self, operation, **kwargs):
        from pianchang.sdk_transport import call_sdk
        try:
            return call_sdk(self.config.route, operation, timeout=self.config.request_timeout, **kwargs)
        except (TraceError, VideoError):
            raise
        except Exception as exc:
            wire = getattr(exc, "wire_receipt", {}) or {}
            error = VideoError(f"SDK 视频操作 {operation} 失败：{get_tracer().sanitize(str(exc))}",
                               http_status=wire.get("http_status"), request_id=wire.get("provider_request_id"))
            error.network_dispatched = getattr(exc, "network_dispatched", None)
            raise error from exc

    def submit(self, payload, *, record_wire):
        body = ark_body(payload)
        def verify(method, url, actual, wire_sha256):
            if method != "POST" or str(url) != ark_create_url(self.config.base_url) or actual != body:
                raise VideoError("SDK 实际视频请求与确认的模型、时长、画幅或参考素材不匹配。")
            record_wire(wire_sha256)

        kwargs = {key: body[key] for key in ("resolution", "ratio", "watermark", "seed") if key in body}
        if len(body["content"]) > 1:
            kwargs["content"] = body["content"][1:]
        result = self._call("video_generation", prompt=payload["input"]["prompt"], seconds=str(body["duration"]),
                            before_send=verify, extra_body=kwargs)
        proof = result.get("_wire_receipt") or {}
        raw_id = result.get("_provider_task_id") or proof.get("provider_task_id")
        if not raw_id or not result.get("id") or not proof.get("wire_body_sha256"):
            raise VideoError("SDK 未返回可核对的供应商任务 ID 或传输回执，须核对原始提交。")
        return {"output": {"task_id": raw_id, "task_status": "PENDING"},
                "provider_task_id": raw_id, "request_id": result.get("_provider_request_id"),
                "wire_receipt": proof, "expected_size": result.get("size")}

    def get_task(self, task_id):
        result = self._call("video_status", video_id=task_id)
        status = {"queued": "PENDING", "in_progress": "RUNNING", "completed": "SUCCEEDED",
                  "failed": "FAILED", "cancelled": "CANCELED"}.get(result.get("status"), "UNKNOWN")
        return {"output": {"task_id": result.get("_provider_task_id"), "task_status": status,
                            "message": (result.get("error") or {}).get("message")},
                "request_id": result.get("_provider_request_id"), "usage": result.get("usage")}

    def download_task(self, task_id, output_path):
        data = self._call("video_content", video_id=task_id)
        if not isinstance(data, bytes):
            raise VideoError("SDK 没有返回视频文件字节。")
        validate_mp4_header(data[:12],len(data))
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        from pianchang.atomic_files import replace_with_retry
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=output_path.parent, delete=False) as handle:
                temp = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temp, output_path)
            event("artifact.downloaded", {"path":str(output_path.resolve()), "bytes":len(data),
                  "sha256":hashlib.sha256(data).hexdigest(), "provider_task_id":task_id}, kind="artifact")
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        return output_path
