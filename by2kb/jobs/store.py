from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from by2kb.jobs.model import Job, JobStatus, utcnow_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT,
    destination TEXT,
    options_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_category TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idempotency_key
    ON jobs (platform, video_id);
"""


class JobStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def create_job(self, job: Job) -> None:
        self._conn.execute(
            "INSERT INTO jobs (id, platform, video_id, status, requested_by,"
            " destination, options_json, attempt_count, last_error_category,"
            " error_message, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.platform,
                job.video_id,
                job.status.value,
                job.requested_by,
                job.destination,
                json.dumps(job.options, ensure_ascii=False),
                job.attempt_count,
                job.last_error_category,
                job.error_message,
                job.created_at,
                job.updated_at,
            ),
        )
        self._conn.commit()

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, last_error_category = ?, error_message = ?,"
            " attempt_count = attempt_count + 1, updated_at = ? WHERE id = ?",
            (status.value, error_category, error_message, utcnow_iso(), job_id),
        )
        self._conn.commit()

    def get_job(self, job_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _row_to_job(row) if row else None

    def find_existing(self, platform: str, video_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE platform = ? AND video_id = ?",
            (platform, video_id),
        ).fetchone()
        return _row_to_job(row) if row else None

    def add_artifact(
        self, job_id: str, kind: str, path: str, content_hash: str | None = None
    ) -> None:
        self._conn.execute(
            "DELETE FROM artifacts WHERE job_id = ? AND kind = ?", (job_id, kind)
        )
        self._conn.execute(
            "INSERT INTO artifacts (job_id, kind, path, content_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, kind, path, content_hash, utcnow_iso()),
        )
        self._conn.commit()

    def artifacts(self, job_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT kind, path, content_hash, created_at FROM artifacts"
            " WHERE job_id = ? ORDER BY kind",
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        platform=row["platform"],
        video_id=row["video_id"],
        status=JobStatus(row["status"]),
        requested_by=row["requested_by"],
        destination=row["destination"],
        options=json.loads(row["options_json"] or "{}"),
        attempt_count=row["attempt_count"],
        last_error_category=row["last_error_category"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
