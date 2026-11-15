/* Offline story-draft visibility and navigation checks. No model/network calls. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.listeners={}; this.attributes={}; this.dataset={}; this.textContent=""; this.value=""; this.classList={toggle(){}}; }
  set innerHTML(_) { throw new Error("Untrusted story data must stay text"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  setAttribute(key,value) { this.attributes[key]=value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  async fire(name) { return this.listeners[name]?.({preventDefault(){}}); }
  querySelectorAll(tag) { return walk(this).filter(item=>item!==this && item.tagName===tag); }
  focus() {} remove() {}
}
function walk(element) { return [element,...element.children.flatMap(child=>typeof child==="object" ? walk(child):[])]; }
function text(element) { return walk(element).map(item=>item.textContent).join("\n"); }
const source=fs.readFileSync(path.join(__dirname,"../../web/studio.js"),"utf8")+"\n"+fs.readFileSync(path.join(__dirname,"../../web/app.js"),"utf8").replace(/initialize\(\);\s*$/,"");
const id="a".repeat(32), other="b".repeat(32), trace="c".repeat(32);
const projects=[{id:"A",title:"钥匙故事"},{id:"B",title:"红钥"},{id:"C",title:"雪国故事"}];
const draft={id,title:"结婚当天新娘逃跑",idea:"结婚当天新娘逃跑",status:"failed",error:"节拍数量不符",trace_id:trace,project_id:null,
  inputs:{idea:"结婚当天新娘逃跑",episodes:2,scenes:3,beats:4},draft:{title:"婚礼",outline:"<img src=x onerror=alert(1)>"},validation:{ready_for_generation:false},
  artifacts:[{name:"草稿",url:"/assets/draft"},{name:"恶意链接",url:"https://evil.invalid/key"}],job:{id,kind:"story",status:"failed",project_id:null,error:"节拍数量不符",trace_id:trace}};
function harness() {
  const elements=new Map(),calls=[];
  const context=vm.createContext({console,URL,URLSearchParams,Date,Map,Set,JSON,Number,String,Object,Array,Promise,queueMicrotask,
    setTimeout(){},setInterval(){},location:{hash:""},history:{replaceState:(_,__,hash)=>{context.location.hash=hash;}},
    window:{location:{origin:"http://127.0.0.1:8765"},crypto:{randomUUID:()=>"offline-draft"},addEventListener(){}},
    document:{body:new Element("body"),activeElement:null,createElement:tag=>new Element(tag),querySelectorAll:()=>[],getElementById:id=>{
      const mounted=[...elements.values()].flatMap(walk).find(item=>item.id===id);if(mounted)return mounted;
      if(!elements.has(id))elements.set(id,new Element("div"));return elements.get(id);
    }},fetch:async(url,options)=>{calls.push({url,options});throw new Error("Unexpected network request");}
  });
  vm.runInContext(source,context,{filename:"web/app.js"});
  const run=script=>vm.runInContext(script,context);
  run('studioRefresh=async()=>{};renderStudio=()=>node("div");'); // This suite isolates draft routing; stage content is tested separately.
  run(`state.projects=${JSON.stringify(projects)};state.storyDrafts=[${JSON.stringify(draft)}];state.jobs=[${JSON.stringify(draft.job)}];state.config={key_configured:true};state.loading=false;state.projectId="A";state.project={id:"A",title:"钥匙故事",shots:[]};
    api.storyDraft=async()=>(${JSON.stringify(draft)});api.project=async(id)=>({id,title:"完成工程",shots:[]});api.bootstrap=async()=>({projects:state.projects,story_drafts:state.storyDrafts,config:state.config});`);
  return {run,context,elements,calls};
}
async function main() {
 let passed=0;
 {
  const h=harness();h.run("renderProjects()");
  assert.equal(h.elements.get("project-list").children.length,4);assert.equal(h.elements.get("project-count").textContent,"04");
  assert.ok(text(h.elements.get("project-list")).includes("结婚当天新娘逃跑") && text(h.elements.get("project-list")).includes("生成失败 · 待修复"));passed++;
  await h.run(`openStoryDraft("${id}")`);
  assert.equal(h.run("state.projectId"),null);assert.equal(h.run("state.project"),null);assert.equal(h.elements.get("workflow-tabs").hidden,false);
  assert.ok(h.context.location.hash.includes(`story=${id}`));assert.ok(!h.context.location.hash.includes("project="));
  const stage=h.elements.get("stage");assert.ok(text(stage).includes("2 集 · 每集 3 场 · 每场 4 节拍"));assert.ok(text(stage).includes("尚未通过校验"));
  assert.ok(text(stage).includes("<img src=x onerror=alert(1)>") && !walk(stage).some(item=>item.tagName==="img"));
  assert.equal(walk(stage).filter(item=>item.tagName==="a").length,1);passed++;
  h.run('setStep("agent")');assert.equal(h.run("state.step"),"story");assert.ok(!text(stage).includes("启动 Agent"));passed++;
  const retry=walk(stage).find(item=>item.tagName==="button"&&item.textContent==="使用此创意重新创作");await retry.fire("click");
  assert.equal(h.run("state.creating"),true);assert.equal(h.run("state.storyDraftId"),null);
  assert.deepEqual(JSON.parse(h.run("JSON.stringify(state.form)")),{idea:draft.idea});assert.equal(h.calls.length,0);passed++;
 }
 {
  const h=harness();h.run(`state.jobs.push({id:"${other}",kind:"story",status:"failed",error:"别的故事失败"},{id:"video",kind:"generate",project_id:"A",status:"succeeded"});`);
  await h.run(`openStoryDraft("${id}")`);assert.equal(h.elements.get("job-list").children.length,1);assert.ok(!text(h.elements.get("job-list")).includes("别的故事失败"));
  assert.ok(text(h.elements.get("activity-intro")).includes("结婚当天新娘逃跑"));passed++;
 }
 {
  const h=harness();h.context.location.hash=`#story=${id}&step=agent`;
  await h.run("applyLocationRoute()");assert.equal(h.run("state.storyDraftId"),id);assert.equal(h.run("state.step"),"story");
  assert.ok(h.context.location.hash.endsWith("step=story"));
  h.context.location.hash="#project=A&step=story";await h.run("applyLocationRoute()");assert.equal(h.run("state.storyDraftId"),null);assert.equal(h.run("state.projectId"),"A");
  h.context.location.hash=`#story=${id}&step=story`;await h.run("applyLocationRoute()");assert.equal(h.run("state.storyDraftId"),id);passed++;
 }
 {
  const h=harness();let finish;h.context.pending=new Promise(resolve=>{finish=resolve;});h.run("api.project=()=>pending");
  const old=h.run('openProject("B")');await h.run(`openStoryDraft("${id}")`);finish({id:"B",title:"旧响应",shots:[]});await old;
  assert.equal(h.run("state.storyDraftId"),id);assert.equal(h.run("state.projectId"),null);assert.ok(!text(h.elements.get("stage")).includes("旧响应"));passed++;
 }
 {
  const h=harness();await h.run(`openStoryDraft("${id}")`);
  h.run(`state.jobs[0].status="running";api.jobs=async()=>({jobs:[${JSON.stringify(draft.job)}]});`);await h.run("poll()");
  assert.equal(h.run("state.storyDraft.status"),"failed");assert.equal(h.run("state.storyDraftId"),id);assert.ok(text(h.elements.get("stage")).includes("节拍数量不符"));passed++;
 }
 {
  const h=harness();await h.run(`openStoryDraft("${id}")`);
  h.run(`const previousTab={section:"video",token:0};studioLocal=()=>previousTab;
    state.jobs[0].status="running";api.jobs=async()=>({jobs:[{id:"${id}",kind:"studio_script",status:"succeeded",result:{project_id:"D"}}]});api.bootstrap=async()=>({projects:[...state.projects,{id:"D",title:"新完成故事"}],story_drafts:[],config:state.config});`);
  await h.run("poll()");assert.equal(h.run("state.projectId"),"D");assert.equal(h.run("state.storyDraftId"),null);assert.equal(h.run("state.storyDrafts.length"),0);
  assert.equal(h.run("state.step"),"studio"); assert.equal(h.run("previousTab.section"),"assets");
  assert.equal(h.elements.get("project-list").children.length,4);passed++;
 }
 {
  const h=harness();h.run(`state.step="traces";state.trace.id="${trace}";state.trace.detail={trace_id:"${trace}",marker:"preserved"};state.jobs[0].status="running";
   api.jobs=async()=>({jobs:[{id:"${id}",kind:"story",status:"succeeded",result:{project_id:"D"}}]});api.bootstrap=async()=>({projects:[...state.projects,{id:"D",title:"新完成故事"}],story_drafts:[],config:state.config});`);
  await h.run("poll()");assert.equal(h.run("state.projects.length"),4);assert.equal(h.run("state.storyDrafts.length"),0);assert.equal(h.run("state.step"),"traces");assert.equal(h.run("state.trace.detail.marker"),"preserved");passed++;
 }
 {
  const h=harness();h.run(`api.storyDraft=async()=>({project_id:"D"});api.bootstrap=async()=>({projects:[...state.projects,{id:"D",title:"新完成故事"}],story_drafts:[],config:state.config});`);
  h.context.location.hash=`#story=${id}&step=story`;await h.run("applyLocationRoute()");assert.equal(h.run("state.projectId"),"D");assert.equal(h.run("state.storyDraftId"),null);passed++;
 }
 {
  const h=harness();await h.run('openStoryDraft("invalid")');assert.ok(text(h.elements.get("stage")).includes("故事记录标识无效"));assert.equal(h.run("state.projectId"),null);assert.equal(h.calls.length,0);passed++;
 }
 {
  const h=harness();h.run(`state.jobs=[];state.storyDrafts=[];state.form=${JSON.stringify(draft.inputs)};let submitted=[];api.create=async(body)=>{submitted.push(body);return {job:{id:"${id}",kind:"story",status:"queued",story_input:body}}};startNew();`);
  const form=walk(h.elements.get("stage")).find(item=>item.id==="create-form");
  assert.ok(!walk(form).some(item=>["episodes","scenes","beats"].includes(item.name)));
  assert.ok(text(form).includes("按剧情决定分几集"));
  await form.fire("submit");
  assert.deepEqual(JSON.parse(h.run("JSON.stringify(submitted)")),[{idea:draft.idea}]);assert.equal(h.run("state.storyDraftId"),id);assert.equal(h.run("state.storyDrafts.length"),1);assert.equal(h.run("state.creating"),false);passed++;
 }
 {
  const h=harness();let finish;
  h.context.pendingDraft=new Promise(resolve=>{finish=resolve;});
  const currentJob={id:other,kind:"story",status:"queued",story_input:{idea:"新故事的创意"},message:"本次故事正在排队"};
  h.run(`state.form={idea:"新故事的创意"};
    api.create=async()=>({job:${JSON.stringify(currentJob)}});api.storyDraft=()=>pendingDraft;startNew();`);
  assert.ok(text(h.elements.get("job-list")).includes("新故事还没有制作任务"));
  assert.ok(!text(h.elements.get("job-list")).includes(draft.error));
  const form=walk(h.elements.get("stage")).find(item=>item.id==="create-form");
  const submitting=form.fire("submit");await Promise.resolve();
  assert.equal(h.run("state.storyDraftId"),other,"Submitted story owns the rail before its detail response arrives");
  assert.equal(h.run("state.loading"),true);
  assert.ok(text(h.elements.get("job-list")).includes("本次故事正在排队"));
  assert.ok(!text(h.elements.get("job-list")).includes(draft.error));
  finish({id:other,title:"本次新故事",status:"queued",inputs:currentJob.story_input,job:currentJob});await submitting;
  assert.ok(text(h.elements.get("activity-intro")).includes("本次新故事"));
  assert.equal(h.run("state.jobs.length"),2,"Historical failure remains available in its original story");
  h.run(`api.storyDraft=async()=>(${JSON.stringify(draft)})`);
  await h.run(`openStoryDraft("${id}")`);
  assert.ok(text(h.elements.get("job-list")).includes(draft.error));
  assert.ok(!text(h.elements.get("job-list")).includes("本次故事正在排队"));passed++;
 }
 {
  const h=harness();await h.run(`openStoryDraft("${id}")`);
  h.run(`state.storyDraft.inputs={idea:"返乡",episodes:2};
    state.storyDraft.script_structure={episode_count:2,scene_count:3,beat_count:6,episodes:[
      {title:"返乡",scene_count:1,scenes:[{title:"进站",beat_count:1}]},
      {title:"团圆",scene_count:2,scenes:[{title:"等候",beat_count:3},{title:"归家",beat_count:2}]}
    ]};renderStage()`);
  const overview=text(h.elements.get("stage"));
  for(const label of ["本次指定篇幅　2 集","草稿实际结构 · 2 集 · 3 场 · 6 个节拍","第 2 集 · 团圆（2 场）","等候 · 3 个节拍","归家 · 2 个节拍"])
    assert.ok(overview.includes(label),label);
  assert.ok(!overview.includes("每场"));passed++;
 }
 {
  const h=harness();let finish;h.context.pending=new Promise(resolve=>{finish=resolve;});
  h.run(`state.jobs=[];state.storyDrafts=[];state.form=${JSON.stringify(draft.inputs)};api.create=()=>pending;startNew();`);
  const form=walk(h.elements.get("stage")).find(item=>item.id==="create-form");const submitting=form.fire("submit");
  await h.run('openProject("B")');finish({job:{id,kind:"story",status:"queued",story_input:draft.inputs}});await submitting;
  assert.equal(h.run("state.projectId"),"B");assert.equal(h.run("state.storyDraftId"),null);
  assert.equal(h.run("state.storyDrafts.length"),1,"Queued draft remains discoverable without hijacking later navigation");passed++;
 }
 {
  const h=harness();let finish;h.context.pending=new Promise(resolve=>{finish=resolve;});
  h.run(`state.jobs=[];state.storyDrafts=[];state.form=${JSON.stringify(draft.inputs)};api.create=()=>pending;startNew();`);
  const form=walk(h.elements.get("stage")).find(item=>item.id==="create-form");const submitting=form.fire("submit");
  h.run("startNew()");finish({job:{id,kind:"story",status:"queued",story_input:draft.inputs,message:"上一次提交的故事"}});await submitting;
  assert.equal(h.run("state.creating"),true);assert.equal(h.run("state.storyDraftId"),null);
  assert.ok(text(h.elements.get("job-list")).includes("新故事还没有制作任务"));
  assert.ok(!text(h.elements.get("job-list")).includes("上一次提交的故事"));
  assert.equal(h.run("state.storyDrafts.length"),1,"Late submission remains discoverable without entering the new blank story");passed++;
 }
 {
  const h=harness();
  h.run('state.storyDrafts.push({id:"older-draft",title:"Older saved story",status:"failed"})');
  const before=h.run('JSON.stringify(state.storyDrafts.map(item=>item.id))');
  await h.run(`openStoryDraft("${id}")`);
  assert.equal(h.run('JSON.stringify(state.storyDrafts.map(item=>item.id))'),before,
    'Reading a draft must not reorder sidebar numbering');passed++;
 }
 {
  const h=harness();await h.run(`openStoryDraft("${id}")`);
  h.run(`let repairPayload=null;api.create=async(body)=>{repairPayload=body;return {job:{id:"${other}",kind:"story",status:"queued",story_input:${JSON.stringify(draft.inputs)},repair_from:"${id}"}}};
    api.storyDraft=async(requested)=>requested==="${other}"?{id:"${other}",title:"修复中的故事",status:"queued",inputs:${JSON.stringify(draft.inputs)},repair_from:"${id}",job:{id:"${other}",kind:"story",status:"queued",repair_from:"${id}"}}:${JSON.stringify(draft)};
    state.form={idea:"不得覆盖原始参数",episodes:3,scenes:3,beats:6};`);
  const repair=walk(h.elements.get("stage")).find(item=>item.tagName==="button"&&item.textContent==="自动修复这份草稿");
  assert.ok(repair && !repair.disabled);await repair.fire("click");
  assert.deepEqual(JSON.parse(h.run("JSON.stringify(repairPayload)")),{repair_from:id});
  assert.equal(h.run("state.storyDraftId"),other);assert.equal(h.run("state.storyDrafts.length"),2);
  assert.equal(h.run(`state.jobs.find(item=>item.id==="${id}").status`),"failed","Original failure is not rewritten");
  assert.ok(text(h.elements.get("stage")).includes("查看修复前的记录"));passed++;
  const original=walk(h.elements.get("stage")).find(item=>item.tagName==="button"&&item.textContent==="查看修复前的记录");
  await original.fire("click");assert.equal(h.run("state.storyDraftId"),id);passed++;
 }
 {
  const h=harness();await h.run(`openStoryDraft("${id}")`);let finish;
  h.context.pending=new Promise(resolve=>{finish=resolve;});
  h.run('let repairSubmissions=0;api.create=()=>{repairSubmissions++;return pending}');
  const first=h.run("repairStoryDraft(state.storyDraft)");
  await h.run("repairStoryDraft(state.storyDraft)");
  assert.equal(h.run("repairSubmissions"),1,"Duplicate clicks cannot create duplicate repairs");
  assert.ok(walk(h.elements.get("stage")).some(item=>item.tagName==="button"&&item.textContent==="正在提交修复…"&&item.disabled));
  await h.run('openProject("B")');finish({job:{id:other,kind:"story",status:"queued",story_input:draft.inputs,repair_from:id}});await first;
  assert.equal(h.run("state.projectId"),"B");assert.equal(h.run("state.storyDraftId"),null);
  assert.ok(h.run(`state.storyDrafts.some(item=>item.id==="${other}")`));passed++;
 }
 {
  const h=harness();await h.run(`openStoryDraft("${id}")`);
  for(const value of [{status:"running"},{status:"queued"},{status:"succeeded"},{draft:null},{draft:[]},{draft:"invalid"}]) {
   h.run(`state.storyDraft={...${JSON.stringify(draft)},...${JSON.stringify(value)}};renderStage()`);
   assert.ok(!walk(h.elements.get("stage")).some(item=>item.tagName==="button"&&item.textContent==="自动修复这份草稿"));
  }
  h.run(`state.storyDraft={...${JSON.stringify(draft)},status:"interrupted"};state.config.key_configured=false;renderStage()`);
  assert.ok(walk(h.elements.get("stage")).some(item=>item.tagName==="button"&&item.textContent==="自动修复这份草稿"&&item.disabled));
  assert.equal(h.calls.length,0);passed++;
 }
 {
  const h=harness();await h.run(`openStoryDraft("${id}")`);
  h.run('let repairSubmissions=0;api.create=async()=>{repairSubmissions++;throw new Error("提交结果暂不明确，请查看后台任务")};');
  await h.run("repairStoryDraft(state.storyDraft)");
  assert.equal(h.run("repairSubmissions"),1);assert.equal(h.run("state.storyDraftId"),id);
  assert.equal(h.run("state.pending.has('create')"),false);assert.equal(h.run("state.storyDrafts.length"),1);
  assert.ok(text(h.elements.get("toast-stack")).includes("提交结果暂不明确"));passed++;
 }
 {
  const h=harness();await h.run(`openStoryDraft("${id}")`);
  h.run(`state.jobs.push({id:"${other}",kind:"story",status:"running"});renderStage()`);
  const repair=walk(h.elements.get("stage")).find(item=>item.tagName==="button"&&item.textContent==="自动修复这份草稿");
  assert.equal(repair.disabled,true);await repair.fire("click");assert.equal(h.calls.length,0);passed++;
 }
 {
  const h=harness();h.run("startNew()");
  assert.ok(text(h.elements.get("stage")).includes("每个阶段最多修复 2 次"));
  assert.ok(text(h.elements.get("stage")).includes("模型调用结果不确定时"));passed++;
 }
 console.log(`Story drafts UI: ${passed} offline behavior checks passed; no model or network calls.`);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
