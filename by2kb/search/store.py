from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from by2kb.errors import ConfigError
from by2kb.operation_lock import exclusive_file


class SearchStore:
    """Selection state is separate from ingestion jobs; identifiers are never model-made."""

    def __init__(self, home: Path):
        self.root = home / "search"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "sessions.db"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, scope TEXT NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS active (scope TEXT PRIMARY KEY, session_id TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, topic: str, scope: str, ttl: int) -> dict:
        session = {"schema_version": 1, "session_id": uuid.uuid4().hex, "topic": topic,
                   "status": "searching", "candidates": [], "warnings": [], "selected": [], "outcomes": {}}
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                       (session["session_id"], scope, time.time() + ttl, json.dumps(session, ensure_ascii=False)))
            db.execute("INSERT INTO active VALUES (?, ?) ON CONFLICT(scope) DO UPDATE SET session_id=excluded.session_id",
                       (scope, session["session_id"]))
        return session

    def load(self, session_id: str, scope: str, *, require_active: bool = True) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT scope, expires, payload FROM sessions WHERE id=?", (session_id,)).fetchone()
            active = db.execute("SELECT session_id FROM active WHERE scope=?", (scope,)).fetchone()
        if row is None or row[0] != scope:
            raise ConfigError("search session not found for this user/chat")
        if row[1] <= time.time():
            raise ConfigError("search recommendations expired; search again")
        if require_active and (active is None or active[0] != session_id):
            raise ConfigError("search recommendations superseded; use the latest list")
        return json.loads(row[2])

    def latest(self, scope: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT session_id FROM active WHERE scope=?", (scope,)).fetchone()
        if not row:
            return None
        try:
            return self.load(row[0], scope)
        except ConfigError:
            return None

    def save(self, session: dict):
        with self.connect() as db:
            db.execute("UPDATE sessions SET payload=? WHERE id=?",
                       (json.dumps(session, ensure_ascii=False), session["session_id"]))

    def finish(self, session: dict):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM sessions WHERE id=?", (session["session_id"],)).fetchone()
            if row and json.loads(row[0])["status"] == "searching":
                db.execute("UPDATE sessions SET payload=? WHERE id=?",
                           (json.dumps(session, ensure_ascii=False), session["session_id"]))
            elif row:
                session["status"] = json.loads(row[0])["status"]

    def cancel(self, session_id: str, scope: str) -> dict:
        key = hashlib.sha256(session_id.encode()).hexdigest()
        with exclusive_file(self.root.parent / "locks" / f"search-{key}.lock"):
            session = self.load(session_id, scope)
            if session["status"] == "selected":
                raise ConfigError("selection already submitted; use by2kb cancel JOB_ID for ingestion")
            session["status"] = "cancelled"
            self.save(session)
            return session

    def cache_path(self, session_id: str, number: int) -> Path:
        if not (len(session_id) == 32 and all(c in "0123456789abcdef" for c in session_id)) or not 1 <= number <= 5:
            raise ConfigError("invalid search cache identity")
        return self.root / "captions" / session_id / f"{number}.json"

    def put_caption(self, session_id: str, number: int, normalized):
        path = self.cache_path(session_id, number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized.model_dump_json(), encoding="utf-8")
        return hashlib.sha256(path.read_bytes()).hexdigest()
