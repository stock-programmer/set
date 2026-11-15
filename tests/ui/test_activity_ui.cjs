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
function harness() {
  const elements = new Map();
  const context = vm.createContext({
    console, URL, URLSearchParams, Date, Map, Set, JSON, Number, String, Object, Array, Promise, queueMicrotask,
    setTimeout() {}, setInterval() {}, location: { hash: "" },
    history: { replaceState: (_, __, hash) => { context.location.hash = hash; } },
    window: { location: { origin: "http://127.0.0.1:8765" }, crypto: { randomUUID: () => "offline-test" }, addEventListener() {} },
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
      asset_diagnostics:{project_id:"A",items:[
        {name:"张明",purpose:"portrait",reason:"缺少外貌设定。",trace_id:"a".repeat(32),span_id:"b".repeat(32)},
        {name:"书",purpose:"prop",reason:"未能核对封面。",trace_id:"javascript:alert(1)"}
      ]}}];renderJobs()`);
    assert.ok(h.list().includes("张明 · 定妆照") && h.list().includes("缺少外貌设定。") && h.list().includes("未能核对封面。"));
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
  console.log(`Activity UI: ${passed} offline behavior checks passed; no model or network calls.`);
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
