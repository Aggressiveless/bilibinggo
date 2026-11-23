from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from web.app import app
from web.control_service import get_control_service
from web.job_runner import JobRunner
from web.task_queue import TaskQueue


def test_dashboard_buttons_share_reorderable_queue(isolated_home, monkeypatch):
    runner = JobRunner()
    service = get_control_service()
    calls = []
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    def run(action, params, **kwargs):
        calls.append((action, params))
        if action == "refresh_all":
            refresh_started.set()
            assert release_refresh.wait(5)
        return {"ok": True, "message": "done", "log": f"finished {action}", "result": {}}

    monkeypatch.setattr("web.job_runner.runner", runner)
    monkeypatch.setattr("web.app.runner", runner)
    monkeypatch.setattr("web.job_runner.run_action", run)
    monkeypatch.setattr("web.app.get_account_profile", lambda: {"logged_in": True})
    monkeypatch.setattr("web.api_errors.is_llm_ready", lambda: True)
    monkeypatch.setattr("src.llm_settings.is_llm_ready", lambda: True)
    monkeypatch.setattr(service, "account", lambda: {"credentials_present": True})

    client = TestClient(app)

    def submit(action):
        response = client.post("/api/jobs?enqueue=true", json={"action": action, "params": {}})
        assert response.status_code == 200, response.text
        return response.json()["queue_entry"]

    try:
        assert submit("refresh_all")["state"] == "running"
        assert refresh_started.wait(1)
        participation = submit("participate_triple")
        assert participation["state"] == "queued"
        discovery = client.post("/api/discovery/start?enqueue=true", json={"keywords": ["GTA6抽奖"]})
        assert discovery.status_code == 200, discovery.text
        search = discovery.json()["queue_entry"]
        assert search["state"] == "queued"
        removed = submit("refresh_status")
        response = client.patch(f"/api/task-queue/{removed['id']}", json={"operation": "remove"})
        assert response.status_code == 200
        response = client.patch(f"/api/task-queue/{search['id']}", json={"operation": "up"})
        assert response.status_code == 200
        assert [entry["id"] for entry in response.json()["entries"] if entry["state"] == "queued"] == [search["id"], participation["id"]]
        assert [action for action, _ in calls] == ["refresh_all"]
    finally:
        release_refresh.set()

    until = time.monotonic() + 5
    while time.monotonic() < until:
        result = client.get("/api/task-queue")
        assert result.status_code == 200, result.text
        entries = result.json()["entries"]
        if all(entry["state"] in {"success", "cancelled", "error"} for entry in entries):
            break
        time.sleep(0.05)

    assert [entry["state"] for entry in entries] == ["success", "success", "success", "cancelled"]
    assert [action for action, _ in calls] == ["refresh_all", "control_discover", "participate_triple"]
    assert calls[1][1]["keywords"] == ["GTA6抽奖"]
    assert calls[1][1]["request_budget"] is None
    assert calls[2][1]["_queued_serial"] is True
    assert client.get("/api/discovery/current").json()["job"]["state"] == "success"
    for entry in entries[:3]:
        log = client.get(f"/api/task-queue/logs/{entry['job_id']}")
        assert log.status_code == 200
        assert log.json()["log"] == f"finished {entry['action']}"


def test_restart_pauses_remaining_queue(isolated_home):
    service = get_control_service()
    service.store.put("dashboard", "task_queue", {"entries": [
        {"id": "a", "state": "running", "job_id": None},
        {"id": "b", "state": "queued", "job_id": None},
    ]})
    queue = TaskQueue(service, JobRunner())
    state = queue.snapshot()
    assert state["pause_reason"]
    assert [entry["state"] for entry in state["entries"]] == ["interrupted", "queued"]
