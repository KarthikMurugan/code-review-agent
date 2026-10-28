"""Durable single-host queue. SQLite transactions arbitrate API/worker access."""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

REVIEW_VERSION = "comments-v1"


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                    report TEXT, review_id INTEGER, error TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, delivery: str, payload: dict) -> tuple[str, bool]:
        identity = [payload[key] for key in ("installation_id", "repository_id", "number", "base_sha", "head_sha")]
        job_id = hashlib.sha256(json.dumps([identity, REVIEW_VERSION]).encode()).hexdigest()
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT job_id FROM deliveries WHERE id=?", (delivery,)).fetchone()
            if existing:
                return existing["job_id"], False
            inserted = db.execute(
                "INSERT OR IGNORE INTO jobs(id,payload,status,created,updated) VALUES(?,?,'queued',?,?)",
                (job_id, json.dumps(payload), now, now),
            ).rowcount
            db.execute("INSERT INTO deliveries VALUES(?,?)", (delivery, job_id))
        return job_id, bool(inserted)

    def get(self, job_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def claim(self):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status='failed',updated=? WHERE status='running' AND lease_until<? AND attempts>=3", (now, now))
            row = db.execute(
                "SELECT id FROM jobs WHERE (status='queued' OR (status='running' AND lease_until<?)) "
                "AND attempts<3 ORDER BY created LIMIT 1", (now,),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running', attempts=attempts+1,lease_until=?,updated=? WHERE id=?",
                       (now + 900, now, row["id"]))
        return self.get(row["id"])

    def save_report(self, job_id: str, report: str):
        with self.connect() as db:
            db.execute("UPDATE jobs SET report=?,updated=? WHERE id=?", (report, time.time(), job_id))

    def finish(self, job_id: str, status: str, review_id: int | None = None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,review_id=?,error=NULL,lease_until=0,updated=? WHERE id=?",
                       (status, review_id, time.time(), job_id))

    def fail(self, job_id: str, error: str):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN attempts<3 THEN 'queued' ELSE 'failed' END, "
                       "error=?,lease_until=0,updated=? WHERE id=?", (error, time.time(), job_id))
