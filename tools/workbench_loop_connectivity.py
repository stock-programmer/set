"""Recheck the configured provider routes without submitting production work or credentials."""
from contextlib import closing
import time
from pathlib import Path
from urllib.parse import urlsplit

from pianchang.app_service import read_json, write_json
from pianchang.model_routes import load_routes
from pianchang.sdk_transport import _upstream_transport
from pianchang.story_spec import digest_json
from trace_system import TraceError, current_context, event, traced


@traced("workbench_loop.voice_connectivity",kind="http",capture_inputs=False)
def _probe(url):
    return _head_route(url, "workbench.voice_probe")


def _head_route(url, event_prefix, *, include_route=False):
    return _request_route(url, event_prefix, "HEAD", include_route=include_route)


def _request_route(url, event_prefix, method, *, include_route=False):
    import httpx
    timeout=8
    req=httpx.Request(method,url,extensions={"timeout":httpx.Timeout(timeout).as_dict()})
    event(event_prefix + ".request",{"method":req.method,"url":str(req.url),
          "headers":dict(req.headers),"timeout_s":timeout},kind="http")
    try:
        with closing(_upstream_transport()) as transport, closing(transport.handle_request(req)) as response:
            status=response.status_code
            event(event_prefix + ".response",{"http_status":status,"headers":dict(response.headers)},kind="http")
            result = {"status":"http_response","http_status":status,"trace_id":current_context()["trace_id"]}
            if include_route:
                result["route"] = getattr(transport, "selected_route", {"mode":"direct", "target_host":req.url.host})
            return result
    except TraceError:
        raise
    except (httpx.HTTPError,OSError) as exc:
        event(event_prefix + ".error",{"error_type":type(exc).__name__},kind="http")
        return {"status":"connection_failed","error_type":type(exc).__name__,"trace_id":current_context()["trace_id"]}


def check_voice_route(root, failure):
    """Share results between the failed asset and dependent shots, across watcher restarts."""
    if not failure.get("url_digest"):
        return {"status":"original_route_unavailable"}
    try:
        route=load_routes(Path(root)/".env").resolve("voice.synthesis")
    except TraceError:
        raise
    except (ValueError,OSError):
        return {"status":"original_route_unavailable"}
    base=route.base_url.rstrip("/")
    parsed=urlsplit(base)
    if (route.provider!="openai" or route.adapter!="litellm" or route.sdk_provider!="openai"
            or parsed.scheme!="https" or not parsed.hostname
            or parsed.username is not None or parsed.query or parsed.fragment
            or digest_json(base+"/chat/completions")!=failure["url_digest"]):
        return {"status":"original_route_unavailable"}
    identity=digest_json([failure["trace_id"],failure["span_id"],failure["request_seq"],base])
    path=Path(root)/"outputs/workbench-loop/watch/connectivity"/(identity+".json")
    if path.is_file():
        cached=read_json(path)
        if cached["next_check"]>time.time():
            return cached["result"]
    result=_probe(base+"/models")
    # A successful HTTP exchange verifies today's network path, independently of the old POST receipt.
    interval=86400 if result.get("http_status") in {200,401,403,405} else 900
    write_json(path,{"result":result,"next_check":time.time()+interval})
    return result


@traced("workbench_loop.planner_connectivity", kind="http", capture_inputs=False)
def check_planner_route(root, proof):
    """Use the same SDK transport and exact configured model URL as the next planner turn."""
    if (proof.get("request_kind") != "model" or proof.get("dispatch") != "not_sent"
            or proof.get("response_seq") is not None or proof.get("phase") not in ("connect_tcp", "start_tls")
            or proof.get("error_type") not in ("ConnectError","ConnectTimeout")
            or not proof.get("trace_id") or not proof.get("span_id")
            or type(proof.get("request_seq")) is not int or type(proof.get("error_seq")) is not int
            or proof["error_seq"] <= proof["request_seq"]):
        return {"status":"retry_later","reason":"planner_evidence_incomplete"}
    try:
        route = load_routes(Path(root)/".env").resolve("video.decision")
    except TraceError:
        raise
    except (ValueError, OSError, KeyError, RuntimeError):
        return {"status":"retry_later","reason":"planner_route_unavailable"}
    url = route.base_url.rstrip("/") + "/chat/completions"
    target = urlsplit(url)
    if (route.adapter != "litellm" or route.sdk_provider != "openai" or target.scheme != "https"
            or not target.hostname or target.username is not None or target.query or target.fragment
            or target.hostname != proof.get("target_host") or route.model_id != proof.get("model_id")
            or digest_json(url) != proof.get("url_digest")):
        return {"status":"retry_later","reason":"planner_route_changed"}
    identity = digest_json([proof["trace_id"],proof["span_id"],proof["request_seq"],proof["error_seq"],url])
    path = Path(root)/"outputs/workbench-loop/watch/connectivity"/(identity+".json")
    if path.is_file():
        cached = read_json(path)
        if cached["next_check"] > time.time():
            return cached["result"]
    check = _head_route(url, "workbench.planner_probe", include_route=True)
    selection = check.get("route") or {}
    reachable = check.get("http_status") in (200, 204, 401, 405) and selection.get("target_host") == target.hostname
    directory = None
    if check.get("http_status") == 404 and selection.get("target_host") == target.hostname:
        # Some completion APIs register only POST and return 404 to HEAD, even on
        # a working route. Confirm the current host using its read-only model list.
        directory = _request_route(route.base_url.rstrip("/")+"/models", "workbench.planner_catalog_probe",
                                   "GET", include_route=True)
        reachable = directory.get("http_status") in (200, 401) and directory.get("route") == selection
    result = {**check, "status":"reachable" if reachable else "retry_later",
              "reason":"planner_route_reachable" if reachable else "planner_route_unavailable"}
    if directory is not None:
        result["catalog_http_status"] = directory.get("http_status")
    # A fresh OS proxy configuration is valid for the next call. The historic not-sent
    # proof remains bound to the original request and its actual route.
    if selection:
        result["same_proxy"] = all(selection.get(field) == proof.get("proxy_mode" if field == "mode" else field)
                                   for field in ("mode","proxy_scheme","proxy_host","proxy_port"))
    event("workbench.planner_probe.result", {"evidence":identity, **result}, kind="validation")
    write_json(path, {"next_check":time.time()+(60 if reachable else 15), "result":result})
    return result
