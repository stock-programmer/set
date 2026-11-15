"""Resolve frozen inputs into a provider request. Voice bytes use DashScope temporary storage."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from urllib import error, parse, request
from pathlib import Path

from pianchang.bailian_video import VideoError, _NoRedirect, build_request, validate_reference_capabilities
from pianchang.reference_assets import resolve_references
from pianchang.collab_store import CollabStore
from trace_system import event, get_tracer, register_secret, span, traced


def _exchange(opener, req, timeout, request_record, *, policy=False):
    with span("reference.upload.http", kind="http", inputs=request_record) as call:
        event("http.request", request_record, kind="http")
        try:
            response = opener.open(req, timeout=timeout)
        except error.HTTPError as exc:
            response = exc
        except (OSError, error.URLError) as exc:
            raise VideoError("百炼临时素材上传连接失败：" + type(exc).__name__) from None
        with response:
            raw = response.read(1024 * 1024 + 1)
            status = response.status
            record = {"http_status": status, "response_sha256": hashlib.sha256(raw).hexdigest(),
                      "trace_id": call.trace_id, "span_id": call.span_id,
                      "captured_bytes": len(raw),
                      "request_id": response.headers.get("x-oss-request-id") or response.headers.get("x-request-id")}
            data = None
            if policy and status == 200 and len(raw) <= 1024 * 1024:
                try:
                    response_body = json.loads(raw)
                    data = response_body["data"]
                    if not isinstance(data, dict):
                        raise ValueError("invalid data")
                    for key in ("policy", "signature", "oss_access_key_id", "security_token"):
                        if data.get(key):
                            register_secret(data[key])
                    record.update(data=data, request_id=response_body.get("request_id") or record["request_id"])
                except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                    record["invalid_response"] = type(exc).__name__
            call.set_output(record)
            event("http.response", record, kind="http")
            if status != 200 or len(raw) > 1024 * 1024 or record.get("invalid_response"):
                raise VideoError(f"百炼临时素材上传失败（HTTP {status}）。", http_status=status)
            return data, record


@traced("reference.upload_voice", kind="tool", capture_inputs=False)
def upload_voice(config, resolved):
    """Upload exactly the verified bytes, record their digest, return the locator given to R2V."""
    if not config.api_key:
        raise VideoError("上传音色参考需要百炼 API Key。")
    register_secret(config.api_key)
    mime = resolved["mime_type"]
    suffix = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav"}.get(mime)
    header, comma, encoded = resolved["provider_url"].partition(",")
    if not suffix or not comma or header != f"data:{mime};base64":
        raise ValueError("音色上传仅接受已解析的 WAV 或 MP3 字节。")
    content = base64.b64decode(encoded, validate=True)
    sha = hashlib.sha256(content).hexdigest()
    if sha != resolved["media_sha256"] or not 0 < len(content) <= 15 * 1024 * 1024:
        raise ValueError("音色文件哈希或大小与锁定要求不符。")
    opener = request.build_opener(_NoRedirect())
    query = parse.urlencode({"action": "getPolicy", "model": config.model})
    policy_url = config.base_url.rstrip("/") + "/uploads?" + query
    policy_req = request.Request(policy_url, headers={"Authorization": "Bearer " + config.api_key})
    policy, _ = _exchange(opener, policy_req, config.request_timeout,
        {"method": "GET", "url": policy_url, "headers": dict(policy_req.header_items())}, policy=True)
    host = parse.urlsplit(policy["upload_host"])
    if (host.scheme != "https" or not (host.hostname or "").endswith(".aliyuncs.com")
            or host.username or host.password or host.query or host.fragment or host.port not in (443, None)):
        raise VideoError("临时上传凭证给出的 OSS 地址无效。")
    prefix = policy["upload_dir"].rstrip("/")
    if not re.fullmatch(r"dashscope-instant/[a-zA-Z0-9_./-]+", prefix) or ".." in prefix.split("/"):
        raise VideoError("临时上传凭证给出的对象路径无效。")
    if policy["x_oss_object_acl"] != "private" or str(policy["x_oss_forbid_overwrite"]).lower() != "true":
        raise VideoError("临时音色参考要求私有、禁止覆盖的对象。")
    boundary = "drama" + uuid.uuid4().hex
    filename = sha + "." + suffix
    key = prefix + "/" + filename
    fields = {"OSSAccessKeyId": policy["oss_access_key_id"], "Signature": policy["signature"],
              "policy": policy["policy"], "x-oss-object-acl": "private", "x-oss-forbid-overwrite": "true",
              "key": key, "success_action_status": "200"}
    lines = []
    for field, value in fields.items():
        lines.extend([f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{field}"'.encode(),
                      b"", str(value).encode()])
    lines.extend([f"--{boundary}".encode(), f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
                  f"Content-Type: {mime}".encode(), b"", content, f"--{boundary}--".encode(), b""])
    body = b"\r\n".join(lines)
    req = request.Request(host.geturl(), data=body, method="POST",
                          headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    _, result = _exchange(opener, req, config.request_timeout, {"method": "POST", "url": req.full_url,
        "headers": dict(req.header_items()), "fields": fields, "file": {"media_sha256": sha, "bytes": len(content)},
        "wire_body_sha256": hashlib.sha256(body).hexdigest()})
    locator = "oss://" + key
    return {"provider_url": locator, "media_sha256": sha, "bytes": len(content), "trace_id": result["trace_id"],
            "span_id": result["span_id"], "request_id": result["request_id"]}


@traced("reference.verify_voice_upload", kind="validation", capture_inputs=False)
def verify_voice_upload(receipt):
    """Match an opaque OSS locator to the original successful upload of the frozen file's bytes."""
    if not isinstance(receipt, dict):
        return {"status":"UNKNOWN", "checks":[{"path":"upload.receipt","expected":"receipt","actual":None,"status":"UNKNOWN"}]}
    tracer = get_tracer()
    tid, sid = receipt.get("trace_id"), receipt.get("span_id")
    if not all(isinstance(x,str) and re.fullmatch("[a-f0-9]{32}",x) for x in (tid,sid)):
        return {"status":"UNKNOWN","checks":[{"path":"upload.span","expected":"trace_id + span_id","actual":None,"status":"UNKNOWN"}]}
    after = 0
    events = []
    while True:
        batch = tracer.get_events(tid, after=after, span_id=sid)
        events.extend(e for e in batch if e["span_id"] == sid)
        if len(batch) < 1000:
            break
        after = batch[-1]["trace_seq"]
    checks = []
    def compare(path, expected, actual):
        status = "UNKNOWN" if actual is None and expected is not None else "PASS" if expected == actual else "FAIL"
        checks.append({"path":path,"expected":expected,"actual":actual,"status":status})
    start = next((e for e in events if e["kind"] == "span.start"), {})
    outgoing = next((e for e in events if e["name"] == "http.request"), {})
    incoming = next((e for e in events if e["name"] == "http.response"), {})
    end = next((e for e in events if e["kind"] == "span.end"), {})
    req = tracer.resolve(outgoing.get("data")) or {}
    res = tracer.resolve(incoming.get("data")) or {}
    compare("upload.boundary", "reference.upload.http", start.get("name"))
    compare("upload.method", "POST", req.get("method"))
    destination = parse.urlsplit(req.get("url", ""))
    compare("upload.oss_host", True, destination.scheme == "https" and (destination.hostname or "").endswith(".aliyuncs.com") if req else None)
    compare("upload.sequence", True, (start["trace_seq"] < outgoing["trace_seq"] < incoming["trace_seq"] < end["trace_seq"])
            if all((start, outgoing, incoming, end)) else None)
    compare("upload.expected_media_sha256", True, bool(re.fullmatch("[0-9a-f]{64}", str(receipt.get("media_sha256")))))
    compare("upload.expected_byte_count", True, type(receipt.get("bytes")) is int and receipt["bytes"] > 0)
    compare("upload.wire_sha256", True, bool(re.fullmatch("[0-9a-f]{64}", str(req.get("wire_body_sha256")))) if req else None)
    fields, file = req.get("fields", {}), req.get("file", {})
    compare("upload.locator", receipt.get("provider_url"), "oss://" + fields["key"] if "key" in fields else None)
    compare("upload.private", "private", fields.get("x-oss-object-acl"))
    compare("upload.no_overwrite", "true", fields.get("x-oss-forbid-overwrite"))
    compare("upload.file_sha256", receipt.get("media_sha256"), file.get("media_sha256"))
    compare("upload.file_bytes", receipt.get("bytes"), file.get("bytes"))
    compare("upload.http_status", 200, res.get("http_status"))
    compare("upload.provider_request", receipt.get("request_id"), res.get("request_id"))
    compare("upload.span_status", "OK", (tracer.resolve(end.get("data")) or {}).get("status"))
    result = {"status":"FAIL" if any(c["status"] == "FAIL" for c in checks) else
              "UNKNOWN" if any(c["status"] == "UNKNOWN" for c in checks) else "PASS", "checks":checks}
    event("reference.upload.binding",result,kind="validation")
    return result


@traced("reference.prepare_shot_request", kind="tool", capture_inputs=False, capture_output=False)
def prepare_shot_request(config, compiled, *, store=None, story_id=None, request_options=None, dry_run=False):
    pins = compiled["metadata"].get("required_refs", [])
    options = {"duration": compiled["duration"], **(request_options or {})}
    if not pins:
        return build_request(config, compiled["prompt"], **options), []
    if store is None or not story_id:
        raise ValueError("有锁定参考的镜头需要指定参考资产库和所属故事。")
    from pianchang.reference_inputs import effective_reference_pins,verify_reference_bundle,bundle_prompt
    effective=effective_reference_pins(compiled["metadata"],config)
    validate_reference_capabilities(config, effective)
    if options.get("audio_url") or options.get("image_url"):
        raise VideoError("固定参考与未锁定的媒体 URL 不能混用。")
    prompt=compiled["prompt"]
    if compiled["metadata"].get("reference_bundle"):
        bundle=verify_reference_bundle(compiled["metadata"]["reference_bundle"],source_refs=pins,story_id=story_id,
            project_sha256=compiled["metadata"]["project_sha256"],shot_id=compiled["metadata"]["shot_id"],config=config)
        prompt+="\n"+bundle_prompt(bundle)
    resolved = resolve_references(store, effective, story_id=story_id)
    # Check every slot, prompt, duration and seed before transferring any local voice.
    if dry_run or any(p["purpose"] == "voice" for p in resolved):
        plan = build_request(config, prompt, resolved_refs=resolved, reference_plan=True, **options)
        if dry_run:
            return plan, []
    for item in resolved:
        if item["purpose"] == "voice" and not config.sdk_video:
            receipt = upload_voice(config, item)
            item.update(provider_url=receipt["provider_url"], upload_receipt=receipt)
    payload = build_request(config, prompt, resolved_refs=resolved, **options)
    return payload, resolved


@traced("reference.prepare_workbench_request", kind="tool", capture_inputs=False, capture_output=False)
def prepare_workbench_request(config, compiled, project, root):
    scope = project.get("reference_story_id")
    database = Path(root) / "outputs" / "collaborations" / "assets.sqlite3"
    if not scope or not database.is_file():
        raise ValueError("镜头要求参考素材，请先在工作台资产库中登记并绑定到制作工程。")
    compiled["metadata"].update(story_id=scope, reference_story_id=scope)
    return prepare_shot_request(config, compiled, store=CollabStore(database), story_id=scope)
