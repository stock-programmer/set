/* Persistent story conversations. Execution and acceptance stay on the server. */
const agentChatViews=new Map();
let agentChatCurrent=null;
let agentLiveActive=null;
function agentChatState(scope) {
  const key=JSON.stringify(scope);
  if(!agentChatViews.has(key))agentChatViews.set(key,{role:"script",mode:"work",drafts:{script:"",video:""},loading:false,data:null,error:null});
  return agentChatViews.get(key);
}
function primeAgentChat(scope,conversations){
  const view=agentChatState(scope);view.snapshots=conversations;view.snapshotsAt=Date.now();
  if(conversations?.[view.role]){view.data=conversations[view.role];view.lastRead=Date.now();}
}
function renderAgentChat(scope,{phase,preferredRole,selectRole=false}={}) {
  const view=agentChatState(scope),host=node("section","panel agent-chat");host.id="agent-chat";
  if(phase && view.phase!==phase){view.phase=phase;if(preferredRole && view.role!==preferredRole){view.role=preferredRole;view.data=null;}}
  if(selectRole&&preferredRole&&view.role!==preferredRole){view.role=preferredRole;view.data=null;}
  if(!view.data&&view.snapshots?.[view.role]&&Date.now()-view.snapshotsAt<2000){view.data=view.snapshots[view.role];view.lastRead=view.snapshotsAt;}
  agentChatCurrent={scope,view,host};
  append(host,node("span","section-label","持续对话 · Pi"),node("h2","","与制作 Agent 对话"),
    node("p","small-print","这里的消息会进入当前故事的持久会话。可继续中断工作、补充要求或询问原因；制作完成仍以实际验收为准。"));
  const tabs=node("div","agent-chat-tabs");
  for(const [role,label]of [["script","剧本 Agent"],["video","视频制作 Agent"]]) {
    const tab=button(label,()=>{view.role=role;view.data=null;renderStage();},{quiet:view.role!==role,primary:view.role===role,small:true});
    tab.setAttribute("aria-pressed",String(view.role===role));tabs.append(tab);
  }
  host.append(tabs);
  const status=node("div","agent-chat-status small-print");status.setAttribute("role","status");status.setAttribute("aria-live","polite");host.append(status);
  const list=node("div","agent-chat-messages");list.setAttribute("aria-live","polite");host.append(list);
  const form=node("form","agent-chat-form"),input=node("textarea");input.id="agent-chat-input";input.rows=3;input.maxLength=6000;
  input.placeholder="例如：继续运行，沿用已经发布的剧本，先处理尚未完成的分镜。";input.value=view.drafts[view.role];
  input.addEventListener("input",()=>{view.drafts[view.role]=input.value;});
  const controls=node("div","agent-chat-controls");
  const permission=node("select");permission.setAttribute("aria-label","本轮对话允许的操作");
  for(const [value,label]of [["work","按要求推进制作"],["inspect","仅核对与答疑"]]){const option=node("option","",label);option.value=value;permission.append(option);}
  permission.value=view.mode;permission.hidden=!view.data?.capabilities?.permission_modes;
  permission.addEventListener("change",()=>{view.mode=permission.value;});view.permissionControl=permission;
  const send=button("发送",()=>{},{primary:true,disabled:!keyReady(view.role + ".decision")});send.type="submit";
  const resume=button("继续运行",()=>{input.value="继续运行";view.drafts[view.role]=input.value;void submit();},{quiet:true,disabled:!keyReady(view.role + ".decision")});
  const stop=button("停止后续动作",async()=>{
    try{await request("/api/pi/stop",{method:"POST",body:{scope,role:view.role}});toast("已请求停止后续动作，已提交的云端任务保留。");}
    catch(error){toast(error.message,true);}
  },{quiet:true});
  append(controls,permission,send,resume,stop);append(form,input,controls);host.append(form);
  async function submit(){
    const role=view.role,text=input.value.trim(),mode=view.mode;if(!text||view.sending||!keyReady(view.role + ".decision"))return;
    view.sending=true;send.disabled=true;
    const attempt=view.attempt?.text===text&&view.attempt?.role===role&&view.attempt?.mode===mode?view.attempt:{text,role,mode,id:window.crypto.randomUUID().replaceAll("-","")};
    view.attempt=attempt;
    view.pendingTurns ||= {};view.pendingTurns[role]={message_id:attempt.id,state:"submitting"};
    agentChatStatus({view,status},view.data,role);
    try{
      const result=await request("/api/pi/messages",{method:"POST",body:{scope,role,text,message_id:attempt.id,...(view.data?.capabilities?.permission_modes?{mode}:{})}});
      if(result.job)acceptJob(result.job,"Pi 已接收会话消息");
      view.pendingTurns[role]={message_id:attempt.id,job_id:result.job?.id,state:"accepted"};
      agentChatStatus({view,status},view.data,role);
      view.drafts[role]="";view.attempt=null;if(view.role===role)input.value="";
      await agentChatRefresh({scope,view,host,list,status},{force:true});
    }catch(error){view.pendingTurns[role]={message_id:attempt.id,state:"unconfirmed",error:error.message};agentChatStatus({view,status},view.data,role);toast(error.message,true);}
    finally{view.sending=false;send.disabled=!keyReady(view.role + ".decision");}
  }
  form.addEventListener("submit",event=>{event.preventDefault();void submit();});
  agentChatCurrent.list=list;agentChatCurrent.status=status;
  if(view.data)agentChatPaint(agentChatCurrent,view.data,view.role);
  agentLiveMount(agentChatCurrent);
  if(!view.data||Date.now()-(view.lastRead||0)>=2000)void agentChatRefresh(agentChatCurrent);
  else if(typeof markStartupReady==="function")markStartupReady();
  return host;
}
async function agentChatRefresh(current,{force=false}={}){
  const {scope,view,host,list}=current,role=view.role;
  if(force && host.isConnected===false)return;
  if(!force&&view.loading&&view.loadingHost===host&&view.loadingRole===role)return;
  view.readEpochs ||= new Map();
  if(force)view.readEpochs.set(role,(view.readEpochs.get(role)||0)+1);
  const epoch=view.readEpochs.get(role)||0,ticket={},started=Date.now();view.loadingTicket=ticket;
  view.loading=true;view.loadingHost=host;view.loadingRole=role;
  try{
    const query=new URLSearchParams({...scope,role});
    view.reads ||= new Map();
    let entry=view.reads.get(role);
    if(!entry||entry.epoch!==epoch){
      entry?.controller?.abort();
      entry={epoch,controller:typeof AbortController==="function"?new AbortController():null};
      entry.promise=request("/api/pi/sessions?"+query,{signal:entry.controller?.signal}).finally(()=>{if(view.reads.get(role)===entry)view.reads.delete(role);});
      view.reads.set(role,entry);
    }
    const result=await entry.promise;
    if(view.role!==role||host.isConnected===false||(view.readEpochs.get(role)||0)!==epoch||started<(view.liveByRole?.[role]?.lastFeedAt||0))return;
    view.data=result;view.error=null;view.lastRead=Date.now();list.replaceChildren();
    if(result.session.project_id)agentChatViews.set(JSON.stringify({project:result.session.project_id}),view);
    if(result.session.source_job)agentChatViews.set(JSON.stringify({story:result.session.source_job}),view);
    agentChatPaint(current,result,role);
    agentLiveMount(current);
    if(typeof markStartupReady==="function")markStartupReady();
  }catch(error){if(view.role===role&&host.isConnected!==false&&(view.readEpochs.get(role)||0)===epoch){view.error=error.message;agentChatStatus(current,view.data,role);}}
  finally{if(view.loadingTicket===ticket)view.loading=false;}
}
function agentChatUnmount(){
  const view=agentChatCurrent?.view;
  for(const [role,entry] of view?.reads || []){
    entry.controller?.abort();view.readEpochs.set(role,(view.readEpochs.get(role)||0)+1);
  }
  view?.reads?.clear();
  const live=agentLiveActive?.view.liveByRole?.[agentLiveActive.role];
  if(live){
    live.source?.close();live.source=null;live.connected=false;live.sseConnected=false;
    live.controller?.abort();live.feedEpoch=(live.feedEpoch||0)+1;
  }
  agentLiveActive=null;agentChatCurrent=null;
}
async function agentChatPoll(){
  if(!agentChatCurrent||agentChatCurrent.host.isConnected===false){
    agentChatUnmount();return;
  }
  const view=agentChatCurrent.view;
  if(typeof EventSource!=="undefined")void agentLivePoll(agentChatCurrent);
  if(view.liveByRole?.[view.role]?.sseConnected){agentChatStatus(agentChatCurrent,view.data,view.role);return;}
  if(Date.now()-(view.lastRead||0)<(view.pendingTurns?.[view.role]||view.data?.session.active_job?2000:15000))return;
  await agentChatRefresh(agentChatCurrent);
}

function agentChatPaint(current,result,role){
  const {view,list}=current,wasAtBottom=!view.chatPainted||!Number.isFinite(list.scrollHeight)||list.scrollHeight-list.clientHeight-list.scrollTop<90,oldTop=list.scrollTop;list.replaceChildren();view.chatPainted=true;
    const pending=view.pendingTurns?.[role],message=result.messages.find(m=>m.id===pending?.message_id);
    if(pending&&message?.job_id&&result.execution?.id===message.job_id&&!["queued","running"].includes(result.execution.status)&&!["queued","dispatching"].includes(message.status))delete view.pendingTurns[role];
    agentChatStatus(current,result,role);
    if(view.permissionControl)view.permissionControl.hidden=!result.capabilities?.permission_modes;
    if(!result.messages.length)list.append(node("p","small-print","这个 Agent 的持续对话尚未开始。发送消息后会关联已有故事与制作证据。"));
    for(const message of result.messages){
      const row=node("article","agent-chat-message "+message.role);
      let displayText=message.text;
      if(message.role==="agent")try{const detail=JSON.parse(displayText);if(detail.type==="agent_response"&&typeof detail.payload?.summary==="string")displayText=detail.payload.summary;}catch{}
      append(row,node("small","",({user:"你",assistant:role==="script"?"剧本 Agent":"视频制作 Agent",agent:"协作 Agent"})[message.role]||message.role),node("p","",displayText));
      if(["queued","dispatching"].includes(message.status))row.append(node("small","small-print",message.status==="queued"?"等待进入会话":"正在处理"));
      if(message.status==="not_executed")row.append(node("small","small-print","计划未进入执行，具体原因见关联任务。"));
      if(validTraceId(message.trace_id))row.append(button("查看执行证据",()=>openTraceDesk(message.trace_id),{quiet:true,small:true}));
      list.append(row);
    }
    agentLivePaint(current,result,role);
    if(result.session.active_job)list.append(node("p","small-print","Agent 正在执行。现在补充的消息会在工具调用结束后进入当前会话。"));
    list.scrollTop=wasAtBottom?list.scrollHeight:oldTop;
  if(typeof markStartupReady==="function")markStartupReady();
}

function agentChatStatus(current,result,role){
  const {view,status}=current;if(!status)return;
  status.replaceChildren();
  const pending=view.pendingTurns?.[role],execution=pending?.job_id&&result?.execution?.id!==pending.job_id?null:result?.execution;
  const live=view.liveByRole?.[role],worker=execution?.worker;
  let text;
  if(pending?.state==="submitting")text="正在发送到当前故事的 Pi 会话…";
  else if(pending?.state==="unconfirmed")text="发送结果尚未确认："+pending.error+"。重试会沿用同一消息标识，避免重复执行。";
  else if(view.error)text="暂时无法更新会话："+view.error+"。已有记录已保留。";
  else if(execution?.status==="failed")text="本轮执行失败："+(execution.error||execution.message||"请查看执行证据。");
  else if(execution?.status==="interrupted")text="本轮未完成："+(execution.error||execution.message||"执行已中断，已有结果已保留。");
  else if(pending||result?.session?.active_job){
    const progress=execution?.agent_progress;
    text=execution?.status==="running"?(progress?.phase==="model"?"Pi 正在读取会话上下文并调用模型…":progress?.type==="tool_execution_start"?"Pi 正在执行工具："+progress.toolName:execution.message||"Pi 正在处理当前消息…"):"Pi 已接收消息，等待进入当前会话…";
    if(worker?.status==="running")text="制作循环正在执行"+(worker.shot_id?" · "+worker.shot_id:"")+(worker.tool?" · "+agentLiveTool(worker.tool):"");
    const latest=live&&Array.from(live.entries.values()).filter(e=>worker?.status!=="running"||!worker?.trace_id||e.trace_id===worker.trace_id).sort((a,b)=>a.timestamp.localeCompare(b.timestamp)).at(-1);
    if(latest)text=agentLiveLabel(latest);
  }else if(execution?.status==="succeeded")text="本轮对话已结束；制作是否完成以验收记录为准。";
  else text="发送消息后，Pi 会结合当前故事的会话和制作证据处理。";
  status.append(node("p","",text));
  if(live){
    const when=live.lastActivity?Math.max(0,Math.floor((Date.now()-live.lastActivity)/1000)):null;
    status.append(node("small","small-print",(live.connected?(live.mode==="sse"?"实时连接已建立":"进展增量同步中"):"实时连接恢复中")+(live.catchingUp?" · 正在补齐已有执行记录":when===null?" · 正在读取执行记录":" · 最近执行反馈 "+when+" 秒前")));
    if(live.error)status.append(node("p","small-print","进展同步暂时受阻："+live.error+"。任务执行与页面连接独立。"));
    if(live.discovery)status.append(node("small","small-print","正在连接本故事的后台制作循环…"));
    if(live.discoveryError)status.append(node("p","small-print","后台进展同步暂时受阻："+live.discoveryError));
    if(live.recentWindow)status.append(node("small","small-print","已载入最近执行记录；完整历史保留在 Trace。"));
    if(when>=120&&["queued","running"].includes(execution?.status)&&!live.catchingUp)status.append(node("p","small-print","已超过两分钟没有新的执行事件。可能仍在等待工具，也可能阻塞；连接正常不代表制作已有进展，请核对步骤证据。"));
  }
  if(execution&&validTraceId(execution.trace_id))status.append(button("查看本轮执行证据",()=>openTraceDesk(execution.trace_id),{quiet:true,small:true}));
}

/* The shared Trace is the durable source. Transport reconnection never submits a task. */
function agentLiveState(view,role){
  view.liveByRole ||= {};
  return view.liveByRole[role] ||= {entries:new Map(),seen:new Set(),positions:{},cursor:"",connected:false,mode:"http",lastPoll:0};
}
function agentLiveMount(current){
  if(typeof EventSource==="undefined")return;
  const {view}=current,role=view.role,live=agentLiveState(view,role);
  if(agentLiveActive&&(agentLiveActive.view!==view||agentLiveActive.role!==role)){
    const old=agentLiveActive.view.liveByRole?.[agentLiveActive.role];old?.source?.close();if(old){old.source=null;old.connected=false;old.sseConnected=false;}
  }
  agentLiveActive={view,role};
  if(view.openLiveRole&&view.openLiveRole!==role){const old=view.liveByRole[view.openLiveRole];old?.source?.close();if(old){old.source=null;old.sseConnected=false;}}
  view.openLiveRole=role;
  if(!view.data?.capabilities?.live_events)return;
  if(live.source)return;
  const query=new URLSearchParams({...current.scope,role,...(live.cursor?{cursor:live.cursor}:{})});
  const source=new EventSource("/api/pi/events/stream?"+query);live.source=source;live.mode="sse";
  source.onopen=()=>{if(live.source!==source)return;live.connected=true;live.sseConnected=true;live.mode="sse";agentLiveRedraw(view,role,false);};
  source.addEventListener("agent",message=>{
    if(live.source!==source)return;
    try{agentLiveApply(view,role,JSON.parse(message.data));live.connected=true;live.sseConnected=true;live.mode="sse";agentLiveRedraw(view,role);}
    catch(error){live.error=error.message;}
  });
  source.onerror=()=>{if(live.source!==source)return;live.connected=false;live.sseConnected=false;live.error="连接暂时中断，正在续传已有事件";agentLiveRedraw(view,role,false);};
}
function agentLiveApply(view,role,feed){
  const live=agentLiveState(view,role);if(feed.cursor)live.cursor=feed.cursor;
  for(const entry of feed.events||[]){
    if(live.seen.has(entry.id))continue;live.seen.add(entry.id);
    const key=["assistant","assistant_delta"].includes(entry.type)?"assistant:"+entry.trace_id+":"+entry.message_id:entry.type==="video_status"?"video:"+entry.task_id:entry.type==="stage"?"stage:"+entry.trace_id+":"+(["video.references.verify_receipt","reference.usage.verify_exchange"].includes(entry.stage)&&entry.status!=="ERROR"?entry.stage:entry.span_id||entry.stage):entry.id;
    const previous=live.entries.get(key);
    if(entry.type==="assistant_delta")live.entries.set(key,{...entry,type:"assistant",text:(previous?.text||"")+entry.text});
    else if(entry.type==="video_status")live.entries.set(key,{...entry,stateSince:previous?.status===entry.status?(previous.stateSince||previous.timestamp):entry.timestamp});
    else live.entries.set(key,entry);
    const stamp=Date.parse(entry.timestamp);if(Number.isFinite(stamp))live.lastActivity=Math.max(live.lastActivity||0,stamp);
  }
  while(live.entries.size>300)live.entries.delete(live.entries.keys().next().value);
  while(live.seen.size>3000)live.seen.delete(live.seen.values().next().value);
  if(view.role===role&&view.data){
    if(feed.session_id){live.feedEpoch=(live.feedEpoch||0)+1;live.lastFeedAt=Date.now();view.error=null;view.lastRead=Date.now();}
    if(feed.messages)view.data.messages=feed.messages;
    if(feed.execution)view.data.execution=feed.execution;
    if("active_job"in feed)view.data.session.active_job=feed.active_job;
  }
}
function agentLiveRedraw(view,role,paint=true){
  const current=agentChatCurrent;
  if(current?.view!==view||view.role!==role||current.host.isConnected===false)return;
  if(paint&&view.data)agentChatPaint(current,view.data,role);else agentChatStatus(current,view.data,role);
}
function agentLiveTool(name){return ({read_video_state:"读取制作状态",read_current_commission:"核对冻结委托",read_production_diagnostics:"读取失败证据",continue_authorized_production:"接续制作循环",plan_production:"规划制作",generate_shot:"生成镜头",regenerate_shot:"定向重拍",prepare_shot_repair:"准备修复",design_repair_strategy:"设计修复策略",resume_pending_generation:"接续原云端任务并验收",review_existing_shot:"验收已有镜头",review_and_export:"验收并检查预览门禁",prepare_reference_inputs:"编排并核验参考输入",edit_shot_asset:"修改视频素材",ask_other_agent:"发起跨 Agent 协作"})[name]||name||"执行工具";}
function agentLiveLabel(entry){
  const shot=entry.arguments?.shot_id?" · "+entry.arguments.shot_id:"";
  if(entry.type==="assistant")return "Agent 正在说明执行情况";
  if(entry.type==="recovery")return entry.summary||"项目执行器正在恢复原制作委托";
  if(entry.type==="tool_start")return "正在"+agentLiveTool(entry.tool)+shot;
  if(entry.type==="tool_end")return agentLiveTool(entry.tool)+"已返回"+(entry.result?.summary?" · "+entry.result.summary:entry.result?.ready===false?" · 仍有待处理项":"");
  if(entry.type==="tool_error"||entry.type==="error")return "执行遇到问题："+(entry.error||"详见本条 Trace");
  if(entry.type==="model_start")return entry.purpose==="compaction"?"正在整理会话上下文":"正在调用模型决定下一步";
  if(entry.type==="model_end")return "模型已返回"+(entry.model?" · "+entry.model:"");
  if(entry.type==="video_status")return "云端视频任务 · "+({PENDING:"排队中",RUNNING:"生成中",SUCCEEDED:"生成完成，待验收",FAILED:"生成失败",CANCELED:"已取消"}[entry.status]||entry.status||"查询状态")+(["PENDING","RUNNING"].includes(entry.status)?" · 此状态已等待 "+Math.max(0,Math.floor((Date.now()-Date.parse(entry.stateSince||entry.timestamp))/1000))+" 秒":"");
  if(entry.type==="compaction_start")return "正在压缩并保存会话上下文";
  if(entry.type==="compaction_end")return "会话上下文已整理";
  if(entry.type==="session_ready")return "已载入 Pi 原会话 · "+(entry.message_count||0)+" 条原生消息";
  if(entry.type==="finishing")return "正在核对本轮结束条件";
  if(entry.type==="work_queue")return "制作队列 · 可继续 "+entry.actionable+" 项，待补证据 "+entry.blocked+" 项，达到上限 "+entry.exhausted+" 项";
  if(entry.type==="media_start")return "正在"+entry.action+(entry.shot_id?" · "+entry.shot_id.replace("-","→"):"");
  if(entry.type==="media_end")return entry.status==="ERROR"?"验收媒体处理失败："+(entry.error||"详见证据"):"验收媒体子步骤已返回";
  if(entry.type==="stage")return (entry.running?"正在":entry.status==="ERROR"?"步骤失败：":"已返回：")+({"content.model.observe_video":"调用视觉模型验收片段","content.prepare_cut":"准备相邻镜头衔接验收","video.references.verify_receipt":"核对固定参考提交回执","reference.usage.verify_exchange":"核对参考使用证据","video.autonomy.observe_pending":"核对原云端任务并接纳结果"}[entry.stage]||entry.stage)+(entry.status==="ERROR"&&entry.error?" · "+entry.error:"");
  return "制作进展已更新";
}
function agentLivePaint(current,result,role){
  const live=current.view.liveByRole?.[role];if(!live)return;
  const entries=Array.from(live.entries.values()).sort((a,b)=>a.timestamp.localeCompare(b.timestamp));
  for(const entry of entries){
    if(entry.type==="assistant"&&result.messages.some(m=>m.role==="assistant"&&m.text===entry.text))continue;
    const row=node("article","agent-chat-message agent-live-entry "+(entry.type==="assistant"?"assistant":"tool"));
    row.setAttribute("data-event-id",entry.id);
    append(row,node("small","",entry.type==="assistant"?(role==="video"?"视频制作 Agent":"剧本 Agent")+" · 执行说明":"执行进展 · "+new Date(entry.timestamp).toLocaleTimeString()),
      node("p","",entry.type==="assistant"?entry.text:agentLiveLabel(entry)));
    if(entry.type==="assistant")row.append(node("small","small-print",new Date(entry.timestamp).toLocaleTimeString()+" · 实际执行记录"));
    if(entry.result?.reason||entry.result?.error)row.append(node("p","small-print",entry.result.reason||entry.result.error));
    if(validTraceId(entry.trace_id))row.append(button("查看这一步证据",()=>openTraceDesk(entry.trace_id),{quiet:true,small:true}));
    current.list.append(row);
  }
}

async function agentLivePoll(current){
  const {view}=current,role=view.role,live=agentLiveState(view,role);
  if(live.busy||live.sseConnected||Date.now()-live.lastPoll<1500)return;
  live.busy=true;live.lastPoll=Date.now();
  const controller=typeof AbortController==="function"?new AbortController():null;
  live.controller=controller;
  const signal=controller?.signal;
  try{
    if(view.data?.capabilities?.live_events){
      if(!live.source)agentLiveMount(current);
      const query=new URLSearchParams({...current.scope,role,...(live.cursor?{cursor:live.cursor}:{})});
      const epoch=live.feedEpoch||0,feed=await request("/api/pi/events?"+query,{signal});
      if((live.feedEpoch||0)===epoch){agentLiveApply(view,role,feed);live.mode="http";}
    }else{
      const execution=view.data?.execution,traces=new Set([execution?.trace_id,execution?.worker?.trace_id].filter(validTraceId));
      // Compatibility with an already running executor: discover its detached
      // trace from the original story's persisted collaboration stages.
      const job=typeof state!=="undefined"?state.jobs?.find(j=>j.id===execution?.id):null;
      if(job?.commission_id&&live.discoveryJob!==job.id){
        // The already loaded studio snapshot has the exact commission and its
        // repair receipts. Never scan global trace history or validate unrelated
        // productions to attach an already running legacy worker.
        const studio=typeof studioLocal==="function"?studioLocal(current.scope.project)?.data:null;
        const partial=studio?.incomplete_production;
        const receipt=partial?.request_id===job.commission_id?partial.repair_history?.filter(r=>validTraceId(r.trace_id)).at(-1):null;
        if(receipt){live.workerTrace=receipt.trace_id;live.discoveryJob=job.id;live.discoveryError=null;}
        else live.discoveryError="旧服务尚未在本故事的制作快照中提供后台执行证据；会话记录继续同步";
      }
      if(validTraceId(live.workerTrace)){
        // The production worker is current; the parent's pre-handoff dialogue
        // is already in persistent messages. Do not let a large old parent
        // trace delay delivery of current production progress.
        traces.clear();traces.add(live.workerTrace);
      }
      for(const tid of traces){
        if(!(tid in live.positions)){
          // An old executor cannot filter event names. Start with its recent
          // window rather than replaying tens of thousands of validation spans.
          // The complete history remains available through the Trace links.
          live.positions[tid]=await agentLiveRecentPosition(tid,signal);
          if(live.positions[tid]>0)live.recentWindow=true;
        }
        const page=await request("/api/traces/"+tid+"/events?after="+(live.positions[tid]||0)+"&limit=1000",{signal});
        live.catchingUp=!!page.has_more;
        const events=[];
        for(const raw of page.events||[]){
          if(signal?.aborted)return;
          try{const projected=await agentLiveProject(raw,signal);if(projected)events.push(projected);}
          catch(error){events.push({id:raw.event_id,trace_id:raw.trace_id,timestamp:raw.timestamp,type:"error",error:"此执行记录暂时无法显示："+error.message+"；原证据保留在 Trace。"});}
        }
        if(signal?.aborted)return;
        live.positions[tid]=page.next_after||live.positions[tid]||0;
        agentLiveApply(view,role,{events});
        agentLiveRedraw(view,role);
      }
    }
    if(!signal?.aborted){live.connected=true;live.error=null;agentLiveRedraw(view,role);}
  }catch(error){if(!signal?.aborted){live.connected=false;live.error=error.message;agentLiveRedraw(view,role,false);}}
  finally{live.busy=false;}
}
async function agentLiveRecentPosition(tid,signal){
  // The existing indexed event route is cheap even on old servers whose global
  // trace-list query is expensive. Probe coarse sequence blocks, then read the
  // most recent window. All requests remain scoped, read-only and resumable.
  const exists=async after=>(await request("/api/traces/"+tid+"/events?after="+after+"&limit=1",{signal})).events?.length>0;
  let low=0,high=1000;
  while(high<16000000&&await exists(high)){low=high;high*=2;}
  while(high-low>1000){const middle=Math.floor((low+high)/2000)*1000;if(await exists(middle))low=middle;else high=middle;}
  return Math.max(0,low-1000);
}
async function agentLiveProject(raw,signal){
  const names=new Set(["pi.session.event","pi.tool.request","pi.tool.response","pi.tool.error","pi.model.purpose","pi.model.receipt","pi.sdk.ready","pi.run.stopped","pi.video.continuation_failed","bailian.video.submit","bailian.video.get_task","video.autonomy.work_partition","media.process","export.process","content.model.observe_video","content.prepare_cut","video.references.verify_receipt","reference.usage.verify_exchange"]);
  if(!names.has(raw.name))return null;
  if(["bailian.video.submit","bailian.video.get_task"].includes(raw.name)&&raw.kind!=="span.end")return null;
  let data=raw.data;
  if(data&&Object.keys(data).length===3&&data.$artifact&&data.media_type==="application/json")data=(await request("/api/trace-artifacts/"+data.$artifact,{signal})).data;
  if(!data||typeof data!=="object")return null;
  let value=null;
  if(raw.name==="pi.session.event"){
    const message=data.message||{};
    if(data.type==="message_end"&&message.role==="assistant"){
      const text=typeof message.content==="string"?message.content:(message.content||[]).filter(c=>c.type==="text").map(c=>c.text).join("");
      if(text)value={type:"assistant",text,message_id:String(message.timestamp||raw.event_id)};
    }else if(data.type==="assistant_text")value={type:"assistant_delta",text:data.text,message_id:data.message_id};
    else if(["compaction_start","compaction_end"].includes(data.type))value={type:data.type};
    else if(data.type==="tool_execution_start"&&data.toolName==="finish_task")value={type:"finishing"};
  }else if(raw.name==="pi.tool.request")value={type:"tool_start",tool:data.tool,call_id:data.call_id,arguments:{shot_id:data.arguments?.shot_id}};
  else if(["pi.tool.response","pi.tool.error"].includes(raw.name)){
    const result=data.result||{};value={type:raw.name.endsWith("error")?"tool_error":"tool_end",tool:data.tool,error:data.error,
      result:{summary:typeof result.summary==="string"?result.summary.slice(0,2000):null,ready:result.ready,reason:typeof result.reason==="string"?result.reason.slice(0,2000):null,status:result.status}};
  }else if(raw.name==="pi.model.purpose")value={type:"model_start",purpose:data.purpose};
  else if(raw.name==="pi.model.receipt")value={type:"model_end",model:data.model};
  else if(raw.name==="pi.sdk.ready")value={type:"session_ready",message_count:data.messages};
  else if(["pi.run.stopped","pi.video.continuation_failed"].includes(raw.name))value={type:"error",error:data.reason||data.error};
  else if(["bailian.video.submit","bailian.video.get_task"].includes(raw.name)&&raw.kind==="span.end"){
    let output=data.output;if(output?.$artifact&&Object.keys(output).length===3)output=(await request("/api/trace-artifacts/"+output.$artifact)).data;
    if(output?.output?.task_id)value={type:"video_status",task_id:output.output.task_id,status:output.output.task_status};
  }else if(raw.name==="video.autonomy.work_partition")value={type:"work_queue",actionable:data.actionable?.length||0,blocked:data.blocked?.length||0,exhausted:data.exhausted?.length||0};
  else if(["media.process","export.process"].includes(raw.name)){
    if(raw.kind==="span.start"){
      const argv=data.inputs?.argv||[],program=String(argv[0]||""),joined=argv.join(" "),shot=joined.match(/\bS\d{3}(?:-S\d{3})?\b/);
      value={type:"media_start",program:program.includes("ffprobe")?"ffprobe":"ffmpeg",action:program.includes("ffprobe")?"检查验收媒体":joined.includes("concat")?"拼接衔接验收片段":"处理验收片段",shot_id:shot?.[0]};
    }else if(raw.kind==="span.end")value={type:"media_end",status:data.status,error:data.error?.message};
  }else if(["content.model.observe_video","content.prepare_cut","video.references.verify_receipt","reference.usage.verify_exchange"].includes(raw.name))value={type:"stage",stage:raw.name,running:raw.kind==="span.start",status:data.status,error:typeof data.error==="string"?data.error.slice(0,2000):data.error?.message?.slice(0,2000)};
  return value?{id:raw.event_id,trace_id:raw.trace_id,span_id:raw.span_id,timestamp:raw.timestamp,...value}:null;
}
