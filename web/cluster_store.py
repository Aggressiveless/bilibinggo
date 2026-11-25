
"""Single-writer cluster coordinator. Workers interact through the peer API."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from web.cluster_protocol import Activity, Analysis, Change, Exchange, Feed, KEYWORDS, Lease, PAGE_SIZE, Register, Report, TARGET, Task

LEASE_SECONDS = 120
MAX_ATTEMPTS = 4
MAX_NODES = 32
ROUND_INTERVAL = 1800
EXHAUSTED_INTERVAL = 3600
VIDEO_RECHECK = 12 * 3600
SEARCH_PAGE_LIMIT = 60
def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ClusterError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ClusterStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "cluster.db"
        with self.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS nodes(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL,
                    seen INTEGER NOT NULL, work INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0, model_calls INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS catalog(
                    id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, payload TEXT NOT NULL,
                    rank INTEGER NOT NULL, expires INTEGER NOT NULL, origin TEXT NOT NULL, checked INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, round INTEGER NOT NULL, kind TEXT NOT NULL,
                    key TEXT NOT NULL, params TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    node TEXT, lease TEXT, until INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(round,kind,key)
                );
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(round,state,due,id);
                CREATE TABLE IF NOT EXISTS analyses(
                    key TEXT PRIMARY KEY, lease TEXT NOT NULL, until INTEGER NOT NULL, fields TEXT
                );
                CREATE TABLE IF NOT EXISTS checked_videos(id TEXT PRIMARY KEY, checked INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sightings(
                    round INTEGER, job INTEGER, kind TEXT, key TEXT,
                    PRIMARY KEY(round,job,kind,key)
                );
            """)

    @contextmanager
    def tx(self, *, write=True):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def get(db, key, default=None):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, _dump(value)))

    def read(self, key, default=None):
        with self.tx(write=False) as db:
            return self.get(db, key, default)

    def write(self, key, value):
        with self.tx() as db:
            self.put(db, key, value)

    def start_host(self, name, address):
        with self.tx() as db:
            if self.get(db, "session"):
                raise ClusterError("请先退出当前集群")
            for table in ("nodes", "catalog", "jobs", "checked_videos", "sightings", "analyses"):
                db.execute(f"DELETE FROM {table}")
            session = {"role": "host", "name": name, "address": address, "id": uuid.uuid4().hex,
                       "node": uuid.uuid4().hex, "token": secrets.token_hex(32)}
            db.execute("DELETE FROM settings")
            self.put(db, "session", session)
            self.put(db, "invite", secrets.token_hex(32))
            self._register(db, Register(name=name, node=session["node"], token=session["token"]), int(time.time()))
            self.put(db, "round", 0)
            self.put(db, "phase", "running")
            return session

    def register(self, body: Register, invite: str):
        now = int(time.time())
        with self.tx() as db:
            session = self.get(db, "session", {})
            if (session.get("role") != "host" or not secrets.compare_digest(invite, self.get(db, "invite", ""))):
                raise ClusterError("集群未开启或邀请码无效", 401)
            if body.node == session["node"]:
                raise ClusterError("不能加入本机发起的集群")
            self._register(db, body, now)
            return {"cluster_id": session["id"], "protocol": 1}

    @staticmethod
    def _register(db, body, now):
        existing = db.execute("SELECT token FROM nodes WHERE id=?", (body.node,)).fetchone()
        hashed = token_hash(body.token)
        if existing and not secrets.compare_digest(existing["token"], hashed):
            raise ClusterError("节点标识已被占用")
        if not existing and db.execute("SELECT count(*) FROM nodes").fetchone()[0] >= MAX_NODES:
            raise ClusterError("集群节点已满")
        db.execute("""INSERT INTO nodes(id,name,token,seen) VALUES(?,?,?,?)
                      ON CONFLICT(id) DO UPDATE SET name=excluded.name,seen=excluded.seen""",
                   (body.node, body.name, hashed, now))

    def _authenticate(self, db, node, token):
        session = self.get(db, "session", {})
        row = db.execute("SELECT token FROM nodes WHERE id=?", (node,)).fetchone()
        if (session.get("role") != "host" or not row
                or not secrets.compare_digest(row["token"], token_hash(token))):
            raise ClusterError("节点授权失效，请重新加入集群", 401)
        return session

    def leave(self, node, token):
        with self.tx() as db:
            self._authenticate(db, node, token)
            db.execute("DELETE FROM nodes WHERE id=?", (node,))
            db.execute("UPDATE jobs SET until=0 WHERE node=? AND state='leased'", (node,))

    def analysis(self, body: Analysis, token: str):
        now = int(time.time())
        with self.tx() as db:
            self._authenticate(db, body.node, token)
            job = db.execute(
                "SELECT 1 FROM jobs WHERE id=? AND node=? AND lease=? AND state='leased' AND until>?",
                (body.task_id, body.node, body.lease, now),
            ).fetchone()
            if not job:
                raise ClusterError("任务已交回，请等待重新分配")
            row = db.execute("SELECT * FROM analyses WHERE key=?", (body.key,)).fetchone()
            if row and row["until"] > now and row["fields"]:
                return {"fields": json.loads(row["fields"])}
            if row and row["until"] > now and row["lease"] != body.lease:
                return {"busy": True}
            db.execute("""INSERT INTO analyses VALUES(?,?,?,?)
                          ON CONFLICT(key) DO UPDATE SET lease=excluded.lease,until=excluded.until,fields=excluded.fields""",
                       (body.key, body.lease, now + (30 * 86400 if body.fields else 330),
                        _dump(body.fields) if body.fields else None))
            return {"fields": body.fields}

    def set_paused(self, paused):
        with self.tx() as db:
            if self.get(db, "session", {}).get("role") != "host":
                raise ClusterError("只有发起人可以控制检索")
            self.put(db, "paused", paused)

    def _queue(self, db, round_id, kind, key, **params):
        db.execute("INSERT OR IGNORE INTO jobs(round,kind,key,params) VALUES(?,?,?,?)",
                   (round_id, kind, key, _dump(params)))

    def _next_round(self, db, now):
        round_id = self.get(db, "round", 0) + 1
        self.put(db, "round", round_id)
        self.put(db, "phase", "running")
        self.put(db, "next_round", 0)
        # Order first pages before any later pages. Both sorts cover different results.
        for order in ("pubdate", "totalrank"):
            for word in KEYWORDS:
                self._queue(db, round_id, "search", f"{order}:{word}:1", keyword=word, order=order, page=1)
        for row in db.execute("""SELECT id FROM catalog WHERE expires>? AND checked<?
                              ORDER BY checked,expires LIMIT 40""", (now, now - 1800)):
            self._queue(db, round_id, "dynamic", row["id"], depth=4)
        db.execute("DELETE FROM sightings WHERE round<?", (round_id - 2,))
        db.execute("DELETE FROM jobs WHERE round<?", (round_id - 2,))
        db.execute("DELETE FROM checked_videos WHERE checked<?", (now - 7 * 86400,))
        db.execute("DELETE FROM catalog WHERE expires<?", (now - 7 * 86400,))
        db.execute("DELETE FROM analyses WHERE until<=?", (now,))

    def _catalog(self, db, activity: Activity, rank: int, now: int, origin: str):
        expires = min(activity.lottery_time, activity.deadline or activity.lottery_time)
        if expires <= now and not activity.closed:
            return
        payload = _dump(activity.model_dump())
        old = db.execute("SELECT payload,rank,origin FROM catalog WHERE id=?", (activity.dynamic_id,)).fetchone()
        if old:
            if old["rank"] > rank or (rank == 1 and old["origin"] != origin):
                return
            if old["payload"] == payload:
                db.execute("UPDATE catalog SET rank=?, checked=? WHERE id=?",
                           (rank, now, activity.dynamic_id))
                return
        if activity.closed and old is None:
            return
        seq = self.get(db, "sequence", 0) + 1
        self.put(db, "sequence", seq)
        db.execute("""INSERT INTO catalog VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                      seq=excluded.seq,payload=excluded.payload,rank=excluded.rank,expires=excluded.expires,
                      origin=excluded.origin,checked=excluded.checked""",
                   (activity.dynamic_id, seq, payload, rank, now if activity.closed else expires, origin, now))

    def _retry(self, db, job, now, *, delay):
        db.execute("UPDATE jobs SET state=?,node=NULL,lease=NULL,until=0,due=? WHERE id=?",
                   ("failed" if job["attempts"] >= MAX_ATTEMPTS else "pending", now + delay, job["id"]))

    def _finish(self, db, node, report: Report, now):
        job = db.execute("SELECT * FROM jobs WHERE id=? AND node=? AND lease=? AND state='leased' AND until>?",
                         (report.task_id, node, report.lease, now)).fetchone()
        if not job:
            return
        db.execute("UPDATE nodes SET model_calls=model_calls+? WHERE id=?", (report.model_calls, node))
        db.execute("DELETE FROM analyses WHERE lease=? AND fields IS NULL", (report.lease,))
        if report.outcome == "cooldown":
            until = max(now + report.cooldown, self.get(db, "cooldown_until", 0))
            self.put(db, "cooldown_until", until)
            db.execute("UPDATE jobs SET attempts=attempts-1 WHERE id=?", (job["id"],))
            db.execute("UPDATE jobs SET state='pending',node=NULL,lease=NULL,until=0,due=? WHERE id=?",
                       (until, job["id"]))
            return
        if report.outcome == "retry":
            self._retry(db, job, now, delay=30 * 2 ** (job["attempts"] - 1))
            return
        round_id, kind, params = job["round"], job["kind"], json.loads(job["params"])
        db.execute("UPDATE jobs SET state='done',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
        db.execute("UPDATE nodes SET completed=completed+1 WHERE id=?", (node,))
        if kind == "video":
            db.execute("INSERT OR REPLACE INTO checked_videos VALUES(?,?)", (job["key"], now))
        for item in report.activities:
            if kind == "dynamic" and item.dynamic_id == job["key"]:
                self._catalog(db, item, 2, now, node)
        if kind == "search":
            self._add_videos(db, job, report, now)
            count = self._video_count(db, round_id)
            previous = db.execute(
                "SELECT id FROM jobs WHERE round=? AND kind='search' AND key=?",
                (round_id, f"{params['order']}:{params['keyword']}:{params['page'] - 1}"),
            ).fetchone()
            repeated = previous and not db.execute("""SELECT key FROM sightings WHERE job=?
                EXCEPT SELECT key FROM sightings WHERE job=?""", (job["id"], previous["id"])).fetchone()
            if report.more_pages and not repeated and count < TARGET and params["page"] < SEARCH_PAGE_LIMIT:
                params["page"] += 1
                next_key = f"{params['order']}:{params['keyword']}:{params['page']}"
                self._queue(db, round_id, "search", next_key, **params)
            if count >= TARGET:
                db.execute("UPDATE jobs SET state='skipped' WHERE round=? AND kind='search' AND state='pending'", (round_id,))
        elif params.get("depth", 0) < 4:
            for ref in report.references:
                if ref.kind != "video":
                    db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,?,?)", (round_id, job["id"], ref.kind, ref.key))
                    self._queue(db, round_id, ref.kind, ref.key, depth=params.get("depth", 0) + 1)

    @staticmethod
    def _video_count(db, round_id):
        return db.execute("SELECT count(*) FROM jobs WHERE round=? AND kind='video'", (round_id,)).fetchone()[0]

    def _add_videos(self, db, job, report, now):
        round_id = job["round"]
        remaining = TARGET - self._video_count(db, round_id)
        for ref in report.references:
            if ref.kind != "video":
                continue
            db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,?,?)", (round_id, job["id"], ref.kind, ref.key))
            if remaining <= 0 or db.execute("SELECT 1 FROM checked_videos WHERE id=? AND checked>?",
                                            (ref.key, now - VIDEO_RECHECK)).fetchone():
                continue
            if db.execute("INSERT OR IGNORE INTO jobs(round,kind,key,params) VALUES(?,'video',?,'{}')",
                          (round_id, ref.key)).rowcount:
                remaining -= 1

    def _tick(self, db, now):
        for job in db.execute("SELECT * FROM jobs WHERE state='leased' AND until<=?", (now,)).fetchall():
            self._retry(db, job, now, delay=0)
        if self.get(db, "paused", False) or self.get(db, "cooldown_until", 0) > now:
            return
        round_id = self.get(db, "round", 0)
        if not round_id:
            self._next_round(db, now)
        elif not db.execute("SELECT 1 FROM jobs WHERE round=? AND state IN ('pending','leased') LIMIT 1",
                            (round_id,)).fetchone():
            next_round = self.get(db, "next_round", 0)
            if not next_round:
                found = self._video_count(db, round_id)
                failed = db.execute("SELECT count(*) FROM jobs WHERE round=? AND state='failed'",
                                    (round_id,)).fetchone()[0]
                phase = "incomplete" if failed else "complete" if found >= TARGET else "exhausted"
                self.put(db, "phase", phase)
                self.put(db, "next_round", now + (ROUND_INTERVAL if phase == "complete" else EXHAUSTED_INTERVAL))
            elif next_round <= now:
                self._next_round(db, now)

    def _claim(self, db, node, reported, now):
        if (self.get(db, "paused", False) or self.get(db, "cooldown_until", 0) > now
                or db.execute("SELECT 1 FROM jobs WHERE node=? AND state='leased'", (node,)).fetchone()
                or db.execute("SELECT count(*) FROM jobs WHERE state='leased'").fetchone()[0] >= 4):
            return None
        job = db.execute("""SELECT * FROM jobs WHERE round=? AND state='pending' AND due<=? AND id<>?
                            ORDER BY CASE kind WHEN 'dynamic' THEN 0 ELSE 1 END,id LIMIT 1""",
                         (self.get(db, "round", 0), now, reported)).fetchone()
        if job is None:
            return None
        lease = uuid.uuid4().hex
        db.execute("UPDATE jobs SET state='leased',node=?,lease=?,until=?,attempts=attempts+1 WHERE id=?",
                   (node, lease, now + LEASE_SECONDS, job["id"]))
        return Task(task_id=job["id"], lease=lease, kind=job["kind"], key=job["key"], **json.loads(job["params"]))

    def exchange(self, body: Exchange, token: str) -> Feed:
        now = int(time.time())
        with self.tx() as db:
            session = self._authenticate(db, body.node, token)
            db.execute("UPDATE nodes SET seen=?,work=? WHERE id=?", (now, int(body.work), body.node))
            for item in body.activities:
                if not item.closed:
                    self._catalog(db, item, 1, now, body.node)
            if body.working:
                self._renew(db, body.node, body.working, now)
            if body.report:
                self._finish(db, body.node, body.report, now)
            self._tick(db, now)
            task = self._claim(db, body.node, body.report.task_id if body.report else 0, now) if body.work and not body.working else None
            rows = db.execute("SELECT seq,payload FROM catalog WHERE seq>? ORDER BY seq LIMIT ?",
                              (body.after, PAGE_SIZE))
            changes = [Change(seq=row["seq"], activity=Activity.model_validate_json(row["payload"])) for row in rows]
            return Feed(task=task, changes=changes, status=self._status(db, session, now))

    @staticmethod
    def _renew(db, node, lease: Lease, now):
        db.execute("""UPDATE jobs SET until=? WHERE id=? AND node=? AND lease=?
                      AND state='leased' AND until>?""",
                   (now + LEASE_SECONDS, lease.task_id, node, lease.lease, now))

    def status(self):
        with self.tx(write=False) as db:
            session = self.get(db, "session", {})
            return self._status(db, session, int(time.time()))

    def _status(self, db, session, now):
        round_id = self.get(db, "round", 0)
        counts = {}
        for row in db.execute("SELECT kind,state,count(*) AS n FROM jobs WHERE round=? GROUP BY kind,state", (round_id,)):
            counts.setdefault(row["kind"], {})[row["state"]] = row["n"]
        nodes = [dict(row) for row in db.execute("""
            SELECT n.id,n.name,n.seen,n.work,n.completed,n.model_calls,j.kind,j.key
            FROM nodes n LEFT JOIN jobs j ON n.id=j.node AND j.state='leased' AND j.until>?
            ORDER BY n.seen DESC,n.name""", (now,))]
        for node in nodes:
            node["online"] = node["seen"] > now - 30
            node["host"] = node["id"] == session.get("node")
        return {"round": round_id, "target": TARGET, "counts": counts, "nodes": nodes,
                "now": now, "phase": self.get(db, "phase", "running"), "next_round": self.get(db, "next_round", 0),
                "paused": self.get(db, "paused", False), "cooldown_until": self.get(db, "cooldown_until", 0),
                "sequence": self.get(db, "sequence", 0),
                "active": db.execute("SELECT count(*) FROM catalog WHERE expires>?", (now,)).fetchone()[0]}
