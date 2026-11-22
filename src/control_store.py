"""Durable control handles, idempotency and artifacts; no application imports."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class ControlError(Exception):
    def __init__(self, code: str, message: str, *, job_id: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code, self.job_id, self.retryable = code, job_id, retryable


class ControlStore:
    def __init__(self, directory: str | Path):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "control.db"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records(kind TEXT, key TEXT, payload TEXT NOT NULL,
                    PRIMARY KEY(kind,key));
                CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    result TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS control_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL, backend_id INTEGER, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS control_events_job ON events(job_id,seq);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def put(self, kind: str, key: str, value: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload",
                       (kind, key, json.dumps(value, ensure_ascii=False)))

    def get(self, kind: str, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def records(self, kind: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM records WHERE kind=? ORDER BY key", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def claim(self, key: str, parameters: dict) -> dict | None:
        fingerprint = digest(parameters)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,result FROM operations WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ControlError("REVISION_CONFLICT", "幂等键已用于不同参数")
                if row[1] is None:
                    raise ControlError("OUTCOME_UNKNOWN", "原请求尚在执行或被中断，请查询任务，不要重复平台写入")
                return json.loads(row[1])
            db.execute("INSERT INTO operations VALUES(?,?,NULL,?)", (key, fingerprint, utc_now()))
        return None

    def complete(self, key: str, result: dict) -> None:
        with self.connect() as db:
            db.execute("UPDATE operations SET result=? WHERE key=?", (json.dumps(result, ensure_ascii=False), key))

    def new_job(self, action: str, boot_id: str, *, owner="app", metadata=None) -> dict:
        payload = {"job_id": 0, "state": "running", "action": action, "boot_id": boot_id,
                   "result": None, "can_cancel": False, "cancel_requested": False,
                   "created_at": utc_now(), "finished_at": None,
                   "progress": {"step": 0, "total": 0, "message": "任务已接收"}, **(metadata or {})}
        with self.connect() as db:
            cursor = db.execute("INSERT INTO control_jobs(owner,payload) VALUES(?,?)", (owner, json.dumps(payload)))
            payload["job_id"] = cursor.lastrowid
            db.execute("UPDATE control_jobs SET payload=? WHERE id=?", (json.dumps(payload), cursor.lastrowid))
        self.event(payload["job_id"], {"kind": "created", "job": payload})
        return payload

    def bind_job(self, job_id: int, backend_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE control_jobs SET backend_id=? WHERE id=?", (backend_id, job_id))

    def job_record(self, job_id: int) -> tuple[str, int | None, dict]:
        with self.connect() as db:
            row = db.execute("SELECT owner,backend_id,payload FROM control_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ControlError("NOT_FOUND", "未找到该控制任务")
        return row[0], row[1], json.loads(row[2])

    def update_job(self, job_id: int, **changes) -> dict:
        with self.lock:
            _, _, payload = self.job_record(job_id)
            updated = {**payload, **changes}
            if updated == payload:
                return payload
            if updated["state"] != "running" and not updated.get("finished_at"):
                updated["finished_at"] = utc_now()
            with self.connect() as db:
                db.execute("UPDATE control_jobs SET payload=? WHERE id=?", (json.dumps(updated, ensure_ascii=False), job_id))
            self.event(job_id, {"kind": "updated", "job": updated})
            return updated

    def list_jobs(self, *, before=2147483647, limit=50, state=None):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM control_jobs WHERE id<? ORDER BY id DESC LIMIT ?", (before, 1000)).fetchall()
        values = [json.loads(row[0]) for row in rows]
        return [v for v in values if state is None or v["state"] == state][:limit]

    def event(self, job_id: int, value: dict):
        with self.connect() as db:
            db.execute("INSERT INTO events(job_id,payload) VALUES(?,?)", (job_id, json.dumps({"ts": utc_now(), **value}, ensure_ascii=False)))

    def events(self, job_id: int, after=0, limit=100):
        self.job_record(job_id)
        with self.connect() as db:
            rows = db.execute("SELECT seq,payload FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?", (job_id, after, limit)).fetchall()
        return {"events": [{"seq": r[0], **json.loads(r[1])} for r in rows],
                "next_seq": rows[-1][0] if rows else after, "cursor_expired": False}

    def artifact(self, name: str, content: str | bytes, mime="text/plain", *, redacted=True) -> dict:
        identifier = uuid.uuid4().hex
        folder = self.root / "artifacts"
        folder.mkdir(exist_ok=True)
        raw = content.encode("utf-8") if isinstance(content, str) else content
        (folder / identifier).write_bytes(raw)
        value = {"artifact_id": identifier, "name": name, "mime_type": mime,
                 "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                 "resource_uri": f"binggo://artifacts/{identifier}", "redacted": redacted}
        self.put("artifact", identifier, value)
        return value

    def read_artifact(self, identifier: str, offset=0, size=65536):
        value = self.get("artifact", identifier)
        if not value or not identifier.isalnum():
            raise ControlError("NOT_FOUND", "未登记的产物")
        path = (self.root / "artifacts" / identifier).resolve()
        if path.parent != (self.root / "artifacts").resolve():
            raise ControlError("VALIDATION_ERROR", "产物路径越界")
        with path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(size)
        return {"artifact": value, "text": raw.decode("utf-8", errors="replace") if value["mime_type"].startswith(("text/", "application/json")) else None,
                "next_offset": offset + len(raw) if offset + len(raw) < value["size_bytes"] else None}
