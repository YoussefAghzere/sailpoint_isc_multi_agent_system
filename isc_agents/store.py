"""Local durable state. Every public run access requires a complete scope."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

from .models import Rejected, canonical, validate, SCOPE


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL,
            plan TEXT NOT NULL, approval TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (
            scope TEXT NOT NULL, id TEXT NOT NULL, config TEXT NOT NULL,
            PRIMARY KEY(scope,id));
        CREATE TABLE IF NOT EXISTS steps (
            run_id TEXT NOT NULL, name TEXT NOT NULL, result TEXT NOT NULL,
            PRIMARY KEY(run_id,name));
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            at TEXT NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, run_id, scope):
        validate(SCOPE, scope)
        row = self.db.execute("SELECT * FROM runs WHERE id=? AND scope=?",
                              (run_id, canonical(scope))).fetchone()
        if row is None:
            raise Rejected("Run not found in authorized scope")
        result = dict(row)
        for key in ("scope", "plan", "approval"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def source(self, scope, source_id):
        row = self.db.execute("SELECT config FROM sources WHERE scope=? AND id=?",
                              (canonical(scope), source_id)).fetchone()
        return json.loads(row[0]) if row else None

    def event(self, run_id, kind, details):
        self.db.execute("INSERT INTO events(run_id,at,kind,details) VALUES(?,?,?,?)",
                        (run_id, now(), kind, canonical(details)))

    def events(self, run_id, scope):
        self.get(run_id, scope)
        return [{**dict(r), "details": json.loads(r["details"])} for r in
                self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
