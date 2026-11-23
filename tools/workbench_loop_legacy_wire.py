"""Read-only reconciliation for the original single-attempt Windows HTTPX voice transport."""
from contextlib import closing
from datetime import datetime
import hashlib
from importlib import metadata
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import sqlite3


# Trace captured both the module file digest and the code object running call_sdk.
# This audited build uses a fresh HTTPTransport with retries=0, no redirects or auth retries.
_SDK_SOURCE = "c35066885eb8d8be63dc5b4ce08ba649e31cc35f59a9de7f90f8f28815e9732f"
_SDK_CODE = "4ebcd939e00d08a555f7800b34e15efff76505c1556dde94f1b72a719eb118ed"
_SYNC_LIBRARIES = {
    "httpx": ("0.28.1", ["_transports/default.py"],
              "6b7474e1262a9c447274d59aef2099f95ebd6e79e29729cf63296b647d02da2b"),
    "httpcore": ("1.0.9", ["_sync/connection_pool.py", "_sync/http11.py", "_sync/http_proxy.py",
                          "_sync/connection.py", "_backends/sync.py", "_exceptions.py"],
                 "e66c9012879e5588e3f7ccacf08f117d8fe9c1c06d4df2cbd12ce1df2938130f"),
}
_EVENTS = [
    ("span.start", "models.sdk.call"), ("model", "model.route"), ("http", "http.request"),
    ("http", "http.proxy.selected"), ("http", "http.error"), ("span.end", "models.sdk.call"),
]
_FRAME = re.compile(r'^  File "([^"\n]+)", line (\d+), in (\w+)\r?$', re.MULTILINE)


def _installed_at(frame_source, timestamp):
    # Only the same machine's pre-existing installation can corroborate old Trace records.
    # These versions map origin reads/writes and truncated responses to different errors.
    # ConnectError is mapped only while opening a stream or negotiating TLS. An HTTPS
    # tunnel completes both steps before calling handle_request with the origin POST.
    if os.name != "nt":
        return False
    for package, (version, files, expected) in _SYNC_LIBRARIES.items():
        dist = metadata.distribution(package)
        if dist.version != version:
            return False
        folder = Path(dist.locate_file(package)).resolve()
        spec = find_spec(package)
        if not spec or not spec.origin or Path(spec.origin).resolve().parent != folder:
            return False
        if package == "httpx" and frame_source != folder / files[0]:
            return False
        if folder.parent != frame_source.parents[2]:
            return False
        digest = hashlib.sha256()
        for name in files:
            path = folder / name
            stat = path.stat()
            if max(stat.st_ctime, stat.st_mtime) > timestamp:
                return False
            digest.update(name.encode() + b"\0" + path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
        if digest.hexdigest() != expected:
            return False
    return True


def _classify(root, rows, request_seq):
    if [(r[1], r[2]) for r in rows] != _EVENTS or rows[2][0] != request_seq:
        return None
    start, route, wire, proxy, error, end = (json.loads(r[4]) for r in rows)
    attrs = start["attributes"]
    sdk = (root / "pianchang/sdk_transport.py").resolve()
    if (attrs["module"], attrs["qualname"], attrs["source_sha256"], attrs["loaded_code_sha256"]) != (
            "pianchang.sdk_transport", "call_sdk", _SDK_SOURCE, _SDK_CODE):
        return None
    if Path(attrs["source_file"]).resolve() != sdk:
        return None
    if (route["operation"], route["route"]["adapter_version"], route["route"]["sdk_provider"],
            route["route"]["provider"]) != ("completion", "1.109.1", "openai", "openai"):
        return None
    headers = {k.lower():v for k,v in wire["headers"].items()}
    if (wire["method"] != "POST" or wire["url"] != "https://api.openai.com/v1/chat/completions"
            or headers.get("x-stainless-retry-count") != "0" or headers.get("x-stainless-package-version") != "3.52.0"
            or "audio" not in wire["body"]["modalities"] or not isinstance(wire["body"].get("audio"), dict)):
        return None
    if (proxy["mode"], proxy["source"], proxy["proxy_scheme"], proxy["target_host"]) != (
            "proxy", "windows", "http", "api.openai.com"):
        return None
    failure = end["error"]
    if (error["error_type"], end["status"], failure["type"], failure["message"]) != (
            "ConnectError", "ERROR", "ConnectError", "[Errno 2] No such file or directory"):
        return None
    stack = failure["stack"]
    if not stack.rstrip().endswith("httpx.ConnectError: " + failure["message"]):
        return None
    frames = [(Path(name).resolve(), line, function) for name, line, function in _FRAME.findall(stack)]
    if len(frames) < 6:
        return None
    # The original wire called HTTPX directly and its failure came from mapping a
    # real httpcore exception. A stored error message or a custom class name is insufficient.
    transport = frames[-3][0]
    if (frames[-2][0].name != "contextlib.py" or frames[-6:] != [
            (sdk, "100", "handle_request"), (sdk, "134", "_send"), (sdk, "56", "handle_request"),
            (transport, "249", "handle_request"), (frames[-2][0], "155", "__exit__"),
            (transport, "118", "map_httpcore_exceptions")]):
        return None
    if not _installed_at(transport, datetime.fromisoformat(rows[0][3]).timestamp()):
        return None
    return {"delivery":"not_sent", "proof":"legacy_httpx_connect_before_origin_write",
            "phase":"connect_or_tls", "request_seq":request_seq, "error_seq":rows[-2][0],
            "end_seq":rows[-1][0], "exception_type":"ConnectError", "errno":2}


def classify_legacy_voice_call(root, call):
    """Prove that this particular historical request failed before writing the origin POST."""
    trace, span, seq = (call.get(key) for key in ("trace_id", "span_id", "request_seq"))
    if (not all(re.fullmatch(r"[a-f0-9]{32}", str(v)) for v in (trace, span))
            or type(seq) is not int or call.get("request_kind") != "audio"
            or call.get("response_seq") is not None or call.get("error_type") != "ConnectError"):
        return None
    root = Path(root).resolve()
    path = root / "outputs/traces/trace.sqlite3"
    try:
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as db:
            rows = db.execute("SELECT trace_seq,kind,name,timestamp,data FROM events "
                              "WHERE trace_id=? AND span_id=? ORDER BY trace_seq LIMIT 7", (trace,span)).fetchall()
        return _classify(root, rows, seq)
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError, metadata.PackageNotFoundError):
        return None
