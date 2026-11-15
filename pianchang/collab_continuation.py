"""Reconcile already downloaded clips and continue the pinned commission after a provider interruption."""
import argparse
import uuid
from pathlib import Path

from pianchang.app_service import read_json
from pianchang.collaboration_workflow import HOME_DIR, Collaboration, causal, dep, open_ref, ref
from pianchang.story_spec import index_sources
from trace_system import TraceError, get_tracer, trace, traced


def matching(store, run_id, kind, revision, key="script"):
    return [m for m in store.messages(run_id, message_type=kind)
            if m["payload"].get(key, {}).get("revision_id") == revision]


@traced("collaboration.reconcile_first_delivery", kind="task")
def first_delivery(session):
    """Use an existing successful delivery, or verify a failed order's downloaded clips locally."""
    store = session.store
    results = matching(store, session.run_id, "production.result", "r1")
    completed = [m for m in results if m["payload"].get("production")]
    if completed:
        return open_ref(store, completed[0]["payload"]["production"]), completed[0]
    orders = matching(store, session.run_id, "production.requested", "r1")
    if len(orders) != 1 or not results or orders[0]["deliveries"].get("video") not in {"unknown","failed","blocked"}:
        raise ValueError("首版委托必须已有明确的停止记录和可核对的素材")
    from pianchang.video_collab_agent import recover_preview
    order = orders[0]
    source = open_ref(store, order["payload"]["script"])
    template = open_ref(store, order["payload"]["template"])
    recovery = store.send("production.recovery_requested", sender="commissioner", recipient="video",
        payload={"script":ref(source), "original_request_id":order["message_id"], "max_video_generations":0},
        story_id=session.story, correlation_id=session.run_id, causation_id=results[-1]["message_id"])
    with trace("video.reconcile_local_delivery", kind="task", links=[causal(results[-1],"recovers_from"),
               causal(recovery,"recovery_commission")], inputs=recovery["payload"]) as root:
        store.claim("video", message_id=recovery["message_id"])
        store.ack("video", recovery["message_id"], "accepted", result={"network_calls":0})
        session.stage("首版本地恢复", root.trace_id, "running", "r1", "核对三个已下载镜头并导出预览")
        try:
            production = recover_preview(source_directory=session.folder / "video-r1",
                directory=session.folder / ("recover-" + recovery["message_id"]), script=source["content"],
                script_ref=ref(source), template_project=template["content"])
            with store.transaction():
                package = session.register_delivery(production, source, None, recovery)
                payload = {"production":ref(package), "script":ref(source), "media_accepted":production["media_accepted"],
                    "recovered":True, "original_request_id":order["message_id"], "new_cloud_calls":0,
                    "regenerated":production["impact"]["regenerate"], "reused":production["impact"]["reuse"]}
                result = store.send("production.result", sender="video", recipient="script", category="result",
                    story_id=session.story, correlation_id=session.run_id, causation_id=recovery["message_id"], payload=payload)
                store.ack("video", recovery["message_id"], "completed", result={"delivery":ref(package)})
            session.stage("首版本地恢复", root.trace_id, "completed", "r1", "核对已有素材，首版预览已导出；缺失观察保留待审看")
            root.set_output(payload)
            return package, result
        except TraceError:
            raise
        except Exception as exc:
            if store.get_message(recovery["message_id"])["deliveries"]["video"] != "completed":
                store.ack("video", recovery["message_id"], "blocked", result={"reason":str(exc)})
            session.stage("首版本地恢复", root.trace_id, "blocked", "r1", str(exc))
            raise


@traced("collaboration.prepare_script_change", kind="task")
def prepare_change(session, previous, result):
    store = session.store
    prior_requests = store.messages(session.run_id, message_type="script.change_requested")
    if prior_requests:
        return prior_requests[0]
    record = read_json(session.index)
    root = next(s for s in get_tracer().get_trace(record["trace_id"])["spans"] if not s["parent_span_id"])
    initial = get_tracer().resolve(root["inputs"])
    change, beat = initial["change"], initial["beat"]
    script = open_ref(store, previous["content"]["script_ref"])
    shots = previous["content"]["impact"]["shots"]
    direct = [sid for sid, row in shots.items() if beat in row["target"]["source_ids"]]
    order = [s["id"] for s in previous["content"]["project"]["shots"]]
    edges = [[a,b] for a,b in zip(order,order[1:]) if a in direct or b in direct]
    plan = {"status":"proposed", "beat":beat, "before":index_sources(script["content"])[beat]["text"],
            "requested_change":change, "direct_consumers":direct, "adjacent_checks":edges,
            "basis":"首版真实来源引用；目标版本发布后还需比较完整制作输入和状态依赖。"}
    with store.transaction():
        proposal = store.publish_asset(session.story + "/change-proposal", plan, kind="change_proposal",
            producer="commissioner", story_id=session.story, correlation_id=session.run_id,
            dependencies=[dep(script,"proposed_source",beat), dep(previous,"production_baseline")],
            causation_id=result["message_id"])
        return store.send("script.change_requested", sender="commissioner", recipient="script", story_id=session.story,
            correlation_id=session.run_id, causation_id=result["message_id"],
            payload={"base":ref(script), "proposal":ref(proposal), "beat":beat,"goal":change},
            message_id=uuid.uuid5(uuid.NAMESPACE_URL, session.run_id + "/requested-change").hex)


@traced("collaboration.consume_script_change", kind="task")
def consume_change(session, request):
    store = session.store
    base = open_ref(store, request["payload"]["base"])
    if (request["sender"], request["recipient"], request["message_type"], request["story_id"], request["correlation_id"]) != (
            "commissioner","script","script.change_requested",session.story,session.run_id):
        raise ValueError("改稿委托的来源或任务绑定无效")
    published = store.messages(causation_id=request["message_id"], message_type="script.revision_published")
    if published:
        return open_ref(store, published[0]["payload"])
    if not store.claim("script", message_id=request["message_id"]):
        raise ValueError("改稿委托已领取，须核对已有执行记录")
    try:
        if ref(store.get_asset(base["asset_id"])) != ref(base):
            raise ValueError("改稿基础版本已过期")
        store.ack("script", request["message_id"], "accepted")
        target = "r" + str(int(base["revision_id"][1:]) + 1)
        asset = session.script_task(revision=target, base=base, task_message=request,
            beat=request["payload"]["beat"], goal=request["payload"]["goal"],
            result_message=store.get_message(request["causation_id"]))
        store.ack("script", request["message_id"], "completed", result={"script":ref(asset)})
        return asset
    except TraceError:
        raise
    except Exception as exc:
        store.ack("script", request["message_id"], "blocked", result={"reason":str(exc)})
        raise


def continue_run(session, local_only):
    original = read_json(session.index)
    with trace("collaboration.continuation", kind="task", links=[{"trace_id":original["trace_id"],"relation":"continues"}],
               attributes={"correlation_id":session.run_id}, inputs={"local_only":local_only}) as root:
        ready = [m for m in matching(session.store, session.run_id, "production.result", "r2")
                 if m["payload"].get("production")]
        if ready and ready[-1]["deliveries"].get("script") == "completed":
            root.set_output({"existing_delivery":ref(open_ref(session.store, ready[-1]["payload"]["production"]))})
            return
        previous, result = first_delivery(session)
        from pianchang.collab_worker import consume
        for notice in matching(session.store, session.run_id, "production.result", "r1"):
            if notice["deliveries"].get("script") == "pending":
                observation = consume(session, "script", notice["message_id"])
                if observation["status"] != "observed":
                    raise ValueError("剧本端无法核对制作回执：" + str(observation))
                stage = "剧本端接收首版交付" if "production" in notice["payload"] else "剧本端接收中断通知"
                session.stage(stage, observation["trace_id"], "completed", "r1", "已核对并保存制作消息及版本绑定")
        request = prepare_change(session, previous, result)
        plan = open_ref(session.store, request["payload"]["proposal"])["content"]
        session.write(change_proposal=plan)
        if local_only:
            session.write(status="waiting_for_provider", continuation_trace_id=root.trace_id)
            root.set_output({"base_production":ref(previous), "pending_change":request["message_id"], "plan":plan})
            return
        session.write(status="running", continuation_trace_id=root.trace_id)
        try:
            target = consume_change(session, request)
            commission = session.commission(target, None, previous, max(1,len(plan["direct_consumers"])))
            delivered = [m for m in session.store.messages(causation_id=commission["message_id"], message_type="production.result")
                         if m["payload"].get("production")]
            if not delivered:
                _, message = session.worker(commission["message_id"])
            else:
                message = delivered[0]
            if message["deliveries"].get("script") == "pending":
                observation = consume(session, "script", message["message_id"])
                if observation["status"] != "observed":
                    raise ValueError("剧本端未确认新版交付回执：" + str(observation))
                session.stage("剧本端接收最终回执", observation["trace_id"], "completed",
                              target["revision_id"], "已核对新版制作结果和验收状态")
            session.write(status="completed", continuation_trace_id=root.trace_id)
            root.set_output({"production_result":message["payload"],"stage_traces":read_json(session.index)["stages"]})
        except TraceError:
            raise
        except Exception as exc:
            session.write(status="blocked", error=str(exc))
            raise


@traced("cli.collaboration.continue", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description="恢复指定协作任务的本地预览，或接续已授权的改稿与制作")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--directory", type=Path, default=HOME_DIR)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true", help="只核实本地素材、导出、准备改稿委托")
    mode.add_argument("--resume", action="store_true", help="模型账户可用后执行待办改稿和局部制作")
    args = parser.parse_args(argv)
    continue_run(Collaboration(args.directory,args.run_id), args.prepare)


if __name__ == "__main__":
    main()
