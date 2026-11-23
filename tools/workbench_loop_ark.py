"""Discover Ark image routes without generating media, then conditionally update the selected route."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile

from pianchang.atomic_files import replace_with_retry
from pianchang.bailian_video import VideoError, _read_env
from pianchang.model_routes import load_routes
from pianchang.sdk_transport import _upstream_transport
from pianchang.story_spec import digest_json
from trace_system import TraceError, current_context, event, register_secret, traced


_BASE = "https://ark.cn-beijing.volces.com/api/v3"
_CONTROL_HOST = "ark.cn-beijing.volcengineapi.com"
_ENDPOINT = re.compile(r"ep-[a-zA-Z0-9][a-zA-Z0-9-]{1,124}\Z")
_MODEL = re.compile(r"doubao-seedream-5-0(?:-(?:pro|flash|lite))?-\d{6}\Z")
_PRO = "doubao-seedream-5-0-pro-260628"
_LITE_IDS = {"doubao-seedream-5-0-lite-260128", "doubao-seedream-5-0-260128"}
# The provider's migration target. Always intersect this with live catalog capabilities.
_MIGRATION = "https://docs.volcengine.com/docs/ark/model-deprecation-notice?lang=zh"


@traced("workbench.ark.probe_image_transport", kind="http", capture_inputs=False)
def probe_image_transport(root, proof):
    """Probe the exact frozen image URL with HEAD after the SDK established no origin send."""
    from tools.workbench_loop_connectivity import _head_route
    result = {"status": "retry_later", "trace_id": current_context()["trace_id"]}
    if (proof.get("request_kind") != "image" or proof.get("dispatch") != "not_sent"
            or proof.get("response_seq") is not None or proof.get("http_status") is not None
            or proof.get("error_type") not in {"ConnectError", "ConnectTimeout"}
            or proof.get("phase") not in {"start_tls", "connect_tcp"}
            or not re.fullmatch(r"[a-f0-9]{32}", str(proof.get("trace_id")))
            or not re.fullmatch(r"(?:[a-f0-9]{16}|[a-f0-9]{32})", str(proof.get("span_id")))
            or type(proof.get("request_seq")) is not int or proof["request_seq"] < 1):
        return {**result, "reason": "pre_send_evidence_unavailable"}
    try:
        route = load_routes(Path(root)/".env").resolve("reference.image")
    except TraceError:
        raise
    except (OSError, ValueError, KeyError, VideoError):
        return {**result, "reason": "image_route_changed"}
    base = route.base_url.rstrip("/")
    url = base + "/images/generations"
    if ((route.provider, route.adapter, route.sdk_provider, base) != ("volcengine-ark", "litellm", "openai", _BASE)
            or route.model_id != proof.get("model_id") or digest_json(url) != proof.get("url_digest")):
        return {**result, "reason": "image_route_changed"}
    evidence = _head_route(url, "workbench.image_probe")
    status = evidence.get("http_status")
    reachable = status in {200, 204, 401, 405} and evidence.get("status") == "http_response"
    return {**result, "status": "reachable" if reachable else "retry_later", "model_id": route.model_id,
            "reason": "image_route_reachable" if reachable else "image_route_unavailable",
            **({ "http_status": status } if status is not None else {})}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _fetch(method, url, headers, body=b""):
    import httpx
    request = httpx.Request(method, url, headers={**headers, "Accept-Encoding": "identity"}, content=body,
                            extensions={"timeout": httpx.Timeout(20).as_dict()})
    event("ark.discovery.request", {"method": method, "url": str(request.url), "headers": dict(request.headers),
                                   "body": body.decode("utf-8")}, kind="http")
    try:
        with closing(_upstream_transport()) as transport, closing(transport.handle_request(request)) as response:
            status, raw = response.status_code, response.read()
            event("ark.discovery.response", {"http_status": status, "headers": dict(response.headers),
                                            "body": raw.decode("utf-8", errors="replace")}, kind="http")
    except TraceError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        event("ark.discovery.error", {"error_type": type(exc).__name__}, kind="http")
        return None, {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        data = None
    return status, data if isinstance(data, dict) else {}


def _management_credentials(root):
    env = {**(_read_env(root/".env") if (root/".env").is_file() else {}), **os.environ}
    key = env.get("VOLCENGINE_ACCESS_KEY") or env.get("VOLCENGINE_ACCESS_KEY_ID")
    secret = env.get("VOLCENGINE_SECRET_KEY") or env.get("VOLCENGINE_SECRET_ACCESS_KEY")
    token = env.get("VOLCENGINE_SESSION_TOKEN") or env.get("VOLCENGINE_SECURITY_TOKEN")
    if key and secret:
        for value in (key, secret, token):
            if value:
                register_secret(value)
        return key, secret, token
    return None


def _endpoint_page(credentials, page):
    # Volcengine's OpenAPI Signature V4, restricted to ListEndpoints in the data route's region.
    key, secret, token = credentials
    query = "Action=ListEndpoints&Version=2024-01-01"
    body = json.dumps({"PageNumber": page, "PageSize": 100, "Filter": {
        "Statuses": ["Running"], "EndpointModelTypes": ["FoundationModel"]}}, separators=(",", ":")).encode()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {"content-type": "application/json", "host": _CONTROL_HOST,
               "x-content-sha256": _sha(body), "x-date": stamp}
    if token:
        headers["x-security-token"] = token
    names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical = "\n".join(("POST", "/", query, canonical_headers, names, _sha(body)))
    scope = stamp[:8] + "/cn-beijing/ark/request"
    signing_key = secret.encode()
    for field in scope.split("/"):
        signing_key = hmac.digest(signing_key, field.encode(), "sha256")
    message = "\n".join(("HMAC-SHA256", stamp, scope, _sha(canonical.encode())))
    signature = hmac.new(signing_key, message.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"HMAC-SHA256 Credential={key}/{scope}, SignedHeaders={names}, Signature={signature}"
    register_secret(headers["authorization"])
    return _fetch("POST", f"https://{_CONTROL_HOST}/?{query}", headers, body)


def _endpoints(root):
    credentials = _management_credentials(root)
    if not credentials:
        return "management_credentials_unavailable", {}
    found = {}
    for page in range(1, 101):
        status, data = _endpoint_page(credentials, page)
        if status in (401, 403):
            return "endpoint_inventory_forbidden", found
        result = data.get("Result")
        items = result.get("Items") if isinstance(result, dict) else None
        if status != 200 or not isinstance(items, list):
            return "endpoint_inventory_unavailable", found
        for row in items:
            if not isinstance(row, dict) or row.get("Status") != "Running":
                continue
            eid = row.get("Id", "")
            ref = (row.get("ModelReference") or {}).get("FoundationModel") or {}
            name, version = str(ref.get("Name", "")), str(ref.get("ModelVersion", ""))
            if not _MODEL.fullmatch(name) and re.fullmatch(r"\d{6}", version):
                name += "-" + version
            if isinstance(eid, str) and _ENDPOINT.fullmatch(eid) and _MODEL.fullmatch(name):
                found[eid] = name
        total = result.get("TotalCount")
        if len(items) < 100 or (type(total) is int and page * 100 >= total):
            return "listed", found
    return "partial_inventory", found


def _published_images(rows):
    images = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("id")
        if not isinstance(model, str) or not _MODEL.fullmatch(model) or row.get("status") != "Published":
            continue
        # Older versions can omit api_supported. Their actual domain and task must agree.
        apis, tasks = row.get("api_supported"), row.get("task_type")
        if (row.get("domain") == "ImageGeneration" and isinstance(tasks, list) and "text_to_image" in tasks
                and (apis is None or isinstance(apis, dict) and apis.get("images/generations") is True)):
            images.add(model)
    return images


@traced("workbench.ark.inspect_image_route", kind="tool", capture_inputs=False)
def inspect_ark_image_route(root, rejected_models, *, supplied_model=None):
    """Read current account evidence. Candidate visibility never asserts inference permission."""
    root = Path(root)
    rejected = set(rejected_models)
    try:
        path = root / "config/models.json"
        digest = _sha(path.read_bytes())
        route = load_routes(root/".env").resolve("reference.image")
    except TraceError:
        raise
    except (OSError, ValueError, KeyError, VideoError):
        return {"status": "needs_input", "reason": "invalid_image_configuration"}
    base = {"configured_model": route.model_id, "config_digest": digest, "trace_id": current_context()["trace_id"],
            "candidates": [], "endpoint_inventory": "not_queried"}
    if (route.provider, route.adapter, route.sdk_provider, route.base_url.rstrip("/")) != (
            "volcengine-ark", "litellm", "openai", _BASE):
        return {**base, "status": "needs_input", "reason": "unsupported_image_connection"}
    if not route.api_key:
        return {**base, "status": "needs_input", "reason": "missing_api_key"}
    register_secret(route.api_key)
    status, data = _fetch("GET", _BASE + "/models", {"Authorization": "Bearer " + route.api_key})
    base["catalog_http_status"] = status
    if status == 401:
        return {**base, "status": "needs_input", "reason": "invalid_api_key"}
    rows = data.get("data")
    listed = status == 200 and isinstance(rows, list)
    images = _published_images(rows) if listed else set()
    inventory, endpoints = _endpoints(root)
    base.update(endpoint_inventory=inventory, visible_image_models=sorted(images), visible_endpoints=sorted(endpoints))
    endpoint_only = isinstance(rejected_models, dict) and (
        "InvalidEndpointOrModel.ModelIDAccessDisabled" in rejected_models.values())
    if supplied_model is not None:
        if not isinstance(supplied_model, str):
            return {**base, "status": "needs_input", "reason": "invalid_supplied_model"}
        # A person can attest to a private image endpoint outside the scope of the management credential.
        if _ENDPOINT.fullmatch(supplied_model) or (not endpoint_only and supplied_model in images):
            return {**base, "status": "candidate", "reason": "supplied_image_route", "model_id": supplied_model,
                    "candidates": [supplied_model], "access_verified": False}
        return {**base, "status": "needs_input" if listed else "retry_later",
                "reason": "invalid_supplied_model" if listed else "catalog_unavailable"}
    original = {route.model_id, *rejected}
    family_known = any(_MODEL.fullmatch(v) or v in endpoints for v in original if isinstance(v, str))
    if not family_known:
        if not listed and status != 403:
            return {**base, "status": "retry_later", "reason": "catalog_unavailable"}
        return {**base, "status": "needs_input", "reason": "endpoint_model_unknown"}
    # A new configuration set by the operator has priority. A running private endpoint for
    # the original version offers the same model without switching quality or input support.
    choices = []
    if route.model_id in images or route.model_id in endpoints or _ENDPOINT.fullmatch(route.model_id):
        choices.append(route.model_id)
    choices += sorted(e for e, m in endpoints.items() if m in original)
    # The management API can attest to a running endpoint even when the inference key
    # cannot list public models. Only public IDs actually listed by that key are used.
    ranked = sorted(images | set(endpoints.values()), key=lambda m: (m != _PRO, "-pro-" not in m, m))
    for model in ranked:
        choices.extend(sorted(e for e, m in endpoints.items() if m == model))
        if model in images:
            choices.append(model)
    # Explicit endpoint-only rejection applies to all model IDs, and is accepted
    # only as structured evidence from the retry controller.
    usable = list(dict.fromkeys(v for v in choices if v not in rejected and (not endpoint_only or _ENDPOINT.fullmatch(v))))
    if usable:
        chosen = usable[0]
        migration = chosen == _PRO and bool(_LITE_IDS.intersection(original))
        return {**base, "status": "candidate", "model_id": chosen, "candidates": usable, "access_verified": False,
                "reason": "published_successor" if migration else "compatible_image_route",
                **({"migration_source": _MIGRATION} if migration else {})}
    if not listed:
        if status == 403:
            return {**base, "status": "needs_input", "reason": "catalog_permission_denied"}
        return {**base, "status": "retry_later", "reason": "catalog_unavailable"}
    if inventory == "endpoint_inventory_unavailable":
        return {**base, "status": "retry_later", "reason": inventory}
    reason = "provider_access" if any(v in rejected for v in choices) or endpoint_only else "no_compatible_image_route"
    return {**base, "status": "needs_input", "reason": reason}


@traced("workbench.ark.apply_image_route", kind="tool", capture_inputs=False)
def apply_ark_image_route(root, inspection):
    """Apply a server-owned inspection under the shared config lock, without changing any other purpose."""
    from tools.workbench_loop_watch import _lock
    root = Path(root)
    model = inspection.get("model_id", "")
    if (inspection.get("status") != "candidate" or model not in inspection.get("candidates", []) or
            not (_MODEL.fullmatch(model) or _ENDPOINT.fullmatch(model))):
        raise ValueError("缺少可使用的方舟参考图路由")
    path = root/"config/models.json"
    with _lock(root/"outputs/workbench-loop/watch/image-config.lock") as acquired:
        if not acquired:
            return {"status": "retry_later", "reason": "configuration_busy"}
        raw = path.read_bytes()
        route = load_routes(root/".env").resolve("reference.image")
        if _sha(raw) != inspection.get("config_digest") or route.model_id != inspection["configured_model"]:
            return {"status": "retry_later", "reason": "configuration_changed"}
        if model == route.model_id:
            return {"status": "ready", "model_id": model, "changed": False}
        if (route.provider, route.base_url.rstrip("/")) != ("volcengine-ark", _BASE):
            raise ValueError("方舟连接已变化")
        source = raw.decode("utf-8-sig")
        config = json.loads(source)
        alias = config["routes"]["reference.image"]
        shared = list(config["routes"].values()).count(alias) > 1
        if shared:
            base_alias = alias[:90] + "-reference-" + _sha(model.encode())[:12]
            new_alias = base_alias
            target = {**config["models"][alias], "model_id": model}
            suffix = 1
            while new_alias in config["models"] and config["models"][new_alias] != target:
                suffix += 1
                new_alias = f"{base_alias}-{suffix}"
            config["models"][new_alias] = target
            config["routes"]["reference.image"] = new_alias
        else:
            config["models"][alias]["model_id"] = model
        # Keep hand-formatted files byte-for-byte outside a unique model value when possible.
        new_source = source.replace(json.dumps(route.model_id), json.dumps(model))
        if shared or json.loads(new_source) != config:
            new_source = json.dumps(config, ensure_ascii=False, indent=4) + "\n"
            if "\r\n" in source:
                new_source = new_source.replace("\n", "\r\n")
        encoded = new_source.encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
        event("ark.image_config.change_requested", {"purpose": "reference.image", "from": route.model_id,
              "to": model, "input_sha256": _sha(raw), "output_sha256": _sha(encoded)}, kind="state")
        pending = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".models-", delete=False)
        temp = Path(pending.name)
        try:
            with pending:
                pending.write(encoded)
                pending.flush()
                os.fsync(pending.fileno())
            if path.read_bytes() != raw:
                return {"status": "retry_later", "reason": "configuration_changed"}
            os.chmod(temp, path.stat().st_mode)
            replace_with_retry(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        event("ark.image_config.changed", {"purpose": "reference.image", "model_id": model,
                                          "sha256": _sha(encoded)}, kind="state")
        return {"status": "ready", "model_id": model, "changed": True}
