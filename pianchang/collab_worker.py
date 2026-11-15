"""Run one agent's durable inbox for an existing local collaboration session."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import signal
from pathlib import Path
from threading import Event

from pianchang.bailian_text import BailianTextClient, load_text_config
from pianchang.collab_negotiation import VideoNegotiation, consume_revision_work, run_script_negotiation
from pianchang.collaboration_workflow import Collaboration, HOME_DIR, open_ref, ref
from trace_system import TraceError, current_context, event, register_secret, trace, traced


_STOPPED = {"unknown", "failed", "blocked"}


def _check(path, expected, actual):
    passed = actual == expected
    evidence = {"path": path, "expected": expected, "actual": actual, "status": "PASS" if passed else "FAIL"}
    event("collab.worker.validation", evidence, kind="validation")
    if not passed:
        raise ValueError(f"{path}: expected {expected}, got {actual}")


@traced("collab.worker.open", kind="tool")
def open_worker(directory, run_id, env_file=None):
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Expected a 32-digit lowercase hex collaboration ID")
    directory = Path(directory).resolve()
    manifest = directory / "runs" / run_id / "overview.json"
    if not manifest.is_file() or not (directory / "assets.sqlite3").is_file():
        raise ValueError("The collaboration session must already exist")
    _check("session.id", run_id, json.loads(manifest.read_text(encoding="utf-8-sig"))["id"])
    return Collaboration(directory, run_id, env_file=env_file)


@traced("collab.worker.wait", kind="task")
def _wait(session, agent, watch, interval, stop_event):
    while not stop_event.is_set():
        deliveries = session.store._worker_deliveries(agent, session.run_id, session.story)
        pending = next((row for row in deliveries if row["status"] == "pending"), None)
        if pending:
            return {"status": "pending", "message_id": pending["message_id"]}
        unresolved = [row for row in deliveries if row["status"] in _STOPPED]
        if unresolved:
            return {"status": "needs_review", "deliveries": unresolved}
        if not watch:
            return {"status": "already_claimed" if deliveries else "idle", "deliveries": deliveries}
        stop_event.wait(interval)
    return {"status": "stopped"}


def _client(session):
    config = load_text_config(session.env_file)
    register_secret(config.api_key)
    return BailianTextClient(config)


def _observe(session, agent, message, observe):
    store = session.store
    mid = message["message_id"]
    if not store.claim(agent, message_id=mid):
        return {"status": "claimed_elsewhere"}
    try:
        observation = observe()
        store.ack(agent, mid, "completed", result={"observed": observation, "trace_id": current_context()["trace_id"]})
    except TraceError:
        raise
    except Exception as exc:
        store.ack(agent, mid, "blocked", result={"reason": str(exc)})
        raise
    return {"status": "observed", "observation": observation}


def _notice(session, message):
    _check("notice.category", "event", message["category"])
    asset = open_ref(session.store, message["payload"])
    _check("notice.story_id", session.story, asset["story_id"])
    _check("notice.kind", "script", asset["kind"])
    _check("notice.asset_event", message["message_id"], asset["event_id"])
    return {"script": ref(asset), "action": "publication_observed"}


def _production_result(session, message):
    _check("result.category", "result", message["category"])
    _check("result.sender", "video", message["sender"])
    request = session.store.get_message(message["causation_id"])
    if request["message_type"] == "production.recovery_requested":
        original = session.store.get_message(request["payload"]["original_request_id"])
        _check("recovery.original_type", "production.requested", original["message_type"])
        _check("recovery.original_script", ref(original["payload"]["script"]), ref(request["payload"]["script"]))
        _check("recovery.video_budget", 0, request["payload"]["max_video_generations"])
    else:
        _check("result.request_type", "production.requested", request["message_type"])
    _check("result.request_story", session.story, request["story_id"])
    payload = message["payload"]
    script = open_ref(session.store, payload["script"])
    _check("result.story", session.story, script["story_id"])
    _check("result.script", ref(script), ref(request["payload"]["script"]))
    if payload.get("production"):
        delivery = open_ref(session.store, payload["production"])
        _check("result.production_kind", "production", delivery["kind"])
        _check("result.production_story", session.story, delivery["story_id"])
        _check("result.production_baseline", ref(script), ref(delivery["content"]["script_ref"]))
        _check("result.media_accepted", delivery["content"]["media_accepted"], payload.get("media_accepted"))
        return {"production": ref(delivery), "script": ref(script), "media_accepted": payload.get("media_accepted"),
                "regenerated": payload.get("regenerated", []), "reused": payload.get("reused", [])}
    _check("result.stopped", True, payload.get("status") in _STOPPED)
    return {"script": ref(script), "production_status": payload["status"], "reason": payload.get("reason")}


def _dispatch(session, agent, message):
    kind, mid = message["message_type"], message["message_id"]
    if agent == "video" and kind == "script.revision_published":
        return _observe(session, agent, message, lambda: _notice(session, message))
    _check("delivery.recipient", agent, message["recipient"])
    if agent == "video" and kind == "production.requested":
        _check("commission.category", "request", message["category"])
        asset, delivered = session.worker(mid)  # the video agent owns the atomic claim
        return {"status": "completed", "production": ref(asset), "result_message_id": delivered["message_id"]}
    if agent == "script" and kind == "production.result":
        return _observe(session, agent, message, lambda: _production_result(session, message))
    if agent == "script" and kind == "script.change_requested":
        from pianchang.collab_continuation import consume_change
        return {"status": "completed", "script": ref(consume_change(session, message))}
    if agent == "script" and kind in {"script.revision_requested", "script.revision_work_requested"}:
        _check("revision.category", "request", message["category"])
        # Directory names are derived from a digest rather than an arbitrary message ID.
        path = session.folder / "script-inbox" / hashlib.sha256(mid.encode("utf-8")).hexdigest()
        if kind == "script.revision_requested":
            result = run_script_negotiation(_client(session), path, store=session.store, request_id=mid)
        else:
            result = consume_revision_work(_client(session), path, store=session.store, message_id=mid)
        return {"status": result["status"], "agent_trace_id": result["trace_id"]}
    if agent == "video" and kind == "script.revision_response":
        _check("response.category", "response", message["category"])
        _check("response.sender", "script", message["sender"])
        request = session.store.get_message(message["causation_id"])
        tools = VideoNegotiation(session.store, script_ref=request["payload"]["script_ref"],
                                 story_id=session.story, production_message_id=request["causation_id"])
        reply = tools.read_script_response(request["message_id"])
        _check("response.status", "answered", reply["status"])
        return {"status": "observed", "observation": reply}
    raise ValueError(f"Unsupported message {kind} for {agent}")


@traced("collab.worker.consume", kind="task")
def consume(session, agent, message_id):
    message = session.store.get_message(message_id)
    # A mistaken direct call must never claim a message from another session.
    _check("delivery.correlation", session.run_id, message["correlation_id"])
    _check("delivery.story", session.story, message["story_id"])
    _check("delivery.addressee", True, agent in message["deliveries"])
    links = [{**message["trace_context"], "relation": "delivered_from", "message_id": message_id}]
    sender_job = current_context()
    if sender_job:
        links.append({**sender_job, "relation": "dispatched_by"})
    with trace("agent.inbox." + agent, kind="task", inputs=message, links=links,
               attributes={"agent": agent, "correlation_id": session.run_id, "message_id": message_id}) as root:
        try:
            result = _dispatch(session, agent, message)
        except TraceError:
            raise
        except Exception as exc:
            status = session.store.get_message(message_id)["deliveries"].get(agent)
            if status == "pending" and session.store.claim(agent, message_id=message_id):
                session.store.ack(agent, message_id, "blocked", result={"reason": str(exc)})
                status = "blocked"
            if status in _STOPPED:
                result = {"status": "needs_review", "delivery_status": status, "reason": str(exc)}
                root.set_status("ERROR", str(exc))
            else:
                # A concurrent consumer may have won the claim before dispatch.
                result = {"status": "claimed_elsewhere", "delivery_status": status, "detail": str(exc)}
        output = {**result, "message_id": message_id, "message_type": message["message_type"], "trace_id": root.trace_id}
        root.set_output(output)
        return output


@traced("collab.worker.run", kind="task")
def run_worker(session, agent, *, watch=False, poll_interval=2.0, stop_event=None, reporter=None):
    if agent not in {"script", "video"} or not 0.05 <= poll_interval <= 300:
        raise ValueError("Choose script/video and a polling interval from 0.05 to 300 seconds")
    stop = stop_event or Event()
    handled = 0
    last = None
    while True:
        next_item = _wait(session, agent, watch, poll_interval, stop)
        if next_item["status"] != "pending":
            return {**next_item, "handled": handled, "last": last}
        last = consume(session, agent, next_item["message_id"])
        if reporter:
            reporter(last)
        if last["status"] != "claimed_elsewhere":
            handled += 1
        if not watch or last["status"] in _STOPPED | {"needs_review"}:
            return {"status": last["status"], "handled": handled, "last": last}


@traced("cli.collab_worker", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description="领取现有协作会话中一个 Agent 的消息。默认只处理一条。")
    parser.add_argument("--run-id", required=True, help="现有协作 session 的 ID")
    parser.add_argument("--agent", required=True, choices=("script", "video"))
    parser.add_argument("--directory", type=Path, default=HOME_DIR)
    parser.add_argument("--env-file", type=Path, help="仅在有明确模型任务时加载配置")
    parser.add_argument("--watch", action="store_true", help="持续监听；Ctrl+C 在当前任务结束后退出")
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args(argv)
    stop = Event()
    session = open_worker(args.directory, args.run_id, args.env_file)
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *unused: stop.set())
        def emit(value):
            print(json.dumps(value, ensure_ascii=False), flush=True)
        result = run_worker(session, args.agent, watch=args.watch, poll_interval=args.poll_interval,
                            stop_event=stop, reporter=emit if args.watch else None)
        emit(result)
        return 2 if result["status"] in _STOPPED | {"needs_review"} else 0
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
