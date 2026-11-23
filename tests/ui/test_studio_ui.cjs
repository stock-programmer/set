/* Offline production desk behavior. Fixtures below contain no live exchanges. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tagName) { this.tagName = tagName; this.children = []; this.attributes = {}; this.dataset = {}; this.events = {}; }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  setAttribute(k,v) { this.attributes[k] = v; }
  addEventListener(k,fn) { this.events[k] = fn; }
  fire(k) { return this.events[k]?.({preventDefault() {}}); }
  set innerHTML(_) { throw Error("Production data must render as text"); }
}
function walk(root) { return [root, ...root.children.flatMap(walk)]; }
function copy(root) { return walk(root).map(n => n.textContent || "").join("\n"); }
function match(root, predicate) { const found = walk(root).find(predicate); assert.ok(found); return found; }
const pin = {asset_id:"story-A/ref/lin",revision_id:"r2",sha256:"2".repeat(64),media_sha256:"5".repeat(64),purpose:"portrait",subject_id:"lin",media_url:"https://wrong.example/img",origin:{method:"generated",model:"offline-picture"}};
const oldPin = {...pin,revision_id:"r1",sha256:"1".repeat(64)};
const spec = {asset_id:"story-A/spec",revision_id:"r3",sha256:"a".repeat(64)};
const source = fs.readFileSync("web/studio.js","utf8") + "\n" + fs.readFileSync("web/app.js","utf8").replace(/initialize\(\);\s*$/,"");
const events = [], requests = [], pages = new Map();
const context = vm.createContext({URL,URLSearchParams,Promise,console,Date,Map,Set,queueMicrotask,
  window:{location:{origin:"http://127.0.0.1:8765"}},document:{createElement:tag=>new Element(tag)},
  FileReader:class { readAsDataURL(file) { this.result=file.offlineContent;this.onload(); } },
  fakeGet: id => pages.get(id), fakeSend:(url,dict) => { requests.push({url, body:JSON.parse(JSON.stringify(dict.body))}); return {job:{id:"job",kind:"studio_video",status:"queued"},pin,spec,shot_id:"S1",trace_id:"3".repeat(32)}; },
  accept:job=>events.push(job), fetch:() => {throw Error("Offline only");},
});
vm.runInContext(source,context);
const run = src => vm.runInContext(src,context);
const tick = () => new Promise(resolve=>setImmediate(resolve));
const dto = {
  project_id:"A", assets:{entities:[
    {id:"lin",kind:"character",name:"林岚",version:2,canonical:{identity:"短发"},purposes:["portrait","voice"],references:[pin,oldPin],shot_ids:["S1"],source_refs:[{source_id:"B1",quote:"林岚拿起钥匙"}]},
    {id:"key",kind:"prop",name:"钥匙",version:1,purposes:["prop"],canonical:{},references:[],shot_ids:["S1"]},
  ],shots:[{id:"S1",description:"拿钥匙",required_refs:[pin]}],spec,store_path:"catalog.db",project_path:"script-A.json",story_id:"story-A"},
  script:{idea:"寻找钥匙",beats:[{id:"B1",text:"人物拿起钥匙",shot_ids:["S1"]}]},
  asset_work:{style:"低饱和暖色",jobs:[],items:[
    {subject_id:"lin",purpose:"portrait",status:"approved",candidate:pin,selected_pin:pin,review:{verdict:"PASS",
      checks:[{path:"palette",expected:"低饱和暖色",actual:"背景暖黄",frame_ids:["candidate"],status:"PASS"}]},
      attempts:[{pin:oldPin,verdict:"FAIL",review:{verdict:"FAIL",checks:[{path:"palette",expected:"低饱和",actual:"画面鲜红",status:"FAIL"}]}},{pin,verdict:"PASS"}]},
    {subject_id:"lin",purpose:"voice",status:"planned",attempts:[]},
    {subject_id:"key",purpose:"prop",status:"planned",attempts:[]},
  ]},
  stages:[{id:"video",status:"待委托",inputs:["剧本"],outputs:["候选"],storage:["候选目录"],next_use:"审片"}],
  video_plan:{plan_id:"a".repeat(64),new_shots:Array.from({length:12},(_,i)=>"S"+String(i+1).padStart(3,"0")),retake_shots:[],reuse_shots:[]},
  productions:[{revision_id:"r1", affected:["S1"], reused:["S2"],media_accepted:false,preview_path:"preview.mp4",
    shots:[{id:"S1",actual_refs:null}], checks:[{name:"S2 → S3",status:"UNKNOWN",path:"cut.json",detail:"需复核"}]}],
  handoff:{events:[{type:"script.revision_published",producer:"script",consumer:"video",trace_id:"3".repeat(32)}]},
};
pages.set("A", dto);
run('state.step="studio";state.projectId="A";state.project={id:"A",title:"A",shots:[],validation:{}};state.config={key_configured:true};state.loading=false;renderStage=()=>{};renderHeader=()=>{};renderShots=renderChecks=()=>node("div");renderJobs=()=>{};persistRoute=()=>{};acceptJob=accept;request=(url,opts)=>opts?.method==="POST"?fakeSend(url,opts):fakeGet(state.projectId);');
async function main() {
  await run('studioRefresh("A")');
  let desk = run("renderStudio()");
  for (const label of ["设定 v2","B1","钥匙","catalog.db","后台正在安排尚未制作的资产","背景暖黄","当前 r2 · 已选用","使用此版本的镜头　S1","声音"])
    assert.ok(copy(desk).includes(label), label);
  assert.ok(!copy(desk).includes("启动待制作资产"));
  const editor = match(desk,n=>n.className==="studio-reference");
  const previewIndex=editor.children.findIndex(n=>n.className==="studio-media");
  assert.ok(previewIndex>=0);
  assert.equal(editor.children[previewIndex+1].className,"studio-asset-feedback");
  assert.equal(editor.children[previewIndex+2].tagName,"details");
  assert.ok(copy(editor.children[previewIndex+2]).includes("Agent 自检 · 自检通过"));
  assert.ok(!walk(desk).some(n=>n.tagName==="img"), "Cross-origin reference media rejected");
  const voice = {...pin, asset_id:"story-A/ref/lin-voice",purpose:"voice",revision_id:"r1",media_url:"/api/projects/A/studio/media?ref=voice"};
  const key = {...pin, asset_id:"story-A/ref/key",purpose:"prop",subject_id:"key",media_url:"/api/projects/A/studio/media?ref=key"};
  dto.assets.entities[0].references.push(voice); dto.assets.entities[1].references.push(key);
  desk = run("renderStudio()");
  const gallery = match(desk,n=>n.className==="studio-asset-gallery");
  assert.equal(walk(gallery).filter(n=>n.tagName==="article").length,3);
  assert.equal(match(gallery,n=>n.tagName==="audio").src,voice.media_url);
  assert.equal(match(gallery,n=>n.tagName==="img").src,key.media_url);
  dto.assets.entities[1].references.length=0;
  assert.equal(requests.length,0,"Reading a story must not generate any media");
  const lastToken=run('studioLocal("A").token');
  run('studioLocal("A").lastRead=0; studioPollAssets()'); await tick();
  assert.ok(run('studioLocal("A").token')>lastToken,"Pending-only queues refresh without a button or browser-submitted job");
  run('document.querySelectorAll=()=>[{paused:false}];studioPollAssets(true)');
  assert.ok(run('studioLocal("A").refreshDue'),"Keep playing audition when the background batch finishes");
  run('document.querySelectorAll=()=>[];studioPollAssets()');await tick();
  assert.equal(run('studioLocal("A").refreshDue'),false);
  const file=match(desk,n=>n.id==="studio-file");
  file.files=[{size:3,offlineContent:"data:image/png;base64,YWJj"}];file.fire("change");
  match(desk,n=>n.textContent==="导入新版参考").fire("click");await tick();
  assert.deepEqual(requests.pop(),{url:"/api/projects/A/studio/references",body:{subject_id:"lin",purpose:"portrait",data_uri:file.files[0].offlineContent,asset_id:pin.asset_id,parent_revision:"r2"}});
  assert.ok(!copy(desk).includes("待人审"));
  assert.ok(!walk(desk).some(n=>n.tagName==="button" && /确认.*版本|绑定.*镜头|绑定到/.test(n.textContent || "")));
  const advice=match(desk,n=>n.id==="asset-feedback");advice.value="发夹改成银色";advice.fire("input");
  match(desk,n=>n.textContent==="重新生成").fire("click");await tick();
  assert.deepEqual(requests.pop(),{url:"/api/projects/A/studio/asset-regenerate",body:{subject_id:"lin",purpose:"portrait",feedback:advice.value,asset_id:pin.asset_id,parent_revision:"r2"}});
  const direction=match(desk,n=>n.id==="asset-style");direction.value="黑白";direction.fire("input");
  match(desk,n=>n.textContent==="按新风格重新制作资产").fire("click");await tick();
  assert.deepEqual(requests.pop(),{url:"/api/projects/A/studio/asset-production",body:{style:"黑白"}});
  desk = run("renderStudio()");
  const selector = match(desk,n=>n.id==="studio-revision");
  selector.value = oldPin.asset_id + " @ r1"; selector.fire("change"); desk = run("renderStudio()");
  assert.ok(copy(desk).includes("自检未通过") && copy(desk).includes("画面鲜红"));
  assert.ok(!copy(desk).includes("确认此版本并绑定镜头"),"A historical failed candidate cannot be approved");
  assert.equal(events.length,3);
  // A failed replacement remains inspectable while the gallery and default detail show the actual selected media.
  const failedPin={...pin,revision_id:"r3",sha256:"3".repeat(64),created_at:"2026-11-07",
    media_url:"/api/projects/A/studio/media?ref=failed"};
  dto.assets.entities[0].references.unshift(failedPin);
  const portrait=dto.asset_work.items[0];
  const reviewRef={asset_id:"offline/review-lin-r3",revision_id:"r1",sha256:"b".repeat(64)};
  portrait.status="needs_attention"; portrait.candidate=failedPin; portrait.review={verdict:"UNKNOWN",checks:[],review_ref:reviewRef};
  portrait.diagnostic = {name:"林岚",subject_id:"lin",purpose:"portrait",stage:"review",verdict:"UNKNOWN",
    reason:"定妆照能看到脸，剧本却没有可对照的发型设定。",action:"写明发型后重新生成。",revision_id:"r3",
    attempt_count:2,trace_id:"3".repeat(32),span_id:"4".repeat(32),
    evidence:[{path:"asset_use",label:"人物外形",expected:"短发",actual:"发型要求未写明",status:"UNKNOWN"}]};
  run('studioLocal("A").pinKey=null');desk=run("renderStudio()");
  assert.ok(copy(desk).includes("当前 r2 · 新版需处理") && copy(desk).includes("本次候选 r3，镜头继续使用 r2"));
  const problem=match(desk,n=>n.className==="studio-asset-problems");
  const top=walk(desk);
  assert.ok(top.indexOf(problem)<top.findIndex(n=>n.className==="studio-asset-gallery"));
  assert.ok(copy(problem).includes("林岚 · 定妆照") && copy(problem).includes("未能确认") && copy(problem).includes("当前仍使用 r2"));
  assert.ok(copy(problem).includes(portrait.diagnostic.reason) && copy(problem).includes(portrait.diagnostic.action));
  assert.ok(copy(match(problem,n=>n.tagName==="details")).includes("发型要求未写明"));
  assert.equal(match(problem,n=>n.tagName==="a").href,"/#step=traces&trace="+"3".repeat(32)+"&span="+"4".repeat(32));
  for (const status of ["approved","needs_attention"]) {
    assert.equal(match(desk,n=>n.className==="asset-total asset-state-"+status).children[0].textContent,"1");
  }
  assert.equal(match(desk,n=>n.id==="studio-revision").value,pin.asset_id + " @ r2");
  assert.equal(walk(desk).filter(n=>n.textContent==="人工通过").length,1,"The old selected version cannot be passed in the preview");
  assert.ok(!walk(desk).some(n=>n.tagName==="img" && n.src===failedPin.media_url));
  const previouslySelected = portrait.selected_pin;
  portrait.selected_pin = null;
  const reviewRow = match(run("renderStudio()"), n=>n.className==="studio-asset-problem");
  const originalImage = match(reviewRow,n=>n.tagName==="a" && n.href===failedPin.media_url);
  assert.equal(match(originalImage,n=>n.tagName==="img").src,failedPin.media_url);
  assert.ok(copy(reviewRow).includes("发型要求未写明") && match(reviewRow,n=>n.textContent==="人工通过"));
  assert.equal(requests.length,0);
  portrait.selected_pin = previouslySelected;
  match(desk,n=>n.textContent==="重新生成").fire("click");await tick();
  assert.deepEqual(requests.pop(),{url:"/api/projects/A/studio/asset-regenerate",body:{subject_id:"lin",purpose:"portrait",feedback:"",asset_id:pin.asset_id,parent_revision:"r3"}});
  match(problem,n=>n.textContent==="查看素材与处理选项").fire("click");desk=run("renderStudio()");
  assert.equal(match(desk,n=>n.id==="studio-revision").value,pin.asset_id+" @ r3");
  assert.equal(requests.length,0,"Reading a failed asset or its evidence does not retry it");
  assert.ok(match(desk,n=>n.textContent==="重新核验此版本"));
  assert.ok(copy(desk).includes("证据不足") && !copy(desk).includes("确认此版本并绑定镜头"));
  // A stale review must refresh the desk, keep the asset pending and never retry automatically.
  const originalSend=context.fakeSend;
  let staleCalls=0;
  const updatedReview={...reviewRef,revision_id:"r2",sha256:"c".repeat(64)};
  context.fakeSend=(url,dict)=>{
    staleCalls++;
    assert.deepEqual(JSON.parse(JSON.stringify(dict.body.review_ref)),reviewRef);
    portrait.review={...portrait.review,review_ref:updatedReview};
    throw Error("候选的自检记录已更新");
  };
  run("renderJobs=()=>{};state.config.key_configured=false");
  const beforeStale=run('studioLocal("A").token');
  await match(problem,n=>n.textContent==="人工通过").fire("click");
  assert.equal(staleCalls,1);
  assert.ok(run('studioLocal("A").token')>beforeStale && run('studioLocal("A").message').includes("自检记录已更新"));
  desk=run("renderStudio()");
  const manual=match(match(desk,n=>n.className==="studio-media"),n=>n.textContent==="人工通过");
  assert.equal(manual.disabled,false,"Human approval works without a model key");
  context.fakeSend=(url,dict)=>{
    assert.equal(url,"/api/projects/A/studio/assets/approve");
    assert.deepEqual(JSON.parse(JSON.stringify(dict.body)),{
      asset_id:failedPin.asset_id,revision_id:"r3",sha256:failedPin.sha256,review_ref:updatedReview});
    portrait.status="approved";portrait.selected_pin=failedPin;portrait.approval={pin:failedPin,selection_mode:"human",trace_id:"a".repeat(32)};
    dto.assets.shots[0].required_refs=[failedPin];
    return {trace_id:portrait.approval.trace_id};
  };
  await manual.fire("click");
  desk=run("renderStudio()");
  assert.equal(match(desk,n=>n.className==="asset-total asset-state-needs_attention").children[0].textContent,"0");
  assert.ok(!walk(desk).some(n=>n.textContent==="人工通过" || n.className==="studio-asset-problems"));
  assert.ok(copy(desk).includes("当前 r3 · 已人工通过") && copy(desk).includes("Agent 自检 · 证据不足"));
  assert.equal(match(desk,n=>n.textContent==="人工通过 Trace ↗").href,"/#step=traces&trace="+"a".repeat(32));
  portrait.status="needs_attention";portrait.selected_pin=pin;delete portrait.approval;dto.assets.shots[0].required_refs=[pin];
  context.fakeSend=originalSend;run("state.config.key_configured=true");
  dto.asset_work.items[0].review=null;
  desk=run("renderStudio()");
  match(desk,n=>n.textContent==="核验已有候选").fire("click");await tick();
  assert.deepEqual(requests.pop(),{url:"/api/projects/A/studio/asset-inspect",body:{pin:{
    asset_id:failedPin.asset_id,revision_id:failedPin.revision_id,sha256:failedPin.sha256,
    media_sha256:pin.media_sha256,purpose:pin.purpose,subject_id:pin.subject_id,
  }}});
  const other = match(desk,n=>n.className==="studio-gallery-target" && copy(n).includes("钥匙")); other.fire("click");
  desk=run("renderStudio()");assert.ok(copy(desk).includes("道具参考候选将在后台制作后显示") && !copy(desk).includes(pin.asset_id));
  assert.ok(!walk(desk).some(node=>node.tagName==="button" && node.textContent==="重新生成"),"Pending assets require no manual generation step");
  dto.asset_work.items[2].status="needs_attention";
  const missingDesk=run("renderStudio()");
  assert.ok(match(missingDesk,n=>n.textContent==="重新生成"));
  assert.ok(!walk(match(missingDesk,n=>n.className==="studio-reference")).some(n=>n.textContent==="人工通过"),
    "A missing media candidate cannot be passed");
  dto.asset_work.items[2].status="planned";
  dto.asset_work.auto_dispatch={status:"blocked",reason:"离线模拟：未配置百炼"};
  assert.ok(copy(run("renderStudio()")).includes(dto.asset_work.auto_dispatch.reason));
  delete dto.asset_work.auto_dispatch;
  assert.ok(!walk(desk).some(n=>n.className==="studio-flow"),"Stage content must not add a second navigation");
  run('studioLocal("A").section="video"');
  desk = run("renderStudio()"); assert.ok(copy(desk).includes("缺少实际提交证据") && copy(desk).includes(oldPin.asset_id));
  const startLabel="开始制作 · 新拍 12 个";
  const firstShoot = match(desk,n=>n.textContent===startLabel);
  assert.ok(copy(desk).includes(dto.video_plan.new_shots.join("、")) && !walk(desk).some(n=>n.type==="number"));
  assert.equal(requests.length,0,"Reading the plan does not start generation");
  await firstShoot.fire("click");
  assert.deepEqual(requests.pop(),{url:"/api/projects/A/studio/video",body:{plan_id:dto.video_plan.plan_id,spec_ref:spec,script_ref:null}});
  const updated = {plan_id:"b".repeat(64),new_shots:[],retake_shots:["S003"],reuse_shots:["S001","S002"]};
  const originalPlan = dto.video_plan;
  dto.video_plan = updated; dto.assets.production_ref = {...spec,asset_id:"A/production"};
  await firstShoot.fire("click");
  assert.equal(requests.length,0,"Detached old plan cannot submit the changed scope");
  desk=run("renderStudio()");
  assert.ok(copy(desk).includes("S001、S002"));
  const revised = match(desk,n=>n.textContent==="更新制作 · 重拍 1 个，复用 2 个");
  const reason="S002 的交接动作未出现";
  const proposal={proposal_id:"c".repeat(64),shot_ids:["S002"],reason};
  dto.video_plan={...updated,extra_retake:proposal};
  await revised.fire("click");assert.equal(requests.length,0,"Ordinary submit cannot approve a newer retake");
  desk=run("renderStudio()");
  assert.ok(copy(desk).includes(reason) && copy(desk).includes("S001、S002"));
  await match(desk,n=>n.textContent==="更新制作 · 重拍 1 个，复用 2 个").fire("click");
  assert.deepEqual(requests.pop(),{url:"/api/projects/A/studio/video",body:{plan_id:updated.plan_id,spec_ref:spec,script_ref:null}});
  await match(desk,n=>n.textContent==="同意额外重拍并制作 · 重拍 2 个，复用 1 个").fire("click");
  assert.deepEqual(requests.pop(),{url:"/api/projects/A/studio/video",body:{plan_id:updated.plan_id,spec_ref:spec,script_ref:null,proposal_id:proposal.proposal_id}});
  dto.video_plan={...updated,extra_retake:{...proposal,partial:true,reused_shots:["S001","S003"]}};
  desk=run("renderStudio()");
  assert.ok(copy(desk).includes("S001、S003") && !walk(desk).some(n=>n.textContent==="更新制作 · 重拍 1 个，复用 2 个"));
  assert.ok(match(desk,n=>n.textContent==="同意额外重拍并制作 · 重拍 1 个，复用 2 个"));
  delete dto.assets.production_ref; dto.video_plan=originalPlan;
  desk=run("renderStudio()");
  let outdatedRequests=0;
  context.fakeSend=()=>{outdatedRequests++;dto.video_plan=updated;throw Error("拍摄计划已更新");};
  await match(desk,n=>n.textContent===startLabel).fire("click");
  assert.equal(outdatedRequests,1,"A stale plan causes a refresh but no new write");
  assert.ok(copy(run("renderStudio()")).includes("拍摄计划已更新") && copy(run("renderStudio()")).includes("重拍 1 个，复用 2 个"));
  context.fakeSend=originalSend;dto.video_plan=originalPlan;
  dto.assets.pending_script_ref={...spec,revision_id:"r4"};desk=run("renderStudio()");
  assert.ok(copy(desk).includes("等待新版制作规格") && match(desk,n=>n.textContent===startLabel).disabled);
  await match(desk,n=>n.textContent===startLabel).fire("click");assert.equal(requests.length,0);
  delete dto.assets.pending_script_ref;
  // Offline interrupted batch: five retained clips, seven submissions, no full restart.
  dto.video_plan={plan_id:"d".repeat(64),new_shots:originalPlan.new_shots.slice(5),retake_shots:[],
    reuse_shots:originalPlan.new_shots.slice(0,5),continuation:{request_id:"e".repeat(32),sha256:"f".repeat(64)}};
  desk=run("renderStudio()");
  const continueShoot=match(desk,n=>n.textContent==="继续制作 · 补拍 7 个，沿用 5 个");
  await continueShoot.fire("click");
  assert.equal(requests.pop().body.plan_id,dto.video_plan.plan_id);
  assert.ok(!copy(desk).includes("开始制作 · 新拍 12 个"));
  dto.incomplete_production={available_count:5,total:12,reviewed_count:1,missing_shots:dto.video_plan.new_shots,review_jobs:[],
    shots:[{shot_id:"S001",review_status:"completed",status:"UNKNOWN",report_sha256:"1".repeat(64),
      human_confirmed:false,report:{status:"UNKNOWN",task_complete:false,results:[{criterion_id:"action_sequence",required:true,status:"UNKNOWN",reason:"offline continuous evidence missing"}]}}]};
  desk=run("renderStudio()");
  assert.ok(copy(desk).includes("自动验收待补证据") && copy(desk).includes("Agent 待处理"));
  const pendingShot=dto.incomplete_production.shots[0], jobId="b".repeat(32), mediaHash="c".repeat(64);
  Object.assign(dto.incomplete_production,{request_id:"d".repeat(32),job_id:jobId});
  Object.assign(pendingShot,{media_sha256:mediaHash,can_approve:true,
    preview_url:`/api/projects/A/studio/progress/media?job_id=${jobId}&shot_id=S001&sha256=${mediaHash}`});
  context.technical={...dto.incomplete_production,shots:[{...pendingShot,can_approve:false,
    blocked_reason:"等待独立复验相邻衔接"}]};
  const readOnly=run('studioShotApprovals(technical,"A",studioLocal("A"),true)');
  assert.ok(copy(readOnly).includes("等待独立复验相邻衔接"));
  assert.equal(match(readOnly,n=>n.tagName==="video" || n.attributes["aria-label"]?.startsWith("S001 待审片段")).src,pendingShot.preview_url);
  assert.ok(!walk(readOnly).some(n=>n.textContent==="人工通过" || n.textContent==="重新检查并合片"));
  assert.equal(match(readOnly,n=>n.dataset?.shotId==="S001").dataset.reportSha,pendingShot.report_sha256);
  // Three shot groups, two actual outputs each. Only the selected take owns an approval action.
  const commissionId="d".repeat(32);
  const groups=["G001","G002","G003"].map((shotId,group) => {
    const versions=[0,1].map(take => {
      const media=(2*group+take+1).toString().repeat(64);
      return {shot_id:shotId,task_id:"task-"+media,kind:take?"retake":"initial",selected:take===(group===2?1:0),media_sha256:media,
        report_sha256:"f".repeat(64),report:{status:"FAIL",results:[{criterion_id:"action_sequence",required:true,status:"FAIL",reason:"report-"+media}]},
        preview_url:`/api/projects/A/studio/progress/media?job_id=${jobId}&shot_id=${shotId}&sha256=${media}`};
    });
    return {...versions.find(take=>take.selected),versions,shot_id:shotId,request_id:commissionId,can_approve:true};
  });
  context.allTakes={...dto.incomplete_production,shots:groups,available_count:3,total:3,version_count:6};
  const takeGallery=run('studioShotApprovals(allTakes,"A",studioLocal("A"),true)');
  const cards=walk(takeGallery).filter(n=>n.className==="studio-take");
  assert.equal(cards.length,6);assert.ok(copy(takeGallery).includes("3 个镜头组 · 6 个可播放版本"));
  for (const [i,take] of groups.flatMap(g=>g.versions).entries()) {
    const player=match(cards[i],n=>n.tagName==="video");
    assert.equal(player.src,take.preview_url);
    assert.equal(player.preload,"none");
    assert.equal(player.dataset.takeId,take.task_id);
    assert.ok(copy(cards[i]).includes("report-"+take.media_sha256));
    assert.equal(cards[i].dataset.selected,String(take.selected));
    assert.equal(walk(cards[i]).some(n=>n.textContent==="人工通过"),take.selected);
  }
  assert.equal(requests.length,0);
  const src=groups[0].versions[0].preview_url, takeId=groups[0].versions[0].task_id;
  context.players=[{src,time:0,paused:true},{src,takeId,time:7,paused:false}];
  let plays=0, metadata;
  context.player={dataset:{takeId},getAttribute:()=>src,readyState:0,paused:true,currentTime:0,
    addEventListener:(name,cb)=>{assert.equal(name,"loadedmetadata");metadata=cb;},
    play:()=>{plays++;return Promise.resolve();}};
  run("restoreVideoPlayback(player,players)");
  assert.equal(plays,1,"A playing take restarts before metadata arrives even when the monitor shares its URL");
  metadata();assert.equal(context.player.currentTime,7);
  context.player.dataset={};run("restoreVideoPlayback(player,players)");
  assert.equal(plays,1,"The progress monitor must stay paused");
  context.players[1].paused=true;context.player.dataset={takeId};metadata=null;
  run("restoreVideoPlayback(player,players)");
  assert.equal(plays,1,"Restoring an idle version must not start a media request");
  assert.ok(metadata,"A paused take restores its position on the next user playback");
  dto.video_execution={commission_id:commissionId,status:"failed",usage:{reserved_video_generations:6}};
  run('var takeReads=0, previousRead=request; request=(url,opts)=>url.includes("/shot-reviews?")?(takeReads++,allTakes):previousRead(url,opts);');
  context.allTakes.project_id="A";
  run('renderStudio();renderStudio()');
  await tick();
  await tick();
  assert.equal(run('takeReads'),1,"Render and polling share the same commission read");
  assert.equal(walk(run('renderStudio()')).filter(n=>n.className==="studio-take").length,6);
  assert.equal(requests.length,0);
  run('request=previousRead;delete studioLocal("A").shotReviews');
  delete dto.video_execution;
  desk=run("renderStudio()");
  let acceptShot=match(desk,n=>n.attributes["aria-label"]==="人工通过 S001 当前视频版本");
  assert.equal(match(desk,n=>n.attributes["aria-label"]?.startsWith("S001 待审片段")).src,pendingShot.preview_url);
  assert.equal(requests.length,0);
  context.fakeSend=(url,dict)=>{
    assert.equal(url,"/api/projects/A/studio/shots/approve");
    assert.deepEqual(JSON.parse(JSON.stringify(dict.body)),{request_id:"d".repeat(32),shot_id:"S001",
      media_sha256:mediaHash,report_sha256:"1".repeat(64)});
    Object.assign(pendingShot,{human_approved:true,can_approve:false});
    return {delivery:{delivered:true}};
  };
  await acceptShot.fire("click"); context.fakeSend=originalSend;
  desk=run("renderStudio()");
  assert.ok(copy(desk).includes("S001 · 已人工通过") && copy(desk).includes("offline continuous evidence missing"));
  assert.ok(!walk(desk).some(n=>n.attributes["aria-label"]==="人工通过 S001 当前视频版本"));
  run('studioLocal("A").section="review"');desk=run("renderStudio()");
  assert.ok(copy(desk).includes("人工预览尚未解锁"));
  assert.ok(!walk(desk).some(n=>n.textContent==="确认已阅此结论"));
  dto.preview_gate={ready:true};dto.incomplete_production.shots[0].status="PASS";
  await run('studioRefresh("A")');desk=run("renderStudio()");
  await match(desk,n=>n.textContent==="确认已阅此结论").fire("click");
  assert.deepEqual(requests.pop(),{url:"/api/projects/A/studio/review-confirm",body:{shot_id:"S001",report_sha256:"1".repeat(64)}});
  delete dto.incomplete_production;delete dto.preview_gate; dto.video_plan=originalPlan;
  run('studioLocal("A").section="video"');
  desk = run("renderStudio()"); assert.ok(copy(desk).includes("媒体验收待确认") && copy(desk).includes("S2 → S3") && copy(desk).includes("cut.json"));
  assert.ok(walk(desk).some(n=>n.href?.includes("step=traces")));
  // A slow response from A cannot overwrite B after the user switches the story.
  let finish; const slow = new Promise(resolve=>{finish=resolve;});pages.set("A",slow);
  const waiting=run('studioRefresh("A")');
  pages.set("B",{project_id:"B",assets:{entities:[],shots:[]},stages:[],productions:[],handoff:{events:[]}});
  run('state.projectId="B";state.project={id:"B",title:"B",shots:[],validation:{}};');
  await run('studioRefresh("B")');finish(dto);await waiting;
  const current=copy(run("renderStudio()"));assert.ok(!current.includes("林岚") && current.includes("工程尚未提供"));
  assert.equal(run('studioMediaUrl("/api/projects/A/studio/media?ref=2","B")'),null);
  const imported=[];
  context.receiveImport=body=>imported.push(JSON.parse(JSON.stringify(body)));
  const fileForm=run("studioImportForm(receiveImport,false)");
  const importFile=match(fileForm,n=>n.id==="studio-import-script");
  const screenplay={schema_version:"screenplay/1",title:"已有剧本"};
  importFile.files=[{size:80,text:async()=>JSON.stringify(screenplay)}];importFile.fire("change");
  await match(fileForm,n=>n.textContent==="导入并编译分镜").fire("click");assert.deepEqual(imported,[{script:screenplay}]);
  console.log("Production desk: offline story scope, stage navigation, asset revisions, billable actions, and delivery evidence passed.");
}
main().catch(error=>{ console.error(error);process.exitCode=1; });
