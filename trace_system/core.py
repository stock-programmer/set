from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import json
import math
import marshal
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = "agent-trace/1"
ROOT = Path(__file__).resolve().parent.parent
_CURRENT = contextvars.ContextVar("agent_trace_context", default=None)
_TRACERS = {}
_LOCK = threading.RLock()
_WRITER_GATES = {}
_SECRETS = set()
_SECRET_LOCK = threading.RLock()
_SENSITIVE_TEXT = re.compile(r"sk-|bearer|authoriz|cookie|data:|api[_-]?key|password|secret|token|signature|https?://|[?<]",re.IGNORECASE)
_UNSET = object()
_ID = re.compile(r"^[a-f0-9]{32}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_SENSITIVE = {"authorization", "proxyauthorization", "cookie", "setcookie", "apikey",
              "password", "passwd", "secret", "clientsecret", "accesskeysecret",
              "accesstoken", "refreshtoken", "idtoken", "token", "credential",
              "credentials", "signature", "sig", "securitytoken", "ossaccesskeyid"}
_PROCESS_INSTANCE = uuid.uuid4().hex
_SOURCE_HASHES = {}


class _ArtifactReference(dict):
    """In-memory trusted marker; JSON wire shape deliberately stays unchanged."""


class _LiteralPayload(dict):
    """Already-sanitized user data whose shape conflicts with an SDK marker."""


class TraceError(RuntimeError):
    """Trace recording failed; a billable operation must not silently proceed."""


class _WriterGate:
    """Give same-process writers a turn before they contend for SQLite's lock."""

    def __init__(self):
        self._condition = threading.Condition()
        self._queue = deque()

    @contextmanager
    def hold(self):
        ticket = object()
        with self._condition:
            self._queue.append(ticket)
            try:
                while self._queue[0] is not ticket:
                    self._condition.wait()
            except BaseException:
                self._queue.remove(ticket)
                self._condition.notify_all()
                raise
        try:
            yield
        finally:
            with self._condition:
                self._queue.popleft()
                self._condition.notify_all()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _secret_key(key):
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return (normalized in _SENSITIVE or normalized.endswith("apikey")
            or normalized.endswith("password") or normalized.endswith("secret")
            or normalized.endswith("signature")
            or normalized in {"xamzsignature", "xamzcredential", "xamzsecuritytoken",
                              "xgoogsignature", "xgoogcredential", "xosssecuritytoken"})


def register_secret(secret):
    """Register a credential value before recording arbitrary provider strings."""
    if isinstance(secret, str) and secret:
        with _SECRET_LOCK:
            _SECRETS.add(secret)


def _redact_url(match):
    original = match.group(0)
    try:
        parts = urlsplit(original)
        hostname = parts.netloc.rsplit("@", 1)[-1]
        if "@" in parts.netloc:
            hostname = "[REDACTED]@" + hostname
        query = parse_qsl(parts.query, keep_blank_values=True)
        if not any(_secret_key(k) for k, _ in query) and hostname == parts.netloc:
            return original
        return urlunsplit((parts.scheme, hostname, parts.path,
                           urlencode([(k, "[REDACTED]" if _secret_key(k) else v) for k, v in query]),
                           parts.fragment))
    except ValueError:
        return "[UNPARSEABLE_URL]"


def _safe(value, secrets, seen=None, depth=0, *, literal_body=False):
    if depth > 64:
        raise TraceError("Trace payload nesting exceeds 64 levels.")
    if isinstance(value, _ArtifactReference):
        return value
    if isinstance(value, _LiteralPayload):
        # A staged user's literal can still contain a value registered since
        # staging. Recheck it without interpreting its data as an SDK pointer.
        return _LiteralPayload({"$literal": _safe(value["$literal"], secrets, seen, depth, literal_body=True),
                                "$literal_reason": "user_artifact_shape"})
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"nonfinite_number": str(value)}
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"omitted": "binary", "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, str):
        # Most validation fields are short ordinary labels/IDs. Skip the full
        # suite of redaction transforms only when neither registered secrets,
        # JSON text nor any supported credential/URL/header/XML syntax occurs.
        if not value.lstrip().startswith(("{","[")) and not _SENSITIVE_TEXT.search(value) and not any(secret in value for secret in secrets):
            return value
        if value.startswith("data:") and "," in value:
            encoded = value.encode("utf-8")
            return {"omitted": "inline_media", "media_type": value[5:].split(";", 1)[0].split(",", 1)[0],
                    "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(),
                    "hash_basis": "original_data_uri_utf8"}
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
                safe_json = _safe(parsed, secrets, seen, depth + 1)
                if _json(parsed) != _json(safe_json):
                    value = _json(safe_json)
            except (ValueError, TypeError):
                pass
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)(\bauthorization\s*:\s*)(?:Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+",
                       lambda m: m.group(1) + "[REDACTED]", value)
        value = re.sub(r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).+$",
                       lambda m: m.group(1) + "[REDACTED]", value)
        value = re.sub(r"data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=_-]+",
                       lambda m: _json({"omitted": "inline_media", "bytes": len(m.group(0)),
                                        "sha256": _sha(m.group(0)), "hash_basis": "original_data_uri_utf8"}), value)
        # Also protect credentials embedded inside JSON strings or log text.
        value = re.sub(r'''(?ix)(["']?(?:api[_-]?key|authorization|password|client[_-]?secret|access[_-]?token|refresh[_-]?token|cookie|signature)["']?\s*[:=]\s*)(["'])(.*?)(\2)''',
                       lambda m: m.group(1) + m.group(2) + "[REDACTED]" + m.group(2), value)
        value = re.sub(r'''(?ix)\b((?:api[_-]?key|password|client[_-]?secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*)([^\s,;"'}]+)''',
                       lambda m: m.group(1) + "[REDACTED]", value)
        value = re.sub(r"(<((?:[\w.-]+:)?(?:Token|AccessToken|RefreshToken|SecurityToken|Signature|Password|ApiKey|API_Key|Authorization|Cookie|ClientSecret))(?:\s[^>]*)?>)(.*?)(</\2\s*>)",
                       lambda m: m.group(1) + "[REDACTED]" + m.group(4), value, flags=re.IGNORECASE | re.DOTALL)
        # Scan tokens once. Searching with a greedy prefix followed by '?' would
        # rescan every suffix of a long non-URL token (quadratic on model text).
        return re.sub(r"[^\s<>\"']+",
                      lambda m: _redact_url(m) if "?" in m.group(0) or m.group(0).startswith(("http://", "https://")) else m.group(0), value)
    if seen is None:
        seen = set()
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        if id(value) in seen:
            raise TraceError("Trace payload contains a circular reference.")
        seen.add(id(value))
        try:
            if isinstance(value, dict):
                result = {}
                for key, child in value.items():
                    safe_key = _safe(str(key), secrets, seen, depth + 1)
                    if not isinstance(safe_key, str):
                        safe_key = "[trace-key " + _json(safe_key) + "]"
                    result[safe_key] = "[REDACTED]" if _secret_key(key) else _safe(child, secrets, seen, depth + 1)
                if not literal_body and set(value) == {"$artifact", "bytes", "media_type"}:
                    return _LiteralPayload({"$literal": result, "$literal_reason": "user_artifact_shape"})
                return result
            values = list(value)
            if isinstance(value, (set, frozenset)):
                values = sorted(values, key=lambda v: str(type(v)) + str(v) if isinstance(v, (str, int, float)) else str(type(v)))
            result = []
            for child in values:
                if (isinstance(child, (list, tuple)) and len(child) == 2
                        and isinstance(child[0], str) and _secret_key(child[0])):
                    result.append([_safe(child[0], secrets, seen, depth + 1), "[REDACTED]"])
                else:
                    result.append(_safe(child, secrets, seen, depth + 1))
            return result
        finally:
            seen.remove(id(value))
    if isinstance(value, BaseException):
        return _safe({"type": type(value).__name__, "message": str(value)}, secrets, seen, depth + 1)
    # Never inspect self/config objects or use repr(), which may reveal keys.
    return {"python_type": type(value).__module__ + "." + type(value).__qualname__}


class Tracer:
    def __init__(self, directory=None, *, inline_bytes=16384):
        self.directory = Path(directory or os.environ.get("AGENT_TRACE_DIR") or ROOT / "outputs" / "traces").expanduser().resolve()
        if type(inline_bytes) is not int or inline_bytes < 256:
            raise ValueError("inline_bytes must be an integer >= 256")
        self.inline_bytes = inline_bytes
        self.secrets = set()
        self.path = self.directory / "trace.sqlite3"
        self._writer_mutex = "Local\\PianchangTraceWriter-" + hashlib.sha256(os.path.normcase(str(self.path)).encode()).hexdigest()
        with _LOCK:
            key = (os.getpid(), os.path.normcase(str(self.path)))
            self._writer_gate = _WRITER_GATES.setdefault(key, _WriterGate())
        self._pool_lock = threading.RLock()
        self._pool_sessions = 0
        self._pool = []
        self._pool_pid = os.getpid()
        self._read_transaction = contextvars.ContextVar("trace_read_transaction",default=None)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._write_db() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS artifacts (
                        sha256 TEXT PRIMARY KEY, bytes INTEGER NOT NULL, content TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        trace_id TEXT NOT NULL, trace_seq INTEGER NOT NULL,
                        span_id TEXT NOT NULL, parent_span_id TEXT,
                        event_id TEXT NOT NULL UNIQUE, timestamp TEXT NOT NULL,
                        kind TEXT NOT NULL, name TEXT NOT NULL, data TEXT NOT NULL,
                        prev_hash TEXT NOT NULL, hash TEXT NOT NULL,
                        UNIQUE(trace_id, trace_seq));
                    CREATE INDEX IF NOT EXISTS trace_events ON events(trace_id, trace_seq);
                    CREATE INDEX IF NOT EXISTS span_events ON events(trace_id, span_id);
                """)
                db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('store_id',?)", (uuid.uuid4().hex,))
                db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,))
                version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
                if version != SCHEMA_VERSION:
                    raise TraceError("Unsupported trace storage schema.")
                self.store_id = db.execute("SELECT value FROM metadata WHERE key='store_id'").fetchone()[0]
        except (OSError, sqlite3.Error) as exc:
            raise TraceError("Cannot initialize trace storage: " + type(exc).__name__) from None

    @contextmanager
    def storage_session(self):
        """Reuse connections while a server/CLI owns the store; each write still
        commits synchronously before returning. An idle connection keeps WAL
        open, avoiding a checkpoint on the last close after every single event.
        Closing the last owner releases idle connections; in-flight ones close
        when their transaction exits. No events are buffered or dropped.
        """
        with self._pool_lock:
            if self._pool_pid != os.getpid():
                raise TraceError("Open a new Tracer after starting a child process.")
            self._pool_sessions += 1
        try:
            with self._db() as db:
                db.execute("SELECT value FROM metadata WHERE key='store_id'").fetchone()
            yield self
        finally:
            with self._pool_lock:
                self._pool_sessions -= 1
                idle = self._pool if not self._pool_sessions else []
                if not self._pool_sessions:self._pool=[]
            for db in idle:db.close()

    @contextmanager
    def read_batch(self):
        """Group trace writes for an explicitly read-only request.

        Requires an already durable root. No provider calls, domain mutations,
        background dispatch or response sends may run inside this scope. All
        events remain ordered/redacted and commit before the scope returns,
        including evidence from an ordinary validation failure. A storage error
        rolls back this unfinished batch and raises TraceError.
        """
        context=_CURRENT.get()
        if not context or context[0] is not self:
            raise TraceError("A read batch requires an existing trace root in this store.")
        if self._read_transaction.get() is not None:
            yield self
            return
        failure=None
        batch={"thread":threading.get_ident(),"records":[],"starts":{},"ends":{}}
        token=self._read_transaction.set(batch)
        try:yield self
        except TraceError:raise
        except BaseException as exc:failure=(exc,exc.__traceback__)
        finally:self._read_transaction.reset(token)
        # Pure reads do not hold the single SQLite writer while hashing files or
        # building DTOs. Redacted observations are atomically committed here,
        # before this scope can return a response or a result to its caller.
        records = batch["records"]
        if records:
            while True:
                secrets = self._secret_snapshot()
                artifacts = {}
                prepared = [self._prepare_read_record(record, secrets, artifacts) for record in records]
                with self._write_db() as db:
                    try:
                        db.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as exc:
                        owner = context[1]
                        if (getattr(exc, "sqlite_errorname", None) == "SQLITE_BUSY" and not db.in_transaction
                                and failure is None and owner and len(records) <= 1024
                                and all(r["trace_id"] == owner["trace_id"] for r in records)):
                            stopped = TraceError("Trace database operation failed: OperationalError [SQLITE_BUSY]")
                            stopped.uncommitted_read_batch = dict(owner)
                            # Only the operation identities are evidence of what was staged.
                            # They were never committed as successful child events.
                            stopped.staged_read_operations = [
                                [r["span_id"], r["parent_span_id"], r["kind"], r["name"],
                                 r["data"].get("status") if r["kind"] == "span.end" else None]
                                for r in records]
                            raise stopped from exc
                        raise
                    # A credential may have been registered during preparation or
                    # while waiting for the writer. Retry outside the transaction.
                    with _SECRET_LOCK:
                        if secrets != self._secret_snapshot():
                            continue
                        db.executemany("INSERT OR IGNORE INTO artifacts(sha256,bytes,content) VALUES(?,?,?)",
                                       artifacts.values())
                        chain = {}
                        written = [self._insert_record(db, record, chain) for record in prepared]
                        db.commit()
                for staged, final in zip(records, written):
                    staged.clear()
                    staged.update(final)
                break
        if failure:raise failure[0].with_traceback(failure[1])

    def _batch(self):
        batch=self._read_transaction.get()
        if batch and batch["thread"]!=threading.get_ident():raise TraceError("A read batch cannot cross threads.")
        return batch

    def _batch_start(self,batch,context):
        key=(context["trace_id"],context["span_id"])
        if key not in batch["starts"]:
            with self._db() as db:
                row=db.execute("SELECT * FROM events WHERE trace_id=? AND span_id=? AND kind='span.start'",key).fetchone()
            if row is None:raise TraceError("Span does not exist.")
            batch["starts"][key]=self._row(row)
        return batch["starts"][key]

    def _stage(self,batch,context,parent,kind,name,data):
        record={"trace_id":context["trace_id"],"span_id":context["span_id"],"parent_span_id":parent,
            "event_id":uuid.uuid4().hex,"timestamp":_now(),"kind":self.sanitize(str(kind)),"name":self.sanitize(str(name)),
            "data":self.sanitize(data),"pending_read_batch":True}
        batch["records"].append(record)
        return record

    @contextmanager
    def _write_db(self):
        # SQLite's busy handler does not give waiting connections FIFO priority.
        # A stream of refresh batches can otherwise repeatedly overtake a paid
        # task's evidence write until its busy timeout expires. Queue writers
        # before opening a transaction, sharing the gate across Tracer objects.
        # Reads retain the normal connection path and never wait on this gate.
        if self._pool_pid != os.getpid():
            raise TraceError("Inherited SQLite connections cannot be used in a child process.")
        with self._writer_gate.hold():
            # More than one Python process writes during a workbench handoff.
            # Windows' SQLite busy handler cannot queue those processes fairly.
            from .windows_writer import writer_mutex
            lock = writer_mutex(self._writer_mutex) if os.name == "nt" else nullcontext()
            with lock, self._db() as db:
                yield db

    @contextmanager
    def _db(self):
        db = None
        reusable = False
        try:
            with self._pool_lock:
                if self._pool_pid != os.getpid():
                    raise TraceError("Inherited SQLite connections cannot be used in a child process.")
                if self._pool_sessions and self._pool:db=self._pool.pop()
            if db is None:
                db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
                db.row_factory = sqlite3.Row
                db.create_function("trace_casefold", 1, lambda value: str(value).casefold() if value is not None else "", deterministic=True)
                db.execute("PRAGMA busy_timeout=30000")
                db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
            reusable = True
        except sqlite3.Error as exc:
            if db:
                db.rollback()
            code=getattr(exc,"sqlite_errorname",None)
            raise TraceError("Trace database operation failed: " + type(exc).__name__+(" ["+code+"]" if code else "")) from exc
        except BaseException:
            if db:db.rollback()
            raise
        finally:
            if db:
                with self._pool_lock:
                    if reusable and self._pool_sessions and len(self._pool)<8:
                        self._pool.append(db);db=None
                if db:db.close()

    def register_secret(self, secret):
        if isinstance(secret, str) and secret:
            with _SECRET_LOCK:
                self.secrets.add(secret)
                _SECRETS.add(secret)

    def _secret_snapshot(self):
        with _SECRET_LOCK:
            return tuple(sorted(_SECRETS | self.secrets, key=len, reverse=True))

    def sanitize(self, value):
        return _safe(value, self._secret_snapshot())

    def _context(self, context):
        if (not isinstance(context, dict) or not isinstance(context.get("trace_id"), str)
                or not isinstance(context.get("span_id"), str) or not _ID.fullmatch(context["trace_id"])
                or not _ID.fullmatch(context["span_id"])):
            raise TraceError("Invalid trace context.")
        if context.get("store_id") != self.store_id:
            raise TraceError("Trace context belongs to another store.")
        return {"trace_id": context["trace_id"], "span_id": context["span_id"], "store_id": self.store_id}

    def _prepare_payload(self, value, secrets, artifacts):
        safe = _safe(value, secrets)
        content = _json(safe)
        content_bytes = content.encode("utf-8")
        size = len(content_bytes)
        if size <= self.inline_bytes:
            return safe
        sha = hashlib.sha256(content_bytes).hexdigest()
        artifacts.setdefault(sha, (sha, size, content))
        return _ArtifactReference({"$artifact": sha, "bytes": size, "media_type": "application/json"})

    def _pack(self, db, value):
        artifacts = {}
        safe = self._prepare_payload(value, self._secret_snapshot(), artifacts)
        if artifacts:
            db.executemany("INSERT OR IGNORE INTO artifacts(sha256,bytes,content) VALUES(?,?,?)", artifacts.values())
        return safe

    def _prepare_read_record(self, staged, secrets, artifacts):
        data = staged["data"]
        if staged["kind"] in {"span.start", "span.end"}:
            data = dict(data)
            fields = ("inputs", "attributes") if staged["kind"] == "span.start" else ("output", "error")
            for key in fields:
                data[key] = self._prepare_payload(data[key], secrets, artifacts)
        return {**{key: staged[key] for key in ("trace_id", "span_id", "parent_span_id", "event_id", "timestamp")},
                "kind": _safe(str(staged["kind"]), secrets), "name": _safe(str(staged["name"]), secrets),
                "data": self._prepare_payload(data, secrets, artifacts)}

    def _append_in_db(self, db, context, parent_span_id, kind, name, data, *,timestamp=None,event_id=None,chain=None):
        record = {"trace_id": context["trace_id"], "span_id": context["span_id"],
                  "parent_span_id": parent_span_id, "event_id": event_id or uuid.uuid4().hex, "timestamp": timestamp or _now(),
                  "kind": self.sanitize(str(kind)), "name": self.sanitize(str(name)), "data": self._pack(db, data)}
        return self._insert_record(db, record, chain)

    def _insert_record(self, db, record, chain):
        trace_id = record["trace_id"]
        previous = chain.get(trace_id) if chain is not None else None
        if previous is None:
            previous = db.execute("SELECT trace_seq,hash FROM events WHERE trace_id=? ORDER BY trace_seq DESC LIMIT 1", (trace_id,)).fetchone()
        trace_seq, prev_hash = (previous["trace_seq"] + 1, previous["hash"]) if previous else (1, "0" * 64)
        record = {**record, "trace_seq": trace_seq, "prev_hash": prev_hash}
        digest = _sha(_json(record))
        cursor = db.execute("INSERT INTO events(trace_id,trace_seq,span_id,parent_span_id,event_id,timestamp,kind,name,data,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (trace_id, trace_seq, record["span_id"], record["parent_span_id"], record["event_id"], record["timestamp"],
                             record["kind"], record["name"], _json(record["data"]), prev_hash, digest))
        if chain is not None:chain[trace_id]={"trace_seq":trace_seq,"hash":digest}
        return {"seq": cursor.lastrowid, **record, "hash": digest}

    def start_span(self, name, *, kind="tool", inputs=None, attributes=None, links=None, new_trace=False, parent=None):
        if attributes is not None and not isinstance(attributes, dict):
            raise ValueError("Span attributes must be a JSON object.")
        attributes = {**(attributes or {}), "process_id": os.getpid(), "python_version": sys.version}
        selected_parent = parent
        if not new_trace and selected_parent is None:
            current = _CURRENT.get()
            if current and current[0].store_id == self.store_id:
                selected_parent = current[1]
        selected_parent = self._context(selected_parent) if selected_parent and not new_trace else None
        context = {"trace_id": selected_parent["trace_id"] if selected_parent else uuid.uuid4().hex,
                   "span_id": uuid.uuid4().hex, "store_id": self.store_id}
        parent_id = selected_parent["span_id"] if selected_parent else None
        batch=self._batch()
        if batch is not None:
            if selected_parent:self._batch_start(batch,selected_parent)
            record=self._stage(batch,context,parent_id,"span.start",name,{"kind":kind,"inputs":inputs,"attributes":attributes,
                "links":links or [],"process":{"pid":os.getpid(),"hostname":socket.gethostname(),"instance_id":_PROCESS_INSTANCE,"python_version":sys.version}})
            batch["starts"][(context["trace_id"],context["span_id"])]=record
            return Span(self,context,parent_id,str(name),str(kind),record["timestamp"])
        with self._write_db() as db:
            if not db.in_transaction:db.execute("BEGIN IMMEDIATE")
            if selected_parent and not db.execute("SELECT 1 FROM events WHERE trace_id=? AND span_id=? AND kind='span.start'", (context["trace_id"], parent_id)).fetchone():
                raise TraceError("Parent span does not exist.")
            # Pack large input separately so span metadata remains readily queryable.
            record = self._append_in_db(db, context, parent_id, "span.start", name,
                        {"kind": kind, "inputs": self._pack(db, inputs), "attributes": self._pack(db, attributes),
                         "links": links or [], "process": {"pid": os.getpid(), "hostname": socket.gethostname(),
                                                               "instance_id": _PROCESS_INSTANCE, "python_version": sys.version}})
        return Span(self, context, parent_id, str(name), str(kind), record["timestamp"])

    def trace(self, name, *, kind="task", inputs=None, attributes=None, links=None):
        return self.start_span(name, kind=kind, inputs=inputs, attributes=attributes, links=links, new_trace=True)

    def span(self, name, *, kind="tool", inputs=None, attributes=None, links=None, parent=None):
        return self.start_span(name, kind=kind, inputs=inputs, attributes=attributes, links=links, parent=parent)

    def event(self, name, data=None, *, kind="event", context=None):
        current = _CURRENT.get()
        context = context or (current[1] if current and current[0].store_id == self.store_id else None)
        if context is None:
            with self.trace(name, kind="event") as standalone:
                return self.event(name, data, kind=kind, context=standalone.context)
        context = self._context(context)
        if kind in {"span.start", "span.end"}:
            raise TraceError("Reserved event kind; use span APIs.")
        batch=self._batch()
        if batch is not None:
            started=self._batch_start(batch,context)
            return self._stage(batch,context,started["parent_span_id"],kind,name,data)
        with self._write_db() as db:
            if not db.in_transaction:db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT parent_span_id FROM events WHERE trace_id=? AND span_id=? AND kind='span.start'", (context["trace_id"], context["span_id"])).fetchone()
            if not row:
                raise TraceError("Span does not exist.")
            return self._append_in_db(db, context, row[0], kind, name, data)

    def end_span(self, context, *, status="OK", output=None, error=None, duration_ms=None):
        """First end wins. Repeated calls return the original end event, unchanged."""
        context = self._context(context)
        if status not in {"OK", "ERROR", "UNKNOWN"}:
            raise ValueError("Span status must be OK, ERROR or UNKNOWN.")
        batch_failure = (getattr(error, "staged_read_operations", None) if isinstance(error, TraceError)
                         and getattr(error, "uncommitted_read_batch", None) == context and status == "ERROR" else None)
        if isinstance(error, BaseException):
            error = {"type": type(error).__name__, "message": str(error),
                     "stack": "".join(traceback.format_exception(type(error), error, error.__traceback__))}
        batch=self._batch()
        if batch is not None:
            key=(context["trace_id"],context["span_id"])
            if key in batch["ends"]:return batch["ends"][key]
            started=self._batch_start(batch,context)
            if not started.get("pending_read_batch"):
                with self._db() as db:
                    previous=db.execute("SELECT * FROM events WHERE trace_id=? AND span_id=? AND kind='span.end' ORDER BY trace_seq LIMIT 1",key).fetchone()
                if previous:return self._row(previous)
            if duration_ms is None:duration_ms=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(started["timestamp"])).total_seconds()*1000)
            record=self._stage(batch,context,started["parent_span_id"],"span.end",started["name"],{"status":status,"output":output,"error":error,"duration_ms":round(duration_ms,3)})
            batch["ends"][key]=record
            return record
        with self._write_db() as db:
            if not db.in_transaction:
                try:
                    db.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if getattr(exc, "sqlite_errorname", None) == "SQLITE_BUSY" and status == "OK" and error is None:
                        failure = TraceError("Trace database operation failed: OperationalError [SQLITE_BUSY]")
                        failure.unwritten_span_end = dict(context)
                        raise failure from exc
                    raise
            previous = db.execute("SELECT * FROM events WHERE trace_id=? AND span_id=? AND kind='span.end' ORDER BY trace_seq LIMIT 1", (context["trace_id"], context["span_id"])).fetchone()
            if previous:
                return self._row(previous)
            started = db.execute("SELECT * FROM events WHERE trace_id=? AND span_id=? AND kind='span.start'", (context["trace_id"], context["span_id"])).fetchone()
            if not started:
                raise TraceError("Span does not exist.")
            if duration_ms is None:
                duration_ms = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(started["timestamp"])).total_seconds() * 1000)
            record = {"status": status, "output": self._pack(db, output), "error": self._pack(db, error), "duration_ms": round(duration_ms, 3)}
            if batch_failure is not None:
                record["read_batch_before_begin"] = self._pack(db, batch_failure)
            return self._append_in_db(db, context, started["parent_span_id"], "span.end", started["name"], record)

    @staticmethod
    def _row(row):
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result

    def get_events(self, trace_id, *, after=0, limit=1000, span_id=None, names=None):
        if not isinstance(trace_id, str) or not _ID.fullmatch(trace_id):
            raise KeyError("Invalid trace ID.")
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("Invalid trace pagination.")
        conditions, params = ["trace_id=?", "trace_seq>?"], [trace_id, after]
        if span_id is not None:
            if not isinstance(span_id, str) or not _ID.fullmatch(span_id):
                raise ValueError("Invalid span ID.")
            conditions.append("span_id=?")
            params.append(span_id)
        if names is not None:
            if not names:
                return []
            conditions.append("name IN (" + ",".join("?" for _ in names) + ")")
            params.extend(names)
        where = " AND ".join(conditions)
        if span_id is None:
            query = "SELECT * FROM events WHERE " + where + " ORDER BY trace_seq LIMIT ?"
        else:
            # A receipt may be two rows inside a very long trace. Page only
            # keys through the span index before loading any event payloads.
            query = ("SELECT event.* FROM (SELECT seq,trace_seq FROM events INDEXED BY span_events WHERE "
                     + where + " ORDER BY trace_seq LIMIT ?) page JOIN events event ON event.seq=page.seq "
                     "ORDER BY page.trace_seq")
        with self._db() as db:
            rows = db.execute(query, (*params, limit)).fetchall()
        return [self._row(row) for row in rows]

    def _problem_spans(self, trace_id):
        """Index required validation findings independently of the current event page."""
        with self._db() as db:
            rows = db.execute(
                """SELECT span_id, data FROM events WHERE trace_id=? AND kind='validation'
                   AND (json_extract(data,'$.status') IN ('FAIL','ERROR','UNKNOWN') OR json_type(data,'$."$artifact"') IS NOT NULL)
                   ORDER BY trace_seq""", (trace_id,)).fetchall()
        problems = {}
        for row in rows:
            data = self.resolve(json.loads(row["data"]))
            if not isinstance(data, dict) or data.get("required") is False or data.get("status") not in ("FAIL", "ERROR", "UNKNOWN"):
                continue
            issue = problems.setdefault(row["span_id"], {"span_id": row["span_id"], "status": "UNKNOWN", "count": 0})
            issue["count"] += 1
            if data["status"] in ("FAIL", "ERROR"):
                issue["status"] = "FAIL"
        return list(problems.values())

    def get_artifact(self, sha256):
        if not isinstance(sha256, str) or not _HASH.fullmatch(sha256):
            raise KeyError("Invalid artifact ID.")
        with self._db() as db:
            row = db.execute("SELECT * FROM artifacts WHERE sha256=?", (sha256,)).fetchone()
        if not row:
            raise KeyError("Artifact not found.")
        if _sha(row["content"]) != sha256 or len(row["content"].encode("utf-8")) != row["bytes"]:
            raise TraceError("Trace artifact integrity check failed.")
        return {"sha256": sha256, "bytes": row["bytes"], "media_type": "application/json", "data": json.loads(row["content"])}

    def resolve(self, value):
        if isinstance(value, dict) and set(value) == {"$literal", "$literal_reason"} and value["$literal_reason"] == "user_artifact_shape":
            return value["$literal"]
        if isinstance(value, dict) and set(value) == {"$artifact", "bytes", "media_type"}:
            return self.get_artifact(value["$artifact"])["data"]
        return value

    def get_trace(self, trace_id, *, limit=1000, after=0):
        if not isinstance(trace_id, str) or not _ID.fullmatch(trace_id):
            raise KeyError("Invalid trace ID.")
        with self._db() as db:
            rows = db.execute("SELECT * FROM events WHERE trace_id=? AND kind IN ('span.start','span.end') ORDER BY trace_seq", (trace_id,)).fetchall()
            count = db.execute("SELECT COUNT(*) FROM events WHERE trace_id=?", (trace_id,)).fetchone()[0]
        if not rows:
            raise KeyError("Trace not found.")
        spans = {}
        for row in rows:
            event = self._row(row)
            data = self.resolve(event["data"])
            sid = event["span_id"]
            if event["kind"] == "span.start":
                spans[sid] = {"span_id": sid, "parent_span_id": event["parent_span_id"], "name": event["name"],
                              "kind": data["kind"], "status": "UNKNOWN", "start_time": event["timestamp"], "end_time": None,
                              "duration_ms": None, "inputs": data.get("inputs"), "output": None,
                              "attributes": data.get("attributes", {}), "links": data.get("links", []), "process": data.get("process"),
                              "error": None, "end_recorded": False, "events": []}
            elif sid in spans:
                spans[sid].update(status=data["status"], end_time=event["timestamp"], duration_ms=data["duration_ms"],
                                  output=data.get("output"), error=data.get("error"), end_recorded=True)
        events = self.get_events(trace_id, after=after, limit=limit)
        for event in events:
            if event["span_id"] in spans:
                spans[event["span_id"]]["events"].append(event)
        values = list(spans.values())
        root = next((s for s in values if s["parent_span_id"] is None), values[0])
        status = "ERROR" if any(s["status"] == "ERROR" for s in values) else "UNKNOWN" if any(s["status"] == "UNKNOWN" for s in values) else "OK"
        return {"schema_version": SCHEMA_VERSION, "store_id": self.store_id, "trace_id": trace_id,
                "name": root["name"], "kind": root["kind"], "status": status, "start_time": root["start_time"],
                "end_time": root["end_time"], "duration_ms": root["duration_ms"], "attributes": root["attributes"], "links": root["links"],
                "span_count": len(values), "event_count": count, "open_span_count": sum(not s["end_recorded"] for s in values),
                "spans": values, "problem_spans": self._problem_spans(trace_id),
                "events": events, "events_has_more": bool(events and events[-1]["trace_seq"] < count),
                "next_after": events[-1]["trace_seq"] if events else after,
                "status_note": "Open spans are UNKNOWN; absence of an end event does not prove a process is running or dead."}

    def _trace_summary(self, root):
        """Read list metadata without materializing child payloads or event pages."""
        data = self.resolve(json.loads(root["data"]))
        with self._db() as db:
            counts = db.execute("""
                SELECT COUNT(*) AS span_count,
                       SUM(CASE WHEN ended.seq IS NULL THEN 1 ELSE 0 END) AS open_span_count,
                       SUM(CASE WHEN ended.seq IS NULL OR json_extract(ended.data,'$.status')='UNKNOWN'
                                THEN 1 ELSE 0 END) AS unknown_count,
                       SUM(CASE WHEN json_extract(ended.data,'$.status')='ERROR' THEN 1 ELSE 0 END) AS error_count
                FROM events AS started LEFT JOIN events AS ended
                  ON ended.trace_id=started.trace_id AND ended.span_id=started.span_id AND ended.kind='span.end'
                WHERE started.trace_id=? AND started.kind='span.start'
            """, (root["trace_id"],)).fetchone()
            # A small inline threshold can pack the end envelope itself. Resolve
            # only these status envelopes (with normal integrity checks), never
            # the model/tool output artifact nested inside them.
            endings = db.execute("""
                SELECT span_id,timestamp,data FROM events
                WHERE trace_id=? AND kind='span.end'
                  AND (span_id=? OR json_type(data,'$."$artifact"') IS NOT NULL)
                ORDER BY trace_seq
            """, (root["trace_id"], root["span_id"])).fetchall()
        error_count, unknown_count = counts["error_count"], counts["unknown_count"]
        end_time, duration_ms = None, None
        for ending in endings:
            end = self.resolve(json.loads(ending["data"]))
            error_count += end.get("status") == "ERROR"
            unknown_count += end.get("status") == "UNKNOWN"
            if ending["span_id"] == root["span_id"]:
                end_time, duration_ms = ending["timestamp"], end.get("duration_ms")
        return {"schema_version": SCHEMA_VERSION, "store_id": self.store_id, "trace_id": root["trace_id"],
                "name": root["name"], "kind": data["kind"],
                "status": "ERROR" if error_count else "UNKNOWN" if unknown_count else "OK",
                "start_time": root["timestamp"], "end_time": end_time, "duration_ms": duration_ms,
                "attributes": data.get("attributes", {}), "links": data.get("links", []),
                "span_count": counts["span_count"], "event_count": root["event_count"],
                "open_span_count": counts["open_span_count"],
                "status_note": "Open spans are UNKNOWN; absence of an end event does not prove a process is running or dead."}

    def list_traces(self, *, limit=50, offset=0, kind=None, status=None, search=None):
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid trace pagination.")
        with self._db() as db:
            # Filter root metadata before aggregating summaries. A large root
            # held in an artifact remains a candidate for Python fallback.
            conditions = ["root.kind='span.start'", "root.parent_span_id IS NULL"]
            parameters = []
            if kind:
                conditions.append("(json_extract(root.data,'$.kind')=? OR json_type(root.data,'$.\"$artifact\"') IS NOT NULL)")
                parameters.append(kind)
            if search:
                needle = str(search).casefold()
                conditions.append("(instr(trace_casefold(root.name),?)>0 OR instr(trace_casefold(root.trace_id),?)>0 OR instr(trace_casefold(json_extract(root.data,'$.attributes')),?)>0 OR json_type(root.data,'$.\"$artifact\"') IS NOT NULL OR json_type(root.data,'$.attributes.\"$artifact\"') IS NOT NULL)")
                parameters.extend([needle, needle, needle])
            query = ("SELECT root.trace_id,root.span_id,root.name,root.timestamp,root.data,latest.last_seq,latest.event_count FROM events AS root "
                     "JOIN (SELECT trace_id,MAX(seq) AS last_seq,COUNT(*) AS event_count FROM events GROUP BY trace_id) AS latest ON root.trace_id=latest.trace_id "
                     "WHERE " + " AND ".join(conditions) + " ORDER BY latest.last_seq DESC")
            ids = db.execute(query, parameters).fetchall()
        found = []
        for row in ids:
            summary = self._trace_summary(row)
            if kind and summary["kind"] != kind or status and summary["status"] != status:
                continue
            if search:
                searchable = {"name": summary["name"], "trace_id": summary["trace_id"], "attributes": self.resolve(summary["attributes"])}
                if str(search).casefold() not in _json(searchable).casefold():
                    continue
            if offset:
                offset -= 1
                continue
            found.append(summary)
            if len(found) == limit:
                break
        return found

    def _all_events(self, trace_id):
        if not isinstance(trace_id, str) or not _ID.fullmatch(trace_id):
            raise KeyError("Invalid trace ID.")
        with self._db() as db:
            rows = db.execute("SELECT * FROM events WHERE trace_id=? ORDER BY trace_seq", (trace_id,)).fetchall()
        if not rows:
            raise KeyError("Trace not found.")
        return [self._row(row) for row in rows]

    def _artifact_ids(self, value):
        if isinstance(value, dict):
            if set(value) == {"$literal", "$literal_reason"} and value["$literal_reason"] == "user_artifact_shape":
                return
            if set(value) == {"$artifact", "bytes", "media_type"} and isinstance(value["$artifact"], str):
                yield value["$artifact"]
            else:
                for child in value.values():
                    yield from self._artifact_ids(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._artifact_ids(child)

    def verify(self, trace_id):
        try:
            events = self._all_events(trace_id)
        except (ValueError, TypeError):
            return {"ok": False, "trace_id": trace_id, "checked_events": 0, "checked_artifacts": 0,
                    "issues": [{"issue": "event_json_corrupt"}], "limitation": "Unreadable event data; integrity could not be established."}
        issues, checked, artifacts = [], set(), {}
        previous = "0" * 64
        for index, event in enumerate(events, 1):
            if event["trace_seq"] != index:
                issues.append({"event_id": event["event_id"], "issue": "sequence_gap"})
            if event["prev_hash"] != previous:
                issues.append({"event_id": event["event_id"], "issue": "previous_hash_mismatch"})
            signed = {k: v for k, v in event.items() if k not in {"seq", "hash"}}
            if _sha(_json(signed)) != event["hash"]:
                issues.append({"event_id": event["event_id"], "issue": "event_hash_mismatch"})
            previous = event["hash"]
            pending = list(self._artifact_ids(event["data"]))
            while pending:
                sha = pending.pop()
                if sha in checked:
                    continue
                checked.add(sha)
                try:
                    artifact = self.get_artifact(sha)
                    artifacts[sha] = artifact
                    pending.extend(self._artifact_ids(artifact["data"]))
                except (KeyError, TraceError, ValueError):
                    issues.append({"artifact": sha, "issue": "artifact_missing_or_corrupt"})
        return {"ok": not issues, "trace_id": trace_id, "checked_events": len(events), "checked_artifacts": len(checked),
                "issues": issues, "limitation": "Local consistency verification only; not a signed proof of provider-side traffic or protection against privileged rewriting."}

    def export_trace(self, trace_id):
        trace_data = self.get_trace(trace_id)
        events = self._all_events(trace_id)
        artifacts, pending = {}, [sha for event in events for sha in self._artifact_ids(event["data"])]
        while pending:
            sha = pending.pop()
            if sha in artifacts:
                continue
            artifact = self.get_artifact(sha)
            artifacts[sha] = artifact
            pending.extend(self._artifact_ids(artifact["data"]))
        trace_data["events"] = events
        for span_record in trace_data["spans"]:
            span_record["events"] = [e for e in events if e["span_id"] == span_record["span_id"]]
        trace_data.update(events_has_more=False, next_after=events[-1]["trace_seq"])
        return {"schema_version": SCHEMA_VERSION, "exported_at": _now(), "trace": trace_data,
                "artifacts": artifacts, "integrity": self.verify(trace_id)}


class Span:
    def __init__(self, tracer, context, parent_span_id, name, kind, started_at):
        self.tracer = tracer
        self.context = context
        self.trace_id = context["trace_id"]
        self.span_id = context["span_id"]
        self.parent_span_id = parent_span_id
        self.name, self.kind, self.started_at = name, kind, started_at
        self._monotonic = time.monotonic()
        self._output, self._status = None, "OK"
        self._token = None

    def __enter__(self):
        if self._token is not None:
            raise TraceError("A span context cannot be entered twice concurrently.")
        self._token = _CURRENT.set((self.tracer, self.context))
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            successful_exit = isinstance(exc, SystemExit) and exc.code in (None, 0)
            if isinstance(exc, SystemExit):
                self._output = {"exit_code": 0 if exc.code is None else exc.code}
            self.end(status=self._status if exc is None or successful_exit else "ERROR",
                     error=None if successful_exit else exc)
        finally:
            _CURRENT.reset(self._token)
            self._token = None
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.__exit__(exc_type, exc, tb)

    def event(self, name, data=None, *, kind="event"):
        return self.tracer.event(name, data, kind=kind, context=self.context)

    def set_output(self, value):
        self._output = value

    def set_status(self, status, message=None):
        if status not in {"OK", "ERROR", "UNKNOWN"}:
            raise ValueError("Invalid span status.")
        self._status = status
        if message is not None:
            self.event("status.annotation", {"status": status, "message": message})

    def end(self, *, status=None, output=_UNSET, error=None):
        return self.tracer.end_span(self.context, status=status or self._status,
                                   output=self._output if output is _UNSET else output, error=error,
                                   duration_ms=(time.monotonic() - self._monotonic) * 1000)


def get_tracer(directory=None):
    current = _CURRENT.get()
    if directory is None and current:
        return current[0]
    path = Path(directory or os.environ.get("AGENT_TRACE_DIR") or ROOT / "outputs" / "traces").expanduser().resolve()
    with _LOCK:
        key = str(path)
        if key not in _TRACERS:
            _TRACERS[key] = Tracer(path)
        return _TRACERS[key]


def current_context():
    current = _CURRENT.get()
    return dict(current[1]) if current and current[1] else None


@contextmanager
def use_context(context, tracer=None):
    if context is None:
        token = _CURRENT.set((tracer, None) if tracer else None)
    else:
        tracer = tracer or get_tracer()
        token = _CURRENT.set((tracer, tracer._context(context)))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def trace(name, **kwargs):
    return get_tracer().trace(name, **kwargs)


def span(name, **kwargs):
    return get_tracer().span(name, **kwargs)


def event(name, data=None, **kwargs):
    return get_tracer().event(name, data, **kwargs)


def traced(name=None, *, kind="tool", capture_inputs=True, capture_output=True):
    def decorate(function):
        label = name or function.__module__ + "." + function.__qualname__
        def source_attributes():
            try:
                source = inspect.getsourcefile(function)
            except TypeError:
                source = None
            info = {"module": function.__module__, "qualname": function.__qualname__, "source_file": source,
                    "python_version": sys.version, "process_id": os.getpid()}
            if hasattr(function, "__code__"):
                info["loaded_code_sha256"] = hashlib.sha256(marshal.dumps(function.__code__)).hexdigest()
            if source:
                try:
                    path = Path(source).resolve()
                    stat = path.stat()
                    key = (str(path), stat.st_size, stat.st_mtime_ns)
                    with _LOCK:
                        if key not in _SOURCE_HASHES:
                            _SOURCE_HASHES[key] = hashlib.sha256(path.read_bytes()).hexdigest()
                        info["source_sha256"] = _SOURCE_HASHES[key]
                    info["source_hash_basis"] = "source_file_on_disk_at_call; not proof of loaded bytecode"
                except OSError:
                    info["source_sha256"] = None
            return info
        def inputs(args, kwargs):
            if not capture_inputs:
                return None
            try:
                values = dict(inspect.signature(function).bind(*args, **kwargs).arguments)
            except (TypeError, ValueError):
                values = {"args": args, "kwargs": kwargs}
            if kind == "cli":
                argv, hide_next = [], False
                for item in sys.argv:
                    if hide_next:
                        argv.append("[REDACTED]")
                        hide_next = False
                    elif item.startswith("-") and _secret_key(item.split("=", 1)[0]):
                        if "=" in item:
                            argv.append(item.split("=", 1)[0] + "=[REDACTED]")
                        else:
                            argv.append(item)
                            hide_next = True
                    else:
                        argv.append(item)
                values["sys.argv"] = argv
            return values
        def output(active, result):
            if kind == "cli" and type(result) is int:
                if result != 0:
                    active.set_status("ERROR", "CLI returned a nonzero exit code")
                if capture_output:
                    active.set_output({"exit_code": result, "return_value": result})
            elif capture_output:
                active.set_output(result)
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def async_wrapper(*args, **kwargs):
                with span(label, kind=kind, inputs=inputs(args, kwargs), attributes=source_attributes()) as active:
                    result = await function(*args, **kwargs)
                    output(active, result)
                    return result
            async_wrapper.__trace_instrumented__ = True
            return async_wrapper
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with span(label, kind=kind, inputs=inputs(args, kwargs), attributes=source_attributes()) as active:
                result = function(*args, **kwargs)
                output(active, result)
                return result
        wrapper.__trace_instrumented__ = True
        return wrapper
    return decorate
