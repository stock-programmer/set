"use strict";

const panel = document.getElementById("collab-main");
const navigation = document.getElementById("run-list");
const feedback = document.getElementById("page-status");
const reloadButton = document.getElementById("reload");
let runs = [];
let selectedId = "";

function el(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function traceLink(id, title = "查看 Trace ↗") {
  if (!/^[a-f0-9]{32}$/i.test(id || "")) return el("span", "尚无 Trace", "collab-subtle");
  const link = el("a", title, "collab-trace");
  link.href = "/#step=traces&trace=" + encodeURIComponent(id);
  return link;
}

function state(value) {
  const key = String(value || "unknown").toLowerCase();
  const labels = {
    accepted: "已接受任务", blocked: "受阻", cancelled: "已取消", completed: "已完成",
    delivered: "已投递", error: "执行出错", failed: "失败", needs_input: "待补充信息",
    ok: "执行成功", pending: "待处理", received: "已收件", running: "进行中",
    succeeded: "已完成", unknown: "待确认", submitted: "已提交",
    processing: "处理中", queued: "已排队", interrupted: "已中断", waiting_for_provider: "等待供应商账户恢复"
  };
  const style = ["completed", "ok", "succeeded", "accepted"].includes(key) ? " complete" :
    ["failed", "error", "blocked", "interrupted"].includes(key) ? " issue" : "";
  return el("span", labels[key] || value, "collab-state" + style);
}

function textOf(value) {
  if (value === undefined || value === null) return "";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

function shotList(values) {
  if (!Array.isArray(values)) return "待记录";
  if (!values.length) return "无";
  return values.map(item => typeof item === "object" && item ?
    item.shot_id || item.id || textOf(item) : String(item)).join("、");
}

function requestedId() {
  const hash = location.hash.slice(1);
  if (!hash) return "";
  try {
    return hash.startsWith("id=") ? new URLSearchParams(hash).get("id") || "" : decodeURIComponent(hash);
  } catch { return hash; }
}

function row(...nodes) {
  const result = el("div", null, "collab-row");
  result.append(...nodes);
  return result;
}

function stageCards(stages) {
  const section = el("section");
  section.append(el("h2", "Agent 执行过程"));
  for (const stage of stages || []) {
    const card = el("article", null, "collab-card");
    const title = [stage.revision, stage.name].filter(Boolean).join(" · ");
    card.append(row(el("h3", title), state(stage.status)));
    if (stage.summary) card.append(el("p", textOf(stage.summary)));
    card.append(traceLink(stage.trace_id, "打开该阶段的 Trace ↗"));
    section.append(card);
  }
  if (!stages || !stages.length) section.append(el("p", "尚无 Agent 执行记录。", "collab-subtle"));
  return section;
}

function productionCards(productions) {
  const section = el("section");
  section.append(el("h2", "按剧本版本查看制作结果"));
  for (const production of productions || []) {
    const card = el("article", null, "collab-card");
    const versionLabel = production.production_revision && production.production_revision !== production.revision
      ? "制作 " + production.production_revision + " · 剧本 " + production.revision
      : String(production.revision || "待记录") + " · 制作结果";
    card.append(row(el("h3", versionLabel),
      el("span", production.media_accepted === true ? "媒体验收通过" : "媒体验收待确认",
        "collab-state" + (production.media_accepted === true ? " complete" : ""))));
    card.append(el("p", "受影响镜头　" + shotList(production.affected)));
    card.append(el("p", "复用镜头　" + shotList(production.reused)));
    if (Array.isArray(production.changed_sources)) {
      card.append(el("p", "变更的剧本来源　" + shotList(production.changed_sources)));
    }
    if (Array.isArray(production.recheck_edges)) {
      const edges = production.recheck_edges.map(pair => Array.isArray(pair) ? pair.join(" → ") : textOf(pair));
      card.append(el("p", "需复查的镜头衔接　" + shotList(edges)));
    }
    if (production.reasons || production.unresolved) {
      const details = el("details");
      details.append(el("summary", "查看依赖差异与待验收项"));
      const components = {shot: "分镜规格", required_refs: "固定参考素材", entities: "角色与场景资产", events: "剧情事件",
        facts: "剧情状态", sources: "剧本来源", compiled_prompt: "生成提示", duration: "时长"};
      for (const [id, changed] of Object.entries(production.reasons || {})) {
        if (!Array.isArray(changed)) continue;
        const reason = changed.length ? changed.map(key => components[key] || key).join("、") : "依赖指纹相同";
        details.append(el("p", id + "　" + reason));
      }
      for (const [id, pending] of Object.entries(production.unresolved || {})) {
        if (Array.isArray(pending) && pending.length) details.append(el("p", id + " 待验收　" + shotList(pending)));
      }
      card.append(details);
    }
    if (/^\/assets\/[a-zA-Z0-9_-]+$/.test(production.preview_url || "")) {
      const video = el("video");
      video.controls = true;
      video.preload = "none";
      video.playsInline = true;
      video.src = production.preview_url;
      video.setAttribute("aria-label", String(production.revision) + " 制作预览");
      card.append(video);
      const open = el("a", "单独打开预览", "collab-trace");
      open.href = production.preview_url;
      card.append(open);
    } else card.append(el("small", "尚无可播放的预览。", "collab-subtle"));
    section.append(card);
  }
  if (!productions || !productions.length) section.append(el("p", "尚无制作版本。", "collab-subtle"));
  return section;
}

function messagesSection(messages) {
  const section = el("section");
  const cards = new Map();
  section.append(el("h2", "消息与回执"));
  for (const message of messages || []) {
    const card = el("details", null, "collab-card");
    card.tabIndex = -1;
    const title = [message.type, message.sender + " → " + (message.recipient || "订阅者")];
    card.append(el("summary", title.join("　")));
    cards.set(message.message_id, card);
    card.append(el("p", "消息 ID　" + message.message_id, "collab-ids"));
    const causeId = message.causation_id;
    if (causeId) {
      const button = el("button", "定位上游消息", "button button-small");
      button.type = "button";
      button.addEventListener("click", () => {
        const upstream = cards.get(causeId);
        if (!upstream) {
          feedback.textContent = "当前记录未包含上游消息 " + causeId;
          return;
        }
        upstream.open = true;
        upstream.focus();
        upstream.scrollIntoView({block: "center"});
      });
      card.append(button);
    }
    if (message.payload) {
      const detail = el("details");
      detail.append(el("summary", "版本与消息内容"), el("pre", textOf(message.payload), "collab-pre"));
      card.append(detail);
    }
    const list = el("ul", null, "collab-receipts");
    for (const receipt of message.receipts || []) {
      const line = el("li", (receipt.agent || "接收方") + "　");
      line.append(state(receipt.status));
      if (receipt.trace_id) line.append(traceLink(receipt.trace_id, "回执 Trace ↗"));
      list.append(line);
    }
    if (list.childElementCount) card.append(list);
    else card.append(el("p", "尚无回执。"));
    card.append(traceLink(message.trace_id, "消息 Trace ↗"));
    section.append(card);
  }
  if (!messages || !messages.length) section.append(el("p", "尚无协作消息。", "collab-subtle"));
  return section;
}

function referenceLookup(run) {
  const section = el("section");
  const form = el("form", null, "collab-card");
  const id = el("input");
  const revision = el("input");
  id.required = revision.required = true;
  id.maxLength = revision.maxLength = 256;
  revision.placeholder = "r1";
  revision.pattern = "r[1-9][0-9]*";
  const submit = el("button", "查询引用", "button button-small");
  submit.type = "submit";
  const output = el("div");
  output.setAttribute("aria-live", "polite");
  const idLabel = el("label", "参考资产 ID ");
  idLabel.append(id);
  const revisionLabel = el("label", "版本 ");
  revisionLabel.append(revision);
  form.append(row(idLabel, revisionLabel, submit), output);
  section.append(el("h2", "参考素材反查"), form);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    output.replaceChildren(el("p", "正在读取版本和候选记录…"));
    try {
      const query = new URLSearchParams({asset_id: id.value.trim(), revision_id: revision.value.trim(),
        story_id: "story-" + run.id});
      const response = await fetch("/api/reference-usage?" + query, {cache:"no-store"});
      const result = await response.json();
      if (!response.ok) throw Error(result.error || "查询失败");
      output.replaceChildren(el("p", result.reference.asset_id + " @ " + result.reference.revision_id),
        el("p", "要求记录 " + result.required_shots.length + " · 确认提交候选 " + result.submitted_count +
          " · 供应商接受 " + result.accepted_count));
      for (const spec of result.required_shots) {
        output.append(el("p", spec.shot_id + " 要求此版本 · " + spec.source.asset_id + " @ " + spec.source.revision_id));
      }
      const states = {ACCEPTED:"供应商已接受", PREPARED:"预检完成", SUBMISSION_UNKNOWN:"提交结果待核实",
        PROVIDER_REJECTED:"供应商已拒绝", LEGACY_UNKNOWN:"缺少版本回执", INVALID_RECEIPT:"回执校验失败",
        INVALID_PIN:"参考绑定不匹配", RECEIPT_UNCONFIRMED:"任务清单的证据待补齐", BINDING_UNCONFIRMED:"参考版本绑定待核实", TRANSPORT_UNCONFIRMED:"提交与响应待核实",
        JOURNAL_UNAVAILABLE:"任务清单不可用"};
      for (const item of [...result.candidates, ...result.unverified_candidates]) {
        const details = el("details");
        details.append(el("summary", (item.shot_id || "未知镜头") + " · " + (states[item.evidence_status] || item.evidence_status)),
          el("p", "候选 " + item.candidate_id + (item.task_id ? " · 任务 " + item.task_id : "")));
        if (item.job_status) details.append(el("p", "生成任务状态　" + item.job_status));
        if (Array.isArray(item.selected_in)) {
          const chosen = item.selected_in.map(entry => entry.production.asset_id + " @ " + entry.production.revision_id);
          details.append(el("p", chosen.length ? "选用版本　" + chosen.join("、") : "此资产库未记录选用"));
        }
        const transport = item.http_evidence;
        if (transport) {
          const labels = {MATCHED_RESPONSE:"实际 HTTP 请求与回执已核对", REQUEST_ONLY:"Trace 已记录出站请求，送达待确认",
            RESPONSE_UNCORRELATED:"响应尚未与此候选关联", BINDING_UNCONFIRMED:"已记录 HTTP 请求，缺同次提交的资产版本绑定证据",
            NO_TRANSPORT_RECORD:"仅有任务清单回执", CONFLICT:"HTTP Trace 与清单回执冲突"};
          details.append(el("p", labels[transport.status] || transport.status));
        }
        if (item.trace_id) details.append(traceLink(item.trace_id, "打开候选 Trace ↗"));
        output.append(details);
      }
      for (const edge of result.archived_media_dependencies || []) {
        output.append(el("p", "归档媒体依赖　" + edge.shot_id + " · " + edge.media.asset_id + " @ " + edge.media.revision_id));
      }
      if (result.coverage?.unreadable_journals) output.append(el("p", "有任务清单无法读取，查询覆盖范围受限。"));
      if (result.coverage?.journal_directory_exists === false) output.append(el("p", "当前视频任务目录不可用。"));
    } catch (error) {
      output.replaceChildren(el("p", error.message, "collab-error"));
    } finally {
      submit.disabled = false;
    }
  });
  return section;
}

function render() {
  selectedId = requestedId() || (runs[0] && runs[0].id);
  navigation.replaceChildren();
  for (const run of runs) {
    const link = el("a", run.title || run.id);
    link.href = "#" + encodeURIComponent(run.id);
    if (run.id === selectedId) link.setAttribute("aria-current", "page");
    navigation.append(link);
  }
  const run = runs.find(item => item.id === selectedId);
  panel.replaceChildren();
  panel.setAttribute("aria-busy", "false");
  if (!run) {
    panel.append(el("h1", runs.length ? "未找到此协作任务" : "暂无协作记录"),
      el("p", runs.length ? "可以从任务列表选择其他记录。" : "完成首个协作任务后，在这里查看执行过程。", "collab-subtle"));
    return;
  }
  document.title = (run.title || run.id) + " · Agent 协作记录 · 片场";
  const heading = el("div", null, "collab-head");
  heading.append(el("h1", run.title || run.id), state(run.status));
  panel.append(heading, el("p", "协作任务　" + run.id, "collab-ids"),
    traceLink(run.trace_id, "打开协作任务 Trace ↗"), stageCards(run.stages),
    productionCards(run.productions));
  if (run.change_proposal) {
    const change = run.change_proposal;
    const card = el("section", null, "collab-card");
    card.append(el("h2", "改稿委托 · " + change.beat), el("p", change.requested_change),
      el("p", "首版中直接引用该节拍的镜头　" + shotList(change.direct_consumers)),
      el("p", "需要复查的衔接　" + (change.adjacent_checks || []).map(pair => pair.join(" → ")).join("、")),
      el("small", change.basis, "collab-subtle"));
    panel.append(card);
  }
  panel.append(referenceLookup(run), messagesSection(run.messages));
}

async function refresh() {
  if (reloadButton.disabled) return;
  reloadButton.disabled = true;
  feedback.classList.remove("collab-error");
  feedback.textContent = "正在刷新…";
  try {
    const response = await fetch("/api/collaborations", {cache: "no-store"});
    if (!response.ok) throw new Error("HTTP " + response.status);
    const result = await response.json();
    if (!Array.isArray(result.runs)) throw new Error("协作记录格式错误");
    runs = result.runs;
    render();
    feedback.textContent = "已刷新　" + new Date().toLocaleTimeString("zh-CN");
  } catch (error) {
    feedback.classList.add("collab-error");
    feedback.textContent = "刷新失败：" + error.message;
    panel.setAttribute("aria-busy", "false");
    if (!runs.length) panel.replaceChildren(el("h1", "协作记录暂不可用"));
  } finally { reloadButton.disabled = false; }
}
reloadButton.addEventListener("click", refresh);
document.querySelector(".skip-link").addEventListener("click", event => {
  event.preventDefault();
  panel.focus();
});
window.addEventListener("hashchange", render);
refresh();
