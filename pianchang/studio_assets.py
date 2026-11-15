"""The studio's story-scoped view of script entities, media and pinned shot specifications."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from urllib import error, request
from urllib.parse import urlencode, urlsplit

from pianchang.acceptance import file_sha256
from pianchang.bailian_text import BailianTextClient
from pianchang.bailian_video import _NoRedirect, _trace_http_request, _trace_http_response
from pianchang.collab_store import CollabStore
from pianchang.reference_assets import SUBJECT_KINDS, _mime, asset_pin, bind_project, import_reference
from pianchang.story_spec import digest_json
from trace_system import current_context, event, register_secret, traced


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
IMAGE_MODEL = "qwen-image-2.0"
IMAGE_ENDPOINT = "/services/aigc/multimodal-generation/generation"


def _check(path, expected, actual):
    passed = expected == actual
    event("studio.assets.check", {"path": path, "expected": expected, "actual": actual,
                                  "status": "PASS" if passed else "FAIL"}, kind="validation")
    if not passed:
        raise ValueError(f"{path}：需要 {expected}，当前为 {actual}")


def _identity(asset):
    return {k: asset[k] for k in ("asset_id", "revision_id", "sha256")}


def _db_path(service):
    return service.root / "outputs" / "collaborations" / "assets.sqlite3"


def _script_hash(project):
    script = project.get("script_source", {}).get("script")
    return digest_json(script if script is not None else project)


def _source_scope(source, pid):
    from pianchang.studio_jobs import studio_run_id
    # The deterministic scope lets an existing project acquire references before its first commission.
    run_id = studio_run_id(pid)
    metadata = source.parent / "studio.json"
    if metadata.is_file():
        record = json.loads(metadata.read_text(encoding="utf8"))
        _check("工作台工程映射", pid, record["project_id"])
        run_id = record["run_id"]
        _check("工作台协作 ID", True, isinstance(run_id, str) and re.fullmatch("[a-f0-9]{32}", run_id) is not None)
        _check("工作台故事范围", "story-" + run_id, record["story_id"])
        return run_id, record
    return run_id, None


def _read_view(service, pid, revision=None, *, include_references=True):
    """Match a source by exact content. Never join unrelated scripts on a shared title."""
    source, project = service._project(pid)
    source_hash = digest_json(project)
    run_id, metadata = _source_scope(source, pid)
    story = "story-" + run_id
    db_path = _db_path(service)
    script_ref = pending_script_ref = None
    production = spec = None
    references = []
    if db_path.is_file():
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            matches = [r["story_id"] for r in db.execute(
                "SELECT DISTINCT story_id FROM assets WHERE kind='input_project' AND sha256=? ORDER BY story_id",
                (source_hash,))]
            # An ambiguous match is a different commission, not authority to alter either one.
            owns_assets = db.execute("SELECT 1 FROM assets WHERE story_id=? LIMIT 1", (story,)).fetchone()
            if metadata is None and not owns_assets and story not in matches:
                candidates = []
                for candidate in matches:
                    if not re.fullmatch(r"story-[0-9a-f]{32}", candidate):
                        continue
                    index = db_path.parent / "runs" / candidate[6:] / "overview.json"
                    info = json.loads(index.read_text(encoding="utf8")) if index.is_file() else {}
                    if info.get("project_id") in (None, pid):
                        candidates.append(candidate)
                if len(candidates) == 1:
                    story = candidates[0]
                    run_id = story[6:]
            row = db.execute("SELECT * FROM assets WHERE kind='production' AND story_id=? AND asset_id=? "
                             "ORDER BY seq DESC LIMIT 1", (story, story + "/production")).fetchone()
            if row:
                production = CollabStore._asset(db, row)
                project = production["content"]["project"]
                script_ref = production["content"]["script_ref"]
            else:
                row = db.execute("SELECT * FROM assets WHERE kind='script' AND story_id=? AND asset_id=? AND sha256=? "
                                 "ORDER BY seq DESC LIMIT 1", (story, story + "/script", _script_hash(project))).fetchone()
                if row:
                    script_ref = _identity(CollabStore._asset(db, row))
                if metadata and metadata.get("script_ref"):
                    _check("剧本发布凭证", metadata["script_ref"], script_ref)
            source_script_sha = _script_hash(project)
            spec_id = story + "/studio-spec/" + source_script_sha
            sql = "SELECT * FROM assets WHERE kind='production_spec' AND story_id=? AND asset_id=?"
            query = (story, spec_id)
            if revision is not None:
                sql += " AND revision_id=?"
                query += (revision,)
            row = db.execute(sql + " ORDER BY seq DESC LIMIT 1", query).fetchone()
            if row:
                spec = CollabStore._asset(db, row)
                project = spec["content"]
            latest = db.execute("SELECT * FROM assets WHERE kind='script' AND story_id=? AND asset_id=? "
                                "ORDER BY seq DESC LIMIT 1", (story, story + "/script")).fetchone()
            if latest:
                latest_ref = _identity(CollabStore._asset(db, latest))
                if latest_ref != script_ref:
                    pending_script_ref = latest_ref
            # Only the asset catalog consumes media revisions and their reverse
            # dependencies. A project/preview read uses the selected spec pins.
            rows = db.execute("SELECT * FROM assets WHERE kind='reference_media' AND story_id=? "
                              "ORDER BY asset_id,seq DESC", (story,)) if include_references else ()
            for row in rows:
                asset = CollabStore._asset(db, row)
                edges = [dict(edge) for edge in db.execute(
                    "SELECT d.asset_id,d.revision_id,d.role,d.source_id FROM dependencies d "
                    "JOIN assets a USING(asset_id,revision_id) WHERE d.dep_asset_id=? AND d.dep_revision_id=? "
                    "AND a.story_id=? ORDER BY d.asset_id,a.seq,d.source_id",
                    (asset["asset_id"], asset["revision_id"], story))]
                references.append((asset, edges))
    else:
        if metadata:
            raise FileNotFoundError("工程映射所指向的资产库不存在。")
        source_script_sha = _script_hash(project)
        spec_id = story + "/studio-spec/" + source_script_sha
    if revision is not None and spec is None:
        raise KeyError("此故事找不到指定的制作规格版本。")
    spec_ref = _identity(spec) if spec else {"asset_id": spec_id, "revision_id": None, "sha256": digest_json(project)}
    return {"project": project, "project_id": pid, "project_path": str(source), "store_path": str(db_path),
            "story_id": story, "run_id": run_id, "spec": spec_ref, "script_ref": script_ref, "source_script_sha256": source_script_sha,
            "pending_script_ref": pending_script_ref,
            "production_ref": _identity(production) if production else None}, references, production


@traced("studio.assets.video_input", kind="tool", capture_output=False)
def studio_project_for_video(service, pid, spec_revision=None, *, include_delivery=False):
    view, _, production = _read_view(service, pid, spec_revision, include_references=False)
    event("studio.assets.video_input.selected", {k: v for k, v in view.items() if k != "project"})
    if include_delivery:
        # _read_view verified the stored content hash and selected this exact
        # production revision in the same read-only transaction as the spec.
        view["published_delivery"] = production["content"] if production else None
    return view


@traced("studio.assets.catalog", kind="tool", capture_output=False)
def studio_asset_catalog(service, pid, *, include_project=False):
    view, assets, _ = _read_view(service, pid)
    project = view.pop("project")
    references = {}
    for asset, edges in assets:
        pin = asset_pin(asset)
        content = asset["content"]
        query = urlencode({"asset_id": pin["asset_id"], "revision_id": pin["revision_id"]})
        media_path = Path(view["store_path"]).parent / "reference-blobs" / pin["media_sha256"][:2] / pin["media_sha256"]
        entry = {**pin, **{k: content[k] for k in ("mime_type", "bytes", "width", "height", "duration_s") if k in content},
                 "parent_revision": asset["parent_revision"], "created_at": asset["created_at"],
                 "origin": content.get("origin"), "inputs": asset["dependencies"],
                 "media_path": str(media_path), "media_url": f"/api/projects/{pid}/studio/media?{query}",
                 "trace_id": asset["trace_context"].get("trace_id"), "dependencies": edges}
        references.setdefault(pin["subject_id"], []).append(entry)
    shots = [{k: shot.get(k, []) for k in ("id", "scene_id", "entity_ids", "description", "source_refs",
                                           "input_versions", "required_refs")} for shot in project["shots"]]
    entities = []
    for subject, entity in project["entities"].items():
        kind = entity["kind"]
        entities.append({"id": subject, "kind": kind, "name": entity["canonical"].get("name", subject),
                         "canonical": entity["canonical"], "source_refs": entity.get("source_refs", []),
                         "version": entity["version"], "references": references.get(subject, []),
                         "purposes": [p for p, k in SUBJECT_KINDS.items() if k == kind],
                         "shot_ids": [s["id"] for s in shots if subject in s["entity_ids"] or subject == s["scene_id"]]})
    event("studio.assets.catalog.read", {**view, "entity_count": len(entities), "reference_revisions": len(assets)})
    catalog = {**view, "entities": entities, "shots": shots}
    if include_project:
        catalog["project"] = project
    return catalog


def _decode_upload(value):
    if not isinstance(value, str) or len(value) > 64 + 4 * ((MAX_UPLOAD_BYTES + 2) // 3):
        raise ValueError("请选择不超过 8 MiB 的参考文件。")
    header, sep, encoded = value.partition(",")
    allowed = {"image/png", "image/jpeg", "image/webp", "audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp3"}
    if not sep or not header.startswith("data:") or not header.endswith(";base64"):
        raise ValueError("请通过文件选择器上传参考图或声音。")
    declared = header[5:-7].lower()
    if declared not in allowed:
        raise ValueError("请上传 PNG、JPEG、WebP、WAV 或 MP3。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("无效的 base64 文件内容。") from exc
    if not 0 < len(data) <= MAX_UPLOAD_BYTES:
        raise ValueError("参考文件为空或超过 8 MiB。")
    declared = {"audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/mp3": "audio/mpeg"}.get(declared, declared)
    mime = _mime(data)
    _check("上传声明的媒体类型", declared, mime)
    return data, mime


@traced("studio.assets.upload", kind="tool", capture_inputs=False)
def upload_studio_reference(service, pid, body):
    view, assets, _ = _read_view(service, pid)
    data, mime = _decode_upload(body.get("data_uri"))
    target = _target(view, assets, body)
    _check("参考媒体类型", "audio" if target["purpose"] == "voice" else "image", mime.split("/")[0])
    return _import_bytes(view, target, data, mime, {"method": "upload", "review_required": True})


def _target(view, assets, body):
    subject, purpose = body.get("subject_id"), body.get("purpose")
    entity = view["project"]["entities"].get(subject)
    if entity is None or purpose not in SUBJECT_KINDS:
        raise ValueError("请选择本故事的对象及参考用途。")
    _check("资产对象类型", SUBJECT_KINDS[purpose], entity["kind"])
    requested = body.get("asset_id")
    asset_id = requested or (view["story_id"] + "/references/" + digest_json([subject, purpose])[:24])
    existing = next((a for a, _ in assets if a["asset_id"] == asset_id), None)
    if requested:
        if existing is None:
            raise KeyError("找不到本故事要修订的参考资产。")
        _check("参考资产对象", subject, existing["content"]["subject_id"])
        _check("参考资产用途", purpose, existing["content"]["purpose"])
    _check("参考父版本", body.get("parent_revision"), existing["revision_id"] if existing else None)
    return {"subject_id": subject, "purpose": purpose, "asset_id": asset_id, "entity": entity,
            "parent_revision": body.get("parent_revision"), "producer": existing["producer"] if existing else "assets"}


def _import_bytes(view, target, data, mime, provenance):
    sha = hashlib.sha256(data).hexdigest()
    entity = target["entity"]
    origin = {**provenance, "project_id": view["project_id"], "script_ref": view["script_ref"],
              "project_sha256": digest_json(view["project"]), "entity_version": entity["version"],
              "entity_canonical": entity["canonical"], "source_refs": entity.get("source_refs", [])}
    args = {key: target[key] for key in ("asset_id", "subject_id", "purpose", "parent_revision", "producer")}
    event("studio.assets.import.request", {**args, "story_id": view["story_id"], "origin": origin,
                                          "media_sha256": sha, "bytes": len(data), "mime_type": mime})
    store = CollabStore(view["store_path"])
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "upload"
        source.write_bytes(data)
        pin = import_reference(store, story_id=view["story_id"], path=source, origin=origin, **args)
    return {"pin": pin, "trace_id": current_context()["trace_id"]}


@traced("studio.reference.validate", kind="validation")
def validate_studio_reference_body(service, pid, body):
    view, assets, _ = _read_view(service, pid)
    target = _target(view, assets, body)
    options = {}
    for field in ("style", "feedback"):
        if field in body:
            if not isinstance(body[field], str) or len(body[field]) > 3000:
                raise ValueError("风格和修改意见各限 3000 个字符。")
            options[field] = body[field].strip()
    if "max_attempts" in body:
        count = body["max_attempts"]
        if type(count) is not int or not 1 <= count <= 3:
            raise ValueError("单项资产尝试次数范围为 1 至 3。")
        options["max_attempts"] = count
    return {"subject_id": target["subject_id"], "purpose": target["purpose"], "asset_id": body.get("asset_id"),
            "parent_revision": target["parent_revision"], "story_id": view["story_id"],
            "entity_version": target["entity"]["version"], "spec_sha256": view["spec"]["sha256"], **options}


def _image_prompt(target):
    direction = {"portrait": "单人定妆照，正面，自然站姿，清楚呈现脸、发型和服饰。",
                 "scene": "场景空镜，全景构图，清楚呈现空间布局、门窗及固定陈设。",
                 "prop": "道具参考照，单独呈现道具的外形、材质、颜色和尺寸感。",
                 "costume": "服装或配饰参考照，完整呈现版型、材质、纹理与配色。"}
    entity = target["entity"]
    facts = json.dumps(entity["canonical"], ensure_ascii=False)
    excerpts = [str(row.get("quote", ""))[:150] for row in entity.get("source_refs", [])[:2]]
    return ("按影视剧美术方向制作一张参考图。" + direction[target["purpose"]] + "剧本设定：" + facts
            + "。相关剧本原文：" + "；".join(excerpts) + "。只画指定的参考对象，按剧本保留必要的标识。")


@traced("studio.image.compose_prompt", kind="tool")
def compose_reference_prompt(service, target, brief, repair, *, qwen_image=True):
    base = _image_prompt(target)
    if not brief:
        return base
    direction = brief["shared_style"] or brief["default_direction"]
    if brief["script_direction"]:
        direction += " " + json.dumps(brief["script_direction"], ensure_ascii=False)
    anchors = " ".join(text for record in brief.get("reference_appearance", []) for text in record["appearance"])
    sections = [base, direction, brief["script"].get("logline", ""), anchors]
    if brief["human_feedback"]:
        sections.insert(0, "导演修改意见：" + brief["human_feedback"])
    if repair:
        sections.insert(0, "修正已观察到的问题：" + repair)
    prompt = "\n".join(s for s in sections if s)
    # Qwen-Image-2.0 truncates at 1300 tokens. UTF-8 byte length is a conservative upper
    # bound for byte-level tokenization; never put feedback after a truncated full screenplay.
    max_bytes = 1300
    if not qwen_image or len(prompt.encode("utf8")) <= max_bytes:
        return prompt
    routes = getattr(service, "model_routes", None)
    if routes is not None and not routes.resolve("reference.image").api_key:
        raise ValueError("请先配置 reference.image 所用连接的 API Key。")
    system = ("把输入改写为一段可直接用于生图的中文提示，最多 330 个汉字。保留导演修改要求、"
              "上一版的待修正问题、主体识别特征和指定造型、颜色、风格；仅取该资产相关的原文。"
              "定妆/道具/服装可用独立背景，不要求复现整场戏。只输出 JSON 对象，字段 prompt。")
    messages = [{"role":"system","content":system}, {"role":"user","content":prompt}]
    client = BailianTextClient(service.text_config, purpose="reference.prompt", routes=routes)
    for attempt in range(2):
        result = client.complete_json(messages)
        compact = result["data"].get("prompt")
        size = len(compact.encode("utf8")) if isinstance(compact,str) and compact.strip() else 0
        passed = 0 < size <= max_bytes
        event("studio.image.prompt.size", {"path":"prompt.utf8_bytes", "expected":[1,max_bytes],
              "actual":size, "status":"PASS" if passed else "FAIL"}, kind="validation")
        if passed:
            return compact
        if attempt == 0:
            messages.extend([{"role":"assistant","content":json.dumps(result["data"],ensure_ascii=False)},
                             {"role":"user","content":f"提示词长度校验失败，实际 {size} 字节；请压缩到 300 个汉字以内，仍返回 prompt 字段。"}])
    raise ValueError("资产提示词压缩后仍超出生图长度限制，详见 Trace。")


@traced("studio.image.request", kind="model", capture_inputs=False)
def _request_image(config, payload):
    key = config.api_key
    if not key:
        raise ValueError("请先配置 reference.image 所用连接的 API Key。")
    register_secret(key)
    req = request.Request(config.base_url.rstrip("/") + IMAGE_ENDPOINT,
                          data=json.dumps(payload, ensure_ascii=False).encode("utf8"),
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    timeout = max(config.request_timeout, 120)
    _trace_http_request(req, timeout)
    try:
        with request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
            _trace_http_response(response, raw, 1024 * 1024)
    except error.HTTPError as exc:
        with exc:
            raw = exc.read(65537)
            _trace_http_response(exc, raw, 65536)
        raise ValueError(f"百炼生图返回 HTTP {exc.code}，请在 Trace 查看服务商错误。") from None
    except (error.URLError, TimeoutError, OSError) as exc:
        raise ValueError("百炼生图请求中断，受理状态未知。请查看此任务 Trace。") from exc
    if len(raw) > 1024 * 1024:
        raise ValueError("百炼生图响应过大，请查看此任务 Trace。")
    result = json.loads(raw)
    if result.get("code"):
        raise ValueError(f"百炼生图失败，错误码 {result['code']}。")
    return result


@traced("studio.image.download", kind="tool", capture_output=False)
def _download_image(url, timeout):
    if not isinstance(url, str):
        raise ValueError("供应商返回的图像地址无效。")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or not parts.hostname:
        raise ValueError("供应商返回的图像地址无效。")
    req = request.Request(url, headers={"Accept": "image/*"})
    _trace_http_request(req, timeout)
    try:
        with request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(MAX_UPLOAD_BYTES + 1)
            event("studio.image.download.response", {"http_status": response.status, "headers": dict(response.headers),
                  "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}, kind="http")
    except error.HTTPError as exc:
        with exc:
            _trace_http_response(exc, exc.read(65537), 65536)
        raise ValueError(f"下载参考图失败，HTTP {exc.code}。") from None
    if not 0 < len(raw) <= MAX_UPLOAD_BYTES or not _mime(raw).startswith("image/"):
        raise ValueError("下载的参考图为空、格式无效或超过 8 MiB。")
    return raw


def _sdk_reference_image(route, prompt, timeout):
    from pianchang.sdk_transport import call_sdk

    response = call_sdk(route, "image_generation", timeout=timeout, prompt=prompt, size="1024x1024", n=1)
    items = response.get("data")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("供应商未返回请求的一张参考图，请查看任务 Trace。")
    encoded = items[0].get("b64_json")
    if encoded is None and items[0].get("url"):
        data = _download_image(items[0]["url"], timeout)
    else:
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_UPLOAD_BYTES + 2) // 3):
            raise ValueError("参考图编码无效或文件超过 8 MiB。")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("供应商返回的参考图不是有效的 Base64。") from exc
        if not 0 < len(data) <= MAX_UPLOAD_BYTES or not _mime(data).startswith("image/"):
            raise ValueError("参考图为空、格式无效或超过 8 MiB。")
    return data, response


@traced("studio.reference.generate", kind="tool")
def generate_studio_reference(service, pid, body, *, brief=None, repair=""):
    validated = validate_studio_reference_body(service, pid, body)
    view, assets, _ = _read_view(service, pid)
    target = _target(view, assets, body)
    for field, value in (("story_id", view["story_id"]), ("entity_version", target["entity"]["version"]),
                         ("spec_sha256", view["spec"]["sha256"])):
        _check("生图委托." + field, body.get(field, validated[field]), value)
    if view["pending_script_ref"]:
        raise ValueError("剧本已有待适配版本，请先更新制作规格。")
    if target["purpose"] == "voice":
        if brief is None:
            raise ValueError("声音制作需要包含试听要求的资产委托。")
        from pianchang.asset_voice import synthesize_voice
        data, mime, origin = synthesize_voice(service, brief, repair)
        result = _import_bytes(view, target, data, mime, {**origin, "method":"generated", "brief":brief, "review_required":True})
        return {**result, "model": origin.get("model"), "request_id": origin.get("request_id")}
    routes = getattr(service, "model_routes", None)
    route = routes.resolve("reference.image") if routes is not None else None
    model = route.model_id if route else IMAGE_MODEL
    prompt = compose_reference_prompt(service, target, brief, repair, qwen_image=model == IMAGE_MODEL)
    if route and route.adapter == "litellm":
        data, response = _sdk_reference_image(route, prompt, max(120, service.video_config.request_timeout))
    else:
        config = service.video_config
        if route:
            config = replace(config, api_key=route.api_key, base_url=route.base_url)
        payload = {"model": model, "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
                   "parameters": {"size": "1024*1024", "n": 1, "prompt_extend": False, "watermark": False}}
        response = _request_image(config, payload)
        try:
            url = response["output"]["choices"][0]["message"]["content"][0]["image"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("百炼响应没有图像结果，详见任务 Trace。") from exc
        data = _download_image(url, config.request_timeout)
    request_id = response.get("_provider_request_id") or response.get("request_id")
    origin = {"method": "generated", "model": model, "request_id": request_id, "review_required": True,
              "usage": response.get("usage"), "prompt": prompt}
    if route:
        origin.update(provider=route.provider, account=route.account, adapter=route.adapter)
        if route.adapter == "litellm" and response.get("_wire_receipt"):
            wire = response["_wire_receipt"]
            origin["wire_receipt"] = {k: wire[k] for k in ("trace_context", "wire_body_sha256") if k in wire}
    if response.get("model"):
        origin["response_model"] = response["model"]
    if brief is not None:
        origin.update(review_required=True, brief=brief)
    result = _import_bytes(view, target, data, _mime(data), origin)
    return {**result, "model": model, "request_id": request_id}


@traced("studio.assets.bind", kind="tool")
def bind_studio_reference(service, pid, body):
    view, assets, _ = _read_view(service, pid)
    spec = view["spec"]
    if "spec_parent_revision" not in body:
        raise ValueError("请选择当前制作规格版本后再绑定。")
    _check("制作规格父版本", body["spec_parent_revision"], spec["revision_id"])
    if body.get("spec_sha256") is not None:
        _check("制作规格指纹", body["spec_sha256"], spec["sha256"])
    target = next((a for a, _ in assets if a["asset_id"] == body.get("asset_id") and
                   a["revision_id"] == body.get("revision_id")), None)
    if target is None:
        raise KeyError("找不到本故事要绑定的参考版本。")
    pin = asset_pin(target)
    from pianchang.asset_dispatch import check_reference_approval
    check_reference_approval(service, pid, pin)
    shot_id = body.get("shot_id")
    shot = next((s for s in view["project"]["shots"] if s["id"] == shot_id), None)
    if shot is None:
        raise ValueError("本故事找不到此镜头。")
    _check("镜头引用了这个对象", True, pin["subject_id"] in [shot["scene_id"], *shot["entity_ids"]])
    if pin not in shot.get("required_refs", []):
        result = bind_project(CollabStore(view["store_path"]), view["project"], story_id=view["story_id"],
                              shot_id=shot_id, pins=[pin], spec_asset_id=spec["asset_id"],
                              parent_revision=spec["revision_id"], script_ref=view["script_ref"])
        spec = _identity(result)
        shot = next(s for s in result["content"]["shots"] if s["id"] == shot_id)
    return {"spec": spec, "shot_id": shot_id, "required_refs": shot["required_refs"],
            "trace_id": current_context()["trace_id"]}


@traced("studio.assets.media", kind="tool", capture_output=False)
def studio_reference_media(service, pid, asset_id, revision_id):
    view, assets, _ = _read_view(service, pid)
    target = next((a for a, _ in assets if a["asset_id"] == asset_id and a["revision_id"] == revision_id), None)
    if target is None:
        raise KeyError("找不到本故事的参考文件版本。")
    pin = asset_pin(target)
    sha = pin["media_sha256"]
    path = Path(view["store_path"]).parent / "reference-blobs" / sha[:2] / sha
    _check("参考文件指纹", pin["media_sha256"], file_sha256(path))
    event("studio.assets.media.response", {"path": str(path), **pin, "mime_type": target["content"]["mime_type"]})
    return path, target["content"]["mime_type"]
