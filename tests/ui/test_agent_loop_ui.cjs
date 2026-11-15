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
  run('studioRefresh=async()=>{};studioRedraw=()=>{};renderJobs=()=>{};renderStage=()=>{};');
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
    assert.equal(h.calls[0].body.role,'video');assert.equal(h.calls[0].body.mode,'work');
    assert.equal(h.calls[0].body.scope.project,'A');assert.ok(h.calls[0].body.text.includes('c'.repeat(32)));
    assert.equal(h.run('studioLocal("A").data.video_execution.usage.remaining_video_generations'),1);passed++;
    await h.run('studioContinueConversation("A","d".repeat(32))');assert.equal(h.calls.length,2,'Stale commission is rejected');
    h.run('state.projectId="B"');await h.run('studioContinueConversation("A",cid)');assert.equal(h.calls.length,2,'Stale story is rejected');passed++;
  }
  {
    h.run('state.projectId="A";api.project=async id=>({id,title:"故事A",shots:[]})');
    await h.run('openJobResult({kind:"agent",project_id:"A",status:"interrupted"})');
    assert.equal(h.run('currentPhase()'),'video');assert.equal(h.run('studioLocal("A").tool'),'conversation');passed++;
  }
  console.log(`Unified video execution UI: ${passed} offline checks passed; no model or network calls.`);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
