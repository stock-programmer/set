/* Offline full stage routing and integration. No provider/network calls. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.attributes={};this.events={};this.value='';this.classes=new Set();this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
  append(...children){for(const child of children)child.parent=this;this.children.push(...children);}replaceChildren(...children){this.children=[];this.append(...children);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(e=>e!==this);}
  setAttribute(k,v){this.attributes[k]=v;}getAttribute(k){return this.attributes[k];}removeAttribute(k){delete this.attributes[k];}
  addEventListener(k,fn){this.events[k]=fn;}focus(){}set innerHTML(_){throw Error('Use safe text');}
  querySelectorAll(selector){return walk(this).filter(e=>e!==this&&e.tagName===selector);}
}
const walk=e=>[e,...(e.children||[]).flatMap(walk)],text=e=>walk(e).map(n=>n.textContent||'').join('\n');
const nodes=new Map(),tabs=['story','assets','video','review'].map(key=>{const e=new Element('button');e.dataset.step=key;return e;}),labels=tabs.map(t=>{const e=new Element('small');e.dataset.phaseStatus=t.dataset.step;return e;});
const get=id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);};
const dto={project_id:'A',script:{beats:[]},assets:{entities:[],shots:[],project_path:'offline.json'},asset_work:{items:[],jobs:[],counts:{}},
  video_plan:{plan_id:'a'.repeat(64),new_shots:[],retake_shots:[],reuse_shots:[]},productions:[],preview_gate:{ready:false},handoff:{events:[]},stages:[]};
const context=vm.createContext({console,URL,URLSearchParams,AbortController,Map,Set,Date,JSON,Promise,queueMicrotask,setTimeout(){},
  history:{replaceState:(_,__,hash)=>context.location.hash=hash},location:{hash:''},
  window:{location:{origin:'http://127.0.0.1:8765'},crypto:{randomUUID:()=> 'b'.repeat(32)}},
  document:{body:new Element('body'),activeElement:null,createElement:t=>new Element(t),createTextNode:t=>({textContent:t,children:[]}),getElementById:get,
    querySelectorAll:s=>s==='[data-step]'?tabs:s==='[data-phase-status]'?labels:[]},
  fetch:()=>{throw Error('Offline only');},fixture:dto,
});
vm.runInContext(fs.readFileSync('web/studio.js','utf8')+'\n'+fs.readFileSync('web/agent-chat.js','utf8')+'\n'+fs.readFileSync('web/app.js','utf8').replace(/initialize\(\);\s*$/,''),context);
const run=s=>vm.runInContext(s,context);
run(`state.projectId='A';state.project={id:'A',title:'Offline story',shots:[],validation:{}};state.projects=[{id:'A'}];state.config={key_configured:false,ffmpeg_ready:true};state.loading=false;studioLocal('A').data=fixture;
request=async url=>url.includes('/pi/sessions')?{session:{project_id:'A'},messages:[],capabilities:{permission_modes:true}}:fixture;`);
let passed=0;
for(const phase of ['story','assets','video','review']){
  run(`setStep('${phase}')`);
  assert.equal(run('currentPhase()'),phase);assert.ok(context.location.hash.includes('step='+phase));
  assert.equal(tabs.filter(t=>t.attributes['aria-current']==='step').length,1);
  assert.equal(tabs.find(t=>t.attributes['aria-current']==='step').dataset.step,phase);
  assert.ok(!walk(get('stage')).some(e=>e.className==='studio-flow'),'No nested navigation');
  assert.ok(walk(get('stage')).some(e=>e.id==='agent-chat'),'Current story conversation follows every stage');passed++;
}
assert.ok(text(get('stage')).includes('所有镜头及衔接必过项通过后'));
assert.ok(!walk(get('stage')).some(e=>e.tagName==='video'));passed++;
run("setStep('video')");
for(const key of ['shots','checks'])assert.ok(walk(get('stage')).some(e=>e.dataset.productionTool===key));
assert.ok(!walk(get('stage')).some(e=>e.dataset.productionTool==='agent'||e.id==='agent-form'));passed++;
(async()=>{
  for(const [old,phase,tool] of [['shots','video','shots'],['checks','video','checks'],['agent','video',null],['export','review',null],['studio','assets',null]]){
    context.location.hash='#project=A&step='+old;await run('applyLocationRoute()');
    assert.equal(run('currentPhase()'),phase);assert.equal(run("studioLocal('A').tool"),tool);
    assert.ok(context.location.hash.includes('step='+phase));passed++;
  }
  context.location.hash='#project=A&step=studio&section=script';await run('applyLocationRoute()');assert.equal(run('currentPhase()'),'story');passed++;
  run("studioLocal('A').data=null;renderStage()");await run("studioRefresh('A')");
  assert.ok(text(get('stage')).includes('发布与交付记录'),'Async publication metadata is integrated into the script stage');passed++;
  context.location.hash='#project=A&step=studio&section=review';await run('applyLocationRoute()');assert.equal(run('currentPhase()'),'review');passed++;
  const html=fs.readFileSync('web/index.html','utf8');assert.equal((html.match(/data-step=/g)||[]).length,4);passed++;
  // Hold all full snapshots to exercise the actual sidebar click before a response arrives.
  const videoJob = {id:'b'.repeat(32),project_id:'B',kind:'studio_video',status:'running',message:'正在准备工具与输入',message_id:'c'.repeat(32)};
  videoJob.recorded_progress={project_id:'B',job_id:videoJob.id,message_id:videoJob.message_id,recorded_at:'2026-11-01T02:33:45Z',
    completed_count:1,shot_count:2,shots:[{shot_id:'S1',operation:'new',status:'generated'},{shot_id:'S2',operation:'new',status:'reserved'}]};
  const stories=['A','B'].map(id=>({id,title:'故事 '+id,shot_count:2,opening:{
    video_job:id==='B'?videoJob:null,
    logline:'故事简介 '+id,entities:[{id:'P'+id,name:'人物 '+id,kind:'character'},
      {id:'lake',name:'湖畔',kind:'scene',description:'长椅在柳树下'}],
    shots:[{id:'S'+id,description:'画面 '+id,duration_s:4,entity_ids:['P'+id]},
      {id:'lake-wide',description:'湖畔全景',duration_s:5,scene_id:'lake',entity_ids:[]}],
  }}));
  const reads=[];
  context.stories=stories;
  context.readWorkspace=(id,signal)=>new Promise((resolve,reject)=>reads.push({id,signal,resolve,reject}));
  run(`state.projects=stories;state.config.workspace_snapshot=true;api.workspace=readWorkspace;
    state.step="traces";studioLocal("A").section="assets";renderHeader();renderProjects();`);
  const clickProject=index=>get('project-list').children[index].events.click();
  const overview=()=>walk(get('stage')).find(e=>e.dataset?.projectOpening);
  let resolveEarlierPoll;
  context.pendingJobs=new Promise(resolve=>{resolveEarlierPoll=resolve;});
  run('api.jobs=()=>pendingJobs');
  const earlierPoll=run('poll()');
  clickProject(0);
  assert.equal(get('project-title').textContent,'故事 A');assert.ok(context.location.hash.includes('project=A'));
  assert.equal(overview().dataset.projectOpening,'A');assert.ok(text(overview()).includes('人物 A'));
  assert.ok(text(overview()).includes('画面 A'));
  assert.ok(walk(overview()).filter(e=>e.tagName==='button').every(e=>e.dataset.projectEntity));passed++;
  resolveEarlierPoll({jobs:[{id:'stale-job',status:'succeeded'}]});await earlierPoll;
  assert.ok(!run('state.jobs.some(job=>job.id==="stale-job")'));
  run('let pollingRequests=0;api.jobs=async()=>{pollingRequests++;return {jobs:[]};}');
  await run('poll()');await run('openProject("A",{quiet:true})');clickProject(0);
  assert.equal(run('pollingRequests'),0);
  assert.deepEqual(reads.map(r=>r.id),['A']);passed++;
  const pendingImage=new Element('img');
  pendingImage.setAttribute('src','/offline-reference.png');pendingImage.complete=false;
  get('stage').append(pendingImage);
  clickProject(1);
  assert.equal(reads[0].signal.aborted,true,'Leaving A cancels its pending aggregate GET');
  assert.equal(reads[1].signal.aborted,false,'B keeps its own aggregate GET');
  assert.equal(pendingImage.getAttribute('src'),undefined,'Navigation releases the unfinished reference image');passed++;
  const snapshot=id=>({projects:stories,config:{workspace_snapshot:true},jobs:[],studio:{...dto,project_id:id,
    assets:{...dto.assets,entities:stories[0].opening.entities.map(e=>({...e,purposes:[],version:1}))}},
    project:{id,title:'详情 '+id,shots:[],validation:{}},conversations:{}});
  reads[0].resolve(snapshot('A'));await new Promise(resolve=>setImmediate(resolve));
  assert.equal(overview().dataset.projectOpening,'B');assert.ok(!text(get('stage')).includes('画面 A'));
  assert.equal(get('project-title').textContent,'故事 B');passed++;
  walk(overview()).find(e=>e.dataset?.projectEntity==='lake').events.click();
  assert.equal(run('studioLocal("B").subjectId'),'lake');assert.ok(text(overview()).includes('长椅在柳树下'));
  assert.deepEqual(walk(overview()).filter(e=>e.dataset?.projectShot).map(e=>e.dataset.projectShot),['lake-wide']);
  assert.equal(walk(overview()).find(e=>e.dataset?.projectEntity==='lake').getAttribute('aria-pressed'),'true');
  const progressRequests=[];
  let resolveProgress;
  context.fetch=(url,opts)=>{
    progressRequests.push([url,opts.method,opts.priority]);
    return new Promise(resolve=>{resolveProgress=resolve;});
  };
  run('setStep("video")');assert.ok(context.location.hash.includes('step=video'));
  const monitor=()=>walk(get('stage')).find(e=>e.dataset?.progressJob===videoJob.id);
  assert.deepEqual(progressRequests,[[`/api/projects/B/studio/progress?job_id=${videoJob.id}`,'GET','high']]);
  assert.equal(monitor().dataset.readState,'recorded');assert.ok(text(monitor()).includes('上次制作记录'));
  assert.ok(text(monitor()).includes('正在核对最新状态'));assert.ok(!walk(monitor()).some(e=>e.className==='live-shot' && e.children.some(c=>c.tagName==='button')));
  assert.ok(walk(overview()).some(e=>e.dataset?.projectShot==='SB'));
  resolveProgress({status:200,ok:true,json:async()=>({project_id:'B',job_id:videoJob.id,message_id:videoJob.message_id,total:2,completed:1,
    job_status:'running',phase:'generating',current_shot_id:'S2',preview_gate:{ready:false},
    shots:[{shot_id:'S1',status:'ready'},{shot_id:'S2',status:'generating'}]})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(monitor().dataset.readState,'ready');
  assert.equal(overview().dataset.projectOpening,'B','Progress can paint while the complete workspace is still pending');
  assert.ok(text(monitor()).includes('已生成 1 / 2'));passed++;
  reads[1].reject(Error('offline test failure'));await new Promise(resolve=>setImmediate(resolve));
  assert.ok(text(overview()).includes('画面 B'));assert.ok(text(overview()).includes('offline test failure'));
  delete videoJob.recorded_progress;
  const retry=walk(overview()).find(e=>e.textContent==='重新读取详情');
  const retried=retry.events.click();assert.equal(reads[2].id,'B');
  reads[2].resolve(snapshot('B'));await retried;
  assert.equal(monitor().dataset.readState,'recorded','Previous same-commission read stays visible without a server excerpt');
  assert.ok(text(monitor()).includes('已有生成或复用记录 1 / 2'));passed++;
  assert.equal(overview(),undefined);assert.equal(get('project-title').textContent,'详情 B');
  assert.equal(run('currentPhase()'),'video');assert.ok(walk(get('stage')).some(e=>e.dataset?.section==='video'));passed++;
  const desk=get('stage').children[0], before=reads.length;
  clickProject(1);
  assert.equal(reads.length,before);assert.equal(get('stage').children[0],desk,'Clicking the current project preserves the mounted video desk');passed++;
  run('setStep("assets")');
  const fullDetail=walk(get('stage')).find(e=>e.id==='studio-asset-detail');
  assert.ok(fullDetail && text(fullDetail).includes('湖畔'),'Selected scene survives the complete workspace read');passed++;
  run('state.projects=[{id:"old",title:"旧工程",shot_count:3}];renderProjects()');
  clickProject(0);
  assert.equal(get('project-title').textContent,'旧工程');assert.ok(text(get('stage')).includes('正在读取制作详情'));
  assert.ok(!text(get('stage')).includes('画面 B'));passed++;
  run('startNew()');reads[3].resolve(snapshot('old'));await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run('state.creating'),true);assert.equal(overview(),undefined);passed++;
  // Cold deep link: receive the project list, then progress while the workspace is still held.
  context.location.hash='#project=B&step=video';
  context.window.addEventListener=()=>{};
  context.setInterval=()=>{};
  run('Object.assign(state,{config:null,projects:[],project:null,projectId:null,creating:false,loading:true});api.bootstrap=async()=>({projects:stories,config:{workspace_snapshot:true}})');
  const deep=run('initialize()');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(overview().dataset.projectOpening,'B');assert.equal(reads.at(-1).id,'B');
  assert.equal(run('currentPhase()'),'video');assert.equal(progressRequests.at(-1)[0],`/api/projects/B/studio/progress?job_id=${videoJob.id}`);
  resolveProgress({status:200,ok:true,json:async()=>({project_id:'B',job_id:videoJob.id,message_id:videoJob.message_id,total:1,completed:0,
    job_status:'running',phase:'generating',preview_gate:{ready:false},shots:[{shot_id:'S2',status:'generating'}]})});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(monitor().dataset.readState,'ready');
  const jobCard=()=>walk(get('job-list')).find(e=>e.dataset?.jobId===videoJob.id);
  assert.ok(text(jobCard()).includes('已生成 0 / 1'));assert.ok(!text(jobCard()).includes('准备工具与输入'));
  const failedRead=run('studioLocal("B").monitor.refresh(true)');
  resolveProgress({status:503,ok:false,json:async()=>({error:'offline failure'})});await failedRead;
  assert.ok(text(jobCard()).includes('上次读取'));assert.ok(text(jobCard()).includes('已生成 0 / 1'));
  context.otherJobs=[{...videoJob,status:'failed',error:'实际失败原因',updated_at:'2026-11-20T12:00:00Z'}];
  run('state.jobs=otherJobs;state.jobsReady=true;renderJobs()');
  assert.ok(text(jobCard()).includes('实际失败原因'));assert.ok(!text(jobCard()).includes('已生成 0 / 1'));
  run('state.projectId="A";renderJobs()');assert.equal(jobCard(),undefined);
  run('state.projectId="B"');passed++;
  reads.at(-1).resolve(snapshot('B'));await deep;
  assert.equal(overview(),undefined);assert.equal(run('currentPhase()'),'video');passed++;
  // Normal cold entry must expose a usable sidebar while the default story is still loading.
  const coldState='Object.assign(state,{config:null,projects:[],storyDrafts:[],project:null,projectOpening:null,projectId:null,storyDraftId:null,creating:false,loading:true,jobs:[],jobsReady:false})';
  context.location.hash='';run(coldState);context.document.documentElement=new Element('html');
  context.performance={now:()=>450,getEntriesByType:()=>[]};
  const initialRead=reads.length;
  run('api.jobs=()=>{throw Error("The modern workspace response already contains jobs")};');
  const home=run('initialize()');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(overview().dataset.projectOpening,'A');assert.equal(run('currentPhase()'),'assets');
  assert.equal(get('project-list').children.length,2);
  assert.equal(context.document.documentElement.dataset.startupSidebarMs,'450.0');
  assert.equal(context.document.documentElement.dataset.startupReadyMs,undefined);
  assert.equal(run('state.jobsReady'),false);passed++;
  clickProject(1);assert.equal(reads[initialRead].signal.aborted,true);
  reads[initialRead].resolve(snapshot('A'));await home;
  assert.equal(overview().dataset.projectOpening,'B');assert.equal(run('state.jobsReady'),false);
  const selected=snapshot('B');
  selected.conversations={video:{session:{project_id:'B'},messages:[],capabilities:{}}};
  reads[initialRead+1].resolve(selected);await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run('state.project.id'),'B');assert.equal(run('state.jobsReady'),true);
  assert.equal(context.document.documentElement.dataset.startupReadyMs,'450.0');passed++;
  context.location.hash='';run(coldState);
  run('api.bootstrap=async()=>({projects:[],config:{workspace_snapshot:true},story_drafts:[],jobs:[]})');
  const afterProjects=reads.length;
  await run('initialize()');assert.equal(run('state.creating'),true);
  assert.equal(reads.length,afterProjects);assert.equal(run('state.jobsReady'),true);passed++;
  context.location.hash='#project=A&step=studio&section=assets';run(coldState);
  run('api.bootstrap=async()=>({projects:[{id:"A",title:"旧工程"}],config:{}});api.project=async id=>({id,title:"旧工程",shots:[],validation:{}});api.jobs=async()=>({jobs:[]})');
  await run('initialize()');
  assert.equal(run('state.project.id'),'A');assert.equal(run('state.jobsReady'),true);
  assert.equal(reads.length,afterProjects,'Old services do not receive a workspace snapshot request');passed++;
  // Direct video links own their project-scoped read. Archive evidence is an explicit later request.
  const vid='887c283c256e0fca',cid='c'.repeat(32);
  context.detailStory = {id:vid,title:'湖边的倒影',shot_count:1,opening:{logline:'已发布故事',entities:[],shots:[]}};
  context.videoDetail = {projection:'video',project_id:vid,video_plan:null,jobs:[],
    video_execution:{commission_id:cid,status:'running',resumable:false,plan:{plan_id:'f'.repeat(64),new_shots:['S001'],retake_shots:[],reuse_shots:[]},
      limits:{max_video_generations:9},usage:{reserved_video_generations:5,remaining_video_generations:4}},
    assets:{spec:{asset_id:'spec',revision_id:'r1'},shots:[{id:'S001',description:'湖边远景',required_refs:[]}]}};
  context.document.documentElement.dataset.videoDetail='true';
  context.location.hash='#project='+vid+'&step=video';run(coldState);
  let finishSidebar, detailReads=0, statusReads=0;
  context.startSidebar=()=>new Promise(resolve=>{finishSidebar=resolve;});
  context.videoRequest=async url=>{
    if(url.includes('/video-detail')){detailReads++;return context.videoDetail;}
    if(url.includes('/pi/sessions'))return {session:{project_id:vid},messages:[],capabilities:{}};
    if(url==='/api/jobs')statusReads++;
    throw Error('Unexpected request '+url);
  };
  run('api.bootstrap=startSidebar;request=videoRequest;api.jobs=()=>request("/api/jobs")');
  const videoOpening=run('initialize()');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(detailReads,1,'The scoped video read starts before the project list is available');
  finishSidebar({projects:[context.detailStory],config:{workspace_snapshot:true,video_detail:true,key_configured:true}});
  await videoOpening;
  assert.equal(detailReads,1);assert.equal(statusReads,0);assert.equal(reads.length,afterProjects);
  assert.equal(run('state.project.projection'),'video');assert.equal(overview(),undefined);
  assert.ok(text(get('stage')).includes(cid));assert.ok(text(get('stage')).includes('S001'));
  assert.ok(text(get('stage')).includes('9 次'));assert.ok(text(get('stage')).includes('4 次'));
  assert.ok(!text(get('stage')).includes('正在读取制作详情'));
  assert.ok(!walk(get('stage')).some(e=>e.tagName==='button'&&e.textContent.includes('开始制作')),'Frozen plans cannot submit another commission');passed++;
  const all=walk(get('stage')).find(e=>e.tagName==='button'&&e.textContent.includes('完整制作记录'));
  all.events.click();assert.equal(reads.length,afterProjects+1,'Attachments use the full workspace only after a user opens it');
  assert.ok(text(get('stage')).includes(cid),'Fetching attachments preserves the active commission');
  const full=snapshot(vid);full.projects=[context.detailStory];reads.at(-1).resolve(full);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run('state.project.projection'),undefined);passed++;
  run(coldState);context.location.hash='#project='+vid+'&step=video';
  run('api.bootstrap=async()=>({projects:[detailStory],config:{workspace_snapshot:true,video_detail:true}});request=async()=>({...videoDetail,project_id:"OTHER"})');
  await run('initialize()');
  assert.ok(text(get('stage')).includes('未能读取制作详情'));
  assert.ok(!text(get('stage')).includes(cid));assert.equal(walk(get('stage')).some(e=>e.className==='studio-panel'),false);
  run('request=videoRequest');walk(get('stage')).find(e=>e.textContent==='重新读取详情').events.click();
  await new Promise(resolve=>setImmediate(resolve));assert.equal(run('state.project.projection'),'video');
  run('setStep("assets")');assert.equal(reads.length,afterProjects+2);
  reads.at(-1).resolve(full);await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run('currentPhase()'),'assets');passed++;
  console.log(`Unified navigation: ${passed} offline checks passed; stage content, legacy links, scoped chat and locked preview verified.`);
})().catch(error=>{console.error(error);process.exitCode=1;});
