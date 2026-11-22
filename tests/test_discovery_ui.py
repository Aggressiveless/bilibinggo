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
def test_discovery_start_stop_and_reload_keep_partial_progress(browser_control, monkeypatch, mode):
    client, service, runner = browser_control
    monkeypatch.setattr(service, "account", lambda: {"credentials_present": True})
    monkeypatch.setattr("src.llm_settings.is_llm_ready", lambda: True)
    entered = threading.Event()

    def discover(active_service, job_id, params, event, progress):
        assert active_service is service and params["request_budget"] == 180 and params["resume"]
        assert params["mode"] == mode and params["video_limit"] == 36
        assert params.get("candidate_limit", 9) == (0 if mode == "comprehensive_page" else 9)
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
    response = client.post("/api/discovery/start", json=body)
    assert response.status_code == 200
    job_id = response.json()["job"]["job_id"]
    try:
        assert entered.wait(3)
        status = client.get("/api/discovery/current").json()
        assert status["job"]["job_id"] == job_id and status["job"]["source"] == "ui"
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


def test_stop_cannot_cancel_unrelated_job_and_host_origin_are_checked(browser_control):
    client, service, _ = browser_control
    other = service.store.new_job("control_participate", service.descriptor["boot_id"])
    service.store.update_job(other["job_id"], state="success")
    assert client.post("/api/discovery/stop", json={"job_id": other["job_id"]}).status_code == 409
    assert client.post("/api/discovery/start", json={}, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert client.get("/api/discovery/current", headers={"Host": "attacker.invalid"}).status_code == 403


@pytest.mark.parametrize("body", [{"keywords": []}, {"keywords": [" "]}, {"keywords": ["a"] * 4},
                                      {"keywords": ["a" * 81]}, {"request_budget": 0}, {"action": "participate"},
                                      {"mode": "unknown"}, {"mode": "comprehensive_page", "keywords": ["a", "b"]}])
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
