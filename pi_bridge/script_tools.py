"""Resumable story tools. Pi writes/repairs; deterministic validators keep authority."""
import copy
import json
import re
from pathlib import Path

from pianchang.agent_loop import Tool
from pianchang.app_service import project_id_for_path, story_constraints, read_json, write_json
from pianchang.collaboration_workflow import Collaboration, dep, ref
from pianchang.script_collab_agent import ScriptWorkspace, publish_script_revision, DRAFT
from pianchang.story_agent import COMPILE_SYSTEM
from pianchang.story_spec import digest_json, validate_compilation
from trace_system import event, traced
from .requirements import explicit_quantities,check_quantities

EMPTY={"type":"object","properties":{}}
SPEC_PATCH = {"type":"object","properties":{
    "changes":{"type":"array","minItems":1,"maxItems":80,"items":{"type":"object",
        "properties":{"path":{"type":"array","minItems":1,"maxItems":20,"items":{}},
                      "value":{}}, "required":["path","value"]}},
    "changes_json":{"type":"string","minLength":2,"maxLength":12000},
}}


class StoryTools:
    def __init__(self, service, session, job, text, *, collaborator=None):
        self.service,self.session,self.job=service,session,job
        self.text=re.sub(r"^\[message_id=[a-f0-9]{32}\]\n","",text)
        self.retry_patch_step = None
        self.source_id=session["source_job"]
        run_id=self.source_id or session["story_id"].removeprefix("story-")
        self.collab=Collaboration(service.root/"outputs/collaborations", run_id,env_file=service.root/".env")
        self.directory=service.root/"outputs/pi/workspaces"/session["story_id"];self.directory.mkdir(parents=True,exist_ok=True)
        if self.source_id:
            self.original=service._story_directory(self.source_id)
            source_job=next(j for j in service.state["jobs"] if j["id"]==self.source_id)
            self.detail={**service._story_record(source_job),"content_status":"published_script" if source_job.get("script_ref") else "saved_story"}
        else:
            project_path,project=service._project(session["project_id"])
            self.original=project_path.parent
            self.detail={"idea":project.get("title","已有故事"),"inputs":{},"content_status":"external_project"}
        self.script_ref=None;self.script=None;self.result=None;self.spec=None;self.existing_publication=None
        self.collaborator=collaborator
        revisions=self.collab.store.revisions(self.collab.story+"/script")
        if revisions:
            asset=self.collab.store.get_asset(self.collab.story+"/script")
            self.script_ref=ref(asset);self.script=asset["content"]
        if self.script is None and self.detail.get("content_status")=="published_script":
            raise ValueError("已发布剧本缺少可核对的版本资产")
        idea=self.detail["inputs"].get("idea") or self.detail["idea"]
        self.workspace=ScriptWorkspace(self.directory/"script",story_id=self.collab.story,goal=idea,
            publish=self._publish,mode="create",script=None,base_revision=None,beat_id=None,
            constraints=story_constraints(self.detail["inputs"]))
        if self.script:self.workspace.candidate=copy.deepcopy(self.script)
        elif (self.directory/"script/candidate.json").is_file():self.workspace.candidate=read_json(self.directory/"script/candidate.json")
        if self.workspace.candidate is None and not self.source_id:
            self.workspace.candidate=project.get("script_source",{}).get("script")
        imported=self.directory/"imported-script.json"
        if not self.script_ref and imported.is_file():
            self.workspace.candidate=read_json(imported);self.workspace.source=copy.deepcopy(self.workspace.candidate)
            self.workspace.mode="import"
        revision_intent=self.directory/"revision-intent.json"
        restoring_revision=False
        if revision_intent.is_file() and (self.directory/"script/candidate.json").is_file():
            intent=read_json(revision_intent)
            if intent.get("base")==self.script_ref:
                self.workspace.candidate=read_json(self.directory/"script/candidate.json")
                self.workspace.publish_started=False;restoring_revision=True
        self.spec_path=self.directory/"spec-candidate.json"
        if self.spec_path.is_file():self.spec=read_json(self.spec_path)
        else:
            candidates=sorted((self.collab.folder/"compile-r1").glob("compile-*-candidate.json"))
            if candidates:self.spec=read_json(candidates[-1])
            elif (self.original/"project.json").is_file():self.spec=read_json(self.original/"project.json")
        self.unknown=[]
        for path in (self.collab.folder/"compile-r1").glob("*-call.json"):
            call=read_json(path)
            if call.get("status")!="COMPLETED":self.unknown.append({"path":str(path),"sha256":digest_json(call),"status":call.get("status"),"action":"旧调用不重放；用已保存候选与证据进行新的定向修复"})
        if (self.original/"studio.json").is_file():
            published=read_json(self.original/"studio.json")
            source=published.get("source_ref")
            if source and published.get("script_ref")==self.script_ref:
                asset=self.collab.store.get_asset(source["asset_id"],source["revision_id"])
                if asset["sha256"]==source["sha256"]:self.existing_publication=published
        self.tools=self.workspace.tools()+[
            Tool("read_story_context","读取实际冻结剧本、恢复证据、分镜格式和当前约束。",EMPTY,self.read_context),
            Tool("read_production_spec","读取已保存的分镜候选及其真实校验问题。",EMPTY,self.read_spec),
            Tool("patch_production_spec","用原生 changes 数组分批写分镜。每项含 path（字段或数组索引）和 value。每次写一个字段或一个镜头，读取返回的校验再继续；首次创建 shots 数组，之后用当前长度作索引追加。changes_json 仅供旧会话兼容。保留冻结剧本和真实参考。",
                 SPEC_PATCH,self.patch_spec,effect="write"),
            Tool("check_production_spec","按原条件核验分镜、来源、节拍覆盖、状态与真实参考；保留全部未通过项。",EMPTY,self.check_spec),
            Tool("publish_production_spec","结构与剧情来源通过后发布制作工程；缺少参考时保持不可生成，并交给现有资产制作任务。",EMPTY,self.publish_spec,effect="write"),
            Tool("begin_script_revision","用户要求修改剧本时开始新版本；保留原版并使旧分镜候选失效。",
                 {"type":"object","properties":{"reason":{"type":"string","minLength":1,"maxLength":2000}},"required":["reason"]},self.begin_revision,effect="write")]
        if not any(t.name=="write_draft" for t in self.tools):
            self.tools.append(Tool("write_draft","仅在用户授权开始新修订后写剧本候选，导入原稿不可改写。",DRAFT,self.workspace.write_draft,effect="write"))
        # An existing publication is immutable; a continue message cannot rewrite it.
        for tool in self.tools:
            if tool.name=="publish_script":tool_index=self.tools.index(tool);self.tools[tool_index]=Tool(tool.name,tool.description,tool.parameters,self.publish_current,effect="write")
        if self.script_ref and not restoring_revision:self.workspace.publish_started=True
        if collaborator:self.tools.append(collaborator)

    def _publish(self, candidate, report):
        base=self.script_ref
        report={**report,"base_revision":base["revision_id"] if base else None}
        result=publish_script_revision(self.collab.store,candidate,report,asset_id=self.collab.story+"/script",story_id=self.collab.story,
            base_revision=base["revision_id"] if base else None,source_sha256=base["sha256"] if base else None,
            correlation_id=self.collab.run_id)
        self.script_ref={k:result[k] for k in ("asset_id","revision_id","sha256")};self.script=copy.deepcopy(candidate)
        self.service._update(self.job,script_ref=self.script_ref,run_id=self.collab.run_id)
        return result

    @traced("pi.story.context",kind="tool",capture_inputs=False)
    def read_context(self):
        return {"script":self.script or self.workspace.candidate,"script_ref":self.script_ref,
            "constraints":self.workspace.constraints,"original_idea":self.detail["idea"],
            "explicit_quantity_requirements":explicit_quantities(self.detail["idea"]),
            "interruption_evidence":self.unknown,"production_format":COMPILE_SYSTEM,
            "rule":"已有已发布剧本先复用；编译旧请求结果不明不阻止对已保存候选的新修复。参考必须实际生成和通过检查，不能编造pin。"}

    @traced("pi.story.read_spec",kind="tool",capture_inputs=False)
    def read_spec(self):
        return {"candidate":self.spec,"validation":self.check_spec() if self.spec else None,
                "script_ref":self.script_ref,"existing_publication":self.existing_publication,"interruption_evidence":self.unknown}

    @traced("pi.story.patch_spec",kind="tool",capture_inputs=False)
    def patch_spec(self, changes_json=None, *, changes=None):
        if (changes is None) == (changes_json is None):
            raise ValueError("须提供 changes 数组或旧格式 changes_json 中的一项")
        if changes_json is not None:
            try:
                changes=json.loads(changes_json)
            except json.JSONDecodeError as exc:
                self.retry_patch_step = 0
                raise ValueError(f"changes_json 在字符 {exc.pos} 处无效，本次未写入。改用 changes 数组，只重提一个字段或镜头并检查返回结果。") from exc
        elif len(json.dumps(changes,ensure_ascii=False,separators=(",",":"))) > 12000:
            raise ValueError("本批修改过长，请拆成单个字段或镜头提交。")
        if not isinstance(changes,list) or not 1<=len(changes)<=80:raise ValueError("修改须为1～80项数组")
        candidate=copy.deepcopy(self.spec or {})
        for change in changes:
            path=change.get("path")
            if (not isinstance(path,list) or not path or not isinstance(path[0],str)
                    or path[0]=="script_source" or len(path)>20
                    or any(type(field) not in (str,int) for field in path)):
                raise ValueError("修改路径无效或试图修改冻结来源")
            target=candidate
            for field in path[:-1]:target=target[field]
            field=path[-1]
            if isinstance(target,list) and field==len(target):target.append(change["value"])
            else:target[field]=change["value"]
        # Preserve the actual prior version before changing the checkpoint. Keep
        # older, historically misnamed spec-before files untouched.
        if self.spec is not None:
            history=self.directory/("spec-history-"+digest_json(self.spec)+".json")
            if history.exists() and read_json(history)!=self.spec:raise ValueError("历史制作规格内容与哈希不符；停止修改")
            if not history.exists():write_json(history,self.spec)
        write_json(self.spec_path,candidate)
        self.spec=candidate
        self.result=None
        if any(change["path"][0] in {"entities","facts","events","shots"} for change in changes):
            self.retry_patch_step = None
        return self.check_spec()

    def stop_check(self, record):
        if self.retry_patch_step is None:
            return {"allowed":True}
        # Give a recoverable argument error one follow-up decision within the existing budget.
        step = record["steps"]
        if self.retry_patch_step == 0:
            self.retry_patch_step = step
        return {"allowed":step > self.retry_patch_step,
                "next_action":"用 changes 数组补写一个缺失字段或镜头，然后按校验结果继续。"}

    @traced("pi.story.check_spec",kind="validation",capture_inputs=False)
    def check_spec(self):
        if not self.script:return {"structure_ready":False,"reason":"先发布合法剧本"}
        candidate=copy.deepcopy(self.spec or {})
        candidate["script_source"]={"sha256":digest_json(self.script),"script":self.script}
        report=validate_compilation(candidate,self.script)
        requested=check_quantities(self.detail["idea"],self.script,candidate)
        extra=[{"code":"REQUESTED_QUANTITY","path":r["path"],"message":"原创意明确数量未满足",**r} for r in requested if r["status"]=="FAIL"]
        from pi_bridge.requirements import seedance_request_limit
        from pianchang.generation_groups import group_generation_tasks
        limit = seedance_request_limit(self.detail["idea"])
        if limit is not None and not any(i.get("severity","error")=="error" and i["code"]!="MISSING_REFERENCE" for i in report["issues"]):
            try:
                report = validate_compilation(group_generation_tasks(candidate, limit), self.script)
            except ValueError as exc:
                extra.append({"code":"GENERATION_GROUPS","path":"shots","message":str(exc)})
        report={**report,"issues":report["issues"]+extra,"requested_quantity_checks":requested,
                "ready_for_generation":report["ready_for_generation"] and not extra}
        # Distinct pipeline stage, not a downgrade: original ready_for_generation stays false.
        remaining=[i for i in report["issues"] if i["code"]!="MISSING_REFERENCE" and i.get("severity","error")=="error"]
        result={**report,"structure_ready":not remaining,"non_reference_issues":remaining,
                "pending_reference_issues":[i for i in report["issues"] if i["code"]=="MISSING_REFERENCE"]}
        event("pi.story.stage_validation",{"path":"spec.structure_ready","expected":True,"actual":not remaining,
            "status":"PASS" if not remaining else "FAIL","ready_for_generation":report["ready_for_generation"],"issues":report["issues"]},kind="validation")
        write_json(self.directory/"validation-report.json",result)
        return result

    @traced("pi.story.publish_spec",kind="tool",capture_inputs=False)
    def publish_spec(self):
        report=self.check_spec()
        if not report.get("structure_ready"):return {"published":False,"validation":report}
        project=copy.deepcopy(self.spec);project["script_source"]={"sha256":digest_json(self.script),"script":self.script}
        from pi_bridge.requirements import seedance_request_limit
        from pianchang.generation_groups import group_generation_tasks
        limit = seedance_request_limit(self.detail["idea"])
        if limit is not None:
            project = group_generation_tasks(project, limit)
        if self.existing_publication and digest_json(project)==self.existing_publication["source_ref"]["sha256"]:
            self.result=self.existing_publication
            return {**self.result,"reused":True,"summary":"沿用已发布的同一制作工程；没有重复提交资产或视频生成。"}
        path=self.original/"project.json";pid=project_id_for_path(path)
        previous=self.collab.store.revisions(self.collab.story+"/source-project")
        if previous:
            asset=self.collab.store.get_asset(self.collab.story+"/source-project")
            if asset["sha256"]!=digest_json(project):
                asset=self.collab.store.publish_asset(self.collab.story+"/source-project",project,kind="input_project",producer="commissioner",parent_revision=asset["revision_id"],
                    story_id=self.collab.story,correlation_id=self.collab.run_id,dependencies=[dep(self.script_ref,"target_script")])
        else:
            asset=self.collab.store.publish_asset(self.collab.story+"/source-project",project,kind="input_project",producer="commissioner",
                story_id=self.collab.story,correlation_id=self.collab.run_id,dependencies=[dep(self.script_ref,"target_script")])
        data={"run_id":self.collab.run_id,"story_id":self.collab.story,"script_ref":self.script_ref,"source_ref":ref(asset),
              "project_id":pid,"ready_for_generation":report["ready_for_generation"],"backend":"pi"}
        write_json(self.original/"studio.json",data);write_json(self.original/"validation-report.json",report)
        self.collab.write(project_id=pid,script_ref=self.script_ref,source_ref=ref(asset),title=project.get("title"),status="spec_published")
        from pianchang.asset_dispatch import schedule_published_assets
        data["asset_job"]=schedule_published_assets(self.service,self.original,pid,{"id":self.source_id or self.job["id"]},project=project)
        self.service._update(self.job,project_id=pid,run_id=self.collab.run_id,script_ref=self.script_ref)
        self.result=data
        return {**data,"pending_asset_issues":len(report["pending_reference_issues"]),"summary":"剧本和制作规格已发布；参考资产仍按原规则制作与检查，未通过前不能生成视频。"}

    @traced("pi.story.publish_script",kind="tool",capture_inputs=False)
    def publish_current(self):
        if self.script_ref and self.workspace.publish_started:return self.script_ref
        return self.workspace.publish_script()

    @traced("pi.story.begin_revision",kind="tool",capture_inputs=False)
    def begin_revision(self,reason):
        if self.text.strip() in {"继续","继续运行","继续制作","恢复运行"}:raise ValueError("继续运行未授权改写已发布剧本")
        self.workspace.publish_started=False;self.workspace.receipt=None;self.workspace.report=None
        self.workspace.mode="create"
        self.existing_publication=None;self.result=None
        write_json(self.directory/"revision-intent.json",{"base":self.script_ref,"user_message":self.text,"reason":reason})
        self.spec=None;write_json(self.spec_path,{})
        return {"base":self.script_ref,"reason":reason,"instruction":"通过write_draft写新候选，check_script通过后publish_script。原版本保持。"}

    @traced("pi.story.snapshot",kind="tool",capture_inputs=False)
    def snapshot(self):
        return {"story_id":self.collab.story,"role":"script","published_script":self.script_ref,
            "candidate_sha256":digest_json(self.spec) if self.spec else None,"spec_published":bool(self.result or self.existing_publication),
            "unconfirmed_legacy_calls":self.unknown,"original_idea":self.detail["idea"]}

    @traced("pi.story.finish_check",kind="validation",capture_inputs=False)
    def finish_check(self):
        return {"complete":bool(self.result),"scope":"script_and_spec_publication_not_media_acceptance",
            "summary":"已复用或发布剧本并接续制作规格，参考资产进入独立制作与检查。" if self.result else
                "既有剧本与制作规格已发布；本轮对话没有新增制作交付。" if self.existing_publication else "剧本到制作规格尚未完成。",
            "publication":self.result or self.existing_publication,"media_accepted":False}
