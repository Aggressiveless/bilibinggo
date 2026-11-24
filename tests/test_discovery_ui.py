from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from src.control_store import ControlError


@pytest.fixture
def browser_control(isolated_home, monkeypatch):
    from web.app import app
    from web.control_service import get_control_service
    from web.job_runner import JobRunner
    runner = JobRunner()
    monkeypatch.setattr("web.job_runner.runner", runner)
    service = get_control_service()
    return TestClient(app), service, runner


def test_start_requires_saved_login_and_ready_model(browser_control, monkeypatch):
    client, service, _ = browser_control
    monkeypatch.setattr(service, "account", lambda: {"credentials_present": False})
    assert client.post("/api/discovery/start", json={}).json()["error"]["code"] == "AUTH_REQUIRED"
    monkeypatch.setattr(service, "account", lambda: {"credentials_present": True})
    monkeypatch.setattr("src.llm_settings.is_llm_ready", lambda: False)
    assert client.post("/api/discovery/start", json={}).json()["error"]["code"] == "LLM_NOT_READY"


@pytest.mark.parametrize("mode", ["standard", "comprehensive_page"])
@pytest.mark.parametrize("enqueue", [False, True])
def test_discovery_start_stop_and_reload_keep_partial_progress(browser_control, monkeypatch, mode, enqueue):
    client, service, runner = browser_control
    monkeypatch.setattr(service, "account", lambda: {"credentials_present": True})
    monkeypatch.setattr("src.llm_settings.is_llm_ready", lambda: True)
    entered = threading.Event()

    def discover(active_service, job_id, params, event, progress):
        assert active_service is service and params["request_budget"] is None and params["resume"]
        assert params["mode"] == mode and params["video_limit"] == 100
        assert params["target_new_videos"] == 100
        assert params["candidate_limit"] == 0 and params["collect_followers"]
        progress(step=2, total=36, message="扫描视频", result_patch={"videos_checked": 2})
        entered.set()
        assert event.wait(5), "Stop did not reach the worker"
        report = {"keywords": params["keywords"], "candidates": [], "requests_used": 5,
                  "raw": {"has_more": True, "resume_expires_at": time.time() + 1800,
                          "completion_reason": "cancelled", "videos_checked": 2, "verification_remaining": 3}}
        service.store.put("discovery", str(job_id), report)
        return {"eligible": 0, "requests_used": 5, "videos_checked": 2, "verification_remaining": 3,
                "completion_reason": "cancelled", "partial": True, "has_more": True}

    monkeypatch.setattr("web.control_actions.discover", discover)
    body = {"keywords": [" 抽奖 ", "抽奖"]}
    if mode == "comprehensive_page":
        body["mode"] = mode
    response = client.post("/api/discovery/start", params={"enqueue": enqueue}, json=body)
    assert response.status_code == 200
    job_id = response.json()["job"]["job_id"]
    try:
        assert entered.wait(3)
        status = client.get("/api/discovery/current").json()
        assert status["job"]["job_id"] == job_id
        assert status["job"]["source"] == ("queue" if enqueue else "ui")
        assert status["mode"] == mode
        assert status["keywords"] == ["抽奖"] and status["summary"]["videos_checked"] == 2
        assert client.post("/api/discovery/start", json={}).status_code == 409
        assert client.post("/api/discovery/stop", json={"job_id": job_id}).status_code == 200
        deadline = time.monotonic() + 4
        while runner.is_running() and time.monotonic() < deadline:
            threading.Event().wait(.02)
        assert not runner.is_running()
        status = client.get("/api/discovery/current").json()
        assert status["job"]["state"] == "cancelled" and status["resume_available"]
        assert status["mode"] == mode
        assert status["summary"]["requests_used"] == 5
        assert status["summary"]["verification_remaining"] == 3
        assert "已停止检索" in status["job"]["progress"]["message"]
        assert client.post("/api/discovery/stop", json={"job_id": job_id}).status_code == 200
    finally:
        runner.cancel()
        if enqueue:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                entries = client.get("/api/task-queue").json()["entries"]
                if all(row["state"] not in {"queued", "starting", "running"} for row in entries):
                    break
                threading.Event().wait(.02)
            assert entries and entries[-1]["state"] == "cancelled"


def test_stop_cannot_cancel_unrelated_job_and_host_origin_are_checked(browser_control):
    client, service, _ = browser_control
    other = service.store.new_job("control_participate", service.descriptor["boot_id"])
    service.store.update_job(other["job_id"], state="success")
    assert client.post("/api/discovery/stop", json={"job_id": other["job_id"]}).status_code == 409
    assert client.post("/api/discovery/start", json={}, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert client.get("/api/discovery/current", headers={"Host": "attacker.invalid"}).status_code == 403


@pytest.mark.parametrize("body", [{"keywords": []}, {"keywords": [" "]}, {"keywords": ["a"] * 4},
                                      {"keywords": ["a" * 81]}, {"request_budget": 0}, {"action": "participate"},
                                      {"mode": "unknown"}, {"mode": "comprehensive_page", "keywords": ["a", "b"]},
                                      {"target_new_videos": 0}, {"target_new_videos": 10001}, {"target_new_videos": 1.5}])
def test_browser_cannot_submit_invalid_or_unrelated_actions(browser_control, body):
    client, _, _ = browser_control
    assert client.post("/api/discovery/start", json=body).status_code == 400


def test_latest_discovery_is_restored_even_after_other_jobs(browser_control):
    client, service, _ = browser_control
    job = service.store.new_job("control_discover", service.descriptor["boot_id"])
    service.store.update_job(job["job_id"], state="success", result={"completion_reason": "request_budget"})
    service.store.put("discovery", str(job["job_id"]), {"candidates": [], "raw": {"has_more": True,
        "resume_expires_at": time.time() - 1, "verification_remaining": 133}})
    other = service.store.new_job("refresh_status", service.descriptor["boot_id"])
    service.store.update_job(other["job_id"], state="success")
    status = client.get("/api/discovery/current").json()
    assert status["job"]["job_id"] == job["job_id"]
    assert not status["resume_available"] and status["summary"]["verification_remaining"] == 133


def test_completed_search_keeps_bvs_and_imports_only_confirmed_matches(browser_control, monkeypatch):
    from types import SimpleNamespace

    from src.activity_store import append_activities, known_activity_ids
    from src.lottery_discovery import DiscoveryStopped

    client, service, runner = browser_control
    monkeypatch.setattr(service, "account", lambda: {"credentials_present": True})
    monkeypatch.setattr("src.llm_settings.is_llm_ready", lambda: True)
    ids = ["1234567890123456781", "1234567890123456782", "1234567890123456783"]
    opened = []
    for i, identifier in enumerate(ids):
        opened.append({"status": "open", "dynamic_id": identifier, "url": f"https://www.bilibili.com/opus/{identifier}",
                       "deadline": time.time() + (-60 if i == 2 else 3600)})

    opened[0].update(rule_text="视频一键三连参与", publisher="发布者", publisher_uid="12345", followers=2345)
    opened[1]["rule_text"] = "只需关注并转发，无需投币"

    def search(*args, **kwargs):
        kwargs["on_progress"]({
            "checkpoint_state": {"done": True}, "video_page": {
                "id": "test-page", "videos": ["BV11a1111111", "BV11a1111112", "BV11a1111113"],
            },
            "video_checks": {}, "rows": opened, "errors": [], "requests": 0, "eligible": 2,
            "session_id": "test", "resumed": False, "scan_completed": True, "verification_remaining": 0,
            "videos_checked": 3, "search_video_count": 3, "message": "完成", "phase": "finished",
        })
        return dict(open=opened, review=[], closed=[], joined=[], errors=[], stopped="", requests=0,
                    session_id="test", resumed=False, scan_completed=True, has_more=False, verification_remaining=0,
                    sources_remaining=0, videos_checked=3, search_video_count=3, completion_reason="search_complete",
                    cache_hits=0, llm_requests=0, llm_cache_hits=0, min_interval_seconds=2, elapsed_seconds=0)

    attempted = []

    def import_one(urls, *, workers, client, accept):
        did = urls[0].rsplit("/", 1)[-1]
        attempted.append(did)
        if attempted == [ids[1]]:
            raise DiscoveryStopped("已达到本轮 B 站请求预算")
        assert accept(SimpleNamespace(draw_status="active", platform_participated=False, reserve_reserved=False, lottery_time=None))
        assert not accept(SimpleNamespace(draw_status="ended", platform_participated=False, reserve_reserved=False, lottery_time=None))
        count = append_activities([{"dynamic_id": did, "source_url": urls[0], "lottery_type": "互动抽奖"}])
        return SimpleNamespace(ok=True, persisted_count=count)

    def finished_status():
        deadline = time.monotonic() + 5
        while runner.is_running() and time.monotonic() < deadline:
            threading.Event().wait(.02)
        assert not runner.is_running()
        return client.get("/api/discovery/current").json()

    monkeypatch.setattr("src.lottery_discovery_broad.discover_page", search)
    monkeypatch.setattr("web.discovery_queue.run_new_links_pipeline", import_one)
    start = client.post("/api/discovery/start", json={"keywords": ["抽奖"], "mode": "comprehensive_page"}).json()
    job_id = start["job"]["job_id"]
    summary = finished_status()
    assert summary["queue"]["imported"] == 0 and summary["pending_selection"] == 2
    assert summary["bv_count"] == 3
    assert summary["candidates"][0] == {"dynamic_id": ids[0], "prize": "", "publisher": "发布者",
        "publisher_uid": "12345", "followers": 2345, "triple_requirement": "required"}
    assert [row["dynamic_id"] for row in summary["candidates"]] == ids[:2]
    videos = client.get("/api/discovery/videos?offset=1&limit=1").json()
    assert videos["total"] == 3 and videos["items"][0]["bvid"] == "BV11a1111112"
    assert videos["items"][0]["first_seen"] == videos["items"][0]["last_seen"]
    assert not known_activity_ids()

    assert client.post("/api/discovery/import", json={"job_id": job_id}).status_code == 400
    assert client.post("/api/discovery/filter", json={"job_id": job_id, "settings": {"coins": "not_required"}}).status_code == 200
    summary = finished_status()
    assert summary["filter"]["matched"] == 1 and summary["bv_count"] == 3
    assert not known_activity_ids()
    assert client.post("/api/discovery/import", json={"job_id": job_id, "selection": "filtered", "filter_id": "wrong"}).status_code == 400
    selected = {"job_id": job_id, "selection": "filtered", "filter_id": summary["filter"]["id"]}
    assert client.post("/api/discovery/import", json=selected).status_code == 200
    assert finished_status()["queue"]["imported"] == 0
    assert client.post("/api/discovery/import", json=selected).status_code == 200
    summary = finished_status()
    assert summary["queue_job"]["state"] == "success"
    assert summary["queue"]["imported"] == 1 and summary["pending_selection"] == 1
    assert known_activity_ids() == {ids[1]}
    assert attempted == [ids[1], ids[1]]
    assert client.post("/api/discovery/import", json=selected).status_code == 200
    assert not runner.is_running()
    manual = {"job_id": job_id, "selection": "selected", "selected_ids": [ids[0]]}
    assert client.post("/api/discovery/import", json={**manual, "selected_ids": ["999"]}).status_code == 400
    assert client.post("/api/discovery/import", json=manual).status_code == 200
    summary = finished_status()
    assert summary["queue"]["imported"] == 2 and summary["pending_selection"] == 0
    assert known_activity_ids() == set(ids[:2]) and summary["bv_count"] == 3


def test_stop_interrupts_rate_wait_without_sending_another_request(isolated_home, monkeypatch):
    from web.control_actions import discover
    from src.lottery_discovery import DiscoveryStopped
    from src.bilibili_client import BilibiliClient
    from web.control_service import get_control_service
    service = get_control_service()
    event = threading.Event()
    entered = threading.Event()
    finished = threading.Event()
    errors = []

    def find(client, words, **kwargs):
        client._next_request_at = time.monotonic() + 30
        entered.set()
        with pytest.raises(DiscoveryStopped, match="用户取消"):
            client._http_get("https://api.bilibili.com/example")
        with pytest.raises(DiscoveryStopped, match="用户取消"):
            client.extract("example")
        assert client.request_count == 0
        raise ControlError("NOT_FOUND", "test-finished")

    monkeypatch.setattr("src.lottery_discovery_broad.discover_page", find)
    monkeypatch.setattr(BilibiliClient, "_http_get", lambda *a, **k: pytest.fail("Request after stop"))

    def worker():
        try:
            discover(service, 123, {"keywords": ["抽奖"]}, event)
        except ControlError as exc:
            assert str(exc) == "test-finished"
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert entered.wait(3)
    event.set()
    assert finished.wait(2), "Rate wait did not stop promptly"
    thread.join()
    assert not errors


def test_browser_search_crosses_180_requests_in_one_job(browser_control, monkeypatch):
    import httpx

    from src.bilibili_client import BilibiliClient
    from src.control_telemetry import record_request
    client, service, runner = browser_control
    monkeypatch.setattr(service, "account", lambda: {"credentials_present": True})
    monkeypatch.setattr("src.llm_settings.is_llm_ready", lambda: True)

    def platform_get(self, url, **kwargs):
        record_request("GET", url)
        return httpx.Response(200, json={"code": 0})

    def search(discovery_client, *args, **kwargs):
        monkeypatch.setattr(type(discovery_client), "_wait_for_request", lambda *a: None)
        for _ in range(181):
            discovery_client._http_get("https://api.bilibili.com/example")
        with pytest.raises(RuntimeError, match="禁止平台写"):
            record_request("POST", "https://api.bilibili.com/example")
        return dict(open=[], review=[], closed=[], joined=[], errors=[], stopped="", requests=181,
                    session_id="test", resumed=False, scan_completed=True, has_more=False, verification_remaining=0,
                    sources_remaining=0, videos_checked=1, search_video_count=1, completion_reason="search_complete",
                    cache_hits=0, llm_requests=0, llm_cache_hits=0, min_interval_seconds=2, elapsed_seconds=0)

    monkeypatch.setattr(BilibiliClient, "_http_get", platform_get)
    monkeypatch.setattr("src.lottery_discovery_broad.discover_page", search)
    result = client.post("/api/discovery/start", json={}).json()
    deadline = time.monotonic() + 5
    while runner.is_running() and time.monotonic() < deadline:
        threading.Event().wait(.02)
    status = client.get("/api/discovery/current").json()
    assert status["job"]["job_id"] == result["job"]["job_id"]
    assert status["job"]["state"] == "success"
    assert status["summary"]["completion_reason"] == "search_complete"
    assert status["summary"]["requests_used"] == 181
    assert status["summary"]["network_counts"]["platform_read_requests"] == 181
