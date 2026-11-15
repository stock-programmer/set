"""Versioned business assets and durable collaboration messages for local agents.

The business store owns delivery and asset state. Execution evidence belongs to
the shared Trace SDK. Publishing a script only publishes a fact. Starting a
production task requires a separate, explicitly addressed request.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from trace_system import current_context, event, get_tracer, span, traced


_THREAD_TRANSACTIONS = threading.local()


def _active_transactions():
    if not hasattr(_THREAD_TRANSACTIONS, "stores"):
        _THREAD_TRANSACTIONS.stores = {}
    return _THREAD_TRANSACTIONS.stores


def _mutation(function):
    """Poison the outer write transaction even when API validation or its Trace wrapper fails."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        active = _active_transactions().get(self.path)
        if active and active["rollback_only"]:
            raise CollaborationConflict("This package already has a failed write")
        try:
            return function(self, *args, **kwargs)
        except BaseException:
            if active:
                active["rollback_only"] = True
            raise
    return wrapped


class CollaborationConflict(ValueError):
    """Stale parent revision, reused message ID, or invalid state transition."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _name(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{label} must be a non-empty identifier (max 256 characters)")
    if get_tracer().sanitize(value) != value:
        raise ValueError(f"{label} is unsuitable for a public identifier")
    return value


def _revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"r[1-9][0-9]*", value):
        raise ValueError("revision_id must be r1, r2, ...")
    return value


def _context():
    context = current_context()
    if not context:
        raise RuntimeError("Collaboration writes require a Trace context")
    return context


def _public_json(value):
    # Validate the JSON round trip and protect the business store as well as Trace.
    original = _json(value)
    if _json(get_tracer().sanitize(value)) != original:
        raise ValueError("Public collaboration data must contain safe asset references, not secrets or inline media")
    return original


_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT NOT NULL, seq INTEGER NOT NULL, revision_id TEXT NOT NULL,
    kind TEXT NOT NULL, parent_revision TEXT, producer TEXT NOT NULL, story_id TEXT NOT NULL,
    content TEXT NOT NULL, sha256 TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE,
    trace_context TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (asset_id,revision_id), UNIQUE (asset_id,seq),
    FOREIGN KEY (asset_id,parent_revision) REFERENCES assets(asset_id,revision_id)
);
CREATE TABLE IF NOT EXISTS dependencies (
    asset_id TEXT NOT NULL, revision_id TEXT NOT NULL, dep_asset_id TEXT NOT NULL,
    dep_revision_id TEXT NOT NULL, role TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(asset_id,revision_id,dep_asset_id,dep_revision_id,role,source_id),
    FOREIGN KEY(asset_id,revision_id) REFERENCES assets(asset_id,revision_id),
    FOREIGN KEY(dep_asset_id,dep_revision_id) REFERENCES assets(asset_id,revision_id)
);
CREATE INDEX IF NOT EXISTS dependencies_reverse ON dependencies(dep_asset_id,dep_revision_id);
CREATE TABLE IF NOT EXISTS subscriptions (
    agent TEXT NOT NULL, topic TEXT NOT NULL, story_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(agent,topic,story_id)
);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE, message_type TEXT NOT NULL,
    category TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT, story_id TEXT,
    correlation_id TEXT NOT NULL, causation_id TEXT, payload TEXT NOT NULL,
    trace_context TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(causation_id) REFERENCES messages(message_id)
);
CREATE INDEX IF NOT EXISTS message_correlation ON messages(correlation_id,seq);
CREATE TABLE IF NOT EXISTS deliveries (
    message_id TEXT NOT NULL REFERENCES messages(message_id), agent TEXT NOT NULL,
    status TEXT NOT NULL, PRIMARY KEY(message_id,agent)
);
CREATE INDEX IF NOT EXISTS pending_delivery ON deliveries(agent,status);
CREATE TABLE IF NOT EXISTS receipts (
    seq INTEGER PRIMARY KEY, message_id TEXT NOT NULL, agent TEXT NOT NULL,
    status TEXT NOT NULL, result TEXT NOT NULL, trace_context TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(message_id,agent,status),
    FOREIGN KEY(message_id,agent) REFERENCES deliveries(message_id,agent)
);
"""


class CollabStore:
    def __init__(self, db_path):
        self.path = Path(db_path).expanduser().resolve()
        self._initialize()

    @contextmanager
    def _db(self, write=False):
        active = _active_transactions().get(self.path)
        if active is not None:
            try:
                yield active["connection"]
            except BaseException:
                if write:
                    active["rollback_only"] = True
                raise
            return
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        finally:
            # Closing an uncommitted connection rolls back every pending write.
            db.close()

    @traced("collab.store.initialize", kind="tool")
    def _initialize(self):
        if self.path in _active_transactions():
            # Another handle to the same database has already initialized its schema.
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db(write=True) as db:
            # executescript closes any preceding transaction before running its statements.
            db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)

    @contextmanager
    def transaction(self):
        """Atomically publish a business package. Nested scopes join the outermost transaction.

        Scope is the current thread and resolved database path. A caught failed write
        still aborts the package. Files and provider operations are outside SQLite.
        """
        registry = _active_transactions()
        joined = registry.get(self.path)
        tx_id = joined["id"] if joined else uuid.uuid4().hex
        inputs = {"transaction_id": tx_id, "store": str(self.path), "joins_outer_transaction": bool(joined)}
        with span("collab.store.transaction", kind="tool", inputs=inputs) as record:
            if joined:
                try:
                    yield self
                except BaseException:
                    joined["rollback_only"] = True
                    raise
                record.set_output({"transaction_id": tx_id, "outcome": "staged"})
                return
            changes = []
            commit_started = False
            try:
                with self._db(write=True) as connection:
                    active = {"id": tx_id, "connection": connection, "changes": changes, "rollback_only": False}
                    registry[self.path] = active
                    try:
                        yield self
                        if active["rollback_only"]:
                            raise CollaborationConflict("A failed write requires rollback of the entire package")
                        event("collab.transaction.commit_requested", {"transaction_id": tx_id, "changes": changes})
                    finally:
                        del registry[self.path]
                    commit_started = True
                # The SQLite commit returns before any trace describes the package as committed.
            except BaseException:
                outcome = "commit_unknown" if commit_started else "rolled_back"
                record.set_output({"transaction_id": tx_id, "outcome": outcome, "changes": changes})
                event("collab.transaction." + outcome, {"transaction_id": tx_id, "changes": changes})
                raise
            completed = {"transaction_id": tx_id, "outcome": "committed", "changes": changes}
            record.set_output(completed)
            event("collab.transaction.committed", completed)

    def _stage(self, kind, fields):
        active = _active_transactions().get(self.path)
        if active is None:
            return False
        change = {"kind": kind, **fields}
        active["changes"].append(change)
        event("collab.transaction.staged", {"transaction_id": active["id"], **change})
        return True

    @staticmethod
    def _message(db, row):
        value = dict(row)
        value.pop("seq", None)
        value["payload"] = json.loads(value["payload"])
        context = json.loads(value["trace_context"])
        value.update(trace_context=context, trace_id=context["trace_id"])
        recipients = db.execute("SELECT agent,status FROM deliveries WHERE message_id=? ORDER BY agent",
                                (value["message_id"],))
        value["deliveries"] = {r["agent"]: r["status"] for r in recipients}
        return value

    @staticmethod
    def _asset(db, row):
        value = dict(row)
        value.pop("seq", None)
        if hashlib.sha256(value["content"].encode("utf-8")).hexdigest() != value["sha256"]:
            raise CollaborationConflict("Stored asset content does not match its revision hash")
        value["content"] = json.loads(value["content"])
        context = json.loads(value["trace_context"])
        value.update(trace_context=context, trace_id=context["trace_id"])
        rows = db.execute("""SELECT dep_asset_id AS asset_id, dep_revision_id AS revision_id, role, source_id
                             FROM dependencies WHERE asset_id=? AND revision_id=?
                             ORDER BY dep_asset_id,dep_revision_id,role,source_id""",
                          (value["asset_id"], value["revision_id"]))
        value["dependencies"] = [{k: v for k, v in dict(dep).items() if k != "source_id" or v} for dep in rows]
        return value

    def _insert_message(self, db, message_type, *, sender, recipient, payload, story_id,
                        correlation_id, causation_id, message_id, category):
        message_type = _name(message_type, "message_type")
        sender = _name(sender, "sender")
        message_id = uuid.uuid4().hex if message_id is None else _name(message_id, "message_id")
        if recipient is not None:
            _name(recipient, "recipient")
        if story_id is not None:
            _name(story_id, "story_id")
        if causation_id is not None:
            parent = db.execute("SELECT correlation_id FROM messages WHERE message_id=?",
                                (_name(causation_id, "causation_id"),)).fetchone()
            if parent is None:
                raise ValueError("Unknown causal message: " + causation_id)
            if correlation_id is None:
                correlation_id = parent["correlation_id"]
            elif correlation_id != parent["correlation_id"]:
                raise CollaborationConflict("A causal message belongs to another collaboration")
        correlation_id = _name(correlation_id or message_id, "correlation_id")
        if category not in {"event", "request", "response", "result", "receipt"}:
            raise ValueError("Unknown message category")
        if category == "request" and recipient is None:
            raise ValueError("A work request must name a recipient")
        if message_type == "production.requested" and (category != "request" or recipient is None):
            raise ValueError("Production requires an explicitly addressed work request")
        if not isinstance(payload, dict):
            raise ValueError("Message payload must be a JSON object")
        payload_json = _public_json(payload)
        fields = dict(message_type=message_type, sender=sender, recipient=recipient, payload=payload_json,
                      story_id=story_id, correlation_id=correlation_id, causation_id=causation_id, category=category)
        previous = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if previous:
            if any(previous[k] != v for k, v in fields.items()):
                raise CollaborationConflict("Message ID was already used for a different envelope")
            return self._message(db, previous)
        db.execute("""INSERT INTO messages(message_id,message_type,category,sender,recipient,story_id,
                      correlation_id,causation_id,payload,trace_context,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (message_id, message_type, category, sender, recipient, story_id, correlation_id, causation_id,
                    payload_json, _json(_context()), _now()))
        if recipient is None:
            db.execute("""INSERT INTO deliveries(message_id,agent,status)
                          SELECT DISTINCT ?,agent,'pending' FROM subscriptions
                          WHERE topic=? AND (story_id='' OR story_id=?)""", (message_id, message_type, story_id))
        else:
            db.execute("INSERT INTO deliveries VALUES(?,?,'pending')", (message_id, recipient))
        self._stage("message", {"message_id": message_id, "message_type": message_type, "recipient": recipient})
        return self._message(db, db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone())

    @_mutation
    @traced("collab.assets.publish", kind="tool")
    def publish_asset(self, asset_id, content, *, kind, producer, parent_revision=None, dependencies=(),
                      topic=None, correlation_id=None, causation_id=None, story_id=None, event_payload=None):
        """CAS publication. None expects a new asset; asset, links and event commit together."""
        for label, value in (("asset_id", asset_id), ("kind", kind), ("producer", producer)):
            _name(value, label)
        if parent_revision is not None:
            _revision(parent_revision)
        if not isinstance(dependencies, (list, tuple)):
            raise ValueError("dependencies must be a list of typed revision references")
        content_json = _public_json(content)
        sha = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        with self._db(write=True) as db:
            previous = db.execute("SELECT * FROM assets WHERE asset_id=? ORDER BY seq DESC LIMIT 1",
                                  (asset_id,)).fetchone()
            actual = previous["revision_id"] if previous else None
            check = {"path": "asset.parent_revision", "expected": parent_revision, "actual": actual,
                     "status": "PASS" if parent_revision == actual else "FAIL"}
            event("collab.validation", check, kind="validation")
            if actual != parent_revision:
                raise CollaborationConflict(f"Parent conflict for {asset_id}: expected {parent_revision}, current {actual}")
            story_id = _name(story_id or (previous["story_id"] if previous else asset_id), "story_id")
            if previous and (previous["kind"], previous["producer"], previous["story_id"]) != (kind, producer, story_id):
                raise CollaborationConflict("An asset revision must keep its kind, owner and story")
            seq = previous["seq"] + 1 if previous else 1
            revision_id, event_id = f"r{seq}", uuid.uuid4().hex
            db.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (asset_id, seq, revision_id, kind, parent_revision, producer, story_id, content_json, sha,
                        event_id, _json(_context()), _now()))
            for dep in dependencies:
                if not isinstance(dep, dict) or set(dep) - {"asset_id", "revision_id", "role", "source_id"}:
                    raise ValueError("Invalid dependency fields")
                dep_id = _name(dep.get("asset_id"), "dependency asset_id")
                dep_rev = _revision(dep.get("revision_id"))
                role = _name(dep.get("role"), "dependency role")
                source_id = _name(dep["source_id"], "source_id") if "source_id" in dep else ""
                if (dep_id, dep_rev) == (asset_id, revision_id):
                    raise ValueError("A revision cannot depend on itself")
                if not db.execute("SELECT 1 FROM assets WHERE asset_id=? AND revision_id=?", (dep_id, dep_rev)).fetchone():
                    raise ValueError(f"Missing dependency {dep_id}@{dep_rev}")
                db.execute("INSERT INTO dependencies VALUES(?,?,?,?,?,?)",
                           (asset_id, revision_id, dep_id, dep_rev, role, source_id))
            payload = dict(event_payload or {})
            protected = {"asset_id": asset_id, "revision_id": revision_id, "parent_revision": parent_revision,
                         "sha256": sha, "kind": kind}
            if protected.keys() & payload.keys():
                raise ValueError("Custom event fields may not replace the revision identity")
            payload.update(protected)
            publication = self._insert_message(db, topic or "asset.revision_published", sender=producer, recipient=None,
                payload=payload, story_id=story_id, correlation_id=correlation_id, causation_id=causation_id,
                message_id=event_id, category="event")
            if not self._stage("asset_revision", {"asset_id": asset_id, "revision_id": revision_id,
                                                  "sha256": sha, "event_id": event_id}):
                event("collab.message.published", publication, kind="event")
            return self._asset(db, db.execute("SELECT * FROM assets WHERE asset_id=? AND revision_id=?",
                                              (asset_id, revision_id)).fetchone())

    @traced("collab.assets.get", kind="tool")
    def get_asset(self, asset_id, revision_id=None):
        with self._db() as db:
            if revision_id is None:
                row = db.execute("SELECT * FROM assets WHERE asset_id=? ORDER BY seq DESC LIMIT 1", (asset_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM assets WHERE asset_id=? AND revision_id=?",
                                 (asset_id, _revision(revision_id))).fetchone()
            if row is None:
                raise KeyError(f"Unknown asset revision: {asset_id}@{revision_id or 'latest'}")
            return self._asset(db, row)

    @traced("collab.assets.revisions", kind="tool")
    def revisions(self, asset_id):
        with self._db() as db:
            return [self._asset(db, row) for row in db.execute("SELECT * FROM assets WHERE asset_id=? ORDER BY seq",
                                                               (asset_id,))]

    @traced("collab.assets.dependents", kind="tool")
    def dependents(self, asset_id, revision_id):
        """Return edges rather than guessing whether a reference affects a shot."""
        with self._db() as db:
            return [dict(row) for row in db.execute("""SELECT asset_id,revision_id,role,source_id FROM dependencies
                       WHERE dep_asset_id=? AND dep_revision_id=? ORDER BY asset_id,revision_id,role,source_id""",
                       (asset_id, _revision(revision_id)))]

    @_mutation
    @traced("collab.bus.subscribe", kind="tool")
    def subscribe(self, agent, topic, story_id=None):
        """Subscribe to future publications. Each agent is delivered a message at most once."""
        _name(agent, "agent")
        _name(topic, "topic")
        if story_id is not None:
            _name(story_id, "story_id")
        with self._db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO subscriptions VALUES(?,?,?)", (agent, topic, story_id or ""))
        return {"agent": agent, "topic": topic, "story_id": story_id}

    @_mutation
    @traced("collab.bus.send", kind="tool")
    def send(self, message_type, *, sender, recipient=None, payload=None, correlation_id=None, causation_id=None,
             message_id=None, story_id=None, category=None):
        if message_type in {"script.revision_published", "asset.revision_published"}:
            raise ValueError("Revision publication events must be written together with their asset")
        if message_type in {"script.revision_response", "script.revision_work_requested"}:
            raise ValueError("Revision decisions and follow-up work must be committed together")
        with self._db(write=True) as db:
            return self._insert_message(db, message_type, sender=sender, recipient=recipient,
                payload={} if payload is None else payload, story_id=story_id, correlation_id=correlation_id,
                causation_id=causation_id, message_id=message_id,
                category=category or ("request" if recipient else "event"))

    @_mutation
    @traced("collab.bus.reply", kind="tool")
    def respond_to_revision(self, request_id, decision, explanation, instruction, *, recovery=False):
        """A response and its optional follow-up job are one commit, even across process restarts."""
        if decision not in {"retain", "alternative", "accept"} or not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("Provide a supported decision and a reason")
        if not isinstance(instruction, str) or (decision != "retain" and not instruction.strip()):
            raise ValueError("An alternative or accepted revision needs a concrete instruction")
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM messages WHERE message_id=?", (request_id,)).fetchone()
            if not row or row["message_type"] != "script.revision_requested" or row["recipient"] != "script":
                raise ValueError("A script revision request is required")
            original = self._message(db, row)
            if original["deliveries"].get("script") not in ({"accepted","completed","blocked","unknown"} if recovery else {"accepted","completed"}):
                raise CollaborationConflict("The request has not been accepted by the script agent")
            basis = original["payload"]
            # IDs remain unique across retries and fit the public envelope limit.
            prefix = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
            response_id, work_id = f"reply-{prefix}", f"revise-{prefix}"
            existing = db.execute("SELECT * FROM messages WHERE message_id=?", (response_id,)).fetchone()
            response = dict(decision=decision, explanation=explanation, instruction=instruction,
                script_ref=basis["script_ref"], beat_id=basis["beat_id"],
                publication_status="pending_publication" if decision == "accept" else "no_publication",
                revision_work_id=work_id if decision == "accept" else None)
            if existing:
                if json.loads(existing["payload"]) != response:
                    raise CollaborationConflict("This request already has a different decision")
                return self._message(db, existing)
            metadata = dict(sender="script", story_id=row["story_id"], correlation_id=row["correlation_id"])
            reply = self._insert_message(db, "script.revision_response", recipient=row["sender"],
                message_id=response_id, category="response", payload=response, causation_id=request_id, **metadata)
            if decision == "accept":
                self._insert_message(db, "script.revision_work_requested", recipient="script", category="request",
                    payload={"script_ref": basis["script_ref"], "beat_id": basis["beat_id"],
                             "instruction": instruction, "source_request_id": request_id},
                    message_id=work_id, causation_id=response_id, **metadata)
            return reply

    @traced("collab.bus.get_message", kind="tool")
    def get_message(self, message_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown collaboration message: " + str(message_id))
            return self._message(db, row)

    def _receipt(self, db, agent, message_id, status, result):
        db.execute("UPDATE deliveries SET status=? WHERE message_id=? AND agent=?", (status, message_id, agent))
        db.execute("""INSERT INTO receipts(message_id,agent,status,result,trace_context,created_at)
                      VALUES(?,?,?,?,?,?)""", (message_id, agent, status, _public_json(result), _json(_context()), _now()))
        receipt = {"message_id": message_id, "agent": agent, "status": status,
                   "result": result, "trace_context": _context()}
        if not self._stage("receipt", receipt):
            event("collab.delivery.receipt", receipt, kind="event")

    @_mutation
    @traced("collab.bus.claim", kind="tool")
    def claim(self, agent, *, message_id=None):
        """Claim pending work once. An interrupted received/accepted task is never reclaimed automatically."""
        with self._db(write=True) as db:
            query = """SELECT m.* FROM messages m JOIN deliveries d USING(message_id)
                       WHERE d.agent=? AND d.status='pending'"""
            args = [agent]
            if message_id is not None:
                query += " AND d.message_id=?"
                args.append(message_id)
            row = db.execute(query + " ORDER BY m.seq LIMIT 1", args).fetchone()
            if row is None:
                return None
            self._receipt(db, agent, row["message_id"], "received", {})
            return self._message(db, row)

    @_mutation
    @traced("collab.bus.ack", kind="tool")
    def ack(self, agent, message_id, status, *, result=None, recovery=False):
        transitions = {"received": {"accepted", "completed", "failed", "blocked", "unknown"},
                       "accepted": {"completed", "failed", "blocked", "unknown"}}
        if recovery:
            transitions["unknown"] = {"completed","blocked"}
            transitions["blocked"] = {"completed"}
        result = {} if result is None else result
        with self._db(write=True) as db:
            row = db.execute("SELECT status FROM deliveries WHERE message_id=? AND agent=?", (message_id, agent)).fetchone()
            if not row:
                raise ValueError("Message was not addressed to this agent")
            old = row["status"]
            receipt = db.execute("SELECT result FROM receipts WHERE message_id=? AND agent=? AND status=?",
                                 (message_id, agent, status)).fetchone()
            if receipt:
                if receipt["result"] != _public_json(result):
                    raise CollaborationConflict("Conflicting receipt with the same status")
            elif status in transitions.get(old, ()):
                if recovery:
                    event("collab.delivery.recovery_reconciled",{"message_id":message_id,"agent":agent,
                          "previous_status":old,"new_status":status,"result":result},kind="state")
                category = db.execute("SELECT category FROM messages WHERE message_id=?", (message_id,)).fetchone()[0]
                if category == "request" and old == "received" and status == "completed":
                    raise CollaborationConflict("A work request must be accepted before completion")
                self._receipt(db, agent, message_id, status, result)
            else:
                raise CollaborationConflict(f"Invalid delivery state transition: {old} -> {status}")
            return {"message_id": message_id, "agent": agent, "status": status, "result": result}

    @traced("collab.bus.inbox", kind="tool")
    def inbox(self, agent, status=None):
        with self._db() as db:
            query = "SELECT m.* FROM messages m JOIN deliveries d USING(message_id) WHERE d.agent=?"
            args = [agent]
            if status is not None:
                query += " AND d.status=?"
                args.append(status)
            return [self._message(db, row) for row in db.execute(query + " ORDER BY m.seq", args)]

    def _worker_deliveries(self, agent, correlation_id, story_id):
        # Called inside the worker's traced wait boundary. Idle ticks do not materialize
        # full message payloads or create a separate trace/span for each empty poll.
        with self._db() as db:
            return [dict(row) for row in db.execute("""
                SELECT m.message_id,d.status FROM deliveries d JOIN messages m USING(message_id)
                WHERE d.agent=? AND m.correlation_id=? AND m.story_id=? AND d.status<>'completed'
                ORDER BY m.seq""", (agent, correlation_id, story_id))]

    @traced("collab.bus.messages", kind="tool")
    def messages(self, correlation_id=None, *, causation_id=None, message_type=None):
        with self._db() as db:
            conditions = []
            args = []
            for key, value in (("correlation_id", correlation_id), ("causation_id", causation_id),
                               ("message_type", message_type)):
                if value is not None:
                    conditions.append(key + "=?")
                    args.append(value)
            query = "SELECT * FROM messages" + (" WHERE " + " AND ".join(conditions) if conditions else "")
            return [self._message(db, row) for row in db.execute(query + " ORDER BY seq", args)]

    @traced("collab.bus.receipts", kind="tool")
    def receipts(self, message_id):
        with self._db() as db:
            result = []
            for row in db.execute("SELECT * FROM receipts WHERE message_id=? ORDER BY seq", (message_id,)):
                receipt = dict(row)
                receipt["result"] = json.loads(receipt["result"])
                receipt["trace_context"] = json.loads(receipt["trace_context"])
                result.append(receipt)
            return result
