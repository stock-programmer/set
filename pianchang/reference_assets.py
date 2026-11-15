"""Immutable reference media and per-shot specifications, independent of a provider."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from collections import OrderedDict

from pianchang.collab_store import CollabStore
from pianchang.media_tools import _probe, find_ffprobe
from trace_system import current_context, event, get_tracer, traced


PIN_FIELDS = {"asset_id", "revision_id", "sha256", "media_sha256", "subject_id", "purpose"}
SUBJECT_KINDS = {"portrait": "character", "voice": "character", "scene": "scene",
                 "costume": "costume", "prop": "prop"}
MAX_BYTES = 32 * 1024 * 1024
_media_info_cache = OrderedDict()


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip() and value.isprintable()


@traced("references.validate_pins", kind="validation")
def pin_issues(pins, *, path="required_refs", subjects=None):
    """Validate the declared pins; optional subjects maps allowed entity ID to kind."""
    issues = []

    def invalid(field, expected, actual):
        issues.append({"path": field, "expected": expected, "actual": actual, "status": "FAIL"})

    if not isinstance(pins, list):
        invalid(path, "ordered list of reference pins", type(pins).__name__)
        return issues
    seen = set()
    for index, pin in enumerate(pins):
        position = f"{path}[{index}]"
        if not isinstance(pin, dict) or set(pin) != PIN_FIELDS:
            invalid(position, sorted(PIN_FIELDS), sorted(map(str, pin)) if isinstance(pin, dict) else type(pin).__name__)
            continue
        for field in ("asset_id", "subject_id"):
            if not _identifier(pin[field]):
                invalid(f"{position}.{field}", "nonempty stable ID", pin[field])
        for field, pattern in (("revision_id", r"r[1-9][0-9]*"), ("sha256", r"[a-f0-9]{64}"),
                               ("media_sha256", r"[a-f0-9]{64}")):
            if not isinstance(pin[field], str) or not re.fullmatch(pattern, pin[field]):
                invalid(f"{position}.{field}", pattern, pin[field])
        purpose, subject = pin["purpose"], pin["subject_id"]
        if not isinstance(purpose, str) or purpose not in SUBJECT_KINDS:
            invalid(position + ".purpose", sorted(SUBJECT_KINDS), purpose)
            continue
        if not _identifier(subject) or not _identifier(pin["asset_id"]):
            continue
        if subjects is not None and subjects.get(subject) != SUBJECT_KINDS[purpose]:
            invalid(position + ".subject_id", f"{SUBJECT_KINDS[purpose]} included in this shot", subject)
        key = (subject, purpose, None if purpose == "voice" else pin["asset_id"])
        if key in seen:
            invalid(position, "one revision per reference and one voice per subject", key)
        seen.add(key)
    return issues


def _checked(path, expected, actual):
    status = "PASS" if expected == actual else "FAIL"
    check = {"path": path, "expected": expected, "actual": actual, "status": status}
    event("references.check", check, kind="validation")
    if status == "FAIL":
        raise ValueError(f"{path}：要求 {expected}，实际 {actual}")


def _read_bytes(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("参考文件为空或超过 32 MiB。")
    return data


def _mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xff and data[1] & 0xe0 == 0xe0):
        return "audio/mpeg"
    raise ValueError("不支持的参考媒体；支持 PNG、JPEG、WebP、WAV 和 MP3。")


def _blob_path(store, digest):
    return store.path.parent / "reference-blobs" / digest[:2] / digest


def _link_new(data, destination):
    """Publish a complete file without replacing an existing revision or export."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _install(data, destination):
    """Expose a complete content-addressed blob exactly once, even for concurrent imports."""
    if not destination.exists():
        try:
            _link_new(data, destination)
        except FileExistsError:
            pass  # A concurrent import published the same content address.
    _checked("reference.blob.sha256", destination.name, hashlib.sha256(_read_bytes(destination)).hexdigest())


def _media_info(blob, mime):
    content_sha = hashlib.sha256(_read_bytes(Path(blob))).hexdigest()
    tool = find_ffprobe()
    stamp = tool.stat()
    key = (get_tracer().store_id, content_sha, mime, str(tool), stamp.st_size, stamp.st_mtime_ns)
    if key in _media_info_cache:
        result, source = _media_info_cache[key]
        _media_info_cache.move_to_end(key)
        event("references.media_probe_reused", {"path":str(blob), "expected":content_sha,"actual":content_sha,
              "status":"PASS", "source":source,"result":result,"new_process":False}, kind="cache")
        return copy.deepcopy(result)
    probe = _probe(tool, ["-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", str(blob)])
    streams = probe.get("streams", [])
    if len(streams) != 1:
        raise ValueError("参考文件须包含单一图像或声音。")
    stream = streams[0]
    if mime.startswith("image/"):
        expected_codec = {"image/jpeg": "mjpeg", "image/png": "png", "image/webp": "webp"}[mime]
        _checked("reference.image_codec", expected_codec, stream.get("codec_name"))
        width, height = stream.get("width"), stream.get("height")
        if type(width) != int or type(height) != int or min(width, height) < 1:
            raise ValueError("图像分辨率无效。")
        result = {"width": width, "height": height}
        _media_info_cache[key] = (result, current_context())
        if len(_media_info_cache) > 128:
            _media_info_cache.popitem(last=False)
        return copy.deepcopy(result)
    _checked("reference.audio_type", "audio", stream.get("codec_type"))
    if mime == "audio/mpeg":
        _checked("reference.audio_codec", "mp3", stream.get("codec_name"))
    duration = float(stream.get("duration") or probe.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("音色参考没有有效时长。")
    result = {"duration_s": duration}
    _media_info_cache[key] = (result, current_context())
    if len(_media_info_cache) > 128:
        _media_info_cache.popitem(last=False)
    return copy.deepcopy(result)


def asset_pin(asset):
    content = asset["content"]
    return {**{k: asset[k] for k in ("asset_id", "revision_id", "sha256")},
            **{k: content[k] for k in ("media_sha256", "subject_id", "purpose")}}


def _script_dependency(store, story_id, script_ref):
    if script_ref is None:
        return []
    script = store.get_asset(script_ref["asset_id"], script_ref["revision_id"])
    _checked("reference.source.kind", "script", script["kind"])
    _checked("reference.source.story_id", story_id, script["story_id"])
    _checked("reference.source.sha256", script_ref["sha256"], script["sha256"])
    return [{"asset_id": script["asset_id"], "revision_id": script["revision_id"], "role": "target_script"}]


@traced("references.import", kind="tool")
def import_reference(store, *, story_id, asset_id, subject_id, purpose, path, parent_revision=None, producer="assets",
                     origin=None):
    if not _identifier(subject_id) or not isinstance(purpose, str) or purpose not in SUBJECT_KINDS:
        raise ValueError("无效的参考对象或用途。")
    if parent_revision is not None:
        parent = store.get_asset(asset_id, parent_revision)
        _checked("reference.kind", "reference_media", parent["kind"])
        _checked("reference.story", story_id, parent["story_id"])
        _checked("reference.subject", subject_id, parent["content"]["subject_id"])
        _checked("reference.purpose", purpose, parent["content"]["purpose"])
    data = _read_bytes(Path(path))
    mime = _mime(data)
    _checked("reference.media_type", "audio" if purpose == "voice" else "image", mime.split("/")[0])
    digest = hashlib.sha256(data).hexdigest()
    blob = _blob_path(store, digest)
    _install(data, blob)
    content = {"schema_version": "reference-media/1", "subject_id": subject_id, "purpose": purpose,
               "media_sha256": digest, "mime_type": mime, "bytes": len(data), **_media_info(blob, mime)}
    if origin is not None:
        content["origin"] = origin
    dependencies = _script_dependency(store, story_id, (origin or {}).get("script_ref"))
    asset = store.publish_asset(asset_id, content, kind="reference_media", producer=producer,
                                story_id=story_id, parent_revision=parent_revision, dependencies=dependencies)
    return asset_pin(asset)


@traced("references.resolve", kind="tool")
def resolve_references(store, required_refs, *, story_id=None):
    """Return provider input data from verified local bytes, never from a mutable source URL."""
    issues = pin_issues(required_refs)
    if issues:
        raise ValueError("镜头参考锁定信息无效：" + json.dumps(issues, ensure_ascii=False))
    resolved = []
    scope = story_id
    for pin in required_refs:
        asset = store.get_asset(pin["asset_id"], pin["revision_id"])
        _checked("reference.asset_kind", "reference_media", asset["kind"])
        if scope is None:
            scope = asset["story_id"]
        _checked("reference.story", scope, asset["story_id"])
        content = asset["content"]
        _checked("reference.schema", "reference-media/1", content.get("schema_version"))
        _checked("reference.pin", pin, asset_pin(asset))
        blob = _blob_path(store, pin["media_sha256"])
        data = _read_bytes(blob)
        _checked("reference.media_sha256", pin["media_sha256"], hashlib.sha256(data).hexdigest())
        _checked("reference.bytes", content["bytes"], len(data))
        mime = _mime(data)
        _checked("reference.mime_type", content["mime_type"], mime)
        _checked("reference.media_type", "audio" if pin["purpose"] == "voice" else "image", mime.split("/")[0])
        for key, value in _media_info(blob, mime).items():
            _checked("reference." + key, content.get(key), value)
        uri = "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")
        properties = {k: content[k] for k in ("bytes", "width", "height", "duration_s") if k in content}
        resolved.append({**pin, **properties, "mime_type": mime, "provider_url": uri})
    return resolved


@traced("references.bind_project", kind="tool")
def bind_project(store, project, *, story_id, shot_id, pins, spec_asset_id, parent_revision=None, script_ref=None,
                 replace_pin=None):
    """Publish a copy with explicit reference pins. Existing revisions and caller input remain intact."""
    updated, dependencies = _prepare_binding(store, project, story_id=story_id, shot_id=shot_id, pins=pins,
        spec_asset_id=spec_asset_id, parent_revision=parent_revision, script_ref=script_ref, replace_pin=replace_pin)
    return store.publish_asset(spec_asset_id, updated, kind="production_spec", producer="production",
                               story_id=story_id, parent_revision=parent_revision, dependencies=dependencies)


@traced("references.prepare_binding", kind="validation", capture_output=False)
def _prepare_binding(store, project, *, story_id, shot_id, pins, spec_asset_id, parent_revision, script_ref, replace_pin):
    """Validate the spec and probe immutable media before acquiring a publication transaction."""
    from pianchang.production import validate
    from pianchang.story_spec import digest_json, validate_compilation

    _checked("reference.bind.nonempty", True, isinstance(pins, list) and bool(pins))
    issues = pin_issues(pins)
    if issues:
        raise ValueError("绑定的参考无效：" + json.dumps(issues, ensure_ascii=False))
    if parent_revision is not None:
        prior = store.get_asset(spec_asset_id, parent_revision)
        _checked("reference.bind.parent_kind", "production_spec", prior["kind"])
        _checked("reference.bind.base", prior["sha256"], digest_json(project))
    updated = copy.deepcopy(project)
    if "reference_story_id" in updated:
        _checked("reference.bind.story", story_id, updated["reference_story_id"])
    updated["reference_story_id"] = story_id
    shot_ids = [shot_id] if isinstance(shot_id, str) else list(shot_id)
    shots = {s["id"]: s for s in updated["shots"]}
    if not shot_ids or any(sid not in shots for sid in shot_ids):
        raise ValueError("工程内找不到目标镜头。")
    for sid in shot_ids:
        shot = shots[sid]
        current = shot.get("required_refs", [])
        issues = pin_issues(current)
        if issues:
            raise ValueError("镜头已有的参考无效：" + json.dumps(issues, ensure_ascii=False))
        # Replace this asset's older version in place; other portraits keep their order.
        bound = {(p["subject_id"], p["purpose"], p["asset_id"]): p for p in current if p != replace_pin}
        for p in pins:
            if p["purpose"] == "voice":
                for key in list(bound):
                    if key[:2] == (p["subject_id"], "voice") and key[2] != p["asset_id"]:
                        del bound[key]
            bound[p["subject_id"], p["purpose"], p["asset_id"]] = copy.deepcopy(p)
        shot["required_refs"] = list(bound.values())
    report = validate_compilation(updated) if "script_source" in updated else validate(updated)
    if not report.get("ready_for_generation", report.get("spec_pass")):
        raise ValueError("绑定后的制作规格未通过检查：" + json.dumps([i for i in report["issues"]
                                                          if i["severity"] == "error"], ensure_ascii=False))
    dependencies = _script_dependency(store, story_id, script_ref)
    verified = set()
    for item in updated["shots"]:
        required = item.get("required_refs", [])
        issues = pin_issues(required)
        if issues:
            raise ValueError("镜头参考锁定信息无效：" + json.dumps(issues, ensure_ascii=False))
        # Shared shot references point to the same immutable bytes. Probe each exact pin once
        # in this publication while retaining a dependency for every shot that uses it.
        unseen = [pin for pin in required if tuple(sorted(pin.items())) not in verified]
        if unseen:
            resolve_references(store, unseen, story_id=story_id)
            verified.update(tuple(sorted(pin.items())) for pin in unseen)
        dependencies.extend({"asset_id": pin["asset_id"], "revision_id": pin["revision_id"],
                             "role": "required_reference", "source_id": item["id"]} for pin in required)
    return updated, dependencies


def _export_new(asset, output):
    output = Path(output)
    data = (json.dumps(asset["content"], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _link_new(data, output)


@traced("cli.reference_assets", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description="登记不可变的媒体参考，按镜头锁定版本并导出新制作规格。")
    parser.add_argument("--store", required=True, type=Path, help="协作资产库 SQLite 文件")
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("import")
    register.add_argument("--story-id", required=True)
    register.add_argument("--asset-id", required=True)
    register.add_argument("--subject-id", required=True)
    register.add_argument("--purpose", required=True, choices=sorted(SUBJECT_KINDS))
    register.add_argument("--file", type=Path, required=True)
    register.add_argument("--parent-revision", help="更新同一资产时必须给出当前基础版本")
    bind = sub.add_parser("bind")
    bind.add_argument("--story-id", required=True)
    bind.add_argument("--project", type=Path, required=True)
    bind.add_argument("--shot-id", required=True)
    bind.add_argument("--reference", required=True, action="append", nargs=2, metavar=("ASSET_ID", "REVISION"))
    bind.add_argument("--spec-asset-id", required=True)
    bind.add_argument("--parent-revision")
    bind.add_argument("--output", required=True, type=Path)
    export = sub.add_parser("export")
    export.add_argument("--spec-asset-id", required=True)
    export.add_argument("--revision-id", required=True)
    export.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    store = CollabStore(args.store)
    if args.command == "import":
        result = import_reference(store, story_id=args.story_id, asset_id=args.asset_id, subject_id=args.subject_id,
                                  purpose=args.purpose, path=args.file, parent_revision=args.parent_revision)
    else:
        if args.output.exists():
            raise ValueError("输出文件已存在，请提供新路径。")
        if args.command == "bind":
            project = json.loads(args.project.read_text(encoding="utf-8-sig"))
            pins = []
            for identifier, revision in args.reference:
                asset = store.get_asset(identifier, revision)
                _checked("reference.kind", "reference_media", asset["kind"])
                pins.append(asset_pin(asset))
            asset = bind_project(store, project, story_id=args.story_id, shot_id=args.shot_id, pins=pins,
                                 spec_asset_id=args.spec_asset_id, parent_revision=args.parent_revision)
        else:
            asset = store.get_asset(args.spec_asset_id, args.revision_id)
            _checked("spec.kind", "production_spec", asset["kind"])
        _export_new(asset, args.output)
        result = {**{k: asset[k] for k in ("asset_id", "revision_id", "sha256")}, "output": str(args.output)}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
