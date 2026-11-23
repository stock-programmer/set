/* Offline progress and playback checks. No browser service or provider calls. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.dataset={}; this.attrs={}; this.events={}; this.srcWrites=0; }
  append(...kids) { for (const k of kids) { k.parent=this; this.children.push(k); } }
  setAttribute(k,v) { this.attrs[k]=v; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(k,f) { this.events[k]=f; }
  remove() { this.parent.children=this.parent.children.filter(c=>c!==this); }
  set src(value) { this.attrs.src=value; this.srcWrites++; }
  get src() { return this.attrs.src; }
  load() {}
}
const walk = n=>[n,...n.children.flatMap(walk)];
const origin="http://127.0.0.1:8765", hash="b".repeat(64), job="1".repeat(32);
const media=sid=>`/api/projects/A/studio/progress/media?job_id=${job}&shot_id=${sid}&sha256=${hash}`;
const shot=(id,ready,verdict="PENDING")=>({shot_id:id,status:ready?"ready":"generating",operation:"new",
  ...(ready?{media_sha256:hash,preview_url:media(id),acceptance_status:verdict}:{})});
let responseStatus=200, data={project_id:"A",job_id:job,total:3,completed:1,phase:"generating",job_status:"running",reason:"正在准备工具与输入",
  preview_gate:{ready:false},current_shot_id:"S002",shots:[shot("S001",true),shot("S002",false),{shot_id:"S003",status:"pending",operation:"new"}]};
const requests=[];
const ctx=vm.createContext({URL,URLSearchParams,AbortController,Date,Map,Set,window:{location:{origin}},document:{createElement:tag=>new Element(tag)},
  fetch:async (url,options)=>{requests.push([url,options]);return {status:responseStatus,ok:responseStatus===200,json:async()=>data};}});
vm.runInContext(fs.readFileSync("web/studio.js","utf8"),ctx);
const run=x=>vm.runInContext(x,ctx);
async function main() {
  const messageId='d'.repeat(32);
  ctx.receipt={project_id:'A',job_id:job,message_id:messageId,recorded_at:'2026-11-04T18:00:00Z',shot_count:1,completed_count:1,
    shots:[{shot_id:'S001',status:'generated',operation:'new',preview_url:media('S001')}],preview_gate:{ready:true}};
  const historical=run(`studioProgressMonitor("A","${job}",{recorded:receipt,messageId:"${messageId}"})`);
  const historyRows=walk(historical.element).filter(e=>e.tag==='li');
  assert.equal(historical.element.dataset.readState,'recorded');assert.equal(historyRows[0].dataset.status,'generated');
  assert.ok(!walk(historical.element).some(e=>e.src || e.tag==='button'));
  for (const mismatch of [{job_id:'1'}, {project_id:'B'}, {message_id:'different'}, {shot_count:2}, {completed_count:0}]) {
    ctx.mismatch=mismatch;
    assert.equal(run(`studioRecordedProgress({...receipt,...mismatch},"A","${job}","${messageId}")`),null);
  }
  responseStatus=503;await historical.refresh();
  assert.equal(historical.element.dataset.readState,'error');assert.equal(historyRows[0].dataset.status,'generated');
  assert.ok(walk(historical.element).some(e=>e.textContent?.includes('显示上次记录')));responseStatus=200;
  const m=run(`studioProgressMonitor("A","${job}",{standalone:true})`);
  await m.refresh();
  const video=walk(m.element).find(n=>n.tag==="video");
  assert.ok(!walk(m.element).some(n=>n.textContent==="正在准备工具与输入"),"A preparation message is superseded by actual generated shots");
  const row=(sid)=>walk(m.element).find(n=>n.tag==="li" && n.children[0].textContent===sid);
  const verdict=id=>walk(row(id)).find(n=>n.className==="live-shot-acceptance");
  assert.equal(video.src,media("S001"),"The generated shot can be watched while acceptance is pending");
  assert.equal(row("S001").children[3].hidden,false);
  assert.ok(walk(row("S001")).some(n=>n.textContent==="已生成"));
  assert.equal(verdict("S001").textContent,"待验收");
  assert.equal(verdict("S002").hidden,true);assert.equal(row("S002").children[3].hidden,true);
  data={...data,phase:"completed",job_status:"succeeded",completed:3,
    shots:[shot("S001",true,"FAIL"),shot("S002",true,"UNKNOWN"),{...shot("S003",true,"PASS"),status:"reused",operation:"reuse"}]};
  await m.refresh(true);assert.equal(video.src,media("S001"));
  assert.ok(walk(m.element).some(n=>n.textContent==="制作任务已结束"));
  assert.ok(walk(m.element).some(n=>n.textContent?.includes("整片待交付")));
  assert.equal(verdict("S001").textContent,"验收未通过");
  assert.equal(verdict("S002").textContent,"验收待确认");
  assert.equal(verdict("S003").textContent,"验收通过");
  assert.ok(walk(m.element).some(n=>n.textContent==="S001 · 镜头预览 · 验收未通过"));
  assert.equal(row("S003").children[3].hidden,false);
  assert.ok(walk(m.element).some(n=>n.textContent.includes("3 / 3")));
  data={...data,preview_gate:{ready:true}};await m.refresh(true);
  assert.ok(!walk(m.element).some(n=>n.textContent==="整片验收通过 · 可交付"));
  data={...data,production_complete:true,shots:["S001","S002","S003"].map(id=>shot(id,true,"PASS"))};await m.refresh(true);
  assert.ok(walk(m.element).some(n=>n.textContent==="整片验收通过 · 可交付"));
  data={...data,message_id:messageId,production_complete:false,preview_gate:{ready:true,request_id:messageId},
    job_status:"failed",phase:"stopped",shots:[{...shot("S001",true,"FAIL"),human_approved:true},...data.shots.slice(1)]};
  await m.refresh(true);
  assert.equal(verdict("S001").textContent,"已人工通过");
  assert.ok(walk(m.element).some(n=>n.textContent==="成片已交付 · 可预览"));
  video.currentTime=3; video.paused=false;
  data={...data,preview_gate:{ready:false},production_complete:false,job_status:"failed",phase:"stopped",reason:"确认前中断",current_shot_id:null,completed:2,
    shots:[shot("S001",true),shot("S002",true),{shot_id:"S003",status:"not_started",operation:"new"}]};
  await m.refresh(true);
  assert.equal(video.srcWrites,1,"Changing the delivery gate or verdict preserves playback of the same clip");
  assert.equal(video.currentTime,3);
  assert.equal(row("S003").children[3].hidden,true);
  row("S002").children[3].events.click(); assert.equal(video.src,media("S002"));
  assert.ok(walk(m.element).some(n=>n.textContent==="制作未完成 · 已保留进度"));
  const count=requests.length; await m.refresh(); assert.equal(requests.length,count);
  for (const url of ["https://example.com"+media("S001"), media("S002"), media("S001").replace(job,"other")]) {
    ctx.bad={...shot("S001",true),preview_url:url}; assert.equal(run(`studioClipPreviewUrl("A","${job}",bad)`),null);
  }
  responseStatus=503; await m.refresh(true);
  assert.equal(m.element.dataset.readState,"error");
  assert.equal(video.src,undefined,"A failed refresh clears a previously verified source");
  assert.equal(row("S002").children[3].hidden,true);
  responseStatus=404;
  const legacy=run(`studioProgressMonitor("A","${job}")`);
  await legacy.refresh(); const read=requests.length; await legacy.refresh(); assert.equal(requests.length,read);
  const link=walk(legacy.element).find(n=>n.href?.startsWith("/progress?"));
  assert.equal(link.hidden,true); assert.ok(link.href.endsWith("job="+job));
  responseStatus=200;await legacy.refresh(true);assert.equal(link.hidden,false);
  assert.ok(requests.every(([url,opts])=>url===`/api/projects/A/studio/progress?job_id=${job}` && opts.method==="GET" && opts.priority==="high"));
  let finishRead, pendingSignal;
  ctx.fetch=async (url,options)=>{pendingSignal=options.signal;return new Promise(resolve=>{finishRead=resolve;});};
  const pending=m.refresh(true);m.cancel();
  assert.equal(pendingSignal.aborted,true);
  finishRead({status:200,ok:true,json:async()=>data});await pending;
  assert.equal(video.src,undefined,"A late response to a cancelled read cannot restore media access");
  console.log("Offline progress: scoped reads, shot preview and acceptance, episode gate, interrupted jobs and preserved playback passed.");
}
main().catch(err=>{console.error(err);process.exitCode=1;});
