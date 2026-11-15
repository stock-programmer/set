"""Durable session identities and delivery state, not a substitute trace log."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from trace_system import get_tracer, traced


class SessionRepository:
    @traced("pi.sessions.repository_open",kind="tool",capture_inputs=False,capture_output=False)
    def __init__(self, directory):
        self.directory=Path(directory); self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/"registry.sqlite3"
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,story_id TEXT NOT NULL,role TEXT NOT NULL,
              source_job TEXT,project_id TEXT,session_file TEXT,pi_session_id TEXT,active_job TEXT,
              UNIQUE(story_id,role));
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,
              role TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,job_id TEXT,trace_id TEXT,
              mode TEXT NOT NULL DEFAULT 'work',
              created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            """)
            if "mode" not in {r[1] for r in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN mode TEXT NOT NULL DEFAULT 'work'")

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    @traced("pi.sessions.ensure",kind="tool",capture_inputs=False)
    def ensure(self, story_id, role, source_job=None, project_id=None):
        if role not in {"script","video"}:raise ValueError("Agent role must be script or video")
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO sessions(id,story_id,role,source_job,project_id) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex,story_id,role,source_job,project_id))
            db.execute("UPDATE sessions SET source_job=COALESCE(source_job,?),project_id=COALESCE(?,project_id) WHERE story_id=? AND role=?",
                (source_job,project_id,story_id,role))
            row=db.execute("SELECT * FROM sessions WHERE story_id=? AND role=?",(story_id,role)).fetchone()
        return dict(row)

    @traced("pi.sessions.find",kind="tool",capture_inputs=False)
    def find(self,story_id,role):
        with self.db() as db:row=db.execute("SELECT * FROM sessions WHERE story_id=? AND role=?",(story_id,role)).fetchone()
        return dict(row) if row else None

    @traced("pi.sessions.read",kind="tool",capture_inputs=False)
    def get(self, session_id):
        with self.db() as db:
            row=db.execute("SELECT * FROM sessions WHERE id=?",(session_id,)).fetchone()
        if row is None:raise KeyError("会话不存在")
        return dict(row)

    @traced("pi.sessions.update",kind="tool",capture_inputs=False)
    def update(self, session_id, **fields):
        if set(fields)-{"session_file","pi_session_id","active_job","project_id"}:raise ValueError("Invalid session update")
        if fields:
            with self.db() as db:
                db.execute("UPDATE sessions SET "+",".join(k+"=?" for k in fields)+" WHERE id=?",(*fields.values(),session_id))

    @traced("pi.messages.append",kind="tool",capture_inputs=False)
    def append(self, session_id, role, text, *, message_id=None, status="queued", job_id=None, trace_id=None, mode="work"):
        if mode not in {"work","inspect"}:raise ValueError("Invalid conversation permission mode")
        message_id=message_id or uuid.uuid4().hex
        text=get_tracer().sanitize(text)
        with self.db() as db:
            prior=db.execute("SELECT * FROM messages WHERE id=?",(message_id,)).fetchone()
            if prior:
                if prior["session_id"]!=session_id or prior["text"]!=text or prior["role"]!=role or prior["mode"]!=mode:raise ValueError("消息标识已用于其他内容或权限范围")
                return dict(prior)
            db.execute("INSERT INTO messages(id,session_id,role,text,status,job_id,trace_id,mode) VALUES(?,?,?,?,?,?,?,?)",
                (message_id,session_id,role,text,status,job_id,trace_id,mode))
            row=db.execute("SELECT * FROM messages WHERE id=?",(message_id,)).fetchone()
        return dict(row)

    @traced("pi.messages.list",kind="tool",capture_inputs=False)
    def messages(self, session_id):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at,rowid",(session_id,))]

    @traced("pi.messages.status",kind="tool",capture_inputs=False)
    def delivered(self, message_id, status="delivered", job_id=None):
        with self.db() as db:
            db.execute("UPDATE messages SET status=?,job_id=COALESCE(?,job_id) WHERE id=?",(status,job_id,message_id))

    @traced("pi.sessions.recover_metadata",kind="task",capture_inputs=False)
    def recover(self):
        with self.db() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM sessions WHERE active_job IS NOT NULL")]
        # The application process lock proves the previous service is gone.
        for row in rows:
            self.reconcile(row["id"]);self.update(row["id"],active_job=None)
        return {"recovered_sessions":len(rows),"replayed_paid_operations":0}

    @traced("pi.messages.reconcile",kind="task",capture_inputs=False)
    def reconcile(self,session_id,exclude_ids=()):
        session=self.get(session_id);observed=[]
        if session["session_file"] and Path(session["session_file"]).is_file():
            for line in Path(session["session_file"]).read_text("utf-8").splitlines():
                try:entry=json.loads(line)
                except ValueError:continue  # an incomplete final entry is not proof of delivery
                message=entry.get("message",{})
                if entry.get("type")=="message" and message.get("role")=="user":
                    observed.append(json.dumps(message.get("content"),ensure_ascii=False))
        text="\n".join(observed)
        for message in self.messages(session_id):
            if message["id"] not in exclude_ids and message["role"] in {"user","agent"} and message["status"] in {"queued","dispatching"}:
                self.delivered(message["id"],"delivered" if "[message_id="+message["id"]+"]" in text else "queued")
        return {"observed_user_entries":len(observed),"replayed_operations":0}
