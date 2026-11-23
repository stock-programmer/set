/* Offline Agent execution UI contracts. No browser, model or network calls. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.dataset = {}; this.textContent = ""; this.value = ""; this.checked = false; this.disabled = false; this.classList = { toggle() {} }; }
  set innerHTML(_) { throw new Error("Model-derived summaries must remain text"); }
  append(...children) { children.forEach((child) => { if (typeof child === "object") child.parentElement = this; }); this.children.push(...children); }
  replaceChildren(...children) { this.children.forEach((child) => { if (typeof child === "object") child.parentElement = null; }); this.children = []; this.append(...children); }
  get childElementCount() { return this.children.length; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(kind, callback) { this.listeners[kind] = callback; }
  async fire(kind) { return this.listeners[kind]?.({ preventDefault() {} }); }
  querySelectorAll(tag) { return walk(this).filter((item) => item !== this && item.tagName === tag); }
  closest(selector) { for (let item = this; item; item = item.parentElement) if (selector === `#${item.id}`) return item; return null; }
  focus() {}
  remove() {}
}
function walk(root) { return [root, ...root.children.flatMap((item) => typeof item === "object" ? walk(item) : [])]; }
function text(root) { return walk(root).map((item) => item.textContent).join("\n"); }
function field(root, id) { const found = walk(root).find((item) => item.id === id); assert.ok(found, id); return found; }
const source = fs.readFileSync(path.join(__dirname, "../../web/studio.js"), "utf8") + "\n" + fs.readFileSync(path.join(__dirname, "../../web/app.js"), "utf8").replace(/initialize\(\);\s*$/, "");
function harness() {
  const elements = new Map(), calls = [];
  const context = vm.createContext({ console, URL, URLSearchParams, Date, Map, Set, JSON, Number, String, Object, Array, Promise, queueMicrotask,
    setTimeout() {}, setInterval() {}, location: { hash: "" }, history: { replaceState: (_, __, hash) => { context.location.hash = hash; } },
    window: { location: { origin: "http://127.0.0.1:8765" }, crypto: { randomUUID: () => "offline-agent" }, addEventListener() {} },
    document: { body: new Element("body"), activeElement: null, createElement: (tag) => new Element(tag), querySelectorAll: () => [],
      getElementById: (id) => { const mounted = [...elements.values()].flatMap(walk).find((item) => item.id === id); if (mounted) return mounted; if (!elements.has(id)) elements.set(id, new Element("div")); return elements.get(id); } },
    fetch: async (url, options) => {
      calls.push({ url, method: options.method, body: options.body && JSON.parse(options.body), headers: options.headers });
      return { ok: true, json: async () => ({ job: { id: "a".repeat(32), kind: "agent", project_id: "A", status: "queued", trace_id: "b".repeat(32) } }) };
    },
  });
  vm.runInContext(source, context, { filename: "web/app.js" });
  const run = (code) => vm.runInContext(code, context);
  run('const readStudio=studioRefresh;studioRefresh=async()=>{};studioRedraw=()=>{};renderJobs=()=>{};renderStage=()=>{};');
  run('state.config={key_configured:true};state.projects=[{id:"A",title:"故事A"},{id:"B",title:"故事B"}];state.projectId="A";state.project={id:"A",title:"故事A",shots:[]};state.loading=false;state.step="agent"');
  return { run, context, elements, calls, render: () => run("studioExecutionLimits(studioLocal(\"A\").data)") };
}
async function main() {
  let passed = 0;
  const h=harness();
  h.run('const spec={asset_id:"story-A/spec",revision_id:"r1",sha256:"1".repeat(64)};const script={...spec,asset_id:"story-A/script"};const cid="c".repeat(32);const policy={shot_ids:["S001"],max_repairs_per_shot:2,max_local_edits_per_shot:2};studioLocal("A").data={video_plan:{plan_id:"p",new_shots:[],retake_shots:[],reuse_shots:["S001"],acceptance_loop:policy},video_execution:{commission_id:cid,resumable:true,script_ref:script,spec_ref:spec,plan:{acceptance_loop:policy},limits:{max_video_generations:3,max_model_calls:32,max_repairs_per_shot:2,max_local_edits_per_shot:2},usage:{reserved_video_generations:2,remaining_video_generations:1,model_calls:7}}}');
  {
    assert.equal(h.run('typeof renderAgent'), 'undefined');
    assert.equal(h.run('typeof agentForm'), 'undefined');
    assert.ok(!source.includes('高级执行设置'));
    const ui=h.render();
    for(const label of ['当前冻结制作委托','生成调用上限','3 次','剩余生成调用','1 次','32 次'])assert.ok(text(ui).includes(label),label);
    assert.ok(!walk(ui).some(n=>['form','input','textarea','select'].includes(n.tagName)),'Limits cannot start another model or alter the commission');passed++;
  }
  {
    const ui=h.run('studioVideoPlan(studioLocal("A").data,{script_ref:script,spec},studioLocal("A"))');
    const labels=walk(ui).filter(n=>n.tagName==='button').map(n=>n.textContent);
    assert.ok(text(ui).includes('核对当前委托并恢复制作'));
    assert.ok(labels.includes('继续当前制作委托'));assert.ok(labels.includes('向视频 Agent 补充要求'));
    assert.ok(!labels.some(x=>x.includes('开始制作')||x.includes('确认新的')),'Same frozen scope cannot silently restart its budget');passed++;
  }
  {
    let fail=true;
    h.context.fetch=async(url,options)=>{h.calls.push({url,body:JSON.parse(options.body)});if(fail){fail=false;throw Error('Explicit offline ambiguous response');}return {ok:true,json:async()=>({session_id:'same-session',duplicate:true})};};
    await h.run('studioContinueConversation("A",cid)');
    await h.run('studioContinueConversation("A",cid)');
    assert.equal(h.calls.length,2);assert.equal(h.calls[0].url,'/api/pi/messages');
    assert.equal(h.calls[0].body.message_id,h.calls[1].body.message_id,'An ambiguous retry preserves message identity');
    assert.equal(h.calls[0].body.continue_commission,h.run('cid'));
    assert.equal(h.calls[1].body.continue_commission,h.calls[0].body.continue_commission);
    assert.equal(h.calls[0].body.role,'video');assert.equal(h.calls[0].body.mode,'work');
    assert.equal(h.calls[0].body.scope.project,'A');assert.ok(h.calls[0].body.text.includes('c'.repeat(32)));
    assert.equal(h.run('studioLocal("A").data.video_execution.usage.remaining_video_generations'),1);passed++;
    await h.run('studioContinueConversation("A","d".repeat(32))');assert.equal(h.calls.length,2,'Stale commission is rejected');
    h.run('state.projectId="B"');await h.run('studioContinueConversation("A",cid)');assert.equal(h.calls.length,2,'Stale story is rejected');passed++;
  }
  {
    // The button is already mounted when the projected workbench starts its slow GET.
    h.run('state.projectId="A";const view=studioLocal("A");view.videoReadMode="video";view.data.projection="video";view.data.project_id="A";view.data.video_execution.plan={...view.data.video_plan};studioRefresh=readStudio');
    const plan=()=>h.run('studioVideoPlan(view.data,{script_ref:script,spec},view)');
    const continueButton=()=>walk(plan()).find(n=>n.textContent==='继续当前制作委托');
    const mounted=continueButton();
    assert.equal(mounted.disabled,false);
    const reads=[], posts=[];
    h.context.fetch=(url,options)=>new Promise(resolve=>{
      if(options.method==='GET')reads.push({resolve});
      else posts.push({resolve,body:JSON.parse(options.body)});
    });
    const polling=h.run('studioRefresh("A",true)');
    const submitted=mounted.fire('click');
    await mounted.fire('click');
    assert.equal(posts.length,1,'Click during polling submits once, even with a second click');
    const nextTick=h.run('studioRefresh("A",true)');
    assert.equal(reads.length,1,'Slow poll must not be replaced by the next polling tick');
    await nextTick;
    assert.ok(posts[0].body.text.includes(h.run('cid')));
    assert.equal(continueButton().disabled,true);
    assert.ok(text(plan()).includes('正在向视频 Agent 提交继续要求'));
    const reply=(data,ok=true)=>({ok,status:ok?200:409,json:async()=>data});
    const snapshot=JSON.parse(JSON.stringify(h.run('view.data')));
    reads[0].resolve(reply({...snapshot,jobs:[]}));
    await polling;
    posts[0].resolve(reply({error:'制作委托已变更，请核对原计划'},false));
    await submitted;
    assert.ok(text(plan()).includes('制作委托已变更，请核对原计划'),'See the rejection next to the clicked button');
    const retried=continueButton().fire('click');
    assert.equal(posts[1].body.message_id,posts[0].body.message_id);
    const job={id:'a'.repeat(32),kind:'pi_video',project_id:'A',status:'queued',message:'Pi 会话已接收消息'};
    posts[1].resolve(reply({job,message:{job_id:job.id}}));
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(reads.length,2,'An accepted continuation refreshes the project');
    reads[1].resolve(reply({...snapshot,jobs:[job]}));
    await retried;
    assert.ok(text(plan()).includes(job.message),'Show the actual acknowledgement from the conversation job');
    h.run('Object.assign(state.jobs[0],{status:"interrupted",result:{summary:"历史欠费推断"}})');
    assert.ok(text(plan()).includes('制作尚未接续'));
    assert.ok(!text(plan()).includes('历史欠费推断'));
    h.run('state.jobs[0].commission_id=cid;view.data.video_execution.last_execution={id:state.jobs[0].id};view.data.video_execution.submission_rejections=[{shot_id:"S003",provider:"volcengine-ark",http_status:400,code:"InputImageSensitiveContentDetected.PrivacyInformation",references:[{subject_id:"角色A",revision_id:"r1"}]}]');
    assert.ok(text(plan()).includes('S003 · 火山方舟拒绝受理（HTTP 400）'));
    assert.ok(text(plan()).includes('角色A r1'));
    assert.ok(!text(plan()).includes('历史欠费推断'));
    h.run('state.jobs[0].status="failed";state.jobs[0].error={message:"原任务恢复失败"}');
    assert.ok(text(plan()).includes('原任务恢复失败'),'Show the subsequent job failure at the action');
    passed++;
  }
  {
    h.run('state.projectId="A";api.project=async id=>({id,title:"故事A",shots:[]})');
    await h.run('openJobResult({kind:"agent",project_id:"A",status:"interrupted"})');
    assert.equal(h.run('currentPhase()'),'video');assert.equal(h.run('studioLocal("A").tool'),'conversation');passed++;
  }
  {
    // Same-story, same-version transition from an interrupted commission to a newly selected video model.
    const ui = harness();
    const shots = ["S001", "S002", "S003"];
    const spec = {asset_id:"spec", revision_id:"r1", sha256:"1".repeat(64)};
    const catalog = {script_ref:{...spec, asset_id:"script"}, spec};
    const cid = "c".repeat(32);
    const policy = {shot_ids:shots, max_repairs_per_shot:2, max_local_edits_per_shot:2};
    const frozen = {plan_id:"a".repeat(64), new_shots:shots, retake_shots:[], reuse_shots:[], acceptance_loop:policy};
    const route = {adapter:"litellm", provider:"volcengine-ark", account:"studio-ark", model_id:"doubao-seedance-2-0-mini-260615"};
    const next = {...frozen, plan_id:"b".repeat(64), new_shots:[], reuse_shots:shots,
      continuation:{request_id:cid, sha256:"2".repeat(64)}, video_routes:{"video.reference_to_video":route}};
    const execution = {commission_id:cid, status:"interrupted", resumable:true, script_ref:catalog.script_ref, spec_ref:spec, blockers:[], plan:frozen};
    ui.context.catalog = catalog;
    const render = (data) => {
      ui.context.snapshot = data;
      return ui.run('studioLocal("A").data=snapshot;studioVideoPlan(snapshot,catalog,studioLocal("A"))');
    };
    const findButton = (panel, label) => walk(panel).find(el => el.tagName === "button" && el.textContent.startsWith(label));
    const confirm = (data) => findButton(render(data), "确认新的制作计划");
    const displayed = {project_id:"A", video_execution:execution, video_plan:next};
    const details = render({...displayed, projection:"video", video_plan:null});
    assert.ok(findButton(details, "继续当前制作委托"));
    assert.ok(!findButton(details, "确认新的制作计划"));
    const navigation = [];
    ui.context.openProject = (id, options) => navigation.push({id, ...options});
    const inspect = findButton(details, "查看当前模型的拍摄计划");
    assert.ok(inspect, "A frozen commission offers read-only inspection of a new model's plan");
    await inspect.fire("click");
    assert.deepEqual(navigation, [{id:"A", preserveStep:true, full:true}]);
    assert.equal(ui.calls.length, 0);

    const frozenWith = (snapshot) => ({...execution, plan:{...execution.plan, video_routes:{"video.reference_to_video":snapshot}}});
    assert.ok(!confirm({...displayed, video_execution:frozenWith({...route})}), "Identical routes preserve the existing commission");
    assert.ok(!confirm({...displayed, video_plan:{...next, continuation:null}}), "Route changes alone cannot restart paid work");
    assert.ok(!confirm({...displayed, video_plan:{...next, continuation:{...next.continuation, request_id:"f".repeat(32)}}}),
      "An older commission's recovery evidence cannot authorize the current plan");
    const blocked = confirm({...displayed, video_plan:{...next, blocked_reason:"已有修复的供应商结果未知", unresolved_shots:["S002"]}});
    if (blocked) {
      assert.equal(blocked.disabled, true);
      await blocked.fire("click");
    }
    assert.equal(ui.calls.length, 0, "No paid request when repair outcomes are unresolved");
    assert.ok(confirm({...displayed, video_execution:frozenWith({...route, model_id:"earlier-video-model"})}),
      "A change between two explicit routes also requires a new confirmation");
    const partial = {proposal_id:"d".repeat(64), partial:{request_id:execution.commission_id, sha256:"e".repeat(64)},
      shot_ids:["S003"], reused_shots:["S001","S002"], reason:"S003 的原任务已确认失败"};
    const withPartial = (proposal) => render({...displayed, video_plan:{...next, continuation:null, extra_retake:proposal}});
    assert.ok(findButton(withPartial(partial), "同意额外重拍并制作"), "Terminal failure evidence can be reauthorized");
    const stalePartial = findButton(withPartial({...partial, partial:{...partial.partial, request_id:"f".repeat(32)}}), "同意额外重拍并制作");
    assert.ok(!stalePartial || stalePartial.disabled, "A partial receipt must belong to the displayed commission");
    const approval = confirm(displayed);
    assert.ok(approval && !approval.disabled, "A new plan can be approved for recovered media from a legacy commission");
    await approval.fire("click");
    assert.equal(ui.calls.length, 1);
    assert.deepEqual({url:ui.calls[0].url, method:ui.calls[0].method, body:ui.calls[0].body}, {
      url:"/api/projects/A/studio/video", method:"POST",
      body:{plan_id:next.plan_id, script_ref:catalog.script_ref, spec_ref:spec},
    });
    passed++;
  }
  console.log(`Unified video execution UI: ${passed} offline checks passed; no model or network calls.`);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
