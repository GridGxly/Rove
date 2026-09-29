"""Exact workflow state. Large artifacts and applicant facts remain private files."""

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS applications (
              id TEXT PRIMARY KEY, job_key TEXT UNIQUE NOT NULL,
              profile_hash TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS browser_runs (
              id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id),
              status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints (
              id INTEGER PRIMARY KEY, browser_run TEXT NOT NULL REFERENCES browser_runs(id),
              event TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS submission_attempts (
              application_id TEXT PRIMARY KEY REFERENCES applications(id),
              package_hash TEXT NOT NULL, status TEXT NOT NULL);
        """)

    def create(self, job_key: str, profile: dict) -> tuple[str, str]:
        digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        application_id, browser_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO applications VALUES (?,?,?,?)",
                (application_id, job_key, digest, "PREPARING"),
            )
            row = self.db.execute(
                "SELECT id,profile_hash FROM applications WHERE job_key=?", (job_key,)
            ).fetchone()
            if row[1] != digest:
                raise ValueError("Existing application uses a different frozen profile")
            application_id = row[0]
            self.db.execute(
                "INSERT INTO browser_runs VALUES (?,?,?)", (browser_id, application_id, "PREPARING")
            )
        return application_id, browser_id

    def checkpoint(self, browser_id: str, event: str):
        with self.db:
            self.db.execute(
                "INSERT INTO checkpoints(browser_run,event) VALUES (?,?)", (browser_id, event)
            )

    def status(self, application_id: str, status: str):
        if status not in {"PREPARING", "NEEDS_USER", "READY_FOR_REVIEW", "UNKNOWN_SUBMISSION"}:
            raise ValueError("Status cannot assert a submission result")
        with self.db:
            self.db.execute("UPDATE applications SET status=? WHERE id=?", (status, application_id))

    def begin_submission(self, *_args, **_kwargs):
        # No submission implementation is enabled until the separate acceptance gates pass.
        raise PermissionError("Prepare-only runtime: submission is disabled")

    def finish_preparation(self, browser_id: str):
        """Complete a verified prepare-only run and its application atomically."""
        with self.db:
            row = self.db.execute(
                "SELECT application_id FROM browser_runs WHERE id=?", (browser_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown browser run")
            self.db.execute("UPDATE browser_runs SET status='NEEDS_USER' WHERE id=?", (browser_id,))
            self.db.execute("UPDATE applications SET status='NEEDS_USER' WHERE id=?", (row[0],))
