/* Offline production-activity behavior checks. No server, browser or model calls. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.textContent = ""; this.className = ""; this.classList = { toggle() {} }; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(kind, callback) { this.listeners[kind] = callback; }
  querySelectorAll(tag) { return walk(this).filter((element) => element !== this && element.tagName === tag); }
  showModal() { this.open = true; this.openCount = (this.openCount || 0) + 1; }
  close() { this.open = false; }
  focus() {}
  remove() {}
}
function walk(root) { return [root, ...root.children.flatMap((child) => typeof child === "object" ? walk(child) : [])]; }
function text(root) { return walk(root).map((element) => element.textContent).join("\n"); }
const source = fs.readFileSync(path.join(__dirname, "../../web/app.js"), "utf8").replace(/initialize\(\);\s*$/, "");
const projects = [{ id: "A", title: "钥匙故事" }, { id: "B", title: "红钥故事" }, { id: "C", title: "雪国故事" }];
const jobs = [
  { id: "a-video", kind: "generate", project_id: "A", status: "succeeded", message: "A 视频完成", created_at: "2026-11-04T10:00:00Z" },
  { id: "a-check", kind: "check", project_id: "A", status: "succeeded", message: "A 检查完成", created_at: "2026-11-04T10:01:00Z" },
  { id: "a-export", kind: "export", project_id: "A", status: "succeeded", message: "A 导出完成", created_at: "2026-11-04T10:02:00Z" },
  { id: "c-create", kind: "story", project_id: null, status: "succeeded", result: { project_id: "C" }, message: "C 创建完成", created_at: "2026-11-04T11:00:00Z" },
  { id: "failed-create", kind: "story", project_id: null, status: "failed", error: "未归属创建失败", created_at: "2026-11-04T11:10:00Z" },
  { id: "unbound-tool", kind: "check", project_id: null, status: "failed", error: "无归属工具错误", created_at: "2026-11-04T11:11:00Z" },
];
function harness(session = new Map()) {
  const elements = new Map();
  const context = vm.createContext({
    console, URL, URLSearchParams, Date, Map, Set, JSON, Number, String, Object, Array, Promise, queueMicrotask,
    setTimeout() {}, setInterval() {}, location: { hash: "" },
    history: { replaceState: (_, __, hash) => { context.location.hash = hash; } },
    window: { location: { origin: "http://127.0.0.1:8765" }, crypto: { randomUUID: () => "offline-test" }, addEventListener() {},
      sessionStorage:{getItem:key=>session.get(key),setItem:(key,value)=>session.set(key,value)} },
    document: { body: new Element("body"), activeElement: null, createElement: (tag) => new Element(tag), querySelectorAll: () => [],
      getElementById: (id) => { if (!elements.has(id)) elements.set(id, new Element("div")); return elements.get(id); } },
    fetch: () => { throw new Error("Offline activity tests must not perform network requests"); },
  });
  vm.runInContext(source, context, { filename: "web/app.js" });
  const run = (script) => vm.runInContext(script, context);
  run(`state.projects=${JSON.stringify(projects)}; state.jobs=${JSON.stringify(jobs)}; state.config={}; state.loading=false; state.projectId="A"; state.project={id:"A",title:"钥匙故事",shots:[]};
    renderProjects=()=>{}; renderHeader=()=>{}; renderStage=()=>{}; renderConfig=()=>{};`);
  return { run, context, elements, list: () => text(elements.get("job-list")), intro: () => text(elements.get("activity-intro")), count: () => elements.get("active-job-count").textContent };
}

async function main() {
  let passed = 0;
  const id = "c".repeat(32);
  const active = {id,project_id:"A",kind:"asset_production",status:"failed"};
  const prompt = {id:"r1",project_id:"A",kind:"ark_access",title:"提供可用的接入点",message:"旁路已核对当前账号。",
    fields:[{name:"endpoint_id",type:"text",label:"接入点 ID",required:false},
      {name:"confirm",type:"confirm",label:"已开通权限",required:false}],submit_label:"重新核对并继续"};
  const setPrompt = (h,p=prompt) => h.run(`state.jobs=[${JSON.stringify(active)}];
    feedbackJobs['${id}']={status:"needs_input",running:true,human_request:${JSON.stringify(p)}};renderJobs()`);
  const click = (root,label) => walk(root).find(el => el.textContent === label).listeners.click();
  const submit = dialog => walk(dialog).find(el => el.tagName === "form").listeners.submit({preventDefault(){}});
  {
    const h=harness();
    const source={...active, kind:"studio_video",message_id:"frozen"};
    const review={id:"review",kind:"studio_review",project_id:"A",status:"queued",review_request_id:"frozen"};
    const question={...prompt,kind:"review_commission_shots",commission_id:"frozen",fields:[],title:"已有镜头需要审看"};
    h.run(`const studioViews=new Map([["A",{data:{project_id:"A",video_execution:{commission_id:"frozen"}}}]]);
      state.jobs=[${JSON.stringify(source)},${JSON.stringify(review)}];
      feedbackJobs['${id}']={status:"needs_input",running:true,human_request:${JSON.stringify(question)}};renderJobs()`);
    const dialog=h.elements.get("asset-intervention-dialog");
    assert.ok(!dialog.open);
    assert.match(h.list(),/自动复验已排队/);
    h.run('state.jobs[1].status="running";renderJobs()');
    assert.match(h.list(),/正在自动复验/);
    h.run('state.jobs[1].review_request_id="another-order";renderJobs()');
    assert.ok(dialog.open);
    h.run('state.jobs[1].review_request_id="frozen";renderJobs()');
    assert.equal(dialog.open,false);
    h.run('state.jobs[1].status="succeeded";renderJobs()');
    assert.equal(h.elements.get("asset-intervention").hidden,false);
    passed++;
  }
  {
    const session = new Map(), h = harness(session);
    h.run(`const studioViews=new Map([['A',{data:{asset_work:{items:[{status:'needs_attention',
      diagnostic:{code:'image_model_access'}}]}}}]]);state.jobs=[${JSON.stringify(active)}];
      feedbackJobs['${id}']={status:'needs_input',dependency:{human_intervention:{kind:'reference_image_model_access'}}};
      renderJobs()`);
    const dialog = h.elements.get("asset-intervention-dialog"), card = h.elements.get("asset-intervention");
    assert.equal(card.hidden,true); assert.ok(!dialog.open);
    h.run(`feedbackJobs['${id}']={status:'handling',human_request:${JSON.stringify(prompt)},progress_message:'正在查询方舟模型'};
      state.jobRenderSignature=null;renderJobs()`);
    assert.equal(card.hidden,true);assert.ok(!dialog.open);
    assert.match(h.run(`feedbackText(feedbackJobs['${id}'])`),/正在查询方舟模型/);
    setPrompt(h,{...prompt,project_id:"B"});assert.ok(!dialog.open);
    setPrompt(h);assert.equal(dialog.open,true);assert.match(text(dialog),/旁路已核对/);
    click(dialog,"稍后处理");h.run("renderJobs()");assert.equal(dialog.open,false);assert.equal(card.hidden,false);
    const reload=harness(session);setPrompt(reload);assert.ok(!reload.elements.get("asset-intervention-dialog").open);
    const observer=harness();observer.context.window.pianchangFeedbackObserver=true;setPrompt(observer);
    assert.equal(observer.elements.get("asset-intervention").hidden,false);assert.ok(!observer.elements.get("asset-intervention-dialog").open);
    click(card,"回应请求");assert.ok(dialog.open);h.run("state.projectId='B';renderJobs()");assert.equal(dialog.open,false);
    setPrompt(h,{...prompt,id:"r2"});h.run("state.projectId='A';renderJobs()");assert.equal(dialog.open,true);
    passed++;
  }
  {
    const h = harness(); setPrompt(h);
    const dialog=h.elements.get("asset-intervention-dialog");
    const controls=walk(dialog).filter(el=>el.tagName==="input");
    controls[0].value="  ep-offline  ";controls[1].checked=false;
    let resolvePost, rejected=false;
    const writes=[];
    h.context.call = async (path,options) => {
      if (options?.method === "POST") {
        writes.push([path,JSON.parse(JSON.stringify(options.body))]);
        if (!rejected) { rejected=true; throw Error("接入点暂不可用"); }
        return new Promise(resolve=>{ resolvePost=resolve; });
      }
      return {jobs:{[id]:{status:"needs_input",human_request:prompt}}};
    };
    h.run("request=call");
    await submit(dialog); assert.equal(dialog.open,true); assert.equal(controls[0].value,"  ep-offline  ");
    assert.match(text(dialog),/接入点暂不可用/);
    const pending=submit(dialog);await submit(dialog);
    assert.equal(writes.length,2);
    assert.deepEqual(writes[1],["/api/projects/A/feedback-input",{job_id:id,request_id:"r1",answers:{endpoint_id:"ep-offline",confirm:false}}]);
    assert.ok(walk(dialog).find(el=>el.type==="submit").disabled);
    resolvePost({accepted:true});await pending;
    assert.equal(dialog.open,false);assert.equal(h.elements.get("asset-intervention").hidden,true);
    await h.run("refreshFeedbackJobs()");assert.ok(!dialog.open);
    setPrompt(h,{...prompt,id:"r2",fields:[{name:"model_id",type:"select",label:"选择模型",
      options:[{value:"m1",label:"模型一"},{value:"m2",label:"模型二"}]}]});
    assert.ok(dialog.open);const select=walk(dialog).find(el=>el.tagName==="select");assert.ok(select.required);
    assert.deepEqual(select.children.map(el=>el.value),["","m1","m2"]);select.value="m2";
    const choice=submit(dialog);resolvePost({accepted:true});await choice;
    assert.deepEqual(writes[2][1].answers,{model_id:"m2"});
    passed++;
  }
  {
    const h=harness();
    h.run(`state.calls=[];setStep=async step=>state.calls.push(step);currentPhase=()=>"video";
      const data={project_id:"A",assets:{spec:{revision_id:"spec-1"}},video_execution:{commission_id:"frozen"}};
      const studioViews=new Map([['A',{data}]]);var studioLocal=()=>studioViews.get("A");
      var studioReadShotReviews=async (pid,cid)=>state.calls.push([pid,cid]);`);
    setPrompt(h,{...prompt,kind:"confirm_video_plan",spec_revision:"spec-1",fields:[],submit_label:"查看拍摄计划"});
    const dialog=h.elements.get("asset-intervention-dialog");
    click(dialog,"查看拍摄计划"); await Promise.resolve();
    assert.deepEqual(Array.from(h.run("state.calls")),["video"]);
    h.run('data.assets.spec.revision_id="spec-2";renderJobs()');
    assert.equal(h.elements.get("asset-intervention").hidden,true);
    h.run('data.projection="video"');
    setPrompt(h,{...prompt,id:"r2",kind:"review_retake_limit",commission_id:"frozen",fields:[],submit_label:"审看镜头"});
    click(dialog,"审看镜头"); await Promise.resolve();await Promise.resolve();
    assert.deepEqual(JSON.parse(h.run("JSON.stringify(state.calls)")),["video","video",["A","frozen"]]);
    const panel=new Element("section"), current=new Element("article");
    panel.dataset.requestId="frozen";
    current.dataset={shotId:"S001",mediaSha:"sha",reportSha:"report"};
    panel.append(current);current.scrollIntoView=()=>{current.scrolled=true;};
    panel.scrollIntoView=()=>{panel.scrolled=true;};
    h.context.document.querySelectorAll=selector=>selector===".studio-shot-approvals"?[panel]:[];
    await h.run('openFeedbackDestination({pid:"A",prompt:{project_id:"A",commission_id:"frozen",kind:"inspect_commission_reports",review_shots:[{shot_id:"S001",media_sha256:"sha",report_sha256:"report"}]}})');
    assert.equal(current.scrolled,true);
    current.scrolled=false;
    await h.run('openFeedbackDestination({pid:"A",prompt:{project_id:"A",commission_id:"frozen",kind:"inspect_commission_reports",review_shots:[{shot_id:"S001",media_sha256:"stale",report_sha256:"report"}]}})');
    assert.equal(current.scrolled,false);
    assert.equal(panel.scrolled,true);
    click(h.elements.get("asset-intervention"),"回应请求");assert.ok(dialog.open);
    h.run('delete data.projection;data.video_execution.commission_id="newer";renderJobs()');assert.equal(dialog.open,false);
    assert.equal(h.elements.get("asset-intervention").hidden,true);
    passed++;
  }
  {
    const h = harness(), row = new Element("article");
    row.dataset = {assetJobId:"recovery",subjectId:"prop:key",purpose:"prop",revisionId:"img-2",
      assetId:"key",candidateSha:"image-hash",reviewSha:"report-hash"};
    const evidence = new Element("details");
    row.querySelector = selector => selector === "details" ? evidence : null;
    row.focus = () => {row.focused=true;}; row.scrollIntoView = () => {row.scrolled=true;};
    h.context.document.querySelectorAll = selector => selector === ".studio-asset-problem" ? [row] : [];
    h.run(`const unit={job_id:'recovery',subject_id:'prop:key',purpose:'prop',candidate:{asset_id:'key',revision_id:'img-2',sha256:'image-hash'},
        status:'needs_attention',review:{status:'FAIL',review_ref:{sha256:'report-hash'}}};
      const data={project_id:'A',asset_work:{items:[unit]}}, view={data:{project_id:'A',projection:'video'}};
      const studioViews=new Map([['A',view]]);
      var studioLocal=()=>view;currentPhase=()=>'assets';setStep=async step=>{if(step!=='assets')throw Error(step);view.data=data;};`);
    setPrompt(h,{...prompt,id:"review-1",kind:"review_asset_candidates",candidate_job_id:"recovery",
      items:[{subject_id:"prop:key",purpose:"prop",revision_id:"img-2"}],fields:[],submit_label:"查看待审参考图"});
    const dialog=h.elements.get("asset-intervention-dialog"), card=h.elements.get("asset-intervention");
    assert.ok(dialog.open);click(dialog,"查看待审参考图");await Promise.resolve();await Promise.resolve();
    assert.ok(row.focused && row.scrolled && evidence.open);
    h.run("unit.candidate.revision_id='img-3';renderJobs()");assert.equal(card.hidden,true);
    h.run("unit.candidate.revision_id='img-2';unit.job_id='different-job';renderJobs()");assert.equal(card.hidden,true);
    h.run("unit.job_id='recovery';unit.status='approved';renderJobs()");assert.equal(card.hidden,true);
    passed++;
    h.run("unit.status='needs_attention';");
    const target = {subject_id:"prop:key",purpose:"prop",asset_id:"key",revision_id:"img-2",
      sha256:"image-hash",review_ref:{sha256:"report-hash"}};
    const afterRecheck = {...prompt,id:"review-after-recheck",kind:"review_asset_candidates",fields:[],submit_label:"查看待审参考图",
      items:[{...target,subject_id:"prop:other",candidate_job_id:"first-job"},{...target,candidate_job_id:"recovery"}]};
    setPrompt(h,afterRecheck); assert.ok(dialog.open && !card.hidden);
    row.focused = false; click(dialog,"查看待审参考图"); await Promise.resolve(); await Promise.resolve(); assert.ok(row.focused);
    for (const mismatch of [{candidate_job_id:"first-job"},{asset_id:"another"},{revision_id:"superseded"},
        {sha256:"different-bytes"},{review_ref:{sha256:"new-report"}}]) {
      setPrompt(h,{...afterRecheck,candidate_job_id:"recovery",items:[{...target,candidate_job_id:"recovery",...mismatch}]});
      assert.ok(card.hidden);
    }
    passed++;
    const revisionRequest = {...afterRecheck,id:"review-authorize",message:"本次最多修订 1 张，生成和核验可能计费。",
      items:[{...target,candidate_job_id:"recovery"}],navigation_label:"查看候选图与报告",submit_label:"授权并修订本批候选",
      fields:[{name:"authorize_revision",type:"confirm",label:"授权本次批量修订"}]};
    const posted=[];
    let finishPost;
    h.context.revisionApi = async(path,options) => {
      if (options?.method === "POST") {
        posted.push([path,JSON.parse(JSON.stringify(options.body))]);
        return new Promise(resolve=>{finishPost=resolve;});
      }
      return {jobs:{[id]:{status:"needs_input",human_request:revisionRequest}}};
    };
    h.run("request=revisionApi");
    setPrompt(h,revisionRequest);
    const auth = () => walk(dialog).find(el=>el.type==="checkbox");
    const submitButton = () => walk(dialog).find(el=>el.type==="submit");
    assert.ok(dialog.open && submitButton().disabled && auth().checked===false);
    assert.match(text(dialog),/最多修订 1 张.*可能计费/);
    await submit(dialog);assert.equal(posted.length,0);
    auth().checked=true;auth().listeners.change();assert.ok(!submitButton().disabled);
    click(dialog,"查看候选图与报告");await Promise.resolve();await Promise.resolve();assert.equal(posted.length,0);
    click(card,"回应请求");assert.ok(!auth().checked && submitButton().disabled);
    auth().checked=true;click(dialog,"稍后处理");assert.equal(posted.length,0);
    click(card,"回应请求");assert.equal(auth().checked,false);
    auth().checked=true;auth().listeners.change();
    const receipt=submit(dialog);await submit(dialog);
    assert.deepEqual(posted,[["/api/projects/A/feedback-input",{job_id:id,request_id:"review-authorize",answers:{authorize_revision:true}}]]);
    finishPost({status:"accepted"});await receipt;assert.ok(!dialog.open && card.hidden);
    passed++;
  }
  {
    const h=harness(), story="1".repeat(32), job="2".repeat(32), other="3".repeat(32);
    const prompt={id:"creative-1",project_id:null,story_job_id:story,job_id:job,kind:"script_creative_input",
      title:"主角的名字是什么？",message:"需要沿用你指定的名字。",fields:[{type:"text",name:"answer",label:"主角姓名",max_length:500}]};
    h.run(`state.storyDraftId='${story}';state.projectId=null;
      state.jobs=[{id:'${job}',story_job_id:'${story}',kind:'pi_script',status:'interrupted'}];
      var agentChatCurrent={scope:{story:'${story}'},view:{role:'script'}};renderJobs();`);
    const writes=[];
    let info={status:"needs_input",human_request:prompt};
    h.context.fakeApi=async(path,options)=>{
      if (!options) {assert.equal(path,"/api/feedback-watch?job="+job);return {jobs:{[job]:info}};}
      writes.push([path,JSON.parse(JSON.stringify(options.body))]);info={status:"handling",progress_message:"剧本 Agent 正在继续"};
      return {status:"accepted",request_id:options.body.request_id};
    };
    await h.run("request=fakeApi;refreshFeedbackJobs()");
    const dialog=h.elements.get("asset-intervention-dialog"),card=h.elements.get("asset-intervention");
    assert.ok(dialog.open);assert.match(text(dialog),/主角的名字是什么/);
    const reply=walk(dialog).find(el=>el.name==="answer");assert.equal(reply.maxLength,500);reply.value="柏安";
    await submit(dialog);assert.deepEqual(writes[0],["/api/story-drafts/"+story+"/feedback-input",
      {job_id:job,request_id:"creative-1",answers:{answer:"柏安"}}]);
    assert.equal(dialog.open,false);
    const second={...prompt,id:"creative-2",project_id:"A",fields:[{type:"select",name:"answer",label:"你指定的结局",
      options:[{value:"重逢",label:"重逢"},{value:"离别",label:"离别"}]}]};
    h.run(`state.jobs[0].project_id='A';feedbackJobs['${job}']={status:'needs_input',human_request:${JSON.stringify(second)}};
      agentChatCurrent.view.role='video';renderJobs();`);assert.ok(!dialog.open);
    h.run(`agentChatCurrent.view.role='script';state.storyDraftId='${other}';renderJobs();`);assert.ok(!dialog.open);
    h.run(`state.storyDraftId='${story}';state.jobs[0].kind='pi_video';renderJobs();`);assert.ok(!dialog.open);
    h.run(`state.jobs[0].kind='pi_script';feedbackJobs['${job}'].human_request.story_job_id='${other}';renderJobs();`);assert.ok(!dialog.open);
    h.run(`feedbackJobs['${job}'].human_request.story_job_id='${story}';window.pianchangFeedbackObserver=true;renderJobs();`);
    assert.equal(card.hidden,false);assert.ok(!dialog.open);
    click(card,"回应请求");walk(dialog).find(el=>el.tagName==="select").value="重逢";await submit(dialog);
    assert.deepEqual(writes[1],["/api/projects/A/feedback-input",{job_id:job,request_id:"creative-2",answers:{answer:"重逢"}}]);
    passed++;
  }
  {
    const h = harness(), jobId = "f".repeat(32);
    h.context.document.querySelectorAll = selector => selector === "[data-feedback-job]" ?
      walk(h.elements.get("job-list")).filter(el => el.dataset.feedbackJob) : [];
    let finish;
    h.context.readFeedback = () => new Promise(resolve => { finish = resolve; });
    h.run(`state.jobs=[{id:"${jobId}",kind:"studio_video",project_id:"A",status:"failed",error:"参考素材缺失"}];
      request=readFeedback; renderJobs();`);
    const pending = h.run("refreshFeedbackJobs()");
    h.run("renderJobs()");
    finish({jobs:{[jobId]:{running:true,status:"awaiting_evidence",triage:{status:"reported"},
      dependency:{missing_count:1,requires_plan_reconfirmation:true,
        transport_failure:{request_kind:"audio",dispatch:"not_sent"},connectivity:{status:"http_response"},
        missing_references:[{shot_id:"S001",subject_id:"char:librarian",purpose:"voice"}]}}}});
    await pending;
    assert.ok(h.list().includes("已收到 Codex 的故障分析"));
    h.run("renderJobs()");
    assert.ok(h.list().includes("仍缺 1 项") && h.list().includes("新版制作计划待确认"));
    assert.ok(h.list().includes("S001 / char:librarian / 声音"));
    assert.ok(h.list().includes("已核实未发送") && h.list().includes("当前语音线路可达"));
    passed++;
  }
  {
    const h = harness();
    h.run(`const studioViews=new Map(), proposal={proposal_id:'p1',shot_ids:['S003'],reason:'交接动作未出现'};
      studioViews.set('A',{data:{video_plan:{extra_retake:proposal}}});renderJobs();`);
    assert.ok(h.list().includes("S003 · 交接动作未出现") && h.list().includes("查看额外重拍计划"));
    h.run('state.projectId="B";renderJobs()');
    assert.ok(!h.list().includes("额外重拍"));
    h.run('state.projectId="A";studioViews.get("A").data.video_plan.extra_retake=null;renderJobs()');
    assert.ok(!h.list().includes("额外重拍"));
    passed++;
  }
  {
    const h = harness();
    h.run(`const studioViews=new Map(), pin={asset_id:'A/book',revision_id:'r2',sha256:'c'.repeat(64)},
      proof={asset_id:'A/review',revision_id:'r1',sha256:'f'.repeat(64)};
      state.jobs=[{id:'batch',kind:'asset_production',project_id:'A',status:'failed',message:'3 项待处理。',
        asset_diagnostics:{project_id:'A',items:[{name:'书',purpose:'prop',candidate:pin,review_ref:proof}]}}];
      renderJobs();`);
    assert.ok(h.list().includes("任务结束时：3 项待处理。") && !h.list().includes("已人工通过"));
    h.run(`const selection={selected_pin:pin,status:'approved',approval:{review_ref:proof,selection_mode:'human'}};
      studioViews.set('A',{data:{asset_work:{items:[selection]}}});renderJobs();`);
    assert.ok(h.list().includes("当前：r2 已人工通过") && h.list().includes("任务失败"));
    h.run(`selection.approval.review_ref={...proof,revision_id:'r2'};renderJobs();`);
    assert.ok(!h.list().includes("已人工通过"));
    passed++;
  }
  {
    const h = harness();
    h.run(`state.jobs = [{id:"batch",kind:"asset_production",project_id:"A",status:"failed",message:"2 项待处理。",
      asset_diagnostics:{project_id:"A",summary:"2 项待处理。上次生图时服务商返回 HTTP 404。",items:[
        {name:"张明",purpose:"portrait",reason:"缺少外貌设定。",trace_id:"a".repeat(32),span_id:"b".repeat(32)},
        {name:"书",purpose:"prop",reason:"未能核对封面。",trace_id:"javascript:alert(1)"}
      ]}}];renderJobs()`);
    assert.ok(h.list().includes("张明 · 定妆照") && h.list().includes("缺少外貌设定。") && h.list().includes("未能核对封面。"));
    assert.ok(h.list().includes("任务结束时：2 项待处理。上次生图时服务商返回 HTTP 404。"));
    const detailLinks = walk(h.elements.get("job-list")).filter(n=>n.tagName==="a");
    assert.deepEqual(detailLinks.map(n=>n.href),["/#step=traces&trace="+"a".repeat(32)+"&span="+"b".repeat(32)]);
    h.run('state.jobs[0].asset_diagnostics={project_id:"B",items:[{name:"其他故事的问题"}]};renderJobs()');
    assert.ok(!h.list().includes("其他故事的问题") && !h.list().includes("张明"));
    passed++;
  }
  {
    const h = harness();
    h.run(`state.jobs = [
      {id:"old",kind:"asset_production",project_id:"A",status:"succeeded",message:"7 项待人工确认"},
      {id:"now",kind:"asset_production",project_id:"A",status:"succeeded",message:"已选用 7 项"},
      {id:"other",kind:"check",project_id:"A",status:"succeeded",message:"剪辑待人工确认"},
      {id:"active",kind:"asset_inspect",project_id:"A",status:"running",message:"测试：待人工确认"}
    ]; renderJobs()`);
    assert.ok(h.list().includes("旧流程记录（当时）：7 项待人工确认") && h.list().includes("当前选用状态见「共享资产」"));
    assert.equal(h.list().split("旧流程记录").length-1,1);
    assert.equal(h.run('state.jobs[0].message'),"7 项待人工确认");
    passed++;
  }
  {
    const h = harness(); h.run("renderJobs()");
    assert.equal(h.elements.get("job-list").children.length, 3);
    assert.ok(h.list().includes("A 视频完成") && h.list().includes("A 导出完成"));
    assert.ok(!h.list().includes("C 创建完成") && !h.list().includes("未归属创建失败"));
    assert.ok(h.intro().includes("钥匙故事")); passed++;
    h.run('state.projectId="B"; renderJobs()');
    assert.ok(h.list().includes("这个故事还没有制作任务"));
    assert.ok(!h.list().includes("A 视频完成")); assert.ok(h.intro().includes("红钥故事")); passed++;
    h.run('state.projectId="C"; renderJobs()');
    assert.equal(h.elements.get("job-list").children.length, 1);
    assert.ok(h.list().includes("C 创建完成")); assert.ok(h.intro().includes("雪国故事")); passed++;
  }
  {
    const h = harness();
    const savedJobs = h.run("JSON.stringify(state.jobs)");
    h.run("renderJobs(); startNew()");
    assert.equal(h.run("state.projectId"), "A", "Fixture retains the previous project, as the real new-story page does");
    assert.ok(h.list().includes("新故事还没有制作任务"));
    for (const job of jobs) assert.ok(!h.list().includes(job.message || job.error));
    assert.ok(h.intro().includes("本次创作")); assert.equal(h.count(), "0");
    assert.equal(h.run("JSON.stringify(state.jobs)"), savedJobs, "Starting a new story preserves historical jobs");
    h.run('state.creating=false; renderJobs()');
    assert.ok(h.list().includes("A 视频完成"), "Returning to the original story restores its activity"); passed++;
  }
  {
    const h = harness();
    h.run('state.jobs=[{id:"a",kind:"generate",project_id:"A",status:"running"},{id:"c",kind:"check",project_id:"C",status:"queued"},{id:"new",kind:"story",status:"running"}]; state.projectId="C"; renderJobs()');
    assert.equal(h.count(), "1");
    h.run('state.projectId="B"; renderJobs()'); assert.equal(h.count(), "0");
    h.run('state.creating=true; renderJobs()'); assert.equal(h.count(), "0"); passed++;
  }
  {
    const h = harness();
    h.run('state.jobs=[...Array.from({length:20},(_,i)=>({id:"foreign"+i,kind:"generate",project_id:"C",status:"running",created_at:"2026-11-05T00:00:00Z"})),...state.jobs]; renderJobs()');
    assert.equal(h.elements.get("job-list").children.length, 3);
    assert.ok(h.list().includes("A 检查完成")); assert.equal(h.count(), "0"); passed++;
    h.run('state.jobs=Array.from({length:15},(_,i)=>({id:"own"+i,kind:"generate",project_id:"A",status:"running",message:"own",created_at:"2026-11-05T00:00:00Z"})); renderJobs()');
    assert.equal(h.elements.get("job-list").children.length, 12);
    assert.equal(h.count(), "15", "Activity count includes jobs outside the 12-card display window"); passed++;
  }
  {
    const h = harness(); let complete;
    h.context.pendingProject = new Promise((resolve) => { complete = resolve; });
    h.run("api.project=()=>pendingProject; renderJobs()");
    const loading = h.run('openProject("C")');
    assert.equal(h.run("state.loading"), true);
    assert.ok(h.list().includes("C 创建完成") && !h.list().includes("A 视频完成"), "Switch activity before the project response arrives");
    complete({ id: "C", title: "雪国故事", shots: [] }); await loading;
    assert.ok(h.list().includes("C 创建完成")); passed++;
  }
  {
    const h = harness(); const completions = {};
    h.context.loadProject = (id) => new Promise((resolve) => { completions[id] = resolve; });
    h.run("api.project=loadProject");
    const first = h.run('openProject("C")'); const second = h.run('openProject("B")');
    completions.C({ id: "C", title: "雪国故事", shots: [] }); await first;
    assert.equal(h.run("state.projectId"), "B"); assert.ok(!h.list().includes("C 创建完成"));
    completions.B({ id: "B", title: "红钥故事", shots: [] }); await second;
    assert.ok(h.list().includes("这个故事还没有制作任务")); passed++;
  }
  {
    const h = harness();
    h.run(`state.jobs=[{id:"own",kind:"generate",project_id:"A",status:"running",message:"本故事生成中"},{id:"foreign",kind:"generate",project_id:"C",status:"running"}];
      api.jobs=async()=>({jobs:[{id:"own",kind:"generate",project_id:"A",status:"running",message:"本故事生成中"},{id:"foreign",kind:"generate",project_id:"C",status:"failed",error:"别的故事失败"}]});
      api.bootstrap=async()=>({projects:state.projects,config:{}}); api.project=async(id)=>({id,title:"钥匙故事",shots:[]});`);
    await h.run("poll()");
    assert.equal(h.elements.get("toast-stack")?.children.length || 0, 0);
    assert.ok(h.list().includes("本故事生成中") && !h.list().includes("别的故事失败")); assert.equal(h.count(), "1"); passed++;
    h.run('api.jobs=async()=>({jobs:[{id:"own",kind:"generate",project_id:"A",status:"failed",error:"本故事失败"}]})');
    await h.run("poll()");
    assert.ok(text(h.elements.get("toast-stack")).includes("本故事失败")); assert.equal(h.count(), "0"); passed++;
  }
  {
    const h = harness();
    h.run(`state.creating=true; state.jobs=[{id:"tool",kind:"generate",project_id:"A",status:"running"},{id:"new",kind:"story",status:"running"}];
      api.jobs=async()=>({jobs:[{id:"tool",kind:"generate",project_id:"A",status:"failed",error:"别的工程视频失败"},{id:"new",kind:"story",status:"failed",error:"新建故事失败"}]});
      api.bootstrap=async()=>({projects:state.projects,config:{}});`);
    await h.run("poll()");
    assert.equal(h.elements.get("toast-stack")?.children.length || 0, 0, "Blank new stories receive no other story's notifications");
    assert.ok(h.list().includes("新故事还没有制作任务"));
    assert.ok(!h.list().includes("新建故事失败") && !h.list().includes("别的工程视频失败")); passed++;
  }
  {
    const h = harness(), draftId = "a".repeat(32);
    h.run(`state.creating=false; state.storyDraftId="${draftId}"; state.projectId=null;
      state.jobs=[{id:"${draftId}",kind:"story",status:"running"},{id:"older",kind:"story",status:"running"}];
      api.jobs=async()=>({jobs:[{id:"${draftId}",kind:"story",status:"failed",error:"本次故事失败"},{id:"older",kind:"story",status:"failed",error:"旧草稿失败"}]});
      api.storyDraft=async()=>({id:"${draftId}",title:"本次新故事",status:"failed"});
      api.bootstrap=async()=>({projects:state.projects,config:{}});`);
    await h.run("poll()");
    const notifications = text(h.elements.get("toast-stack"));
    assert.ok(notifications.includes("本次故事失败") && !notifications.includes("旧草稿失败"));
    assert.ok(h.list().includes("本次故事失败") && !h.list().includes("旧草稿失败"));
    assert.ok(h.intro().includes("本次新故事")); passed++;
  }
  {
    const h = harness();
    h.run(`state.jobs=[{id:"1".repeat(32),kind:"studio_video",project_id:"A",message_id:"commission",status:"failed",error:"已有镜头素材 0 / 3"},
      {id:"2".repeat(32),kind:"pi_video",project_id:"A",commission_job_id:"1".repeat(32),commission_id:"commission",status:"running",created_at:"2026-11-22T08:00:00Z",message:"正在验收已有片段"}];
      const metrics={commission_id:"commission",last_execution:{id:"2".repeat(32)},usage:{reserved_video_generations:2,model_calls:7},limits:{max_video_generations:6,max_model_calls:47}},
        studioViews=new Map([["A",{data:{video_execution:metrics}}]]);
      renderJobs()`);
    assert.match(h.list(),/上次执行记录/);
    assert.match(h.list(),/同一委托的最新续做：进行中 · 正在验收已有片段/);
    assert.match(h.list(),/查看续做进度与视频 Agent 记录/);
    assert.match(h.list(),/已预留视频生成 2 \/ 6 次/);
    h.run('metrics.usage.reserved_video_generations=3;renderJobs()');
    assert.match(h.list(),/已预留视频生成 3 \/ 6 次/);
    const go = walk(h.elements.get("job-list")).find(el=>el.textContent==="查看续做进度与视频 Agent 记录");
    h.run('globalThis.visits=[];setStep=step=>visits.push(step)');
    go.listeners.click();
    h.run('state.projectId="B"');
    go.listeners.click();
    assert.deepEqual([...h.run("visits")],["agent"]);
    h.run('state.projectId="A"');
    h.run(`state.jobs[1].status="succeeded";state.jobs[1].result={task_complete:false,action_required:true};renderJobs()`);
    assert.match(h.list(),/同一委托的最新续做：等待处理/);
    assert.match(h.list(),/制作未完成/);
    assert.equal(h.run("isFeedbackJob(state.jobs[1])"),true);
    h.run('state.jobs[1].commission_id="unrelated";state.jobs[1].updated_at="later";renderJobs()');
    assert.doesNotMatch(h.list(),/同一委托的最新续做/);
    const matches = h.run(`(() => {const prompt={project_id:"A",kind:"confirm_commission_budget",commission_id:"c",plan_id:"p",proposal_id:"t"},
      data={project_id:"A",video_execution:{commission_id:"c"},video_plan:{plan_id:"p",extra_retake:{proposal_id:"t"}}};
      return [feedbackScopeMatches(prompt,data),feedbackScopeMatches({...prompt,proposal_id:"stale"},data)];})()`);
    assert.deepEqual([...matches],[true,false]);
    passed++;
  }
  console.log(`Activity UI: ${passed} offline behavior checks passed; no model or network calls.`);
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
