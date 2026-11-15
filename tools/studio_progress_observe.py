"""Read one accepted cloud task and save its result outside the production workspace."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from pianchang.app_service import write_json
from pianchang.bailian_video import BailianVideoClient, load_config
from pianchang.acceptance import file_sha256
from pianchang.story_spec import digest_json
from trace_system import current_context, event, get_tracer, register_secret, traced


def _read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _check(path, expected, actual):
    passed = expected == actual
    event("studio.video.observation.check", {"path": path, "expected": expected, "actual": actual,
          "status": "PASS" if passed else "FAIL"}, kind="validation")
    if not passed:
        raise ValueError("视频查询与原委托不符：" + path)


def _bound(root, project_id, job_id, shot_id, task_id, candidate, config):
    for name, value, pattern in (("project", project_id, r"[0-9a-f]{16}"), ("job", job_id, r"[0-9a-f]{32}"),
                                 ("shot", shot_id, r"[\w-]{1,128}"), ("task", task_id, r"[A-Za-z0-9_-]{1,128}"),
                                 ("candidate", candidate, r"[0-9a-f]{24}(?:-[0-9a-f]{12})?")):
        if not isinstance(value, str) or not re.fullmatch(pattern, value) or value in {".", ".."}:
            raise ValueError("无效标识：" + name)
    state = _read(root / "outputs/web/state.json")
    job = next((j for j in state["jobs"] if j["id"] == job_id), None)
    if not job:
        raise KeyError("制作任务不存在")
    _check("job.project", ["studio_video", project_id], [job["kind"], job["project_id"]])
    database = root / "outputs/collaborations/assets.sqlite3"
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("""SELECT story_id,correlation_id,payload FROM messages
                 WHERE message_id=? AND message_type='production.requested' AND recipient='video'""",
                         (job["message_id"],)).fetchone()
    if not row:
        raise ValueError("制作委托不存在")
    story, run, raw = row
    payload = json.loads(raw)
    plan = payload["shooting_plan"]
    _check("order.identity", [job["story_id"], job["run_id"], project_id],
           [story, run, plan["project_id"]])
    _check("order.shot", True, shot_id in plan["new_shots"] + plan["retake_shots"])
    media_dir = config.output_dir.resolve()
    manifest_file = (media_dir / candidate / "job.json").resolve()
    if not manifest_file.is_relative_to(media_dir):
        raise ValueError("候选文件路径不在视频目录")
    manifest = _read(manifest_file)
    metadata = manifest["metadata"]
    _check("manifest.source", [payload["script"], plan["plan_id"], payload["template"]["sha256"], shot_id],
           [metadata.get("script_asset_ref"), metadata.get("shooting_plan_id"),
            metadata.get("project_sha256"), metadata.get("shot_id")])
    from pianchang.video_agent import config_for_manifest
    config = config_for_manifest(config, manifest)
    snapshot = metadata.get("model_route")
    if plan.get("video_routes"):
        purpose = "video.reference_to_video" if metadata.get("required_refs") else "video.text_to_video"
        _check("manifest.authorized_route", plan["video_routes"].get(purpose), snapshot)
    _check("manifest.provider", [config.route.provider if config.route else "aliyun-bailian", config.base_url, task_id, task_id],
           [manifest.get("provider"), manifest.get("base_url"), manifest.get("task_id"),
            manifest.get("provider_response", {}).get("output", {}).get("task_id")])
    return manifest_file, config, {"project_id": project_id, "job_id": job_id, "shot_id": shot_id, "task_id": task_id,
            "story_id": story, "run_id": run, "message_id": job["message_id"], "plan_id": plan["plan_id"],
            "script_ref": payload["script"], "spec_ref": payload["template"], "manifest_sha256": digest_json(manifest),
            "source_trace_id": job.get("trace_id")}


@traced("studio.video.observe_existing_task", kind="tool", capture_inputs=False, capture_output=False)
def observe_existing_task(root, project_id, job_id, shot_id, task_id, candidate, *, client=None):
    root = Path(root).resolve()
    config = client.config if client else load_config(root / ".env", allow_missing=True)
    register_secret(config.api_key)
    manifest_file, config, binding = _bound(root, project_id, job_id, shot_id, task_id, candidate, config)
    base = (root / "outputs/web/progress-observations").resolve()
    folder = (base / job_id / shot_id / task_id).resolve()
    if not folder.is_relative_to(base):
        raise ValueError("观测目录无效")
    receipt_file = folder / "receipt.json"
    media = folder / "video.mp4"
    if receipt_file.is_file():
        receipt = _read(receipt_file)
        if (all(receipt.get(k) == v for k, v in binding.items()) and receipt.get("provider_status") == "SUCCEEDED"
                and receipt.get("media_file") == "video.mp4" and media.is_file()
                and file_sha256(media) == receipt.get("media_sha256")):
            event("studio.video.observation.reuse", binding, kind="cache")
            return receipt
    from pianchang.sdk_video import SDKVideoClient
    client = client or (SDKVideoClient(config) if config.sdk_video else BailianVideoClient(config))
    event("studio.video.observation.request", {**binding, "method": "GET", "base_url": config.base_url})
    response = client.get_task(task_id)
    event("studio.video.observation.response", {"task_id":task_id,"response":response}, kind="provider")
    output = response.get("output") or {}
    _check("response.task_id", task_id, output.get("task_id"))
    status = output.get("task_status")
    _check("response.recognized_status", True, status in {
        "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "CANCELLED", "UNKNOWN"})
    trace_id = current_context()["trace_id"]
    receipt = {**binding, "provider_status": status, "provider_response": get_tracer().sanitize(response),
               "trace_id": trace_id, "observed_at": datetime.now(timezone.utc).isoformat(),
               "media_file": None, "media_sha256": None}
    if status == "SUCCEEDED":
        if config.sdk_video:
            client.download_task(task_id, media)
        else:
            if not output.get("video_url"):
                raise ValueError("云端任务已成功，但响应中没有视频地址")
            client.download(output["video_url"], media)
        receipt.update(media_file="video.mp4", media_sha256=file_sha256(media))
    _check("manifest.original_version", binding["manifest_sha256"], digest_json(_read(manifest_file)))
    event("studio.video.observation.publish", {"receipt_sha256": digest_json(receipt), "path": str(receipt_file),
          "task_id": task_id, "status": status, "media_sha256": receipt["media_sha256"]}, kind="artifact")
    write_json(folder / (trace_id + ".json"), receipt)
    write_json(receipt_file, receipt)
    return receipt


@traced("studio.video.observation_cli", kind="cli")
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("project", "job", "shot", "task", "candidate"):
        parser.add_argument("--" + field, required=True)
    args = parser.parse_args()
    receipt = observe_existing_task(Path(__file__).resolve().parents[1], args.project, args.job, args.shot,
                                    args.task, args.candidate)
    print(json.dumps({k: receipt[k] for k in ("shot_id", "provider_status", "trace_id", "media_sha256")}))


if __name__ == "__main__":
    main()
