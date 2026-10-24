import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..models import canonical


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, password TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants(user_id TEXT, environment_id TEXT, PRIMARY KEY(user_id,environment_id));
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT,csrf TEXT,expires REAL);
CREATE TABLE IF NOT EXISTS login_limits(id TEXT PRIMARY KEY, failures INTEGER, until REAL);
CREATE TABLE IF NOT EXISTS environments(id TEXT PRIMARY KEY, config TEXT, credentials TEXT, revision TEXT);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, scope TEXT, url TEXT, title TEXT, content TEXT,
 hash TEXT, fetched REAL, etag TEXT, modified TEXT, trust TEXT);
CREATE INDEX IF NOT EXISTS documents_scope ON documents(scope);
CREATE TABLE IF NOT EXISTS crawled_pages(url TEXT PRIMARY KEY,links TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(id UNINDEXED,title,content);
CREATE TABLE IF NOT EXISTS specs(id TEXT PRIMARY KEY, version TEXT, document TEXT, hash TEXT, origin TEXT, fetched REAL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, environment_id TEXT, payload TEXT,
 state TEXT, result TEXT, error TEXT, lease_until REAL, owner TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,kind TEXT,environment_id TEXT,payload TEXT,
 interval_seconds INTEGER,next_run REAL);
CREATE TABLE IF NOT EXISTS live_runs(id TEXT PRIMARY KEY, environment_id TEXT, creator TEXT, status TEXT,
 plan TEXT, hash TEXT, approval TEXT, created REAL, error TEXT);
CREATE TABLE IF NOT EXISTS live_steps(run_id TEXT, ordinal INTEGER, state TEXT, result TEXT,
 PRIMARY KEY(run_id,ordinal));
CREATE TABLE IF NOT EXISTS environment_locks(environment_id TEXT PRIMARY KEY,run_id TEXT);
CREATE TABLE IF NOT EXISTS tools(id TEXT PRIMARY KEY,environment_id TEXT,manifest TEXT,hash TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,environment_id TEXT,name TEXT,content TEXT,hash TEXT,size INTEGER);
CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,environment_id TEXT,name TEXT,content TEXT,hash TEXT,review TEXT);
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,at REAL,actor TEXT,
 environment_id TEXT,kind TEXT,details TEXT,previous TEXT,hash TEXT);
CREATE TABLE IF NOT EXISTS budgets(environment_id TEXT PRIMARY KEY,window REAL,requests INTEGER);
CREATE TABLE IF NOT EXISTS code_runs(id TEXT PRIMARY KEY,environment_id TEXT,draft_id TEXT,revision TEXT,content TEXT,created REAL);
CREATE TABLE IF NOT EXISTS answers(id TEXT PRIMARY KEY,environment_id TEXT,creator TEXT,kind TEXT,revision TEXT,content TEXT,created REAL);
CREATE INDEX IF NOT EXISTS answers_environment ON answers(environment_id,created);
CREATE TABLE IF NOT EXISTS model_profiles(id TEXT PRIMARY KEY,owner TEXT,name TEXT,config TEXT,secret TEXT,revision TEXT,created REAL,updated REAL);
CREATE TABLE IF NOT EXISTS model_preferences(user_id TEXT PRIMARY KEY,profile_id TEXT);
"""


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(path, timeout=20, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def execute(self, sql, args=()):
        return self.conn.execute(sql, args)

    def audit(self, actor, environment_id, kind, details):
        from ..models import digest
        # Call inside a transaction so sequence and hash chain cannot race.
        last = self.one("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1")
        previous = last["hash"] if last else "0" * 64
        event = {"at": time.time(), "actor": actor, "environment": environment_id,
                 "kind": kind, "details": details, "previous": previous}
        self.execute("INSERT INTO audit(at,actor,environment_id,kind,details,previous,hash) VALUES(?,?,?,?,?,?,?)",
                     (event["at"], actor, environment_id, kind, canonical(details), previous, digest(event)))

    def enqueue(self, kind, environment_id, payload):
        job_id, at = uuid.uuid4().hex, time.time()
        self.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (job_id, kind, environment_id, canonical(payload), "queued", None, None, 0, None, at, at))
        return job_id


def decoded(row, *fields):
    if row:
        for field in fields:
            row[field] = json.loads(row[field]) if row[field] else None
    return row
