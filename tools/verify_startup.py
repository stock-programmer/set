"""Measure actual local HTTP startup reads, without model/media generation."""
import argparse
import hashlib
import http.client
import json
import re
import time
from pathlib import Path

from pi_bridge.runtime import persist
from trace_system import event,get_tracer,trace,traced,use_context

ROOT=Path(__file__).resolve().parents[1]


@traced("performance.startup.http",kind="validation",capture_inputs=False)
def measure(port,path):
    headers={"Origin":f"http://127.0.0.1:{port}"}
    event("performance.http.request",{"method":"GET","url":f"http://127.0.0.1:{port}"+path,"headers":headers},kind="http")
    connection=http.client.HTTPConnection("127.0.0.1",port,timeout=120)
    try:
        start=time.perf_counter();connection.request("GET",path,headers=headers)
        response=connection.getresponse();body=response.read();elapsed=(time.perf_counter()-start)*1000
        result={"path":path,"status":response.status,"elapsed_ms":round(elapsed,2),"bytes":len(body),
                "body_sha256":hashlib.sha256(body).hexdigest(),"server_trace_id":response.getheader("X-Trace-Id")}
        event("performance.http.received",{**result,"response_headers":dict(response.getheaders()),
            "response_evidence":"Full response payload is recorded by the linked server HTTP trace."},kind="http")
        result["passed"]=response.status==200 and elapsed<3000
        event("performance.http.target",{"path":path,"expected":"HTTP 200, complete body under 3000 ms",
            "actual":result,"status":"PASS" if result["passed"] else "FAIL"},kind="validation")
        return result
    finally:connection.close()


@traced("cli.performance.startup",kind="cli")
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=8765);parser.add_argument("--project",action="append",required=True)
    parser.add_argument("--repeat",type=int,default=3)
    parser.add_argument("--browser-evidence",type=Path);args=parser.parse_args()
    if not 1024<=args.port<=65535 or not 1<=args.repeat<=10 or any(not re.fullmatch(r"[0-9a-f]{16}",p) for p in args.project):raise ValueError("Invalid local benchmark arguments")
    tracer=get_tracer(ROOT/"outputs/traces")
    with tracer.storage_session(),use_context(None,tracer=tracer),trace("performance.startup.verify",kind="validation",inputs={"label":"actual local HTTP timings; no provider calls","projects":args.project,"repeat":args.repeat}) as task:
        paths=["/","/workbench.css","/workbench.js","/api/workspace"]
        for pid in args.project:paths.append(f"/api/workspace?project={pid}")
        samples=[]
        for iteration in range(args.repeat):
            for path in paths:
                sample=measure(args.port,path);sample["iteration"]=iteration+1;samples.append(sample)
                print(json.dumps(sample,ensure_ascii=False),flush=True)
        report={"schema":"startup-performance/1","label":"Actual complete-body local HTTP timings; benchmark tracing excluded from elapsed time. Browser paint/readiness is a separate measurement.",
            "trace_id":task.trace_id,"target_ms":3000,"passed":all(s["passed"] for s in samples),"samples":samples}
        if args.browser_evidence:
            evidence_path=args.browser_evidence.resolve()
            if not evidence_path.is_relative_to(ROOT/"outputs/web"):raise ValueError("Browser evidence must be a local workbench artifact")
            raw=evidence_path.read_bytes();browser=json.loads(raw)
            if browser.get("schema")!="browser-startup-observations/1" or len(browser.get("samples",[]))<3:raise ValueError("Insufficient browser observations")
            browser_passed=True
            for sample in browser["samples"]:
                passed=all(sample.get(k) is True for k in ("stageLoaded","conversationLoaded","jobsLoaded")) and all(0<sample[k]<3000 for k in ("readyMs","sidebarMs"))
                event("performance.browser.target",{"path":sample["kind"],"expected":"real project/activity/conversation ready under 3000 ms",
                    "actual":sample,"status":"PASS" if passed else "FAIL"},kind="validation")
                browser_passed=browser_passed and passed
            report.update(browser_evidence={"path":str(evidence_path),"sha256":hashlib.sha256(raw).hexdigest(),"observations":browser},
                passed=report["passed"] and browser_passed)
        output=ROOT/"outputs/web"/("startup-performance-"+task.trace_id+".json");persist(output,report)
    print(json.dumps({"passed":report["passed"],"samples":len(samples),"report":str(output),"trace_integrity":tracer.verify(task.trace_id)["ok"]},ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__=="__main__":raise SystemExit(main())
