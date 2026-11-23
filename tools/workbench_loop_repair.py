"""Obtain a narrowly scoped Codex repair and verify it before replacing a source file."""
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from trace_system import TraceError, event, get_tracer, traced


_TOOLS = {
    "write_draft": ("pianchang/script_collab_agent.py", "write_draft"),
    "patch_production_spec": ("pi_bridge/script_tools.py", "patch_spec"),
    "read_production_spec": ("pi_bridge/script_tools.py", "read_spec"),
    "check_production_spec": ("pi_bridge/script_tools.py", "check_spec"),
}
_TESTS = {"script_tools": "test_script_spec_patch", "service": "test_pi_service",
          "runtime": "test_pi_runtime", "web_server": "test_web",
          "repository": "test_pi_sessions", "app_service": "test_web",
          "sdk_transport":"test_sdk_proxy","model_gateway":"test_model_gateway"}
_WEB_TESTS = {"app.js":"tests/ui/test_navigation_ui.cjs",
              "agent-chat.js":"tests/ui/test_pi_chat_ui.cjs",
              "studio.js":"tests/ui/test_studio_ui.cjs",
              "collaborations.js":"tests/ui/test_collaboration_ui.cjs",
              "progress.js":"tests/ui/test_live_progress_ui.cjs"}
# Changes to the pass criteria need a more specific acceptance review than these regression suites.
_GATES = {"validate","validate_script","validate_compilation","_validate","preview_status",
          "require_preview","execution_limits","check_quantities","stop_check","finish_check"}
_RESPONSE = {"type":"object","properties":{
    name:{"type":"string"} for name in ("file","old","new")},
    "required":["file","old","new"],"additionalProperties":False}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _process_env(*, codex=False):
    names={"PATH","SYSTEMROOT","SYSTEMDRIVE","COMSPEC","WINDIR","PATHEXT","TEMP","TMP",
           "USERPROFILE","LOCALAPPDATA","APPDATA","HOME","LANG","LC_ALL"}
    if codex:
        # Use the installed CLI's own sign-in and network route. The repair proposal
        # needs no media provider credentials or application-specific environment.
        names.update({"CODEX_HOME","CODEX_API_KEY","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY",
                      "SSL_CERT_FILE","SSL_CERT_DIR","NODE_EXTRA_CA_CERTS"})
    return {k:v for k,v in os.environ.items() if k.upper() in names}


def _importing_suite(root, module):
    package, member = module.rsplit(".",1)
    for path in sorted((root / "tests").glob("test_*.py")):
        if path.is_symlink() or not path.is_file():
            continue
        source=path.read_text(encoding="utf-8-sig")
        if member not in source:
            continue
        try:
            tree=ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node,ast.Import) and any(alias.name==module for alias in node.names):
                return path
            if isinstance(node,ast.ImportFrom) and not node.level and (node.module==module
                    or node.module==package and any(alias.name==member for alias in node.names)):
                return path
    return None


def _evidence(root, fault, previous=()):
    frames = list(fault.get("frames") or [])[-8:]
    tool = _TOOLS.get(fault.get("tool"))
    if tool:
        frames.insert(0,{"file":tool[0], "function":tool[1]})
    evidence = {}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        name = str(frame.get("file", "")).replace("\\", "/")
        candidate = (root / name).resolve()
        if not candidate.is_relative_to(root):
            continue
        name = candidate.relative_to(root).as_posix()
        if not candidate.is_file():
            continue
        if name == "web/"+candidate.name and candidate.name in _WEB_TESTS:
            suite = _WEB_TESTS[candidate.name]
            test_path = root / suite
        elif re.fullmatch(r"(pi_bridge|pianchang|tools)/[a-z][a-z0-9_]*\.py", name):
            suite = "tests."+_TESTS.get(candidate.stem, "test_" + candidate.stem)
            test_path = root / (suite.replace(".","/")+".py")
        else:
            continue
        if not test_path.is_file() and name.endswith(".py"):
            # Several workbench modules are covered by an integration suite whose
            # filename differs from the module. Only consider checked-in test code.
            module = name[:-3].replace("/", ".")
            imported = _importing_suite(root,module)
            if imported:
                test_path = imported
                suite = "tests." + test_path.stem
        if not test_path.is_file() or not test_path.resolve().is_relative_to((root/"tests").resolve()):
            continue
        data = candidate.read_bytes()
        lines = data.decode("utf-8-sig").splitlines()
        line = frame.get("line")
        if not isinstance(line, int) or not 1 <= line <= len(lines):
            line = next((n for n, s in enumerate(lines, 1)
                         if s.lstrip().startswith("def " + str(frame.get("function")) + "(")), 1)
        first = max(0,line-80)
        excerpt = "\n".join(lines[first:line+55])
        # Include the actual regression source, with the latest failed assertion
        # in context. Model inputs are limited to these repository files.
        checks = next((p.get("checks",{}) for p in reversed(previous) if p.get("file")==name
                       and p.get("before_sha256")==_sha(data) and p.get("checks",{}).get("suite")==suite), {})
        test_lines = test_path.read_text(encoding="utf-8-sig").splitlines()
        anchors = [n for n in checks.get("lines",[]) if type(n) is int and 1 <= n <= len(test_lines)]
        if not anchors:
            symbol = frame.get("function") or candidate.stem
            anchors = [i for i,s in enumerate(test_lines,1) if symbol in s]
        start = max(0,(anchors[0] if anchors else 1)-25)
        evidence[name] = (data,suite,excerpt,{"first_line":first+1,"fault_line":line,
            "regression":{"suite":suite,"file":test_path.relative_to(root).as_posix(),"first_line":start+1,
                          "source":"\n".join(test_lines[start:start+85])[:9000]}})
    return evidence


def _gate_definitions(data):
    protected=[]
    for node in ast.walk(ast.parse(data)):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in _GATES:
            protected.append(ast.dump(node))
        if isinstance(node,ast.ExceptHandler) and node.type and any(
                getattr(t,"id",None)=="TraceError" or getattr(t,"attr",None)=="TraceError" for t in ast.walk(node.type)):
            protected.append(ast.dump(node))
    return protected


def _stage(root, target):
    # Include the current checkout's source and offline fixtures. Never copy live workspaces.
    for folder in ("pianchang","pi_bridge","trace_system","tools","tests","fixtures","contracts","config","web"):
        for src in (root / folder).rglob("*"):
            if src.suffix not in {".py",".json",".toml",".js",".cjs",".html",".css"} or not src.is_file() or src.is_symlink():
                continue
            if not src.resolve().is_relative_to(root / folder):
                continue
            path = target / src.relative_to(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, path)


def _offline_tests(cli, root, stage, suite, timeout_s):
    # Only the staged checkout gets write access; this child receives no provider keys.
    js = suite.endswith(".cjs")
    cmd = [cli,"sandbox","-P",":workspace","-C",str(stage),
           "-c","sandbox_workspace_write.network_access=false","--"]
    cmd += [shutil.which("node") or "node",suite] if js else [sys.executable,"-E","-s","-m","unittest",suite]
    event("workbench_loop.validation.request",{"argv":cmd,"suite":suite,"network":False})
    with tempfile.TemporaryFile() as log:
        result = subprocess.run(cmd,cwd=stage,env=_process_env(),stdin=subprocess.DEVNULL,
                                stdout=log,stderr=subprocess.STDOUT,timeout=timeout_s)
        log.seek(0)
        output=log.read(65536)
    ran = b"passed" in output if js else re.search(rb"Ran [1-9]\d* tests? in ",output) is not None
    # Only retain test identifiers and source locations. Assertion messages can contain story data.
    log_text = output.decode("utf-8",errors="replace").replace("\\","/")
    test_path = suite if js else suite.replace(".","/")+".py"
    locations = re.findall(re.escape(test_path) + (r":(\d+):\d+" if js else r'", line (\d+), in '),log_text)
    ran = bool(ran or js and locations)
    event("workbench_loop.validation.response",{"suite":suite,"exit":result.returncode,"suite_ran":ran})
    if not ran:
        raise ValidationUnavailable("isolated_test_did_not_run")
    if result.returncode:
        names = re.findall(r"^(?:ERROR|FAIL): (test_[a-zA-Z0-9_]{1,150})\b",log_text,re.M)
        checks = {"suite":suite, "file":test_path, "lines":sorted({int(n) for n in locations})[:5],
                  "failed_tests":list(dict.fromkeys(names))[:5], "exit":result.returncode}
        event("workbench_loop.validation.failed_checks",checks,kind="validation")
        raise RegressionFailed(checks)
    return "codex_workspace_write"


class ValidationUnavailable(Exception):
    pass


class RegressionFailed(Exception):
    pass


@traced("workbench_loop.repair", kind="tool", capture_inputs=False)
def repair_fault(root, fault, *, timeout_s=900, previous=()):
    """Return applied only after a verified edit; callers can then restart and replay the UI."""
    root = Path(root).resolve()
    blocked = lambda reason: {"status":"blocked","reason":reason,"files":[],"tests":[]}
    cli = shutil.which("codex.exe" if os.name=="nt" else "codex")
    if not cli:
        return blocked("codex_unavailable")
    try:
        targets = _evidence(root,fault,previous)
        if not targets:
            return blocked("no_supported_source_and_test")
        error = str(fault.get("error_type",""))
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,64}",error):
            error = "ApplicationError"
        context = {"exception":error,"tool":fault.get("tool") if fault.get("tool") in _TOOLS else None,
                   "sources":{name:{"source":entry[2],**entry[3]} for name,entry in targets.items()}}
        if previous:
            context["previous_attempts"] = [p for p in previous if p.get("reason") or
                (p.get("file") in targets and _sha(targets[p["file"]][0]) in
                 {p.get("before_sha256"),p.get("after_sha256")})][-8:]
        prompt = ("Repair the localized workbench error using the source, regression tests and previous attempts. "
                  "The host tests your edit in an isolated checkout and applies it if validation passes. Return exactly one "
                  "minimal literal replacement in one listed file. If the cause cannot be established, "
                  "return three empty strings. Preserve published versions, credentials, billing permissions, "
                  "validation thresholds, trace failure handling, role permissions, and all unrelated edits. "
                  "Account for any rejected candidate and failed checks before proposing a different correction. "
                  "Do not use tools. Data follows.\n"+json.dumps(context,ensure_ascii=False))
        prompt = get_tracer().sanitize(prompt)
        with tempfile.TemporaryDirectory(prefix="pianchang-repair-") as tmp:
            work = Path(tmp)
            schema = work / "response.schema.json"
            response = work / "proposal.json"
            schema.write_text(json.dumps(_RESPONSE),encoding="utf-8")
            args = [cli,"exec","--ignore-user-config","--ephemeral","--sandbox","read-only",
                    "--skip-git-repo-check","--disable","shell_tool","--disable","unified_exec",
                    "--disable","apps","--disable","plugins","-c",'web_search="disabled"',
                    "-c",'approval_policy="never"',"--output-schema",str(schema),
                    "--output-last-message",str(response),"-"]
            event("workbench_loop.codex.request",{"prompt":prompt,"schema":_RESPONSE,"argv":args})
            result = subprocess.run(args,input=prompt.encode(),cwd=work,env=_process_env(codex=True),stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,timeout=timeout_s)
            raw = response.read_text(encoding="utf-8-sig") if response.is_file() else None
            event("workbench_loop.codex.response", {"exit":result.returncode,"proposal":raw})
            if result.returncode or not response.is_file():
                return blocked("codex_failed")
            proposal = json.loads(raw)
            name, old, new = (proposal.get(k) for k in ("file","old","new"))
            if name not in targets or not all(isinstance(v,str) for v in (old,new)) or not old or old==new:
                return blocked("no_applicable_repair")
            data,suite,excerpt,_ = targets[name]
            # Preserve the file's existing encoding and surrounding bytes (including CRLF).
            newline = "\r\n" if b"\r\n" in data else "\n"
            old = old.replace("\r\n","\n")
            original = old.replace("\n",newline).encode()
            replacement = new.replace("\r\n","\n").replace("\n",newline).encode()
            if old not in excerpt or data.count(original)!=1 or len(replacement)>16000:
                return blocked("repair_hunk_mismatch")
            after = data.replace(original,replacement,1)
            if any(p.get("file")==name and p.get("after_sha256")==_sha(after) for p in previous):
                return blocked("repeated_rejected_proposal")
            def reject(reason, checks):
                return {**blocked(reason),"validation":{"file":name,"before_sha256":_sha(data),
                    "after_sha256":_sha(after),"old":old,"new":new,"checks":checks}}
            if name.endswith(".py"):
                try:
                    compile(after,name,"exec")
                except SyntaxError as exc:
                    return reject("repair_syntax_invalid",{"error":type(exc).__name__,"source_line":exc.lineno})
                if _gate_definitions(data)!=_gate_definitions(after):
                    return reject("acceptance_or_authorization_change_requires_review",
                                  {"error":"candidate_changed_protected_definition"})
            stage = work / "source"
            _stage(root, stage)
            (stage / name).write_bytes(after)
            try:
                isolation = _offline_tests(cli,root,stage,suite,timeout_s)
            except RegressionFailed as exc:
                return reject("isolated_regression_failed",exc.args[0])
            if not isolation:
                return blocked("isolated_regression_failed")
            path = root / name
            with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as out:
                temporary = Path(out.name)
                out.write(after)
            try:
                shutil.copymode(path,temporary)
                if path.resolve()!=path or path.read_bytes()!=data:
                    return blocked("source_changed_during_verification")
                receipt = {"status":"applied","files":[name],"tests":[suite],"isolation":isolation,
                           "before_sha256":_sha(data),"after_sha256":_sha(after)}
                event("workbench_loop.repair.apply_request",receipt)
                os.replace(temporary,path)
                event("workbench_loop.repair.applied",receipt)
                return receipt
            finally:
                temporary.unlink(missing_ok=True)
    except TraceError:
        raise
    except ValidationUnavailable as exc:
        return blocked(str(exc))
    except subprocess.TimeoutExpired:
        return blocked("repair_timed_out")
    except (OSError,ValueError,TypeError,AttributeError,SyntaxError) as exc:
        return blocked(type(exc).__name__)
