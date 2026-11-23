"""Loopback-only HTTP server for the drama agent workbench (Python stdlib)."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from trace_system import TraceError, current_context, get_tracer, traced, use_context
from pianchang.preview_access import PreviewBlocked
from pianchang.video_execution import StandaloneAgentRetired

ROOT = Path(__file__).resolve().parents[1]
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css",
                "/agent-chat.js":"agent-chat.js","/agent-chat.css":"agent-chat.css", "/pianchang.svg": "pianchang.svg",
                  "/studio.js": "studio.js", "/studio.css": "studio.css", "/progress": "progress.html", "/progress.js": "progress.js",
                "/collaborations": "collaborations.html", "/collaborations.html": "collaborations.html",
                "/collaborations.js": "collaborations.js", "/collaborations.css": "collaborations.css"}
MAX_BODY = 32 * 1024
PROGRESS_FILES = {"/": "progress.html", "/progress.js": "progress.js", "/studio.js": "studio.js",
                  "/studio.css": "studio.css", "/styles.css": "styles.css", "/pianchang.svg": "pianchang.svg"}
PROGRESS_ROUTE = re.compile(r"/api/projects/([\w-]+)/studio/progress(/media)?")
WORKBENCH_JS = ("studio.js", "agent-chat.js", "app.js")
WORKBENCH_CSS = ("styles.css", "studio.css", "agent-chat.css")
CSP = ("default-src 'self'; script-src 'self'{scripts}; style-src 'self'{styles}; "
       "img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


def create_server(service, host="127.0.0.1", port=8765, *, progress_only=False):
    if host != "127.0.0.1":
        raise ValueError("工作台仅允许绑定 127.0.0.1。")
    tracer = getattr(service, "tracer", None) or get_tracer(Path(getattr(service, "root", ROOT)) / "outputs" / "traces")
    from pianchang.studio_progress_server import ProgressSource
    progress_source = service if progress_only else ProgressSource(
        getattr(service, "root", ROOT), state_dir=getattr(service, "state_dir", None),
        video_root=getattr(getattr(service, "video_config", None), "output_dir", None), tracer=tracer)

    class Handler(BaseHTTPRequestHandler):
        server_version = "DramaWorkbench/1"
        protocol_version = "HTTP/1.1"
        _active_trace = None
        _response_started = False

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            # Request URLs and exception strings may include private input.
            pass

        def _headers(self, status, content_type, size, extra=None):
            self._record_headers(status, content_type, size, extra)
            self._send_headers(status, content_type, size, extra)

        def _record_headers(self, status, content_type, size, extra=None):
            if self._active_trace:
                self._active_trace.event("http.response.headers", {"status": status, "content_type": content_type,
                                         "content_length": size, "headers": extra or {}}, kind="http")
                if status >= 400:
                    self._active_trace.set_status("ERROR", f"HTTP {status}")

        def _send_headers(self, status, content_type, size, extra=None):
            self.send_response(status)
            if self._active_trace:
                self.send_header("X-Trace-Id", self._active_trace.trace_id)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if not extra or "Content-Security-Policy" not in extra:
                self.send_header("Content-Security-Policy", CSP.format(scripts="", styles=""))
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self._response_started = True

        def _prepare_json(self, status, value):
            if hasattr(service, "sanitize"):
                value = service.sanitize(value)
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if self._active_trace:
                if urlsplit(self.path).path.startswith(("/api/traces", "/api/trace-artifacts")):
                    # Do not recursively capture the tracing system observing its
                    # own event history. Store a precise digest and query identity.
                    details = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                               "body_omitted_reason": "trace_query_response_avoids_recursive_capture"}
                    if isinstance(value, dict):
                        details["result_trace_ids"] = [t.get("trace_id") for t in value.get("traces", [])]
                        details["trace_id"] = value.get("trace_id")
                else:
                    details = {"body": value, "bytes": len(data)}
                self._active_trace.event("http.response.body", details, kind="http")
            self._record_headers(status, "application/json; charset=utf-8", len(data))
            return data

        def _send_json(self, status, data):
            self._send_headers(status, "application/json; charset=utf-8", len(data))
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, status, value):
            self._send_json(status, self._prepare_json(status, value))

        def _boundary(self, mutation=False):
            expected = f"127.0.0.1:{self.server.server_address[1]}"
            if self.headers.get_all("Host") != [expected]:
                self.close_connection = True
                self._json(403, {"error": "仅接受本机工作台请求。"})
                return False
            origin = self.headers.get("Origin")
            if (mutation or origin is not None) and origin != "http://" + expected:
                self.close_connection = True
                self._json(403, {"error": "请求来源不匹配，请在本机工作台操作。"})
                return False
            # A top-level link from the workbench or chat may open the read-only viewer.
            # Keep the same host/origin restrictions on all other requests.
            viewer_navigation = self.command in {"HEAD", "GET"} and urlsplit(self.path).path == ("/" if progress_only else "/progress")
            if self.headers.get("Sec-Fetch-Site") == "cross-site" and not viewer_navigation:
                self.close_connection = True
                self._json(403, {"error": "不接受跨站请求。"})
                return False
            return True

        def _path(self):
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if parsed.scheme or parsed.netloc or "\\" in path or "\x00" in path or any(part in {".", ".."} for part in path.split("/")):
                raise KeyError("地址不存在。")
            return path

        def _file(self, path, ranges=False, mime=None):
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            extra = {"Accept-Ranges": "bytes"} if ranges else {}
            requested = self.headers.get("Range") if ranges else None
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                valid = bool(match and any(match.groups()) and size)
                if valid:
                    left, right = match.groups()
                    if left:
                        start = int(left)
                        end = min(int(right), size - 1) if right else size - 1
                    else:
                        count = int(right)
                        start, end = max(0, size - count), size - 1
                        valid = count > 0
                    valid = valid and 0 <= start <= end < size
                if not valid:
                    self._headers(416, "application/octet-stream", 0, {"Content-Range": f"bytes */{size}"})
                    return
                status = 206
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            content_type = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                            ".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
                            ".md": "text/plain; charset=utf-8", ".wav": "audio/wav"}.get(path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            length = max(0, end - start + 1)
            if self._active_trace:
                self._active_trace.event("http.response.file", {"path": str(path.resolve()), "size": size,
                                         "range_start": start, "range_end": end, "content_length": length,
                                         "binary_capture": "served_file_reference"}, kind="artifact")
            self._headers(status, mime or content_type, length, extra)
            if self.command == "HEAD":
                return
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    data = stream.read(min(256 * 1024, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)

        def _bundle_content(self, names, separator):
            pieces, sources = [], []
            for name in names:
                path=ROOT/"web"/name;data=path.read_bytes()
                pieces.append(data);sources.append({"path":str(path),"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data)})
            return separator.join(pieces), sources

        def _bundle(self,names,mime):
            body, sources = self._bundle_content(names, b"\n;\n" if mime.startswith("text/javascript") else b"\n")
            with tracer.read_batch():
                self._active_trace.event("http.response.bundle",{"sources":sources,"bytes":len(body),"sha256":hashlib.sha256(body).hexdigest()},kind="artifact")
                self._record_headers(200,mime,len(body))
            self._send_headers(200,mime,len(body))
            if self.command!="HEAD":self.wfile.write(body)

        def _workbench_index(self):
            # The first navigation gets the application in one traced round trip.
            # Only these exact static bytes are authorized as inline code by CSP.
            template, index_sources = self._bundle_content(("index.html",), b"")
            style, css_sources = self._bundle_content(WORKBENCH_CSS, b"\n")
            script, js_sources = self._bundle_content(WORKBENCH_JS, b"\n;\n")
            # CSP hashes use the text after the HTML parser normalizes newlines.
            style = style.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            script = script.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            css_tag = b'<link rel="stylesheet" href="/workbench.css">'
            js_tag = b'<script src="/workbench.js" defer></script>'
            if (template.count(css_tag) != 1 or template.count(js_tag) != 1 or template.count(b"</body>") != 1
                    or b"</style" in style.lower() or b"</script" in script.lower()):
                self._file(ROOT / "web/index.html")
                return
            body = template.replace(css_tag, b"<style>" + style + b"</style>").replace(js_tag, b"")
            body = body.replace(b"<html ", b'<html data-video-detail="true" ', 1)
            body = body.replace(b"</body>", b"<script>" + script + b"</script>\n</body>")
            def csp_hash(content):
                return " 'sha256-" + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii") + "'"
            headers = {"Content-Security-Policy": CSP.format(scripts=csp_hash(script), styles=csp_hash(style))}
            mime = "text/html; charset=utf-8"
            with tracer.read_batch():
                self._active_trace.event("http.response.bundle", {
                    "sources": index_sources + css_sources + js_sources, "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest()}, kind="artifact")
                self._record_headers(200, mime, len(body), headers)
            self._send_headers(200, mime, len(body), headers)
            if self.command != "HEAD":
                self.wfile.write(body)

        def _handle_error(self, exc):
            if isinstance(exc, TraceError):
                raise exc
            from pianchang.image_recovery import ImageRecoveryConflict
            if self._active_trace:
                self._active_trace.event("http.exception", {"type": type(exc).__name__, "message": str(exc)}, kind="error")
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
                if self._active_trace:
                    self._active_trace.set_status("UNKNOWN", "Connection ended before response delivery could be confirmed.")
                self.close_connection = True
                return
            if isinstance(exc, (KeyError, FileNotFoundError)):
                self._json(404, {"error": "工程、素材或地址不存在。"})
            elif isinstance(exc, PreviewBlocked):
                self._json(409,{"error":str(exc),"preview_gate":{k:v for k,v in exc.gate.items() if k!="allowed_media"}})
            elif isinstance(exc,StandaloneAgentRetired):
                self._json(410,{"error":str(exc),"code":"AGENT_ENTRY_MERGED","conversation":exc.conversation})
            elif isinstance(exc, ImageRecoveryConflict):
                self._json(409, {"error": str(exc), "code": exc.code})
            elif isinstance(exc, (ValueError, TypeError)):
                message = service.sanitize(str(exc)) if hasattr(service, "sanitize") else "请求参数无效。"
                self._json(400, {"error": message})
            else:
                self._json(500, {"error": "工作台发生内部错误；请检查本机服务日志或工程状态。"})

        def _traced_request(self, operation):
            self._response_started = False
            self._active_trace = None
            try:
                with tracer.trace("HTTP " + self.command + " " + urlsplit(self.path).path, kind="http",
                                  inputs={"method": self.command, "target": self.path, "headers": dict(self.headers)},
                                  attributes={"http_method": self.command, "route": urlsplit(self.path).path,
                                              "observability": progress_only or bool(PROGRESS_ROUTE.fullmatch(urlsplit(self.path).path))
                                              or urlsplit(self.path).path.startswith(("/api/traces", "/api/trace-artifacts"))}) as request_span:
                    self._active_trace = request_span
                    operation()
            except TraceError:
                self._active_trace = None
                self.close_connection = True
                if not self._response_started:
                    self._json(503, {"error": "Trace 写入失败，已停止本次请求；请检查存储目录与磁盘空间。"})
            finally:
                self._active_trace = None

        def do_GET(self):
            self._traced_request(self._get)

        def _production_progress(self, route):
            from pianchang.studio_live_progress import read_production_progress, resolve_production_clip
            pid, serve_media = route.groups()
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            fields = {"job_id", "shot_id", "sha256"} if serve_media else {"job_id"}
            if set(params) != fields or any(len(v) != 1 for v in params.values()):
                raise ValueError("请指定本次制作任务及精确的候选版本。")
            job_id = params["job_id"][0]
            if not re.fullmatch(r"[a-f0-9]{32}", job_id):
                raise ValueError("制作任务 ID 无效。")
            if serve_media:
                sid, digest = params["shot_id"][0], params["sha256"][0]
                if not 0 < len(sid) <= 128 or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("镜头或候选版本无效。")
                with tracer.read_batch():
                    job = progress_source.job(pid, job_id)
                    clip = resolve_production_clip(progress_source.root, pid, job, sid, digest,
                                                   video_root=progress_source.video_root,state_dir=progress_source.state_dir)
                self._file(clip, ranges=True, mime="video/mp4")
                return
            def preview_url(sid, digest):
                return self._path() + "/media?" + urlencode({"job_id": job_id, "shot_id": sid, "sha256": digest})
            from pianchang.preview_access import preview_status,ReadOnlyPreviewSource
            with tracer.read_batch():
                job = progress_source.job(pid, job_id)
                result = read_production_progress(progress_source.root, pid, job, video_root=progress_source.video_root,
                                                  preview_url=preview_url)
                gate=preview_status(service if not progress_only else ReadOnlyPreviewSource(progress_source.root,progress_source.state_dir),pid)
                result["preview_gate"]={k:v for k,v in gate.items() if k not in {"allowed_media","preview"}}
                body=self._prepare_json(200,result)
            self._send_json(200,body)

        def _session_stream(self,session,cursor):
            from pi_bridge.live import read_session_events,decode_cursor
            headers={"Content-Type":"text/event-stream; charset=utf-8","Cache-Control":"no-store","Connection":"close","X-Accel-Buffering":"no"}
            self._active_trace.event("http.response.headers",{"status":200,"headers":headers,"framing":"SSE, close-delimited"},kind="http")
            self.send_response(200)
            for name,value in headers.items():self.send_header(name,value)
            self.send_header("X-Trace-Id",self._active_trace.trace_id)
            self.send_header("X-Content-Type-Options","nosniff");self.end_headers();self._response_started=True;self.close_connection=True
            started=time.monotonic();last_heartbeat=started;last_state=None
            try:
                while time.monotonic()-started<55 and not self.server.live_stop.is_set():
                    result=read_session_events(service,session,cursor)
                    state={k:v for k,v in result.items() if k not in {"events","cursor","has_more"}}
                    version=hashlib.sha256(json.dumps(state,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                    if result["events"] or version!=last_state:
                        value={**result,"state_changed":version!=last_state}
                        if version==last_state:value.pop("messages",None)
                        value=tracer.sanitize(value)
                        frame=("id: "+result["cursor"]+"\nevent: agent\ndata: "+json.dumps(value,ensure_ascii=False)+"\n\n").encode("utf-8")
                        self._active_trace.event("http.stream.frame",{"body":value,"bytes":len(frame),"sha256":hashlib.sha256(frame).hexdigest()},kind="http")
                        self.wfile.write(frame);self.wfile.flush();last_state=version
                    cursor=decode_cursor(result["cursor"])
                    if time.monotonic()-last_heartbeat>=10:
                        self._active_trace.event("http.stream.heartbeat",{"body":": connected\n\n","task_progress":False},kind="http")
                        self.wfile.write(b": connected\n\n");self.wfile.flush();last_heartbeat=time.monotonic()
                    self.server.live_stop.wait(.01 if result["has_more"] else .5)
            except (BrokenPipeError,ConnectionResetError,TimeoutError):
                self._active_trace.event("http.stream.disconnected",{"session_id":session["id"],"task_stopped":False},kind="http")

        def _get(self):
            if not self._boundary():
                return
            try:
                path = self._path()
                progress_route = PROGRESS_ROUTE.fullmatch(path)
                if progress_route:
                    self._production_progress(progress_route)
                elif progress_only:
                    self._file(ROOT / "web" / PROGRESS_FILES[path])
                elif path in {"/", "/index.html"}:
                    self._workbench_index()
                elif path=="/workbench.css":
                    self._bundle(WORKBENCH_CSS,"text/css; charset=utf-8")
                elif path=="/workbench.js":
                    self._bundle(WORKBENCH_JS,"text/javascript; charset=utf-8")
                elif path in STATIC_FILES:
                    self._file(ROOT / "web" / STATIC_FILES[path])
                elif path == "/api/bootstrap":
                    query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                    if query and query!={"initial":["1"]}:raise ValueError("首页参数无效。")
                    with tracer.read_batch():
                        result = service.bootstrap(include_jobs=False) if query else service.bootstrap()
                        body = self._prepare_json(200, result)
                    self._send_json(200, body)
                elif re.fullmatch(r"/api/projects/[a-f0-9]{16}/studio/video-detail", path):
                    from pianchang.studio_flow import studio_video_detail
                    if urlsplit(self.path).query:
                        raise ValueError("视频制作详情只接受工程 ID。")
                    with tracer.read_batch():
                        result = studio_video_detail(service, path.split("/")[3])
                        body = self._prepare_json(200, result)
                    self._send_json(200, body)
                elif re.fullmatch(r"/api/projects/[a-f0-9]{16}/studio/shot-reviews", path):
                    from pianchang.shot_approval import shot_review_panel
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if set(query) != {"request_id"} or len(query["request_id"]) != 1 or not re.fullmatch(r"[0-9a-f]{32}", query["request_id"][0]):
                        raise ValueError("请指定要审看的制作委托。")
                    with tracer.read_batch():
                        result = shot_review_panel(service, path.split("/")[3], query["request_id"][0])
                        data = self._prepare_json(200, result)
                    self._send_json(200, data)
                elif path=="/api/workspace":
                    from pianchang.workspace_read import workspace_snapshot
                    query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                    if query and (set(query)!={"project"} or len(query["project"])!=1 or not re.fullmatch(r"[a-f0-9]{16}",query["project"][0])):raise ValueError("工作区工程参数无效。")
                    pid=query.get("project",[None])[0]
                    # On a hit this is one pure read transaction, including the
                    # exact response. Commit it before writing to the socket.
                    with tracer.read_batch():
                        snapshot=workspace_snapshot(service,pid,cached_only=True)
                        data=self._prepare_json(200,snapshot) if snapshot is not None else None
                    if data is None:
                        # A miss can create/link sessions and needs its own durable
                        # mutation boundaries outside the read-only transaction.
                        snapshot=workspace_snapshot(service,pid)
                        with tracer.read_batch():
                            data=self._prepare_json(200,snapshot)
                    self._send_json(200,data)
                elif path == "/api/collaborations":
                    from pianchang.collaboration_workflow import list_runs
                    self._json(200, list_runs(service))
                elif path == "/api/reference-usage":
                    from pianchang.reference_usage import query_reference_usage
                    args = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    required = {"asset_id", "revision_id"}
                    if not required.issubset(args) or set(args) - (required | {"story_id"}) or any(
                            len(values) != 1 or not values[0] or len(values[0]) > 256 for values in args.values()):
                        raise ValueError("请提供资产 ID 和版本，可选用故事 ID 限定范围。")
                    self._json(200, query_reference_usage(
                        service.root / "outputs" / "collaborations" / "assets.sqlite3",
                        args["asset_id"][0], args["revision_id"][0], story_id=args.get("story_id", [None])[0],
                        jobs_dir=service.video_config.output_dir))
                elif path in {"/api/pi/events","/api/pi/events/stream"}:
                    from pi_bridge.service import get_sessions
                    from pi_bridge.live import read_session_events,decode_cursor
                    query=parse_qs(urlsplit(self.path).query)
                    if self.command!="GET":self._json(405,{"error":"实时事件只支持GET"});return
                    if set(query)-{"story","project","role","cursor"} or any(len(v)!=1 for v in query.values()):raise ValueError("实时事件参数无效")
                    scope={k:query[k][0] for k in ("story","project") if k in query}
                    session=get_sessions(service).resolve(scope,query.get("role",["video"])[0])
                    cursor=decode_cursor(self.headers.get("Last-Event-ID") or query.get("cursor",[""])[0])
                    if path.endswith("/stream"):self._session_stream(session,cursor)
                    else:self._json(200,read_session_events(service,session,cursor))
                elif path == "/api/pi/sessions":
                    from pi_bridge.service import get_sessions
                    args=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                    role=args.pop("role",["script"])
                    if len(role)!=1 or any(len(v)!=1 for v in args.values()):raise ValueError("会话参数无效")
                    self._json(200,get_sessions(service).view({k:v[0] for k,v in args.items()},role[0]))
                elif path == "/api/feedback-watch":
                    query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                    ids=query.get("job",[])
                    if (set(query)!={"job"} or not 1<=len(ids)<=24
                            or not all(re.fullmatch(r"[a-f0-9]{32}",jid) for jid in ids)):
                        raise ValueError("请指定要查看的任务 ID。")
                    self._json(200,_feedback_status(service.root, ids))
                elif path == "/api/jobs":
                    with tracer.read_batch():result={"jobs":service.list_jobs()}
                    self._json(200, result)
                elif re.fullmatch(r"/api/projects/[\w-]+/studio", path):
                    from pianchang.studio_flow import studio_flow
                    with tracer.read_batch():result=studio_flow(service, path.split("/")[3])
                    self._json(200, result)
                elif re.fullmatch(r"/api/projects/[\w-]+/studio/media", path):
                    from pianchang.studio_assets import studio_reference_media
                    args = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if set(args) != {"asset_id", "revision_id"} or any(len(v) != 1 or not v[0] for v in args.values()):
                        raise ValueError("请选择参考资产的 ID 和版本。")
                    with tracer.read_batch():media, mime = studio_reference_media(service, path.split("/")[3], args["asset_id"][0], args["revision_id"][0])
                    self._file(media, ranges=True, mime=mime)
                elif re.fullmatch(r"/api/story-drafts/[a-f0-9]{32}", path):
                    self._json(200, service.story_draft_detail(path.split("/")[3]))
                elif path == "/api/traces":
                    query = parse_qs(urlsplit(self.path).query)
                    def argument(name, default=None):
                        return query.get(name, [default])[0]
                    limit, offset = int(argument("limit", "50")), int(argument("offset", "0"))
                    if not 1 <= limit <= 100 or offset < 0 or offset > 100000:
                        raise ValueError("Trace 分页参数无效。")
                    result = tracer.list_traces(limit=limit, offset=offset, kind=argument("kind"),
                                                status=argument("status"), search=argument("search"))
                    self._json(200, {"traces": result, "limit": limit, "offset": offset})
                elif re.fullmatch(r"/api/traces/[a-f0-9]{32}(?:/(?:export|verify))?", path):
                    parts = path.split("/")
                    trace_id = parts[3]
                    result = tracer.export_trace(trace_id) if len(parts) == 5 and parts[4] == "export" else (
                             tracer.verify(trace_id) if len(parts) == 5 else tracer.get_trace(trace_id))
                    if len(parts) == 4:
                        diagnostic = service.asset_diagnostics_for_trace(trace_id)
                        if diagnostic:
                            result["asset_diagnostics"] = diagnostic
                    self._json(200, result)
                elif re.fullmatch(r"/api/traces/[a-f0-9]{32}/events", path):
                    query = parse_qs(urlsplit(self.path).query)
                    after = int(query.get("after", ["0"])[0])
                    limit = int(query.get("limit", ["1000"])[0])
                    if after < 0 or not 1 <= limit <= 1000:
                        raise ValueError("事件分页参数无效。")
                    events = tracer.get_events(path.split("/")[3], after=after, limit=limit + 1,
                                              span_id=query.get("span", [None])[0])
                    visible = events[:limit]
                    self._json(200, {"events": visible, "has_more": len(events) > limit,
                                     "next_after": visible[-1]["trace_seq"] if visible else after})
                elif re.fullmatch(r"/api/trace-artifacts/[a-f0-9]{64}", path):
                    self._json(200, tracer.get_artifact(path.split("/")[3]))
                elif re.fullmatch(r"/api/projects/[\w-]+", path):
                    with tracer.read_batch():result=service.project_detail(path.split("/")[3])
                    self._json(200, result)
                elif re.fullmatch(r"/assets/[\w-]+", path):
                    self._file(Path(service.resolve_asset(path.split("/")[2])), ranges=True)
                else:
                    raise KeyError(path)
            except Exception as exc:
                self._handle_error(exc)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            self._traced_request(self._post)

        def _post(self):
            if progress_only:
                self.close_connection = True
                self._json(405, {"error": "进度页仅供查看。"})
                return
            if not self._boundary(mutation=True):
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    self._json(400, {"error": "不支持分块请求。"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self.close_connection = True
                    self._json(415, {"error": "请求必须使用 application/json。"})
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise ValueError("请求长度无效。")
                length = int(lengths[0])
                path = self._path()
                upload = bool(re.fullmatch(r"/api/projects/[\w-]+/studio/references", path))
                feedback_input = re.fullmatch(
                    r"/api/(?:projects/(?P<project>[a-f0-9]{16})|story-drafts/(?P<story>[a-f0-9]{32}))/feedback-input", path)
                limit = 12 * 1024 * 1024 if upload else (1024 * 1024 if path == "/api/studio/scripts" else MAX_BODY)
                if not 0 < length <= limit:
                    self.close_connection = True
                    self._json(413, {"error": f"请求过大或为空；上限 {limit // 1024} KB。"})
                    return
                raw_body = self.rfile.read(length)
                captured = {"captured_wire_sha256": hashlib.sha256(raw_body).hexdigest(), "bytes": len(raw_body)}
                if upload:
                    captured["binary_capture"] = "upload represented by wire digest and verified media digest"
                elif feedback_input:
                    # Unvalidated text can contain credentials pasted in error. The accepted,
                    # typed answer is traced by the service before it is handed to the watcher.
                    captured["body_representation"] = "wire digest; accepted fields in workbench.feedback.reply.accepted"
                else:
                    captured.update(body_text=raw_body.decode("utf-8", errors="replace"), text_encoding="utf-8-with-replacement")
                self._active_trace.event("http.request.body", captured, kind="http")
                body = json.loads(raw_body.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("JSON 数值无效。")))
                if not isinstance(body, dict):
                    raise ValueError("请求必须为 JSON 对象。")
                key = self.headers.get("Idempotency-Key")
                if path == "/api/pi/script-input":
                    from pi_bridge.service import get_sessions
                    from tools.workbench_loop_script import accept_script_reply
                    self._json(202, accept_script_reply(get_sessions(service), body))
                    return
                if feedback_input:
                    from tools.workbench_loop_input import answer_request
                    self._json(202, answer_request(service.root, feedback_input["project"], body,
                                                   story_id=feedback_input["story"]))
                    return
                if path in {"/api/pi/messages","/api/pi/stop"}:
                    from pi_bridge.service import get_sessions
                    allowed={"scope","role"} if path.endswith("/stop") else {"scope","role","text","message_id","mode","continue_commission"}
                    if set(body)-allowed:raise ValueError("会话请求包含未知字段")
                    sessions=get_sessions(service)
                    result=(sessions.stop(body["scope"],body["role"]) if path.endswith("/stop") else
                            sessions.submit(body["scope"],body["role"],body["text"],body.get("message_id"),
                                            mode=body.get("mode","work"),continue_commission=body.get("continue_commission")))
                    self._json(202,result)
                    return
                if path == "/api/studio/scripts":
                    self._json(202, {"job": service.submit("studio_script", None, body, key)})
                    return
                studio = re.fullmatch(r"/api/projects/([\w-]+)/studio/(references|bindings|video|review|review-confirm|shots/approve|generate-reference|asset-production|asset-regenerate|asset-approve|asset-inspect|assets/approve)", path)
                if studio:
                    from pianchang.studio_assets import bind_studio_reference, upload_studio_reference
                    pid, action = studio.groups()
                    if action == "review-confirm":
                        from pianchang.studio_recovery import confirm_review
                        self._json(200, confirm_review(service, pid, body))
                    elif action == "shots/approve":
                        from pianchang.shot_approval import approve_shot
                        self._json(200, approve_shot(service, pid, body))
                    elif action == "review":
                        self._json(202, {"job": service.submit("studio_review", pid, body, key)})
                    elif action == "references":
                        result = upload_studio_reference(service, pid, body)
                        # Only inspect this exact imported revision. A replay must never generate replacement media.
                        pin = result["pin"]
                        pin_key = hashlib.sha256(json.dumps(pin, sort_keys=True).encode()).hexdigest()
                        job = service.submit("asset_inspect", pid, {"pin": pin}, "inspect-" + pin_key)
                        self._json(201, {**result, "job": job})
                    elif action == "asset-approve":
                        from pianchang.asset_dispatch import approve_asset
                        self._json(200, approve_asset(service, pid, body))
                    elif action == "assets/approve":
                        from pianchang.asset_dispatch import manually_approve_asset
                        self._json(200, manually_approve_asset(service, pid, body))
                    elif action in ("asset-production", "asset-regenerate", "asset-inspect"):
                        self._json(202, {"job": service.submit(action.replace("-", "_"), pid, body, key)})
                    elif action == "bindings":
                        self._json(200, bind_studio_reference(service, pid, body))
                    else:
                        task = "studio_video" if action == "video" else "studio_reference"
                        self._json(202, {"job": service.submit(task, pid, body, key)})
                    return
                alternate_video = re.fullmatch(r"/api/studio/projects/([\w-]+)/video", path)
                if alternate_video:
                    self._json(202, {"job": service.submit("studio_video", alternate_video[1], body, key)})
                    return
                if path == "/api/projects":
                    self._json(202, {"job": service.submit("story", None, body, key)})
                    return
                cancel_match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/cancel", path)
                if cancel_match:
                    self._json(200, {"job": service.cancel_agent(cancel_match.group(1))})
                    return
                match = re.fullmatch(r"/api/projects/([\w-]+)/(generate|resume|select|check|export|agent)", path)
                if not match:
                    raise KeyError(path)
                pid, action = match.groups()
                if action == "select":
                    self._json(200, {"project": service.select(pid, body)})
                else:
                    self._json(202, {"job": service.submit(action, pid, body, key)})
            except Exception as exc:
                self.close_connection = True
                self._handle_error(exc)

        def do_OPTIONS(self):
            self._traced_request(lambda: self._json(405, {"error": "仅支持 GET、HEAD 和 POST。"}))

        do_DELETE = do_PUT = do_PATCH = do_OPTIONS

    class WorkbenchServer(ThreadingHTTPServer):
        def __init__(self,address,handler):
            self._trace_storage=None
            self.live_stop=threading.Event()
            super().__init__(address,handler)
            storage=tracer.storage_session()
            try:storage.__enter__()
            except BaseException:
                super().server_close();raise
            self._trace_storage=storage

        def server_close(self):
            self.live_stop.set()
            try:super().server_close()
            finally:
                storage=self._trace_storage;self._trace_storage=None
                if storage:storage.__exit__(None,None,None)

    server = WorkbenchServer((host, port), Handler)
    server.daemon_threads = True
    return server


def _watch_recovery_bound(service,context,stop):
    from pi_bridge.recovery import watch_recovery
    with use_context(context,tracer=service.tracer):
        try:watch_recovery(service,stop)
        except Exception as exc:
            service.tracer.end_span(context,status="ERROR",error={"type":type(exc).__name__,"message":str(exc)})
            raise
        else:service.tracer.end_span(context,status="OK")


@traced("workbench.start_listener", kind="tool", capture_output=False)
def _start_listener(port):
    from pianchang.app_service import AppService
    service = AppService()
    try:
        server = create_server(service, port=port)
    except Exception:
        service.close(wait=False)
        raise
    return service, server


@traced("workbench.watch.public_status",kind="tool",capture_inputs=False,capture_output=False)
def _feedback_status(root, ids):
    from tools.workbench_loop_watch import watch_status
    return {"jobs":{jid:watch_status(root,jid) for jid in dict.fromkeys(ids)}}


def _keep_feedback_watcher(service, stop, generation, context):
    from tools.workbench_loop_watch import mark_trace_pause, start_watcher, watcher_alive, watcher_paused
    from tools.workbench_loop_trace_recovery import reconcile_readonly_pause
    with use_context(context,tracer=service.tracer):
        while not stop.wait(30):
            try:
                if watcher_paused(service.root) and not reconcile_readonly_pause(service.root):
                    continue
                if not watcher_alive(service.root):
                    start_watcher(service.root,generation=generation)
            except TraceError:
                mark_trace_pause(service.root)
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    context = os.environ.pop("PIANCHANG_START_CONTEXT", "")
    with use_context(json.loads(context) if context else None, tracer=get_tracer(ROOT / "outputs/traces")):
        service, server = _start_listener(args.port)
        from tools.workbench_loop_watch import start_watcher
        generation=f"server-{os.getpid()}"
        feedback=start_watcher(service.root,generation=generation)
        if feedback.get("status")!="paused":
            threading.Thread(target=_keep_feedback_watcher,
                             args=(service,server.live_stop,generation,current_context()),
                             daemon=True,name="feedback-watch-supervisor").start()
    print(f"片场已启动，可访问 http://127.0.0.1:{server.server_address[1]}", flush=True)
    scan = None
    try:
        from pianchang.asset_dispatch import enqueue_library_assets
        # Production startup owns the backfill. Page reads and isolated AppService instances are read-only.
        scan = service.library_asset_executor.submit(enqueue_library_assets, service)
        from pi_bridge.service import get_sessions
        get_sessions(service)  # Recovery reserves the same durable video session.
        from pianchang.video_autonomy import enqueue_recovery
        service.executor.submit(enqueue_recovery, service,recover_trace_failures=True)
        recovery=service.tracer.start_span("agent.task.recovery_watch",kind="task",new_trace=True,
            inputs={"scope":"existing authorized frozen commissions only; no budget reset"})
        from pi_bridge.runtime import persist
        with use_context(recovery.context,tracer=service.tracer):
            persist(service.state_dir/"recovery-watch.json",{"trace_context":recovery.context,"kind":"production recovery watchdog"})
        threading.Thread(target=_watch_recovery_bound,args=(service,recovery.context,server.live_stop),daemon=True,name="production-recovery").start()
        from pianchang.studio_recovery import enqueue_incomplete_reviews
        service.executor.submit(enqueue_incomplete_reviews, service)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            if scan is not None:
                scan.result()
        finally:
            service.close(wait=False)


if __name__ == "__main__":
    main()
