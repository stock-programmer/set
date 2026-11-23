"use strict";

const studioSteps = [
  ["script", "剧本与分镜", "创意 → 剧本与分镜"],
  ["assets", "共享资产", "制作 → 自检 → 自动选用"],
  ["video", "视频制作", "拍摄 → 自动验收 → 修复"],
  ["review", "人工预览", "整片验收通过后预览"],
];
const studioKinds = { character: "人物", scene: "场景", prop: "道具", costume: "服装" };
const studioViews = new Map();
const assetStates = {planned:"待制作",generating:"制作与自检中",needs_attention:"需处理",awaiting_review:"等待自动选用",approved:"已选用"};

function studioLocal(id) {
  if (!studioViews.has(id)) studioViews.set(id, {
    section: "assets", tool:null, subjectId: null, purpose: null, pinKey: null,
    data: null, error: null, loading: false, token: 0, pending: false, message: "", lastTrace: null,
    assetFeedback:new Map(), assetStyle:null, lastRead:0, refreshDue:false,
  });
  return studioViews.get(id);
}
function studioPath(id) { return "/api/projects/" + encodeURIComponent(id) + "/studio"; }
function studioRedraw(id) {
  if(state.projectId===id && !state.loading && typeof renderHeader==="function")renderHeader();
  if (state.step==="studio" && state.projectId===id && studioLocal(id).section==="video" && studioAssetInteractionActive()) {
    studioLocal(id).refreshDue=true;
    return;
  }
  if (["studio","story"].includes(state.step) && state.projectId === id && !state.loading && !state.creating) renderStage({preserveAgentEditing:true});
}
async function studioRefresh(id, quiet=false) {
  const view = studioLocal(id), videoDetail = view.videoReadMode === "video";
  if (quiet && videoDetail && (view.loading || view.videoPollToken === view.token)) return;
  const background = quiet && videoDetail && view.data?.projection === "video" &&
    view.data.project_id === id && !view.error;
  const token = ++view.token;
  view.readController?.abort();
  const controller=typeof AbortController==="function"?new AbortController():null;
  view.readController=controller;
  const previousError=view.error;
  let changed = true;
  view.videoPollToken = background ? token : null; view.loading = !background; view.error = null;
  try {
    const data = await request(studioPath(id)+(videoDetail?"/video-detail":""),{signal:controller?.signal});
    if (view.token === token) {
      if(videoDetail){
        if(data.project_id!==id || data.projection!=="video")throw new Error("制作详情与当前故事不一致，请重新读取。");
        if(state.projectId===id)applyVideoJobs(id,data);
      }
      changed = !!previousError || !quiet || JSON.stringify(view.data) !== JSON.stringify(data);
      view.data = data;
    }
  } catch (error) {
    if (view.token === token && !controller?.signal.aborted) { view.error = error.message; }
  } finally {
    if (view.readController===controller) view.readController=null;
    if (view.token === token) {
      view.videoPollToken = null; view.loading = false; view.lastRead = Date.now();
      if (quiet && studioAssetInteractionActive()) view.refreshDue=true;
      else if (changed) studioRedraw(id);
      if (changed && state.projectId === id && typeof renderJobs === "function") renderJobs();
    }
  }
}
function studioAssetInteractionActive() {
  const focused = document.activeElement;
  return (focused?.matches?.("textarea,input,select") && focused.closest?.(".studio-inspector")) ||
    [...(document.querySelectorAll?.(".studio-inspector audio, .studio-take video") || [])].some(media => !media.paused);
}
function studioPollAssets(force=false) {
  if (state.step !== "studio" || state.creating || !state.projectId) return;
  const view = studioLocal(state.projectId);
  if (["review", "video"].includes(view.section)) {
    if (view.refreshDue && !view.loading && !view.pending && !studioAssetInteractionActive()) {
      view.refreshDue=false;
      studioRedraw(state.projectId);
    }
    if (!view.pending && !view.loading && Date.now() - view.lastRead >= 5000 &&
        visibleJobs().some(job => ACTIVE.has(job.status) && ["studio_review", "studio_video"].includes(job.kind))) void studioRefresh(state.projectId,true);
    return;
  }
  if (view.section !== "assets") return;
  if (force) view.refreshDue=true;
  if (view.pending || view.loading || studioAssetInteractionActive()) return;
  if (view.refreshDue) { view.refreshDue=false; void studioRefresh(state.projectId); return; }
  if (Date.now() - view.lastRead < 5000) return;
  const work = view.data?.asset_work;
  const running = work?.jobs?.some(job => ACTIVE.has(job.status))
    || visibleJobs().some(job => ACTIVE.has(job.status) && (job.kind === "studio_reference" || job.kind?.startsWith("asset_")));
  if (running || !view.data || (work?.auto_dispatch?.status !== "blocked" &&
      work?.items?.some(item => ["planned","generating","awaiting_review"].includes(item.status)))) void studioRefresh(state.projectId,true);
}
function studioPin(ref) { return ref?.revision_id ? ref.asset_id + " @ " + ref.revision_id : ""; }
function studioLink(href, label) {
  const link = node("a", "studio-link", label); link.href = href; return link;
}
function studioTrace(id, label = "打开 Trace ↗") {
  return validTraceId(id) ? studioLink("/#step=traces&trace=" + encodeURIComponent(id), label) : null;
}
function studioMediaUrl(value, pid) {
  if (!value) return null;
  if (assetUrl(value)) return assetUrl(value);
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && url.pathname === studioPath(pid) + "/media" ? url.pathname + url.search : null;
  } catch { return null; }
}
function studioImportForm(submitScript, disabled) {
  const box = node("div", "studio-import");
  const file = node("input"); file.type = "file"; file.accept = ".json,application/json";
  const send = button("导入并编译分镜", async () => {
    const chosen = file.files?.[0]; if (!chosen || disabled) return;
    try {
      if (chosen.size > 900 * 1024) throw new Error("请选择小于 900 KiB 的剧本 JSON 文件。");
      const text = await chosen.text();
      let script;
      try { script = JSON.parse(text.replace(/^\uFEFF/,"")); } catch { throw new Error("文件不是有效 JSON，请检查后再导入。"); }
      if (script?.schema_version !== "screenplay/1" || typeof script.title !== "string") throw new Error("剧本须包含 schema_version=screenplay/1 及标题。");
      await submitScript({script});
    } catch (error) { toast(error.message, true); }
  }, {small:true, disabled:true});
  file.disabled = disabled; file.addEventListener("change", () => { send.disabled = disabled || !file.files?.length; });
  append(box, formField("studio-import-script","已有剧本文件",file,"选择 screenplay/1 JSON。剧本 Agent 会检查版本并编译分镜，可能产生文本模型费用。"),send);
  return studioDetails("从已有剧本开始",box);
}
function studioRows(rows) {
  const list = node("dl", "studio-rows");
  for (const [label, value] of rows) {
    const line = node("div");
    const dd = node("dd");
    if (typeof value === "string" || typeof value === "number") dd.textContent = String(value); else if (value) dd.append(value);
    append(line, node("dt", "", label), dd); list.append(line);
  }
  return list;
}
function studioSection(title, subtitle) {
  const head = node("div", "studio-section-head");
  append(head, node("h2", "", title), subtitle ? node("p", "", subtitle) : null);
  return head;
}
function studioDetails(title, content) {
  const d = node("details", "studio-details");
  append(d, node("summary", "", title), content);
  return d;
}
function studioSources(refs) {
  if (!refs?.length) return null;
  const list = node("ul", "studio-sources");
  for (const ref of refs) list.append(node("li", "", [ref.source_id, ref.quote].filter(Boolean).join("　")));
  return studioDetails("剧本来源 · " + refs.length, list);
}
function studioOptions(items, value, change, label) {
  const select = node("select", "studio-select"); select.setAttribute("aria-label", label);
  items.forEach(([id, title]) => { const opt = node("option", "", title); opt.value = id; select.append(opt); });
  select.value = value || items[0]?.[0] || "";
  if (change) select.addEventListener("change", () => change(select.value));
  return select;
}
function studioCatalog(data) { return data.catalog || data.assets || data; }
function studioProduction(data) {
  return data.productions?.[0] || null;
}
function studioScript(data, catalog) {
  const box = node("div", "studio-inspector"), project = state.project;
  const job = visibleJobs().find(isStoryJob), script = data.script || {};
  append(box, studioSection("剧本与分镜交付", project.logline || project.title),
    studioRows([["输入", script.idea || job?.story_input?.idea || "当前工程的剧本来源与创作记录"],
      ["输出", studioPin(script.script_ref || catalog.script_ref) || (project.script_text ? "已保存剧本 · 尚无发布版本记录" : "已有分镜工程 · 缺少剧本发布记录")],
      ["保存位置", script.path || catalog.project_path], ["下游使用", "资产库读取人物、场景和道具设定，视频制作 Agent 读取节拍和镜头规格。"]]));
  append(box, storyStructureDetails(script.structure));
  if (script.beats?.length) {
    const table = node("div", "studio-shot-list");
    for (const beat of script.beats) {
      const row = node("div","studio-shot-summary");
      append(row,node("code","",beat.id),node("span","",beat.text),node("small","",beat.shot_ids?.join("、") || "待对应镜头")); table.append(row);
    }
    box.append(studioDetails("节拍与镜头 · " + script.beats.length,table));
  }
  const actions = node("div", "studio-actions");
  if (assetUrl(script.url)) actions.append(studioLink(assetUrl(script.url), "查看已发布剧本 " + script.revision_id + " ↗"));
  append(actions, button("阅读工程原稿", () => setStep("story"), {small:true}), studioTrace(script.trace_id || job?.trace_id, "创作 Trace ↗"));
  box.append(actions); return box;
}
function studioSelectSubject(view, subjectId, purpose, pin=null) {
  view.subjectId = subjectId; view.pinKey = pin ? studioPin(pin) : null; view.purpose = purpose;
  view.message = ""; view.lastTrace = null; renderStage();
  document.getElementById?.("studio-asset-detail")?.scrollIntoView?.({block:"start"});
}
async function studioWrite(id, change, done, {refreshOnError = false, pendingApproval = null, continuation = null} = {}) {
  const view = studioLocal(id);
  if (view.pending || view.loading || state.projectId !== id) return;
  view.pending = true; view.pendingApproval = pendingApproval; view.continuationReceipt = continuation;
  view.message = ""; view.lastTrace = null; studioRedraw(id);
  try {
    const result = await change();
    view.message = done(result); view.lastTrace = result.trace_id || result.job?.trace_id;
    await studioRefresh(id);
  } catch (error) {
    view.message = error.message;
    if (refreshOnError) await studioRefresh(id);
  }
  finally { view.pending = false; view.pendingApproval = null; studioRedraw(id); }
}
function readStudioFile(file) {
  return new Promise((resolve, reject) => {
    if (!file || file.size <= 0 || file.size > 8 * 1024 * 1024) { reject(new Error("请选择不超过 8 MiB 的 PNG、JPEG、WebP、WAV 或 MP3 文件。")); return; }
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("无法读取本机文件，请重新选择。"));
    reader.readAsDataURL(file);
  });
}
function sameAssetPin(left, right) {
  return !!left && !!right && left.asset_id === right.asset_id && left.revision_id === right.revision_id && left.sha256 === right.sha256;
}

function studioSelectedPin(item) {
  return item?.selected_pin || (item?.status === "approved" ? item.candidate : null);
}

function studioAssetDisplay(refs, item) {
  const current = refs.find(ref => sameAssetPin(ref,studioSelectedPin(item)));
  const candidate = refs.find(ref => sameAssetPin(ref,item?.candidate));
  const newest = refs.reduce((best,ref) => !best || (ref.created_at || "") > (best.created_at || "") ? ref : best,null);
  return current || candidate || newest;
}

function studioAssetStatus(item, ref) {
  const current = studioSelectedPin(item);
  if (ref && sameAssetPin(current,ref)) {
    if (item.status === "generating") return "正在制作新版";
    if (item.status === "needs_attention") return "新版需处理";
    if (item.status === "awaiting_review") return "新版等待自动选用";
    if (sameAssetPin(item.approval?.pin, ref)) {
      if (item.approval.selection_mode === "human") return "已人工通过";
      if (item.approval.authority === "asset_agent") return "已自动选用";
    }
    return "已选用";
  }
  return assetStates[item?.status] || (ref ? "已有候选" : "待制作");
}

function studioEvidenceText(value) {
  if (value == null) return "";
  if (Array.isArray(value)) return value.map(studioEvidenceText).filter(Boolean).join(" · ");
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function studioHumanApproval(item, ref, view, id, subjectName) {
  const review = item?.review, reviewRef = review?.review_ref;
  if (item?.status !== "needs_attention" || !sameAssetPin(item.candidate, ref)
      || !studioMediaUrl(ref.media_url, id) || !reviewRef?.sha256
      || !["FAIL","UNKNOWN"].includes(review.verdict || review.status)) return null;
  const {asset_id, revision_id, sha256} = ref;
  const name = subjectName + "的" + (studioPurposes[item.purpose] || item.purpose) + " " + revision_id;
  const saving = sameAssetPin(view.pendingApproval, ref);
  const approve = button(saving ? "正在通过并绑定镜头…" : "人工通过", async () => {
    await studioWrite(id,
      () => request(studioPath(id) + "/assets/approve", {method:"POST",body:{asset_id, revision_id, sha256, review_ref:reviewRef}}),
      () => name + " 已人工通过并选用。", {refreshOnError:true, pendingApproval:ref});
  }, {small:true, primary:true, disabled:view.pending || view.loading});
  approve.title = "采用此版本作为镜头参考，保留 Agent 原自检记录";
  approve.setAttribute("aria-label",(saving ? "正在通过" : "人工通过") + name);
  if (saving) approve.setAttribute("aria-busy","true");
  return approve;
}

function studioAssetProblems(work, entities, view, id) {
  const pending = work?.items?.filter(item => item.status === "needs_attention") || [];
  if (!pending.length) return null;
  const box = node("section","studio-asset-problems");
  const title = pending.length + " 项资产需要处理";
  box.setAttribute("aria-label",title);
  box.append(node("h3","",title));
  for (const item of pending) {
    const entity = entities.find(e => e.id === item.subject_id);
    const issue = item.diagnostic || {subject_id:item.subject_id, name:entity?.name, purpose:item.purpose, trace_id:item.trace_id};
    const row = node("article","studio-asset-problem");
    Object.assign(row.dataset, {assetJobId:item.job_id || "", subjectId:item.subject_id, purpose:item.purpose,
      assetId:item.candidate?.asset_id || "", revisionId:item.candidate?.revision_id || "",
      candidateSha:item.candidate?.sha256 || "", reviewSha:item.review?.review_ref?.sha256 || ""});
    row.tabIndex = -1;
    const heading = node("div","studio-problem-heading");
    heading.append(node("strong","",assetIssueName(issue)));
    if (issue.verdict === "FAIL") heading.append(pill("FAIL","未通过"));
    if (issue.verdict === "UNKNOWN") heading.append(pill("UNKNOWN","未能确认"));
    const selected = studioSelectedPin(item);
    const metadata = [issue.revision_id && "候选 " + issue.revision_id,
      issue.attempt_count > 0 && "本次核验 " + issue.attempt_count + " 次",
      selected && "当前仍使用 " + selected.revision_id].filter(Boolean);
    append(row, heading, metadata.length ? node("small","studio-muted",metadata.join(" · ")) : null,
      node("p","studio-problem-reason",issue.reason || "这项资产缺少具体的失败说明。"));
    if (issue.action) row.append(node("p","studio-problem-action","建议：" + issue.action));
    const checks = issue.evidence || [];
    if (checks.length) {
      const evidence = node("div");
      for (const check of checks) {
        const lines = node("div","studio-criterion");
        append(lines,node("strong","",check.label || check.path),pill(check.status));
        if (check.expected != null) lines.append(node("p","","要求　" + studioEvidenceText(check.expected)));
        if (check.actual != null) lines.append(node("p","","记录　" + studioEvidenceText(check.actual)));
        evidence.append(lines);
      }
      row.append(studioDetails("查看依据（" + checks.length + "）",evidence));
    }
    const actions = node("div","studio-actions");
    if (entity) {
      const candidate = entity.references?.find(ref => sameAssetPin(ref,item.candidate));
      const imageUrl = item.purpose !== "voice" && candidate && studioMediaUrl(candidate.media_url, id);
      if (!selected && imageUrl && item.review?.review_ref?.sha256) {
        const image = node("img","studio-problem-image");
        image.alt = entity.name + "的待审" + (studioPurposes[item.purpose] || item.purpose) + " " + candidate.revision_id;
        image.loading = "lazy"; image.src = imageUrl;
        const open = studioLink(imageUrl,"");
        open.target = "_blank"; open.rel = "noopener";
        open.append(image,node("span","","查看候选原图"));
        row.append(open);
      }
      append(actions, button("查看素材与处理选项",()=>studioSelectSubject(view,item.subject_id,item.purpose,item.candidate),{small:true,quiet:true}),
        studioHumanApproval(item,candidate,view,id,entity.name));
    }
    const url = assetIssueTraceURL(issue);
    if (url) actions.append(studioLink(url,issue.span_id ? "定位到 Trace 记录 ↗" : "查看任务 Trace ↗"));
    row.append(actions); box.append(row);
  }
  return box;
}

function studioCandidateReview(item, selected) {
  const panel = node("section","studio-self-review");
  const attempt = (item?.attempts || []).filter(row => sameAssetPin(row.pin, selected)).at(-1);
  const current = sameAssetPin(item?.candidate, selected);
  const review = (current ? item.review : null) || attempt?.review;
  if (!review) {
    panel.append(node("p","studio-muted","此版本尚无自检报告。"));
    return studioDetails("Agent 自检 · 暂无报告",panel);
  }
  const verdict = review.verdict || review.status;
  const resultLabel = {PASS:"自检通过",FAIL:"自检未通过",UNKNOWN:"证据不足"};
  if (review.coverage) panel.append(node("p","studio-muted",review.coverage));
  if (review.observation_error) panel.append(note(review.observation_error,true));
  const criteriaLabels={subject:"对象设定",scene_use:"镜头用途",asset_use:"资产用途",style:"风格与样式",palette:"配色与光照",concept:"创作意图",
    voice:"声音设定",performance:"语气与节奏",feedback:"修改意见",revision:"修改意见",audibility:"声音清晰度",dialogue:"台词"};
  for (const rule of review.checks || review.criteria || []) {
    const row = node("div","studio-criterion");
    const label = rule.label || criteriaLabels[rule.path] || (/^anchor_\d+$/.test(rule.path) ? "参考素材比对" : rule.path);
    append(row,node("strong","",label + (rule.required === false ? "（补充观察）" : "")),pill(rule.status),
      node("p","","要求　" + (studioEvidenceText(rule.expected) || "未记录")),
      node("p","","观察　" + (studioEvidenceText(rule.actual) || "缺少观察证据")));
    if (rule.evidence) row.append(node("p","studio-muted",studioEvidenceText(rule.evidence)));
    if (rule.frame_ids?.length) row.append(node("small","studio-muted","观察依据　"+rule.frame_ids.join(" · ")));
    panel.append(row);
  }
  append(panel,studioTrace(review.trace_id || attempt?.trace_id || item.trace_id,"核验 Trace ↗"));
  return studioDetails("Agent 自检 · " + (resultLabel[verdict] || "尚无结论"),panel);
}

function studioAssetFeedback(item, entity, purpose, selected, latest, view, id) {
  const area = node("div","studio-asset-feedback");
  if (!item || item.status === "generating" || item.status === "planned") return area;
  const lastPin = latest || item.candidate;
  if (selected && !sameAssetPin(selected,lastPin) && !sameAssetPin(selected,studioSelectedPin(item))) {
    area.append(node("p","studio-muted","查看当前使用版本或最新候选即可重新生成。"));
    return area;
  }
  const key = JSON.stringify([entity.id,purpose,lastPin?.asset_id,lastPin?.revision_id]);
  const feedback = node("textarea"); feedback.rows = 2; feedback.maxLength=3000;
  feedback.value = view.assetFeedback.get(key) || "";
  feedback.placeholder = {voice:"例如：保留音色，最后一句放慢半拍。", scene:"例如：窗边用冷色光，让门与柜台的位置更清楚。",
    prop:"例如：保留原来的材质，让道具轮廓更清楚。", costume:"例如：保留版型，让领口的珠饰更清楚。"}[purpose]
    || "例如：保留发型，减少背景里的红色，让发卡更清楚。";
  feedback.addEventListener("input", () => view.assetFeedback.set(key,feedback.value));
  const redo = button("重新生成", () => {
    const correction = feedback.value.trim();
    void studioWrite(id, () => request(studioPath(id) + "/asset-regenerate",{method:"POST",body:{
      subject_id:entity.id,purpose,feedback:correction,
      ...(lastPin ? {asset_id:lastPin.asset_id,parent_revision:lastPin.revision_id} : {}),
    }}), result => {
      acceptJob(result.job); view.assetFeedback.delete(key); view.pinKey=null;
      return "Agent 将按修改要求生成新版，通过自检后自动选用。";
    });
  }, {small:true,disabled:view.pending || !keyReady(purpose === "voice" ? "voice.synthesis" : "reference.image")});
  append(area,formField("asset-feedback","修改要求（可选）",feedback),redo);
  return area;
}

function studioReferenceEditor(catalog, entity, view, id, work) {
  const area = node("div", "studio-reference");
  const purposes = entity.purposes || [];
  if (!purposes.length) { area.append(note("此类对象尚未配置可导入的参考类型。")); return area; }
  const purpose = purposes.includes(view.purpose) ? view.purpose : purposes[0];
  const controls = node("div", "studio-fields");
  controls.append(formField("studio-purpose", "参考用途", studioOptions(purposes.map(p => [p, studioPurposes[p] || p]), purpose, p => {
    view.purpose = p; view.pinKey = null; view.message = ""; renderStage();
  }, "参考用途")));
  const refs = (entity.references || []).filter(ref => ref.purpose === purpose);
  const item = work?.items?.find(row => row.subject_id === entity.id && row.purpose === purpose);
  const selected = refs.find(ref => studioPin(ref) === view.pinKey) || studioAssetDisplay(refs,item);
  const latest = selected && refs.find(ref => ref.asset_id === selected.asset_id);
  const current = studioSelectedPin(item);
  if (selected) controls.append(formField("studio-revision", "已保存的资产版本", studioOptions(refs.map(ref => [studioPin(ref), ref.asset_id.split("/").pop() + " @ " + ref.revision_id
    + (sameAssetPin(current,ref) ? " · 当前使用" : sameAssetPin(item?.candidate,ref) ? " · 当前候选" : "")]),
    studioPin(selected), value => { view.pinKey = value; view.message = ""; renderStage(); }, "已保存的资产版本")));
  area.append(controls);
  if (item) {
    area.append(node("strong","asset-state asset-state-" + item.status,[
      current && "当前使用 " + current.revision_id, studioAssetStatus(item,current),
    ].filter(Boolean).join(" · ")));
    if (current && item.candidate && !sameAssetPin(current,item.candidate)) {
      area.append(node("p","studio-muted","本次候选 " + item.candidate.revision_id + "，镜头继续使用 " + current.revision_id + "。"));
    }
    if (item.diagnostic && (!selected || sameAssetPin(item.candidate,selected))) {
      append(area, node("p","studio-problem-reason",item.diagnostic.reason),
        item.diagnostic.action ? node("p","studio-muted","建议：" + item.diagnostic.action) : null);
    }
    if (item.error) area.append(studioDetails("技术错误记录",note(studioEvidenceText(item.error),true)));
    if (item.feedback) area.append(node("p","studio-muted","上次修改意见　" + item.feedback));
    if (item.attempts?.length) {
      const attempts=node("div","studio-attempts");
      for (const a of item.attempts) {
        const row=node("div","studio-attempt");
        append(row,node("code","",a.pin?.revision_id || "尚未登记版本"),pill(a.verdict || a.review?.verdict),studioTrace(a.trace_id,"核验 Trace ↗"));
        attempts.append(row);
      }
      area.append(studioDetails("制作与核验记录 · " + item.attempts.length,attempts));
    }
  }
  if (selected) {
    const url = studioMediaUrl(selected.media_url, id);
    const preview = node("div", "studio-media");
    if (url) {
      const media = node(purpose === "voice" ? "audio" : "img");
      if (purpose === "voice") { media.controls = true; media.preload = "none"; }
      else { media.alt = entity.name + "的" + studioPurposes[purpose] + " " + selected.revision_id; media.loading = "lazy"; }
      media.src = url;
      if (purpose === "voice") preview.append(media);
      else {
        const full = studioLink(url,""); full.target="_blank"; full.rel="noopener"; full.className="studio-reference-image";
        full.setAttribute("aria-label","查看完整候选图片"); full.append(media); preview.append(full);
      }
    }
    const meta = node("div");
    append(meta, node("strong", "", studioPin(selected)), node("p", "", selected.mime_type || ""),
      selected.origin ? node("p","","来源　" + (selected.origin.method === "generated" ? "模型生成 · " + selected.origin.model : "本地导入")
        + (selected.origin.entity_version ? " · 设定 v" + selected.origin.entity_version : "")) : null,
      studioDetails("保存位置与内容哈希", studioRows([["文件", selected.media_path], ["SHA-256", selected.media_sha256]])),
      studioTrace(selected.trace_id, selected.origin?.method === "generated" ? "生成 Trace ↗" : "导入 Trace ↗"));
    const boundIds = catalog.shots.filter(shot => shot.required_refs?.some(pin => studioPin(pin) === studioPin(selected) && pin.sha256 === selected.sha256)).map(shot=>shot.id);
    meta.append(node("p","",boundIds.length ? "使用此版本的镜头　" + boundIds.join(" · ") : "此版本尚未被镜头引用"));
    if (sameAssetPin(current,selected) && sameAssetPin(item?.approval?.pin,selected)) {
      append(meta,studioTrace(item.approval.trace_id,item.approval.selection_mode === "human" ? "人工通过 Trace ↗" : "版本选用 Trace ↗"));
    }
    if (selected.dependencies?.length) {
      const consumers = node("ul","studio-sources");
      for (const dep of selected.dependencies) consumers.append(node("li","",[studioPin(dep),dep.source_id,dep.role].filter(Boolean).join(" · ")));
      meta.append(studioDetails("读取此版本的归档资产",consumers));
    }
    append(meta, studioHumanApproval(item,selected,view,id,entity.name));
    append(preview, meta); area.append(preview);
    area.append(studioAssetFeedback(item,entity,purpose,selected,latest,view,id));
    area.append(studioCandidateReview(item,selected));
    const reviewed = item?.attempts?.some(a=>sameAssetPin(a.pin,selected) && a.review)
      || (sameAssetPin(item?.candidate,selected) && !!item.review);
    const inconclusive = sameAssetPin(item?.candidate,selected) && item?.diagnostic?.stage === "review" && item.diagnostic.verdict === "UNKNOWN";
    if ((!reviewed || inconclusive) && item?.status !== "generating" && sameAssetPin(latest,selected)) {
      const inspect=button(reviewed ? "重新核验此版本" : "核验已有候选",()=>{
        const pin = Object.fromEntries(["asset_id","revision_id","sha256","media_sha256","purpose","subject_id"]
          .map(key => [key,selected[key]]));
        void studioWrite(id,()=>request(studioPath(id)+"/asset-inspect",{method:"POST",body:{pin}}),
          result=>{acceptJob(result.job);return "已委托 Agent 核验此版本。";});
      },{small:true,disabled:view.pending || !keyReady(purpose === "voice" ? "voice.review" : "vision.review")});
      area.append(inspect);
    }
  } else {
    area.append(node("p", "studio-empty", (studioPurposes[purpose] || purpose) +
      (item?.status === "needs_attention" ? "制作需要处理，详情见上方记录。" : "候选将在后台制作后显示。")));
    if (item?.status === "needs_attention") area.append(studioAssetFeedback(item,entity,purpose,null,null,view,id));
  }

  const upload = node("div", "studio-upload");
  const file = node("input"); file.type = "file"; file.accept = purpose === "voice" ? ".wav,.mp3" : ".png,.jpg,.jpeg,.webp";
  const send = button(latest ? "导入新版参考" : "导入参考文件", () => {
    const chosen = file.files?.[0];
    if (!chosen) { file.focus(); return; }
    void studioWrite(id, async () => request(studioPath(id) + "/references", {method:"POST", body:{
      subject_id:entity.id, purpose, data_uri: await readStudioFile(chosen),
      ...(latest ? {asset_id:latest.asset_id, parent_revision:latest.revision_id} : {}),
    }}), result => {
      view.pinKey = studioPin(result.pin);
      if (result.job) acceptJob(result.job);
      return "已保存导入版本 " + view.pinKey + (result.job ? "，Agent 将核验该候选。" : "。");
    });
  }, {small:true, disabled:true});
  file.addEventListener("change", () => { send.disabled = view.pending || !file.files?.length; });
  append(upload, formField("studio-file", "本地参考文件", file, "导入后 Agent 会调用模型核验实际文件。"), send); area.append(studioDetails("补充本地参考文件",upload));
  if (!selected) return area;
  area.append(studioUsage(selected, catalog.story_id));
  return area;
}
function studioUsage(ref, storyId) {
  const box = node("div", "studio-usage"), output = node("div");
  const lookup = button("查实际提交与候选 Trace", async () => {
    lookup.disabled = true; output.replaceChildren(node("p", "", "正在核对任务回执…"));
    try {
      const query = new URLSearchParams({asset_id:ref.asset_id, revision_id:ref.revision_id, story_id:storyId});
      const result = await request("/api/reference-usage?" + query);
      output.replaceChildren(node("p", "", "确认提交 " + result.submitted_count + " 个候选 · 供应商接受 " + result.accepted_count + " 个"));
      for (const requirement of result.required_shots || []) output.append(node("p","",requirement.shot_id + " 固定此版本 · " + studioPin(requirement.source)));
      for (const item of [...result.candidates || [], ...result.unverified_candidates || []]) {
        const row = node("div");
        append(row, node("span", "", [item.shot_id, item.candidate_id, item.evidence_status].filter(Boolean).join(" · ")), studioTrace(item.trace_id, "候选 Trace ↗")); output.append(row);
      }
    } catch(error) { output.replaceChildren(note(error.message,true)); }
    finally { lookup.disabled = false; }
  }, {small:true, quiet:true});
  append(box, lookup, output); return box;
}
function studioAssetOverview(work, view, id) {
  const box = node("div","studio-asset-work");
  const summary = node("div","studio-asset-totals");
  const items = work?.items || [];
  const assetKeyReady = item => keyReady(item.purpose === "voice" ? "voice.synthesis" : "reference.image");
  for (const [key,label] of Object.entries(assetStates)) {
    const count = items.filter(row => key === "approved" ? studioSelectedPin(row) : row.status === key).length;
    if (key === "awaiting_review" && !count) continue;
    const cell=node("div","asset-total asset-state-" + key);
    append(cell,node("strong","",String(count)),node("span","",label)); summary.append(cell);
  }
  box.append(summary);
  const activeJob = (work.jobs || []).find(job => ACTIVE.has(job.status));
  const active = activeJob || items.some(item => item.status === "generating");
  const hasPending = items.some(item=>item.status === "planned");
  if (active) {
    box.append(node("p","studio-running",activeJob?.status === "queued" ? "资产制作任务已入队。" : (activeJob?.message || "Agent 正在制作和核验资产。")));
  } else if (work.auto_dispatch?.status === "blocked") {
    box.append(note(work.auto_dispatch.reason || "资产制作受阻，请查看任务记录。",true));
  } else if (hasPending) {
    const issue = (work.jobs || []).find(job=>["failed","interrupted"].includes(job.status));
    const canGenerate = items.some(item => item.status === "planned" && assetKeyReady(item));
    box.append(node("p",issue || !canGenerate ? "studio-muted" : "studio-running",issue?.error || issue?.message
      || (!canGenerate ? "待制作资产的模型缺少密钥。" : "后台正在安排尚未制作的资产。")));
  }
  if (work.style) box.append(studioDetails("全剧制作要求",node("p","studio-muted",work.style)));
  if (!active) {
    const controls=node("div","studio-batch-controls");
    const style=node("textarea");style.rows=2;style.maxLength=2000;style.value=view.assetStyle ?? work?.style ?? "";
    style.placeholder="例如：写实电影质感，低饱和青灰色，冷光中保留暖色肤色。";
    const send=button("按新风格重新制作资产",()=>{
      const direction = style.value.trim();
      if (!direction || direction === (work.style || "").trim()) return;
      void studioWrite(id,()=>request(studioPath(id)+"/asset-production",{method:"POST",body:{style:direction}}),
        result=>{acceptJob(result.job);return "Agent 已接手制作，候选和自检报告将显示在对应对象下。";});
    },{small:true,disabled:true});
    const updateButton = () => {
      send.disabled=view.pending || !items.some(assetKeyReady) || !style.value.trim() || style.value.trim() === (work.style || "").trim();
    };
    style.addEventListener("input",()=>{view.assetStyle=style.value;updateButton();});
    updateButton();
    append(controls,formField("asset-style","新的全剧风格与配色",style,"提交后会按新要求重新核验并制作候选，可能产生模型费用。"),send);
    box.append(studioDetails("修改全剧风格",controls));
  }
  return box;
}
function studioAssetGallery(entities, work, view, id) {
  const grid = node("div","studio-asset-gallery");
  grid.setAttribute("aria-label","全剧参考图片与声音");
  const cards = [];
  for (const entity of entities) {
    for (const purpose of entity.purposes || []) {
      const item = work?.items?.find(row => row.subject_id === entity.id && row.purpose === purpose);
      const refs = (entity.references || []).filter(ref => ref.purpose === purpose);
      const ref = studioAssetDisplay(refs,item);
      const url = studioMediaUrl(ref?.media_url,id);
      const status = studioAssetStatus(item,ref);
      const versionLabel = ref && (sameAssetPin(studioSelectedPin(item),ref) ? "当前 " : "候选 ") + ref.revision_id;
      const selected = entity.id === (view.subjectId || entities[0]?.id) && purpose === (view.purpose || entity.purposes[0]);
      const card = node("article","studio-asset-card" + (selected ? " selected" : ""));
      const open = node("button","studio-gallery-target");
      open.type="button"; open.setAttribute("aria-pressed",String(selected));
      open.setAttribute("aria-label","查看" + entity.name + "的" + (studioPurposes[purpose] || purpose));
      open.addEventListener("click",()=>studioSelectSubject(view,entity.id,purpose));
      if (purpose !== "voice" && url) {
        const image = node("img","studio-gallery-image");
      image.loading="lazy"; image.fetchPriority="low"; image.alt=entity.name + " · " + (studioPurposes[purpose] || purpose); image.src=url; open.append(image);
      } else if (purpose !== "voice") {
        open.append(node("span","studio-gallery-empty",ref ? "图片暂不可用" : status));
      }
      append(open,node("small","studio-gallery-type",studioPurposes[purpose] || purpose),node("strong","",entity.name),
        node("span","studio-gallery-status asset-state-" + (item?.status || "planned"),[versionLabel,status].filter(Boolean).join(" · ")));
      card.append(open);
      if (purpose === "voice" && url) {
        const player=node("audio","studio-gallery-audio");player.controls=true;player.preload="none";
        player.setAttribute("aria-label",entity.name + "的试音");player.src=url;card.append(player);
      }
      cards.push({card,ready:!!url});
    }
  }
  for (const entry of cards.sort((a,b)=>Number(b.ready)-Number(a.ready))) grid.append(entry.card);
  return grid;
}

function studioAssets(data, catalog, view) {
  const box = node("div", "studio-inspector");
  const entities = catalog.entities || [];
  const chosen = entities.find(entity => entity.id === view.subjectId) || entities[0];
  const work = data.asset_work;
  const total = entities.reduce((count,entity)=>count + (entity.purposes || []).length,0);
  const ready = entities.reduce((count,entity)=>count + (entity.purposes || []).filter(purpose =>
    entity.references?.some(ref => ref.purpose === purpose && studioMediaUrl(ref.media_url,state.projectId))).length,0);
  const activeJob = work?.jobs?.find(job=>ACTIVE.has(job.status));
  const progress = activeJob?.status === "queued" ? "制作任务已排队" : activeJob?.message;
  const blocked = work?.auto_dispatch?.status === "blocked" ? work.auto_dispatch.reason : "";
  append(box, studioSection("剧本资产", ["已有素材 " + ready + " / " + total,progress || blocked].filter(Boolean).join(" · ")));
  if (!chosen) { box.append(note("工程尚未提供人物、场景或道具设定。")); return box; }
  append(box,studioAssetProblems(work,entities,view,state.projectId),studioAssetGallery(entities,work,view,state.projectId));
  if (work) box.append(studioAssetOverview(work,view,state.projectId));
  else box.append(note("尚未读取到资产制作计划，请刷新记录。"));
  const detail = node("section", "studio-entity-detail studio-detail-panel");detail.id="studio-asset-detail";
  const header = node("div", "studio-object-title"); append(header, node("h3", "", chosen.name),
    node("span", "studio-version", "设定 v" + chosen.version));
  const canonical = Object.entries(chosen.canonical || {}).filter(([key]) => key !== "name")
    .map(([,value]) => typeof value === "string" ? value : JSON.stringify(value)).join(" · ");
  append(detail, header, node("code", "studio-object-id", chosen.id), canonical ? node("p", "studio-canonical", canonical) : null,
    node("p", "studio-shots", "引用镜头　" + (chosen.shot_ids?.join(" · ") || "尚无")),
    studioSources(chosen.source_refs), studioReferenceEditor(catalog, chosen, view, state.projectId,work));
  box.append(detail);
  if (!data.stages?.some(stage=>stage.id==="assets")) box.append(studioDetails("资产如何保存与复用", studioRows([
    ["输入", "剧本的人物、场景、道具设定及各自的来源节拍"], ["输出", "通过自检的参考文件及其版本，Agent 自动写入相关镜头的制作规格"],
    ["资产库", catalog.store_path], ["下游使用", "视频制作 Agent 按规格读取确切版本。供应商实际收到的参考由提交回执与 Trace 确认。"],
  ])));
  return box;
}
function studioActualShot(shot) {
  const refs = shot.actual_refs;
  const box = node("div");
  append(box,studioRows([["候选", [shot.id, shot.status,studioPin(shot.media_ref)].filter(Boolean).join(" · ")],
    ["保存位置", shot.path || "待记录"], ["实际提交参考", refs?.length ? refs.map(studioPin).join("、") :
      Array.isArray(refs) ? "记录的参考列表为空" : "缺少实际提交证据"],
    ["提交回执", shot.receipt_status || "缺少回执记录"]]),assetUrl(shot.url) ? studioLink(assetUrl(shot.url),"播放此候选 ↗") : null);
  return box;
}

const progressStatuses = {pending:"待制作",generating:"制作中",ready:"已生成",reused:"已复用",failed:"生成失败",unknown:"结果待核实",not_started:"未开始"};
const progressPhases = {queued:"等待开始",preparing:"准备镜头",generating:"正在制作",reviewing:"自动验收与修复",repairing:"正在针对验收修复",completed:"整片验收通过 · 可交付",needs_approval:"待处理制作阻碍",stopped:"制作未完成 · 已保留进度"};
function shotAcceptanceLabel(status) {
  return ({PASS:"验收通过",FAIL:"验收未通过",PENDING:"待验收"})[status] || "验收待确认";
}

function studioObserverUrl(projectId, jobId) {
  return "/progress?" + new URLSearchParams({project:projectId,job:jobId});
}
function studioWorkbenchOrigin() {
  return window.location.pathname === "/" ? "http://127.0.0.1:8765" : "";
}
function studioClipPreviewUrl(projectId,jobId,shot) {
  if (!["ready","reused"].includes(shot.status) || !/^[a-f0-9]{64}$/.test(shot.media_sha256 || "") || !shot.preview_url) return null;
  try {
    const url = new URL(shot.preview_url, window.location.origin);
    const path = studioPath(projectId) + "/progress/media";
    return url.origin === window.location.origin && !url.username && !url.password && url.pathname === path
      && url.searchParams.get("job_id") === jobId && url.searchParams.get("shot_id") === shot.shot_id
      && url.searchParams.get("sha256") === shot.media_sha256 ? path + url.search : null;
  } catch { return null; }
}

// The standalone viewer uses the same read-only component without loading the workbench.
const recordedShotStatuses = {generated:"已记录生成",reused:"已记录复用",reserved:"已预留",pending:"待制作",unknown:"待核实"};
function studioRecordedProgress(value, projectId, jobId, messageId) {
  if (!value || value.project_id!==projectId || value.job_id!==jobId || !/^[0-9a-f]{32}$/.test(messageId || "") || value.message_id!==messageId ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.test(value.recorded_at || "") ||
      !Number.isFinite(Date.parse(value.recorded_at)) || !Array.isArray(value.shots) || !value.shots.length ||
      value.shot_count!==value.shots.length) return null;
  const ids = new Set(), shots = [];
  for (const row of value.shots) {
    if (typeof row?.shot_id!=="string" || !row.shot_id.trim() || ids.has(row.shot_id)) return null;
    ids.add(row.shot_id);
    shots.push({shot_id:row.shot_id, status:Object.hasOwn(recordedShotStatuses,row.status)?row.status:"unknown",
      operation:["new","retake","reuse"].includes(row.operation)?row.operation:null});
  }
  const completed = shots.filter(row=>row.status==="generated" || row.status==="reused").length;
  if (value.completed_count!==completed) return null;
  return {project_id:projectId,job_id:jobId,message_id:messageId,recorded_at:value.recorded_at,completed_count:completed,shot_count:shots.length,shots};
}

function studioProgressMonitor(projectId,jobId,{standalone=false,onUpdate=null,onRead=null,jobStatus=null,messageId=null,recorded=null}={}) {
  function el(tag,css="",value="") {
    const element = document.createElement(tag);
    element.className = css;
    element.textContent = value;
    return element;
  }
  const element = el("section","studio-live");
  element.setAttribute("aria-label","本次制作进度");
  element.dataset.progressJob = jobId;
  element.dataset.readState = "loading";
  const title = el("h3","","本次制作进度");
  const phase = el("span","live-phase");
  phase.textContent = ({queued:"任务已排队",running:"任务进行中",succeeded:"任务已完成",failed:"任务失败",interrupted:"任务已中断",cancelled:"任务已取消"})[jobStatus] || "";
  const heading = el("div","live-heading"); heading.append(title,phase);
  const count = el("p","live-count","正在读取镜头记录…");
  count.setAttribute("aria-live","polite");
  const bar = el("progress","live-bar"); bar.setAttribute("aria-label","已有素材的镜头数"); bar.hidden=true;
  const reason = el("p","live-reason"); reason.hidden=true;
  const original = el("details","live-notice"); original.hidden=true;
  const originalReason=el("p"); original.append(el("summary","","原委托的停止原因"),originalReason);
  const statusLine = el("p","studio-muted live-notice"); statusLine.hidden=true;
  const preview = el("div","live-preview"); preview.hidden=true;
  const caption = el("strong");
  const video = el("video","video-player"); video.controls=true; video.preload="metadata"; video.playsInline=true;
  preview.append(caption,video);
  const list = el("ol","live-shots");
  const nav = el("div","studio-actions");
  const lastEvent = el("small","studio-muted"); nav.append(lastEvent);
  const traceLink = el("a","studio-link","查看本次 Trace ↗"); traceLink.hidden=true;
  nav.append(traceLink);
  const observerLink = el("a","studio-link","打开镜头进度页 ↗");
  observerLink.href=studioObserverUrl(projectId,jobId); observerLink.target="_blank"; observerLink.rel="noopener";
  if (!standalone) nav.append(observerLink);
  element.append(heading,count,bar,reason,original,preview,list,statusLine,nav);
  const rows = new Map();
  let previousRows = [];
  let selectedId=null, selectedSrc=null, latest=null, busy=false, lastRead=0, unavailable=false, sample=null;
  let staleTimer=null;
  let readController=null;
  function select(row) {
    const source = row && studioClipPreviewUrl(projectId,jobId,row);
    if (!source) {
      if (selectedSrc) { video.removeAttribute("src"); video.load(); }
      selectedId=selectedSrc=null; preview.hidden=true; return;
    }
    selectedId=row.shot_id;
    caption.textContent=row.shot_id + " · 镜头预览 · " + (row.human_approved ? "已人工通过" : shotAcceptanceLabel(row.acceptance_status));
    video.setAttribute("aria-label",row.shot_id + " 候选片段");
    if (source !== selectedSrc) { video.src=source; selectedSrc=source; }
    preview.hidden=false;
    for (const [id,parts] of rows) parts.play.setAttribute("aria-pressed",String(id===selectedId));
  }
  function revokePlayback() {
    latest=null; select(null);
    for (const parts of rows.values()) parts.play.hidden=true;
  }
  function draw(data) {
    if (data.project_id !== projectId || data.job_id !== jobId || (messageId && data.message_id!==messageId) || !Array.isArray(data.shots)) throw Error("进度记录与当前委托不符。");
    previousRows.forEach(row=>row.remove()); previousRows=[];
    latest=data;
    unavailable=false;
    observerLink.hidden=false;
    element.dataset.readState = "ready";
    title.textContent="本次制作进度";
    phase.textContent=data.phase==="completed" && (data.preview_gate?.ready!==true || data.production_complete!==true)
      ? "制作任务已结束" : progressPhases[data.phase] || "正在核对进度";
    const deliveredHere = data.preview_gate?.ready===true && !!data.message_id && data.preview_gate.request_id===data.message_id;
    if (deliveredHere) phase.textContent="成片已交付 · 可预览";
    const deliveryLabel = deliveredHere || (data.preview_gate?.ready===true && data.production_complete===true) ? "" : " · 整片待交付";
    const mediaCount = `已有镜头素材 ${data.completed} / ${data.total}`;
    count.textContent = data.total ? mediaCount + deliveryLabel
      : ["queued","running"].includes(data.job_status) ? "本次委托尚无镜头进度记录" : "本次委托没有可核对的镜头记录";
    if (data.current_shot_id && ["preparing","generating"].includes(data.phase)) count.textContent += " · 当前 " + data.current_shot_id;
    bar.hidden=!data.total; if (data.total) { bar.max=data.total; bar.value=data.completed; }
    const superseded = /^正在准备工具与输入[。…\s]*$/.test(data.reason || "") &&
      (data.completed > 0 || !["queued","preparing"].includes(data.phase));
    reason.textContent=superseded || deliveredHere ? "" : data.reason || ""; reason.hidden=!reason.textContent;
    originalReason.textContent=data.original_reason || (deliveredHere && data.job_status!=="succeeded" ? data.reason : "") || "";
    original.hidden=!originalReason.textContent || originalReason.textContent===reason.textContent;
    const stamp=Date.parse(data.updated_at || "");
    lastEvent.textContent=Number.isFinite(stamp) ? "最近记录 " + new Date(stamp).toLocaleString("zh-CN",{hour12:false}) : "";
    const ids = new Set(data.shots.map(shot=>shot.shot_id));
    for (const [id,parts] of rows) if (!ids.has(id)) { parts.item.remove(); rows.delete(id); }
    for (const shot of data.shots) {
      let parts=rows.get(shot.shot_id);
      if (!parts) {
        const item=el("li","live-shot");
        const id=el("strong","",shot.shot_id), operation=el("small","studio-muted"), status=el("span","live-shot-status");
        const states=el("div","live-shot-states"), acceptance=el("span","live-shot-acceptance");
        states.append(status,acceptance);
        const play=el("button","button button-small button-quiet","预览"); play.type="button";
        play.setAttribute("aria-label","预览 " + shot.shot_id);
        play.addEventListener("click",()=>{
          select(latest?.shots.find(row=>row.shot_id === shot.shot_id));
          if (!selectedSrc) return;
          preview.scrollIntoView?.({block:"nearest"});
          video.play?.()?.catch(()=>{});
        });
        const detail=el("small","live-shot-detail");
        item.append(id,operation,states,play,detail); list.append(item);
        parts={item,operation,status,acceptance,play,detail}; rows.set(shot.shot_id,parts);
      }
      parts.item.dataset.status=shot.status;
      parts.operation.textContent={new:"新拍",retake:"重拍",reuse:"复用"}[shot.operation] || "";
      parts.play.hidden=!studioClipPreviewUrl(projectId,jobId,shot);
      parts.status.textContent=progressStatuses[shot.status] || "待核实";
      parts.acceptance.hidden=!["ready","reused"].includes(shot.status);
      parts.acceptance.dataset.verdict=shot.human_approved ? "PASS" : shot.acceptance_status || "UNKNOWN";
      parts.acceptance.textContent=parts.acceptance.hidden ? "" : shot.human_approved ? "已人工通过" : shotAcceptanceLabel(shot.acceptance_status);
      parts.detail.textContent=shot.reason || ""; parts.detail.hidden=!shot.reason;
    }
    const first = data.shots.find(row=>studioClipPreviewUrl(projectId,jobId,row));
    const selected = selectedId && data.shots.find(row=>row.shot_id === selectedId && studioClipPreviewUrl(projectId,jobId,row));
    select(selected || first);
    const traceId = data.video_trace_id || data.trace_id;
    traceLink.hidden=!/^[a-f0-9]{32}$/.test(traceId || "");
    if (!traceLink.hidden) traceLink.href=(standalone ? studioWorkbenchOrigin() : "") + "/#step=traces&trace=" + traceId;
    const summary = [phase.textContent];
    if (data.total > 0) summary.push(mediaCount);
    if (data.current_shot_id && ["preparing","generating"].includes(data.phase)) summary.push("当前 " + data.current_shot_id);
    sample = { status:data.job_status, updatedAt:data.updated_at, readAt:Date.now(), text:summary.join(" · ") };
    if (onRead && typeof setTimeout==="function") {
      if (staleTimer!=null) clearTimeout(staleTimer);
      if (["queued","running"].includes(data.job_status)) staleTimer=setTimeout(onRead,15000);
    }
    if (onUpdate) onUpdate(data);
  }
  async function refresh(force=false) {
    const terminal=latest && !["queued","running"].includes(latest.job_status);
    const waitingForReceipt=terminal && latest.shots.some(shot=>shot.status==="unknown" && shot.task_id);
    const delay=waitingForReceipt ? 15000 : 4500;
    if (busy || (!force && (unavailable || (terminal && !waitingForReceipt) || Date.now()-lastRead<delay))) return;
    busy=true; lastRead=Date.now();
    const controller=typeof AbortController==="function"?new AbortController():null;
    readController=controller;
    try {
      const response=await fetch(studioPath(projectId)+"/progress?"+new URLSearchParams({job_id:jobId}),
        {method:"GET",priority:"high",signal:controller?.signal,credentials:"same-origin",cache:"no-store",headers:{Accept:"application/json"}});
      if (controller?.signal.aborted) return;
      if (response.status === 404 && !standalone) {
        unavailable=true;
        element.dataset.readState="unavailable";
        revokePlayback();
        if (!sample) count.textContent="未找到这次制作委托的进度。";
        else { statusLine.textContent="最新进度不可用，显示上次记录。"; statusLine.hidden=false; }
        observerLink.hidden=true; return;
      }
      const data=await response.json();
      if (controller?.signal.aborted) return;
      if (!response.ok) throw Error(typeof data.error==="string" ? data.error : "暂时无法读取制作进度。");
      draw(data); statusLine.hidden=true;
    } catch (error) {
      if (controller?.signal.aborted) return;
      element.dataset.readState="error";
      if (!sample) count.textContent="未能读取本次委托的镜头记录。";
      // Keep previously observed counts readable; verify authorization again before playback.
      revokePlayback();
      statusLine.textContent=(sample?"核对最新状态失败，显示上次记录。":"") + (error.message || "暂时无法读取进度，稍后会再试。");
      statusLine.hidden=false;
    } finally {
      if (readController===controller) { busy=false; readController=null; }
      if (!controller?.signal.aborted && onRead) onRead();
    }
  }
  function showRecorded(value) {
    if (!value || sample && (!sample.recorded || Date.parse(value.recorded_at)<=sample.readAt)) return false;
    const record = studioRecordedProgress(value,projectId,jobId,messageId);
    if (!record) return false;
    const stamp = Date.parse(record.recorded_at);
    previousRows.forEach(row=>row.remove()); previousRows=[];
    for (const shot of record.shots) {
      const item=el("li","live-shot"); item.dataset.status=shot.status;
      item.append(el("strong","",shot.shot_id),el("small","studio-muted",{new:"新拍",retake:"重拍",reuse:"复用"}[shot.operation] || ""),
        el("span","live-shot-status",recordedShotStatuses[shot.status]));
      list.append(item); previousRows.push(item);
    }
    element.dataset.readState="recorded"; title.textContent="上次制作记录";
    const text=`已有生成或复用记录 ${record.completed_count} / ${record.shot_count} 个镜头`;
    count.textContent=text;
    lastEvent.textContent="记录时间 " + new Date(stamp).toLocaleString("zh-CN",{hour12:false});
    statusLine.hidden=false; statusLine.textContent="正在核对最新状态…";
    sample={recorded:true,status:jobStatus,readAt:stamp,text};
    return true;
  }
  showRecorded(recorded);
  function cancel() {
    if (readController) { readController.abort(); readController=null; busy=false; lastRead=0; }
    if (staleTimer!=null) { clearTimeout(staleTimer); staleTimer=null; }
    revokePlayback();
  }
  return {element,refresh,cancel,showRecorded,sample:()=>sample};
}

function studioCurrentMonitor(id, view) {
  const jobs=state.jobs.filter(job=>job.kind==="studio_video" && jobProjectId(job)===id);
  const openingJob = state.projects.find(project=>project.id===id)?.opening?.video_job;
  const fallback = openingJob?.project_id===id && openingJob.kind==="studio_video"
    ? jobs.find(job=>job.id===openingJob.id) || openingJob : null;
  const job=jobs.find(job=>job.id===view.progressJobId) || jobs.find(job=>ACTIVE.has(job.status)) ||
    (fallback && ACTIVE.has(fallback.status) ? fallback : null) || jobs.find(job=>job.message_id) || fallback;
  if (!job) { view.monitor?.cancel(); view.monitor=null; return null; }
  const matchingOpening = openingJob?.id===job.id && openingJob.project_id===id ? openingJob : null;
  const messageId = job.message_id || matchingOpening?.message_id;
  const recorded = matchingOpening?.recorded_progress;
  let created=false;
  if (view.monitor?.jobId!==job.id || view.monitor.jobStatus!==job.status || view.monitor.jobUpdated!==job.updated_at || view.monitor.messageId!==messageId) {
    view.monitor?.cancel();
    const monitor = {jobId:job.id,jobStatus:job.status,jobUpdated:job.updated_at,messageId,
      ...studioProgressMonitor(id,job.id,{jobStatus:job.status,messageId,recorded:view.recordedProgress,onUpdate:data=>{
        if (view.monitor!==monitor || !messageId || data.message_id!==messageId) return;
        const statuses = {ready:"generated",reused:"reused",generating:"reserved",pending:"pending"};
        view.recordedProgress=studioRecordedProgress({project_id:id,job_id:job.id,message_id:messageId,
          recorded_at:new Date().toISOString(),shot_count:data.total,completed_count:data.completed,
          shots:data.shots.map(row=>({shot_id:row.shot_id,status:statuses[row.status] || "unknown",operation:row.operation}))},id,job.id,messageId);
      },onRead:()=>{
        if (view.monitor===monitor && state.projectId===id && !state.creating && state.step!=="traces") renderJobs();
      }})};
    view.monitor = monitor;
    created=true;
  }
  if ((view.monitor.showRecorded(recorded) || created) && view.monitor.sample()?.recorded) renderJobs();
  return view.monitor;
}
function videoRoutesChanged(previous, proposed) {
  return Object.entries(proposed || {}).some(([purpose, route]) => {
    // Plans made before route snapshots always execute through the Bailian client.
    if (!previous) return route.adapter !== "bailian";
    const frozen = previous[purpose];
    return !frozen || Object.keys(route).some(key => route[key] !== frozen[key]);
  });
}

function studioVideoPlan(data, catalog, view) {
  const execution=data.video_execution;
  const frozenPlan=execution?.commission_id && (data.projection==="video" || !data.video_plan);
  const plan = frozenPlan ? execution.plan : data.video_plan;
  const panel = node("section", "studio-video-plan");
  panel.append(node("h3", "", execution?.commission_id?"本次拍摄计划 · 已确认":"本次拍摄计划"));
  if (!plan?.plan_id || ![plan.new_shots,plan.retake_shots,plan.reuse_shots].every(Array.isArray)) {
    panel.append(note(data.video_plan_error || "镜头计划尚未就绪，请刷新记录。", true));
    return panel;
  }
  const id = state.projectId;
  const extra = plan.extra_retake;
  if (extra && (!extra.proposal_id || !Array.isArray(extra.shot_ids) || !extra.shot_ids.length)) {
    panel.append(note("额外重拍申请尚未就绪，请刷新记录。", true));
    return panel;
  }
  function shotGroups(p) {
    return [[p.continuation ? "补拍" : "新拍", p.new_shots], ["重拍", p.retake_shots], [p.continuation ? "沿用" : "复用", p.reuse_shots]];
  }
  const groups = shotGroups(plan);
  if (plan.generation_tasks) {
    panel.append(note("生成请求总上限 " + plan.generation_request_limit + " 次，首拍和重拍共用。"));
    panel.append(studioRows(plan.generation_tasks.map(task => [
      task.id + " · " + task.duration_s + " 秒", task.creative_shots.join(" → ")
    ])));
  }
  function showRoutes(routes) {
    const labels = {"video.text_to_video": "文生视频", "video.reference_to_video": "参考生视频"};
    panel.append(studioRows(Object.entries(routes).map(([purpose, route]) =>
      [labels[purpose] || purpose, route.model_id + " · " + route.provider + " / " + route.account])));
  }
  const frozen=execution?.plan?.acceptance_loop?.shot_ids;
  const sameScope=execution?.commission_id && Array.isArray(frozen) && JSON.stringify([...frozen].sort())===JSON.stringify([...(plan.acceptance_loop?.shot_ids||[])].sort());
  const sameVersions=execution?.commission_id && sameAssetPin(execution.script_ref,catalog.script_ref) && sameAssetPin(execution.spec_ref,catalog.spec);
  if(execution?.commission_id){
    if (execution.plan?.video_routes) showRoutes(execution.plan.video_routes);
    else panel.append(note("此委托创建于模型路由登记前，继续执行时使用百炼视频配置。"));
    const confirmed = execution.plan;
    if (confirmed?.acceptance_loop) panel.append(note("每镜头最多自动重拍 " + Math.min(1,confirmed.acceptance_loop.max_repairs_per_shot) + " 次。复验仍未通过时停止，并记录具体理由。"));
    if (Array.isArray(confirmed?.new_shots) && Array.isArray(confirmed.retake_shots) && Array.isArray(confirmed.reuse_shots)) {
      panel.append(studioRows(shotGroups(confirmed).map(([label, shots]) => [label + " " + shots.length + " 个",shots.join("、") || "无"])));
    }
    const review = activeCommissionReview(id,execution.commission_id);
    if (review) {
      panel.append(note(reviewProgress(review)),button("向视频 Agent 补充要求",()=>setStep("agent"),{quiet:true}));
      return panel;
    }
    panel.append(note("继续后，工作台核对当前委托并恢复制作。需要重拍时，使用本委托已确认的视频生成模型和剩余额度。"));
    if(execution.resumable)panel.append(button("继续当前制作委托",()=>studioContinueConversation(id,execution.commission_id),{primary:true,disabled:view.pending||view.loading||!!view.error||busyProject()||!keyReady("video.decision")}));
    else if(execution.blockers?.length)panel.append(note(execution.blockers.join("；"),true));
    panel.append(button("向视频 Agent 补充要求",()=>setStep("agent"),{quiet:true}));
    const receipt = view.continuationReceipt?.commissionId === execution.commission_id ? view.continuationReceipt : null;
    const jobId = receipt ? receipt.jobId : execution.last_execution?.id;
    const job = jobId && state.jobs.find(item=>item.id===jobId && jobProjectId(item)===id);
    const rejections = execution.submission_rejections || [];
    const chatEndedWithoutHandoff = job?.kind === "pi_video" && !job.commission_id &&
      ["succeeded", "interrupted"].includes(job.status);
    let feedback = job?.result?.summary || job?.message || (receipt ? (view.pending ? "正在向视频 Agent 提交继续要求…" : view.message) : execution.last_execution?.message);
    if (job?.error) feedback = errorText(job.error);
    else if (chatEndedWithoutHandoff) feedback = "上次继续请求只产生了对话回复，制作尚未接续。";
    else if (rejections.length && job?.id===execution.last_execution?.id && job.status==="interrupted") feedback = "本次制作已中断，原镜头已保留。";
    if (feedback) {
      const status = node("div","studio-feedback");
      status.setAttribute("role","status");
      const refusalTrace = rejections.length && execution.last_execution?.worker_trace_id;
      append(status, note(feedback, !!job?.error), studioTrace(refusalTrace || job?.trace_id || (receipt ? view.lastTrace : execution.last_execution?.trace_id)));
      panel.append(status);
    }
    for (const refusal of rejections) {
      const provider = {"volcengine-ark":"火山方舟", "aliyun-bailian":"阿里云百炼"}[refusal.provider] || refusal.provider;
      const reason = refusal.code==="InputImageSensitiveContentDetected.PrivacyInformation" ? "供应商认为参考图可能含真人" : refusal.code;
      const references = (refusal.references || []).map(ref => `${ref.subject_id || ref.asset_id} ${ref.revision_id}`).join("、");
      panel.append(note(`${refusal.shot_id} · ${provider}拒绝受理（HTTP ${refusal.http_status}）${reason ? " · " + reason : ""}${references ? "。涉及素材：" + references : ""}`, true));
      if (refusal.provider_request_id) {
        const details = node("details");
        details.append(node("summary", "", `${refusal.shot_id} 供应商回执`), studioRows([["模型",refusal.model],["错误码",refusal.code || "未提供"],["请求 ID",refusal.provider_request_id]]));
        panel.append(details);
      }
    }
    if (frozenPlan) {
      if (data.projection==="video" && (execution.resumable || execution.exhausted_budget)) {
        panel.append(button(view.archiveLoading?"正在读取拍摄计划…":"查看当前模型的拍摄计划", () => {
          if (state.projectId === id) return openProject(id,{preserveStep:true,full:true});
        }, {quiet:true, disabled:view.archiveLoading || view.pending}));
      } else if(data.video_plan_error) panel.append(note(data.video_plan_error,true));
      return panel;
    }
    if (extra?.partial && extra.partial.request_id !== execution.commission_id) {
      panel.append(note("额外重拍申请对应的委托已变更，请刷新拍摄计划。",true));
      return panel;
    }
    const laterBudget = execution.exhausted_budget && plan.continuation?.request_id === execution.commission_id &&
      plan.plan_id !== execution.plan?.plan_id && !plan.blocked_reason && !plan.unresolved_shots?.length;
    if (sameScope && sameVersions && !extra && !laterBudget) {
      if (!execution.resumable) return panel;
      if (!videoRoutesChanged(execution.plan?.video_routes,plan.video_routes)) {
        panel.append(note(confirmed?.video_routes ? "当前配置和已确认委托使用相同的视频路由，可继续当前委托。" : "当前配置仍使用百炼视频，可继续当前委托。"));
        return panel;
      }
      if (plan.continuation?.request_id !== execution.commission_id) {
        panel.append(note("视频模型已变更，尚无可核对的恢复记录来确定复用与补拍范围。",true));
        return panel;
      }
    }
    panel.append(node("h3","","待确认的新拍摄计划"));
  }
  if (plan.video_routes) showRoutes(plan.video_routes);
  if (plan.continuation) panel.append(note("可沿用 " + plan.reuse_shots.length + " 个已有镜头。已有候选仍需验收，未通过项在本次确认的额度内修复。"));
  if (plan.acceptance_loop) panel.append(note("镜头及相邻衔接全部通过后才交付整片。每镜头最多自动重拍 " + plan.acceptance_loop.max_repairs_per_shot + " 次。"));
  if (plan.blocked_reason) panel.append(note(plan.blocked_reason,true));
  function addAction(title, breakdown, proposalId, existingOnly=false) {
    const counts = breakdown.filter(([, shots]) => shots.length).map(([name, shots]) => name + " " + shots.length + " 个").join("，");
    panel.append(studioRows(breakdown.map(([name, shots]) => [name + " " + shots.length + " 个", shots.join("、") || "无"])));
    if(plan.acceptance_loop){
      const policy=plan.acceptance_loop,scope=existingOnly?plan.reuse_shots.length:policy.shot_ids.length;
      const initial=existingOnly?0:breakdown.filter(([name])=>/新拍|补拍|重拍/.test(name)).reduce((sum,[,ids])=>sum+ids.length,0);
      const ids=existingOnly ? plan.reuse_shots : policy.shot_ids;
      const remaining=ids.filter(sid=>!policy.previously_retried_shots?.includes(sid)).length;
      const calls=(!proposalId && !existingOnly ? plan.execution_limits?.max_video_generations : null) ?? Math.min(plan.generation_request_limit ?? Infinity, initial + remaining * Math.min(1,policy.max_repairs_per_shot));
      panel.append(note("本次确认上限：生成调用 "+calls+" 次，制作循环模型调用 "+Math.max(30,scope*(6+3*policy.max_repairs_per_shot)+20)+" 次；每镜头最多本地修改 "+policy.max_local_edits_per_shot+" 次。"));
    }
    const canStart = counts && !plan.blocked_reason && !view.pending && !view.loading && !view.error && !busyProject() && keyReady("video.decision")
      && !catalog.pending_script_ref && catalog.spec?.sha256;
    const submit = button(title + (counts ? " · " + counts : ""), () => {
      if (submit.disabled || busyProject() || state.projectId !== id) return;
      const current = view.data?.video_plan;
      if (current?.plan_id !== plan.plan_id || current.extra_retake?.proposal_id !== extra?.proposal_id) {
        view.message = "拍摄计划已更新，请查看最新镜头清单后再开始。"; studioRedraw(id); return;
      }
      const body = {plan_id:plan.plan_id, script_ref:catalog.script_ref || null,
        ...(catalog.spec.revision_id ? {spec_ref:catalog.spec} : {project_sha256:catalog.spec.sha256})};
      if (proposalId) body.proposal_id = proposalId;
      if (existingOnly) body.repair_existing_only = true;
      return studioWrite(id, () => request(studioPath(id) + "/video", {method:"POST",body}), result => {
        view.progressJobId=result.job.id;
        acceptJob(result.job, "视频制作委托已提交");
        return proposalId ? "已同意这次额外重拍。进度见制作动态。" : "拍摄计划已提交。进度见制作动态。";
      }, {refreshOnError:true});
    }, {primary:true, disabled:!canStart});
    panel.append(submit);
  }
  if (!extra || (!extra.partial && (plan.new_shots.length || plan.retake_shots.length))) {
    addAction(execution?.commission_id?"确认新的制作计划":plan.continuation ? (plan.new_shots.length ? "继续制作" : "继续验收与导出") : catalog.production_ref ? "更新制作" : "开始制作", groups);
  }
  if (plan.continuation && plan.reuse_shots.length && !extra?.partial && !execution?.commission_id) {
    addAction("自主修复已有镜头", [["检查并修复",plan.reuse_shots]], null, true);
    panel.append(note("仅处理以上已有镜头，其余镜头暂不生成。未通过项会自主换策略并复验；缺少的相邻镜头仍保留为受阻，全片预览须全部验收通过。"));
  }
  if (extra) {
    panel.append(node("h4", "", "等待你同意额外重拍"), node("p", "", extra.reason),
      node("p", "", "申请额外重拍的镜头　" + extra.shot_ids.join("、")));
    const retakes = new Set(extra.shot_ids);
    const breakdown = extra.partial ? [["重拍", extra.shot_ids], ["复用", extra.reused_shots || []]] :
      [["新拍",plan.new_shots], ["重拍",[...new Set([...plan.retake_shots, ...extra.shot_ids])]],
        ["复用",plan.reuse_shots.filter(shot => !retakes.has(shot))]];
    addAction("同意额外重拍并制作", breakdown, extra.proposal_id);
  }
  panel.append(node("p","studio-cost",plan.acceptance_loop ? "视频生成、自动修复重拍与模型评估按实际调用计费。修复额度内由 Agent 自主执行。" : "视频生成与模型评估按实际调用计费。"));
  return panel;
}

async function studioContinueConversation(id,commissionId){
  const view=studioLocal(id);if(view.pending||busyProject()||state.projectId!==id)return;
  if(view.loading||view.error||!view.data?.video_execution?.resumable||view.data.video_execution.commission_id!==commissionId){view.message="当前委托已更新，请重新核对。";studioRedraw(id);return;}
  const text="继续当前制作委托 "+commissionId+"，沿用原冻结范围和剩余额度，按验收证据继续修复，全部必过项通过后再交付整片。";
  view.continuationAttempt=view.continuationAttempt?.commissionId===commissionId?view.continuationAttempt:{commissionId,id:window.crypto.randomUUID().replaceAll("-","")};
  const receipt = {commissionId};
  await studioWrite(id,()=>request("/api/pi/messages",{method:"POST",body:{scope:{project:id},role:"video",mode:"work",text,message_id:view.continuationAttempt.id,continue_commission:commissionId}}),result=>{
    if(result.job)acceptJob(result.job,"已将继续要求送入当前视频 Agent 会话");
    receipt.jobId = result.message?.job_id || (result.queued ? null : result.job?.id);
    view.continuationAttempt=null;
    if (result.queued || result.message?.status==="queued") return "继续要求已排队，等待视频 Agent 处理。";
    if (result.message?.status==="not_executed") return "继续要求未进入执行，请查看关联任务的停止原因。";
    if (result.duplicate) return "已找到同一条继续要求，处理进度见原会话。";
    return "视频 Agent 已收到继续要求。";
  }, {continuation:receipt});
}

function studioExecutionLimits(data){
  const execution=data.video_execution,limits=execution?.limits||data.video_plan?.execution_limits;
  const box=node("div","studio-execution-limits");
  box.append(note(execution?.commission_id?"以下限制来自当前冻结制作委托。补充要求和恢复执行沿用它们；超出范围须另行确认新的拍摄计划。":"以下是待确认拍摄计划的执行限制，确认计划时与制作范围一起冻结。"));
  const value=(number,suffix)=>Number.isInteger(number)?number+suffix:"尚未登记";
  const rows=[["制作委托",execution?.commission_id||"尚未确认"],["生成调用上限",value(limits?.max_video_generations," 次")],
    ["每镜头自动重拍",value(limits?.max_repairs_per_shot," 次")],["每镜头本地修改",value(limits?.max_local_edits_per_shot," 次")],
    ["制作循环模型调用上限",value(limits?.max_model_calls," 次")]];
  if(execution?.usage)rows.push(["已预留生成调用",value(execution.usage.reserved_video_generations," 次")],["剩余生成调用",value(execution.usage.remaining_video_generations," 次")],
    ["制作循环已用模型调用",value(execution.usage.model_calls," 次")]);
  box.append(studioRows(rows));
  return studioDetails("执行限制 · "+(execution?.commission_id?"当前委托":"待确认计划"),box);
}
async function studioReadShotReviews(id, requestId) {
  const view = studioLocal(id);
  if (view.shotReviews?.requestId===requestId && view.shotReviews.loading) return view.shotReviews.promise;
  const read = {requestId,loading:true};
  view.shotReviews = read;
  read.promise = Promise.resolve().then(() => request(studioPath(id) + "/shot-reviews?" + new URLSearchParams({request_id:requestId}))).then(result => {
    if (result.project_id !== id || result.request_id !== requestId) throw Error("验收记录和当前委托不符，请刷新。");
    read.result=result;
  }).catch(error => { read.error=error.message; }).finally(() => {
    read.loading=false;
    if (view.shotReviews === read) studioRedraw(id);
  });
  studioRedraw(id);
  return read.promise;
}
function studioShotApprovals(partial, id, view, includeReadOnly=false) {
  const review = activeCommissionReview(id,partial?.request_id);
  const rows = partial?.shots?.filter(shot => includeReadOnly || shot.can_approve || shot.human_approved) || [];
  if (!rows.length && !includeReadOnly) return null;
  const panel = node("section","panel studio-shot-approvals");
  panel.dataset.requestId=partial.request_id;
  panel.tabIndex=-1;
  const versionCount = partial.version_count;
  panel.append(studioSection(Number.isInteger(versionCount) ? "拍摄版本与验收报告" : "当前委托的镜头与验收报告",
    Number.isInteger(versionCount) ? partial.total + " 个镜头组 · " + versionCount + " 个可播放版本" : "查看选用片段和对应的验收报告。"));
  if (review) panel.append(note(reviewProgress(review)));
  if (!rows.length) panel.append(note(partial.blocked_reason || "当前尚无待审的失败报告。可在制作进度中查看镜头和验收状态。"));
  const retryDelivery = (partial.all_shots_accepted || (partial.available_count === partial.total && partial.shots.length === partial.total
    && partial.shots.every(shot => shot.status === "PASS" || shot.human_approved)))
    && !partial.review_jobs?.some(job=>ACTIVE.has(job.status));
  for (const shot of rows) {
    const entry = node("article","studio-shot");
    Object.assign(entry.dataset,{requestId:partial.request_id,shotId:shot.shot_id,mediaSha:shot.media_sha256 || "",reportSha:shot.report_sha256 || ""});
    entry.tabIndex=-1;
    entry.append(node("h4","",shot.shot_id + (shot.human_approved ? " · 已人工通过" : shot.can_approve && !review ? " · 待人工审看" : "")));
    if (shot.blocked_reason) entry.append(note(shot.blocked_reason));
    const jobId = shot.job_id || partial.job_id;
    const grid=node("div","studio-take-grid");
    let selectedCard=null, retakes=0;
    for (const take of shot.versions || [{...shot,selected:true}]) {
      if (take.shot_id !== shot.shot_id) continue;
      const selected=take.selected===true && take.media_sha256===shot.media_sha256;
      const label=take.kind==="retake" ? "第 " + (++retakes) + " 次重拍" :
        ({initial:"首拍",reuse:"复用版本",edit:"本地修改"})[take.kind] || "镜头视频";
      const version=take.media_sha256?.slice(0,12) || "";
      const card=node("section","studio-take");
      Object.assign(card.dataset,{takeKind:take.kind || "current",selected:String(selected),mediaSha:take.media_sha256 || "",
        reportSha:take.report_sha256 || ""});
      card.append(node("h5","",label + (selected ? " · 当前选用" : " · 历史版本")));
      const source=studioClipPreviewUrl(id,jobId,{...take,status:"ready"});
      if (source) {
        const preview=node("div","live-preview"), video=node("video","video-player");
        video.controls=true; video.preload="none"; video.playsInline=true; video.src=source;
        video.dataset.takeId=take.task_id || shot.shot_id;
        video.setAttribute("aria-label",shot.shot_id + " 待审片段 " + label + " " + version);
        preview.append(video); card.append(preview);
        if (selected && /^[a-f0-9]{64}$/.test(shot.report_sha256 || "") && take.report_sha256===shot.report_sha256) selectedCard=card;
      }
      if (take.selection_reason==="rejected_regression") card.append(note("本次重拍在关键验收项上退步，已保留先前版本。"));
      const report=selected ? shot.report : take.report;
      if (report) {
        const checks=node("details","studio-take-report");
        const status=({PASS:"通过",FAIL:"未通过",UNKNOWN:"待核实"})[report.status];
        checks.append(node("summary","",(selected ? "当前验收报告" : "此版本验收报告") + (status ? " · " + status : "")));
        for (const result of report.results || []) {
          if (result.required && result.status!=="PASS") checks.append(note(criterionName(result) + " · " + (result.reason || result.status)));
        }
        if (version) checks.append(node("code","studio-muted","视频版本 " + version));
        card.append(checks);
      } else if (source) card.append(note("此版本暂无可核对的验收报告。"));
      grid.append(card);
    }
    entry.append(grid);
    const retry = retryDelivery && shot === rows[0] && shot.human_approved;
    if (selectedCard && !review && ((shot.can_approve && !shot.human_approved) || retry)) {
      const body = {request_id:shot.request_id || partial.request_id,shot_id:shot.shot_id,
        media_sha256:shot.media_sha256,report_sha256:shot.report_sha256};
      const approve = button(retry ? "重新检查并合片" : "人工通过", async () => {
        await studioWrite(id,
          () => request(studioPath(id) + "/shots/approve",{method:"POST",body}),
          result => {
            void view.monitor?.refresh(true);
            const delivery = result.delivery;
            if (delivery?.delivered) return shot.shot_id + " 已人工通过。成片已生成，可以在“人工预览”中播放。";
            const reasons = (delivery?.issues || []).map(issue => typeof issue === "string" ? issue :
              [issue.shot_id,issue.reason || issue.criterion_id].filter(Boolean).join(" · ")).filter(Boolean);
            return shot.shot_id + " 已人工通过。" + (reasons.length ? "整片待交付：" + reasons.join("；") : "等待其余镜头及衔接验收。");
          }, {refreshOnError:true});
        if (view.shotReviews?.requestId === body.request_id && state.projectId===id) await studioReadShotReviews(id,body.request_id);
      }, {small:true,primary:true,disabled:view.pending || view.loading});
      if (!retry) approve.setAttribute("aria-label","人工通过 " + shot.shot_id + " 当前视频版本");
      selectedCard.append(approve);
    }
    panel.append(entry);
  }
  return panel;
}
function studioVideo(data, catalog, view) {
  const box = node("div", "studio-inspector");
  const production = studioProduction(data);
  const spec = catalog.spec?.revision_id ? catalog.spec : null;
  box.append(studioSection("视频制作委托"));
  const monitor=studioCurrentMonitor(state.projectId,view);
  if (monitor) { box.append(monitor.element); void monitor.refresh(); }
  let openedReview=false;
  if (data.video_execution?.commission_id) {
    const commission = data.video_execution.commission_id;
    const read = view.shotReviews?.requestId === commission ? view.shotReviews : null;
    const execution=data.video_execution;
    if (!read && execution.usage?.reserved_video_generations>0 && ["failed","interrupted","succeeded"].includes(execution.last_execution?.status || execution.status)) {
      queueMicrotask(() => {
        if (state.projectId===data.project_id && currentPhase()==="video" && view.data?.video_execution?.commission_id===commission &&
            view.shotReviews?.requestId!==commission) void studioReadShotReviews(data.project_id,commission);
      });
    }
    box.append(button(read?.loading?"正在读取拍摄版本…":read?.result?"刷新拍摄版本与验收报告":"查看全部拍摄版本与验收报告",
      () => studioReadShotReviews(data.project_id,commission),{quiet:true,disabled:read?.loading || view.pending}));
    if (read?.error) {
      const issue = node("section","panel studio-shot-approvals");
      issue.dataset.requestId=commission; issue.tabIndex=-1;
      issue.append(note(read.error,true));
      box.append(issue);
    }
    if (read?.result?.delivered) box.append(button("预览成片",()=>setStep("review"),{primary:true}));
    if (read?.result && (!read.result.delivered || read.result.shots?.length)) {
      box.append(studioShotApprovals(read.result,data.project_id,view,true));
      openedReview=true;
    }
  }
  if (!openedReview) append(box,studioShotApprovals(data.incomplete_production,state.projectId,view));
  if (data.preview_gate?.ready) box.append(button("预览成片",()=>setStep("review"),{primary:true}));
  if (catalog.pending_script_ref) box.append(note("剧本 " + catalog.pending_script_ref.revision_id + " 已发布。当前分镜对应 "
    + (catalog.script_ref?.revision_id || "原工程") + "，等待新版制作规格后可委托。", true));
  box.append(studioVideoPlan(data,catalog,view));
  box.append(studioExecutionLimits(data));
  const inputs=data.video_execution?.reference_inputs||[];
  if(inputs.length){
    const evidence=node("section","panel studio-reference-inputs");
    evidence.append(studioSection("模型输入编排","原参考要求全部保留；参考板核验通过仅表示可提交，成片仍须自动验收。"));
    for(const input of inputs){
      const row=node("article","studio-shot");
      row.append(node("h3","",input.shot_id+" · "+(input.ready?(input.boards?.length?"编排输入已核验":"原参考可直接输入"):"等待兼容输入")));
      row.append(note("原图片参考 "+input.original_image_count+" 张 → "+(input.provider_image_count==null?"尚未准备":input.provider_image_count+" 个模型视觉槽")));
      if(input.trace_id)row.append(studioTrace(input.trace_id,"查看输入核验 Trace ↗"));
      for(const board of input.boards||[])row.append(studioLink(board.url,"查看图"+board.slot+"参考板："+board.sources.map(s=>s.subject_id+" / "+s.purpose).join("、")));
      for(const check of input.checks||[])row.append(note(check.status+" · "+check.actual,check.status!=="PASS"));
      if(!input.ready)for(const strategy of input.strategies||[])if(strategy.error||strategy.checks?.length||strategy.blockers?.length)
        row.append(note(strategy.strategy+" · "+strategy.status+" · "+(strategy.error||strategy.blockers.join("；")||(strategy.checks||[]).filter(c=>c.status!=="PASS").map(c=>c.actual).join("；")),true));
      evidence.append(row);
    }
    box.append(evidence);
  }
  if(data.video_execution?.reference_input_error)box.append(note("模型输入证据读取失败："+data.video_execution.reference_input_error,true));
  if (!keyReady("video.decision")) box.append(note("视频 Agent 的对话模型缺少密钥，暂时无法委托制作。", true));
  box.append(studioRows([["剧本输入", studioPin(catalog.script_ref) || "当前工程内的剧本 · 首次委托时登记版本"],
    ["制作规格", studioPin(spec) || "当前工程快照 · " + (catalog.spec?.sha256?.slice(0,12) || "缺少指纹")],
    ["上次制作", studioPin(catalog.production_ref) || "首次制作"]]));
  const shotList = node("div", "studio-shot-list");
  for (const shot of catalog.shots || []) {
    const row = node("div", "studio-shot");
    const refs = shot.required_refs || [];
    const actual = production?.shots?.find(item => item.id === shot.id);
    const summary = node("div", "studio-shot-summary");
    append(summary, node("code", "", shot.id), node("span", "", shot.description), node("small", "", refs.length ? refs.length + " 个固定引用" : "未绑定媒体"));
    row.append(summary);
    if (refs.length) row.append(node("p", "studio-pins", refs.map(studioPin).join(" · ")));
    if (actual) row.append(studioDetails("制作记录与实际参考", studioActualShot(actual)));
    shotList.append(row);
  }
  box.append(shotList);
  if (data.incomplete_production || data.production_review || data.productions?.length) {
    box.append(studioDetails("Agent 自动验收与修复记录", studioReview(data,catalog,true)));
  }
  return box;
}
function studioReview(data, catalog, internal=false) {
  const box = node("div", "studio-inspector");
  const ready = data.preview_gate?.ready === true;
  append(box, studioSection(internal ? "Agent 自动验收与修复记录" : "人工预览",
    ready ? "整片验收已通过，可以预览并确认。" : internal?"可在本页的制作进度中预览已生成的镜头，在此查看验收问题和修复记录。":"整片人工预览尚未解锁。已生成的镜头可在视频制作页逐个预览。"));
  if(!internal && !ready){
    box.append(note(data.preview_gate?.reason || "所有镜头及衔接必过项通过后开放整片预览。",true));
    box.append(button("查看镜头预览与验收记录",()=>setStep("video"),{primary:true}));
    return box;
  }
  const partial = data.incomplete_production || data.production_review;
  if (partial) {
    box.append(studioRows([["已有素材", partial.available_count + " / " + partial.total + " 个镜头"],
      ["自动验收", partial.reviewed_count + " / " + partial.available_count + " 个已有镜头已出结论"],
      ["参考与视频内容检查", (partial.content_reviewed_count || 0) + " / " + partial.available_count + " 个镜头已有内容报告"],
      ["剩余制作", partial.missing_shots.join("、") || "无"], ["制作状态", ready ? "验收已通过 · 可人工预览" : "等待镜头及衔接通过"]]));
    const reviewJob = partial.review_jobs?.find(job=>ACTIVE.has(job.status));
    if (reviewJob) box.append(note(reviewJob.message || "正在自动验收已有镜头，结论将逐个更新。"));
    if (partial.autonomy?.version) {
      const work = partial.autonomy;
      box.append(studioRows([["仍可自主修复", (work.actionable || []).length + " 项"],
        ["待补证据", (work.blocked || []).length + " 项"],
        ["已到修复上限", (work.exhausted || []).length + " 项"],
        ["剩余重拍额度", String(work.remaining_video_generations ?? "待核对")]]));
      for (const item of work.coordination || []) {
        const reply = item.response?.response;
        if (reply) box.append(note("剧本 Agent 回应：" + reply.explanation));
      }
      for (const problem of Object.values(work.coordination_errors || {})) {
        box.append(note("Agent 协作受阻：" + problem.reason + "（已记录 " + problem.attempts + " 次处理）", true));
      }
      for (const selection of work.candidate_selection || []) {
        box.append(note(selection.shot_id + "：新候选出现关键内容退步，项目已自动恢复较好的历史候选，并重新检查相邻衔接。"));
      }
      for (const item of [...(work.blocked || []), ...(work.exhausted || [])]) {
        box.append(note(item.shot_id + " · " + criterionName(item) + "：" +
          (item.operation?.reason || item.reason || "具体证据见镜头报告") +
          (item.blocker_kind === "authorized_repair_limit" ? "（已到本轮重拍上限，仍未通过）" :
            item.blocker_kind === "local_edit_limit" ? "（已到本轮本地修改上限，仍未通过）" : "")));
      }
    }
    for (const repair of partial.repair_history || []) {
      const row=node("div","studio-check");
      append(row,node("strong","",repair.shot_id + " · " +
        ({prompt_and_locked_references:"定向提示词与参考重拍",local_black_tail:"本地视频资产修改"}[repair.strategy] || repair.strategy)),
        node("p","",({started_or_unknown:"执行结果待核实",reviewed:"已重拍并复验",reviewed_rejected_regression:"新候选出现退步，保留原候选",provider_failed:"供应商已确认失败"}[repair.status] || repair.status) +
          " · 原结论 " + (repair.source_status || "无候选") + " → 新结论 " + (repair.result_status || "尚未取得")));
      if (repair.strategy_design) {
        const design=repair.strategy_design;
        const family={timed_action_beats:"动作时间编排",blocking_and_paths:"人物走位与路径",camera_and_visibility:"机位与动作可见性",reference_composition:"参考素材组织"}[design.strategy_family] || "新拍摄方案";
        row.append(node("p","",family + "：" + design.change_from_previous));
      }
      if (repair.retake_failure) row.append(note(repair.retake_failure,true));
      const trace=studioTrace(repair.trace_id,"本轮修复 Trace ↗");if(trace)row.append(trace);box.append(row);
    }
    for (const shot of partial.shots) {
      const card = node("details", "studio-delivery"); card.open = shot.shot_id === (selectedShot()?.id || partial.shots[0]?.shot_id);
      card.append(node("summary", "", shot.shot_id + " · " + (shot.review_status === "completed" ?
        ({PASS:"自动验收通过",FAIL:"自动验收未通过",UNKNOWN:"自动验收待补证据"}[shot.status] || shot.status) : "等待自动验收") +
        (shot.human_approved ? " · 已人工通过" : "") +
        (shot.review_status === "completed" ? (shot.human_confirmed ? " · 已确认已阅" : ready ? " · 可人工预览" : shot.can_approve ? " · 可人工审看" : shot.human_approved ? "" : " · Agent 待处理") : "")));
      if (ready && assetUrl(shot.preview_url)) card.append(player(assetUrl(shot.preview_url), shot.shot_id + " 已有视频"));
      if (shot.report) {
        card.append(renderReport({...shot.report, report_url:shot.report_url,
          checks:shot.report.results.map(row=>({...row,id:row.criterion_id,expected:row.expectation || ""}))}));
        const actions = node("div", "studio-actions");
        const pid=state.projectId, view=studioLocal(pid);
        if (ready) actions.append(button(shot.human_confirmed ? "已确认已阅" : "确认已阅此结论", () => studioWrite(pid,
          () => request(studioPath(pid) + "/review-confirm", {method:"POST",body:{shot_id:shot.shot_id,report_sha256:shot.report_sha256}}),
          () => "已保存你对这份自动验收结论的确认。"), {small:true,disabled:shot.human_confirmed || view.pending || !!reviewJob}));
        const trace=studioTrace(shot.trace_id,"自动验收 Trace ↗"); if(trace) actions.append(trace);
        card.append(actions);
      }
      box.append(card);
    }
  }
  const productions = data.productions || [];
  if (!productions.length) {
    if (partial) {
      box.append(note("完整预览片将在全部镜头及衔接验收通过后导出。当前记录用于查看 Agent 的问题与修复进度。"));
      return box;
    }
    const exportUrl = assetUrl(state.project.export?.video_url);
    const readyCount = shots().filter(readyCandidate).length, reports = shots().filter(s=>s.report).length;
    box.append(studioRows([["输入", "已选镜头 " + readyCount + " / " + shots().length], ["输出", reports + " 份镜头报告 · " + (exportUrl ? "工程已有预览片" : "尚无预览片")],
      ["协作交付", "尚无已发布的制作版本"], ["下一步", "查看镜头验收报告和仍待确认的检查项。"]]));
    if (exportUrl && ready) box.append(player(exportUrl, "当前工程预览片"));
    if(internal)box.append(button("查看本地诊断工具",()=>setStep("checks"),{small:true}));
    return box;
  }
  for (const [index,p] of productions.entries()) {
    const details = node("details", "studio-delivery"); details.open = index === 0;
    const title = [p.revision_id, studioPin(p.script_ref)].filter(Boolean).join(" · ");
    details.append(node("summary", "", title + (p.media_accepted === true ? " · 媒体验收通过" : " · 媒体验收待确认")));
    const facts = studioRows([["制作规格", studioPin(p.spec_ref) || "该版未登记固定规格"],["沿用的上一版制作",studioPin(p.previous_ref) || "无"],
      ["变更的剧本来源",(p.changed_sources || []).join("、") || "无记录"],["重做镜头", (p.affected || []).join("、") || "无"],
      ["复用镜头", (p.reused || []).join("、") || "无"], ["预览位置", p.preview_path || "尚无预览文件"]]);
    details.append(facts);
    const edges = p.recheck_edges || [];
    if (edges.length) details.append(node("p", "studio-shots", "复查衔接　" + edges.map(pair => pair.join(" → ")).join("、")));
    for (const check of p.checks || []) {
      const detail = node("div", "studio-check");
      append(detail, node("strong","",check.name), pill(check.status), node("p","",typeof check.detail === "object" ? JSON.stringify(check.detail) : check.detail), node("code","",check.path));
      const links = node("div","studio-actions");
      if (check.human_approval) {
        detail.append(pill("PASS","已人工通过"));
        append(links,studioTrace(check.human_approval.trace_id,"人工通过 Trace ↗"));
      }
      if (assetUrl(check.url)) links.append(studioLink(assetUrl(check.url),"查看检查报告 ↗"));
      if (assetUrl(check.clip_url)) links.append(studioLink(assetUrl(check.clip_url),"播放衔接片段 ↗"));
      append(links,studioTrace(check.trace_id,"复查 Trace ↗")); detail.append(links); details.append(detail);
    }
    const url = assetUrl(p.preview_url);
    if (url && ready && p.media_accepted === true){
      details.append(player(url, "制作 " + p.revision_id + " 预览片"));
      const download=studioLink(url,"下载已验收预览 ↓");download.download="preview.mp4";details.append(download);
    }
    const trace = studioTrace(p.trace_id, "制作 Trace ↗"); if (trace) details.append(trace);
    box.append(details);
  }
  return box;
}
function studioEvidence(data, section) {
  const stage = data.stages?.find(item => item.id === section);
  if (!stage) return null;
  const rows = [["输入",stage.inputs], ["输出",stage.outputs], ["保存位置",stage.storage], ["下游使用",stage.next_use]];
  return studioDetails("本环节的输入、输出与保存位置", studioRows(rows.map(([label,value]) => [label, Array.isArray(value) ? value.join("\n") || "尚无记录" : value || "尚无记录"])));
}
function studioHandoff(data, catalog) {
  const section = node("section", "studio-handoff");
  const heading = node("div","studio-handoff-heading");
  append(heading,node("h3","","两个 Agent 的交接记录"), catalog.run_id && data.handoff?.run_path ? studioLink("/collaborations#id=" + encodeURIComponent(catalog.run_id),"协作详情 ↗") : null);
  section.append(heading);
  const events = data.handoff?.events || [];
  if (!events.length) {
    section.append(node("p", "studio-muted", "此故事暂无发布或接单消息。"));
    return section;
  }
  const list = node("div","studio-events");
  const agents = {script:"剧本 Agent", video:"视频制作 Agent", commissioner:"制作委托方", assets:"资产库"};
  const types = {"production.requested":"发出制作委托", "production.result":"交付制作结果",
    "asset.revision_published":"发布剧本版本", "script.change_requested":"提交改稿要求",
    "production.blocked":"制作受阻", "production.recovery_requested":"恢复制作委托"};
  for (const event of events) {
    const item = node("div", "studio-event");
    const receivers = event.consumer ? [event.consumer] : Object.keys(event.deliveries || {});
    const recipient = receivers.map(value => agents[value] || value).join("、") || "发布到消息总线";
    append(item, node("strong","",[agents[event.producer] || event.producer,recipient].join(" → ")), node("span","",types[event.type] || event.type),
      node("small","",[studioPin(event.script_ref), studioPin(event.spec_ref), studioPin(event.previous_ref), studioPin(event.production_ref)].filter(Boolean).join(" · ")),
      studioTrace(event.trace_id));
    list.append(item);
  }
  section.append(studioDetails("查看 " + events.length + " 条消息", list));
  return section;
}
function renderStudio() {
  const id = state.projectId, view = studioLocal(id), outer = node("div", "studio-board");
  if (!view.data && !view.error && !view.loading) void studioRefresh(id);
  const data = view.data, catalog = data && studioCatalog(data);
  const head = node("div", "studio-board-head");
  append(head, node("h2","",({assets:"共享资产",video:"视频制作",review:"人工预览",script:"剧本与分镜"})[view.section]), button(view.loading ? "读取中…" : "刷新记录", () => { void studioRefresh(id); studioRedraw(id); }, {quiet:true, small:true, disabled:view.loading || view.pending || view.archiveLoading}));
  append(outer, head);
  if (!data) {
    outer.append(note(view.error || "正在读取当前故事的剧本、资产和制作版本。", !!view.error));
    return outer;
  }
  if (view.error) outer.append(note("刷新失败，当前显示最近读取的记录。" + view.error,true));
  const panel = node("section", "studio-panel");
  panel.dataset.section = view.section;
  const inspector = {script:studioScript, assets:studioAssets, video:studioVideo, review:studioReview}[view.section];
  const detail = view.section==="review"?studioReview(data,catalog,false):inspector(data, catalog, view);
  const projected=state.project?.projection==="video";
  if(view.section==="video" && projected) {
    if(view.archiveError)detail.append(note("完整工程记录读取失败："+view.archiveError,true));
    detail.append(button(view.archiveLoading?"正在读取验收和候选记录…":"查看完整制作记录、候选与验收",()=>openProject(id,{preserveStep:true,full:true}),{quiet:true,disabled:view.archiveLoading}));
  } else if(view.section==="video"){
    for(const [key,label,render] of [["shots","镜头候选与下载",renderShots],["checks","本地诊断工具",renderChecks]]){
      const tools=studioDetails(label,render());tools.dataset.productionTool=key;tools.id="production-tool-"+key;
      tools.open=view.tool===key;
      tools.addEventListener("toggle",()=>{if(tools.open)view.tool=key;else if(view.tool===key)view.tool=null;persistRoute();});
      detail.append(tools);
    }
  }
  const evidence = data.projection==="video" ? null : studioEvidence(data,view.section);
  if (evidence && detail.firstElementChild) detail.firstElementChild.after(evidence); else if (evidence) detail.append(evidence);
  panel.append(detail);
  const feedbackInPlan = view.section==="video" && view.continuationReceipt?.commissionId &&
    view.continuationReceipt.commissionId===data.video_execution?.commission_id;
  if ((view.pending || view.message) && !feedbackInPlan) {
    const message = node("div","studio-feedback"); message.setAttribute("role","status");
    append(message,node("p","",view.pending ? "正在保存…" : view.message),studioTrace(view.lastTrace)); panel.append(message);
  }
  append(outer, panel, data.projection==="video"?null:studioHandoff(data,catalog)); return outer;
}
