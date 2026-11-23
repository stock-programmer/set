"""Send a verified original voice task back to this workbench's asset worker."""
import http.client
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from pianchang.app_service import read_json
from pianchang.story_spec import digest_json
from tools.workbench_loop_restart import workbench_http_identity
from trace_system import event, traced


class ImageRecoveryRejected(ValueError):
    """A verified workbench replied that it rejected this command."""

    def __init__(self, reason, http_status):
        self.reason = reason
        self.http_status = http_status
        super().__init__(f"工作台已拒绝本次补图请求（HTTP {http_status}，{reason}）。")


_LEGACY_REJECTIONS = {
    "不能核实这份原始参考图委托。": "image_scope_changed",
    "原资产委托的剧本或设定已改变。": "image_scope_changed",
    "原工单没有仍缺少素材且供应商已明确拒绝的参考图。": "image_scope_changed",
    "原工单没有仍可按现有报告和剩余额度修订的参考图。": "image_scope_changed",
    "当前参考图路由与已核对的模型不一致。": "image_route_changed",
}


@traced("workbench.watch.resume_voice", kind="tool", capture_inputs=False)
def request_voice_recovery(root, job):
    return _submit(root, job, "asset-regenerate", {"resume_voice_from":job["id"]}, "voice-recovery-" + job["id"])


@traced("workbench.watch.reinspect_voice", kind="tool", capture_inputs=False)
def request_voice_inspection(root, job, pin):
    from tools.workbench_loop_voice_review import FORMAT
    key = "voice-inspection-" + digest_json([job["id"], pin, FORMAT])
    return _submit(root, job, "asset-inspect", {"review_voice_from":job["id"],"pin":pin}, key)


@traced("workbench.watch.resume_images", kind="tool", capture_inputs=False)
def request_image_recovery(root, job, model_id, *, after_interrupted=None, after_review=None, after_not_sent=None, after_reinspect=None,
                           after_authorized_review=None, after_model_rejection=None, answer_id=None):
    from pianchang.image_recovery import image_recovery_key
    payload = {"resume_images_from": job["id"], "model_id": model_id}
    if after_interrupted:
        payload["after_interrupted"] = after_interrupted
    if after_reinspect:
        payload["after_reinspect"] = after_reinspect
    if after_authorized_review:
        payload["after_authorized_review"] = after_authorized_review
    if after_model_rejection:
        payload["after_model_rejection"] = after_model_rejection
    if after_review:
        payload["after_review"] = after_review
    if after_not_sent:
        payload["after_not_sent"] = after_not_sent
    if answer_id:
        payload["answer_id"] = answer_id
    return _submit(root, job, "asset-production", payload, image_recovery_key(job["project_id"], payload))


def _submit(root, job, action, payload, key):
    jid, pid = job["id"], job["project_id"]
    if not re.fullmatch("[a-f0-9]{32}", jid) or not re.fullmatch("[a-f0-9]{16}", pid):
        raise ValueError("无效的原资产任务。")
    origin = read_json(Path(root) / "outputs/web/server-process.json")["url"]
    port = urlsplit(origin).port
    if not port or origin != f"http://127.0.0.1:{port}" or not workbench_http_identity(port):
        raise RuntimeError("工作台身份暂未核实。")
    path = f"/api/projects/{pid}/studio/{action}"
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    headers = {"Content-Type": "application/json", "Origin": origin, "Idempotency-Key": key}
    label = "workbench.images.resume" if action == "asset-production" else "workbench.voice.resume"
    event(label + ".request", {"method":"POST", "url":origin+path, "headers":headers,
          "body":json.loads(body)}, kind="http")
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        client.request("POST", path, body=body, headers=headers)
        response = client.getresponse()
        reply = json.loads(response.read())
        if not isinstance(reply, dict):
            raise ValueError("工作台未返回有效的工单回执。")
        child = reply.get("job") or {}
        if not isinstance(child, dict):
            raise ValueError("工作台未返回有效的工单回执。")
        event(label + ".response", {"status":response.status, "job_id":child.get("id"),
              "trace_id":response.getheader("X-Trace-Id"), "error":reply.get("error"), "code":reply.get("code")}, kind="http")
        if (action == "asset-production" and response.status in (400, 409) and not child
                and isinstance(reply.get("error"), str) and reply["error"]):
            code = reply.get("code")
            if code is None:
                code = _LEGACY_REJECTIONS.get(reply["error"])
            reason = code if code in ("image_scope_changed", "image_route_changed", "image_transport_unavailable") else "image_command_rejected"
            raise ImageRecoveryRejected(reason, response.status)
        if (response.status != 202 or child.get("kind") != action.replace("-", "_") or child.get("project_id") != pid
                or not re.fullmatch("[a-f0-9]{32}", child.get("id", ""))):
            raise ValueError("工作台未确认原资产任务的恢复请求。")
        return child["id"]
    except (OSError, http.client.HTTPException, ValueError) as exc:
        event(label + ".error", {"error_type":type(exc).__name__}, kind="http")
        raise
    finally:
        client.close()
