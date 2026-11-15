/* Explicit offline conversation UI behavior; no server/model/browser requests. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element{
  constructor(tag,text=''){this.tagName=tag;this.textContent=text;this.children=[];this.listeners={};this.value='';this.isConnected=true;}
  append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}setAttribute(){}
  addEventListener(name,fn){this.listeners[name]=fn;}fire(name){return this.listeners[name]?.({preventDefault(){}});}
  set innerHTML(_){throw new Error('Conversation content must remain text');}
}
const walk=e=>[e,...e.children.flatMap(c=>c instanceof Element?walk(c):[])];
const content=e=>walk(e).map(n=>n.textContent).join('\n');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../web/agent-chat.js'),'utf8');
function harness(){
  const calls=[],scope={story:'a'.repeat(32)};let current,fail=false,counter=0;const options={},network={};
  const context=vm.createContext({Map,JSON,URLSearchParams,Promise,AbortController,
    window:{crypto:{randomUUID:()=>String(++counter).padStart(32,'0')}},
    node:(tag,cls,text)=>new Element(tag,text||''),append:(e,...c)=>e.append(...c),
    button:(label,handler,options={})=>{const e=new Element('button',label);e.disabled=options.disabled;e.addEventListener('click',handler);return e;},
    keyReady:()=>true,validTraceId:id=>/^[a-f0-9]{32}$/.test(id||''),openTraceDesk(){},toast(){},acceptJob(){},
    request:async(url,options)=>{calls.push({url,options});if(network.handle)return network.handle(url,options);if(options?.method==='POST'){
      if(fail){fail=false;throw new Error('offline ambiguous connection');}return {duplicate:true};}
      const query=new URLSearchParams(url.split('?')[1]);return {session:{active_job:null},messages:[{role:'assistant',text:query.get('role')==='script'?'剧本会话记录':'视频会话记录',status:'delivered'}]};},
    renderStage:()=>{current.isConnected=false;current=vm.runInContext('renderAgentChat(scope,options)',context);},scope,options});
  vm.runInContext(source,context);current=vm.runInContext('renderAgentChat(scope,options)',context);
  return {context,calls,network,get host(){return current;},failNext(){fail=true;},stage(phase,role){options.phase=phase;options.preferredRole=role;context.renderStage();}};
}
async function flush(){for(let i=0;i<24;i++)await Promise.resolve();}
async function main(){
  {
    const h=harness();
    h.context.keyReady=purpose=>purpose==='video.decision';
    h.stage('authoring','script');
    assert.equal(walk(h.host).find(e=>e.textContent==='发送').disabled,true);
    h.stage('shooting','video');
    assert.equal(walk(h.host).find(e=>e.textContent==='发送').disabled,false);
  }
  {
    const h=harness();await flush();assert.ok(content(h.host).includes('剧本会话记录'));
    const input=walk(h.host).find(e=>e.tagName==='textarea');input.value='保留尚未发送的剧本修改';input.fire('input');
    walk(h.host).find(e=>e.textContent==='视频制作 Agent').fire('click');await flush();
    assert.ok(content(h.host).includes('视频会话记录'));assert.equal(walk(h.host).find(e=>e.tagName==='textarea').value,'');
    walk(h.host).find(e=>e.textContent==='剧本 Agent').fire('click');await flush();
    assert.equal(walk(h.host).find(e=>e.tagName==='textarea').value,'保留尚未发送的剧本修改');
    assert.ok(!content(h.host).includes('视频会话记录'));
  }
  {
    const h=harness();await flush();const input=walk(h.host).find(e=>e.tagName==='textarea');input.value='继续运行';input.fire('input');
    const form=walk(h.host).find(e=>e.tagName==='form');h.failNext();form.fire('submit');await flush();
    assert.equal(input.value,'继续运行');form.fire('submit');await flush();
    const attempts=h.calls.filter(c=>c.options?.method==='POST');assert.equal(attempts.length,2);
    assert.equal(attempts[0].options.body.message_id,attempts[1].options.body.message_id,'Retry preserves the original message id');
    assert.equal(input.value,'');
  }
  {
    const h=harness();h.stage('video','video');await flush();
    assert.ok(content(h.host).includes('视频会话记录'));
    walk(h.host).find(e=>e.textContent==='剧本 Agent').fire('click');await flush();
    h.stage('video','video');await flush();assert.ok(content(h.host).includes('剧本会话记录'),'Manual role choice survives polling');
    h.context.options.selectRole=true;h.stage('video','video');await flush();
    assert.ok(content(h.host).includes('视频会话记录'),'An explicit video conversation shortcut always selects video');
    h.context.options.selectRole=false;
    h.stage('story','script');await flush();h.stage('video','video');await flush();
    assert.ok(content(h.host).includes('视频会话记录'),'Entering the production stage selects its existing video conversation');
  }
  {
    const h=harness();await flush();const before=h.calls.length;
    vm.runInContext(`primeAgentChat(scope,{script:{session:{active_job:null},messages:[{role:'assistant',text:'快照中的剧本对话',status:'delivered'}]},video:{session:{active_job:null},messages:[{role:'assistant',text:'快照中的视频对话',status:'delivered'}]}})`,h.context);
    h.stage('video','video');await flush();
    assert.ok(content(h.host).includes('快照中的视频对话'));
    assert.equal(h.calls.length,before,'A fresh workspace snapshot avoids another startup request');
    vm.runInContext('agentChatState(scope).snapshotsAt=0',h.context);
    walk(h.host).find(e=>e.textContent==='剧本 Agent').fire('click');await flush();
    assert.equal(h.calls.length,before+1,'Expired role snapshots must load the actual persisted conversation');
  }
  {
    const h=harness();await flush();
    let stale;h.network.handle=()=>new Promise(resolve=>{stale=resolve;});
    vm.runInContext('agentChatRefresh(agentChatCurrent)',h.context);await flush();
    h.network.handle=async(url,options)=>options?.method==='POST'?{job:{id:'current-job'}}:{
      session:{active_job:'current-job'},execution:{id:'current-job',status:'running',agent_progress:{phase:'model'}},
      messages:[{id:'1'.padStart(32,'0'),role:'user',text:'新的诊断请求',status:'delivered',job_id:'current-job'}]};
    const input=walk(h.host).find(e=>e.tagName==='textarea');input.value='新的诊断请求';input.fire('input');
    walk(h.host).find(e=>e.tagName==='form').fire('submit');await flush();
    assert.ok(content(h.host).includes('调用模型'),'Accepted turn shows real model progress immediately');
    stale({session:{active_job:null},messages:[{role:'assistant',text:'发送前的旧快照'}]});await flush();
    assert.ok(content(h.host).includes('新的诊断请求'));assert.ok(!content(h.host).includes('发送前的旧快照'),'Pre-submit GET must never erase the newly accepted turn');
    h.network.handle=async()=>({session:{active_job:null},execution:{id:'current-job',status:'failed',error:'实际输入 8 张，允许 1–5 张',trace_id:'f'.repeat(32)},
      messages:[{id:'1'.padStart(32,'0'),role:'user',text:'新的诊断请求',status:'delivered',job_id:'current-job'}]});
    vm.runInContext('agentChatRefresh(agentChatCurrent,{force:true})',h.context);await flush();
    assert.ok(content(h.host).includes('本轮执行失败：实际输入 8 张'));
    assert.ok(content(h.host).includes('查看本轮执行证据'));
    assert.equal(vm.runInContext('!!agentChatState(scope).pendingTurns.script',h.context),false);
  }
  {
    const h=harness();await flush();const streams=[];
    class OfflineEventSource{
      constructor(url){this.url=url;this.listeners={};streams.push(this);}addEventListener(name,fn){this.listeners[name]=fn;}close(){this.closed=true;}
      emit(value){this.listeners.agent({data:JSON.stringify(value)});}
    }
    h.context.EventSource=OfflineEventSource;
    h.network.handle=async()=>({session:{id:'offline-session',active_job:'live-job'},execution:{id:'live-job',status:'running',trace_id:'a'.repeat(32)},
      messages:[{role:'user',text:'继续制作',job_id:'live-job',status:'delivered'}],capabilities:{live_events:true}});
    h.stage('video','video');await flush();assert.equal(streams.length,1);streams[0].onopen();
    const time='2026-11-13T10:00:00Z',tid='a'.repeat(32);
    const started={id:'event-1',trace_id:tid,timestamp:time,type:'tool_start',tool:'regenerate_shot',arguments:{shot_id:'S002'}};
    streams[0].emit({cursor:'offline-cursor',events:[started]});streams[0].emit({cursor:'offline-cursor',events:[started]});
    assert.equal(content(h.host).split('正在定向重拍 · S002').length-1,2,'One status and one event, never duplicate timeline entries');
    streams[0].emit({events:[{id:'text-1',trace_id:tid,timestamp:time,type:'assistant_delta',message_id:'m',text:'公开'},
      {id:'text-2',trace_id:tid,timestamp:time,type:'assistant_delta',message_id:'m',text:'说明'},
      {id:'text-3',trace_id:tid,timestamp:time,type:'assistant',message_id:'m',text:'公开说明'}]});
    assert.equal(content(h.host).split('公开说明').length-1,1,'Final native text replaces accumulated deltas');
    const projected=await vm.runInContext(`agentLiveProject({name:'pi.session.event',event_id:'private-check',trace_id:'${tid}',timestamp:'${time}',data:{type:'message_end',message:{role:'assistant',timestamp:9,content:[{type:'thinking',thinking:'NEVER PUBLISH'},{type:'text',text:'可见说明'}]}}})`,h.context);
    assert.equal(projected.text,'可见说明');assert.ok(!JSON.stringify(projected).includes('NEVER PUBLISH'));
    streams[0].emit({events:[{id:'stage-error',trace_id:tid,span_id:'failed-check',timestamp:time,type:'stage',stage:'content.model.observe_video',status:'ERROR',error:'offline: 视频超过内联视频上限'},
      {id:'stage-retry',trace_id:tid,span_id:'next-check',timestamp:time,type:'stage',stage:'content.model.observe_video',running:true}]});
    assert.ok(content(h.host).includes('步骤失败：调用视觉模型验收片段 · offline: 视频超过内联视频上限'));
    assert.ok(content(h.host).includes('正在调用视觉模型验收片段'),'A retry must retain the prior failure evidence');
    streams[0].onerror();
    h.network.handle=async url=>url.startsWith('/api/pi/events?')?{session_id:'offline-session',active_job:'live-job',execution:{id:'live-job',status:'running'},events:[started],cursor:'resumed'}:
      {session:{active_job:'live-job'},messages:[],capabilities:{live_events:true}};
    await vm.runInContext('agentLivePoll(agentChatCurrent)',h.context);await flush();
    assert.ok(h.calls.some(c=>c.url.includes('/api/pi/events?')&&c.url.includes('cursor=offline-cursor')));
    assert.equal(h.calls.filter(c=>c.options?.method==='POST').length,0,'Reconnection never resubmits the user request');
    let finishOldRead;
    h.network.handle=()=>new Promise(resolve=>{finishOldRead=resolve;});
    const pending=vm.runInContext('agentChatRefresh(agentChatCurrent,{force:true})',h.context);
    const oldSignal=h.calls.at(-1).options.signal;
    vm.runInContext('agentChatUnmount()',h.context);
    assert.ok(oldSignal.aborted,'Navigation aborts the old scoped conversation read');
    assert.ok(streams[0].closed,'Navigation releases the prior event stream before the new workspace finishes');
    finishOldRead({session:{},messages:[]});await pending;
    assert.equal(streams.length,1,'An obsolete GET cannot reopen a closed stream');
  }
  {
    const h=harness();await flush();const tid='b'.repeat(32),outer='c'.repeat(32),pid='d'.repeat(16);
    h.context.EventSource=class{constructor(){throw new Error('Legacy compatibility must not open SSE');}};
    const story='e'.repeat(32);
    h.context.state={jobs:[{id:'old-executor',commission_id:'commission',story_job_id:story}]};
    h.context.studioLocal=()=>({data:{incomplete_production:{request_id:'commission',repair_history:[{trace_id:tid}]}}});
    delete h.context.scope.story;h.context.scope.project=pid;
    h.network.handle=async url=>{
      if(url.startsWith('/api/pi/sessions?'))return {session:{active_job:'old-executor'},execution:{id:'old-executor',status:'running',trace_id:outer},messages:[],capabilities:{}};
      if(url.startsWith('/api/traces/')){const query=new URLSearchParams(url.split('?')[1]);if(Number(query.get('after'))>=50000)return {events:[],next_after:50000,has_more:false};return {events:[{event_id:url.includes(tid)?'worker-live':'outer-live',trace_id:url.includes(tid)?tid:outer,
        timestamp:'2026-11-13T11:00:00Z',kind:'span.start',name:'export.process',data:{inputs:{argv:['ffmpeg','S001-S002.mp4','concat']}}}],next_after:50000,has_more:false};
      }
      throw new Error('Unexpected offline request '+url);
    };
    h.stage('video','video');await flush();
    await vm.runInContext('agentLivePoll(agentChatCurrent)',h.context);await flush();
    vm.runInContext('agentChatCurrent.view.liveByRole.video.lastPoll=0',h.context);
    await vm.runInContext('agentLivePoll(agentChatCurrent)',h.context);await flush();
    assert.equal(vm.runInContext('agentChatCurrent.view.liveByRole.video.workerTrace',h.context),tid);
    assert.ok(h.calls.some(c=>c.url.includes(tid+'/events?after=48000&limit=1000')),'Existing executor loads recent events without replaying a huge validation history');
    assert.ok(!h.calls.some(c=>c.url.startsWith('/api/traces?')||c.url==='/api/collaborations'),'Legacy progress must not scan global history');
    assert.ok(content(h.host).includes('正在拼接衔接验收片段 · S001→S002'));
    assert.ok(content(h.host).includes('完整历史保留在 Trace'));
    assert.equal(h.calls.filter(c=>c.options?.method==='POST').length,0);
  }
  console.log('Pi chat UI: 8 offline behavior checks passed; no network or model calls.');
}
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
module.exports={harness,flush,content,walk};
