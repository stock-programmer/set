"""Turn a verified request for an author's creative constraint into a scoped Pi reply."""
import http.client
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from pi_bridge.creative_input import TOPICS, _read_rows, validate_reservation
from pianchang.app_service import read_json
from tools.workbench_loop_input import _answers, human_request
from tools.workbench_loop_restart import workbench_http_identity
from trace_system import event, traced


def _question(root, job, *, journaled_answer_id=""):
    result = job.get("result") or {}
    check = result.get("completion_check") or {}
    proof = check.get("creative_input")
    if (job.get("kind") not in {"studio_script", "pi_script"} or job.get("status") != "interrupted"
            or job.get("cancel_requested") or job.get("trace_incomplete")
            or result.get("status") != "needs_input" or result.get("reason") != "script_creative_input"
            or result.get("backend") != "pi" or check.get("complete") is not False
            or check.get("reason") != "script_creative_input" or not isinstance(proof, dict)):
        return None
    sid = proof.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch("[a-f0-9]{32}", sid):
        return None
    rows = _read_rows(Path(root) / "outputs/pi/registry.sqlite3", "SELECT * FROM sessions WHERE id=?", (sid,))
    if not rows or rows[0]["active_job"] not in (job["id"], None):
        return None
    proposed = {k: proof[k] for k in ("topic", "source_quote", "choices") if k in proof}
    try:
        if validate_reservation(root, rows[0], job, proposed, journaled_answer_id=journaled_answer_id) != proof:
            return None
    except ValueError:
        return None
    label = TOPICS[proof["topic"]][0]
    field = {"name": "answer", "label": label, "required": True, "type": "text", "max_length": 500}
    if "choices" in proof:
        field.update(type="select", options=[{"value": v, "label": v} for v in proof["choices"]])
        del field["max_length"]
    request = human_request(job, "script_creative_input", proof, "剧本需要你补充" + label,
                            "你在原故事中要求先提供这项资料。提交后，原剧本 Agent 会接着写作。",
                            fields=[field], submit_label="提交并继续写作")
    return {**request, "story_id": rows[0]["source_job"], "session_id": sid}


@traced("workbench.script.question", kind="validation", capture_inputs=False, capture_output=False)
def script_question(root, job):
    return _question(root, job)


def _message_id(request_id):
    return uuid.uuid5(uuid.NAMESPACE_URL, "pianchang/script-creative-input/" + request_id).hex


@traced("pi.story.submit_creative_answer", kind="task", capture_inputs=False)
def accept_script_reply(sessions, body):
    from tools.workbench_loop_watch import _folder
    if (set(body) != {"job_id", "request_id"} or not re.fullmatch("[a-f0-9]{32}", str(body["job_id"]))
            or not re.fullmatch("[a-f0-9]{64}", str(body["request_id"]))):
        raise ValueError("创作资料的提交标识无效。")
    root, jid, rid = sessions.service.root, body["job_id"], body["request_id"]
    saved_path = _folder(root) / "answers" / (rid + ".json")
    if not saved_path.is_file():
        raise ValueError("原问题尚未收到作者的答复。")
    saved = read_json(saved_path)
    request = saved["request"]
    if ((saved.get("request_id"), saved.get("job_id"), request.get("id"), request.get("job_id"), request.get("kind"))
            != (rid, jid, rid, jid, "script_creative_input")):
        raise ValueError("答复与原问题不匹配。")
    aid = _message_id(rid)
    with sessions.lock:
        job = next((j for j in sessions.service.state["jobs"] if j["id"] == jid), None)
        if not job:
            return {"status": "superseded", "request_id": rid}
        session = sessions.repo.get(request["session_id"])
        if (session["role"] != "script" or not isinstance(session["source_job"], str)
                or request.get("story_id") != session["source_job"]
                or session["story_id"] != "story-" + session["source_job"]):
            raise ValueError("资料与剧本会话不匹配。")
        value = _answers(request, saved["answers"])["answer"]
        label = request["fields"][0]["label"]
        text = "继续完成原剧本。作者补充的创作资料如下。\n" + json.dumps({label: value}, ensure_ascii=False)
        existing = next((m for m in sessions.repo.messages(session["id"]) if m["id"] == aid), None)
        if existing:
            if (existing["text"] != text or existing["role"] != "user" or existing["mode"] != "work"
                    or existing.get("creative_answer") != rid):
                raise ValueError("已有答复与原消息不匹配。")
            previous = next((j for j in sessions.service.state["jobs"] if j["id"] == existing.get("job_id")), None)
            if not previous:
                # Submission was interrupted between journaling and creating a task.
                # No worker could have been dispatched without the task and its ID.
                if existing["status"] != "queued" or session["active_job"]:
                    raise ValueError("原创作资料的投递结果有待核实。")
            else:
                if (previous.get("kind") != "pi_script" or previous.get("session_id") != session["id"]
                        or previous.get("_pi_creative_answer") != rid or previous.get("_pi_mode") != "work"):
                    raise ValueError("已有消息没有原资料交接凭据。")
                return {"message_id": aid, "job_id": existing["job_id"], "session_id": session["id"], "duplicate": True}
        if session["active_job"] == jid:
            raise RuntimeError("原剧本进程正在关闭，稍后复查同一份答复。")
        if _question(root, job, journaled_answer_id=aid if existing else "") != request or session["active_job"]:
            return {"status": "superseded", "request_id": rid}
        event("pi.story.creative_reply.request", {"request_id": rid, "source_job": jid, "source_trace_id": job.get("trace_id"),
              "answer_trace_id": saved.get("trace_id"), "message_id": aid,
              "session_id": session["id"], "mode": "work", "story_id": session["story_id"], "text": text}, kind="state")
        response = sessions.submit({"story": session["source_job"]}, "script", text, aid,
                                   mode="work", creative_answer=rid)
        message = response["message"]
        child = (response.get("job") or {}).get("id") or message["job_id"]
        if message["id"] != aid or response["session_id"] != session["id"] or not child:
            raise ValueError("原剧本会话未确认收到资料。")
        return {"message_id": aid, "job_id": child, "session_id": session["id"], "duplicate": bool(response.get("duplicate"))}


@traced("workbench.script.submit_reply", kind="http", capture_inputs=False, capture_output=False)
def submit_script_reply(root, job, request, answer):
    # The server retrieves the author's durable answer. An HTTP caller can only request delivery.
    _answers(request, {"answer": answer})
    origin = read_json(Path(root) / "outputs/web/server-process.json")["url"]
    port = urlsplit(origin).port
    if not port or origin != f"http://127.0.0.1:{port}" or not workbench_http_identity(port):
        raise RuntimeError("工作台身份暂未核实。")
    path = "/api/pi/script-input"
    payload = {"job_id": job["id"], "request_id": request["id"]}
    headers = {"Content-Type": "application/json", "Origin": origin}
    event("workbench.script.reply.request", {"method": "POST", "url": origin + path, "headers": headers,
                                             "body": payload}, kind="http")
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        client.request("POST", path, body=json.dumps(payload).encode(), headers=headers)
        response = client.getresponse()
        data = json.loads(response.read())
        event("workbench.script.reply.response", {"status": response.status, "body": data}, kind="http")
        if response.status == 202 and data == {"status": "superseded", "request_id": request["id"]}:
            return data
        if response.status != 202 or not isinstance(data, dict) or data.get("message_id") != _message_id(request["id"]) or (
                data.get("session_id") != request["session_id"] or not re.fullmatch("[a-f0-9]{32}", str(data.get("job_id")))):
            raise ValueError("原剧本会话未确认资料投递。")
        return data
    except (OSError, http.client.HTTPException, ValueError) as exc:
        event("workbench.script.reply.error", {"type": type(exc).__name__}, kind="http")
        raise
    finally:
        client.close()
