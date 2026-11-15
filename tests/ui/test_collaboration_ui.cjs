/* Offline cooperation view checks. No browser traffic or model calls. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.handlers = {};
    this.attributes = {};
    this.classList = {add() {}, remove() {}};
  }
  set innerHTML(_) { throw Error("API fields must be rendered as text"); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  get childElementCount() { return this.children.length; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(event, fn) { this.handlers[event] = fn; }
  focus() { this.focused = true; }
  scrollIntoView() {}
}
function walk(root) { return [root, ...root.children.flatMap(walk)]; }

async function main() {
  const dom = new Map();
  const mounted = id => {
    if (!dom.has(id)) dom.set(id, new Element("div"));
    return dom.get(id);
  };
  const window = new Element("window");
  const script = fs.readFileSync(path.join(__dirname, "../../web/collaborations.js"), "utf8");
  const stub = {
    id: "target", title: "红毯尽头", trace_id: "a".repeat(32), status: "completed",
    stages: [{name: "视频改稿", revision: "r2", status: "completed", trace_id: "b".repeat(32)}],
    productions: [
      {revision: "r1", affected: ["S001"], reused: [], media_accepted: false, preview_url: "/assets/preview"},
      {revision: "r2", affected: ["S003"], reused: ["S001", "S002"], preview_url: "https://untrusted.invalid/",
       changed_sources: ["B003"], reasons: {S003: ["sources"]}, recheck_edges: [["S002", "S003"]]}
    ],
    messages: [
      {type: "script.revision_published", sender: "script", message_id: "first", trace_id: "a".repeat(32)},
      {type: "production.requested", sender: "client", recipient: "video", message_id: "second", causation_id: "first",
       trace_id: "b".repeat(32), receipts: [{agent: "video", status: "accepted", trace_id: "c".repeat(32)}]}
    ]
  };
  let calls = 0;
  const ctx = vm.createContext({document: {getElementById: mounted, querySelector: mounted,
    createElement: tag => new Element(tag)}, window, location: {hash: "#target"}, URLSearchParams,
    fetch: async (url, options) => {
      calls++;
      if (url.startsWith("/api/reference-usage?")) {
        const params = new URLSearchParams(url.split("?")[1]);
        assert.equal(params.get("story_id"), "story-target");
        assert.equal(params.get("asset_id"), "portrait/lin");
        assert.equal(params.get("revision_id"), "r3");
        return {ok:true, json:async () => ({
          reference:{asset_id:"portrait/lin",revision_id:"r3"}, required_shots:[{shot_id:"S003",source:{asset_id:"spec",revision_id:"r1"}}],
          candidates:[{shot_id:"S003",candidate_id:"offline-candidate",evidence_status:"SUBMISSION_UNKNOWN"}],
          unverified_candidates:[{shot_id:"S003",candidate_id:"offline-unbound",evidence_status:"BINDING_UNCONFIRMED",
            http_evidence:{status:"BINDING_UNCONFIRMED"}}],submitted_count:0,accepted_count:0})};
      }
      assert.equal(url, "/api/collaborations");
      assert.equal(options.cache, "no-store");
      return {ok: true, json: async () => ({runs: [{id: "first-run", title: "别的故事"}, stub]})};
    }
  });
  vm.runInContext(script, ctx);
  await new Promise(setImmediate);
  const main = mounted("collab-main");
  let nodes = walk(main);
  const text = nodes.map(node => node.textContent).filter(Boolean).join("\n");
  assert.ok(text.includes("红毯尽头") && text.includes("S001、S002"));
  assert.ok(text.includes("B003") && text.includes("S002 → S003") && text.includes("剧本来源"));
  assert.ok(text.includes("已接受任务") && text.includes("媒体验收待确认"));
  assert.equal(nodes.filter(n => n.tag === "video").length, 1);
  assert.ok(nodes.some(n => n.href === "/#step=traces&trace=" + "c".repeat(32)));
  nodes.find(n => n.textContent === "定位上游消息").handlers.click();
  assert.ok(nodes.some(n => n.open && n.focused && n.tag === "details"));
  const form = nodes.find(n => n.tag === "form");
  const inputs = walk(form).filter(n => n.tag === "input");
  inputs[0].value = "portrait/lin";
  inputs[1].value = "r3";
  await form.handlers.submit({preventDefault() {}});
  const lookupText = walk(form).map(n => n.textContent).filter(Boolean).join("\n");
  assert.ok(lookupText.includes("确认提交候选 0") && lookupText.includes("提交结果待核实") && lookupText.includes("S003 要求此版本"));
  assert.ok(lookupText.includes("参考版本绑定待核实") && lookupText.includes("缺同次提交的资产版本绑定证据"));
  ctx.location.hash = "#id=missing";
  window.handlers.hashchange();
  assert.ok(walk(main).some(n => n.textContent === "未找到此协作任务"));
  ctx.location.hash = "#id=target";
  await mounted("reload").handlers.click();
  assert.equal(calls, 3);
  assert.ok(walk(main).some(n => n.textContent === "红毯尽头"));
  console.log("Cooperation UI: deep links, affected/reused shots, local media, receipt traces, causation and refresh passed (offline).");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
