"""Story-scoped durable conversations connecting Pi to the workbench."""
import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from pianchang.agent_loop import Tool
from pianchang.app_service import now
from pianchang.bailian_text import BailianTextClient
from pianchang.collab_store import CollabStore
from pianchang.story_spec import digest_json
from trace_system import TraceError, current_context, event, traced, use_context
from .repository import SessionRepository
from .runtime import PiLoop, bind_sessions


class PiSessions:
    @traced("pi.sessions.service_open",kind="task",capture_inputs=False,capture_output=False)
    def __init__(self, service):
        self.service=service;self.repo=SessionRepository(service.root/"outputs/pi")
        self.executor=ThreadPoolExecutor(max_workers=4,thread_name_prefix="pi-session")
        self.lock=threading.RLock();self.repo.recover()

    @traced("pi.sessions.resolve",kind="tool",capture_inputs=False)
    def resolve(self, scope, role):
        if role not in {"script","video"}:raise ValueError("Agent role must be script or video")
        with self.service.tracer.read_batch():
            if set(scope)=={"story"} and re.fullmatch(r"[a-f0-9]{32}",scope["story"]):
                with self.service.lock:
                    source=next((dict(j) for j in self.service.state["jobs"] if j["id"]==scope["story"] and j["kind"] in {"story","studio_script"}),None)
                if source is None:raise KeyError("故事记录不存在")
                detail=self.service._story_record(source)
                story="story-"+scope["story"];source_id=scope["story"];pid=detail.get("project_id")
            elif set(scope)=={"project"} and re.fullmatch(r"[a-f0-9]{16}",scope["project"]):
                from pianchang.studio_assets import studio_project_for_video
                view=studio_project_for_video(self.service,scope["project"])
                with self.service.lock:
                    source_id=view["run_id"] if any(j["id"]==view["run_id"] and j["kind"] in {"story","studio_script"} for j in self.service.state["jobs"]) else None
                story=view["story_id"];pid=scope["project"]
            else:raise ValueError("请选择一个有效故事或工程")
            existing=self.repo.find(story,role)
        if existing and (not source_id or existing["source_job"]) and (not pid or existing["project_id"]==pid):return existing
        # Creation/linking is a real metadata write: keep its trace durable before
        # the repository write rather than placing it in a read-only batch.
        return self.repo.ensure(story,role,source_job=source_id,project_id=pid)

    @traced("pi.sessions.view",kind="tool",capture_inputs=False)
    def view(self, scope, role):
        return self.resolved_view(self.resolve(scope,role))

    @traced("pi.sessions.resolved_view",kind="tool",capture_inputs=False)
    def resolved_view(self, session):
        """Read a session already matched to its story and project in this request."""
        messages=self.repo.messages(session["id"])
        job_ids=[m["job_id"] for m in messages if m.get("job_id")]
        target=session.get("active_job") or (job_ids[-1] if job_ids else None)
        with self.service.lock:
            job=next((j for j in self.service.state["jobs"] if j["id"]==target and j.get("session_id")==session["id"]),None)
            execution={k:job.get(k) for k in ("id","status","message","error","trace_id","updated_at","trace_incomplete")} if job else None
            if execution:execution["agent_progress"]={k:v for k,v in (job.get("agent_progress") or {}).items() if k in {"phase","type","step","summary","toolName"}}
        from .live import session_sources
        live=session_sources(self.service,session)
        if live["execution"]:
            progress=(execution or {}).get("agent_progress") if (execution or {}).get("id")==live["execution"]["id"] else None
            execution=live["execution"]
            if progress:execution["agent_progress"]=progress
        return {"session":{k:v for k,v in session.items() if k!="session_file"},"messages":messages,"execution":execution,
                "backend":"pi","capabilities":{"persistent_history":True,"compaction":True,"steering":True,"operation_recovery":True,"permission_modes":True,"live_events":True}}

    def _options(self, session, job=None):
        session=self.repo.get(session["id"])
        if not session["active_job"] or session["active_job"]==(job or {}).get("id"):
            self.repo.reconcile(session["id"],exclude_ids=[(job or {}).get("_pi_message_id")])
        def ready(data):
            self.repo.update(session["id"],session_file=data["sessionFile"],pi_session_id=data["sessionId"])
            if job:self.service._update(job,pi_session_id=data["sessionId"],backend="pi")
        def on_event(data):
            message=data.get("message") or {}
            if message.get("role")=="user":
                content=message.get("content","");text=content if isinstance(content,str) else json.dumps(content,ensure_ascii=False)
                for entry in self.repo.messages(session["id"]):
                    if "[message_id="+entry["id"]+"]" in text:self.repo.delivered(entry["id"])
        def inbox():
            output=[]
            if job and job.get("cancel_requested"):output.append({"type":"stop"})
            for message in self.repo.messages(session["id"]):
                if message["status"]=="queued" and message["job_id"]!=(job or {}).get("id"):
                    self.repo.delivered(message["id"],"dispatching",(job or {}).get("id"))
                    output.append({"type":"steer","text":"[message_id="+message["id"]+"]\n"+message["text"]})
            return output
        initial=next((m for m in self.repo.messages(session["id"]) if m["id"]==(job or {}).get("id") and m["role"]=="user" and m["status"]!="delivered"),None) if (job or {}).get("kind")=="studio_video" else None
        routes = getattr(self.service, "model_routes", None)
        return {"session_file":session["session_file"],"on_ready":ready,"on_event":on_event,"inbox":inbox,
                "model_route":routes.resolve(session["role"] + ".decision") if routes is not None else None,
                "credential_env_names":routes.credential_env_names if routes is not None else (),
                "initial_message":"[message_id="+initial["id"]+"]\n"+initial["text"] if initial else None,
                "cancelled":lambda:bool((job or {}).get("cancel_requested")),
                "session_dir":str(self.repo.directory/"sessions"/session["story_id"]/session["role"]),
                "execution_lock":str(self.repo.directory/"locks"/(session["id"]+".lock"))}

    @contextmanager
    def binding(self, session, job=None):
        owned=False
        with self.lock:
            active=self.repo.get(session["id"])["active_job"]
            if job and (job.get("cancel_requested") or job.get("trace_incomplete")):raise ValueError("已停止后续制作动作，原委托和未确认记录保留。")
            if job and active and active!=job["id"]:raise ValueError("当前视频 Agent 会话已有执行任务，补充要求应投递到同一会话。")
            if job and not active:
                self.repo.update(session["id"],active_job=job["id"]);owned=True
            if job and job.get("kind")=="studio_video" and job.get("session_id")!=session["id"]:
                self.service._update(job,session_id=session["id"],backend="pi")
        def resolve(role):
            target=self.repo.ensure(session["story_id"],role,session["source_job"],session["project_id"])
            options=self._options(target,job if role==session["role"] else None)
            if job:options["cancelled"]=lambda:bool(job.get("cancel_requested"))
            if job and role==session["role"]:
                original=options.get("on_event")
                def worker_event(data):
                    if original:original(data)
                    typ=data.get("type")
                    if typ=="assistant_text":return
                    progress={k:v for k,v in data.items() if k in {"type","toolName","success","reason","error"}}
                    self.service._update(job,worker_trace_id=(current_context() or {}).get("trace_id"),
                        agent_progress={"phase":"production",**progress},message="制作循环执行中"+(" · "+data["toolName"] if data.get("toolName") else ""))
                options["on_event"]=worker_event
            return options
        try:
            with bind_sessions(resolve):yield
        finally:
            if owned and (job or {}).get("kind")!="studio_video":
                self.repo.update(session["id"],active_job=None)
                pending=[m for m in self.repo.messages(session["id"]) if m["status"]=="queued"]
                if pending and not (job or {}).get("cancel_requested"):self.resume_queued(session,pending[0])

    @traced("pi.video.submit_production",kind="task",capture_inputs=False)
    def submit_production(self,pid,body,enqueue):
        with self.lock:
            session=self.resolve({"project":pid},"video")
            active=next((j for j in self.service.state["jobs"] if j["id"]==session["active_job"] and j["status"] in {"queued","running"}),None)
            if active and active["kind"]!="studio_video":raise ValueError("视频 Agent 正在处理当前会话。请在对话中补充要求，处理完成后再确认拍摄计划。")
            result=enqueue()
            job=next(j for j in self.service.state["jobs"] if j["id"]==result["id"])
            if job["status"] not in {"queued","running"}:return self._public_job(job)
            try:
                self.repo.update(session["id"],active_job=job["id"])
                self.repo.append(session["id"],"user","确认拍摄计划：\n"+json.dumps(body,ensure_ascii=False,sort_keys=True)+
                    "\n在此冻结范围与额度内自主制作、验收与修复；所有必过项通过后交付人工预览。",
                    message_id=job["id"],job_id=job["id"],trace_id=job["trace_id"])
                self.service._update(job,session_id=session["id"],backend="pi")
            except Exception:
                # A worker waiting for this session lock must stop before side effects.
                with self.service.lock:job["cancel_requested"]=True;self.service._save()
                raise
            return self._public_job(job)

    @traced("pi.video.release_production",kind="task",capture_inputs=False)
    def release_production(self,job):
        session=self.repo.get(job["session_id"]) if job.get("session_id") else self.resolve({"project":job["project_id"]},"video")
        with self.lock:
            self.repo.append(session["id"],"assistant",job.get("message") or "本次执行已结束，验收仍以实际报告为准。",
                message_id=uuid.uuid5(uuid.NAMESPACE_URL,job["id"]+"/"+str(job.get("trace_id"))+"/production-outcome").hex,
                status="delivered",job_id=job["id"],trace_id=job.get("trace_id"))
            if self.repo.get(session["id"])["active_job"]!=job["id"]:return
            self.repo.reconcile(session["id"])
            approval=next((m for m in self.repo.messages(session["id"]) if m["id"]==job["id"]),None)
            if approval and approval["status"]=="queued":self.repo.delivered(job["id"],status="not_executed")
            self.repo.update(session["id"],active_job=None)
            pending=[m for m in self.repo.messages(session["id"]) if m["status"]=="queued" and m["id"]!=job["id"]]
            if pending and not job.get("cancel_requested") and not job.get("trace_incomplete"):self.resume_queued(session,pending[0])

    @traced("pi.messages.submit",kind="task",capture_inputs=False)
    def submit(self, scope, role, text, message_id=None, *, sender="user", bus_message=None, mode="work"):
        if not isinstance(text,str) or not 1<=len(text.strip())<=6000:raise ValueError("消息须为1～6000字")
        if mode not in {"work","inspect"}:raise ValueError("请选择推进制作或仅核对答疑")
        if message_id is not None and not re.fullmatch(r"[a-f0-9]{32}",message_id):raise ValueError("消息标识无效")
        with self.lock:
            session=self.resolve(scope,role)
            existing=next((m for m in self.repo.messages(session["id"]) if m["id"]==message_id),None)
            if existing:
                if existing["text"]!=text or existing["mode"]!=mode:raise ValueError("同一消息标识不可更换内容或权限")
                if existing["status"]!="queued" or session["active_job"]:
                    return {"message":existing,"session_id":session["id"],"duplicate":True}
            active=next((j for j in self.service.state["jobs"] if j["id"]==session["active_job"] and j["status"] in {"running","queued"}),None)
            if active:
                message=self.repo.append(session["id"],sender,text,message_id=message_id,job_id=None,mode=mode,trace_id=(current_context() or {}).get("trace_id"))
                return {"message":message,"session_id":session["id"],"queued":True,"job":self._public_job(active)}
            job_id=uuid.uuid4().hex;message_id=message_id or uuid.uuid4().hex
            parent=current_context();links=[{**parent,"relation":"submitted_by"}] if parent else []
            root=self.service.tracer.start_span("agent.task.pi_"+role,kind="task",new_trace=True,
                inputs={"scope":scope,"role":role,"text":text,"message_id":message_id,"sender":sender,"mode":mode},
                attributes={"session_id":session["id"],"story_id":session["story_id"]},links=links)
            message=self.repo.append(session["id"],sender,text,message_id=message_id,job_id=job_id,trace_id=root.trace_id,mode=mode)
            if existing:self.repo.delivered(message_id,"queued",job_id)
            job={"id":job_id,"kind":"pi_"+role,"project_id":session["project_id"],"story_job_id":session["source_job"],
                "status":"queued","message":"Pi 会话已接收消息","created_at":now(),"updated_at":now(),
                "trace_id":root.trace_id,"_trace_context":root.context,"session_id":session["id"],"backend":"pi",
                "_pi_message_id":message_id,"_pi_bus_message":bus_message,"_pi_mode":mode}
            with self.service.lock:
                self.service.state["jobs"].append(job);self.service._save()
            self.repo.update(session["id"],active_job=job_id)
            self.executor.submit(self._execute_bound,session,job,text)
            return {"message":message,"session_id":session["id"],"job":self._public_job(job)}

    def _public_job(self, job):return {k:v for k,v in job.items() if not k.startswith("_")}

    def _execute_bound(self,session,job,text):
        with use_context(job["_trace_context"],tracer=self.service.tracer):self.execute(session,job,text)

    @traced("pi.messages.stop",kind="tool",capture_inputs=False)
    def stop(self, scope, role):
        session=self.resolve(scope,role)
        job=next((j for j in self.service.state["jobs"] if j["id"]==session["active_job"]),None)
        if job:self.service._update(job,cancel_requested=True,message="已请求停止后续动作；已提交的云端任务保留。")
        return {"requested":bool(job)}

    @traced("pi.collaboration.request",kind="tool",capture_inputs=False)
    def ask(self, session, target_role, request_text):
        if target_role==session["role"] or target_role not in {"script","video"}:raise ValueError("请选择另一个 Agent")
        store=CollabStore(self.service.root/"outputs/collaborations/assets.sqlite3")
        pins=[]
        for suffix in ("/script","/source-project"):
            if store.revisions(session["story_id"]+suffix):
                asset=store.get_asset(session["story_id"]+suffix)
                pins.append({k:asset[k] for k in ("asset_id","revision_id","sha256")})
        active=self.repo.get(session["id"])["active_job"] or session["id"]
        active_job=next((j for j in self.service.state["jobs"] if j["id"]==active),{})
        parent=store.get_message(active_job["_pi_bus_message"]) if active_job.get("_pi_bus_message") else None
        origin=(parent or {}).get("payload",{}).get("origin_job",active)
        depth=(parent or {}).get("payload",{}).get("depth",0)+1
        prior_requests=[m for m in store.messages(message_type="agent.conversation_requested")
                        if m["story_id"]==session["story_id"] and m["payload"].get("origin_job")==origin]
        if depth>3 or len(prior_requests)>=8:raise ValueError("本轮跨Agent协商达到3层/8个请求上限；保留具体未决问题，不能无限互相委派。")
        request_id=uuid.uuid5(uuid.NAMESPACE_URL,"pi-collaboration/"+active+"/"+digest_json([target_role,request_text,pins])).hex
        msg=store.send("agent.conversation_requested",sender=session["role"],recipient=target_role,message_id=request_id,
            story_id=session["story_id"],correlation_id=session["id"],payload={"request":request_text,"pins":pins,"origin_job":origin,"depth":depth})
        scope={"story":session["source_job"]} if session["source_job"] else {"project":session["project_id"]}
        result=self.submit(scope,target_role,"来自 "+session["role"]+" Agent 的请求（绑定版本见协作记录）：\n"+request_text,
            message_id=msg["message_id"],sender="agent",bus_message=msg["message_id"],mode=active_job.get("_pi_mode","work"))
        return {"request_id":msg["message_id"],"target_session_id":result.get("session_id"),"pins":pins,"status":"accepted"}

    def collaboration_tool(self, session):
        return Tool("ask_other_agent","向同一故事的另一Agent提出请求；固定当前版本并持久投递，目标会话自主处理。",
            {"type":"object","properties":{"target_role":{"type":"string","enum":["script","video"]},
             "request_text":{"type":"string","minLength":1,"maxLength":4000}},"required":["target_role","request_text"]},
            lambda target_role,request_text:self.ask(session,target_role,request_text),effect="write")

    @traced("pi.collaboration.inbox",kind="tool",capture_inputs=False)
    def collaboration_inbox(self, session):
        store=CollabStore(self.service.root/"outputs/collaborations/assets.sqlite3")
        requests=[];responses=[]
        for message in store.inbox(session["role"]):
            if message["story_id"]==session["story_id"] and message["message_type"]=="agent.conversation_response":
                responses.append({"message_id":message["message_id"],"request_id":message["causation_id"],"sender":message["sender"],"payload":message["payload"]})
                continue
            if (message["story_id"]!=session["story_id"] or message["message_type"]!="agent.conversation_requested"
                    or message["deliveries"].get(session["role"])=="completed"):continue
            assets=[]
            for pin in message["payload"].get("pins",[]):
                asset=store.get_asset(pin["asset_id"],pin["revision_id"])
                if asset["story_id"]!=session["story_id"] or asset["sha256"]!=pin["sha256"]:raise ValueError("协作请求的版本证据不匹配")
                assets.append({"pin":pin,"content":asset["content"]})
            requests.append({"request_id":message["message_id"],"sender":message["sender"],"payload":message["payload"],"bound_assets":assets})
        return {"requests":requests,"responses":responses,"rule":"按请求绑定版本回应，不能用另一个故事或后来版本代替。"}

    @traced("pi.collaboration.answer",kind="tool",capture_inputs=False)
    def answer_collaboration(self,session,request_id,reply_text):
        if request_id not in {r["request_id"] for r in self.collaboration_inbox(session)["requests"]}:raise ValueError("请求不在本故事的待办协作中")
        current=next((j for j in self.service.state["jobs"] if j["id"]==self.repo.get(session["id"])["active_job"]),None)
        if not current:raise ValueError("协作回应须属于实际执行任务")
        return self.reply_to_agent(session,{**current,"_pi_bus_message":request_id},
            {"summary":reply_text,"task_complete":False,"scope":"agent_response_not_media_acceptance"})

    def recovery_collaboration_tools(self,session):
        return [Tool("read_collaboration_requests","读取同一故事尚未回应的协作请求及其固定版本，重启后仍保留。",{"type":"object","properties":{}},lambda:self.collaboration_inbox(session)),
            Tool("reply_collaboration_request","依据已读取的固定版本回应指定协作请求。不能声明媒体验收通过。",
                {"type":"object","properties":{"request_id":{"type":"string","minLength":32,"maxLength":32},
                 "reply_text":{"type":"string","minLength":1,"maxLength":4000}},"required":["request_id","reply_text"]},
                lambda request_id,reply_text:self.answer_collaboration(session,request_id,reply_text),effect="write")]

    @traced("pi.sessions.execute",kind="task",capture_inputs=False,capture_output=False)
    def execute(self, session, job, text):
        with use_context(job["_trace_context"],tracer=self.service.tracer):
            try:
                self.service._update(job,status="running",message="Pi 正在读取原会话与实际制作状态")
                self.repo.delivered(job["_pi_message_id"],"dispatching",job["id"])
                if session["role"]=="script":result=self.run_script(session,job,"[message_id="+job["_pi_message_id"]+"]\n"+text)
                else:result=self.run_video(session,job,"[message_id="+job["_pi_message_id"]+"]\n"+text)
                message=result.get("assistant_text") or result.get("summary") or result.get("loop",{}).get("summary") or "本轮已结束，制作状态见验收记录。"
                self.repo.append(session["id"],"assistant",message,status="delivered",job_id=job["id"],trace_id=job["trace_id"])
                self.service._update(job,status="succeeded" if result.get("task_complete") or result.get("turn_complete") else "interrupted",message=message,result=result)
                if result.get("project_id"):self.repo.update(session["id"],project_id=result["project_id"])
                # A request may have entered as native steering while this job was
                # already running. Its durable message ID retains the bus causation.
                store=CollabStore(self.service.root/"outputs/collaborations/assets.sqlite3")
                request_ids={m["message_id"] for m in store.messages(message_type="agent.conversation_requested")
                    if m["story_id"]==session["story_id"] and m["recipient"]==session["role"]}
                delivered={m["id"] for m in self.repo.messages(session["id"]) if m["role"]=="agent"
                    and m["status"]=="delivered" and m["job_id"]==job["id"]}
                if job.get("_pi_bus_message"):delivered.add(job["_pi_bus_message"])
                for request_id in sorted(request_ids & delivered):
                    self.reply_to_agent(session,{**job,"_pi_bus_message":request_id},result)
            except TraceError as exc:
                with self.service.lock:
                    job.update(status="failed",trace_incomplete=True,error=str(exc),message="Trace 写入失败；后续操作已停止。")
                    self.service._save()
            except Exception as exc:
                event("pi.session.execution_error",{"type":type(exc).__name__,"error":str(exc)},kind="error")
                self.service._update(job,status="failed",error=str(exc),message=str(exc))
                self.repo.append(session["id"],"assistant","本轮执行失败："+str(exc)+"\n已有会话和执行证据已保留；未通过的制作不能进入人工预览。",
                    message_id=uuid.uuid5(uuid.NAMESPACE_URL,job["id"]+"/execution-error").hex,
                    status="delivered",job_id=job["id"],trace_id=job["trace_id"])
            finally:
                with self.lock:
                    if self.repo.get(session["id"])["active_job"]==job["id"]:
                        self.repo.update(session["id"],active_job=None)
                        queued=[m for m in self.repo.messages(session["id"]) if m["status"]=="queued" and m["id"]!=job["_pi_message_id"]]
                        if queued and not job.get("trace_incomplete") and not job.get("cancel_requested"):
                            # Requeue delivery as a new execution, retaining the original message ID.
                            self.resume_queued(session,queued[0])
                if job.get("commission_id") and job.get("status") in {"failed","interrupted"} and not job.get("trace_incomplete") and not job.get("cancel_requested"):
                    from pianchang.video_autonomy import enqueue_recovery
                    self.service.executor.submit(enqueue_recovery,self.service)

    @traced("pi.sessions.resume_queued",kind="task",capture_inputs=False)
    def resume_queued(self, session, message):
        scope={"story":session["source_job"]} if session["source_job"] else {"project":session["project_id"]}
        bus_message=None
        if message["role"]=="agent":
            store=CollabStore(self.service.root/"outputs/collaborations/assets.sqlite3")
            bus_message=next((m["message_id"] for m in store.messages(message_type="agent.conversation_requested")
                if m["message_id"]==message["id"] and m["story_id"]==session["story_id"] and m["recipient"]==session["role"]),None)
        return self.submit(scope,session["role"],message["text"],message["id"],sender=message["role"],bus_message=bus_message,mode=message.get("mode","work"))

    @traced("pi.collaboration.reply",kind="tool",capture_inputs=False)
    def reply_to_agent(self, session, job, result):
        store=CollabStore(self.service.root/"outputs/collaborations/assets.sqlite3")
        source=store.get_message(job["_pi_bus_message"])
        if source["story_id"]!=session["story_id"] or source.get("recipient")!=session["role"]:raise ValueError("协作请求归属不匹配")
        existing=store.messages(causation_id=source["message_id"],message_type="agent.conversation_response")
        if store.claim(session["role"],message_id=source["message_id"]):
            store.ack(session["role"],source["message_id"],"accepted")
        reply=existing[-1] if existing else store.send("agent.conversation_response",sender=session["role"],recipient=source["sender"],story_id=session["story_id"],
            category="response",message_id=uuid.uuid5(uuid.NAMESPACE_URL,source["message_id"]+"/pi-response").hex,
            causation_id=source["message_id"],correlation_id=source["correlation_id"],payload={"summary":result.get("summary"),"trace_id":job["trace_id"],"task_complete":result.get("task_complete",False),"scope":result.get("scope","bound_agent_task")})
        if source["deliveries"].get(session["role"])!="completed":
            store.ack(session["role"],source["message_id"],"completed",recovery=source["deliveries"].get(session["role"]) in {"unknown","blocked"},result={"response_id":reply["message_id"]})
        target=self.repo.ensure(session["story_id"],source["sender"],session["source_job"],session["project_id"])
        self.repo.append(target["id"],"agent",json.dumps({"type":"agent_response","message_id":reply["message_id"],"payload":reply["payload"]},ensure_ascii=False),message_id=reply["message_id"],mode="inspect")
        target=self.repo.get(target["id"])
        if not target["active_job"]:
            pending=[m for m in self.repo.messages(target["id"]) if m["status"]=="queued"]
            if pending:self.resume_queued(target,pending[0])
        return {"response_id":reply["message_id"],"request_id":source["message_id"]}

    @traced("pi.story.initial",kind="agent",capture_inputs=False)
    def run_initial(self, job, body):
        session=self.resolve({"story":job["id"]},"script")
        self.repo.update(session["id"],active_job=job["id"])
        text=body.get("idea") or "核验并原样发布导入剧本，接续制作规格。"
        if body.get("script"):
            from pianchang.app_service import write_json
            write_json(self.service.root/"outputs/pi/workspaces"/session["story_id"]/"imported-script.json",body["script"])
        self.repo.append(session["id"],"user",text,message_id=job["id"],job_id=job["id"],status="dispatching",trace_id=job["trace_id"])
        try:
            result=self.run_script(session,job,"[message_id="+job["id"]+"]\n"+text)
            self.repo.append(session["id"],"assistant",result.get("assistant_text") or result["summary"],status="delivered",job_id=job["id"],trace_id=job["trace_id"])
            if result.get("project_id"):self.repo.update(session["id"],project_id=result["project_id"])
            return result
        finally:self.repo.update(session["id"],active_job=None)

    @traced("pi.story.run",kind="agent",capture_inputs=False)
    def run_script(self, session, job, text):
        from .script_tools import StoryTools
        workspace=StoryTools(self.service,session,job,text,collaborator=self.collaboration_tool(session))
        work=self.service.root/"outputs/pi/runs"/job["id"]
        tools=workspace.tools+self.recovery_collaboration_tools(session)
        if job.get("_pi_mode")=="inspect":tools=[t for t in tools if t.effect=="read" or t.name in {"ask_other_agent","reply_collaboration_request"}]
        loop=PiLoop(BailianTextClient(self.service.text_config),tools,work,
            snapshot=workspace.snapshot,finish_check=workspace.finish_check,
            system_prompt="你是剧本 Agent。用户要求继续运行时，先read_story_context和read_production_spec，复用已经发布的剧本，依据真实校验问题定向修复分镜，再publish_production_spec。不得伪造参考。普通咨询则回答问题，不擅自改稿。",
            progress=lambda p:self.service._update(job,agent_progress=p,message=p.get("summary") or "Pi 剧本会话执行中"),
            **self._options(session,job))
        result=loop.run(text,max_steps=30)
        return {**result,**(workspace.result or {}),"assistant_text":loop.record.get("assistant_text"),"backend":"pi"}

    @traced("pi.video.run",kind="agent",capture_inputs=False)
    def run_video(self, session, job, text):
        from pianchang.studio_flow import studio_flow
        from .context import video_observation,reference_observation
        from .diagnostics import production_diagnostics
        from .input_tools import commission_reference_inputs
        def inputs():return commission_reference_inputs(self.service,session,lambda:loop.before_model({"auxiliary_model":"reference_board_review"}))
        def diagnostics():return production_diagnostics(self.service,session)
        def state():
            return studio_flow(self.service,session["project_id"]) if session["project_id"] else {"blocked":"故事还没有可制作工程，须由剧本Agent先完成制作规格。"}
        from pianchang.video_execution import current_commission
        def commission():
            return current_commission(self.service,session["project_id"]) if session["project_id"] else {"commission_id":None,"resumable":False,"blockers":["尚无制作工程"]}
        requested=[]
        def request_continuation(commission_id):
            current=commission()
            if current.get("commission_id")!=commission_id:raise ValueError("当前制作委托已改变，请重新读取，不能恢复旧委托。")
            if not current["resumable"]:return {"accepted":False,"reason":"；".join(current["blockers"]) or "当前委托正在执行或已完成。"}
            other=current.get("last_execution") or {}
            if other.get("status") in {"queued","running"} and other.get("id")!=job["id"]:raise ValueError("此委托已有会话执行，不能并行恢复。")
            observed=diagnostics()
            if observed.get("preflight_ready") is False:
                return {"accepted":False,"reason":"冻结输入尚未通过模型能力校验，原样重试不能修复。先依据具体失败项调整兼容方案；不得丢弃必需参考或绕过验收。",
                        "diagnostics":observed}
            requested.append(current)
            self.service._update(job,commission_id=commission_id,commission_job_id=current["job_id"])
            return {"accepted":True,"execution_status":"pending_handoff","production_started":False,"original_job_id":current["job_id"],"commission":commission_id,"limits":current["limits"],"usage":current["usage"],
                    "rule":"本轮会话落盘后在同一Pi会话接续原冻结委托；沿用原验收门禁和额度。"}
        def read_issue(subject_id,purpose):
            value=state()
            item=next((i for i in value.get("asset_work",{}).get("items",[]) if i.get("subject_id")==subject_id and i.get("purpose")==purpose),None)
            if item is None:raise ValueError("当前故事没有这个参考项")
            return item
        tool=Tool("read_video_state","读取最新制作状态和所有当前阻碍；新观察优先于历史对话，保留完整结果的证据哈希。",{"type":"object","properties":{}},state,context_result=video_observation)
        issue_tool=Tool("read_reference_issue","按角色/场景/道具标识和用途读取当前参考的全部验收检查、错误及证据引用。",
            {"type":"object","properties":{"subject_id":{"type":"string"},"purpose":{"type":"string"}},"required":["subject_id","purpose"]},
            read_issue,context_result=reference_observation)
        continuation=Tool("continue_authorized_production","先read_current_commission。仅在用户要求继续或修复时，用当前commission_id接续冻结委托，不能新建范围或重置额度。",
            {"type":"object","properties":{"commission_id":{"type":"string","pattern":"^[a-f0-9]{32}$"}},"required":["commission_id"]},request_continuation,effect="write")
        commission_tool=Tool("read_current_commission","读取同一故事的唯一当前制作委托、冻结范围、限制、已预留额度和具体恢复阻碍。",{"type":"object","properties":{}},commission)
        diagnostic_tool=Tool("read_production_diagnostics","读取当前冻结委托的真实失败任务、每镜头图片数量、模型兼容校验与固定版本。只读，不重放生成。",{"type":"object","properties":{}},diagnostics)
        tools=[tool,issue_tool,commission_tool,diagnostic_tool,self.collaboration_tool(session)]+self.recovery_collaboration_tools(session)
        input_schema={"type":"object","properties":{"shot_id":{"type":"string"}},"required":["shot_id"]}
        tools.append(Tool("read_reference_inputs","读取当前冻结镜头的输入方案、已审核参考板及失败证据。",input_schema,lambda shot_id:inputs().read(shot_id)))
        if job.get("_pi_mode")!="inspect":
            tools.extend([continuation,Tool("prepare_reference_inputs","修复参考数量超限：保留全部固定参考，合成有来源区域映射的参考板并独立核验。不修改剧本；失败换策略后继续其他镜头。",
                {**input_schema,"properties":{**input_schema["properties"],"strategy":{"type":"string","enum":["identity_first","balanced_boards"]}}},
                lambda shot_id,strategy="identity_first":inputs().prepare(shot_id,strategy),effect="write")])
        loop=PiLoop(BailianTextClient(self.service.text_config),tools,self.service.root/"outputs/pi/runs"/job["id"],
            progress=lambda p:self.service._update(job,agent_progress=p,message=p.get("summary") or "Pi 视频会话执行中"),
            system_prompt="你是本故事唯一的视频制作 Agent，另一角色是剧本 Agent。任务要求、继续与修复都属于这个持续会话；read_current_commission是当前冻结委托和剩余额度的依据，不得跳回较早失败委托。用户只问问题时正常回答；要求继续或修复时先读状态和当前委托，在允许恢复时调用continue_authorized_production，不能只口头答应或要求再点另一个Agent启动按钮。没有已确认计划、额度耗尽或版本改变时说明实际阻碍，不得创建新预算。最新read_video_state优先于历史回复。给出具体失败项与证据。responded仅结束本轮答复；视频交付必须所有镜头及衔接必过项通过并发布。",
            snapshot=lambda:{"role":"video","project_id":session["project_id"],"policy":"沿用原委托范围与验收门禁；新制作使用页面已显示的拍摄计划。问题诊断应引用实际失败任务和每镜头校验，不能从未发布推断生成未启动。pending_handoff只表示等待交接，不代表修复成功。用户询问原因及如何解决时先答疑，不擅自接续制作。参考超限时先prepare_reference_inputs；有已核验输入后再接续。某镜头失败要处理其余可编排镜头，保留源引用不丢弃。",
                             "production_diagnostics":diagnostics()},
            finish_check=lambda:{"complete":False,"summary":"本轮对话不代表视频制作通过"},**self._options(session,job))
        result=loop.run(text,max_steps=12)
        if requested and not job.get("cancel_requested"):
            from pianchang.studio_jobs import resume_video_job
            current=commission()
            if current.get("commission_id")!=requested[-1]["commission_id"] or not current["resumable"]:
                raise ValueError("会话结束前当前委托或授权状态已改变，停止恢复，请重新核对。")
            previous=next(j for j in self.service.state["jobs"] if j["id"]==current["job_id"])
            self.repo.append(session["id"],"assistant","已接受接续要求，正在交接至原制作循环。后续模型、工具、验收及修复进展会显示在本对话中；交接不表示制作已完成。",
                message_id=uuid.uuid5(uuid.NAMESPACE_URL,job["id"]+"/production-handoff").hex,
                status="delivered",job_id=job["id"],trace_id=job.get("trace_id"))
            try:
                with self.binding(session,job):
                    continued=resume_video_job(self.service,previous)
            except TraceError:raise
            except Exception as exc:
                event("pi.video.continuation_failed",{"commission_id":current["commission_id"],"error":str(exc),"conversation":result},kind="error")
                self.service._update(job,continuation_error=str(exc))
                message="Pi 已处理本轮消息，但接续制作失败："+str(exc)+"\n交接被接受不代表制作成功。原委托、额度和失败证据已保留，验收仍未通过。"
                return {"status":"needs_input","turn_complete":False,"task_complete":False,"summary":message,"assistant_text":message,
                        "conversation":result,"continuation_error":str(exc),"backend":"pi"}
            return {**continued,"conversation":result,"backend":"pi"}
        return {**result,"assistant_text":loop.record.get("assistant_text")}

    @traced("pi.sessions.close",kind="tool",capture_inputs=False)
    def close(self, wait=True):self.executor.shutdown(wait=wait,cancel_futures=not wait)


@traced("pi.sessions.initialize",kind="task",capture_inputs=False,capture_output=False)
def get_sessions(service):
    with service.lock:
        if not hasattr(service,"_pi_sessions"):service._pi_sessions=PiSessions(service)
        return service._pi_sessions
