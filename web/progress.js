"use strict";

(() => {
  const params = new URLSearchParams(window.location.search);
  const project = params.get("project") || "", job = params.get("job") || "";
  const panel = document.getElementById("observer-panel");
  const back = document.getElementById("observer-back");
  back.href = studioWorkbenchOrigin() + "/";
  if (!/^[a-f0-9]{16}$/.test(project) || !/^[a-f0-9]{32}$/.test(job)) {
    panel.textContent = "请从故事的「视频制作 Agent」打开这次委托的进度。";
    return;
  }
  back.href += "#" + new URLSearchParams({project,step:"video"});
  const monitor = studioProgressMonitor(project,job,{standalone:true,onUpdate:data=>{
    if (data.project_title) {
      document.getElementById("observer-title").textContent = data.project_title;
      document.title = data.project_title + " · 镜头进度 · 片场";
    }
  }});
  panel.append(monitor.element);
  void monitor.refresh();
  const timer = window.setInterval(()=>{ if (!document.hidden) void monitor.refresh(); },5000);
  document.addEventListener("visibilitychange",()=>{ if (!document.hidden) void monitor.refresh(); });
  window.addEventListener("pagehide",()=>window.clearInterval(timer),{once:true});
})();
