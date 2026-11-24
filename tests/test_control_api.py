from __future__ import annotations
import copy
import json
import uuid
import pytest
from fastapi.testclient import TestClient
from src.control_store import ControlStore, ControlError


def test_public_dashboard_checks_origin_and_requires_dashboard_header(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.control_routes import install_control

    monkeypatch.setenv("BINGGO_WEB_ORIGIN", "https://dashboard.example")
    application = FastAPI()
    application.post("/api/test-write")(lambda: {"ok": True})
    install_control(application)
    client = TestClient(application)
    assert client.post("/api/test-write").status_code == 403
    headers = {"X-Binggo-Dashboard": "1", "Origin": "https://dashboard.example"}
    assert client.post("/api/test-write", headers=headers).status_code == 200
    assert client.post("/api/test-write", headers={**headers, "Origin": "https://other.example"}).status_code == 403


@pytest.fixture
def controlled(isolated_home):
    from web.app import app
    from web.control_service import get_control_service
    service = get_control_service()
    client = TestClient(app)
    headers = {"Authorization": "Bearer "+service.access()["token"]}
    return client, service, headers


def call(controlled, name, **args):
    client, service, headers = controlled
    values = {"instance_id": service.instance_id, **args}
    return client.post("/api/control/v2/tools/binggo_"+name, json=values, headers=headers).json()


def writing(service):
    return {"expected_boot_id": service.descriptor["boot_id"], "idempotency_key": uuid.uuid4().hex}


def test_authentication_and_origin_are_required(controlled):
    client, service, headers = controlled
    assert client.get("/api/control/v2/identity").status_code == 401
    assert client.get("/api/control/v2/identity", headers=headers).json()["instance_id"] == service.instance_id
    assert client.post("/api/jobs", json={"action": "login"}, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert client.get("/api/control/v2/identity", headers={**headers, "Host": "attacker.invalid"}).status_code == 403


def test_wrong_instance_and_boot_are_rejected(controlled):
    client, service, headers = controlled
    result = client.post("/api/control/v2/tools/binggo_instance_get", json={"instance_id": "other"}, headers=headers).json()
    assert result["error"]["code"] == "INSTANCE_NOT_FOUND"
    result = call(controlled, "settings_update", expected_boot_id="stale", idempotency_key="once", expected_revision="initial", patch={"participate_text": "sample"})
    assert result["error"]["code"] == "INSTANCE_CHANGED"


def test_cached_account_does_not_contact_platform(controlled, monkeypatch):
    monkeypatch.setattr("web.account_service.get_account_profile", lambda: pytest.fail("network call"))
    result = call(controlled, "account_get")
    assert result["ok"] and result["data"]["auth_state"] == "absent"


def test_long_log_lines_are_bounded_before_schema_validation(controlled, monkeypatch):
    monkeypatch.setattr("src.log_query.query_log_lines", lambda **kwargs: {"lines": ["long log entry " * 900]})
    result = call(controlled, "logs_query")
    assert result["ok"] and len(result["data"]["lines"][0]) == 4000


def test_invalid_output_never_echoes_the_rejected_instance():
    from src.control_contract import envelope, validate_output
    from src.control_store import ControlError
    value = envelope("instance", {"lines": ["private-sentinel" * 1000], "next_cursor": None})
    with pytest.raises(ControlError) as error:
        validate_output("binggo_logs_query", value)
    assert error.value.code == "INVALID_RESPONSE"
    assert "private-sentinel" not in str(error.value)


def test_settings_revision_idempotency_and_conflict(controlled):
    _, service, _ = controlled
    revision = call(controlled, "settings_get")["data"]["revision"]
    values = {**writing(service), "expected_revision": revision, "patch": {"participate_text": "MCP test text"}}
    first = call(controlled, "settings_update", **values)
    second = call(controlled, "settings_update", **values)
    assert first == second and first["ok"]
    changed = copy.deepcopy(values)
    changed["patch"]["participate_text"] = "different"
    assert call(controlled, "settings_update", **changed)["error"]["code"] == "REVISION_CONFLICT"
    assert call(controlled, "settings_update", **values) == first
    assert call(controlled, "settings_update", **{**writing(service), "expected_revision": revision, "patch": {"participate_text": "old revision"}})["error"]["code"] == "REVISION_CONFLICT"


def test_scope_and_unknown_fields(controlled):
    _, service, _ = controlled
    service.access_path.write_text(json.dumps({"token": service.access()["token"], "scopes": ["app:read"]}))
    assert call(controlled, "account_logout", **writing(service))["error"]["code"] == "SCOPE_DENIED"
    assert call(controlled, "account_get", cookie_file="secret")["error"]["code"] == "VALIDATION_ERROR"


def test_job_mapping_does_not_confuse_same_action(controlled, monkeypatch):
    from web.job_runner import runner, JobStatus
    _, service, _ = controlled
    first = service.store.new_job("refresh_status", service.descriptor["boot_id"])
    second = service.store.new_job("refresh_status", service.descriptor["boot_id"])
    service.store.bind_job(first["job_id"], 101)
    service.store.bind_job(second["job_id"], 102)
    monkeypatch.setattr(runner, "resolve_job_status", lambda n: JobStatus(id=n, action="refresh_status", state="success" if n == 101 else "running"))
    assert call(controlled, "job_get", job_id=first["job_id"])["data"]["state"] == "success"
    assert call(controlled, "job_get", job_id=second["job_id"])["data"]["state"] == "running"


def test_store_pending_operations_never_replay(tmp_path):
    store = ControlStore(tmp_path)
    assert store.claim("test", {"a": 1}) is None
    with pytest.raises(ControlError, match="原请求"):
        store.claim("test", {"a": 1})
    with pytest.raises(ControlError, match="不同参数"):
        store.claim("test", {"a": 2})


def test_artifact_bounds_and_path_injection(tmp_path):
    store = ControlStore(tmp_path)
    item = store.artifact("safe.log", "abcdef")
    assert store.read_artifact(item["artifact_id"], 1, 2)["text"] == "bc"
    with pytest.raises(ControlError):
        store.read_artifact("../../config/cookies.txt")


def test_plan_expiry_account_and_settings_guard(controlled, monkeypatch):
    from datetime import datetime, timedelta, timezone
    _, service, _ = controlled
    monkeypatch.setattr("src.bilibili_auth.get_login_uid", lambda: 1)
    service.store.put("plan", "one", {"plan_id": "one", "boot_id": service.descriptor["boot_id"], "account_ref": "1", "settings_revision": service.settings()["revision"], "expires_at": (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
    with pytest.raises(ControlError, match="计划已过期"):
        service.get_plan("one")


def test_platform_telemetry_blocks_writes_in_read_only_mode():
    from src.control_telemetry import network_span, record_request
    with network_span(read_only=True) as counters:
        record_request("GET", "https://api.bilibili.com/x/web-interface/nav")
        with pytest.raises(RuntimeError, match="禁止平台写"):
            record_request("POST", "https://api.bilibili.com/x/dynamic/feed/create/dyn")
        assert counters["platform_read_requests"] == 1
        assert counters["platform_write_requests"] == 0
