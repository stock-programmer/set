"use strict";

// The browser receives configuration status and opaque asset URLs, never credentials.
const state = {
  config: null, projects: [], storyDrafts: [], jobs: [], jobsReady:true, project: null, projectId: null,
  storyDraftId: null, storyDraft: null, storyDraftError: null, storyDraftRefreshedAt: 0,
  step: "story", shotId: null, creating: false, loading: true,
  pending: new Set(), prompts: new Map(), idempotency: new Map(),
  form: { idea: "" }, assetStyle:"",
  cancelPending: new Set(),
  projectRevision: "", projectLoad: 0, projectOpening: null, pollBusy: false, online: true,
  trace: { list: [], id: null, detail: null, spanId: null, view: "spans", showAllSpans: false, filters: { kind: "task", status: "", search: "" },
    offset: 0, limit: 50, listLoading: false, detailLoading: false, listError: null, detailError: null,
    listToken: 0, detailToken: 0, artifacts: new Map(), autoRefresh: false, refreshedAt: null, verifying: false, verification: null },
};
const $ = (id) => document.getElementById(id);
const ACTIVE = new Set(["queued", "running"]);
const studioPurposes = { portrait: "定妆照", voice: "声音", scene: "场景参考", prop: "道具参考", costume: "服装参考" };
const STEP_NAMES = { studio: "制作流程", story: "剧本与分镜", shots: "镜头素材", checks: "镜头验收", export: "预览片", agent: "Agent 执行" };
Object.assign(STEP_NAMES,{assets:"共享资产",video:"视频制作",review:"人工预览"});
function currentPhase(){
  if(state.step!=="studio")return state.step;
  const section=typeof studioLocal==="function"?studioLocal(state.projectId).section:"assets";
  return section==="script"?"story":section;
}
function productionRoute(step,section,tool){
  const legacy={shots:"shots",checks:"checks",agent:"agent"};
  if(legacy[step])return {step:"studio",section:"video",tool:step==="agent"?"conversation":legacy[step]};
  if(step==="export")return {step:"studio",section:"review",tool:null};
  if(step==="studio")step=section==="script"?"story":["assets","video","review"].includes(section)?section:"assets";
  if(["assets","video","review"].includes(step))return {step:"studio",section:step,tool:step==="video"&&["shots","checks","conversation","agent"].includes(tool)?(tool==="agent"?"conversation":tool):null};
  return {step:"story",section:"script",tool:null};
}
const CRITERIA = {
  evidence_integrity: "证据完整性", candidate_binding: "候选与镜头绑定",
  file_decode: "文件完整解码", duration: "镜头时长", resolution: "画面分辨率",
  character_identity: "角色身份一致性", scene_geometry: "场景与机位关系",
  action_sequence: "完整动作与先后顺序", start_end_state: "镜头开始与结束状态",
  script_subtitle: "剧本字幕与实际画面",
  sampled_prop_appearance: "抽样画面中的道具外观", sampled_costume_appearance: "抽样画面中的服装外观",
  reference_submission: "镜头所需参考与实际提交",
};

function node(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined && text !== null) value.textContent = String(text);
  return value;
}
function append(parent, ...children) {
  children.flat().forEach((child) => { if (child) parent.append(child); });
  return parent;
}
function button(label, onClick, { primary = false, quiet = false, small = false, disabled = false, className = "" } = {}) {
  const value = node("button", `button${primary ? " button-primary" : ""}${quiet ? " button-quiet" : ""}${small ? " button-small" : ""}${className ? ` ${className}` : ""}`, label);
  value.type = "button";
  value.disabled = disabled;
  value.addEventListener("click", onClick);
  return value;
}
function pill(status, label) {
  const code = String(status || "UNKNOWN").toUpperCase();
  const types = { PASS: "pass", FAIL: "fail", UNKNOWN: "unknown", SUCCEEDED: "neutral", FAILED: "fail", CANCELED: "neutral", CANCELLED: "neutral", QUEUED: "running", PENDING: "running", RUNNING: "running", INTERRUPTED: "unknown", SUBMISSION_UNKNOWN: "unknown" };
  const labels = { PASS: "通过", FAIL: "失败", UNKNOWN: "未知", SUCCEEDED: "已生成", FAILED: "失败", CANCELED: "已取消", CANCELLED: "已取消", QUEUED: "排队中", PENDING: "排队中", RUNNING: "进行中", INTERRUPTED: "已中断", SUBMISSION_UNKNOWN: "提交待核实" };
  return node("span", `status-pill ${types[code] || "neutral"}`, label || labels[code] || "待处理");
}
function emptyState(title, description, action) {
  const value = node("section", "empty-state");
  append(value, node("span", "empty-symbol", "▤"), node("h2", "", title), node("p", "", description), action);
  return value;
}
function billing(text = "调用已配置的模型，可能计费。请留意后台任务，勿重复提交。") { return node("p", "billing-note", text); }
function note(text, warning = false) { return node("p", `inline-note${warning ? " warning" : ""}`, text); }
function announce(text) { $("screen-reader-status").textContent = text; }
function toast(message, error = false) {
  const item = node("div", `toast${error ? " error" : ""}`);
  const close = button("×", () => item.remove(), { quiet: true });
  close.setAttribute("aria-label", "关闭提示");
  append(item, node("span", "", message), close);
  $("toast-stack").append(item);
  setTimeout(() => item.remove(), error ? 16000 : 6500);
}
function errorText(value) {
  if (typeof value === "string") return value;
  return value?.message || value?.error || "操作未完成，请查看后台任务后再处理。";
}
function assetUrl(value) {
  if (!value || typeof value !== "string") return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && url.pathname.startsWith("/assets/") ? `${url.pathname}${url.search}` : null;
  } catch (_) { return null; }
}
function uuid() { return window.crypto?.randomUUID?.() || `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
async function request(path, { method = "GET", body, signal } = {}) {
  const headers = { Accept: "application/json" };
  const signature = method === "POST" ? `${path}\n${JSON.stringify(body || {})}` : null;
  if (signature) {
    headers["Content-Type"] = "application/json";
    if (!state.idempotency.has(signature)) state.idempotency.set(signature, uuid());
    headers["Idempotency-Key"] = state.idempotency.get(signature);
  }
  let response;
  try {
    response = await fetch(path, { method, headers, credentials: "same-origin", cache: "no-store", ...(signature ? { body: JSON.stringify(body || {}) } : {signal}) });
  } catch (error) {
    if (signal?.aborted && method==="GET") throw error;
    throw new Error(signature ? "连接中断，提交结果暂不明确。请先查看后台任务；相同操作再次提交会复用本次请求标识。" : "暂时无法连接本机服务，正在等待恢复。请保持服务窗口运行。");
  }
  let data;
  try { data = await response.json(); }
  catch (_) { throw new Error("服务未返回有效结果。请查看后台任务，避免重复创建付费任务。"); }
  if (!response.ok) {
    if (response.status < 500 && signature) state.idempotency.delete(signature);
    throw new Error(errorText(data?.error || data?.message || data));
  }
  if (signature) state.idempotency.delete(signature);
  return data;
}
const api = {
  workspace:(id,signal)=>request("/api/workspace"+(id?"?project="+encodeURIComponent(id):""),{signal}),
  bootstrap: () => request("/api/bootstrap?initial=1"),
  project: (id,signal) => request(`/api/projects/${encodeURIComponent(id)}`,{signal}),
  storyDraft: (id) => request(`/api/story-drafts/${encodeURIComponent(id)}`),
  jobs: () => request("/api/jobs"),
  create: (body) => request(body.repair_from ? "/api/projects" : "/api/studio/scripts", { method: "POST", body }),
  action: (id, action, body) => request(`/api/projects/${encodeURIComponent(id)}/${action}`, { method: "POST", body }),
  cancelAgent: (id) => request(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} }),
  traces: (query) => request(`/api/traces?${new URLSearchParams(query)}`),
  trace: (id) => request(`/api/traces/${encodeURIComponent(id)}`),
  traceEvents: (id, after, spanId=null) => request(`/api/traces/${encodeURIComponent(id)}/events?${new URLSearchParams({
    after: String(after || 0), limit: spanId ? "100" : "1000", ...(spanId ? {span:spanId} : {})})}`),
  traceArtifact: (id) => request(`/api/trace-artifacts/${encodeURIComponent(id)}`),
  verifyTrace: (id) => request(`/api/traces/${encodeURIComponent(id)}/verify`),
};
function applyVideoJobs(id,data) {
  if(!Array.isArray(data.jobs) || data.jobs.some(job=>job.project_id!==id))
    throw new Error("未能核对当前工程的任务，请重新读取。");
  state.jobs=[...data.jobs,...state.jobs.filter(job=>jobProjectId(job)!==id)];
  state.jobsReady=true;
}
function shots() { return Array.isArray(state.project?.shots) ? state.project.shots : []; }
function selectedShot() { return shots().find((shot) => shot.id === state.shotId) || shots()[0] || null; }
function selectedCandidate(shot) {
  if (!shot) return null;
  const candidates = Array.isArray(shot.candidates) ? shot.candidates : [];
  return candidates.find((candidate) => candidate.id === shot.selected_candidate_id) || candidates[candidates.length - 1] || null;
}
function readyCandidate(shot) { const candidate = selectedCandidate(shot); return candidate && candidate.status === "SUCCEEDED" && (candidate.media_available===true || assetUrl(candidate.video_url)) ? candidate : null; }
function jobProjectId(job) { return job.result?.project_id || job.project_id || null; }
function assetIssueName(issue) {
  return [issue.name || issue.subject_id, studioPurposes[issue.purpose] || issue.purpose].filter(Boolean).join(" · ");
}
function assetIssueTraceURL(issue) {
  if (!validTraceId(issue.trace_id)) return null;
  const route = new URLSearchParams({step:"traces", trace:issue.trace_id});
  if (validTraceId(issue.span_id)) route.set("span", issue.span_id);
  return "/#" + route;
}
function isStoryJob(job) { return /story|create|build|script/.test(String(job.kind || "")); }
function visibleJobs(jobs = state.jobs) {
  if (state.creating) return [];
  if (state.storyDraftId) return jobs.filter((job) => job.id === state.storyDraftId || job.story_job_id === state.storyDraftId);
  if (!state.projectId) return [];
  return jobs.filter((job) => jobProjectId(job) === state.projectId);
}
function busyProject() {
  return state.jobsReady===false || [...state.pending].some((id) => id !== "create") || visibleJobs().some((job) => ACTIVE.has(job.status));
}
function keyReady(...purposes) {
  const routes = state.config?.model_routes;
  if (!routes) return state.config?.key_configured === true;
  return purposes.length > 0 && purposes.every(purpose => routes[purpose]?.key_configured === true);
}
function toolReady() { return state.config?.ffmpeg_ready === true; }
function readableTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
async function openJobResult(job) {
  const id = jobProjectId(job);
  if (!id) return;
  if (job.kind === "studio_video") {
    const desk = studioLocal(id);
    desk.section = "video"; desk.progressJobId = job.id; desk.tool = null;
    state.step = "studio"; state.shotId = job.shot_id || job.result?.shot_id || null;
    await openProject(id, { preserveStep: true });
    return;
  }
  await openProject(id,{full:true});
  if (state.projectId !== id || !state.project) return;
  state.shotId = job.shot_id || job.result?.shot_id || state.shotId;
  const kind = String(job.kind || "");
  if (kind.startsWith("asset_") || kind === "studio_reference") {
    studioLocal(id).section = "assets";
    setStep("studio");
    return;
  }
  if (kind === "studio_review") {
    studioLocal(id).section = "review"; setStep("studio"); void studioRefresh(id); return;
  }
  setStep(kind.startsWith("studio_") ? "studio" : kind === "agent" ? "agent" : /check|inspect|observe/.test(kind) ? "checks" : /export/.test(kind) ? "export" : /generate|resume/.test(kind) ? "shots" : "story");
}
function persistRoute() {
  const route = new URLSearchParams();
  if (state.step === "traces") {
    route.set("step", "traces");
    if (state.trace.id) route.set("trace", state.trace.id);
    if (state.trace.id && validTraceId(state.trace.spanId)) route.set("span", state.trace.spanId);
    history.replaceState(null, "", `#${route}`);
    return;
  }
  if (state.storyDraftId && !state.creating) {
    route.set("story", state.storyDraftId); route.set("step", "story");
    history.replaceState(null, "", `#${route}`); return;
  }
  if (state.projectId && !state.creating) route.set("project", state.projectId);
  route.set("step", state.creating ? "new" : currentPhase());
  if(state.step==="studio" && currentPhase()==="video" && studioLocal(state.projectId).tool)route.set("tool",studioLocal(state.projectId).tool);
  if (state.shotId && !state.creating) route.set("shot", state.shotId);
  history.replaceState(null, "", `#${route}`);
}
let routeInFlight = null;
async function applyLocationRoute() {
  // replaceState in persistRoute does not emit hashchange/popstate. External
  // navigation is handled here, without resetting drafts or reloading a matching view.
  if (!state.config) return;
  const incomingHash = location.hash;
  if (routeInFlight === incomingHash) return;
  routeInFlight = incomingHash;
  try {
    const route = new URLSearchParams(incomingHash.slice(1));
    if (route.get("step") === "traces") {
      const id = route.get("trace") || null;
      const spanId = validTraceId(route.get("span")) ? route.get("span").toLowerCase() : null;
      if (state.step !== "traces" || state.trace.id !== id) await openTraceDesk(id, spanId);
      else {
        if (spanId) state.trace.view = "spans";
        state.trace.spanId = state.trace.detail ? selectedTraceSpan(state.trace.detail.spans, spanId) : spanId;
        persistRoute(); renderStage();
      }
      if (spanId && state.step === "traces" && state.trace.id === id && state.trace.spanId === spanId) {
        $("stage").querySelector?.(".trace-selected-span")?.scrollIntoView({ block: "start" });
      }
      return;
    }
    if (route.has("story")) {
      const id = route.get("story");
      if (state.storyDraftId !== id || state.step !== "story" || state.loading) await openStoryDraft(id);
      else persistRoute();
      return;
    }
    if (route.get("step") !== "new" && !state.projects.length && state.storyDrafts.length) {
      await openStoryDraft(state.storyDrafts[0].id); return;
    }
    if (route.get("step") === "new" || !state.projects.length) {
      if (!state.creating || state.step !== "story" || state.loading) startNew();
      else persistRoute();
      return;
    }
    const requestedId = route.get("project");
    const id = state.projects.some((project) => project.id === requestedId)
      ? requestedId : state.projects[0].id;
    const target=productionRoute(route.get("step")||"studio",route.get("section"),route.get("tool")),step=target.step;
    const view=studioLocal(id);
    const changedSection=view.section!==target.section || view.tool!==target.tool;
    view.section=target.section;view.tool=target.tool;
    const requestedShot = route.get("shot");
    if (state.projectOpening?.summary.id === id) {
      state.step = step; state.shotId = requestedShot;
      const loading = changedSection ? switchProjectRead(id) : null;
      if (loading) { await loading; return; }
      renderHeader(); renderStage(); persistRoute();
      return;
    }
    if (!state.creating && !state.loading && state.project?.id === id) {
      const shotId = shots().some((shot) => shot.id === requestedShot) ? requestedShot : shots()[0]?.id || null;
      if (state.step !== step || state.shotId !== shotId || changedSection) {
        state.step = step; state.shotId = shotId;
        const loading = switchProjectRead(id);
        if (loading) { await loading; return; }
        renderHeader(); renderStage();
        announce(`已切换到${STEP_NAMES[step]}`);
      }
      persistRoute();
      return;
    }
    state.step = step; state.shotId = requestedShot;
    await openProject(id, { preserveStep: true });
  } finally {
    if (routeInFlight === incomingHash) routeInFlight = null;
  }
}
function videoReadMode(id) {
  if (typeof studioLocal!=="function" || currentPhase()!=="video" || ["shots","checks"].includes(studioLocal(id).tool)) return "workspace";
  if (state.config?.video_detail===true || document.documentElement?.dataset?.videoDetail==="true") return "video";
  // Older running servers already expose this project's production desk separately.
  const saved=state.projects.find(project=>project.id===id)?.opening?.video_job;
  return saved?.run_id && saved?.plan_id && state.config?.workspace_snapshot ? "studio" : "workspace";
}
function switchProjectRead(id) {
  const desired=videoReadMode(id);
  const opening=state.projectOpening;
  if (opening?.summary.id===id ? opening.mode!==desired :
      state.project?.id===id && state.project.projection==="video" && desired==="workspace") {
    return openProject(id,{preserveStep:true});
  }
  return null;
}
function setStep(step) {
  if(state.storyDraftId && !state.creating && step!=="story"){
    const pid=state.storyDraft?.project_id;
    if(!pid)return;
    const target=productionRoute(step);studioLocal(pid).section=target.section;studioLocal(pid).tool=target.tool;state.step=target.step;
    void openProject(pid,{preserveStep:true});return;
  }
  const target=productionRoute(step,state.projectId?studioLocal(state.projectId).section:null);
  state.step=target.step;
  if(state.projectId){studioLocal(state.projectId).section=target.section;studioLocal(state.projectId).tool=target.tool;}
  const projectRead = state.projectId && switchProjectRead(state.projectId);
  if (projectRead) return projectRead;
  renderHeader(); renderStage(); persistRoute();
  announce(`已切换到${STEP_NAMES[currentPhase()]}`);
}
function selectShot(id) { state.shotId = id; renderStage(); persistRoute(); }
function startNew() {
  state.projectRead?.abort();
  state.projectLoad += 1;
  state.projectOpening = null;
  state.storyDraftId = null; state.storyDraft = null; state.storyDraftError = null;
  state.creating = true; state.step = "story"; state.loading = false;
  renderProjects(); renderHeader(); renderJobs(); renderStage(); persistRoute();
  $("idea-input")?.focus();
}

function renderConfig() {
  const key = $("key-status");
  const routes = state.config?.model_routes;
  const entries = Object.entries(routes || {});
  const ready = entries.filter(([, route]) => route.key_configured).length;
  const allReady = entries.length ? ready === entries.length : state.config?.key_configured === true;
  key.className = `key-status status-pill ${allReady ? "pass" : "unknown"}`;
  key.textContent = allReady ? "模型密钥已配置" : ready ? `${ready}/${entries.length} 项用途已配密钥` : "模型密钥未配置";
  key.title = entries.map(([purpose, route]) => `${purpose} · ${route.model} · ${route.key_configured ? "已配置" : "缺少密钥"}`).join("\n");
  const models = routes ? [["剧本对话","script.decision"],["视频对话","video.decision"],["生图","reference.image"],["视频","video.text_to_video"],["参考视频","video.reference_to_video"]]
    .filter(([,purpose]) => routes[purpose]).map(([label,purpose]) => label + " " + routes[purpose].model)
    : [state.config?.text_model, state.config?.video_model, state.config?.vision_model,
      state.config?.reference_video_model ? "参考镜头 " + state.config.reference_video_model : null].filter(Boolean);
  $("model-caption").textContent = models.length ? `当前模型 · ${models.join(" / ")}\n${toolReady() ? "本地媒体工具已就绪" : "本地媒体工具未就绪"}` : "模型配置暂不可用";
}
function renderProjects() {
  const list = $("project-list"); list.replaceChildren();
  const drafts = state.storyDrafts.filter((draft) => !draft.project_id);
  $("project-count").textContent = String(state.projects.length + drafts.length).padStart(2, "0");
  if (!state.projects.length && !drafts.length) { list.append(node("p", "list-loading quiet", "创建你的第一个故事")); return; }
  state.projects.forEach((project, index) => {
    const active = state.step !== "traces" && !state.creating && state.projectId === project.id;
    const item = node("button", `project-item${active ? " active" : ""}`); item.type = "button";
    if (active) item.setAttribute("aria-current", "true");
    const copy = node("span", "project-item-copy");
    append(copy, node("strong", "", project.title || "未命名故事"), node("small", "", `${project.shot_count || 0} 个镜头 · 本机工程`));
    append(item, node("span", "project-glyph", String(index + 1).padStart(2, "0")), copy);
    item.addEventListener("click", () => {
      if (state.step !== "traces" && !state.creating && !state.storyDraftId && state.projectId === project.id &&
          (state.project?.id === project.id || (state.loading && state.projectOpening?.summary.id === project.id))) return;
      void openProject(project.id);
    }); list.append(item);
  });
  drafts.forEach((draft, index) => {
    const active = state.step !== "traces" && !state.creating && state.storyDraftId === draft.id;
    const item = node("button", `project-item${active ? " active" : ""}`); item.type = "button";
    if (active) item.setAttribute("aria-current", "true");
    const copy = node("span", "project-item-copy");
    append(copy, node("strong", "", draft.title || draft.idea || "待完成故事"), node("small", "", `${draftStatus(draft.status)} · 故事草稿`));
    append(item, node("span", "project-glyph", String(state.projects.length + index + 1).padStart(2, "0")), copy);
    item.addEventListener("click", () => openStoryDraft(draft.id)); list.append(item);
  });
}
function draftStatus(status) { return ({ queued: "排队中", running: "生成中", failed: "生成失败 · 待修复", interrupted: "已中断", succeeded: "待核对" })[status] || "待完成"; }
function applyBootstrap(result) {
  state.projects = result.projects || [];
  state.storyDrafts = result.story_drafts || [];
  state.config = result.config || state.config;
}
function renderHeader() {
  const tracing = state.step === "traces";
  document.body.classList.toggle("trace-mode", tracing);
  $("workflow-tabs").hidden = tracing;
  const phase=state.creating||state.storyDraftId?"story":currentPhase();
  const phaseData=state.projectId&&!state.storyDraftId&&typeof studioLocal==="function"?studioLocal(state.projectId).data:null;
  document.querySelectorAll("[data-step]").forEach(tab=>{
    const active=tab.dataset.step===phase;tab.classList.toggle("active",active);
    if(active)tab.setAttribute("aria-current","step");else tab.removeAttribute("aria-current");
    tab.disabled=tab.dataset.step!=="story" && (state.creating || !!state.storyDraftId&&!state.storyDraft?.project_id || !state.project&&!state.projectOpening&&!state.storyDraft?.project_id);
  });
  document.querySelectorAll("[data-phase-status]").forEach(label=>{
    const key=label.dataset.phaseStatus==="story"?"script":label.dataset.phaseStatus;
    const status=phaseData?.stages?.find(s=>s.id===key)?.status;
    label.textContent=key==="review"?(phaseData?.preview_gate?.ready?"验收通过 · 可预览":"验收通过后解锁"):
      status||({script:"创作与发布",assets:"制作与自检",video:"拍摄、验收与修复"})[key];
  });
  $("open-traces").classList.toggle("active", tracing);
  if (tracing) $("open-traces").setAttribute("aria-current", "page"); else $("open-traces").removeAttribute("aria-current");
  if (tracing) {
    $("breadcrumb-title").textContent = "Trace 调试台";
    $("workspace-eyebrow").textContent = "AGENT OBSERVABILITY";
    $("project-title").textContent = "每一步，都有记录。";
    $("project-subtitle").textContent = "在同一套 Trace 系统里查看请求、任务、工具、模型上下文与校验结果。所有内容均为服务端脱敏后的记录。";
    $("project-meta").replaceChildren();
    document.title = "Trace 调试台 · 片场";
    return;
  }
  if (state.storyDraftId && !state.creating) {
    const draft = state.storyDraft || state.storyDrafts.find((item) => item.id === state.storyDraftId);
    const title = draft?.title || "故事草稿";
    $("breadcrumb-title").textContent = title;
    $("workspace-eyebrow").textContent = "STORY DRAFT / 创作记录";
    $("project-title").textContent = title;
    $("project-subtitle").textContent = "创意与已保存结果会持续保留。制作规格通过检查后，才能进入镜头制作。";
    $("project-meta").replaceChildren(pill(draft?.status, draftStatus(draft?.status)));
    document.title = `${title} · 片场`; return;
  }
  const summary = state.projectOpening?.summary ||
    (state.project?.projection==="video" && state.projects.find(p=>p.id===state.projectId));
  const project = state.creating ? null : (summary || state.project);
  const title = project?.title || (state.creating ? "新建故事" : "我的故事");
  $("breadcrumb-title").textContent = title;
  $("workspace-eyebrow").textContent = project ? "STORY IN PRODUCTION" : "YOUR NEXT STORY";
  $("project-title").textContent = project?.title || "一个故事，从这里开拍。";
  $("project-subtitle").textContent = project ? (summary?.opening?.logline || project.logline || "从剧本到候选，从素材到证据。按顺序完成你的制作。") : "让创意成为剧本，让每一个镜头都有据可查。";
  $("project-meta").replaceChildren();
  if (summary && Number.isInteger(summary.shot_count)) {
    const metric = node("div", "metric");
    append(metric, node("strong", "", summary.shot_count), node("small", "", "分镜")); $("project-meta").append(metric);
  } else if (state.project && project) {
    const total = shots().reduce((sum, shot) => sum + Number(shot.duration_s || 0), 0);
    [[shots().length, "分镜"], [shots().filter(readyCandidate).length, "素材就绪"], [`${total}s`, "目标时长"]].forEach(([value, label]) => {
      const metric = node("div", "metric"); append(metric, node("strong", "", value), node("small", "", label)); $("project-meta").append(metric);
    });
  }
  document.title = `${title} · 片场`;
}
function jobTitle(job) {
  const kind = String(job.kind || "").toLowerCase();
  let title = "制作任务";
  if (kind === "pi_script") title = "剧本 Agent · Pi 会话";
  else if (kind === "pi_video") title = "视频制作 Agent · Pi 会话";
  else if (kind === "studio_video") title = "视频制作 Agent · 制作委托";
  else if (kind === "studio_review") title = "视频制作 Agent · 自动验收";
  else if (kind === "studio_script") title = "剧本 Agent · 创作";
  else if (kind === "asset_inspect") title = "资产 Agent · 核验已有候选";
  else if (kind.includes("asset")) title = kind.includes("regen") ? "资产 Agent · 修改与自检" : "资产 Agent · 制作与自检";
  else if (kind.includes("reference")) title = "生成参考图";
  else if (kind === "agent") title = "Agent 执行";
  else if (/story|create|build|script/.test(kind)) title = "生成剧本与分镜";
  else if (/resume/.test(kind)) title = "恢复视频任务";
  else if (/generate|video/.test(kind)) title = "生成视频";
  else if (/check|inspect|observe|accept/.test(kind)) title = "检查镜头素材";
  else if (/export|render/.test(kind)) title = "导出预览片";
  return job.shot_id ? `${job.shot_id} · ${title}` : title;
}
function agentResultLabel(job) {
  const outcome = job.result?.loop_status || job.result?.status;
  if (outcome === "completed" && job.result?.task_complete === true) return "目标已完成";
  return ({ needs_input: "需要补充信息 · 未完成", budget_exhausted: "次数已用完 · 未完成", cancelled: "已停止 · 未完成", error: "执行错误 · 未完成" })[outcome]
    || (ACTIVE.has(job.status) ? (job.status === "queued" ? "排队中" : "进行中") : "目标未完成");
}
function agentReportLink(job, label = "查看执行报告 JSON ↓") {
  const url = assetUrl(job.result?.report_url);
  if (!url) return null;
  const link = node("a", "button button-quiet button-small", label); link.href = url; link.target = "_blank"; link.rel = "noopener";
  return link;
}
async function cancelAgentJob(job) {
  if (job.kind !== "agent" || !ACTIVE.has(job.status) || job.cancel_requested || state.cancelPending.has(job.id)) return;
  state.cancelPending.add(job.id); renderJobs();
  try {
    const result = await api.cancelAgent(job.id); acceptJob(result.job, "已请求停止 Agent 的后续动作");
    toast("已请求停止后续动作。当前调用返回后生效；已提交的云端生成任务不会被撤销。");
  } catch (error) { toast(error.message, true); }
  finally { state.cancelPending.delete(job.id); renderJobs(); if (state.step === "agent" && state.projectId === jobProjectId(job)) renderStage(); }
}
function currentHumanAssetDecisions(projectId) {
  if (!projectId || typeof studioViews === "undefined") return new Set();
  const items = studioViews.get(projectId)?.data?.asset_work?.items || [];
  return new Set(items.filter(item => item.status === "approved" && item.approval?.selection_mode === "human")
    .map(item => assetDecisionKey(item.selected_pin, item.approval.review_ref)).filter(Boolean));
}
function assetDecisionKey(candidate, review) {
  if (![candidate, review].every(pin => pin?.asset_id && pin.revision_id && pin.sha256)) return null;
  return JSON.stringify([candidate.asset_id, candidate.revision_id, candidate.sha256, review.asset_id, review.revision_id, review.sha256]);
}
let feedbackReading = false, feedbackReadAt = 0;
let feedbackJobs = {};
const shownFeedbackRequests = new Set();
const acceptedFeedbackRequests = new Set();
const pendingFeedbackAnswers = new Set();
const feedbackNavigation = new Map([
  ["confirm_video_plan", "查看拍摄计划"],
  ["confirm_commission_budget", "查看后续计划"],
  ["review_retake_limit", "审看镜头"],
  ["review_commission_shots", "审看镜头与报告"],
  ["inspect_commission_reports", "查看镜头与阻塞原因"],
  ["review_asset_candidates", "查看待审参考图"],
]);

function requestedAssetCandidates(prompt, data) {
  if (!Array.isArray(prompt.items)) return [];
  return (data?.asset_work?.items || []).filter(item => {
    const review = item.review, pin = item.candidate;
    return item.job_id && item.status === "needs_attention" && !item.selected_pin && pin?.revision_id &&
      review?.review_ref?.sha256 && !review.observation_error && ["FAIL","UNKNOWN"].includes(review.verdict || review.status) &&
      prompt.items.some(target => target && (target.candidate_job_id ?? prompt.candidate_job_id) === item.job_id &&
        target.subject_id === item.subject_id && target.purpose === item.purpose && target.revision_id === pin.revision_id &&
        (target.asset_id === undefined || target.asset_id === pin.asset_id) &&
        (target.sha256 === undefined || target.sha256 === pin.sha256) &&
        (target.review_ref === undefined || (target.review_ref?.sha256 &&
          Object.keys(target.review_ref).every(key => target.review_ref[key] === review.review_ref[key]))));
  });
}

function activeCommissionReview(projectId, commissionId) {
  return commissionId && state.jobs.find(job => job.kind === "studio_review" && job.project_id === projectId &&
    job.review_request_id === commissionId && ACTIVE.has(job.status));
}

function reviewProgress(review) {
  return review.status === "queued" ? "本次委托的自动复验已排队。" : "正在自动复验本次委托的镜头与验收报告。";
}

function feedbackScopeMatches(prompt, data) {
  if (!feedbackNavigation.has(prompt.kind)) return true;
  if (data?.project_id && data.project_id !== prompt.project_id) return false;
  if (activeCommissionReview(prompt.project_id, prompt.commission_id)) return false;
  if (prompt.kind === "review_asset_candidates") {
    if (!Array.isArray(prompt.items) || !prompt.items.some(target =>
      target?.revision_id && target.subject_id && target.purpose && (target.candidate_job_id ?? prompt.candidate_job_id))) return false;
    // The video projection has no assets. Navigation loads and rechecks the full workspace.
    return data?.projection === "video" || requestedAssetCandidates(prompt, data).length > 0;
  }
  const spec = data?.assets?.spec || data?.catalog?.spec;
  if (prompt.spec_revision && spec?.revision_id !== prompt.spec_revision) return false;
  if (prompt.commission_id && prompt.commission_id !==
      (data?.video_execution?.commission_id || data?.incomplete_production?.request_id)) return false;
  if (prompt.proposal_id && data?.projection !== "video" && data?.video_plan?.extra_retake?.proposal_id !== prompt.proposal_id) return false;
  if (prompt.exhausted_budget) {
    const expected = prompt.exhausted_budget, actual = data?.video_execution?.exhausted_budget;
    if (!actual || expected.kind !== actual.kind || expected.limit !== actual.limit || expected.used !== actual.used) return false;
  }
  if (!prompt.plan_id) return true;
  const currentPlan = data?.video_plan?.plan_id;
  return currentPlan === prompt.plan_id || (data?.projection === "video" && !currentPlan);
}

function scriptFeedbackRole(draftId, pid) {
  const chat = typeof agentChatCurrent !== "undefined" && agentChatCurrent;
  if (chat?.host?.isConnected !== false && chat?.view?.role &&
      (draftId ? chat.scope?.story === draftId : chat.scope?.project === pid)) return chat.view.role === "script";
  return currentPhase() === "story";
}

function currentFeedbackRequests() {
  const draftId = state.storyDraftId, pid = draftId ? null : state.projectId;
  if ((!pid && !draftId) || state.loading || state.creating || state.step === "traces") return [];
  const requests = [], data = typeof studioViews !== "undefined" && pid && studioViews.get(pid)?.data;
  for (const job of visibleJobs()) {
    const info = feedbackJobs[job.id], prompt = info?.human_request;
    if (info?.status !== "needs_input" || !prompt?.id || !prompt.title ||
        (prompt.job_id && prompt.job_id !== job.id)) continue;
    if (prompt.kind === "script_creative_input") {
      if (!["studio_script","pi_script"].includes(job.kind) || (prompt.role && prompt.role !== "script") ||
          !scriptFeedbackRole(draftId,pid)) continue;
    }
    const sourceStory = prompt.story_job_id || prompt.story_id;
    if (draftId) {
      if (prompt.kind !== "script_creative_input" || !/^[a-f0-9]{32}$/.test(draftId) || sourceStory !== draftId ||
          (prompt.project_id && prompt.project_id !== (jobProjectId(job) || state.storyDraft?.project_id))) continue;
    } else if (prompt.project_id !== pid || !feedbackScopeMatches(prompt,data)) continue;
    const scope = prompt.project_id ? "/api/projects/" + encodeURIComponent(prompt.project_id) :
      "/api/story-drafts/" + encodeURIComponent(draftId);
    const key = JSON.stringify([scope, job.id, prompt.id]);
    if (!acceptedFeedbackRequests.has(key)) requests.push({pid, draftId, scope, jobId:job.id, key, prompt});
  }
  return requests;
}

async function openFeedbackDestination(entry) {
  const {pid, prompt} = entry, kind = prompt.kind;
  if (state.projectId !== pid || state.storyDraftId || state.creating) return;
  if (kind === "review_asset_candidates") {
    await setStep("assets");
    if (state.projectId !== pid || currentPhase() !== "assets") return;
    const current = requestedAssetCandidates(prompt, studioLocal(pid).data);
    const row = [...document.querySelectorAll(".studio-asset-problem")].find(({dataset}) =>
      current.some(item => item.job_id === dataset.assetJobId && item.subject_id === dataset.subjectId &&
        item.purpose === dataset.purpose && item.candidate.revision_id === dataset.revisionId &&
        item.candidate.asset_id === (dataset.assetId || undefined) &&
        item.candidate.sha256 === (dataset.candidateSha || undefined) && item.review.review_ref.sha256 === dataset.reviewSha));
    if (row) {
      const evidence = row.querySelector?.("details");
      if (evidence) evidence.open = true;
      row.focus({preventScroll:true});
      row.scrollIntoView?.({block:"start"});
    }
    return;
  }
  await setStep("video");
  if (state.projectId !== pid || currentPhase() !== "video") return;
  const view = studioLocal(pid);
  const planAction = ["confirm_video_plan","confirm_commission_budget"].includes(kind);
  if (planAction && view.data?.projection === "video" && view.data.video_execution?.commission_id)
    await openProject(pid,{preserveStep:true, full:true});
  if (state.projectId !== pid || currentPhase() !== "video" || !feedbackScopeMatches(prompt, view.data)) return;
  if (["review_retake_limit","review_commission_shots","inspect_commission_reports"].includes(kind)) {
    const commission = view.data?.video_execution?.commission_id;
    if (commission !== prompt.commission_id) return;
    await studioReadShotReviews(pid, commission);
    if (state.projectId !== pid || currentPhase() !== "video" || view.data?.video_execution?.commission_id !== commission) return;
    const scope = [...document.querySelectorAll(".studio-shot-approvals")].find(el => el.dataset.requestId === commission);
    if (!scope) return;
    const row = [...scope.querySelectorAll("article")].find(el => prompt.review_shots?.some(pin =>
      el.dataset.shotId === pin.shot_id && el.dataset.mediaSha === (pin.media_sha256 || "") && el.dataset.reportSha === (pin.report_sha256 || "")));
    if (!row && prompt.review_shots?.length) {
      scope.prepend?.(note("原提示中的镜头或报告版本已更新，请查看此委托当前的阻塞原因。"));
    }
    const target = row || scope;
    const report = row?.querySelector?.('.studio-take[data-selected="true"] .studio-take-report');
    if (report) report.open = true;
    target.focus?.({preventScroll:true}); target.scrollIntoView?.({block:"start"});
    return;
  }
  if (state.projectId === pid) document.querySelector?.(planAction ?
    ".studio-video-plan" : ".studio-shot-approvals")?.scrollIntoView?.({block:"start"});
}

function feedbackInput(field) {
  const label = node("label","feedback-field");
  const input = node(field.type === "select" ? "select" : "input");
  input.name = field.name;
  input.required = field.required !== false;
  if (field.type === "confirm") {
    input.type = "checkbox";
    input.checked = false;
    input.autocomplete = "off";
    label.className += " feedback-field-confirm";
    label.append(input, node("span","",field.label));
  } else {
    if (field.type === "select") {
      const placeholder = node("option","","请选择");
      placeholder.value = "";
      input.append(placeholder);
      for (const option of field.options || []) {
        const row = node("option","",option.label);
        row.value = option.value;
        input.append(row);
      }
    } else {
      input.type = "text";
      input.autocomplete = "off";
      if (field.name === "endpoint_id") {
        input.pattern = "ep-[A-Za-z0-9][A-Za-z0-9\\-]{1,123}";
        input.title = "请填写 ep- 开头的方舟接入点 ID";
      }
      if (field.placeholder) input.placeholder = field.placeholder;
      if (Number.isInteger(field.max_length) && field.max_length > 0) input.maxLength = field.max_length;
    }
    label.append(node("span","",field.label), input);
  }
  if (field.hint) label.append(node("small","small-print",field.hint));
  return {label, input, type:field.type};
}

function openFeedbackRequest(entry) {
  const {pid, jobId, key, prompt} = entry;
  if (!currentFeedbackRequests().some(row => row.key === key)) return;
  const dialog = $("asset-intervention-dialog");
  const fields = Array.isArray(prompt.fields) ? prompt.fields : [];
  const batchRevision = prompt.kind === "review_asset_candidates" && fields.length > 0;
  if (dialog.dataset.request === key && (dialog.open || !batchRevision || pendingFeedbackAnswers.has(key))) {
    if (!dialog.open) dialog.showModal();
    return;
  }
  const heading = node("h2","",prompt.title);
  heading.id = "asset-intervention-title";
  dialog.dataset.request = key;
  const actions = node("div","studio-actions");
  const later = button("稍后处理", () => dialog.close(), {quiet:true});
  const children = [heading, node("p","feedback-message",prompt.message)];
  if (feedbackNavigation.has(prompt.kind)) {
    const navigationLabel = prompt.navigation_label || (!batchRevision && prompt.submit_label) || feedbackNavigation.get(prompt.kind);
    actions.append(button(navigationLabel, () => {
      dialog.close();
      if (currentFeedbackRequests().some(row => row.key === key)) void openFeedbackDestination(entry);
    }, {primary:!batchRevision}));
  }
  if (feedbackNavigation.has(prompt.kind) && !batchRevision) {
    actions.append(later);
    children.push(actions);
  } else {
    const form = node("form","feedback-form");
    const supported = fields.length > 0 && fields.every(f => f?.name && f.label && ["confirm","select","text"].includes(f.type)) &&
      (!batchRevision || fields.some(f => f.name === "authorize_revision" && f.type === "confirm"));
    const controls = supported ? fields.map(feedbackInput) : [];
    const authorization = controls.find(row => row.input.name === "authorize_revision")?.input;
    const error = node("p","feedback-error");
    error.setAttribute("role","alert");
    const submit = node("button","button button-primary",prompt.submit_label || "提交并继续");
    submit.type = "submit";
    const updateSubmit = () => {submit.disabled = !supported || pendingFeedbackAnswers.has(key) || (batchRevision && !authorization?.checked);};
    updateSubmit();
    if (batchRevision && authorization) authorization.addEventListener("change", updateSubmit);
    if (!supported) error.textContent = "此请求需要更新工作台页面，请刷新后再试。";
    actions.append(submit, later);
    form.append(...controls.map(row => row.label), error, actions);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (!supported || pendingFeedbackAnswers.has(key) || (batchRevision && !authorization?.checked) ||
          !currentFeedbackRequests().some(row => row.key === key)) return;
      const answers = Object.fromEntries(controls.map(({input,type}) =>
        [input.name, type === "confirm" ? input.checked : input.value.trim()]));
      if (!Object.values(answers).some(Boolean)) {
        error.textContent = "请填写一项答复或勾选确认后继续。";
        return;
      }
      pendingFeedbackAnswers.add(key);
      submit.disabled = true;
      error.textContent = "";
      try {
        await request(entry.scope + "/feedback-input",
          {method:"POST",body:{job_id:jobId,request_id:prompt.id,answers}});
        acceptedFeedbackRequests.add(key);
        if (dialog.dataset.request === key) dialog.close();
        if (entry.draftId ? state.storyDraftId === entry.draftId : state.projectId === pid && !state.storyDraftId)
          toast("旁路已收到答复，正在继续处理。");
      } catch (failure) {
        error.textContent = failure.message;
      } finally {
        pendingFeedbackAnswers.delete(key);
        updateSubmit();
        feedbackReadAt = 0;
        renderFeedbackRequests();
        void refreshFeedbackJobs();
      }
    });
    children.push(form);
  }
  dialog.replaceChildren(...children);
  if (!dialog.open) dialog.showModal();
  shownFeedbackRequests.add(key);
  try { window.sessionStorage.setItem("pianchang.feedback." + key, "1"); } catch {}
}

function renderFeedbackRequests() {
  const card = $("asset-intervention"), dialog = $("asset-intervention-dialog");
  const requests = currentFeedbackRequests();
  if (dialog.dataset.request && !requests.some(row => row.key === dialog.dataset.request)) {
    if (dialog.open) dialog.close();
    delete dialog.dataset.request;
    dialog.replaceChildren();
  }
  const signature = JSON.stringify(requests.map(row => row.key));
  if (card.dataset.signature === signature) return;
  card.dataset.signature = signature;
  card.hidden = !requests.length;
  card.replaceChildren();
  for (const row of requests) {
    const action = button("回应请求", () => openFeedbackRequest(row), {small:true});
    card.append(node("h3","",row.prompt.title), node("p","feedback-message",row.prompt.message), action);
  }
  if (dialog.open || window.pianchangFeedbackObserver) return;
  const unseen = requests.find(({key}) => {
    if (shownFeedbackRequests.has(key)) return false;
    try { return window.sessionStorage.getItem("pianchang.feedback." + key) !== "1"; } catch { return true; }
  });
  if (unseen) openFeedbackRequest(unseen);
}

function feedbackText(info, job) {
  const review = job && ["pi_video","studio_video"].includes(job.kind) &&
    activeCommissionReview(job.project_id, job.commission_id || job.message_id);
  if (review) return reviewProgress(review);
  if (!info) return "暂时无法读取旁路状态";
  const labels = {queued:"异常已入队",handling:"正在跟踪修复",succeeded:"已复验通过",
    blocked:"推进受阻",unknown:"结果待核对",awaiting_evidence:"等待依赖就绪",needs_input:"需要你处理",
    historical:"历史记录",untracked:"尚未接管",watching:"监听中",cleared:"依赖复验通过",resolved:"原任务状态已更新"};
  const parts = [info.running ? "旁路在线" : "旁路未连接", labels[info.status] || "已记录"];
  if (info.progress_message) return [...parts, info.progress_message].join(" · ");
  if (info.reason === "acceptance_retake_limit") parts.push("自动重拍已停止，等待人工审看镜头");
  if (info.triage?.status === "reported") parts.push("已收到 Codex 的故障分析");
  if (info.triage?.status === "running") parts.push("Codex 正在分析原始故障记录");
  if (info.triage?.status === "unavailable") parts.push("本地 Codex 未返回诊断回执");
  const dependency=info.dependency;
  if (dependency?.human_intervention?.kind === "reference_image_model_access") parts.push("上次生图请求被供应商拒绝");
  if (Number.isInteger(dependency?.current_shot_count)) parts.push("当前规格 "+dependency.current_shot_count+" 镜头");
  if (Number.isInteger(dependency?.missing_count)) {
    parts.push(dependency.missing_count ? "当前仍缺 "+dependency.missing_count+" 项镜头参考" : "参考门禁已通过");
  }
  for (const ref of dependency?.missing_references || []) {
    parts.push([ref.shot_id, ref.subject_id, studioPurposes[ref.purpose]].filter(Boolean).join(" / "));
  }
  const failure = dependency?.transport_failure;
  if (failure?.request_kind === "model" && failure.dispatch === "not_sent") {
    parts.push("已核实原规划请求在连接阶段中断，尚未发送");
    if (dependency.connectivity?.status === "reachable") {
      parts.push("规划模型线路已复通");
      if (info.status === "handling") parts.push("正在按原制作委托续做");
    } else if (dependency.connectivity?.status === "retry_later") {
      parts.push("正在等待规划线路恢复，旁路会自动复查");
    }
  }
  if (info.reason === "provider_receipt_unknown" && info.running) {
    parts.push("旁路会继续核对原请求的发送与受理记录");
  }
  if (failure?.request_kind === "audio") {
    if (failure.dispatch === "not_sent") parts.push("原试音在连接阶段失败，已核实未发送");
    else if (failure.error_type) parts.push("原试音传输异常 " + failure.error_type);
    if (dependency.connectivity?.status === "http_response") parts.push("当前语音线路可达");
  }
  if (info.voice_recovery_job_id) parts.push("已按原选角提交试音恢复");
  if (dependency?.voice_review) {
    parts.push("音频已送达，原听审无法判断");
    if (info.voice_inspection_job_id) parts.push("已提交同一候选复听");
  }
  if (dependency?.receipt_unknown) parts.push("原供应商请求回执待核对");
  if (dependency?.requires_plan_reconfirmation) parts.push("新版制作计划待确认");
  return parts.join(" · ");
}
function isFeedbackJob(job) {
  const partial = ["studio_video","pi_video"].includes(job.kind) && job.status === "succeeded" && job.result?.action_required === true &&
    (job.result.task_complete === false || job.result.extra_retake?.partial);
  return (["failed", "interrupted"].includes(job.status) || partial) && /^[0-9a-f]{32}$/.test(job.id) &&
    ["pi_script","studio_script","pi_video","studio_video","asset_production","asset_regenerate",
      "asset_inspect","asset_approve","studio_reference"].includes(job.kind);
}
async function refreshFeedbackJobs() {
  if (feedbackReading || Date.now() - feedbackReadAt < 5000) return;
  const ids = visibleJobs().filter(isFeedbackJob).map(job => job.id);
  if (!ids.length) return;
  feedbackReading = true;
  feedbackReadAt = Date.now();
  try {
    // An active request can belong to an older task outside the twelve visible task cards.
    for (let offset = 0; offset < ids.length; offset += 24) {
      const group = ids.slice(offset, offset + 24);
      const result = await request("/api/feedback-watch?" + new URLSearchParams(group.map(id => ["job",id])));
      for (const id of group) feedbackJobs[id] = result.jobs?.[id] || null;
    }
  } catch {
    // Retain the last confirmed state and any answer being typed through a polling outage.
    for (const id of ids) if (!Object.hasOwn(feedbackJobs,id)) feedbackJobs[id] = null;
  } finally { feedbackReading = false; }
  for (const el of document.querySelectorAll("[data-feedback-job]")) {
    if (ids.includes(el.dataset.feedbackJob)) el.textContent = feedbackText(feedbackJobs[el.dataset.feedbackJob],
      state.jobs.find(job => job.id === el.dataset.feedbackJob));
  }
  renderJobs();
}

function renderJobs() {
  renderFeedbackRequests();
  const openingJob = state.projectOpening?.summary.opening?.video_job;
  const onlyOpeningJob = state.jobsReady===false && openingJob?.project_id===state.projectId && openingJob?.kind==="studio_video";
  if(state.jobsReady===false && !onlyOpeningJob){
    $("active-job-count").textContent="…";
    const list = $("job-list"); delete list.dataset.signature;
    list.replaceChildren(node("p","small-print","正在读取当前故事的制作动态…"));
    return;
  }
  const scoped = onlyOpeningJob ? [openingJob] : visibleJobs();
  const draftScope = !state.creating && !!state.storyDraftId;
  const projectScope = !state.creating && !draftScope && !!state.projectId;
  const projectTitle = state.projects.find((project) => project.id === state.projectId)?.title
    || (state.project?.id === state.projectId ? state.project.title : null) || "当前故事";
  $("activity-intro").textContent = draftScope
    ? `《${state.storyDraft?.title || state.storyDrafts.find((item) => item.id === state.storyDraftId)?.title || "故事草稿"}》的创建动态，仅显示这次故事任务。`
    : projectScope
    ? `《${projectTitle}》的制作动态，仅显示这个故事的任务。`
    : "新故事的制作动态会从本次创作开始记录。";
  const jobs = [...scoped].sort((a, b) => Number(ACTIVE.has(b.status))-Number(ACTIVE.has(a.status)) ||
    String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""))).slice(0, 12);
  $("active-job-count").textContent = onlyOpeningJob ? "…" : String(scoped.filter((job) => ACTIVE.has(job.status)).length);
  const list = $("job-list");
  const humanDecisions = currentHumanAssetDecisions(projectScope && !draftScope ? state.projectId : null);
  const projectId = projectScope && !draftScope ? state.projectId : null;
  const execution = projectId && typeof studioViews !== "undefined" ? studioViews.get(projectId)?.data?.video_execution : null;
  const proposal = projectId && typeof studioViews !== "undefined" ? studioViews.get(projectId)?.data?.video_plan?.extra_retake : null;
  const monitor = projectId && typeof studioViews !== "undefined" ? studioViews.get(projectId)?.monitor : null;
  const monitoredJob = monitor && jobs.find(job => job.id===monitor.jobId && job.kind==="studio_video");
  const sample = monitoredJob && monitor.sample();
  let progressText = "";
  if (sample && monitoredJob.status===sample.status && monitoredJob.status===monitor.jobStatus &&
      monitoredJob.updated_at===monitor.jobUpdated && !(Date.parse(monitoredJob.updated_at)>Date.parse(sample.updatedAt))) {
    const stale = monitor.element.dataset.readState!=="ready" || (ACTIVE.has(monitoredJob.status) && Date.now()-sample.readAt>=15000);
    const stamp = new Date(sample.readAt).toLocaleString("zh-CN",{hour12:false});
    progressText = sample.recorded ? "上次制作记录（"+stamp+"）：" + sample.text
      : stale ? "上次读取（" + stamp + "）：" + sample.text : sample.text;
  }
  const usage = execution?.usage, limits = execution?.limits;
  const signature = JSON.stringify([draftScope ? `draft:${state.storyDraftId}` : projectScope ? state.projectId : "new", [...humanDecisions], proposal?.proposal_id, monitoredJob?.id, progressText,
    execution?.commission_id, execution?.last_execution?.id, usage?.reserved_video_generations, usage?.model_calls, limits?.max_video_generations, limits?.max_model_calls,
    jobs.map((job) => [job.id, job.status, job.updated_at, job.message, job.error, job.result, job.asset_diagnostics, job.trace_id,
      feedbackJobs[job.id]?.review_link?.id, job.cancel_requested, state.cancelPending.has(job.id)])]);
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature; list.replaceChildren();
  if (proposal?.proposal_id && proposal.shot_ids?.length && !activeCommissionReview(projectId,proposal.partial?.request_id)) {
    const card = node("article","job-card");
    append(card, node("h3","","有镜头等待你同意重拍"),node("p","",proposal.shot_ids.join("、") + " · " + proposal.reason),
      button("查看额外重拍计划", () => {
        if (state.projectId !== projectId) return;
        studioLocal(projectId).section = "video"; setStep("studio"); void studioRefresh(projectId);
      }, {quiet:true,small:true}));
    list.append(card);
  }
  if (!jobs.length) {
    if (proposal?.proposal_id) return;
    const empty = node("div", "quiet-state");
    const storyScope = draftScope || projectScope;
    append(empty, node("span", "quiet-state-symbol", "◷"), node("p", "", storyScope ? "这个故事还没有制作任务" : "新故事还没有制作任务"), node("small", "", storyScope ? "在这个故事中提交制作任务后，记录会显示在这里。" : "提交创意后，只显示这次故事的任务。")); list.append(empty); return;
  }
  jobs.forEach((job) => {
    const card = node("article", `job-card${job.status === "failed" ? " error" : ""}`);
    card.dataset.jobId=job.id;
    const heading = node("div", "job-card-top");
    let statusLabel = job.kind === "agent" ? agentResultLabel(job) : { succeeded: "任务完成", failed: "任务失败", interrupted: "已中断", queued: "排队中", running: "进行中" }[job.status];
    const review = ["pi_video","studio_video"].includes(job.kind) && activeCommissionReview(job.project_id,job.commission_id || job.message_id);
    if(job.kind?.startsWith("pi_")&&job.result?.turn_complete&&!job.result?.task_complete)statusLabel="本轮已回复";
    if (job.kind === "pi_video" && job.result?.action_required && job.result?.task_complete === false) statusLabel="制作未完成";
    if (job.kind === "studio_video" && job.result?.action_required) {
      statusLabel = job.result.extra_retake?.proposal_id ?
        (job.result.extra_retake.proposal_id === proposal?.proposal_id ? "等你决定重拍" : "已提出重拍申请") : "制作未完成";
    }
    if (review) statusLabel = review.status === "queued" ? "自动复验已排队" : "正在自动复验";
    append(heading, node("h3", "", jobTitle(job)), pill(review ? review.status : job.status, statusLabel));
    const message = ACTIVE.has(job.status) ? job.message || "任务正在后台处理，请稍候。" : job.error ? errorText(job.error) : job.message || "任务记录已保存。";
    const pastAssetReview = !ACTIVE.has(job.status) && ["asset_production","asset_regenerate","asset_inspect","studio_reference"].includes(job.kind)
      && message.includes("待人工确认");
    const pastVideoBudget = job.kind === "studio_video" && job.status === "failed" && !job.plan_id
      && message.includes("超过授权上限");
    const issues = job.asset_diagnostics?.project_id === jobProjectId(job) ? job.asset_diagnostics?.items : null;
    const assetIssues = job.status === "failed" && Array.isArray(issues) && issues.length > 0;
    const displayMessage = pastVideoBudget ? "旧版委托因手填上限停止。可在当前拍摄计划中开始制作。"
      : pastAssetReview ? "旧流程记录（当时）：" + message
      : assetIssues ? "任务结束时：" + (job.asset_diagnostics.summary || message) : message;
    const showProgress = monitoredJob?.id===job.id && progressText;
    const reported = job.kind==="studio_video" && ACTIVE.has(job.status) && job.message ? "任务上报：" + displayMessage : displayMessage;
    const continuation = job.kind === "studio_video" && job.message_id && scoped.filter(child =>
      child.kind === "pi_video" && child.commission_job_id === job.id && child.commission_id === job.message_id &&
      jobProjectId(child) === jobProjectId(job)).sort((a,b) =>
        String(b.created_at || b.updated_at || "").localeCompare(String(a.created_at || a.updated_at || "")))[0];
    append(card, heading, node("p", "", (continuation || review) && !showProgress ? "上次执行记录：" + reported : showProgress || reported));
    if (showProgress && (job.error || job.result?.action_required)) card.append(node("p","", (continuation ? "上次执行记录：" : "") + displayMessage));
    if (continuation) {
      const phase = ACTIVE.has(continuation.status) ? "进行中" : continuation.result?.task_complete ? "已返回交付回执" :
        continuation.result?.action_required ? "等待处理" : "已停止";
      card.append(node("p","","同一委托的最新续做：" + phase + (continuation.message ? " · " + continuation.message : "")));
      if (execution?.commission_id === job.message_id && execution.last_execution?.id === continuation.id && usage && limits) {
        const counts = [];
        if (Number.isInteger(usage.reserved_video_generations) && Number.isInteger(limits.max_video_generations))
          counts.push("已预留视频生成 " + usage.reserved_video_generations + " / " + limits.max_video_generations + " 次");
        if (Number.isInteger(usage.model_calls) && Number.isInteger(limits.max_model_calls))
          counts.push("模型调用 " + usage.model_calls + " / " + limits.max_model_calls + " 次");
        if (counts.length) card.append(node("p","small-print",counts.join(" · ")));
      }
      card.append(button("查看续做进度与视频 Agent 记录", () => {
        if (state.projectId === jobProjectId(job) && !state.storyDraftId && !state.creating) return setStep("agent");
      }, {quiet:true,small:true,className:"job-result-button"}));
    }
    if (isFeedbackJob(job)) {
      const feedback=node("p","small-print",Object.hasOwn(feedbackJobs,job.id) ?
        feedbackText(feedbackJobs[job.id],job) : review ? reviewProgress(review) : "正在读取自动旁路状态…");
      feedback.dataset.feedbackJob=job.id;
      card.append(feedback);
      const link = feedbackJobs[job.id]?.review_link;
      if (!review && link?.kind === "inspect_commission_reports" && link.job_id === job.id && link.project_id === projectId &&
          link.commission_id === job.message_id) {
        card.append(button("查看镜头与阻塞原因", () => {
          if (feedbackJobs[job.id]?.review_link?.id === link.id)
            return openFeedbackDestination({pid:projectId,prompt:link});
        }, {quiet:true,small:true}));
      }
    }
    if (pastAssetReview) card.append(node("small","small-print","当前选用状态见「共享资产」。"));
    if (assetIssues) {
      const list = node("ul","asset-job-problems");
      list.setAttribute("aria-label","本次任务的问题");
      for (const issue of issues) {
        const row = node("li");
        const label = assetIssueName(issue), url = assetIssueTraceURL(issue);
        if (url) {
          const link = node("a","",label); link.href = url;
          row.append(link);
        } else row.append(node("strong","",label));
        row.append(node("div","",issue.reason || "此任务没有记录具体原因。"));
        if (humanDecisions.has(assetDecisionKey(issue.candidate, issue.review_ref))) {
          row.append(node("strong", "", `当前：${issue.candidate.revision_id} 已人工通过`));
        }
        list.append(row);
      }
      card.append(list, button("查看当前资产", () => openJobResult(job), {quiet:true,small:true,className:"job-result-button"}));
    }
    if (ACTIVE.has(job.status)) card.append(node("div", "job-progress"));
    if (job.kind === "agent" && ACTIVE.has(job.status)) {
      card.append(button(job.cancel_requested ? "已请求停止，等待当前调用返回" : state.cancelPending.has(job.id) ? "正在请求停止…" : "停止后续动作", () => cancelAgentJob(job),
        { quiet: true, small: true, disabled: job.cancel_requested === true || state.cancelPending.has(job.id), className: "job-result-button" }));
      if (job.cancel_requested) card.append(node("p", "", "当前调用结束后停止；已提交的云端任务不会被撤销。"));
    }
    card.append(node("time", "job-time", readableTime(job.updated_at || job.created_at)));
    if (job.result?.acceptance_status) {
      const resultLine = node("div", "job-result-line");
      const status = job.result.acceptance_status;
      resultLine.append(pill(status, `验收：${status === "PASS" ? "通过" : status === "FAIL" ? "失败" : "未知 / 待补证据"}`));
      card.append(resultLine);
    }
    if (job.kind === "agent" && job.result) {
      const detail = node("details", "agent-job-summary"); detail.append(node("summary", "", "执行结果与停止原因"));
      detail.append(node("p", "", typeof job.result.summary === "string" ? job.result.summary : "执行记录已保存，可查看 Trace 与报告。"));
      const reportLink = agentReportLink(job); if (reportLink) detail.append(reportLink);
      card.append(detail);
    }
    if ((job.status === "succeeded" || ["agent","studio_video","studio_review"].includes(job.kind)) && jobProjectId(job)) {
      const label = job.kind === "agent" ? "回到视频 Agent 对话  →" : job.kind === "studio_video" ? "查看镜头进度与片段  →" : "查看结果  →";
      card.append(button(label, () => openJobResult(job), { quiet:true, small:true, className:"job-result-button" }));
    }
    if (validTraceId(job.trace_id)) card.append(button("查看 Trace  ↗", () => openTraceDesk(job.trace_id), { quiet: true, small: true, className: "job-result-button" }));
    else card.append(node("small", "legacy-trace-note", "历史任务：未采集 Trace"));
    list.append(card);
  });
}
function formField(id, label, input, hint) {
  const field = node("div", "form-field");
  const labelNode = node("label", "", label); labelNode.htmlFor = id; input.id = id;
  append(field, labelNode, input);
  if (hint) { const help = node("p", "field-hint", hint); help.id = `${id}-hint`; input.setAttribute("aria-describedby", help.id); field.append(help); }
  return field;
}
function renderCreate() {
  const form = node("form", "panel form-section"); form.id = "create-form";
  append(form, node("span", "section-label", "剧本 Agent"), node("h2", "", "你想讲一个怎样的故事？"), node("p", "form-intro", "输入一句话创意，剧本 Agent 会按剧情决定分几集、每集写几场，以及每场需要哪些节拍。"));
  const idea = node("textarea"); idea.value = state.form.idea; idea.name = "idea"; idea.required = true; idea.maxLength = 6000; idea.rows = 7;
  idea.placeholder = "例如：深夜便利店里，店员认出了十年前失联的朋友。";
  idea.addEventListener("input", () => { state.form.idea = idea.value; });
  form.append(formField("idea-input", "故事创意", idea, "最多 6000 字符。也可以写下你对篇幅、节奏或结局的要求。"));
  const style=node("textarea"); style.value=state.assetStyle; style.rows=2; style.maxLength=2000;
  style.placeholder="例如：整体低饱和，写实质感，暖色灯光只用于表现重逢。";
  style.addEventListener("input",()=>{state.assetStyle=style.value;});
  form.append(formField("script-asset-style","全剧风格、配色与创作意图（可选）",style));
  form.append(node("p", "small-print", "分镜编译的每个阶段最多修复 2 次，模型调用结果不确定时保留 Trace。剧本和分镜就绪后，Agent 自动制作图片和声音，按剧本与风格自检，通过后将该版本用于相关镜头。在共享资产中可填写修改要求并重新生成。生成与核验会调用各用途配置的模型。"));
  if (!keyReady("script.decision")) form.append(note("左侧的《最后一把钥匙》可以直接查看。要生成新剧本，请在 .env 填写剧本对话模型所用连接的 API Key，然后重启工作台。", true));
  const busy = state.pending.has("create") || state.jobs.some((job) => ACTIVE.has(job.status) && /story|create|build|script/.test(job.kind));
  const submit = button(busy ? "剧本制作中…" : "创作剧本并制作资产  →", () => {}, { primary: true, disabled: busy || !keyReady("script.decision") }); submit.type = "submit";
  const footer = node("div", "form-bottom"); append(footer, billing("调用已配置的模型，可能计费。创意与制作结果保存在本机。"), submit); form.append(footer);
  async function submitScript(body) {
    if (busy || state.pending.has("create") || !keyReady("script.decision")) return;
    const submittingView = state.projectLoad;
    state.pending.add("create"); submit.disabled = true; submit.textContent = "正在提交…";
    try {
      const direction=state.assetStyle.trim();
      const result = await api.create(direction ? {...body,style:direction} : body);
      if (state.assetStyle.trim() === direction) state.assetStyle="";
      acceptJob(result.job); toast("故事已进入后台创作，分镜就绪后将自动打开资产页。");
      if (result.job.status === "succeeded" && result.job.result?.project_id) {
        applyBootstrap(await api.bootstrap());
        renderProjects();
        if (state.creating && submittingView === state.projectLoad) await openProducedStory(result.job.result.project_id);
      } else {
        const input = result.job.story_input || { idea: body.idea || body.script?.idea || body.script?.title || "导入剧本" };
        const title = body.script?.title || input.idea || "导入剧本";
        const draft = { ...result.job, idea: input.idea, title: title.slice(0, 60), inputs: input, project_id: null };
        state.storyDrafts = [draft, ...state.storyDrafts.filter((item) => item.id !== draft.id)];
        renderProjects();
        if (state.creating && submittingView === state.projectLoad) await openStoryDraft(result.job.id);
      }
    } catch (error) { toast(error.message, true); }
    finally { state.pending.delete("create"); renderStage(); }
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); const text = idea.value.trim(); if (!text) { idea.focus(); return; }
    await submitScript({ idea:text });
  });
  if (typeof studioImportForm === "function") form.append(studioImportForm(submitScript, busy || !keyReady("script.decision")));
  return form;
}
function repairableStoryDraft(draft) {
  return ["failed", "interrupted"].includes(draft?.status)
    && (!draft.job?.kind || draft.job.kind === "story")
    && /^[a-f0-9]{32}$/.test(String(draft?.id || ""))
    && draft?.draft !== null && typeof draft?.draft === "object" && !Array.isArray(draft.draft);
}
async function repairStoryDraft(draft) {
  if (!repairableStoryDraft(draft) || !keyReady("story.compile") || state.pending.has("create")
      || state.jobs.some((job) => ACTIVE.has(job.status) && isStoryJob(job))) return;
  const submittingView = state.projectLoad;
  const stillViewingSource = () => state.storyDraftId === draft.id && !state.creating
    && state.step === "story" && submittingView === state.projectLoad;
  state.pending.add("create"); renderStage();
  try {
    const result = await api.create({ repair_from: draft.id });
    acceptJob(result.job); toast("已将原草稿交给自动修复；原始失败记录保留，修复过程可查看 Trace。");
    if (result.job.status === "succeeded" && result.job.result?.project_id) {
      applyBootstrap(await api.bootstrap()); renderProjects();
      if (stillViewingSource()) await openProducedStory(result.job.result.project_id);
    } else {
      const input = result.job.story_input || draft.inputs || { idea: draft.idea || "" };
      const created = { ...result.job, repair_from: result.job.repair_from || draft.id,
        idea: input.idea, title: (input.idea || draft.title || "故事修复").slice(0, 60), inputs: input, project_id: null };
      state.storyDrafts = [created, ...state.storyDrafts.filter((item) => item.id !== created.id)];
      renderProjects();
      if (stillViewingSource()) await openStoryDraft(result.job.id);
    }
  } catch (error) { toast(error.message, true); }
  finally { state.pending.delete("create"); renderStage(); }
}
function storyStructureDetails(structure, label = "剧本结构") {
  if (!Array.isArray(structure?.episodes)) return null;
  const episodes = node("div", "story-structure");
  structure.episodes.forEach((episode, index) => {
    const section = node("section");
    const title = [`第 ${index + 1} 集`, episode.title].filter(Boolean).join(" · ");
    section.append(node("strong", "", `${title}（${episode.scene_count} 场）`));
    const scenes = node("ul");
    episode.scenes.forEach((scene, sceneIndex) => {
      const name = [`第 ${sceneIndex + 1} 场`, scene.title].filter(Boolean).join(" · ");
      scenes.append(node("li", "", `${name} · ${scene.beat_count} 个节拍`));
    });
    append(section, scenes); episodes.append(section);
  });
  return disclosure(`${label} · ${structure.episode_count} 集 · ${structure.scene_count} 场 · ${structure.beat_count} 个节拍`, [episodes]);
}
function renderStoryDraft() {
  if (state.storyDraftError) return emptyState("故事记录暂不可读取", state.storyDraftError,
    button("重新读取记录", () => openStoryDraft(state.storyDraftId), { quiet: true }));
  const draft = state.storyDraft || state.storyDrafts.find((item) => item.id === state.storyDraftId) || {};
  const container = node("div", "story-draft-view");
  const overview = node("section", "panel story-cover");
  append(overview, node("span", "section-label", "SAVED STORY / 已保存的创作"), node("h2", "", draft.title || "故事草稿"),
    pill(draft.status, draftStatus(draft.status)), node("p", "story-logline", draft.idea || draft.inputs?.idea || "本次创意未能恢复，可查看原始 Trace。"));
  const inputs = draft.inputs || {};
  const constraints = [
    Number.isInteger(inputs.episodes) && `${inputs.episodes} 集`,
    Number.isInteger(inputs.scenes) && `每集 ${inputs.scenes} 场`,
    Number.isInteger(inputs.beats) && `每场 ${inputs.beats} 节拍`,
  ].filter(Boolean);
  if (constraints.length) overview.append(node("p", "small-print", "本次指定篇幅　" + constraints.join(" · ")));
  append(overview, storyStructureDetails(draft.script_structure, draft.content_status === "published_script" ? "剧本结构" : "草稿实际结构"));
  overview.append(note(draft.error || draft.message || "创作记录已保存。", draft.status === "failed"));
  if (draft.content_status === "published_script" && draft.job?.script_ref) {
    const ref = draft.job.script_ref;
    overview.append(node("p", "small-print", `剧本 Agent 已发布 ${ref.asset_id} @ ${ref.revision_id}。可在下方打开剧本文件。`));
  }
  overview.append(node("p", "small-print", "当前结果尚未形成通过检查的制作工程，不能进入视频生成或声明制作完成。"));
  const actions = node("div", "story-actions");
  if (validTraceId(draft.trace_id)) actions.append(button("查看本次 Trace ↗", () => openTraceDesk(draft.trace_id), { quiet: true, small: true }));
  if (validTraceId(draft.job?.script_trace_id) && draft.job.script_trace_id !== draft.trace_id)
    actions.append(button("剧本 Agent Trace ↗", () => openTraceDesk(draft.job.script_trace_id), { quiet: true, small: true }));
  if (repairableStoryDraft(draft)) {
    const busy = state.pending.has("create") || state.jobs.some((job) => ACTIVE.has(job.status) && isStoryJob(job));
    actions.append(button(state.pending.has("create") ? "正在提交修复…" : "自动修复这份草稿", () => repairStoryDraft(draft),
      { primary: true, small: true, disabled: busy || !keyReady("story.compile") }));
    overview.append(node("p", "small-print", "自动修复会沿用原创意与创作参数，把已保存草稿和校验错误交给模型；每个阶段最多修复 2 次，可能产生文本调用费用。原失败记录继续保留。"));
  }
  const sourceId = draft.repair_from || draft.job?.repair_from;
  if (/^[a-f0-9]{32}$/.test(String(sourceId || ""))) {
    actions.append(button("查看修复前的记录", () => openStoryDraft(sourceId), { quiet: true, small: true }));
  }
  actions.append(button("使用此创意重新创作", () => {
    state.form = { idea: inputs.idea || draft.idea || "" };
    state.assetStyle = inputs.style || "";
    startNew(); announce("已填入原创意，可以开始新的创作。");
  }, { quiet: true, small: true }));
  overview.append(actions); container.append(overview);
  if (draft.script_text) {
    const reading = node("section", "panel draft-reading");
    append(reading, node("h3", "", "已保存的剧本文本"), node("pre", "draft-script", draft.script_text)); container.append(reading);
  } else if (draft.draft) {
    const reading = node("section", "panel draft-reading");
    append(reading, node("h3", "", "模型草稿 · 尚未通过校验"), node("pre", "trace-json", JSON.stringify(draft.draft, null, 2))); container.append(reading);
  } else container.append(note(ACTIVE.has(draft.status) ? "任务仍在进行；模型输出保存后会在这里更新。" : "尚无可阅读的剧本文本，已有调用记录可在 Trace 中查看。"));
  if (draft.validation) {
    const validation = node("details", "panel draft-reading"); validation.open = true;
    append(validation, node("summary", "", "校验结果与失败依据"), node("pre", "trace-json", JSON.stringify(draft.validation, null, 2))); container.append(validation);
  }
  const artifacts = node("section", "panel draft-reading"); artifacts.append(node("h3", "", "本次保存的文件"));
  for (const artifact of draft.artifacts || []) {
    const url = assetUrl(artifact.url); if (!url) continue;
    const link = node("a", "button button-quiet button-small", artifact.name || "查看文件"); link.href = url; link.target = "_blank"; link.rel = "noopener"; artifacts.append(link);
  }
  if (artifacts.children.length > 1) {
    artifacts.append(node("p", "small-print", "下载内容已脱敏，原始记录保留。"));
    container.append(artifacts);
  }
  return container;
}
async function openStoryDraft(id, { quiet = false } = {}) {
  if (!quiet) state.projectRead?.abort();
  const token = ++state.projectLoad;
  if (!quiet) {
    state.projectOpening = null;
    state.storyDraftId = id; state.storyDraft = null; state.storyDraftError = null;
    state.projectId = null; state.project = null; state.projectRevision = ""; state.shotId = null;
    state.creating = false; state.step = "story"; state.loading = true;
    renderProjects(); renderHeader(); renderJobs(); renderStage(); persistRoute();
  }
  try {
    if (!/^[a-f0-9]{32}$/.test(String(id || ""))) throw new Error("故事记录标识无效。");
    const draft = await api.storyDraft(id);
    if (token !== state.projectLoad || state.storyDraftId !== id || state.step === "traces" || state.creating) return;
    if (draft.project_id) {
      applyBootstrap(await api.bootstrap());
      if (token === state.projectLoad && state.storyDraftId === id) await openProducedStory(draft.project_id);
      return;
    }
    state.storyDraft = draft; state.storyDraftError = null; state.loading = false; state.storyDraftRefreshedAt = Date.now();
    state.storyDrafts = state.storyDrafts.some((item) => item.id === id)
      ? state.storyDrafts.map((item) => item.id === id ? draft : item)
      : [draft, ...state.storyDrafts];
    if (draft.job) state.jobs = [draft.job, ...state.jobs.filter((job) => job.id !== draft.job.id)];
    renderProjects(); renderHeader(); renderJobs(); renderStage(); persistRoute();
  } catch (error) {
    if (token !== state.projectLoad) return;
    state.loading = false; state.storyDraftError = error.message;
    renderHeader(); renderStage();
  }
}
function renderScriptText(text) {
  const reader = node("div", "script-reader");
  String(text || "").split(/\r?\n/).forEach((line) => {
    if (!line.trim()) return;
    const header = /^(#{1,4})\s+(.+)$/.exec(line);
    if (header) {
      if (header[1] === "#" && header[2] === state.project?.title) return;
      const section = node("div", "story-section"); section.append(node("h3", "", header[2])); reader.append(section);
    } else {
      reader.append(node("p", "story-outline", line.replace(/^[-*]\s+/, "· ")));
    }
  });
  return reader;
}
function renderStory() {
  const container = node("div"); const project = state.project;
  const cover = node("section", "panel story-cover");
  append(cover, node("span", "section-label", "THE SCREENPLAY / 剧本"), node("h2", "", project.title), node("p", "story-logline", project.logline || "剧本内容见下方阅读稿。"));
  append(cover, storyStructureDetails(project.script_structure));
  const ready = project.validation?.ready_for_generation === true;
  const actions = node("div", "story-actions");
  const statuses = node("div", "validation-statuses");
  [ ["规格", project.validation?.spec_pass], ["来源", project.validation?.provenance_pass], ["节拍覆盖", project.validation?.coverage_pass] ].forEach(([name, value]) => statuses.append(pill(value === true ? "PASS" : value === false ? "FAIL" : "UNKNOWN", `${name} ${value === true ? "已核对" : value === false ? "未通过" : "待核对"}`)));
  append(actions, statuses, button("进入分镜制作  →", () => setStep("shots"), { primary: true, disabled: !ready || !shots().length })); cover.append(actions); container.append(cover);
  if (!ready) container.append(note("制作规格尚未达到生成条件。请查看后台任务与验证结果，当前工程不能继续生成视频。", true));
  const reading = node("section", "story-section");
  reading.append(node("h3", "", "剧本阅读"));
  if (project.script_text) reading.append(renderScriptText(project.script_text));
  else reading.append(node("p", "story-outline", "该工程尚无可阅读的剧本。请等待后台编译完成。"));
  container.append(reading);
  container.append(node("p", "small-print", "规格、来源与节拍覆盖检查通过，表示具备生成条件。故事表达和最终画面仍需审看。"));
  return container;
}
function shotStrip() {
  const strip = node("div", "shot-strip"); strip.setAttribute("aria-label", "选择镜头");
  const current = selectedShot();
  shots().forEach((shot) => {
    const item = node("button", `shot-card${current?.id === shot.id ? " selected" : ""}`); item.type = "button";
    item.setAttribute("aria-pressed", String(current?.id === shot.id)); item.setAttribute("aria-label", `${shot.id}，${shot.duration_s} 秒，${shot.description || "镜头"}`);
    const top = node("div", "shot-card-top"); append(top, node("span", "shot-number", shot.id), node("span", "shot-duration", `${shot.duration_s || 0} SEC`));
    const bottom = node("div", "shot-card-bottom");
    const candidate = selectedCandidate(shot);
    append(bottom, candidate ? pill(candidate.status, readyCandidate(shot) ? "素材就绪" : undefined) : pill("neutral", "待生成"), node("span", "shot-selection-mark", current?.id === shot.id ? "↗" : ""));
    append(item, top, node("p", "shot-card-title", shot.description || shot.scene_name || "尚无镜头描述"), bottom);
    item.addEventListener("click", () => selectShot(shot.id)); strip.append(item);
  });
  return strip;
}
function player(url, label, emptyText = "这个镜头，等待开拍。") {
  const wrap = node("div", "video-stage"); const source = assetUrl(url);
  if (source) {
    const video = node("video"); video.controls = true; video.preload = "metadata"; video.playsInline = true; video.src = source; video.setAttribute("aria-label", label);
    video.addEventListener("error", () => toast("视频暂时无法播放，请确认候选文件仍存在，或在本机播放器中打开。", true));
    wrap.append(video);
  } else {
    const empty = node("div", "video-empty"); append(empty, node("span", "video-icon"), node("p", "", emptyText), node("small", "", "生成完成并下载后，可在此审看片段。")); wrap.append(empty);
  }
  return wrap;
}
function disclosure(title, children, open = false) {
  const element = node("details", "disclosure"); element.open = open; append(element, node("summary", "", title), ...children); return element;
}
function renderCandidates(shot) {
  const candidates = Array.isArray(shot.candidates) ? shot.candidates : [];
  if (!candidates.length) return null;
  const list = node("div", "candidate-list");
  [...candidates].reverse().forEach((candidate, index) => {
    const selected = candidate.id === shot.selected_candidate_id;
    const row = node("div", `candidate-row${selected ? " active" : ""}`);
    const text = node("div");
    append(text, node("strong", "", `候选 ${String(candidates.length - index).padStart(2, "0")}${selected ? " · 当前选用" : ""}`), node("small", "", `${readableTime(candidate.created_at)}${candidate.duration_s ? ` · ${candidate.duration_s} 秒` : ""}${candidate.task_id ? ` · 任务 ${String(candidate.task_id).slice(0, 8)}` : ""}`));
    const actions = node("div", "candidate-actions"); actions.append(pill(candidate.status));
    if (!selected && candidate.status === "SUCCEEDED" && assetUrl(candidate.video_url)) actions.append(button("选用", () => performAction("select", { shot_id: shot.id, candidate_id: candidate.id }, "已更新镜头选用；相关检查结果以当前候选为准。"), { small: true, disabled: busyProject() }));
    if (candidate.task_id && !assetUrl(candidate.video_url) && !["FAILED", "CANCELED", "CANCELLED", "UNKNOWN", "SUBMISSION_UNKNOWN"].includes(candidate.status)) actions.append(button("恢复 / 下载", () => performAction("resume", { candidate_id: candidate.id }, "已开始恢复已有候选，不会重新提交视频生成。"), { small: true, disabled: busyProject() }));
    append(row, text, actions); list.append(row);
  });
  return disclosure(`候选记录 · ${candidates.length} 个`, [list]);
}
function renderShots() {
  const container = node("div");
  const toolbar = node("div", "toolbar");
  const title = node("div"); append(title, node("h2", "", "逐个镜头，把故事拍出来。"), node("p", "", "选择一个分镜，生成、审看并保留你的候选。"));
  append(toolbar, title, node("span", "caption-caps", `${shots().length} SHOTS`)); append(container, toolbar, shotStrip());
  const shot = selectedShot(); if (!shot) return emptyState("分镜还在路上", "先完成剧本与制作规格，镜头会按故事顺序出现在这里。", button("查看剧本", () => setStep("story")));
  const candidate = selectedCandidate(shot); const ready = readyCandidate(shot);
  const detail = node("section", "panel shot-detail");
  const heading = node("div", "detail-heading"); const info = node("div");
  append(info, node("h3", "", `${shot.id} · ${shot.scene_name || "镜头制作"}`), node("small", "", `目标 ${shot.duration_s} 秒${candidate?.duration_s ? ` · 当前候选 ${candidate.duration_s} 秒` : ""} · ${candidate ? `保留 ${shot.candidates.length} 个候选` : "尚无视频候选"}`));
  const acceptance = shot.report?.status || "PENDING";
  append(heading, info, ready ? pill(acceptance, shotAcceptanceLabel(acceptance)) : pill(candidate?.status || "neutral", candidate ? undefined : "待生成"));
  append(detail, heading, player(ready?.video_url, `${shot.id} 当前视频候选`));
  const content = node("div", "shot-content"); content.append(node("p", "shot-description", shot.description));
  if (shot.characters?.length) { const entities = node("div", "shot-entities"); shot.characters.forEach((name) => entities.append(node("span", "entity-tag", name))); content.append(entities); }
  const promptKey = `${state.projectId}/${shot.id}`;
  const custom = node("textarea"); custom.value = state.prompts.get(promptKey) || ""; custom.placeholder = "留空使用根据剧本、人物设定和状态自动编译的提示词。"; custom.maxLength = 8000;
  custom.addEventListener("input", () => state.prompts.set(promptKey, custom.value));
  const promptField = formField("shot-prompt", "自定义完整提示词（可选）", custom, "填写后会替换整个自动提示词。请自行保留人物、场景、动作与起止状态要求；已有候选不会被改写。");
  const details = [promptField];
  if (shot.prompt) details.push(disclosure("查看自动编译的提示词", [node("p", "small-print text-pre", shot.prompt), button("复制自动提示词", async () => { try { await navigator.clipboard.writeText(shot.prompt); toast("自动提示词已复制。"); } catch (_) { toast("浏览器未允许复制，请展开提示词手动选择。", true); } }, { quiet: true, small: true })]));
  content.append(disclosure("提示词与生成条件", details));
  if (shot.state_before || shot.state_after) content.append(disclosure("查看剧情起止状态", [node("p", "small-print text-pre", `开始状态\n${JSON.stringify(shot.state_before || {}, null, 2)}\n\n结束状态\n${JSON.stringify(shot.state_after || {}, null, 2)}`)]));
  const actions = node("div", "action-row");
  const managed=!!studioLocal(state.projectId).data?.asset_work?.items?.length;
  const disabled = busyProject() || !keyReady("video.text_to_video") || state.project.validation?.ready_for_generation !== true || managed;
  if(managed)content.append(note("本故事使用固定参考和拍摄计划。请在本阶段上方委托或继续视频制作 Agent；此处保留候选信息与已有任务下载。"));
  const createBody = (newCandidate) => ({ shot_id: shot.id, ...(custom.value.trim() ? { prompt: custom.value.trim() } : {}), ...(newCandidate ? { new_candidate: true } : {}) });
  if (!candidate || !ready) {
    const resumable = candidate?.task_id && !["FAILED", "CANCELED", "CANCELLED", "UNKNOWN", "SUBMISSION_UNKNOWN"].includes(candidate.status);
    if (resumable) actions.append(button("恢复任务 / 下载", () => performAction("resume", { candidate_id: candidate.id }, "已开始恢复已有任务，不会重新提交视频生成。"), { primary: true, disabled:busyProject() }));
    else if (!candidate) actions.append(button("生成这个镜头  ↗", () => performAction("generate", createBody(false), "镜头生成任务已提交。"), { primary: true, disabled }));
  }
  if (candidate) actions.append(button("再次生成 · 新候选", () => performAction("generate", createBody(true), "已请求新候选，将产生新的模型调用。"), { primary: !ready, disabled }));
  if (ready) actions.append(button("检查这个镜头  →", () => setStep("checks"), { primary: true }));
  append(content, actions, billing("生成视频会调用已配置的模型，可能计费。‘新候选’会再次生成，并保留已有版本。"));
  if (!keyReady("video.text_to_video")) content.append(note("视频生成模型缺少密钥，暂不能生成新候选。", true));
  if (candidate?.status === "SUBMISSION_UNKNOWN") content.append(note("上次提交结果不明确，请先到原供应商的控制台核对。系统不会自动重发；创建新候选可能重复计费。", true));
  if (ready) content.append(note("视频已生成。人物身份、连续动作与完整声音等条件，仍需各自的验收证据。"));
  content.append(...[renderCandidates(shot)].filter(Boolean)); detail.append(content); container.append(detail); return container;
}
function reportItems(report) { return Array.isArray(report?.checks) ? report.checks : Array.isArray(report?.results) ? report.results : []; }
function criterionName(item) {
  const id = item.id || item.criterion_id || "";
  return CRITERIA[id] || (id.startsWith("continuity:") ? `相邻镜头衔接 · ${id.slice(11)}` : item.expected || item.expectation || id || "检查条件");
}
function renderReport(report) {
  const section = node("section", "story-section");
  const declared = ["PASS", "FAIL", "UNKNOWN"].includes(report.status) ? report.status : "UNKNOWN";
  const status = declared === "PASS" && report.task_complete !== true ? "UNKNOWN" : declared;
  const complete = status === "PASS" && report.task_complete === true;
  const line = node("div", "report-status-line");
  append(line, pill(status, status === "PASS" ? "本次检查通过" : status === "FAIL" ? "检查发现失败项" : "验收未完成 · 证据不足"), node("p", "", complete ? "契约中的必过项已通过；创作质量仍需独立审看。" : "任务尚未验收完成，未知条件不会视为成功。")); section.append(line);
  const items = reportItems(report);
  const counts = { PASS: 0, FAIL: 0, UNKNOWN: 0 }; items.forEach((item) => { counts[["PASS", "FAIL", "UNKNOWN"].includes(item.status) ? item.status : "UNKNOWN"] += 1; });
  const summary = node("div", "report-summary");
  [["PASS", "通过条件"], ["FAIL", "失败条件"], ["UNKNOWN", "未知 / 待补证据"]].forEach(([value, label]) => { const stat = node("div", `report-stat ${value.toLowerCase()}`); append(stat, node("strong", "", counts[value]), node("span", "", label)); summary.append(stat); }); section.append(summary);
  const list = node("div", "check-list");
  items.forEach((item) => {
    const row = node("article", "check-item"); const top = node("div", "check-item-top"); const title = node("h3", "", criterionName(item));
    if (item.required === true) title.append(node("span", "required-label", "必过"));
    append(top, title, pill(item.status)); append(row, top, node("p", "", item.reason || "暂无足够证据。"));
    if (item.execution_status) {
      row.append(node("p","small-print",({executed:"已实际检查 · 结论待你确认",inconclusive:"已实际检查 · 证据仍不足",
        evidence_invalid:"已收到观察 · 证据核验未通过",execution_error:"执行出错 · 未取得有效结论",
        blocked_missing_media:"素材缺失 · 此项尚未执行",not_run:"此项尚未执行"})[item.execution_status] || item.execution_status));
    }
    for (const part of item.components || []) row.append(node("p","small-print",
      ({identity:"人物",costume_props:"服装与道具",space:"空间",action:"动作",audio:"声音"}[part.name] || part.name) +
      " · " + part.verdict + " · " + part.reason));
    for (const evidence of item.evidence || []) {
      if (!assetUrl(evidence.url)) continue;
      const link=node("a","studio-link", evidence.type === "video" ?
        "查看视频证据 " + Number(evidence.start_s).toFixed(2) + "–" + Number(evidence.end_s).toFixed(2) + " 秒 ↗" :
        evidence.type === "audio" ? "试听实际音轨 " + Number(evidence.start_s).toFixed(2) + "–" + Number(evidence.end_s).toFixed(2) + " 秒 ↗" :
        evidence.type === "reference_audio" ? "试听音色参考 " + evidence.asset_id + " ↗" :
        evidence.type === "reference_image" ? "对照参考 " + evidence.asset_id + " ↗" : "查看证据 ↗");
      link.href=assetUrl(evidence.url) + (["video","audio"].includes(evidence.type) ? "#t=" + evidence.start_s + "," + evidence.end_s : "");
      link.target="_blank"; link.rel="noopener"; row.append(link);
      if (evidence.precision === "whole_submitted_track") row.append(node("p","small-print","音轨定位范围为实际提交的整段切点片段；模型未给出更细的句级时间。"));
      if (evidence.observation) row.append(node("p","small-print",evidence.observation));
    }
    if (item.limitations?.length) row.append(node("p","small-print","判断边界：" + item.limitations.join("；")));
    if (item.state_checks?.length) {
      const detail=node("details",""); detail.append(node("summary","","查看逐项起止状态"));
      if (item.supplemental_state_note) detail.append(node("p","small-print",item.supplemental_state_note));
      for (const check of item.state_checks) detail.append(node("p","small-print",
        (check.phase === "start" ? "开始" : "结束") + " · " + check.path + " · " + check.verdict +
        " · 预期 " + JSON.stringify(check.expected) + "，实际 " + JSON.stringify(check.actual) + " · " + check.reason));
      row.append(detail);
    }
    const expectation = item.expected || item.expectation;
    if (expectation && expectation !== criterionName(item)) row.append(node("p", "", `预期：${typeof expectation === "string" ? expectation : JSON.stringify(expectation)}`));
    list.append(row);
  }); section.append(list);
  const frames = (report.frames || []).filter((frame) => assetUrl(frame.url));
  if (frames.length) {
    const heading = node("div", "story-section"); append(heading, node("h3", "", report.content_review_version ? "视频关键画面 · 便于定位" : "带时间戳的抽样帧"),
      node("p", "small-print", report.content_review_version ?
        "下列静帧用于快速查看。内容结论来自上方已绑定的视频片段、参考素材与切点声音检查；视频按10fps审看，更短时间内的变化仍有采样边界。" :
        "这些画面只代表实际抽到的时刻，不能证明整个动作过程或声音内容。")); section.append(heading);
    const grid = node("div", "frame-grid");
    frames.forEach((frame) => {
      const figure = node("figure", "frame-card"); const link = node("a"); link.href = assetUrl(frame.url); link.target = "_blank"; link.rel = "noopener";
      const image = node("img"); image.src = assetUrl(frame.url); image.alt = `${frame.id}，视频 ${Number(frame.timestamp_s || 0).toFixed(2)} 秒的抽样帧`; image.loading = "lazy"; link.append(image);
      const caption = node("figcaption"); append(caption, node("span", "", frame.id), node("span", "", `${Number(frame.timestamp_s || 0).toFixed(2)}s`)); append(figure, link, caption); grid.append(figure);
    }); section.append(grid);
  }
  if (assetUrl(report.report_url)) { const link = node("a", "button button-quiet button-small report-download", "下载完整验收报告 ↗"); link.href = assetUrl(report.report_url); link.download = "acceptance-report.json"; section.append(link); }
  return section;
}
function renderChecks() {
  const shot = selectedShot(); if (!shot) return emptyState("先准备好你的分镜", "有了制作规格和镜头候选，才能按照固定契约检查素材。", button("查看剧本", () => setStep("story")));
  const container = node("div"); append(container, shotStrip());
  const candidate = readyCandidate(shot); const panel = node("section", "panel");
  const heading = node("div", "panel-heading"); const text = node("div");
  append(text, node("h2", "", `${shot.id} · 本地媒体诊断`), node("p", "", "用于定位技术和抽样画面问题。完整验收与修复由本阶段的视频 Agent 持续执行。")); append(heading, text, pill(shot.report?.status || "UNKNOWN", shot.report ? undefined : "尚未检查")); panel.append(heading);
  if (!candidate) panel.append(note("当前镜头还没有可检查的视频。请先生成或选用已下载的候选。", true));
  const actions = node("div", "action-row");
  const body = { shot_id: shot.id, ...(candidate ? { candidate_id: candidate.id } : {}) };
  const blocked = !candidate || busyProject() || !toolReady();
  append(actions,
    button("本地检查", () => performAction("check", { ...body, with_model: false }, "已开始本地媒体检查。"), { disabled: blocked }),
    button("视觉观察 + 检查  ↗", () => performAction("check", { ...body, with_model: true }, "已开始抽帧观察与检查，视觉模型只判断抽样静帧。"), { primary: true, disabled: blocked || !keyReady("vision.review") }));
  append(panel, actions, billing("本地检查不调用模型。视觉观察会调用已配置的模型，可能计费。"));
  if (!toolReady()) panel.append(note("本地 FFmpeg / ffprobe 未就绪，暂时不能检查媒体或导出。", true));
  panel.append(node("p", "small-print", "抽样观察只覆盖可见的道具、服装等静态内容。角色身份、连续动作、场景几何、音频和邻片衔接缺少相应证据时保留 UNKNOWN。")); container.append(panel);
  if (shot.report) container.append(renderReport(shot.report));
  else container.append(note("尚无验收报告。生成接口返回成功，只说明任务完成，不表示画面满足剧本。"));
  return container;
}
function renderExport() {
  const data=studioLocal(state.projectId).data;
  return data?studioReview(data,studioCatalog(data),false):note("正在核对当前版本的验收与预览权限。");
}
function validTraceId(value) { return typeof value === "string" && /^[a-f0-9]{32}$/i.test(value); }
function traceStatus(value, endRecorded) {
  const status = String(value || "UNKNOWN").toUpperCase();
  return pill(status === "OK" ? "PASS" : status === "ERROR" ? "FAIL" : "UNKNOWN",
    status === "OK" ? "执行完成" : status === "ERROR" ? "执行失败" : endRecorded === false ? "未结束" : "未知");
}
function traceTimestamp(value) {
  if (!value) return "未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3, hour12: false,
  });
}
function traceDuration(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "时长待确定";
  const amount = Number(value);
  return amount < 1000 ? `${amount.toFixed(1)} ms` : `${(amount / 1000).toFixed(2)} s`;
}
function traceSafe(value, key = "") {
  // The server is the authoritative redaction boundary. This also prevents a
  // secret-shaped field or an unprocessed data URI from being rendered by the UI.
  if (/^(authorization|proxy-authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)$/i.test(key)) return "[REDACTED]";
  if (typeof value === "string") {
    if (/^data:[^;]+;base64,/i.test(value)) return `[二进制 data URI：${value.length} 字符，未在界面展开]`;
    return value.replace(/\bsk-[A-Za-z0-9_-]{12,}\b/g, "[REDACTED]");
  }
  if (Array.isArray(value)) return value.map((item) => traceSafe(item));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, traceSafe(item, name)]));
  return value;
}
function isTraceLiteral(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.prototype.hasOwnProperty.call(value, "$literal") && value.$literal_reason === "user_artifact_shape";
}
function isTraceArtifact(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === 3 && ["$artifact", "bytes", "media_type"].every((key) => Object.prototype.hasOwnProperty.call(value, key));
}
function traceSummary(value) {
  if (value === undefined) return "未记录";
  if (value === null) return "null";
  if (isTraceLiteral(value)) return "用户 JSON · 非内容引用";
  if (isTraceArtifact(value)) return `独立内容 · ${Number(value.bytes || 0).toLocaleString()} bytes`;
  if (Array.isArray(value)) return `${value.length} 项`;
  if (typeof value === "object") return `${Object.keys(value).length} 个字段`;
  if (typeof value === "string") return `${value.length.toLocaleString()} 字符`;
  return String(value);
}
function tracePre(value) {
  return node("pre", "trace-json", typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "未记录");
}
function traceValue(label, original, { open = false } = {}) {
  const value = traceSafe(original);
  const details = node("details", "trace-value");
  const summary = node("summary"); append(summary, node("span", "", label), node("small", "", traceSummary(value))); details.append(summary);
  const content = node("div", "trace-value-body"); details.append(content);
  let filled = false;
  const fill = () => {
    if (filled) return;
    filled = true;
    if (isTraceLiteral(value)) {
      // This SDK escape marks ordinary user data that happens to resemble an
      // artifact reference. Never traverse it as references or issue a fetch.
      append(content, node("p", "trace-meta", "这段用户数据与引用结构同形，按普通 JSON 展示。"), tracePre(value.$literal));
      return;
    }
    if (isTraceArtifact(value)) {
      const artifactId = value.$artifact;
      content.append(node("p", "trace-meta", `SHA-256 · ${artifactId}`));
      if (!/^[a-f0-9]{64}$/i.test(artifactId)) { content.append(note("内容标识无效，未读取任何文件。", true)); return; }
      const load = button("加载完整内容", async () => {
        load.disabled = true; load.textContent = "正在读取内容…";
        try {
          let artifact = state.trace.artifacts.get(artifactId);
          if (!artifact) { artifact = await api.traceArtifact(artifactId); state.trace.artifacts.set(artifactId, artifact); }
          content.replaceChildren(node("p", "trace-meta", `SHA-256 · ${artifactId}`), traceValue("完整内容", artifact.data, { open: true }));
        } catch (error) { load.disabled = false; load.textContent = "重试加载"; content.append(note(error.message, true)); }
      }, { small: true });
      content.append(load);
      return;
    }
    if (value && typeof value === "object") {
      const entries = Object.entries(value);
      if (!entries.length) { content.append(tracePre(value)); return; }
      entries.slice(0, 200).forEach(([name, item]) => content.append(traceValue(Array.isArray(value) ? `[${name}]` : name, item)));
      if (entries.length > 200) content.append(node("p", "trace-meta", `已展开前 200 / ${entries.length} 项；下方可查看完整 JSON。`));
      const all = node("details", "trace-whole-json"); all.append(node("summary", "", "查看完整 JSON"));
      all.addEventListener("toggle", () => { if (all.open && all.childElementCount === 1) all.append(tracePre(value)); }); content.append(all);
    } else content.append(tracePre(value));
  };
  details.addEventListener("toggle", () => { if (details.open) fill(); });
  if (open) { details.open = true; fill(); }
  return details;
}
function traceProblemIds(spans, trace) {
  const items = trace?.asset_diagnostics?.items;
  if (items?.length) {
    // A handled lookup error or a repaired attempt is not a reason the final asset failed.
    return new Map(items.filter(item => item.span_id && (!item.trace_id || item.trace_id === trace.trace_id))
      .map(item => [item.span_id, item.verdict]));
  }
  const issues = new Map((trace?.problem_spans || []).map(problem => [problem.span_id, problem.status]));
  for (const span of spans) {
    if (!trace?.problem_spans) {
      const check = span.events?.find(e => e.kind === "validation" && e.data?.required !== false && ["FAIL","UNKNOWN"].includes(e.data?.status));
      if (check) issues.set(span.span_id, check.data.status);
    }
    if (span.status === "ERROR" || span.status === "UNKNOWN") issues.set(span.span_id, span.status);
  }
  return issues;
}
function selectedTraceSpan(spans = [], requested = null) {
  if (spans.some(span => span.span_id === requested)) return requested;
  const issues = traceProblemIds(spans, state.trace.detail);
  const available = new Set(spans.map(span => span.span_id));
  return [...issues.keys()].find(id => available.has(id)) || spans[0]?.span_id || null;
}
async function openTraceDesk(id = null, spanId = null) {
  state.projectRead?.abort();
  state.projectOpening = null;
  state.projectLoad += 1; state.creating = false; state.loading = false; state.step = "traces";
  state.trace.id = validTraceId(id) ? id : null; state.trace.detail = null;
  state.trace.spanId = state.trace.id && validTraceId(spanId) ? spanId.toLowerCase() : null;
  state.trace.view = "spans"; state.trace.showAllSpans = false;
  state.trace.detailError = id && !validTraceId(id) ? "Trace 标识格式无效。" : null;
  state.trace.verification = null; state.trace.eventsError = null; state.trace.detailToken += 1;
  renderProjects(); renderHeader(); renderStage(); persistRoute();
  announce("已打开 Trace 调试台");
  if (state.trace.id) {
    // A linked detail should not wait for the independent, potentially large list.
    await Promise.all([loadTraceList(), loadTraceDetail(state.trace.id)]);
    return;
  }
  const detailToken = state.trace.detailToken;
  await loadTraceList();
  if (state.step !== "traces" || detailToken !== state.trace.detailToken) return;
  if (!state.trace.detailError && state.trace.list.length) await selectTrace(state.trace.list[0].trace_id);
}
async function loadTraceList() {
  const tracing = state.trace; const token = ++tracing.listToken;
  tracing.listLoading = true; tracing.listError = null;
  if (state.step === "traces") renderStage();
  const query = { limit: String(tracing.limit), offset: String(tracing.offset) };
  for (const [key, value] of Object.entries(tracing.filters)) if (value) query[key] = value;
  try {
    const response = await api.traces(query);
    if (token !== tracing.listToken) return;
    tracing.list = Array.isArray(response.traces) ? response.traces : [];
    tracing.refreshedAt = Date.now();
  } catch (error) { if (token === tracing.listToken) tracing.listError = error.message; }
  finally { if (token === tracing.listToken) { tracing.listLoading = false; if (state.step === "traces") renderStage(); } }
}
async function loadTraceDetail(id) {
  if (!validTraceId(id)) return;
  const tracing = state.trace; const token = ++tracing.detailToken;
  tracing.detailLoading = true; tracing.detailError = null;
  if (state.step === "traces") renderStage();
  try {
    const response = await api.trace(id);
    if (token !== tracing.detailToken || id !== tracing.id) return;
    tracing.detail = response;
    const spans = Array.isArray(response.spans) ? response.spans : [];
    const requested = tracing.spanId;
    tracing.spanId = selectedTraceSpan(spans, requested);
    if (requested && requested !== tracing.spanId && state.step === "traces") persistRoute();
  } catch (error) { if (token === tracing.detailToken) tracing.detailError = error.message; }
  finally { if (token === tracing.detailToken) { tracing.detailLoading = false; if (state.step === "traces") renderStage(); } }
}
async function selectTrace(id) {
  if (!validTraceId(id)) return;
  state.trace.id = id; state.trace.detail = null; state.trace.spanId = null; state.trace.verification = null; state.trace.eventsError = null; state.trace.eventLimit = 100;
  state.trace.showAllSpans = false; state.trace.view = "spans";
  persistRoute(); await loadTraceDetail(id);
}
async function refreshTraces() {
  if (state.trace.listLoading || state.trace.detailLoading) return;
  const id = state.trace.id;
  await loadTraceList();
  if (id && state.step === "traces" && state.trace.id === id) await loadTraceDetail(id);
}
async function loadMoreTraceEvents() {
  const tracing = state.trace; const trace = tracing.detail;
  if (!trace || tracing.eventsLoading || !trace.events_has_more) return;
  const id = tracing.id; tracing.eventsLoading = true; tracing.eventsError = null; renderStage();
  try {
    const response = await api.traceEvents(id, trace.next_after);
    if (tracing.id !== id || tracing.detail !== trace) return;
    const combined = new Map((trace.events || []).map((event) => [event.seq, event]));
    (response.events || []).forEach((event) => combined.set(event.seq, event));
    trace.events = [...combined.values()].sort((a, b) => Number(a.seq) - Number(b.seq));
    trace.events_has_more = Boolean(response.has_more); trace.next_after = response.next_after;
    (trace.spans || []).forEach((span) => { span.events = trace.events.filter((event) => event.span_id === span.span_id); });
    tracing.eventLimit = Math.max(tracing.eventLimit || 100, trace.events.length);
  } catch (error) { if (tracing.id === id) tracing.eventsError = error.message; }
  finally { tracing.eventsLoading = false; if (state.step === "traces") renderStage(); }
}
function renderTraceFilters() {
  const tracing = state.trace;
  const form = node("form", "trace-filters");
  const kind = node("select"); kind.id = "trace-kind"; kind.setAttribute("aria-label", "Trace 类型");
  [["task", "任务"], ["http", "HTTP 请求"], ["", "全部类型"]].forEach(([value, label]) => { const option = node("option", "", label); option.value = value; kind.append(option); });
  kind.value = tracing.filters.kind;
  const status = node("select"); status.id = "trace-status"; status.setAttribute("aria-label", "Trace 状态");
  [["", "全部状态"], ["ERROR", "执行失败"], ["OK", "执行完成"], ["UNKNOWN", "未知 / 未结束"]].forEach(([value, label]) => { const option = node("option", "", label); option.value = value; status.append(option); });
  status.value = tracing.filters.status;
  const search = node("input"); search.id = "trace-search"; search.type = "search"; search.value = tracing.filters.search; search.placeholder = "搜索名称 / 任务 ID"; search.maxLength = 200; search.setAttribute("aria-label", "搜索 Trace 名称或任务 ID");
  kind.addEventListener("change", () => { tracing.filters.kind = kind.value; });
  status.addEventListener("change", () => { tracing.filters.status = status.value; });
  search.addEventListener("input", () => { tracing.filters.search = search.value; });
  const submit = button("筛选", () => {}, { primary: true, disabled: tracing.listLoading }); submit.type = "submit";
  form.addEventListener("submit", (event) => { event.preventDefault(); tracing.offset = 0; void loadTraceList(); });
  append(form, kind, status, search, submit, button("刷新", () => refreshTraces(), { disabled: tracing.listLoading || tracing.detailLoading }));
  const auto = node("label", "trace-auto"); const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = tracing.autoRefresh;
  checkbox.addEventListener("change", () => { tracing.autoRefresh = checkbox.checked; }); append(auto, checkbox, node("span", "", "自动刷新 · 5 秒")); form.append(auto);
  return form;
}
function renderTraceList() {
  const tracing = state.trace; const container = node("section", "trace-list-pane");
  const head = node("div", "trace-pane-heading"); append(head, node("h2", "", "调用记录"), node("span", "trace-meta", tracing.listLoading ? "正在读取…" : `本页 ${tracing.list.length} 条`)); container.append(head);
  if (tracing.listError) container.append(note(tracing.listError, true));
  if (!tracing.list.length && !tracing.listLoading && !tracing.listError) container.append(node("p", "trace-empty", "当前筛选下没有 Trace。旧任务不会补造历史事件；新任务及接入 SDK 的程序会从执行时开始记录。"));
  const list = node("div", "trace-list"); list.setAttribute("aria-label", "Trace 记录列表");
  tracing.list.forEach((trace) => {
    const selected = trace.trace_id === tracing.id;
    const item = node("button", `trace-list-item${selected ? " selected" : ""}`); item.type = "button"; item.setAttribute("aria-pressed", String(selected));
    item.setAttribute("aria-label", `${trace.name || "未命名 Trace"}，${trace.trace_id}`);
    const top = node("div", "trace-list-top"); append(top, node("span", "trace-kind", trace.kind || "trace"), traceStatus(trace.status, !trace.open_span_count && Boolean(trace.end_time)));
    append(item, top, node("strong", "", trace.name || "未命名 Trace"), node("code", "trace-id", trace.trace_id), node("span", "trace-meta", `${traceTimestamp(trace.start_time)} · ${traceDuration(trace.duration_ms)}`));
    append(item, node("span", "trace-meta", `${trace.span_count || 0} spans · ${trace.event_count || 0} events`));
    item.disabled = !validTraceId(trace.trace_id); item.addEventListener("click", () => selectTrace(trace.trace_id)); list.append(item);
  });
  container.append(list);
  const paging = node("div", "trace-pagination");
  append(paging, button("上一页", () => { tracing.offset = Math.max(0, tracing.offset - tracing.limit); void loadTraceList(); }, { small: true, disabled: tracing.offset === 0 || tracing.listLoading }),
    node("span", "trace-meta", `第 ${Math.floor(tracing.offset / tracing.limit) + 1} 页`),
    button("下一页", () => { tracing.offset += tracing.limit; void loadTraceList(); }, { small: true, disabled: tracing.list.length < tracing.limit || tracing.listLoading })); container.append(paging);
  return container;
}
function renderSpanTree(spans) {
  const section = node("section", "trace-span-tree"); section.setAttribute("aria-label", "调用层级");
  const tracing = state.trace;
  const problems = traceProblemIds(spans, tracing.detail);
  const byId = new Map(spans.map(s => [s.span_id,s]));
  const visible = new Set(spans.filter(s => !byId.has(s.parent_span_id)).map(s => s.span_id));
  for (const id of [...problems.keys(), tracing.spanId]) {
    let span = byId.get(id);
    while (span && !visible.has(span.span_id)) {
      visible.add(span.span_id);
      span = byId.get(span.parent_span_id);
    }
  }
  const rows = tracing.showAllSpans ? spans : spans.filter(s => visible.has(s.span_id));
  const controls = node("div", "trace-focus-controls");
  controls.append(node("span", "trace-meta", rows.length === spans.length ? `显示全部 ${spans.length} 个调用`
    : `显示 ${rows.length} / ${spans.length} 个调用 · 其余分支已收起`));
  if (spans.length > visible.size || tracing.showAllSpans) {
    controls.append(button(tracing.showAllSpans ? (problems.size ? "只看问题调用" : "收起已完成调用") : "展开全部调用", () => {
      tracing.showAllSpans = !tracing.showAllSpans;
      if (!tracing.showAllSpans) { tracing.spanId = selectedTraceSpan(spans); persistRoute(); }
      renderStage();
    }, {small:true}));
  }
  section.append(controls);
  const children = new Map(); const visited = new Set();
  rows.forEach((span) => { const parent = byId.has(span.parent_span_id) ? span.parent_span_id : null; if (!children.has(parent)) children.set(parent, []); children.get(parent).push(span); });
  const walk = (items, depth = 0) => {
    const list = node("ul", "trace-tree-level");
    items.forEach((span) => {
      if (visited.has(span.span_id)) return;
      visited.add(span.span_id);
      const item = node("li"); const pick = node("button", `trace-span-button${state.trace.spanId === span.span_id ? " selected" : ""}`); pick.type = "button";
      pick.setAttribute("aria-pressed", String(state.trace.spanId === span.span_id));
      const finding = problems.get(span.span_id);
      const badge = span.status === "OK" && ["FAIL", "UNKNOWN"].includes(finding)
        ? pill(finding, finding === "FAIL" ? "检查未通过" : "证据不足") : traceStatus(span.status, span.end_recorded);
      append(pick, node("span", "trace-kind", span.kind || "span"), node("strong", "", span.name || span.span_id), badge, node("small", "trace-meta", traceDuration(span.duration_ms)));
      pick.addEventListener("click", () => { state.trace.spanId = span.span_id; persistRoute(); renderStage(); }); item.append(pick);
      if (children.has(span.span_id) && depth < 100) item.append(walk(children.get(span.span_id), depth + 1));
      list.append(item);
    });
    return list;
  };
  section.append(walk(children.get(null) || []));
  const unlinked = rows.filter((span) => !visited.has(span.span_id));
  if (unlinked.length) { section.append(node("p", "trace-meta", "以下记录存在循环或缺失的关联，按平级展示：")); section.append(walk(unlinked, 100)); }
  return section;
}
function renderTraceEvents(events, { compact = false } = {}) {
  const collection = [...events].sort((a, b) => Number(a.trace_seq ?? a.seq) - Number(b.trace_seq ?? b.seq));
  const limit = compact ? 100 : state.trace.eventLimit || 100;
  const list = node("div", "trace-events");
  if (!collection.length) list.append(node("p", "trace-empty", "没有已记录事件。"));
  collection.slice(0, limit).forEach((event) => {
    const row = node("details", "trace-event"); const summary = node("summary", "trace-event-summary");
    append(summary, node("span", "trace-sequence", `#${event.trace_seq ?? event.seq ?? "?"}`), node("span", "trace-event-time", traceTimestamp(event.timestamp)), node("span", "trace-kind", event.kind || "event"), node("strong", "", event.name || "事件")); row.append(summary);
    let filled = false;
    row.addEventListener("toggle", () => {
      if (!row.open || filled) return; filled = true;
      const body = node("div", "trace-event-body");
      append(body, node("code", "trace-meta", `span_id: ${event.span_id || "—"} · event_id: ${event.event_id || "—"}`), traceValue("事件数据", event.data, { open: true }), traceValue("完整事件记录", event)); row.append(body);
    }); list.append(row);
  });
  if (collection.length > limit) {
    list.append(node("p", "trace-meta", `已展示 ${limit} / ${collection.length} 条事件；JSON 导出包含全部记录。`));
    if (!compact) list.append(button("继续显示 100 条", () => { state.trace.eventLimit = limit + 100; renderStage(); }, { small: true }));
  }
  return list;
}
function selectedSpanEvents(trace, span) {
  const box = node("details", "trace-span-events");
  const local = span.events || [];
  if (!trace.events_has_more && local.length <= 100) {
    append(box, node("summary", "", `此调用的事件 · ${local.length} 条`), renderTraceEvents(local, {compact:true}));
    return box;
  }
  const summary = node("summary", "", "查看此调用的事件");
  const pages = node("div"); const footer = node("div", "trace-detail-actions");
  append(box, summary, pages, footer);
  let busy = false, more = true, cursor = 0, count = 0;
  const load = async () => {
    if (busy || !more) return;
    busy = true; footer.replaceChildren(node("small", "trace-meta", "正在读取此调用的事件…"));
    try {
      const page = await api.traceEvents(trace.trace_id, cursor, span.span_id);
      pages.append(renderTraceEvents(page.events, {compact:true}));
      count += page.events.length; more = page.has_more; cursor = page.next_after;
      summary.textContent = `此调用的事件 · 已载入 ${count} 条`;
      footer.replaceChildren();
      if (more) footer.append(button("加载此调用的下一页", load, {small:true}));
    } catch (error) { footer.replaceChildren(note(error.message, true), button("重试读取", load, {small:true})); }
    finally { busy = false; }
  };
  box.addEventListener("toggle", () => { if (box.open && !count) void load(); });
  return box;
}
function renderTraceFindings(trace) {
  const diagnostics = trace.asset_diagnostics;
  const box = node("section", "trace-findings");
  if (diagnostics?.items?.length) {
    box.append(node("h3", "", `本次任务有 ${diagnostics.items.length} 项资产需处理`));
    for (const item of diagnostics.items) {
      const row = node("article", "trace-finding");
      const title = node("div", "trace-finding-heading");
      const label = assetIssueName(item);
      append(title, node("strong", "", label + (item.revision_id ? " · " + item.revision_id : "")),
        pill(item.verdict === "FAIL" ? "FAIL" : "UNKNOWN", item.verdict === "FAIL" ? "未通过" : "证据不足"));
      append(row, title, node("p", "", traceSafe(item.reason || "原任务没有记录具体原因。")));
      if (item.action) row.append(node("p", "trace-next-action", "建议操作　" + traceSafe(item.action)));
      if (item.evidence?.length) {
        const evidence = node("details", "trace-check-evidence");
        evidence.append(node("summary", "", `自检依据 · ${item.evidence.length} 项`));
        for (const check of item.evidence) {
          const part = node("div");
          append(part, node("strong", "", check.label || check.path), traceValue("要求", check.expected),
            traceValue("实际观察", check.actual, {open:true}));
          evidence.append(part);
        }
        row.append(evidence);
      }
      const url = assetIssueTraceURL(item);
      if (url && item.span_id) {
        const direct = node("a", "button button-small", "查看对应调用");
        direct.href = url; direct.setAttribute("aria-label", "查看" + label + "的对应调用");
        row.append(direct);
      }
      box.append(row);
    }
    return box;
  }
  const root = trace.spans?.find(s => !s.parent_span_id);
  if (root?.status !== "ERROR" || typeof root.error?.message !== "string" || !root.error.message) return null;
  append(box, node("h3", "", "任务停止原因"), node("p", "", traceSafe(root.error.message)));
  return box;
}
function renderTraceDetail() {
  const tracing = state.trace; const trace = tracing.detail; const panel = node("section", "trace-detail-pane");
  if (tracing.detailError) { panel.append(note(tracing.detailError, true)); return panel; }
  if (!trace) { panel.append(node("p", "trace-empty", tracing.detailLoading ? "正在读取完整 Trace…" : "选择一条 Trace，查看上下文、响应、工具调用及校验结果。")); return panel; }
  const heading = node("div", "trace-detail-heading"); const title = node("div"); append(title, node("span", "section-label", "TRACE DETAIL / 脱敏记录"), node("h2", "", trace.name || "Trace"), node("code", "trace-id", trace.trace_id));
  append(heading, title, traceStatus(trace.status, !trace.open_span_count && Boolean(trace.end_time))); panel.append(heading);
  panel.append(node("p", "trace-meta", `${traceTimestamp(trace.start_time)} → ${trace.end_time ? traceTimestamp(trace.end_time) : "尚无结束事件"} · ${traceDuration(trace.duration_ms)}`));
  const findings = renderTraceFindings(trace); if (findings) panel.append(findings);
  const actions = node("div", "trace-detail-actions");
  const download = node("a", "button button-small", "导出 Trace JSON ↓"); download.href = `/api/traces/${encodeURIComponent(trace.trace_id)}/export`; download.download = `trace-${trace.trace_id}.json`;
  const verify = button(tracing.verifying ? "正在核对…" : "核对记录完整性", async () => {
    const id = trace.trace_id; tracing.verifying = true; tracing.verification = null; renderStage();
    try { const result = await api.verifyTrace(id); if (tracing.id === id) tracing.verification = result; }
    catch (error) { if (tracing.id === id) tracing.verification = { ok: false, issues: [error.message] }; }
    finally { tracing.verifying = false; if (state.step === "traces") renderStage(); }
  }, { small: true, disabled: tracing.verifying }); append(actions, download, verify); panel.append(actions);
  if (trace.name === "collaboration.commission.lifecycle") {
    const correlation = trace.attributes?.correlation_id;
    if (validTraceId(correlation)) {
      const overview = node("a", "button button-small", "查看协作任务 ↗");
      overview.href = "/collaborations#" + encodeURIComponent(correlation);
      actions.append(overview);
    }
    const root = (trace.spans || []).find((span) => !span.parent_span_id);
    const stages = new Map();
    const seen = (trace.events || []).filter((item) => item.name === "collaboration.stage").map((item) => item.data);
    for (const stage of [...seen, ...(Array.isArray(root?.output?.stages) ? root.output.stages : [])]) {
      if (validTraceId(stage?.trace_id) && stage.trace_id !== trace.trace_id) stages.set(stage.trace_id, stage);
    }
    if (stages.size) {
      const links = node("div", "trace-links");
      links.setAttribute("aria-label", "各 Agent 阶段 Trace");
      for (const stage of stages.values()) {
        const label = [stage.revision, stage.name || "协作阶段"].filter(Boolean).join(" · ");
        const link = node("a", "button button-small", label + " ↗");
        link.href = "/#step=traces&trace=" + encodeURIComponent(stage.trace_id);
        links.append(link);
      }
      panel.append(links);
    }
  }
  if (tracing.verification) {
    const result = tracing.verification; const box = node("div", `trace-integrity ${result.ok ? "pass" : "fail"}`);
    append(box, node("strong", "", result.ok ? "本地记录完整性核对通过" : "完整性核对发现问题"), node("p", "trace-meta", `检查了 ${result.checked_events || 0} 条事件、${result.checked_artifacts || 0} 份独立内容。核对本地记录与哈希，不代表供应商独立签名。`));
    if (result.issues?.length) box.append(traceValue("核对问题", result.issues, { open: true })); panel.append(box);
  }
  if (trace.events_has_more) {
    const more = node("div", "trace-more-events");
    append(more, node("span", "trace-meta", `已载入 ${(trace.events || []).length} 条事件，仍有后续记录。调用概要包含全部 span。`), button(tracing.eventsLoading ? "正在加载…" : "加载后续事件", () => loadMoreTraceEvents(), { small: true, disabled: tracing.eventsLoading })); panel.append(more);
  }
  if (tracing.eventsError) panel.append(note(tracing.eventsError, true));
  if (trace.links?.length) {
    const links = node("div", "trace-links"); trace.links.forEach((link) => { if (validTraceId(link.trace_id)) links.append(button(`${link.relation || "关联 Trace"} ↗ ${link.trace_id.slice(0, 8)}`, () => selectTrace(link.trace_id), { small: true, quiet: true })); }); panel.append(links);
  }
  panel.append(traceValue("Trace 属性 / 关联任务", trace.attributes));
  const tabs = node("nav", "trace-view-tabs"); tabs.setAttribute("aria-label", "Trace 详情视图");
  [["spans", "调用层级与输入输出"], ["events", "全部事件时间线"]].forEach(([view, label]) => { const item = button(label, () => { tracing.view = view; renderStage(); }, { quiet: true }); item.setAttribute("aria-pressed", String(tracing.view === view)); item.classList.toggle("selected", tracing.view === view); tabs.append(item); }); panel.append(tabs);
  const spans = Array.isArray(trace.spans) ? trace.spans : [];
  if (tracing.view === "events") panel.append(renderTraceEvents(Array.isArray(trace.events) ? trace.events : []));
  else {
    panel.append(renderSpanTree(spans));
    const selected = spans.find((span) => span.span_id === tracing.spanId);
    if (selected) {
      const span = node("section", "trace-selected-span"); const head = node("div", "trace-pane-heading"); append(head, node("h3", "", selected.name || selected.span_id), traceStatus(selected.status, selected.end_recorded)); span.append(head);
      const verdict = traceProblemIds(spans, trace).get(selected.span_id);
      if (selected.status === "OK" && (verdict === "FAIL" || verdict === "UNKNOWN")) {
        span.append(note(verdict === "UNKNOWN" ? "此调用已完成，自检结论为证据不足。" : "此调用已完成，自检发现了未通过的检查项。"));
      }
      append(span, node("code", "trace-id", `span ${selected.span_id} · parent ${selected.parent_span_id || "根调用"}`), node("p", "trace-meta", `${traceTimestamp(selected.start_time)} → ${selected.end_time ? traceTimestamp(selected.end_time) : "尚无结束事件"} · ${traceDuration(selected.duration_ms)}`));
      if (selected.error) span.append(traceValue("错误与调用栈", selected.error, { open: true }));
      const expandPayload = tracing.showAllSpans || (!trace.asset_diagnostics?.items?.length && trace.status !== "ERROR");
      append(span, traceValue("输入 / 模型上下文", selected.inputs, {open:expandPayload}), traceValue("输出 / 模型响应", selected.output, {open:expandPayload && selected.output !== undefined}), traceValue("调用属性", selected.attributes));
      span.append(selectedSpanEvents(trace, selected)); panel.append(span);
    } else panel.append(node("p", "trace-empty", "当前 Trace 尚无可展示的 span。事件时间线会保留已经记录的事件。"));
  }
  return panel;
}
function renderTraces() {
  const container = node("div", "trace-workbench"); container.append(renderTraceFilters());
  const explanation = node("div", "trace-explainer");
  append(explanation, node("p", "", "默认查看任务 Trace。切换到 HTTP 请求可查看界面请求；‘全部类型’包含独立程序与工具调用。点开任一调用，逐层展开输入、上下文、响应及校验数据。"), node("small", "", "未结束表示没有记录到结束事件，不能据此判定成功或失败。历史任务不补造 Trace。时间以北京时间显示。")); container.append(explanation);
  const layout = node("div", "trace-layout"); append(layout, renderTraceList(), renderTraceDetail()); container.append(layout);
  return container;
}

function renderProjectOpening({ summary, error }) {
  const panel = node("section", "panel studio-inspector");
  panel.dataset.projectOpening = summary.id;
  const phase = currentPhase();
  panel.append(node("h2", "", STEP_NAMES[phase]));
  const opening = summary.opening;
  const view = studioLocal(summary.id);
  if (phase === "video") {
    const monitor = studioCurrentMonitor(summary.id, view);
    if (monitor) { panel.append(monitor.element); void monitor.refresh(); }
  }
  const selected = phase === "assets" && opening?.entities?.find(entity => entity.id === view.subjectId);
  if (phase === "assets" && opening?.entities?.length) {
    const list = node("div", "studio-actions");
    for (const entity of opening.entities) {
      const isSelected = selected?.id === entity.id;
      const label = [studioKinds[entity.kind] || entity.kind, entity.name].filter(Boolean).join(" · ");
      const choice = button(label, () => studioSelectSubject(view, entity.id, null),
        { small: true, primary: isSelected, quiet: !isSelected });
      choice.dataset.projectEntity = entity.id;
      choice.setAttribute("aria-pressed", String(isSelected));
      list.append(choice);
    }
    append(panel, node("h3", "", "剧本对象"), list);
    if (selected) {
      const detail = node("section", "studio-detail-panel");
      detail.id = "studio-asset-detail";
      append(detail, node("h3", "", selected.name), node("code", "studio-object-id", selected.id),
        selected.description && node("p", "studio-canonical", selected.description));
      panel.append(detail);
    }
  }
  const shots = opening?.shots || [];
  const filterByEntity = selected && shots.some(shot => Array.isArray(shot.entity_ids) || shot.scene_id);
  if (shots.length) {
    const heading = node("div", "studio-board-head");
    heading.append(node("h3", "", filterByEntity ? "关联分镜" : "剧本分镜"));
    if (filterByEntity) heading.append(button("查看全部分镜", () => studioSelectSubject(view, null, null), { small: true, quiet: true }));
    panel.append(heading);
    const list = node("div", "studio-shot-list");
    for (const shot of shots) {
      const relatedIds = new Set([shot.scene_id, ...(shot.entity_ids || [])].filter(Boolean));
      if (filterByEntity && !relatedIds.has(selected.id)) continue;
      const shotDetail = node("details", "studio-shot");
      shotDetail.dataset.projectShot = shot.id;
      const row = node("summary", "studio-shot-summary");
      append(row, node("code", "", shot.id), node("span", "", shot.description),
        Number.isFinite(shot.duration_s) && node("small", "", shot.duration_s + " 秒"));
      const related = opening.entities?.filter(entity => relatedIds.has(entity.id)).map(entity => entity.name) || [];
      append(shotDetail, row, node("p", "studio-shots", related.length ? "剧本对象　" + related.join("、") : "暂无对象关联记录"));
      list.append(shotDetail);
    }
    panel.append(list.children.length ? list : node("p", "small-print", "剧本中暂无关联分镜。"));
  }
  if (error) {
    append(panel, note("未能读取制作详情：" + error, true),
      button("重新读取详情", () => openProject(summary.id, { preserveStep: true,full:state.projectOpening?.mode==="workspace" }), { small: true }));
    if (state.projectOpening?.mode && state.projectOpening.mode!=="workspace")
      panel.append(button("打开完整工程",()=>openProject(summary.id,{preserveStep:true,full:true}),{small:true}));
  } else {
    const status = node("p", "small-print", "正在读取制作详情…");
    status.setAttribute("role", "status"); panel.append(status);
  }
  return panel;
}

function restoreVideoPlayback(video, previousVideos) {
  const src=video.getAttribute("src");
  if (!src) return;
  const previous=previousVideos.find(entry=>entry.src===src && entry.takeId===video.dataset.takeId);
  if (!previous) return;
  if (previous.element!==video && Number.isFinite(previous.time) && previous.time>0) {
    const seek=()=>{ video.currentTime=previous.time; };
    if (video.readyState>=1) seek();
    else video.addEventListener("loadedmetadata",seek,{once:true});
  }
  // A paused take with preload="none" stays idle. A playing take must start
  // the request itself before its loadedmetadata handler can restore the time.
  if (!previous.paused && video.paused) video.play().catch(()=>{});
}

function renderStage({ preserveAgentEditing = false } = {}) {
  // Polling must not detach a composing textarea or an open native select.
  // Manual navigation still renders immediately; the activity rail refreshes independently.
  const active = document.activeElement;
  if(preserveAgentEditing && active?.closest?.("#agent-chat"))return;
  if (preserveAgentEditing && state.step === "studio" && typeof studioAssetInteractionActive === "function"
      && studioAssetInteractionActive()) return;
  const stage = $("stage"); stage.setAttribute("aria-busy", String(state.loading && !state.projectOpening));
  const scope=!state.creating && state.step!=="traces" ? state.storyDraftId || state.projectId : null;
  const phase=scope ? currentPhase() : state.step;
  if (stage.dataset.projectScope!==String(scope) || stage.dataset.projectPhase!==phase) {
    // Detached <img> nodes may finish downloading. Release old gallery sources explicitly on navigation.
    for (const img of stage.querySelectorAll("img")) if (!img.complete) img.removeAttribute("src");
    if (stage.dataset.projectPhase==="video" && typeof studioViews!=="undefined") {
      const view=studioViews.get(stage.dataset.projectScope);
      if (view?.monitor) { view.monitor.cancel(); view.monitor=null; }
    }
  }
  if (stage.dataset.projectScope!==String(scope) || !scope || state.projectOpening) {
    if (typeof agentChatUnmount==="function") agentChatUnmount();
    if (stage.dataset.projectScope && stage.dataset.projectScope!==String(scope) && typeof studioViews!=="undefined")
      studioViews.get(stage.dataset.projectScope)?.readController?.abort();
  }
  stage.dataset.projectScope=String(scope);stage.dataset.projectPhase=phase;
  if (state.projectOpening) {
    stage.replaceChildren(renderProjectOpening(state.projectOpening));
    return;
  }
  // Preserve locally edited text and playing media across unrelated task updates.
  const activeId = document.activeElement?.id;
  const cursor = document.activeElement?.selectionStart;
  const openDetails = [...stage.querySelectorAll("details")].map((element) => element.open);
  const videos = [...stage.querySelectorAll("video")].map(video=>({element:video,src:video.getAttribute("src"),
    takeId:video.dataset.takeId,time:video.currentTime,paused:video.paused}));
  stage.replaceChildren();
  if (state.loading) { const loading = node("div", "loading-state"); append(loading, node("span", "spinner"), node("p", "", "正在打开工程")); stage.append(loading); return; }
  if (state.step === "traces") stage.append(renderTraces());
  else if (state.creating && state.step === "story") stage.append(renderCreate());
  else if (state.creating) stage.append(state.step === "story" ? renderCreate() : emptyState("先写下你的故事", "生成剧本与分镜后，就可以继续制作镜头、检查素材和导出预览。", button("返回创意", () => setStep("story"), { primary: true })));
  else if (state.storyDraftId) {
    stage.append(renderStoryDraft());
  }
  else if (!state.project) stage.append(emptyState("制作工作空间已准备好", "写下一段创意，开始制作你的第一部短剧。", button("新建故事", startNew, { primary: true })));
  else if(state.step==="story"){
    const reading=renderStory(),data=studioLocal(state.projectId).data;
    if(data)reading.append(studioDetails("发布与交付记录",studioScript(data,studioCatalog(data))));
    stage.append(reading);
  }else stage.append(renderStudio());
  if (!state.creating && state.step!=="traces" && typeof renderAgentChat==="function") {
    const scope=state.storyDraftId?{story:state.storyDraftId}:state.projectId?{project:state.projectId}:null;
    if(scope){
      const phase=currentPhase(),focusConversation=phase==="video"&&studioLocal(state.projectId).tool==="conversation";
      stage.append(renderAgentChat(scope,{phase,preferredRole:["video","review"].includes(phase)?"video":phase==="story"?"script":null,selectRole:focusConversation}));
      if(focusConversation){studioLocal(state.projectId).tool=null;queueMicrotask(()=>{$("agent-chat-input")?.focus();$("agent-chat")?.scrollIntoView?.({block:"start"});});}
    }
  }
  if (state.step !== "studio") [...stage.querySelectorAll("details")].forEach((element, index) => { if (openDetails[index] !== undefined) element.open = openDetails[index]; });
  if (activeId && $(activeId)) { const focus = $(activeId); focus.focus({ preventScroll: true }); if (typeof cursor === "number" && focus.setSelectionRange) focus.setSelectionRange(cursor, cursor); }
  stage.querySelectorAll("video").forEach(video=>restoreVideoPlayback(video,videos));
  markStartupReady();
}
async function openProducedStory(id) {
  if (typeof studioLocal === "function") studioLocal(id).section = "assets";
  await openProject(id);
}
async function openProject(id, { quiet = false, preserveStep = false, preserveAgentEditing = false, full = false } = {}) {
  if (quiet && state.projectOpening) return;
  if (quiet && state.project?.projection==="video") return studioRefresh(id,true);
  const keepVideo=full && state.project?.id===id && state.project.projection==="video" && currentPhase()==="video";
  if(keepVideo && studioLocal(id).archiveLoading)return;
  state.projectRead?.abort();
  const prefetch=state.initialVideoRead;
  state.initialVideoRead=null;
  const token = ++state.projectLoad;
  if (!quiet && typeof studioLocal === "function") {
    const view = studioLocal(id);
    view.readController?.abort();view.token++;view.loading=false;view.archiveLoading=keepVideo;view.archiveError=null;
    if(!keepVideo){
      view.monitor?.cancel();view.data=null;view.error=null;view.monitor=null;view.videoReadMode=null;
    }
  }
  if (keepVideo) renderStage();
  else if (!quiet) {
    const summary = state.projects.find(project => project.id === id) || { id, title: "故事工程" };
    state.storyDraftId = null; state.storyDraft = null; state.storyDraftError = null;
    state.projectId = id; state.project = null; state.projectRevision = ""; state.projectOpening = { summary, error: null };
    state.creating = false; state.loading = true;
    if (!preserveStep) { state.step = "studio"; state.shotId = null; }
    state.projectOpening.mode=full?"workspace":videoReadMode(id);
    renderProjects(); renderHeader(); renderJobs(); renderStage(); persistRoute();
  }
  const mode=state.projectOpening?.mode || "workspace";
  const reuse=prefetch && prefetch.id===id && mode==="video";
  if(!reuse)prefetch?.controller?.abort();
  const controller=reuse?prefetch.controller:(typeof AbortController==="function"?new AbortController():null);
  state.projectRead=controller;
  // Both reads are independent snapshots; do not serialize the production desk
  // behind compilation of the shot prompts.
  const snapshotRead=!quiet&&state.config?.workspace_snapshot===true;
  if(!quiet&&!snapshotRead&&typeof studioRefresh==="function")void studioRefresh(id,true);
  try {
    if(mode!=="workspace"){
      const view=studioLocal(id);
      const result=mode==="studio" ? await request(studioPath(id),{signal:controller?.signal}) :
        await (reuse?prefetch.promise:request(studioPath(id)+"/video-detail",{signal:controller?.signal}));
      if(token!==state.projectLoad)return;
      if(result.project_id!==id || (mode==="video" && result.projection!=="video"))throw new Error("制作详情与当前故事不一致，请重新读取。");
      const summary=state.projectOpening.summary;
      if(mode==="video")applyVideoJobs(id,result);
      view.videoReadMode=mode; view.data=result; view.lastRead=Date.now();
      state.project={id,title:summary.title,projection:"video"};
      state.projectOpening=null;state.loading=false;
      renderProjects(); renderHeader(); renderJobs(); renderStage(); persistRoute();
      return;
    }
    const snapshot=snapshotRead?await api.workspace(id,controller?.signal):null;
    const project = snapshot?.project || await api.project(id,controller?.signal);
    if (token !== state.projectLoad) return;
    if (quiet && (state.creating || id !== state.projectId)) return;
    if(snapshot){
      applyBootstrap(snapshot);state.jobs=snapshot.jobs;state.jobsReady=true;
      const view=studioLocal(id);view.data=snapshot.studio;view.loading=false;view.lastRead=Date.now();view.videoReadMode=null;view.archiveLoading=false;
      if(typeof primeAgentChat==="function")primeAgentChat({project:id},snapshot.conversations);
    }
    const revision = JSON.stringify(project);
    const changed = state.projectRevision !== revision;
    state.projectOpening = null;
    state.project = project; state.projectId = id; state.creating = false; state.loading = false; state.projectRevision = revision;
    if (!shots().some((shot) => shot.id === state.shotId)) state.shotId = shots()[0]?.id || null;
    renderProjects(); renderHeader(); renderJobs(); if (changed || !quiet) renderStage({ preserveAgentEditing: quiet && preserveAgentEditing }); persistRoute();
  } catch (error) {
    if (token !== state.projectLoad) return;
    state.loading = false;
    if(keepVideo){
      const view=studioLocal(id);view.archiveLoading=false;view.archiveError=error.message;
      renderStage();
    } else if (!quiet && state.projectOpening) {
      state.projectOpening.error = error.message || "读取失败。";
      renderHeader(); renderStage();
    } else if (!quiet) {
      toast(error.message, true);
    }
  } finally { if (state.projectRead===controller) state.projectRead=null; }
}
function acceptJob(job, announcement) {
  if (!job?.id) throw new Error("任务响应缺少记录，请先检查后台任务，勿立即重复提交。");
  state.jobs = [job, ...state.jobs.filter((existing) => existing.id !== job.id)]; renderJobs(); announce(announcement || `${jobTitle(job)}已提交`);
}
async function performAction(action, body, message) {
  const id = state.projectId; if (!id || busyProject()) return;
  state.pending.add(action); renderStage();
  try {
    const result = await api.action(id, action, body);
    if (result.project && id === state.projectId) {
      state.project = result.project; state.projectRevision = JSON.stringify(result.project); renderHeader();
    } else acceptJob(result.job);
    toast(message);
  } catch (error) { toast(error.message, true); }
  finally { state.pending.delete(action); if (id === state.projectId && !state.creating) renderStage(); }
}
function connection(online, message) {
  state.online = online; const banner = $("connection-banner"); banner.hidden = online; banner.textContent = message || "暂时无法连接本机服务；已显示的工程仍可阅读。恢复后将自动更新任务。";
}
function startupMetric(name){
  const root=document.documentElement;
  if(!root||typeof performance==="undefined")return;
  const key="startup"+name+"Ms";
  if(root.dataset[key]!==undefined)return;
  const measure=()=>{
    if(root.dataset[key]!==undefined)return;
    root.dataset[key]=performance.now().toFixed(1);
    if(name==="Ready")root.dataset.startupResources=JSON.stringify(performance.getEntriesByType("resource").map(entry=>({
      path:new URL(entry.name).pathname,start_ms:Math.round(entry.startTime),end_ms:Math.round(entry.responseEnd),duration_ms:Math.round(entry.duration)
    })));
  };
  if(typeof window.requestAnimationFrame==="function")window.requestAnimationFrame(measure);else measure();
}
function markStartupReady(){
  if(state.loading||!state.config||state.jobsReady===false||state.step==="traces")return;
  if(typeof studioLocal!=="function")return;
  if(!state.creating){
    if(state.projectId&&!state.storyDraftId&&!studioLocal(state.projectId).data)return;
    if(!state.project&&!state.storyDraft)return;
    if(typeof agentChatCurrent!=="undefined"&&(!agentChatCurrent||!agentChatCurrent.view.data))return;
  }
  startupMetric("Ready");
}
async function poll() {
  void refreshFeedbackJobs();
  if (!state.config && state.loading) return;
  if (state.projectOpening && state.loading) {
    if (currentPhase() === "video") void studioLocal(state.projectId).monitor?.refresh();
    return;
  }
  if(typeof agentChatPoll==="function")void agentChatPoll();
  if (!state.creating && !state.storyDraftId && currentPhase()==="video" && state.project?.projection==="video" && studioLocal(state.projectId).videoReadMode==="video") {
    const view=studioLocal(state.projectId);
    void view.monitor?.refresh();
    if (!view.archiveLoading && !view.pending && !view.loading && Date.now()-view.lastRead>=5000) void studioRefresh(state.projectId,true);
    return;
  }
  if (state.pollBusy) return;
  state.pollBusy = true;
  const projectLoad = state.projectLoad;
  try {
    const recover = !state.online || !state.config;
    const result = await api.jobs(); connection(true);
    if (projectLoad !== state.projectLoad) return;
    const previous = new Map(state.jobs.map((job) => [job.id, job.status]));
    state.jobs = Array.isArray(result.jobs) ? result.jobs : [];state.jobsReady=true;
    renderJobs();
    if (state.projectOpening) return;
    if (state.step === "studio" && !state.creating && state.projectId && typeof studioCurrentMonitor === "function") {
      const view = studioLocal(state.projectId);
      if (view.section === "video") {
        const previous = view.monitor;
        const monitor = studioCurrentMonitor(state.projectId,view);
        if (monitor && monitor !== previous && !state.loading) studioRedraw(state.projectId);
        if (monitor) void monitor.refresh();
      }
    }
    const changed = state.jobs.filter((job) => previous.get(job.id) !== job.status);
    const finished = changed.filter((job) => ["succeeded", "failed", "interrupted"].includes(job.status) && previous.has(job.id));
    if (typeof studioRefresh === "function" && state.step === "studio" && finished.some(job => jobProjectId(job) === state.projectId)) {
      if (studioLocal(state.projectId).section === "assets") studioPollAssets(true);
      else void studioRefresh(state.projectId);
    }
    else if (typeof studioPollAssets === "function") studioPollAssets();
    const relevantFinished = visibleJobs(finished);
    relevantFinished.forEach((job) => {
      if (job.kind === "agent") toast(`${jobTitle(job)}：${agentResultLabel(job)}。${typeof job.result?.summary === "string" ? job.result.summary : job.error || job.message || "可查看执行报告。"}`, job.result?.task_complete !== true);
      else toast(job.status === "succeeded" ? `${jobTitle(job)}完成，结果已更新。` : errorText(job.error || job.message || "后台任务未完成。"), job.status !== "succeeded");
    });
    if (state.step === "traces") {
      if (changed.length || recover) {
        const bootstrap = await api.bootstrap();
        if (projectLoad !== state.projectLoad) return;
        applyBootstrap(bootstrap); renderProjects(); renderConfig();
      }
      if (state.trace.autoRefresh && (!state.trace.refreshedAt || Date.now() - state.trace.refreshedAt > 5000)) await refreshTraces();
      return;
    }
    if (changed.length || recover) {
      const bootstrap = await api.bootstrap();
      if (projectLoad !== state.projectLoad) return;
      applyBootstrap(bootstrap); renderProjects(); renderConfig();
      if (state.storyDraftId && !state.creating) {
        const selectedJob = state.jobs.find((job) => job.id === state.storyDraftId);
        const selectedProject = selectedJob && jobProjectId(selectedJob);
        if (selectedProject && state.projects.some((project) => project.id === selectedProject)) await openProducedStory(selectedProject);
        else await openStoryDraft(state.storyDraftId, { quiet: true });
      }
      else if (state.projectId && !state.creating) await openProject(state.projectId, { quiet: true, preserveStep: true, preserveAgentEditing: true });
      else if (!state.projectId && !state.creating && state.projects.length) await openProject(state.projects[0].id);
      else renderStage({ preserveAgentEditing: true });
    } else if (state.storyDraftId && !state.creating && ACTIVE.has(state.jobs.find((job) => job.id === state.storyDraftId)?.status)
        && Date.now() - state.storyDraftRefreshedAt > 5000) {
      await openStoryDraft(state.storyDraftId, { quiet: true });
    }
  } catch (error) { connection(false, error.message); }
  finally { state.pollBusy = false;markStartupReady(); }
}
async function initialize() {
  $("new-project").addEventListener("click", startNew);
  $("open-traces").addEventListener("click", () => openTraceDesk());
  $("guide-toggle").addEventListener("click", () => { const guide = $("quick-guide"); guide.hidden = !guide.hidden; $("guide-toggle").setAttribute("aria-expanded", String(!guide.hidden)); });
  document.querySelectorAll("[data-step]").forEach((tab) => tab.addEventListener("click", () => setStep(tab.dataset.step)));
  // Some history traversals emit both events; coalesce them and keep an in-flight
  // route guard so an unchanged address never recreates editable fields.
  let routeQueued = false;
  const onLocationChange = () => {
    if (routeQueued) return;
    routeQueued = true;
    queueMicrotask(() => { routeQueued = false; void applyLocationRoute(); });
  };
  window.addEventListener("hashchange", onLocationChange);
  window.addEventListener("popstate", onLocationChange);
  $("workflow-tabs").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...document.querySelectorAll("[data-step]")]; const current = tabs.indexOf(document.activeElement); if (current < 0) return;
    event.preventDefault(); const index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length; tabs[index].focus(); tabs[index].click();
  });
  setInterval(poll, 2000);
  try {
    const route=new URLSearchParams(location.hash.slice(1)), id=route.get("project");
    if(document.documentElement?.dataset?.videoDetail==="true" && /^[a-f0-9]{16}$/.test(id||"") && !route.has("story")) {
      const target=productionRoute(route.get("step"),route.get("section"),route.get("tool"));
      if(target.section==="video" && !["checks","shots"].includes(target.tool)) {
        const controller=new AbortController();
        const promise=request(studioPath(id)+"/video-detail",{signal:controller.signal});
        promise.catch(()=>{}); // The route consumes the original result after bootstrap.
        state.initialVideoRead={id,controller,promise};
      }
    }
    const sidebar = await api.bootstrap();
    applyBootstrap(sidebar);
    state.config ||= {};
    state.jobsReady = Array.isArray(sidebar.jobs);
    if (state.jobsReady) state.jobs = sidebar.jobs;
    state.loading = false;
    renderConfig(); renderProjects(); renderJobs(); startupMetric("Sidebar");
    const opening = applyLocationRoute();
    const summary = state.projectOpening?.summary;
    const hasSnapshot = summary && state.config.workspace_snapshot===true;
    const needsVideoJob = summary && currentPhase()==="video" && !Object.hasOwn(summary.opening || {}, "video_job");
    const mode=state.projectOpening?.mode;
    if (!state.jobsReady && (!hasSnapshot || mode==="studio" || (needsVideoJob && mode!=="video"))) {
      void api.jobs().then(result => {
        if (state.jobsReady || !Array.isArray(result.jobs)) return;
        state.jobs = result.jobs; state.jobsReady = true; renderJobs();
        if (!state.loading || (state.projectOpening && currentPhase()==="video")) renderStage({preserveAgentEditing:true});
        markStartupReady();
      }).catch(error => connection(false, error.message));
    }
    await opening;
    connection(true);
  } catch (error) { state.loading = false; renderProjects(); renderHeader(); renderStage(); connection(false, error.message); }
  finally { state.initialVideoRead?.controller.abort(); state.initialVideoRead=null; }
}
initialize();
