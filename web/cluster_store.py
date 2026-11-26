
"""Single-writer cluster coordinator. Workers interact through the peer API."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from web.activity_service import collection_source_for_mid
from web.cluster_protocol import (Activity, Analysis, Change, DispatchSettings, Exchange, Feed, KEYWORDS, Lease,
                                  PAGE_SIZE, Register, Report, TARGET, Task, VideoAssignment, WatchAssignment,
                                  WatchBatch, WatchFindings)

LEASE_SECONDS = 120
MAX_ATTEMPTS = 4
MAX_NODES = 32
MAX_CONCURRENT = 4
ONLINE_SECONDS = 30
IMPORT_TIMEOUT = 120
SEARCH_PAGE_LIMIT = 60
EVENT_RETENTION = 2000
RECENT_EVENTS = 80
DETECTIONS_PER_PAGE = 10
TRACKED_USERS_PER_PAGE = 12
JOB_LABEL = {"search": "检索", "video": "视频检查", "dynamic": "动态核验",
             "article": "专栏检查", "short": "短链展开", "author": "发布者检查", "watch": "追踪 UP 主"}
RETRY_LABEL = {"platform": "平台内容暂不可读", "model": "模型未返回有效结果",
               "network": "网络请求失败", "analysis_busy": "其它节点正在分析同一内容",
               "worker": "工作节点处理失败", "source_response": "B 站内容接口未返回可用数据",
               "dynamic_detail": "关联动态详情不可读取", "dynamic_body": "关联动态缺少可核验正文"}
DETECTION_EVENT = re.compile(
    r"视频检查 (BV[0-9A-Za-z]{10})完成：(核验 ([0-9]+) 项开放活动|未确认开放活动)(?:，调用模型 [0-9]+ 次)?"
)


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
                CREATE TABLE IF NOT EXISTS cluster_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, level TEXT NOT NULL,
                    node TEXT, node_name TEXT, message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cluster_events_node ON cluster_events(node,id);
                CREATE TABLE IF NOT EXISTS cluster_node_round(
                    round INTEGER NOT NULL, node TEXT NOT NULL,
                    pages INTEGER NOT NULL, found INTEGER NOT NULL,
                    videos INTEGER NOT NULL, activities INTEGER NOT NULL,
                    PRIMARY KEY(round,node)
                );
                CREATE TABLE IF NOT EXISTS cluster_video_results(
                    job INTEGER PRIMARY KEY, round INTEGER NOT NULL, node TEXT NOT NULL,
                    matched INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cluster_video_results_round ON cluster_video_results(round,node);
                CREATE TABLE IF NOT EXISTS node_detection_totals(
                    node TEXT PRIMARY KEY, videos INTEGER NOT NULL, counted INTEGER NOT NULL, matched INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nodes(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL,
                    seen INTEGER NOT NULL, work INTEGER NOT NULL DEFAULT 0,
                    search_ready INTEGER NOT NULL DEFAULT 0,
                    watch_batch INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS local_checked_videos(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS node_history(node TEXT PRIMARY KEY, complete INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS node_video_history(node TEXT, bvid TEXT, PRIMARY KEY(node,bvid));
                CREATE TABLE IF NOT EXISTS node_local_work(node TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS watch_sources(
                    node TEXT NOT NULL, mid TEXT NOT NULL, name TEXT NOT NULL, revision TEXT NOT NULL,
                    PRIMARY KEY(node,mid)
                );
                CREATE INDEX IF NOT EXISTS watch_sources_mid ON watch_sources(mid);
                CREATE TABLE IF NOT EXISTS watch_checks(mid TEXT PRIMARY KEY, checked INTEGER, hits INTEGER);
                CREATE TABLE IF NOT EXISTS sightings(
                    round INTEGER, job INTEGER, kind TEXT, key TEXT,
                    PRIMARY KEY(round,job,kind,key)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            node_columns = {row["name"] for row in db.execute("PRAGMA table_info(nodes)")}
            if "search_ready" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN search_ready INTEGER NOT NULL DEFAULT 0")
            if "watch_batch" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN watch_batch INTEGER NOT NULL DEFAULT 0")
            self._migrate_round(db)
            self._restore_video_results(db)
            if not self.get(db, "detection_totals_initialized"):
                db.execute("""INSERT INTO node_detection_totals
                    SELECT node,max(videos,counted),counted,matched FROM (
                        SELECT node,sum(videos) AS videos,sum(counted) AS counted,sum(matched) AS matched FROM (
                            SELECT node,videos,0 AS counted,0 AS matched FROM cluster_node_round
                            UNION ALL SELECT node,0,1,matched FROM cluster_video_results
                        ) GROUP BY node
                    )""")
                self.put(db, "detection_totals_initialized", True)

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

    def _migrate_round(self, db):
        session = self.get(db, "session") or {}
        if session.get("role") != "host" or self.get(db, "two_stage"):
            return
        round_id = self.get(db, "round", 0)
        # Old video jobs only produced references. Reopen parents of unfinished
        # checks so the new video worker can complete that work within its lease.
        db.execute("""
            WITH RECURSIVE affected(kind,key) AS (
                SELECT kind,key FROM jobs WHERE round=? AND
                    kind NOT IN ('search','video') AND state IN ('pending','leased')
                UNION
                SELECT parent.kind,parent.key FROM affected a
                JOIN sightings s ON s.round=? AND s.kind=a.kind AND s.key=a.key
                JOIN jobs parent ON parent.id=s.job
            )
            UPDATE jobs SET state='pending',due=0,attempts=0
            WHERE round=? AND kind='video' AND state='done'
              AND key IN (SELECT key FROM affected WHERE kind='video')
        """, (round_id, round_id, round_id))
        # A worker that already held a video lease may still be running the old checker.
        db.execute("""UPDATE jobs SET state='pending',node=NULL,lease=NULL,until=0,due=0,
                       attempts=max(0,attempts-1)
                      WHERE round=? AND kind='video' AND state='leased'""", (round_id,))
        db.execute("""UPDATE jobs SET state='skipped',node=NULL,lease=NULL,until=0
                      WHERE round=? AND kind NOT IN ('search','video') AND state IN ('pending','leased')""",
                   (round_id,))
        self.put(db, "two_stage", True)
        if not self.get(db, "next_round", 0):
            self.put(db, "phase", "collecting")

    def _restore_video_results(self, db):
        session = self.get(db, "session") or {}
        if session.get("role") != "host" or self.get(db, "video_results_backfilled"):
            return
        round_id = self.get(db, "round", 0)
        start = db.execute("SELECT max(id) FROM cluster_events WHERE message=?",
                           (f"第 {round_id} 轮开始，目标 {TARGET} 个不同视频",)).fetchone()[0]
        # Some hosts enabled event logging after round one had already started.
        # Since a new cluster clears its event table, all its events belong to round one.
        if start or round_id == 1:
            for event in db.execute("""SELECT node,message FROM cluster_events
                                       WHERE id>? AND node IS NOT NULL AND level='info' ORDER BY id DESC""",
                                    (start or 0,)):
                match = DETECTION_EVENT.fullmatch(event["message"])
                if match:
                    db.execute("""INSERT OR IGNORE INTO cluster_video_results
                                  SELECT id,round,?,? FROM jobs
                                  WHERE round=? AND kind='video' AND key=? AND state='done'""",
                               (event["node"], int(int(match[3] or 0) > 0), round_id, match[1]))
        self.put(db, "video_results_backfilled", True)

    @staticmethod
    def _event(db, at, message, *, node=None, name=None, level="info"):
        if node and name is None:
            row = db.execute("SELECT name FROM nodes WHERE id=?", (node,)).fetchone()
            name = row["name"] if row else None
        insert = db.execute("INSERT INTO cluster_events(at,level,node,node_name,message) VALUES(?,?,?,?,?)",
                            (at, level, node, name, message))
        db.execute("DELETE FROM cluster_events WHERE id<=?", (insert.lastrowid - EVENT_RETENTION,))

    @staticmethod
    def _job_label(job):
        if job["kind"] == "search":
            params = json.loads(job["params"])
            order = "发布时间" if params["order"] == "pubdate" else "综合排序"
            return f"检索「{params['keyword']}」第 {params['page']} 页（{order}）"
        return f"{JOB_LABEL[job['kind']]} {job['key']}"

    def start_host(self, name, address, *, video_history=False):
        with self.tx() as db:
            if self.get(db, "session"):
                raise ClusterError("请先退出当前集群")
            for table in ("nodes", "catalog", "jobs", "checked_videos", "sightings", "analyses",
                          "cluster_events", "cluster_node_round", "cluster_video_results", "node_history",
                          "node_video_history", "node_detection_totals", "node_local_work", "watch_sources", "watch_checks"):
                db.execute(f"DELETE FROM {table}")
            session = {"role": "host", "name": name, "address": address, "id": uuid.uuid4().hex,
                       "node": uuid.uuid4().hex, "token": secrets.token_hex(32)}
            db.execute("DELETE FROM settings")
            self.put(db, "session", session)
            self.put(db, "invite", secrets.token_hex(32))
            self._register(db, Register(name=name, node=session["node"], token=session["token"]), int(time.time()))
            if video_history:
                db.execute("INSERT INTO node_history VALUES(?,0)", (session["node"],))
            self.put(db, "round", 0)
            self.put(db, "phase", "collecting")
            self.put(db, "two_stage", True)
            self.put(db, "video_results_backfilled", True)
            self.put(db, "detection_totals_initialized", True)
            self._event(db, int(time.time()), "主节点已发起工作集群", name=name)
            return session

    def register(self, body: Register, invite: str, *, video_history=False):
        now = int(time.time())
        with self.tx() as db:
            session = self.get(db, "session", {})
            if (session.get("role") != "host" or not secrets.compare_digest(invite, self.get(db, "invite", ""))):
                raise ClusterError("集群未开启或邀请码无效", 401)
            if body.node == session["node"]:
                raise ClusterError("不能加入本机发起的集群")
            if self._register(db, body, now):
                self._event(db, now, "节点已加入集群", node=body.node)
            if video_history:
                db.execute("INSERT OR IGNORE INTO node_history VALUES(?,0)", (body.node,))
            return {"cluster_id": session["id"], "protocol": 1, "video_history": True}

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
        return existing is None

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
            self._event(db, int(time.time()), "节点已退出集群", node=node)
            db.execute("DELETE FROM nodes WHERE id=?", (node,))
            db.execute("DELETE FROM node_history WHERE node=?", (node,))
            db.execute("DELETE FROM node_video_history WHERE node=?", (node,))
            db.execute("DELETE FROM node_local_work WHERE node=?", (node,))
            db.execute("DELETE FROM watch_sources WHERE node=?", (node,))
            db.execute("UPDATE jobs SET until=0 WHERE node=? AND state='leased'", (node,))
            db.execute("""UPDATE jobs SET params=json_remove(params,'$.assigned') WHERE kind='video'
                          AND state IN ('pending','leased') AND json_extract(params,'$.assigned')=?""", (node,))
            for setting, fields in (("dispatch", ("search_nodes", "video_nodes")), ("watch_batch", ("nodes",))):
                current = self.get(db, setting)
                if current:
                    for field in fields:
                        if current.get(field) and node in current[field]:
                            current[field] = [selected for selected in current[field] if selected != node] or None
                    self.put(db, setting, current)
            db.execute("""UPDATE jobs SET state='skipped' WHERE kind='watch' AND state='pending' AND
                          json_extract(params,'$.batch') IS NULL AND
                          (json_extract(params,'$.assigned')=? OR key NOT IN (SELECT mid FROM watch_sources))""", (node,))

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
            if paused != self.get(db, "paused", False):
                self.put(db, "paused", paused)
                self._event(db, int(time.time()), "暂停领取新任务" if paused else "恢复领取任务")

    def _queue(self, db, round_id, kind, key, **params):
        db.execute("INSERT OR IGNORE INTO jobs(round,kind,key,params) VALUES(?,?,?,?)",
                   (round_id, kind, key, _dump(params)))

    @staticmethod
    def _upcoming_keywords(current):
        if current not in KEYWORDS:
            return KEYWORDS
        index = KEYWORDS.index(current)
        return KEYWORDS[index + 1:] + KEYWORDS[:index]

    def _next_round(self, db, now):
        round_id = self.get(db, "round", 0) + 1
        keyword = self._upcoming_keywords(self.get(db, "round_keyword"))[0]
        self.put(db, "round", round_id)
        self.put(db, "round_keyword", keyword)
        self.put(db, "round_search", {"round": round_id, "node": None})
        self.put(db, "phase", "collecting")
        self.put(db, "next_round", 0)
        self._event(db, now, f"第 {round_id} 轮开始，目标 {TARGET} 个不同视频")
        self._queue(db, round_id, "search", f"pubdate:{keyword}:1", keyword=keyword, order="pubdate", page=1)
        db.execute("DELETE FROM cluster_node_round WHERE round<?", (round_id - 2,))
        db.execute("DELETE FROM cluster_video_results WHERE round<?", (round_id - 2,))
        db.execute("DELETE FROM sightings WHERE round>0 AND round<?", (round_id - 2,))
        # Keep a cumulative page count when old rounds are pruned. Older installations
        # can recover only the rounds whose job rows still exist at upgrade time.
        cutoff = round_id - 2
        archived = db.execute(
            "SELECT count(*) FROM jobs WHERE round<? AND kind='search' AND state='done'",
            (cutoff,),
        ).fetchone()[0]
        if archived:
            self.put(db, "search_pages_archived", self.get(db, "search_pages_archived", 0) + archived)
        first = db.execute("SELECT min(round) FROM jobs WHERE round>0").fetchone()[0]
        if first and not self.get(db, "search_history_start"):
            self.put(db, "search_history_start", first)
        db.execute("DELETE FROM jobs WHERE round>0 AND round<?", (cutoff,))
        db.execute("DELETE FROM catalog WHERE expires<?", (now - 7 * 86400,))
        db.execute("DELETE FROM analyses WHERE until<=?", (now,))

    def _catalog(self, db, activity: Activity, rank: int, now: int, origin: str):
        old = db.execute("SELECT payload,rank,origin FROM catalog WHERE id=?", (activity.dynamic_id,)).fetchone()
        if old:
            previous = Activity.model_validate_json(old["payload"])
            other_source = rank == 1 and old["origin"] != origin
            improves_time = previous.lottery_time is None and activity.lottery_time is not None and not previous.closed
            if old["rank"] > rank or (other_source and (activity.closed or not improves_time)):
                return
            if activity.lottery_time is None and previous.lottery_time is not None:
                if not activity.closed:
                    return
                activity = activity.model_copy(update={"lottery_time": previous.lottery_time})
        elif activity.closed:
            return
        expires = activity.expires_at
        if expires <= now and not activity.closed:
            return
        payload = _dump(activity.model_dump())
        if old:
            if old["payload"] == payload:
                db.execute("UPDATE catalog SET rank=?, checked=? WHERE id=?",
                           (rank, now, activity.dynamic_id))
                return
        seq = self.get(db, "sequence", 0) + 1
        self.put(db, "sequence", seq)
        db.execute("""INSERT INTO catalog VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                      seq=excluded.seq,payload=excluded.payload,rank=excluded.rank,expires=excluded.expires,
                      origin=excluded.origin,checked=excluded.checked""",
                   (activity.dynamic_id, seq, payload, rank, now if activity.closed else expires, origin, now))

    def _retry(self, db, job, now, *, delay, reason):
        if job["kind"] == "video" and db.execute("SELECT 1 FROM checked_videos WHERE id=?", (job["key"],)).fetchone():
            db.execute("UPDATE jobs SET state='skipped',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
            return
        if job["kind"] == "watch":
            assigned = json.loads(job["params"]).get("assigned")
            if assigned and not db.execute("""SELECT 1 FROM watch_sources
                WHERE mid=? AND EXISTS (SELECT 1 FROM nodes WHERE id=?)""", (job["key"], assigned)).fetchone():
                db.execute("UPDATE jobs SET state='skipped',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
                return
        failed = job["attempts"] >= MAX_ATTEMPTS
        db.execute("UPDATE jobs SET state=?,node=NULL,lease=NULL,until=0,due=? WHERE id=?",
                   ("failed" if failed else "pending", now + delay, job["id"]))
        result = "已达到重试上限" if failed else f"{delay} 秒后可重试" if delay else "已重新排队"
        self._event(db, now, f"{self._job_label(job)}：{reason}，{result}",
                    node=job["node"], level="error" if failed else "warning")

    def _publish_findings(self, db, job, node, activities, now):
        for item in activities:
            self._catalog(db, item, 2, now, node)
            if not item.closed and item.expires_at > now:
                db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,'open_activity',?)",
                           (job["round"], job["id"], item.dynamic_id))
            else:
                db.execute("DELETE FROM sightings WHERE round=? AND job=? AND kind='open_activity' AND key=?",
                           (job["round"], job["id"], item.dynamic_id))

    def publish_watch_findings(self, body: WatchFindings, token):
        now = int(time.time())
        with self.tx() as db:
            self._authenticate(db, body.node, token)
            job = db.execute("""SELECT * FROM jobs WHERE id=? AND node=? AND lease=?
                                AND state='leased' AND until>? AND kind='watch'""",
                             (body.task_id, body.node, body.lease, now)).fetchone()
            if not job:
                raise ClusterError("追踪任务已交回")
            self._publish_findings(db, job, body.node, body.activities, now)
        return {"ok": True}

    def _finish(self, db, node, report: Report, now):
        job = db.execute("SELECT * FROM jobs WHERE id=? AND node=? AND lease=? AND state='leased' AND until>?",
                         (report.task_id, node, report.lease, now)).fetchone()
        if not job:
            return
        db.execute("UPDATE nodes SET model_calls=model_calls+? WHERE id=?", (report.model_calls, node))
        db.execute("DELETE FROM analyses WHERE lease=? AND fields IS NULL", (report.lease,))
        kind = job["kind"]
        activities = {item.dynamic_id: item for item in report.activities} if kind in {"video", "watch"} else {}
        self._publish_findings(db, job, node, activities.values(), now)
        if report.outcome == "cooldown":
            until = max(now + report.cooldown, self.get(db, "cooldown_until", 0))
            self.put(db, "cooldown_until", until)
            db.execute("UPDATE jobs SET attempts=attempts-1 WHERE id=?", (job["id"],))
            db.execute("""UPDATE jobs SET state=CASE WHEN kind='video' AND key IN (SELECT id FROM checked_videos)
                          THEN 'skipped' ELSE 'pending' END,node=NULL,lease=NULL,until=0,due=? WHERE id=?""",
                       (until, job["id"]))
            self._event(db, now, f"{self._job_label(job)}：平台限流，集群暂停领取任务 {until - now} 秒",
                        node=node, level="warning")
            return
        if report.outcome == "retry":
            self._retry(db, job, now, delay=30 * 2 ** (job["attempts"] - 1),
                        reason=RETRY_LABEL.get(report.reason, "任务未能完成"))
            return
        if kind == "watch":
            params = json.loads(job["params"])
            if params.get("legacy_batch_check") and collection_source_for_mid(job["key"]):
                params.update(legacy_batch_check=False, collection_pending=True)
                db.execute("""UPDATE jobs SET params=?,state='pending',node=NULL,lease=NULL,until=0,due=0,attempts=0
                              WHERE id=?""", (_dump(params), job["id"]))
                db.execute("UPDATE nodes SET completed=completed+1 WHERE id=?", (node,))
                self._event(db, now, "已完成动态检查，等待新版节点补查合集", node=node)
                return
        round_id = job["round"]
        db.execute("UPDATE jobs SET state='done',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
        db.execute("UPDATE nodes SET completed=completed+1 WHERE id=?", (node,))
        if kind == "video":
            db.execute("INSERT OR IGNORE INTO checked_videos VALUES(?,?)", (job["key"], now))
            matched = db.execute("SELECT EXISTS(SELECT 1 FROM sightings WHERE round=? AND job=? AND kind='open_activity')",
                                 (round_id, job["id"])).fetchone()[0]
            db.execute("INSERT INTO cluster_video_results VALUES(?,?,?,?)", (job["id"], round_id, node, matched))
            db.execute("""INSERT INTO node_detection_totals VALUES(?,1,1,?) ON CONFLICT(node) DO UPDATE SET
                          videos=videos+1,counted=counted+1,matched=node_detection_totals.matched+excluded.matched""",
                       (node, matched))
        open_activities = db.execute("SELECT count(*) FROM sightings WHERE round=? AND job=? AND kind='open_activity'",
                                     (round_id, job["id"])).fetchone()[0]
        summary = f"核验 {open_activities} 项开放活动" if open_activities else "未确认开放活动"
        if kind == "watch":
            db.execute("""INSERT INTO watch_checks VALUES(?,?,?) ON CONFLICT(mid) DO UPDATE SET
                          checked=excluded.checked,hits=excluded.hits""", (job["key"], now, open_activities))
            self._event(db, now, f"{self._job_label(job)}完成：{summary}", node=node)
            return
        new_videos = 0
        if kind == "search":
            params = json.loads(job["params"])
            before = self._video_count(db, round_id)
            self._add_videos(db, job, report)
            count = self._video_count(db, round_id)
            new_videos = count - before
            summary = f"返回 {len(report.references)} 个视频，去重后收录 {new_videos} 个"
            previous = db.execute(
                "SELECT id FROM jobs WHERE round=? AND kind='search' AND key=?",
                (round_id, f"{params['order']}:{params['keyword']}:{params['page'] - 1}"),
            ).fetchone()
            repeated = previous and not db.execute("""SELECT key FROM sightings WHERE job=? AND kind='video'
                EXCEPT SELECT key FROM sightings WHERE job=? AND kind='video'""", (job["id"], previous["id"])).fetchone()
            if report.more_pages and not repeated and params["page"] < SEARCH_PAGE_LIMIT:
                params["page"] += 1
                next_key = f"{params['order']}:{params['keyword']}:{params['page']}"
                self._queue(db, round_id, "search", next_key, **params)
        db.execute("""INSERT INTO cluster_node_round VALUES(?,?,?,?,?,?)
                      ON CONFLICT(round,node) DO UPDATE SET pages=pages+excluded.pages,
                      found=found+excluded.found,videos=videos+excluded.videos,
                      activities=activities+excluded.activities""",
                   (round_id, node, int(kind == "search"), new_videos, int(kind == "video"), open_activities))
        if report.model_calls:
            summary += f"，调用模型 {report.model_calls} 次"
        self._event(db, now, f"{self._job_label(job)}完成：{summary}", node=node)

    @staticmethod
    def _video_count(db, round_id):
        return db.execute("SELECT count(*) FROM jobs WHERE round=? AND kind='video' AND state<>'skipped'",
                          (round_id,)).fetchone()[0]

    def _add_videos(self, db, job, report):
        round_id = job["round"]
        remaining = TARGET - self._video_count(db, round_id)
        for ref in report.references:
            if ref.kind != "video":
                continue
            db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,?,?)", (round_id, job["id"], ref.kind, ref.key))
            if remaining <= 0 or db.execute("SELECT 1 FROM checked_videos WHERE id=?", (ref.key,)).fetchone():
                continue
            if db.execute("INSERT OR IGNORE INTO jobs(round,kind,key,params) VALUES(?,'video',?,'{}')",
                          (round_id, ref.key)).rowcount:
                remaining -= 1
                db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,'collected',?)", (round_id, job["id"], ref.key))

    def _merge_history(self, db, node, batch, now):
        previous = db.execute("SELECT complete FROM node_history WHERE node=?", (node,)).fetchone()
        db.execute("""INSERT INTO node_history VALUES(?,?) ON CONFLICT(node) DO UPDATE SET
                      complete=max(complete,excluded.complete)""", (node, batch.complete))
        db.executemany("INSERT OR IGNORE INTO node_video_history VALUES(?,?)", ((node, key) for key in batch.ids))
        db.executemany("INSERT OR IGNORE INTO checked_videos VALUES(?,?)", ((key, now) for key in batch.ids))
        if batch.ids:
            round_id = self.get(db, "round", 0)
            skipped = db.execute("""UPDATE jobs SET state='skipped' WHERE round=? AND kind='video' AND state='pending'
                                    AND key IN (SELECT id FROM checked_videos)""", (round_id,)).rowcount
            if skipped and self.get(db, "phase") == "collecting" and self._video_count(db, round_id) < TARGET:
                db.execute("""UPDATE jobs SET state='pending'
                              WHERE round=? AND kind='search' AND state='skipped'""", (round_id,))
        if batch.complete and (not previous or not previous[0]):
            total = db.execute("SELECT count(*) FROM node_video_history WHERE node=?", (node,)).fetchone()[0]
            self._event(db, now, f"本机历史 BV 已同步，共 {total} 个", node=node)

    @staticmethod
    def _history_syncing(db, now):
        return db.execute("""SELECT count(*) FROM node_history h JOIN nodes n ON n.id=h.node
                             WHERE h.complete=0 AND n.seen>?""", (now - ONLINE_SECONDS,)).fetchone()[0]

    def _begin_import(self, db, round_id, now, session):
        nodes = [row[0] for row in db.execute("""SELECT n.id FROM nodes n JOIN node_history h ON h.node=n.id
            WHERE n.seen>? AND h.complete=1 ORDER BY n.id<>?, n.name, n.id""", (now - ONLINE_SECONDS, session["node"]))]
        self.put(db, "phase", "importing")
        self.put(db, "next_round", 0)
        self.put(db, "round_import", {"round": round_id, "pending": nodes,
                                     "done": 0, "skipped": 0, "deadline": now + IMPORT_TIMEOUT})
        if nodes:
            self._event(db, now, f"开始轮间活动导入，共 {len(nodes)} 个节点")

    def _advance_import(self, db, body, now, *, advance_round):
        state = self.get(db, "round_import")
        pending = state["pending"]
        latest = db.execute("SELECT coalesce(max(seq),0) FROM catalog").fetchone()[0]
        while pending:
            node = pending[0]
            seen = db.execute("SELECT seen FROM nodes WHERE id=?", (node,)).fetchone()
            acknowledged = body.node == node and body.import_round == state["round"]
            if acknowledged and state.get("participating") == node and body.round_participation == "running":
                state["deadline"] = now + IMPORT_TIMEOUT
            imported = acknowledged and body.after >= latest
            if imported:
                outcome = body.round_participation
                if outcome in {"ready", "running"}:
                    if not advance_round and state.get("participating") != node:
                        break
                    if state.get("participating") != node:
                        state["participating"] = node
                        state["deadline"] = now + IMPORT_TIMEOUT
                        self._event(db, now, "已导入集群活动，安排轮间顺序参与", node=node)
                    if state["deadline"] > now:
                        break
                    imported = False
                if outcome in {"done", "failed"} and state.get("participating") == node:
                    self._event(db, now, "轮间顺序参与完成" if outcome == "done" else "轮间顺序参与未全部完成",
                                node=node, level="info" if outcome == "done" else "warning")
            if imported:
                state["done"] += 1
                self._event(db, now, "轮间活动导入完成", node=node)
            elif not seen or seen[0] <= now - ONLINE_SECONDS or state["deadline"] <= now:
                state["skipped"] += 1
                self._event(db, now, "节点离线或超时，轮间导入已跳过，将随恢复连接继续同步",
                            node=node, level="warning")
            else:
                break
            pending.pop(0)
            state.pop("participating", None)
            state["deadline"] = now + IMPORT_TIMEOUT
        self.put(db, "round_import", state)
        if not pending and advance_round:
            self._next_round(db, now)

    def _tick(self, db, now, body, session):
        for job in db.execute("SELECT * FROM jobs WHERE state='leased' AND until<=?", (now,)).fetchall():
            self._retry(db, job, now, delay=0, reason="节点未在租约内交回任务")
        can_advance = not (self.get(db, "paused", False) or self.get(db, "cooldown_until", 0) > now
                           or self._history_syncing(db, now))
        if self.get(db, "phase") == "importing":
            self._advance_import(db, body, now, advance_round=can_advance)
            return
        if not can_advance:
            return
        round_id = self.get(db, "round", 0)
        if not round_id:
            self._next_round(db, now)
            return
        if self.get(db, "next_round", 0):  # Upgrade a host that was waiting on the old timer.
            self._begin_import(db, round_id, now, session)
            self._advance_import(db, body, now, advance_round=True)
            return
        found = self._video_count(db, round_id)
        if self.get(db, "phase") == "collecting":
            if found >= TARGET:
                db.execute("""UPDATE jobs SET state='skipped'
                              WHERE round=? AND kind='search' AND state='pending'""", (round_id,))
            if self._has_work(db, round_id, "search"):
                return
            self.put(db, "phase", "detecting")
            if found:
                reason = "达到收录目标" if found >= TARGET else "本轮检索结束"
                self._event(db, now, f"{reason}，开始分配 {found} 个已收录视频的检测任务")
        if self._has_work(db, round_id, "video"):
            return
        failed = db.execute("""SELECT count(*) FROM jobs WHERE round=? AND state='failed'
                               AND kind IN ('search','video')""", (round_id,)).fetchone()[0]
        self._event(db, now, f"第 {round_id} 轮结束，收录 {found}/{TARGET} 个视频，失败 {failed} 项",
                    level="warning" if failed else "info")
        self._begin_import(db, round_id, now, session)
        self._advance_import(db, body, now, advance_round=True)

    @staticmethod
    def _has_work(db, round_id, kind):
        return db.execute("""SELECT 1 FROM jobs WHERE round=? AND kind=?
                             AND state IN ('pending','leased') LIMIT 1""", (round_id, kind)).fetchone() is not None

    def _effective_pool(self, db, nodes, fallback, now, *, video=False):
        if nodes is None or not fallback:
            return nodes
        available = db.execute("""SELECT id FROM nodes WHERE seen>? AND (?=0 OR work=2)""",
                               (now - ONLINE_SECONDS, video))
        return nodes if any(row[0] in nodes for row in available) else None

    def _search_turn(self, db, job, node, now, pool):
        if pool is not None and node not in pool:
            return False
        search = self.get(db, "round_search", {})
        if search.get("round") != job["round"]:  # Allow an in-flight round from the previous scheduler to finish.
            return True
        owner = search["node"]
        if owner and (pool is None or owner in pool) and db.execute("SELECT 1 FROM nodes WHERE id=? AND seen>?",
                                (owner, now - ONLINE_SECONDS)).fetchone():
            return node == owner
        # Rotate in registration order and wrap, skipping nodes that cannot take work.
        cursor = self.get(db, "search_rotation_cursor", 0)
        candidate = db.execute("""SELECT n.id,n.rowid FROM nodes n
            WHERE n.seen>? AND n.search_ready=1
              AND (? OR n.id IN (SELECT value FROM json_each(?))) AND NOT EXISTS (
                SELECT 1 FROM jobs WHERE node=n.id AND state='leased'
            ) ORDER BY n.rowid<=?,n.rowid LIMIT 1""", (now - ONLINE_SECONDS, pool is None, _dump(pool), cursor)).fetchone()
        if not candidate or candidate["id"] != node:
            return False
        self.put(db, "round_search", {"round": job["round"], "node": node})
        self.put(db, "search_rotation_cursor", candidate["rowid"])
        if owner:
            self._event(db, now, "接续本轮关键词检索", node=node)
        return True

    def _watch_job(self, db, node, now):
        batch = self.get(db, "watch_batch") or {}
        pool = batch.get("nodes")
        modern = bool(db.execute("SELECT watch_batch FROM nodes WHERE id=?", (node,)).fetchone()[0])
        in_pool = pool is None or node in pool
        # A legacy worker can check dynamics. Collection follow-ups prefer selected
        # modern nodes, and otherwise use any online modern node with a ready model.
        preferred_modern = modern and (in_pool or not db.execute("""SELECT 1 FROM nodes
            WHERE watch_batch=1 AND work=2 AND seen>? AND id IN (SELECT value FROM json_each(?))""",
            (now - ONLINE_SECONDS, _dump(pool))).fetchone())
        return db.execute("""SELECT * FROM jobs WHERE round=0 AND kind='watch' AND state='pending' AND due<=?
            AND ((json_extract(params,'$.assigned')=? AND key IN (SELECT mid FROM watch_sources))
                 OR (json_extract(params,'$.batch')=? AND
                     CASE WHEN json_extract(params,'$.collection_pending') THEN ? ELSE ? END))
            ORDER BY coalesce(json_extract(params,'$.collection_pending'),0),id LIMIT 1""",
            (now, node, batch.get("id"), preferred_modern, in_pool)).fetchone(), modern

    def _claim(self, db, node, now, *, video_detection, watch_ready=False):
        phase = self.get(db, "phase")
        kind = {"collecting": "search", "detecting": "video"}.get(phase)
        dispatch = self.get(db, "dispatch", {})
        if (self.get(db, "paused", False) or self.get(db, "cooldown_until", 0) > now or self._history_syncing(db, now)
                or db.execute("SELECT 1 FROM jobs WHERE node=? AND state='leased'", (node,)).fetchone()
                or db.execute("SELECT count(*) FROM jobs WHERE state='leased'").fetchone()[0] >= dispatch.get("concurrency", MAX_CONCURRENT)):
            return None
        job = None
        if watch_ready and video_detection and phase != "importing":
            job, modern = self._watch_job(db, node, now)
        if job is None and kind and (kind != "video" or video_detection):
            pool = self._effective_pool(db, dispatch.get(f"{kind}_nodes"), dispatch.get("fallback"),
                                        now, video=kind == "video")
            job = db.execute("""SELECT * FROM jobs WHERE round=? AND kind=? AND state='pending' AND due<=?
                AND (json_extract(params,'$.assigned')=? OR
                     (json_extract(params,'$.assigned') IS NULL AND ?))
                ORDER BY json_extract(params,'$.assigned') IS NULL,id LIMIT 1""",
                (self.get(db, "round", 0), kind, now, node, pool is None or node in pool)).fetchone()
            if job and kind == "search" and not self._search_turn(db, job, node, now, pool):
                return None
        if job is None:
            return None
        params = json.loads(job["params"])
        if job["kind"] == "watch" and params.get("batch"):
            params["legacy_batch_check"] = not modern
            db.execute("UPDATE jobs SET params=? WHERE id=?", (_dump(params), job["id"]))
        lease = uuid.uuid4().hex
        db.execute("UPDATE jobs SET state='leased',node=?,lease=?,until=?,attempts=attempts+1 WHERE id=?",
                   (node, lease, now + LEASE_SECONDS, job["id"]))
        self._event(db, now, f"开始{self._job_label(job)}（第 {job['attempts'] + 1} 次）", node=node)
        params = params if job["kind"] == "search" else {}
        return Task(task_id=job["id"], lease=lease, kind=job["kind"], key=job["key"], **params)

    def _require_dispatch_nodes(self, db, nodes):
        known = {row[0] for row in db.execute("SELECT id FROM nodes")}
        if not set(nodes) <= known:
            raise ClusterError("有节点已退出集群，请重新选择")

    def set_dispatch(self, body: DispatchSettings):
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以设置任务指派")
            self._require_dispatch_nodes(db, (body.search_nodes or []) + (body.video_nodes or []))
            self.put(db, "dispatch", body.model_dump())
            self._event(db, int(time.time()), f"已更新检索和视频指派，并行上限 {body.concurrency}")

    def assign_video(self, body: VideoAssignment):
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以指派视频检测")
            if body.node:
                self._require_dispatch_nodes(db, [body.node])
            updated = db.execute("""UPDATE jobs SET params=? WHERE round=? AND kind='video' AND key=? AND state='pending'""",
                                 (_dump({"assigned": body.node}) if body.node else "{}",
                                  self.get(db, "round", 0), body.bvid))
            if not updated.rowcount:
                raise ClusterError("仅可改派本轮待检测的视频")
            self._event(db, int(time.time()), f"视频 {body.bvid} 已指定检测节点" if body.node
                        else f"视频 {body.bvid} 已恢复自动分配", node=body.node)

    def assign_watch(self, body: WatchAssignment):
        now = int(time.time())
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以指派追踪检查")
            if not db.execute("SELECT 1 FROM watch_sources WHERE mid=?", (body.mid,)).fetchone():
                raise ClusterError("这个 UP 主已不在集群追踪列表")
            target = db.execute("""SELECT n.name FROM nodes n JOIN node_local_work w ON w.node=n.id
                                 WHERE n.id=? AND n.seen>? AND n.work=2""", (body.node, now - ONLINE_SECONDS)).fetchone()
            if not target:
                raise ClusterError("选择在线、支持追踪检查且模型已就绪的节点")
            existing = db.execute("SELECT state,params FROM jobs WHERE round=0 AND kind='watch' AND key=?", (body.mid,)).fetchone()
            if existing and "batch" in json.loads(existing["params"]):
                raise ClusterError("请在整批追踪中指派这个 UP 主")
            if existing and existing["state"] in {"pending", "leased"}:
                if json.loads(existing["params"])["assigned"] == body.node:
                    return
                if existing["state"] == "leased":
                    raise ClusterError("该 UP 主正在检查中")
            db.execute("""INSERT INTO jobs(round,kind,key,params) VALUES(0,'watch',?,?)
                ON CONFLICT(round,kind,key) DO UPDATE SET params=excluded.params,state='pending',
                node=NULL,lease=NULL,until=0,due=0,attempts=0""", (body.mid, _dump({"assigned": body.node})))
            self._event(db, now, f"追踪 UP 主 {body.mid} 已指派给 {target['name']}")

    def assign_watch_batch(self, body: WatchBatch):
        now = int(time.time())
        nodes = list(dict.fromkeys(body.nodes)) if body.nodes is not None else None
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以指派追踪检查")
            self._require_dispatch_nodes(db, nodes or [])
            workers = "所有可用节点" if nodes is None else f"{len(nodes)} 个节点"
            current = self.get(db, "watch_batch") or {}
            if db.execute("""SELECT 1 FROM jobs WHERE kind='watch' AND state IN ('pending','leased')
                             AND json_extract(params,'$.batch')=?""", (current.get("id"),)).fetchone():
                self.put(db, "watch_batch", {**current, "nodes": nodes})
                self._event(db, now, f"整批追踪剩余任务改由{workers}领取")
                return
            if self._has_work(db, 0, "watch"):
                raise ClusterError("请等待已指派的单项追踪检查结束")
            mids = [row[0] for row in db.execute("SELECT DISTINCT mid FROM watch_sources ORDER BY mid")]
            if not mids:
                raise ClusterError("集群尚无可追踪的 UP 主")
            batch = uuid.uuid4().hex
            db.execute("DELETE FROM sightings WHERE round=0")
            db.execute("DELETE FROM jobs WHERE round=0 AND kind='watch'")
            db.executemany("INSERT INTO jobs(round,kind,key,params) VALUES(0,'watch',?,?)",
                           ((mid, _dump({"batch": batch})) for mid in mids))
            self.put(db, "watch_batch", {"id": batch, "nodes": nodes})
            self._event(db, now, f"已指派{workers}追踪全部 {len(mids)} 位 UP 主")

    @staticmethod
    def _sync_watch_list(db, node, watch_list):
        db.executemany("""INSERT INTO watch_sources VALUES(?,?,?,?) ON CONFLICT(node,mid) DO UPDATE SET
                          name=excluded.name,revision=excluded.revision""",
                       ((node, user.mid, user.name, watch_list.revision) for user in watch_list.users))
        if watch_list.complete:
            db.execute("DELETE FROM watch_sources WHERE node=? AND revision<>?", (node, watch_list.revision))
            db.execute("""UPDATE jobs SET state='skipped' WHERE kind='watch' AND state='pending'
                          AND json_extract(params,'$.batch') IS NULL
                          AND key NOT IN (SELECT mid FROM watch_sources)""")

    def exchange(self, body: Exchange, token: str) -> Feed:
        now = int(time.time())
        with self.tx() as db:
            session = self._authenticate(db, body.node, token)
            readiness = 2 if body.video_detection else int(body.work)
            ready_to_claim = body.work and not body.working and body.local_work in {None, "idle"}
            db.execute("UPDATE nodes SET seen=?,work=?,search_ready=?,watch_batch=? WHERE id=?",
                       (now, readiness, ready_to_claim, bool(body.watch_batch_capable), body.node))
            if body.local_work is None:
                db.execute("DELETE FROM node_local_work WHERE node=?", (body.node,))
            else:
                db.execute("INSERT OR REPLACE INTO node_local_work VALUES(?,?)", (body.node, body.local_work))
            if body.watch_list is not None:
                self._sync_watch_list(db, body.node, body.watch_list)
            sequence = self.get(db, "sequence", 0)
            for item in body.activities:
                if body.unknown_activity_time or not item.closed:
                    self._catalog(db, item, 1, now, body.node)
            updated = self.get(db, "sequence", 0) - sequence
            if updated:
                self._event(db, now, f"上传活动后，公共目录新增或更新 {updated} 条", node=body.node)
            if body.video_history is not None:
                self._merge_history(db, body.node, body.video_history, now)
            if body.working:
                self._renew(db, body.node, body.working, now)
            if body.report:
                self._finish(db, body.node, body.report, now)
            self._tick(db, now, body, session)
            task = None
            if ready_to_claim:
                task = self._claim(db, body.node, now, video_detection=body.video_detection,
                                   watch_ready=body.local_work == "idle")
            pending = self.get(db, "round_import", {}).get("pending", [])
            waiting_turn = self.get(db, "phase") == "importing" and pending and pending[0] != body.node
            rows = [] if waiting_turn else db.execute("""SELECT seq,payload FROM catalog WHERE seq>?
                                  AND (? OR json_extract(payload,'$.lottery_time') IS NOT NULL)
                                  ORDER BY seq LIMIT ?""", (body.after, body.unknown_activity_time, PAGE_SIZE))
            changes = [Change(seq=row["seq"], activity=Activity.model_validate_json(row["payload"])) for row in rows]
            return Feed(task=task, changes=changes, status=self._status(db, session, now, work_node=body.node))

    @staticmethod
    def _renew(db, node, lease: Lease, now):
        db.execute("""UPDATE jobs SET until=? WHERE id=? AND node=? AND lease=?
                      AND state='leased' AND until>?""",
                   (now + LEASE_SECONDS, lease.task_id, node, lease.lease, now))

    def status(self):
        with self.tx(write=False) as db:
            db.execute("BEGIN")
            session = self.get(db, "session") or {}
            return self._status(db, session, int(time.time()), work_node=session.get("node"))

    def detected_activities(self, page: int, *, node: str | None = None, token: str = ""):
        now = int(time.time())
        with self.tx(write=False) as db:
            db.execute("BEGIN")
            if node is not None:
                self._authenticate(db, node, token)
            elif (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("主节点尚未开启集群")
            total = db.execute("SELECT count(*) FROM catalog WHERE rank=2 AND expires>?", (now,)).fetchone()[0]
            pages = max(1, (total + DETECTIONS_PER_PAGE - 1) // DETECTIONS_PER_PAGE)
            page = min(page, pages)
            rows = db.execute("""SELECT payload FROM catalog WHERE rank=2 AND expires>?
                                 ORDER BY expires,id LIMIT ? OFFSET ?""",
                              (now, DETECTIONS_PER_PAGE, (page - 1) * DETECTIONS_PER_PAGE))
            return {"total": total, "page": page, "pages": pages, "items": [json.loads(row[0]) for row in rows]}

    def tracked_users(self, page: int, *, node: str | None = None, token: str = ""):
        with self.tx(write=False) as db:
            db.execute("BEGIN")
            if node is not None:
                self._authenticate(db, node, token)
            elif (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("主节点尚未开启集群")
            total = db.execute("SELECT count(DISTINCT mid) FROM watch_sources").fetchone()[0]
            pages = max(1, (total + TRACKED_USERS_PER_PAGE - 1) // TRACKED_USERS_PER_PAGE)
            page = min(page, pages)
            rows = db.execute("""WITH users AS (
                SELECT s.mid,min(s.name) AS name,json_group_array(n.name) AS sources
                FROM watch_sources s JOIN nodes n ON n.id=s.node GROUP BY s.mid
                ORDER BY name,s.mid LIMIT ? OFFSET ?
            ) SELECT u.*, j.state, n.name AS assigned, w.checked, w.hits FROM users u
              LEFT JOIN jobs j ON j.round=0 AND j.kind='watch' AND j.key=u.mid
              LEFT JOIN nodes n ON n.id=coalesce(j.node,json_extract(j.params,'$.assigned'))
              LEFT JOIN watch_checks w ON w.mid=u.mid ORDER BY u.name,u.mid""",
                              (TRACKED_USERS_PER_PAGE, (page - 1) * TRACKED_USERS_PER_PAGE))
            return {"total": total, "pages": pages, "page": page,
                    "items": [{**dict(row), "sources": json.loads(row["sources"])} for row in rows]}

    @staticmethod
    def _search_tasks(db, round_id):
        rows = db.execute("""SELECT j.params,j.state,n.name,coalesce(v.found,0) AS found
            FROM jobs j LEFT JOIN nodes n ON n.id=j.node
            LEFT JOIN (SELECT s.job,count(*) AS found FROM sightings s
                       JOIN jobs video ON video.round=s.round AND video.kind='video' AND video.key=s.key
                       WHERE s.round=? AND s.kind='collected' AND video.state<>'skipped' GROUP BY s.job) v ON v.job=j.id
            WHERE j.round=? AND j.kind='search' ORDER BY j.id""", (round_id, round_id))
        queries = {}
        for row in rows:
            params = json.loads(row["params"])
            key = params["keyword"], params["order"]
            query = queries.setdefault(key, {"keyword": key[0], "order": key[1], "pages": 0, "found": 0,
                                             "failed": 0, "skipped": 0, "queued": []})
            query["pages"] += row["state"] == "done"
            query["failed"] += row["state"] == "failed"
            query["skipped"] += row["state"] == "skipped"
            query["found"] += row["found"]
            if row["state"] in {"pending", "leased"}:
                query["queued"].append({"page": params["page"], "state": row["state"], "node": row["name"]})
        return list(queries.values())

    def _status(self, db, session, now, *, work_node):
        round_id = self.get(db, "round", 0)
        keyword = self.get(db, "round_keyword")
        counts = {}
        for row in db.execute("""SELECT kind,state,count(*) AS n FROM jobs WHERE round=?
                                 AND kind IN ('search','video') GROUP BY kind,state""", (round_id,)):
            counts.setdefault(row["kind"], {})[row["state"]] = row["n"]
        search = db.execute("""SELECT min(round),count(CASE WHEN kind='search' AND state='done' THEN 1 END)
                               FROM jobs WHERE round>=?""", (max(1, round_id - 2),)).fetchone()
        nodes = [dict(row) for row in db.execute("""
            SELECT n.id,n.name,n.seen,n.work,n.watch_batch,n.completed,n.model_calls,j.kind,j.key,j.params,
                   h.complete AS history_complete,l.state AS local_work,
                   CASE WHEN j.kind='watch' THEN (SELECT min(name) FROM watch_sources WHERE mid=j.key) END AS watch_name,
                   CASE WHEN h.node IS NOT NULL THEN (SELECT count(*) FROM node_video_history WHERE node=n.id) END AS history_count,
                   coalesce(r.pages,0) AS search_pages,coalesce(r.videos,0) AS videos_checked,
                   coalesce(t.videos,0) AS videos_total,coalesce(t.counted,0) AS videos_counted,
                   coalesce(t.matched,0) AS videos_matched
            FROM nodes n LEFT JOIN jobs j ON n.id=j.node AND j.state='leased' AND j.until>?
            LEFT JOIN node_history h ON h.node=n.id
            LEFT JOIN node_local_work l ON l.node=n.id
            LEFT JOIN cluster_node_round r ON r.round=? AND r.node=n.id
            LEFT JOIN node_detection_totals t ON t.node=n.id
            ORDER BY n.seen DESC,n.name""", (now, round_id))]
        for node in nodes:
            node["online"] = node["seen"] > now - ONLINE_SECONDS
            node["host"] = node["id"] == session.get("node")
            node["task_label"] = self._job_label(node) if node["kind"] else None
            if node["watch_name"]:
                node["task_label"] = f"追踪检查「{node['watch_name']}」"
            del node["watch_name"]
            del node["params"]
        local_round = db.execute("SELECT pages,found,videos,activities FROM cluster_node_round WHERE round=? AND node=?",
                                 (round_id, work_node)).fetchone()
        local_events = [dict(row) for row in db.execute(
            "SELECT id,at,level,node,node_name,message FROM cluster_events WHERE node=? ORDER BY id DESC LIMIT 6",
            (work_node,))]
        events = [dict(row) for row in db.execute(
            "SELECT id,at,level,node,node_name,message FROM cluster_events ORDER BY id DESC LIMIT ?",
            (RECENT_EVENTS,))]
        results = db.execute("""SELECT count(*),coalesce(sum(matched),0),
                                       coalesce(sum(node=?),0),coalesce(sum(CASE WHEN node=? THEN matched END),0)
                                FROM cluster_video_results WHERE round=?""", (work_node, work_node, round_id)).fetchone()
        catalog = db.execute("SELECT count(*),coalesce(sum(rank=2),0) FROM catalog WHERE expires>?", (now,)).fetchone()
        phase = self.get(db, "phase", "collecting")
        importing = self.get(db, "round_import", {}) if phase == "importing" else {}
        import_pending = importing.get("pending", [])
        batch = self.get(db, "watch_batch")
        if batch:
            batch = {**batch, "counts": dict(db.execute("""SELECT state,count(*) FROM jobs WHERE kind='watch'
                                 AND json_extract(params,'$.batch')=? GROUP BY state""", (batch["id"],)))}
            batch["collection_pending"] = db.execute("""SELECT count(*) FROM jobs WHERE kind='watch'
                AND state='pending' AND json_extract(params,'$.batch')=?
                AND json_extract(params,'$.collection_pending')""", (batch["id"],)).fetchone()[0]
        dispatch = self.get(db, "dispatch", DispatchSettings().model_dump())
        video_queue = [dict(row) for row in db.execute("""SELECT j.key,j.state,n.name AS node,
                       assigned.name AS assigned,assigned.id AS assigned_id FROM jobs j
                       LEFT JOIN nodes n ON n.id=j.node
                       LEFT JOIN nodes assigned ON assigned.id=json_extract(j.params,'$.assigned')
                       WHERE j.round=? AND j.kind='video'
                       AND j.state IN ('pending','leased') ORDER BY j.state,j.id LIMIT 12""", (round_id,))]
        return {"round": round_id, "target": TARGET, "counts": counts, "nodes": nodes, "events": events,
                "round_keyword": keyword, "upcoming_keywords": self._upcoming_keywords(keyword),
                "concurrency_limit": dispatch["concurrency"], "two_stage": True, "dispatch": dispatch,
                "watch_tracking": True, "watch_batch_supported": True, "watch_batch": batch,
                "round_participation_supported": True,
                "video_history": True,
                "known_videos": db.execute("SELECT count(*) FROM checked_videos").fetchone()[0],
                "history_syncing": self._history_syncing(db, now),
                "search_tasks": self._search_tasks(db, round_id), "video_queue": video_queue,
                "round_import": {"round": round_id, "node": import_pending[0] if import_pending else None,
                                 **({"participating": True} if importing.get("participating") else {}),
                                 "pending": len(import_pending), "done": importing.get("done", 0),
                                 "skipped": importing.get("skipped", 0)} if importing else None,
                "search_pages_total": self.get(db, "search_pages_archived", 0) + search[1],
                "search_history_start": self.get(db, "search_history_start", search[0] or round_id),
                "local_work": dict(local_round) if local_round else {"pages": 0, "found": 0, "videos": 0, "activities": 0},
                "local_events": local_events,
                "conversion": {"checked": results[0], "matched": results[1]},
                "local_conversion": {"checked": results[2], "matched": results[3]},
                "detected_open": catalog[1], "retry_detail": True, "unknown_activity_time": True,
                "now": now, "phase": phase, "next_round": self.get(db, "next_round", 0),
                "paused": self.get(db, "paused", False), "cooldown_until": self.get(db, "cooldown_until", 0),
                "sequence": self.get(db, "sequence", 0),
                "active": catalog[0]}
