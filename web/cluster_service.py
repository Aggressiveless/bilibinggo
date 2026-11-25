
"""Persistent Binggo participant and coordinator lifecycle."""
from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from src.activity_store import activity_file_lock, append_activities, load_activities, update_activity
from src.llm_settings import is_llm_ready
from src.lottery_classifier import PARTICIPATABLE_TYPES
from src.lottery_time import lottery_time_unix
from src.pipeline.status_step import apply_initial_status
from src.sources.common import is_valid_dynamic_id, opus_link
from web.account_service import has_login_cookie
from web.activity_service import invalidate_activity_cache
from web.cluster_protocol import Activity, Analysis, DetectionPageRequest, Exchange, Feed, Lease, PAGE_SIZE, PEER_PATH, POLL_SECONDS, Register, Report, normalize_address
from web.cluster_store import ClusterError, ClusterStore
from web.cluster_work import RetryCheck, execute

logger = logging.getLogger(__name__)
SYNC_SECONDS = 30


def _export(now):
    rows = []
    for item in load_activities():
        key = str(item.get("dynamic_id") or "")
        kind = item.get("lottery_type")
        if (item.get("cluster_id") or kind not in PARTICIPATABLE_TYPES or item.get("skipped")
                or item.get("lottery_time_inferred") or (item.get("conditions") or {}).get("lottery_time_inferred")
                or item.get("draw_status") != "active" or not key.isascii() or not is_valid_dynamic_id(key)):
            continue
        timestamp = lottery_time_unix(item)
        if not timestamp or not now < timestamp <= 253402271999:
            continue
        prizes = item.get("prizes") or []
        prize = "、".join(str(entry.get("description") or "") for entry in prizes if isinstance(entry, dict))
        rows.append(Activity(dynamic_id=key, source_url=opus_link(key), lottery_type=kind,
                             lottery_time=timestamp, prize=prize[:300]))
    return rows


def _import(changes, cluster_id):
    if not changes:
        return
    now = int(time.time())
    with activity_file_lock():
        existing = {str(item["dynamic_id"]): item for item in load_activities()}
        new = []
        changed = False
        for change in changes:
            activity = change.activity
            key = activity.dynamic_id
            current = existing.get(key)
            if current and (not current.get("cluster_id") or (
                current["cluster_id"] == cluster_id and current.get("cluster_seq", 0) >= change.seq
            )):
                continue
            deadline = min(activity.lottery_time, activity.deadline or activity.lottery_time)
            closed = activity.closed or deadline <= now
            if closed and not current:
                continue
            row = dict(current or {})
            row.update(dynamic_id=key, source_url=opus_link(key), lottery_type=activity.lottery_type,
                       lottery_time=activity.lottery_time, prizes=[{"description": activity.prize}] if activity.prize else [],
                       cluster_id=cluster_id, cluster_seq=change.seq, cluster_deadline=activity.deadline,
                       cluster_closed=closed)
            if current:
                if closed:
                    row.update(draw_status="ended", activity_status="已结束", draw_tag="")
                elif current.get("cluster_closed") or (
                    type(current.get("cluster_deadline")) is int and current["cluster_deadline"] <= now
                ):
                    row.update(draw_status="active", status_classified=False)
                update_activity(key, row)
                changed = True
            else:
                row = apply_initial_status(row, now=now)
                row["status_classified"] = False
                new.append(row)
        if append_activities(new) or changed:
            invalidate_activity_cache()


class ClusterService:
    def __init__(self, directory: Path):
        self.store = ClusterStore(directory)
        self.directory = directory
        self.http = httpx.Client(timeout=8, trust_env=False, follow_redirects=False)
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.status_remote = {}
        self.error = ""
        self.last_sync = 0
        self._thread = None
        self._job = None
        self._task = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="binggo-cluster", daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()

    def _remote(self, session, path, token, payload):
        headers = {"Authorization": "Bearer " + token}
        if path == "/exchange":
            # Old v1 hosts reject extra JSON fields but ignore the capability header.
            payload = dict(payload)
            if payload.pop("video_detection", None):
                headers["X-Binggo-Video-Detection"] = "1"
        try:
            response = self.http.post(session["address"] + PEER_PATH + path,
                                      headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise ClusterError("无法连接主服务器，请检查地址、网络和 TLS 证书", 502) from exc
        if response.status_code == 401:
            raise ClusterError("集群授权失败，请检查邀请码或重新加入", 401)
        if response.status_code != 200:
            raise ClusterError(f"主服务器返回 HTTP {response.status_code}，请检查集群入口", 502)
        try:
            return response.json()
        except ValueError as exc:
            raise ClusterError("主服务器未返回集群协议数据", 502) from exc

    def _set_session(self, session):
        self.store.write("session", session)
        self.status_remote = {}
        self.error = ""
        self.last_sync = 0
        self.wake.set()

    def host(self, body):
        address = normalize_address(body.address)
        with self.lock:
            self.store.start_host(body.name.strip() or "主节点", address)
            self.status_remote = {}
            self.error = ""
            self.last_sync = 0
            self.wake.set()

    def join(self, body):
        address = normalize_address(body.address)
        with self.lock:
            if self.store.read("session"):
                raise ClusterError("请先退出当前集群")
            registration = Register(name=body.name.strip() or "工作节点", node=uuid.uuid4().hex,
                                    token=secrets.token_hex(32))
            response = self._remote({"address": address}, "/join", body.invite, registration.model_dump())
            cluster_id = response.get("cluster_id") if isinstance(response, dict) else None
            if not isinstance(cluster_id, str) or len(cluster_id) != 32 or response.get("protocol") != 1:
                raise ClusterError("主服务器的集群协议版本不兼容", 502)
            self._set_session({**registration.model_dump(), "role": "member", "id": cluster_id, "address": address})

    def leave(self):
        with self.lock:
            session = self.store.read("session") or {}
            self._set_session(None)
        if session.get("role") == "member":
            try:
                self._remote(session, f"/leave/{session['node']}", session["token"], {})
            except ClusterError:
                pass

    def snapshot(self):
        config = self.store.read("session") or {}
        role = config.get("role")
        connected = bool(self.last_sync and self.last_sync > time.time() - 30)
        status = self.store.status() if role == "host" else self.status_remote if role else {}
        return {"role": role, "address": config.get("address"), "node": config.get("node"),
                "connected": connected if role else False, "error": self.error if role else "",
                "last_sync": self.last_sync if role else 0,
                "local_cursor": self.store.read(f"cursor:{config['node']}", 0) if role else 0,
                "worker_ready": has_login_cookie() and is_llm_ready(),
                "status": status}

    def invite(self):
        with self.lock:
            if (self.store.read("session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以查看邀请码")
            return {"invite": self.store.read("invite")}

    def detected_activities(self, page):
        session = self.store.read("session") or {}
        if session.get("role") == "host":
            return self.store.detected_activities(page)
        if session.get("role") != "member":
            raise ClusterError("请先加入工作集群")
        if "detected_open" not in self.status_remote:
            raise ClusterError("主节点尚未提供检出活动列表")
        request = DetectionPageRequest(node=session["node"], page=page)
        return self._remote(session, "/detected-activities", session["token"], request.model_dump())

    def _analysis(self, session, task, key, fields=None):
        payload = {"node": session["node"], "task_id": task.task_id, "lease": task.lease, "key": key, "fields": fields}
        if session["role"] == "host":
            response = self.store.analysis(Analysis.model_validate(payload), session["token"])
        else:
            response = self._remote(session, "/analysis", session["token"], payload)
        if response.get("busy"):
            raise RetryCheck("相同内容正在由其他节点分析", reason="analysis_busy")
        return response.get("fields")

    def _execute(self, session, task, *, two_stage):
        return execute(task, self.directory / "worker-cache.json",
                       lambda key: self._analysis(session, task, key),
                       lambda key, result: self._analysis(session, task, key, result),
                       two_stage=two_stage)

    def _run(self):
        session_node = ""
        export_queue = []
        exported = set()
        reload_at = 0
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cluster-worker") as pool:
                while not self.stop_event.is_set():
                    self.wake.clear()
                    session = self.store.read("session") or {}
                    if session.get("node") != session_node:
                        session_node = session.get("node", "")
                        export_queue, exported, reload_at = [], set(), 0
                    if self._job and self._job.done():
                        try:
                            report = self._job.result()
                        except Exception:
                            logger.warning("集群任务未完成", exc_info=True)
                            report = Report(task_id=self._task.task_id, lease=self._task.lease, outcome="retry", reason="worker")
                        report_node = self._task_node
                        self._job, self._task = None, None
                        if report_node == session_node:
                            self.store.write(f"report:{session_node}", report.model_dump())
                    if session_node and has_login_cookie():
                        try:
                            now = int(time.time())
                            if not export_queue and now >= reload_at:
                                offered = [(row.model_dump_json(), row) for row in _export(now)]
                                exported.intersection_update(mark for mark, _ in offered)
                                export_queue = [(mark, row) for mark, row in offered if mark not in exported]
                                reload_at = now + SYNC_SECONDS
                            batch = export_queue[:PAGE_SIZE]
                            feed = self._sync(session, [row for _, row in batch], pool)
                            if feed:
                                exported.update(mark for mark, _ in batch)
                                export_queue = export_queue[PAGE_SIZE:]
                        except ClusterError as exc:
                            self.error = str(exc)
                        except Exception:
                            logger.warning("集群同步失败", exc_info=True)
                            self.error = "同步失败，将自动重试"
                    elif session_node:
                        self.error = "请重新登录 B 站以恢复集群同步"
                    self.wake.wait(POLL_SECONDS)
        finally:
            self.http.close()

    def _sync(self, session, batch, pool):
        node = session["node"]
        pending = self.store.read(f"report:{node}")
        active = self._task if self._task_node_if_working() == node else None
        two_stage = session["role"] == "host" or bool(self.status_remote.get("two_stage"))
        video_detection = is_llm_ready()
        message = Exchange(node=node, after=self.store.read(f"cursor:{node}", 0), activities=batch,
                           work=not self._job and (two_stage or video_detection),
                           video_detection=video_detection if two_stage else None,
                           working=Lease(task_id=active.task_id, lease=active.lease) if active else None,
                           report=Report.model_validate(pending) if pending else None)
        if session["role"] == "host":
            feed = self.store.exchange(message, session["token"])
        else:
            payload = message.model_dump()
            if pending_report := payload["report"]:
                if "events" not in self.status_remote:
                    pending_report.pop("reason", None)
                elif (not self.status_remote.get("retry_detail") and
                      pending_report["reason"] in {"source_response", "dynamic_detail", "dynamic_body"}):
                    pending_report["reason"] = "platform"
            feed = Feed.model_validate(self._remote(session, "/exchange", session["token"], payload))
        with self.lock:
            if self.store.read("session") != session:
                return None
            if pending:
                self.store.write(f"report:{node}", None)
            _import(feed.changes, session["id"])
            if feed.changes:
                self.store.write(f"cursor:{node}", feed.changes[-1].seq)
            self.status_remote, self.last_sync, self.error = feed.status, int(time.time()), ""
            if feed.task and not self.stop_event.is_set():
                self._task, self._task_node = feed.task, node
                self._job = pool.submit(self._execute, session, feed.task, two_stage=bool(feed.status.get("two_stage")))
            return feed

    def _task_node_if_working(self):
        return self._task_node if self._job else None
