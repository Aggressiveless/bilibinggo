
from __future__ import annotations

import secrets
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from src.sources.common import opus_link
from web.cluster_protocol import Activity, Analysis, Exchange, Reference, Register, Report
from web.cluster_service import ClusterService
from web.cluster_store import ClusterError, ClusterStore

KEY = "123456789012345678"
BVID = "BV1234567890"


def _activity(**fields):
    return Activity(dynamic_id=KEY, source_url=opus_link(KEY), lottery_type="互动抽奖",
                    lottery_time=int(time.time()) + 86400, **fields)


def _cluster(path):
    store = ClusterStore(path)
    host = store.start_host("主节点", "http://127.0.0.1:8182")
    member = Register(name="工作节点", node=uuid.uuid4().hex, token=secrets.token_hex(32))
    store.register(member, store.read("invite"))
    return store, host, member.model_dump()


def _exchange(store, node, **values):
    return store.exchange(Exchange(node=node["node"], **values), node["token"])


def _report(task, **values):
    return Report(task_id=task.task_id, lease=task.lease, outcome="done", **values)


def test_round_deduplicates_video_and_fans_out_activity(tmp_path):
    store, host, member = _cluster(tmp_path)
    first = _exchange(store, host, work=True).task
    second = _exchange(store, member, work=True).task
    assert first.keyword != second.keyword
    ref = Reference(kind="video", key=BVID)
    first_result = _report(first, references=[ref])
    _exchange(store, host, report=first_result)
    _exchange(store, member, report=_report(second, references=[ref]))
    _exchange(store, host, report=first_result)
    assert store.status()["counts"]["video"] == {"pending": 1}

    result = _exchange(store, member, activities=[_activity(prize="奖品")])
    assert len(result.changes) == 1
    assert _exchange(store, host).changes == result.changes
    assert _exchange(store, member, after=result.changes[-1].seq).changes == []


def test_expired_lease_cannot_publish_after_reassignment(tmp_path):
    store, host, member = _cluster(tmp_path)
    _exchange(store, host)
    with store.tx() as db:
        db.execute("DELETE FROM jobs")
        store._queue(db, 1, "dynamic", KEY)
    task = _exchange(store, host, work=True).task
    with store.tx() as db:
        db.execute("UPDATE jobs SET until=0 WHERE id=?", (task.task_id,))
    replacement = _exchange(store, member, work=True).task
    assert replacement.task_id == task.task_id
    assert replacement.lease != task.lease
    stale = _exchange(store, host, report=_report(task, activities=[_activity(prize="过期结果")]))
    assert stale.changes == []
    result = _exchange(store, member, report=_report(replacement, activities=[_activity(prize="有效结果")]))
    assert result.changes[-1].activity.prize == "有效结果"


def test_model_reservation_is_reused_across_nodes(tmp_path):
    store, host, member = _cluster(tmp_path)
    first = _exchange(store, host, work=True).task
    second = _exchange(store, member, work=True).task
    key = secrets.token_hex(32)

    def request(node, task, fields=None):
        return store.analysis(Analysis(node=node["node"], task_id=task.task_id,
                                       lease=task.lease, key=key, fields=fields), node["token"])

    assert request(host, first) == {"fields": None}
    assert request(member, second) == {"busy": True}
    fields = {"model_assessment": "entry", "rules_summary": "入口"}
    assert request(host, first, fields) == {"fields": fields}
    assert request(member, second) == {"fields": fields}


def test_host_api_login_auth_and_protocol(isolated_home, monkeypatch):
    from web import cluster_routes
    from web.app import app

    monkeypatch.setattr(cluster_routes, "DATA_DIR", isolated_home / "data")
    logged_in = False
    monkeypatch.setattr(cluster_routes, "get_account_profile", lambda: {"logged_in": logged_in})
    monkeypatch.setattr(cluster_routes, "has_login_cookie", lambda: logged_in)
    with TestClient(app) as api:
        headers = {"X-Binggo-Dashboard": "1"}
        data = {"name": "主节点", "address": "http://127.0.0.1:8182"}
        assert api.post("/api/cluster/host", json=data, headers=headers).status_code == 401
        logged_in = True
        assert api.post("/api/cluster/host", json=data).status_code == 403
        assert api.post("/api/cluster/host", json=data, headers=headers).json()["role"] == "host"
        invite = api.post("/api/cluster/invite", headers=headers).json()["invite"]
        base = "/api/cluster/peer/v1"
        other = Register(name="成员", node=uuid.uuid4().hex, token=secrets.token_hex(32))
        assert api.post(base + "/join", json=other.model_dump()).status_code == 401
        joined = api.post(base + "/join", headers={"Authorization": f"Bearer {invite}"}, json=other.model_dump())
        assert joined.json()["protocol"] == 1
        body = Exchange(node=other.node, after=0, activities=[_activity(prize="同步")]).model_dump()
        creds = {"Authorization": f"Bearer {other.token}"}
        result = api.post(base + "/exchange", headers=creds, json=body)
        assert result.json()["changes"][0]["activity"]["prize"] == "同步"
        assert api.post(base + "/exchange", headers=creds, content=b"x" * (256 * 1024 + 1)).status_code == 413
        assert api.post(base + "/leave/" + other.node, headers=creds).status_code == 200
        assert api.post(base + "/exchange", headers=creds, json=body).status_code == 401


def test_member_reports_disconnect_and_reconnects(tmp_path, monkeypatch):
    from web.cluster_protocol import JoinRequest
    from web import cluster_service

    host, host_config, _ = _cluster(tmp_path / "host")
    svc = ClusterService(tmp_path / "member")
    loaded = []
    monkeypatch.setattr(cluster_service, "is_llm_ready", lambda: False)
    monkeypatch.setattr(cluster_service, "_import", lambda changes, cluster_id: loaded.extend(changes))

    def remote(_session, path, token, payload):
        if path == "/join":
            return host.register(Register.model_validate(payload), token)
        return host.exchange(Exchange.model_validate(payload), token).model_dump()

    monkeypatch.setattr(svc, "_remote", remote)
    svc.join(JoinRequest(name="另一台", address="http://127.0.0.1:8182", invite=host.read("invite")))
    session = svc.store.read("session")
    svc._sync(session, [_activity(prize="双向")], None)
    assert loaded[0].activity.prize == "双向"
    assert _exchange(host, host_config).changes == loaded
    svc._sync(session, [], None)
    assert len(loaded) == 1
    with pytest.raises(ClusterError):
        host.exchange(Exchange(node=session["node"], after=0), "bad token")
    svc.http.close()


def test_round_targets_1000_distinct_videos_and_starts_again(tmp_path):
    from web.cluster_protocol import TARGET

    store, host, _ = _cluster(tmp_path)
    search = _exchange(store, host, work=True).task
    with store.tx() as db:
        for number in range(TARGET - 1):
            store._queue(db, 1, "video", f"BV{number:010d}")
    refs = [Reference(kind="video", key="BV0000000000"),
            Reference(kind="video", key="BV9999999998"),
            Reference(kind="video", key="BV9999999999")]
    feed = _exchange(store, host, report=_report(search, references=refs, more_pages=True))
    assert feed.status["counts"]["video"] == {"pending": TARGET}
    assert feed.status["counts"]["search"] == {"done": 1, "skipped": 11}
    with store.tx() as db:
        db.execute("UPDATE jobs SET state='done' WHERE kind='video'")
    completed = _exchange(store, host).status
    assert completed["phase"] == "complete"
    assert completed["next_round"] > completed["now"]
    store.write("next_round", 1)
    next_round = _exchange(store, host, work=True)
    assert next_round.status["round"] == 2 and next_round.task.page == 1


def test_worker_search_and_linked_activity_without_account_actions(tmp_path, monkeypatch):
    from web import cluster_work
    from web.cluster_protocol import Task

    now = int(time.time())
    link = opus_link(KEY)
    endpoints = {
        cluster_work.d.SEARCH_URL: {"result": [{"bvid": BVID}, {"bvid": BVID}], "numPages": 2},
        cluster_work.d.VIDEO_URL: {"title": "节目福利", "desc": "详情见评论区", "owner": {"mid": 123}, "aid": 42},
        cluster_work.d.REPLIES_URL: {"top": {"upper": {"mid": 123, "member": {"mid": "123"},
                                                     "content": {"message": "抽奖入口：" + link}}}},
        cluster_work.d.DYNAMIC_DETAIL_URL: {"item": {"id_str": KEY, "modules": {}}},
        cluster_work.d.LOTTERY_NOTICE_URL: {"lottery_id": "9", "status": 0, "participated": 1,
                                           "lottery_time": now + 3600, "first_prize_cmt": "奖品"},
    }
    monkeypatch.setattr(cluster_work.WorkClient, "read", lambda self, url, *args, **kwargs:
                        {"code": 0, "data": endpoints[url]})

    def run(kind, key, **params):
        task = Task(task_id=1, lease=uuid.uuid4().hex, kind=kind, key=key, **params)
        return cluster_work.execute(task, tmp_path / "cache.json", lambda _: None, lambda _, fields: fields)

    search = run("search", "pubdate:抽奖:1", keyword="抽奖")
    assert [row.key for row in search.references] == [BVID] and search.more_pages
    scanned = run("video", BVID)
    assert scanned.references == [Reference(kind="dynamic", key=KEY)]
    verified = run("dynamic", KEY)
    assert verified.activities[0].prize == "奖品"
    assert verified.activities[0].lottery_time == now + 3600
    assert verified.model_calls == 0


def test_shared_cluster_log_records_work_retry_and_result(tmp_path):
    store, host, member = _cluster(tmp_path)
    task = _exchange(store, host, work=True).task
    assert task is not None
    retry = Report(task_id=task.task_id, lease=task.lease, outcome="retry", reason="model")
    _exchange(store, host, report=retry)
    member_job = _exchange(store, member, work=True).task
    report = _report(member_job, references=[Reference(kind="video", key=BVID)])
    view_from_member = _exchange(store, member, report=report).status["events"]
    assert view_from_member == store.status()["events"]
    assert any(event["node"] == host["node"] and event["level"] == "warning"
               and "模型未返回有效结果" in event["message"] for event in view_from_member)
    assert any(event["node"] == member["node"] and "去重后收录 1 个" in event["message"]
               for event in view_from_member)
    assert any(event["node"] is None and "第 1 轮开始" in event["message"] for event in view_from_member)
    assert host["token"] not in str(view_from_member) and member["token"] not in str(view_from_member)
    _exchange(store, member, report=report)
    assert store.status()["events"] == view_from_member


def test_existing_cluster_adds_event_table_and_keeps_state(tmp_path):
    store, host, member = _cluster(tmp_path)
    _exchange(store, host, activities=[_activity(prize="原有活动")])
    with store.tx() as db:
        db.execute("DROP TABLE cluster_events")
    updated = ClusterStore(tmp_path)
    assert updated.read("session")["id"] == host["id"]
    assert _exchange(updated, member).changes[0].activity.prize == "原有活动"
    updated.set_paused(True)
    assert _exchange(updated, member).status["events"][0]["message"] == "暂停领取新任务"
