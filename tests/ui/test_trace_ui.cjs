/* Pure offline UI behavior checks: no browser, server, network or model calls. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.dataset = {}; this.textContent = ""; this.value = ""; this.className = ""; this.open = false; this.disabled = false; this.hidden = false; this.classList = { toggle: () => {} }; }
  set innerHTML(_) { throw new Error("UI must not assign HTML from trace payloads"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  get childElementCount() { return this.children.length; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(kind, callback) { this.listeners[kind] = callback; }
  async fire(kind) { if (this.listeners[kind]) return this.listeners[kind]({ preventDefault() {} }); }
  querySelectorAll(tag) { return walk(this).filter((item) => item !== this && item.tagName === tag); }
  focus() {}
  remove() {}
}
function walk(root) { return [root, ...root.children.flatMap((item) => typeof item === "object" ? walk(item) : [])]; }
function text(root) { return walk(root).map((item) => item.textContent).join("\n"); }
const elements = new Map();
const calls = [];
const context = vm.createContext({
  console, URL, URLSearchParams, Date, Map, Set, JSON, Number, String, Object, Array, Promise,
  setTimeout: () => {}, setInterval: () => {}, queueMicrotask,
  location: { hash: "" }, history: { replaceState: (_, __, hash) => { context.location.hash = hash; } },
  window: { location: { origin: "http://127.0.0.1:8765" }, crypto: { randomUUID: () => "test-ui" }, addEventListener() {} },
  document: { body: new Element("body"), activeElement: null, createElement: (tag) => new Element(tag),
    getElementById: (id) => { if (!elements.has(id)) elements.set(id, new Element("div")); return elements.get(id); }, querySelectorAll: () => [] },
  fetch: async (url, options) => { calls.push({ url, method: options.method }); return { ok: true, json: async () => ({ sha256: "a".repeat(64), bytes: 30, media_type: "application/json", data: { messages: [{ role: "user", content: "完整上下文\n第二行" }] } }) }; },
});
const source = fs.readFileSync(path.join(__dirname, "../../web/app.js"), "utf8").replace(/initialize\(\);\s*$/, "");
vm.runInContext(source, context, { filename: "web/app.js" });
const run = (script) => vm.runInContext(script, context);

async function main() {
  let count = 0;
  assert.equal(text(run('traceStatus("UNKNOWN", false)')), "未结束"); count++;
  assert.equal(text(run('traceStatus("OK", true)')), "执行完成"); count++;

  const malicious = run('traceValue("模型响应", "<img src=x onerror=alert(1)>\\n下一行", {open:true})');
  assert.ok(text(malicious).includes("<img src=x onerror=alert(1)>\n下一行"));
  assert.equal(walk(malicious).some((item) => item.tagName === "img"), false); count++;

  const masked = run('traceValue("输入", {Authorization:"Bearer secret", api_key:"private", token_usage:1137, body:"sk-abcdefghijklmnopqrstuv"}, {open:true})');
  for (const detail of walk(masked).filter((item) => item.tagName === "details")) { detail.open = true; await detail.fire("toggle"); }
  assert.ok(!text(masked).includes("Bearer secret") && !text(masked).includes("private"));
  assert.ok(text(masked).includes("1137") && text(masked).includes("[REDACTED]")); count++;

  const artifact = run('traceValue("大内容", {$artifact:"a".repeat(64),bytes:120000,media_type:"application/json"}, {open:true})');
  assert.equal(calls.length, 0);
  await walk(artifact).find((item) => item.tagName === "button").fire("click");
  assert.equal(calls.length, 1); assert.equal(calls[0].url, "/api/trace-artifacts/" + "a".repeat(64)); assert.equal(calls[0].method, "GET");
  assert.ok(text(artifact).includes("完整内容")); count++;

  const invalid = run('traceValue("错误标识", {$artifact:"../../.env",bytes:12,media_type:"application/json"}, {open:true})');
  assert.ok(text(invalid).includes("标识无效")); assert.equal(calls.length, 1); count++;

  const literal = run('traceValue("用户内容", {$literal:{$artifact:"a".repeat(64),bytes:120000,media_type:"application/json"},$literal_reason:"user_artifact_shape"}, {open:true})');
  assert.ok(text(literal).includes("普通 JSON") && text(literal).includes('"$artifact"'));
  assert.equal(walk(literal).some((item) => item.tagName === "button"), false); assert.equal(calls.length, 1); count++;
  const nestedLiteral = run('traceValue("嵌套输入", {payload:{$literal:{$artifact:"a".repeat(64),bytes:120000,media_type:"application/json",nested:{$artifact:"b".repeat(64),bytes:12,media_type:"application/json"}},$literal_reason:"user_artifact_shape"}}, {open:true})');
  for (const detail of walk(nestedLiteral).filter((item) => item.tagName === "details")) { detail.open = true; await detail.fire("toggle"); }
  assert.ok(text(nestedLiteral).includes("普通 JSON")); assert.equal(walk(nestedLiteral).some((item) => item.tagName === "button"), false); assert.equal(calls.length, 1); count++;
  const extraKeys = run('traceValue("同名字段", {$artifact:"a".repeat(64),bytes:12,media_type:"application/json",ordinary:true}, {open:true})');
  assert.equal(walk(extraKeys).some((item) => item.tagName === "button"), false); assert.equal(calls.length, 1); count++;

  run('state.projectId="A";state.creating=false;state.jobs=[{id:"old",kind:"story",project_id:"A",status:"failed",message:"failure",created_at:"2026-11-04T11:00:00Z"}]; renderJobs()');
  assert.ok(text(elements.get("job-list")).includes("历史任务：未采集 Trace")); count++;
  run('state.jobs[0].trace_id="b".repeat(32); renderJobs()');
  assert.ok(text(elements.get("job-list")).includes("查看 Trace")); assert.ok(!text(elements.get("job-list")).includes("历史任务")); count++;

  const sorted = run('renderTraceEvents([{seq:20,trace_seq:2,name:"second",timestamp:"2026-11-04T11:00:01Z"},{seq:10,trace_seq:1,name:"first",timestamp:"2026-11-04T11:00:00Z"}])');
  assert.ok(text(sorted).indexOf("first") < text(sorted).indexOf("second")); count++;

  const cyclic = run('renderSpanTree([{span_id:"a",parent_span_id:"b",name:"A",kind:"tool",status:"UNKNOWN",end_recorded:false},{span_id:"b",parent_span_id:"a",name:"B",kind:"tool",status:"UNKNOWN",end_recorded:false}])');
  assert.ok(text(cyclic).includes("循环") && text(cyclic).includes("A") && text(cyclic).includes("B")); count++;

  run('state.step="traces"; state.projectId="project-should-not-appear"; state.shotId="S001"; state.trace.id="b".repeat(32); persistRoute()');
  assert.equal(context.location.hash, "#step=traces&trace=" + "b".repeat(32)); count++;

  run('state.trace.detail={trace_id:"b".repeat(32),name:"example",kind:"task",status:"UNKNOWN",end_time:"2026-11-04T11:00:00Z",open_span_count:1,spans:[],events:[],events_has_more:true,next_after:1000};state.trace.detailError=null;state.trace.detailLoading=false');
  const page = run('renderTraceDetail()');
  assert.ok(text(page).includes("未结束") && text(page).includes("加载后续事件"));
  const download = walk(page).find((item) => item.tagName === "a");
  assert.equal(download.href, "/api/traces/" + "b".repeat(32) + "/export"); count++;

  const regularDetail = run("state.trace.detail");
  context.regularDetail = regularDetail;
  run('state.trace.detail={...regularDetail,name:"collaboration.commission.lifecycle",attributes:{correlation_id:"1".repeat(32)},spans:[{span_id:"root",name:"root",parent_span_id:null,output:{stages:[{name:"剧本改稿",revision:"r2",trace_id:"3".repeat(32)}]}}],events:[{name:"collaboration.stage",data:{name:"剧本首次发布",revision:"r1",trace_id:"2".repeat(32)}},{name:"collaboration.stage",data:{trace_id:"../../bad"}}]}');
  const collabLinks = walk(run("renderTraceDetail()")).filter(item => item.href).map(item => item.href);
  assert.ok(collabLinks.includes("/collaborations#" + "1".repeat(32)));
  assert.ok(collabLinks.includes("/#step=traces&trace=" + "2".repeat(32)));
  assert.ok(collabLinks.includes("/#step=traces&trace=" + "3".repeat(32)));
  assert.ok(!collabLinks.some(href => href.includes("../"))); count++;
  run('state.trace.detail={...state.trace.detail,attributes:{correlation_id:"../wrong"}}');
  assert.ok(!walk(run("renderTraceDetail()")).some(item => item.href?.startsWith("/collaborations#"))); count++;
  run("state.trace.detail=regularDetail");
  assert.ok(!walk(run("renderTraceDetail()")).some(item => item.href?.startsWith("/collaborations#"))); count++;

  context.fetch = async (url, options) => { calls.push({ url, method: options.method }); return { ok: true, json: async () => ({ events: [{seq:1001,trace_seq:2,span_id:"span",name:"next"},{seq:1000,trace_seq:1,span_id:"span",name:"first"}], has_more:false,next_after:2 }) }; };
  run('state.trace.detail.events=[{seq:1000,trace_seq:1,span_id:"span",name:"first"}];state.trace.detail.spans=[{span_id:"span",events:[]}];state.trace.detail.next_after=1;state.step="story"');
  await run('loadMoreTraceEvents()');
  assert.equal(run('state.trace.detail.events.length'), 2); assert.equal(run('state.trace.detail.spans[0].events.length'), 2);
  assert.equal(run('state.trace.detail.events_has_more'), false); assert.ok(calls.at(-1).url.endsWith("/events?after=1&limit=1000")); count++;

  let releaseList;
  context.pendingTraceList = new Promise((resolve) => { releaseList = resolve; }); context.traceDetailCalls = [];
  run('api.traces=()=>pendingTraceList;api.trace=async(id)=>{traceDetailCalls.push(id);return {trace_id:id,name:"已知 Trace 详情",spans:[],events:[]}}');
  const linked = run('openTraceDesk("c".repeat(32))');
  await new Promise(setImmediate);
  assert.deepEqual(context.traceDetailCalls, ["c".repeat(32)], "A linked detail starts before the list resolves");
  assert.equal(run('state.trace.listLoading'), true); assert.equal(run('state.trace.detail.trace_id'), "c".repeat(32));
  assert.ok(text(elements.get("stage")).includes("已知 Trace 详情"));
  releaseList({ traces: [] }); await linked; count++;

  let releaseOldDetail;
  context.pendingOldDetail = new Promise((resolve) => { releaseOldDetail = resolve; });
  run('api.traces=async()=>({traces:[]});api.trace=async(id)=>id==="d".repeat(32)?pendingOldDetail:{trace_id:id,name:"新选择",spans:[],events:[]}');
  const oldOpening = run('openTraceDesk("d".repeat(32))');
  await run('selectTrace("e".repeat(32))');
  releaseOldDetail({ trace_id: "d".repeat(32), name: "旧响应", spans: [], events: [] }); await oldOpening;
  assert.equal(run('state.trace.id'), "e".repeat(32)); assert.equal(run('state.trace.detail.name'), "新选择", "Late linked details cannot replace a newer selection"); count++;

  run('api.traces=async()=>({traces:[{trace_id:"f".repeat(32),name:"默认首条",status:"OK"}]});api.trace=async(id)=>{traceDetailCalls.push(id);return {trace_id:id,name:"默认首条",spans:[],events:[]}}');
  await run('openTraceDesk()'); assert.equal(run('state.trace.id'), "f".repeat(32)); assert.equal(run('state.trace.detail.name'), "默认首条"); count++;
  const beforeInvalid = context.traceDetailCalls.length;
  await run('openTraceDesk("../invalid")');
  assert.equal(run('state.trace.detailError'), "Trace 标识格式无效。"); assert.equal(context.traceDetailCalls.length, beforeInvalid, "An invalid ID never falls back to loading another detail"); count++;

  const traceId = "a".repeat(32), errorId = "1".repeat(32), decisionId = "2".repeat(32);
  context.spanFixture = [{span_id:errorId, name:"失败的调用", status:"ERROR"},
    {span_id:decisionId, parent_span_id:errorId, name:"依赖分析", status:"OK"}];
  run('state.config={}; state.step="story"; state.trace.view="events"; api.traces=async()=>({traces:[]}); api.trace=async id=>({trace_id:id,name:"制作",spans:spanFixture,events:[]})');
  const traceUrl = "#step=traces&trace=" + traceId;
  context.location.hash = traceUrl + "&span=" + decisionId;
  await run("applyLocationRoute()");
  assert.equal(run("state.trace.spanId"), decisionId); assert.equal(run("state.trace.view"), "spans");
  assert.ok(text(walk(elements.get("stage")).find(item => item.className === "trace-selected-span")).includes("依赖分析")); count++;
  const tree = run("renderSpanTree(spanFixture)");
  await walk(tree).find(item => item.tagName === "button" && text(item).includes("失败的调用")).fire("click");
  assert.equal(context.location.hash, traceUrl + "&span=" + errorId); count++;
  run('state.step="story";state.trace.detail=null;state.trace.spanId=null');
  await run("applyLocationRoute()"); assert.equal(run("state.trace.spanId"), errorId); count++;
  context.location.hash = traceUrl + "&span=" + "f".repeat(32);
  await run("applyLocationRoute()"); assert.equal(context.location.hash, traceUrl + "&span=" + errorId);
  context.location.hash = traceUrl + "&span=bad";
  await run("applyLocationRoute()"); assert.equal(run("state.trace.spanId"), errorId); count++;
  // The final diagnostic identifies the failing check even when the technical
  // tool returned OK. Earlier handled lookup errors are not this asset's cause.
  const loopId = "3".repeat(32), checkId = "4".repeat(32), lookupId = "5".repeat(32);
  const focusFixture = {trace_id:traceId, status:"ERROR", events:[], spans:[
    {span_id:errorId, name:"批次", status:"ERROR", parent_span_id:null, error:{message:"批次失败"}},
    {span_id:decisionId, name:"可复用的成功镜头", status:"OK", parent_span_id:errorId},
    {span_id:lookupId, name:"已处理的查找错误",status:"ERROR",parent_span_id:decisionId},
    {span_id:loopId,name:"资产生产循环",status:"OK",parent_span_id:errorId},
    {span_id:checkId,name:"核验一张图",status:"OK",parent_span_id:loopId}],
    asset_diagnostics:{items:[{name:"铜钥匙",purpose:"prop",stage:"review",verdict:"UNKNOWN",revision_id:"r2",
      trace_id:traceId,span_id:checkId,reason:"静态图里无法看到交接动作。",action:"到对应视频镜头核对交接。",
      evidence:[{path:"subject",label:"对象外观",status:"UNKNOWN",actual:"静帧只看到钥匙。",expected:"交接"}]}]}};
  context.focusFixture = focusFixture;
  run("state.trace.detail=focusFixture; state.trace.id=focusFixture.trace_id; state.trace.showAllSpans=false; state.trace.spanId=selectedTraceSpan(focusFixture.spans)");
  const focused = run("renderTraceDetail()");
  assert.equal(run("state.trace.spanId"), checkId);
  assert.ok(text(focused).includes("1 项资产需处理") && text(focused).includes("静态图里无法看到交接动作。") && text(focused).includes("到对应视频镜头核对交接。"));
  const focusedTree = walk(focused).find(item => item.className === "trace-span-tree");
  assert.ok(text(focusedTree).includes("显示 3 / 5") && text(focusedTree).includes("证据不足"));
  assert.ok(!text(focusedTree).includes("已处理的查找错误") && !text(focusedTree).includes("可复用的成功镜头"));
  assert.ok(walk(focused).some(item => item.href?.endsWith("&span=" + checkId))); count++;
  await walk(focusedTree).find(item=>item.textContent==="展开全部调用").fire("click");
  assert.ok(text(elements.get("stage")).includes("已处理的查找错误"));
  await walk(elements.get("stage")).find(item=>item.textContent==="只看问题调用").fire("click");
  assert.ok(!text(elements.get("stage")).includes("已处理的查找错误")); count++;
  run('state.trace.spanId="' + decisionId + '"');
  assert.ok(text(run("renderSpanTree(focusFixture.spans)")).includes("可复用的成功镜头"),"Explicitly linked successful span remains inspectable"); count++;
  // Semantic problems can be found without downloading the global event tail.
  run('state.trace.detail={...focusFixture,asset_diagnostics:null,problem_spans:[{span_id:"' + checkId + '",status:"FAIL"}],events_has_more:true};state.trace.spanId="' + checkId + '"');
  assert.ok(text(run("renderSpanTree(state.trace.detail.spans)")).includes("检查未通过")); count++;
  context.fetch = async (url, options) => {calls.push({url,method:options.method});return {ok:true,json:async()=>({events:[{seq:7500,name:"后段证据",span_id:checkId}],has_more:false,next_after:7500})};};
  const localEvents = run("selectedSpanEvents(state.trace.detail,state.trace.detail.spans.at(-1))");
  localEvents.open = true; await localEvents.fire("toggle"); await new Promise(setImmediate);
  assert.ok(calls.at(-1).url.includes("span=" + checkId) && text(localEvents).includes("后段证据"),"Read the selected span directly beyond the first global page"); count++;
  assert.ok(calls.every((call) => call.method === "GET"));
  console.log(`Trace UI: ${count} offline behavior checks passed; no model or network calls.`);
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
