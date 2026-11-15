"""Evidence-backed script revision requests between independent production and script agents."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pianchang.agent_loop import AgentLoop, Tool
from pianchang.bailian_text import BailianTextClient, load_text_config
from pianchang.collab_store import CollabStore, CollaborationConflict
from pianchang.script_collab_agent import publish_script_revision, run_script_agent
from pianchang.story_spec import index_sources
from trace_system import TraceError, current_context, event, register_secret, trace, traced


TEXT = {"type": "string", "minLength": 1, "maxLength": 4000}
DECISIONS = ("retain", "alternative", "accept")


def _inspection_context(observed):
    """Keep full evidence in the asset/Trace; avoid repeating unrelated transport journals in planner input."""
    content=observed["evidence"]["content"]
    if not isinstance(content,dict) or "report" not in content:return observed
    report=content["report"]
    if not isinstance(report,dict) or not isinstance(report.get("results"),list):return observed
    selected={k:content[k] for k in ("shot_id","shot","media_sha256","report_sha256","report_path") if k in content}
    selected["checks"]=[r for r in report["results"] if r["criterion_id"] in
                       {"action_sequence","start_end_state","character_identity","scene_geometry"}]
    contract=content.get("contract",{})
    selected["expected_state_before"]=contract.get("expected_state_before")
    selected["expected_state_after"]=contract.get("expected_state_after")
    return {**observed,"evidence":{**observed["evidence"],"content":selected}}


def _parameters(**fields):
    return {"type": "object", "properties": fields, "required": list(fields)}


def _ref(asset):
    return {k: asset[k] for k in ("asset_id", "revision_id", "sha256")}


def _verify(path, expected, actual):
    matches = expected == actual
    check = {"path": path, "expected": expected, "actual": actual, "status": "PASS" if matches else "FAIL"}
    event("negotiation.check", check, kind="validation")
    if not matches:
        raise ValueError(f"{path}: expected {expected}, actual {actual}")
    return check


def _bound_asset(store, ref, story_id):
    asset = store.get_asset(ref["asset_id"], ref["revision_id"])
    _verify("asset.story_id", story_id, asset["story_id"])
    if ref.get("sha256"):
        _verify("asset.sha256", ref["sha256"], asset["sha256"])
    return asset


def _beat(source, beat_id):
    target = index_sources(source["content"]).get(beat_id)
    _verify("target_source.kind", "beat", target and target["kind"])
    return target


def _links(row):
    links = [{**row["trace_context"], "relation": "receives", "message_id": row["message_id"]}]
    if current_context():
        links.append({**current_context(), "relation": "submitted_by"})
    return links


class VideoNegotiation:
    """Optional tools for one explicitly addressed production task. Reading replies never edits the script."""
    def __init__(self, store, *, script_ref, story_id, production_message_id, evidence_refs=(), recovering=False):
        self.store = store
        self.script_ref = script_ref
        self.story_id = story_id
        self.production_message_id = production_message_id
        self.evidence_refs = list(evidence_refs)
        self.recovering = recovering

    def tools(self):
        return [
            Tool("read_negotiation_context", "读取本次剧本版本、可引用的证据与已经发出的改稿请求。",
                 _parameters(), self.context),
            Tool("request_script_revision", "向剧本 Agent 提交具体节拍的矛盾、证据和建议。一次制作委托每个节拍只发一单。"
                 "请求本身不改变剧本。证据可引用当前剧本版本，或相关已归档制作证据。",
                 _parameters(beat_id=TEXT, issue=TEXT, suggestion=TEXT, evidence_asset_id=TEXT,
                             evidence_revision_id=TEXT), self.request_script_revision, effect="write"),
            Tool("read_script_response", "按请求 ID 读取剧本 Agent 回应、待办改稿或实际发布版本；无回复返回 pending。",
                 _parameters(request_id=TEXT), self.read_script_response),
        ]

    @traced("negotiation.video.context", kind="tool")
    def context(self):
        requests = self.store.messages(causation_id=self.production_message_id, message_type="script.revision_requested")
        return {"script_ref": self.script_ref, "evidence_refs": [self.script_ref, *self.evidence_refs],
                "requests": [{"request_id": r["message_id"], "beat_id": r["payload"]["beat_id"]} for r in requests]}

    @traced("negotiation.video.request", kind="tool")
    def request_script_revision(self, beat_id, issue, suggestion, evidence_asset_id, evidence_revision_id):
        if any(not isinstance(text, str) or not text.strip() for text in (issue, suggestion)):
            raise ValueError("A revision request needs an issue and a concrete suggestion")
        source = _bound_asset(self.store, self.script_ref, self.story_id)
        target = _beat(source, beat_id)
        production = self.store.get_message(self.production_message_id)
        _verify("production.type", "production.requested", production["message_type"])
        _verify("production.story_id", self.story_id, production["story_id"])
        _verify("production.recipient", "video", production["recipient"])
        commissioned = production["payload"].get("script") or {}
        _verify("production.script", _ref(source), {k: commissioned.get(k) for k in ("asset_id", "revision_id", "sha256")})
        _verify("production.accepted", True, production["deliveries"]["video"] in
                ({"accepted", "completed","unknown","blocked"} if self.recovering else {"accepted","completed"}))
        evidence = self.store.get_asset(evidence_asset_id, evidence_revision_id)
        _verify("evidence.story_id", self.story_id, evidence["story_id"])
        same_source = _ref(evidence) == _ref(source)
        scoped = any(r.get("asset_id") == evidence_asset_id and r.get("revision_id") == evidence_revision_id
                     and r.get("sha256") == evidence["sha256"] for r in self.evidence_refs)
        linked = any(d["asset_id"] == source["asset_id"] and d["revision_id"] == source["revision_id"]
                     for d in evidence["dependencies"])
        _verify("evidence.in_task_scope", True, same_source or scoped or linked)
        key = hashlib.sha256(json.dumps([production["message_id"], beat_id]).encode("utf-8")).hexdigest()
        payload = {"script_ref": _ref(source), "beat_id": beat_id, "beat_text": target["text"],
                   "issue": issue, "suggestion": suggestion, "evidence_ref": _ref(evidence)}
        msg = self.store.send("script.revision_requested", sender="video", recipient="script", category="request",
            story_id=self.story_id, causation_id=production["message_id"], message_id="review-" + key,
            payload=payload)
        return {"request_id": msg["message_id"], "status": msg["deliveries"]["script"], "submitted_request": payload}

    @traced("negotiation.video.read_response", kind="tool")
    def read_script_response(self, request_id):
        request = self.store.get_message(request_id)
        _verify("request.type", "script.revision_requested", request["message_type"])
        _verify("request.sender", "video", request["sender"])
        _verify("request.story_id", self.story_id, request["story_id"])
        ref = request["payload"]["script_ref"]
        _verify("request.script_asset", self.script_ref["asset_id"], ref["asset_id"])
        replies = self.store.messages(causation_id=request_id, message_type="script.revision_response")
        output = {"request_id": request_id, "source_revision": ref["revision_id"],
                  "applies_to_target_revision": ref["revision_id"] == self.script_ref["revision_id"]}
        if not replies:
            return {**output, "status": "pending", "request_delivery": request["deliveries"]["script"]}
        reply = replies[0]
        if self.store.claim("video", message_id=reply["message_id"]):
            self.store.ack("video", reply["message_id"], "completed", result={"observation": "read"})
        response = reply["payload"]
        work_id = response["revision_work_id"]
        published = []
        if work_id:
            output["revision_work_status"] = self.store.get_message(work_id)["deliveries"]["script"]
            published = self.store.messages(causation_id=work_id, message_type="script.revision_published")
        return {**output, "status": "answered", "response": response,
                "published_revisions": [{k: m["payload"][k] for k in ("asset_id", "revision_id", "sha256")}
                                        for m in published]}


class ScriptNegotiation:
    def __init__(self, store, request):
        self.store, self.request = store, request
        self.inspected = False
        self.reply = None

    def tools(self):
        return [
            Tool("inspect_revision_request", "读取视频方的问题、建议、原剧本节拍和绑定版本的实际证据。",
                 _parameters(), self.inspect,context_result=_inspection_context),
            Tool("decide_revision_request", "retain 保持原稿，alternative 提供替代制作建议，accept 提交单独的改稿任务。"
                 "后两项须在 instruction 中写具体方案。接受请求仅委托改稿，不能声称新版已发布。",
                 _parameters(decision={"type": "string", "enum": list(DECISIONS)}, explanation=TEXT,
                             instruction={"type": "string", "maxLength": 4000}), self.decide, effect="write"),
        ]

    @traced("negotiation.script.snapshot", kind="tool")
    def snapshot(self):
        return {"role": "剧本 Agent，处理视频制作方的改稿请求", "request_id": self.request["message_id"],
                "incoming_payload": self.request["payload"], "evidence_inspected": self.inspected,
                "response": self.reply and self.reply["payload"]}

    @traced("negotiation.script.restore", kind="tool")
    def restore(self, record):
        self.recovering=True
        observed=self.inspect()
        replies=self.store.messages(causation_id=self.request["message_id"],message_type="script.revision_response")
        self.reply=replies[0] if replies else None
        return {"evidence_reinspected":True,"inspection":_inspection_context(observed),
                "persisted_response":self.reply,"new_revision_work":False}

    @traced("negotiation.script.inspect", kind="tool")
    def inspect(self):
        p = self.request["payload"]
        source = _bound_asset(self.store, p["script_ref"], self.request["story_id"])
        evidence = _bound_asset(self.store, p["evidence_ref"], self.request["story_id"])
        target = _beat(source, p["beat_id"])
        _verify("request.beat_text", target["text"], p["beat_text"])
        self.inspected = True
        return {"request": p, "beat": target, "evidence": {"ref": _ref(evidence), "content": evidence["content"]},
                "current_script_head": _ref(self.store.get_asset(source["asset_id"]))}

    @traced("negotiation.script.decide", kind="tool")
    def decide(self, decision, explanation, instruction):
        if not self.inspected:
            raise ValueError("Inspect the bound evidence before making a decision")
        p = self.request["payload"]
        if p["issue"].startswith("[验收规格核对，禁止改写剧本]") and decision=="accept":
            raise ValueError("本请求仅核对验收规格，没有委托改写剧本。请保持原稿并提供有证据的制作建议或冲突说明。")
        if decision == "accept":
            _verify("accept.base_revision", p["script_ref"]["revision_id"],
                    self.store.get_asset(p["script_ref"]["asset_id"])["revision_id"])
        self.reply = self.store.respond_to_revision(self.request["message_id"], decision, explanation, instruction,
                                                   recovery=getattr(self,"recovering",False))
        return {"response_id": self.reply["message_id"], **self.reply["payload"]}

    @traced("negotiation.script.finish_check", kind="validation")
    def finish_check(self):
        persisted = self.reply and self.store.get_message(self.reply["message_id"])
        valid = bool(self.inspected and persisted and persisted["payload"] == self.reply["payload"]
                     and persisted["causation_id"] == self.request["message_id"])
        if valid and persisted["payload"]["decision"] == "accept":
            job = self.store.get_message(persisted["payload"]["revision_work_id"])
            valid = job["causation_id"] == persisted["message_id"] and job["payload"]["script_ref"] == self.request["payload"]["script_ref"]
        return {"complete": valid, "checks": [{"path": "revision_response.persisted", "expected": True,
                "actual": valid, "status": "PASS" if valid else "FAIL"}],
                "summary": "改稿回应已送达消息系统；当前剧本版本以实际发布记录为准。" if valid else "尚需检查证据并保存改稿回应。"}


@traced("negotiation.script.handle", kind="agent")
def run_script_negotiation(client, directory, *, store, request_id, max_steps=8):
    request = store.get_message(request_id)
    _verify("request.type", "script.revision_requested", request["message_type"])
    _verify("request.recipient", "script", request["recipient"])
    _verify("request.sender", "video", request["sender"])
    production = store.get_message(request["causation_id"])
    _verify("causation.type", "production.requested", production["message_type"])
    _verify("causation.story_id", request["story_id"], production["story_id"])
    _verify("causation.recipient", "video", production["recipient"])
    commissioned = production["payload"].get("script") or {}
    _verify("causation.script", request["payload"]["script_ref"],
            {k: commissioned.get(k) for k in ("asset_id", "revision_id", "sha256")})
    with trace("agent.task.script_negotiation", kind="agent", inputs=request, links=_links(request),
               attributes={"agent": "script", "story_id": request["story_id"]}) as root:
        claimed = store.claim("script", message_id=request_id)
        resuming = False
        if not claimed:
            replies = store.messages(causation_id=request_id, message_type="script.revision_response")
            if replies:
                if request["deliveries"]["script"] in {"received","accepted","unknown","blocked"}:
                    if request["deliveries"]["script"]=="received":store.ack("script",request_id,"accepted")
                    store.ack("script",request_id,"completed",recovery=True,
                              result={"response_id":replies[0]["message_id"],"observed_persisted_reply":True})
                result = {"trace_id": root.trace_id, "status": "reply_available",
                          "response": replies[0], "model_called": False}
                root.set_output(result)
                return result
            prior_path=Path(directory)/"run.json"
            recoverable=False
            if prior_path.is_file():
                prior=json.loads(prior_path.read_text("utf-8"))
                failures=[]
                for message in prior["messages"]:
                    if message["role"]!="user":continue
                    try:item=json.loads(message["content"])
                    except ValueError:continue
                    if item.get("type")=="tool_result" and not item.get("ok"):failures.append(item.get("error",{}).get("code"))
                recoverable=(prior["steps"]<prior["limits"]["max_steps"] and failures and
                             all(code in {"INVALID_DECISION","INVALID_TOOL_ARGUMENTS","FINISH_REJECTED"} for code in failures))
            if (request["deliveries"]["script"] in {"received","accepted","unknown"} or
                    request["deliveries"]["script"]=="blocked" and recoverable) and prior_path.is_file():
                resuming=True
            else:raise CollaborationConflict("A prior worker has claimed this request without a recoverable execution record.")
        if claimed or request["deliveries"]["script"]=="received":store.ack("script", request_id, "accepted")
        try:
            workspace = ScriptNegotiation(store, request)
            from pi_bridge.runtime import make_loop
            loop = make_loop(client, workspace.tools(), directory, role="script", snapshot=workspace.snapshot, finish_check=workspace.finish_check,
                             recover=workspace.restore)
            result = loop.run("根据当前改稿请求与制作证据，判断保持剧本、给出替代制作方案、或委托修改目标节拍。"
                              "先检查具体证据，然后向制作方发出有理由的决定。", max_steps=max_steps,resume=resuming)
            receipt_status = "completed" if result["task_complete"] else "blocked"
            if not (resuming and receipt_status=="blocked" and request["deliveries"]["script"]=="blocked"):
                store.ack("script", request_id, receipt_status,recovery=resuming,
                          result={"response_id": workspace.reply and workspace.reply["message_id"], "loop_status": result["status"]})
            output = {"trace_id": root.trace_id, "loop_result": result, "response": workspace.reply, "status": receipt_status}
            if not result["task_complete"]:
                root.set_status("UNKNOWN")
            root.set_output(output)
            return output
        except TraceError:
            raise
        except Exception:
            if store.get_message(request_id)["deliveries"]["script"] not in {"unknown","blocked"}:store.ack("script", request_id, "unknown")
            raise


@traced("negotiation.script.consume_work", kind="agent")
def consume_revision_work(client, directory, *, store, message_id, max_steps=12):
    """Execute a previous accept decision explicitly; a stale base always blocks publication."""
    work = store.get_message(message_id)
    _verify("work.type", "script.revision_work_requested", work["message_type"])
    reply = store.get_message(work["causation_id"])
    _verify("work.decision", "accept", reply["payload"].get("decision"))
    _verify("work.authorized_id", message_id, reply["payload"].get("revision_work_id"))
    p = work["payload"]
    with trace("agent.task.script_revision_work", kind="agent", inputs=work, links=_links(work),
               attributes={"agent": "script", "story_id": work["story_id"]}) as root:
        if not store.claim("script", message_id=message_id):
            published = store.messages(causation_id=message_id, message_type="script.revision_published")
            if published:
                output = {"trace_id": root.trace_id, "status": "publication_available", "publication": published[0]["payload"]}
                root.set_output(output)
                return output
            raise CollaborationConflict("This revision job has already been claimed. Verify its existing work first.")
        store.ack("script", message_id, "accepted")
        try:
            ref = p["script_ref"]
            source = _bound_asset(store, ref, work["story_id"])
            head = store.get_asset(ref["asset_id"])
            if head["revision_id"] != ref["revision_id"]:
                check = {"path": "work.base_revision", "expected": ref["revision_id"], "actual": head["revision_id"], "status": "FAIL"}
                store.ack("script", message_id, "blocked", result=check)
                root.set_status("UNKNOWN")
                output = {"trace_id": root.trace_id, "status": "blocked", "check": check}
                root.set_output(output)
                return output
            def publish(candidate, report):
                return publish_script_revision(store, candidate, report, asset_id=ref["asset_id"], story_id=work["story_id"],
                    base_revision=ref["revision_id"], source_sha256=ref["sha256"], correlation_id=work["correlation_id"],
                    causation_id=message_id)
            result = run_script_agent(client, directory, story_id=work["story_id"], mode="revise",
                goal=p["instruction"], script=source["content"], base_revision=ref["revision_id"], beat_id=p["beat_id"],
                publish=publish, max_steps=max_steps, links=[{**work["trace_context"], "relation": "receives"}])
            status = "completed" if result["loop_result"]["task_complete"] else "blocked"
            store.ack("script", message_id, status, result={"publication": result["receipt"], "script_trace_id": result["trace_id"]})
            output = {"trace_id": root.trace_id, "script_trace_id": result["trace_id"], "publication": result["receipt"], "status": status}
            if status != "completed":
                root.set_status("UNKNOWN")
            root.set_output(output)
            return output
        except TraceError:
            raise
        except Exception:
            store.ack("script", message_id, "unknown")
            raise


@traced("cli.collab_negotiation", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description="处理指定的跨 Agent 改稿请求或已被接受的改稿任务。")
    parser.add_argument("mode", choices=("respond", "revise"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    config = load_text_config(args.env_file)
    register_secret(config.api_key)
    client, store = BailianTextClient(config), CollabStore(args.store)
    if args.mode == "respond":
        result = run_script_negotiation(client, args.output, store=store, request_id=args.message_id)
    else:
        result = consume_revision_work(client, args.output, store=store, message_id=args.message_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"completed", "publication_available", "reply_available"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
