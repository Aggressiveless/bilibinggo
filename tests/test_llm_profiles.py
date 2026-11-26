from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from src import llm_client, llm_settings
from src.app_paths import llm_env_file


def _save(name: str, key: str, base: str = "https://first.example/v1", model: str = "model") -> str:
    result = llm_settings.save_llm_profile(
        profile_id=None, name=name, api_key=key, base_url=base, model_name=model,
    )
    return result["saved_profile_id"]


@pytest.fixture
def mock_provider(monkeypatch, isolated_home):
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm_client, "record_llm_request", lambda *args: None)
    monkeypatch.setattr("src.retry_utils.time.sleep", lambda _: None)
    real_client = httpx.Client
    calls = []

    def install(responses):
        def respond(request):
            key = request.headers["Authorization"].removeprefix("Bearer ")
            payload = json.loads(request.content)
            calls.append((key, request.url.host, payload["model"], "thinking" in payload))
            status, body = responses[key]
            return httpx.Response(status, json=body)

        transport = httpx.MockTransport(respond)
        monkeypatch.setattr(llm_client.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
        return calls

    return install


def test_quota_failover_persists_and_retest_restores_priority(mock_provider):
    backup = _save("Backup", "backup-fake-key", "https://second.example/v1", "backup-model")
    preferred = _save("Preferred", "preferred-fake-key", model="DeepSeek-Flash")
    llm_settings.reorder_llm_profiles([preferred, backup])

    success = {"choices": [{"message": {"content": '{"ok": true}'}}]}
    responses = {
        "preferred-fake-key": (429, {"error": {"code": "credit_balance_exhausted"}}),
        "backup-fake-key": (200, success),
    }
    calls = mock_provider(responses)

    assert llm_client.chat_json(system="test", user="test") == {"ok": True}
    assert calls == [
        ("preferred-fake-key", "first.example", "DeepSeek-Flash", True),
        ("backup-fake-key", "second.example", "backup-model", False),
    ]
    public = llm_settings.get_llm_settings_public()
    assert public["active_profile_id"] == backup
    assert public["profiles"][0]["exhausted"]
    assert "preferred-fake-key" not in json.dumps(public)
    assert "backup-fake-key" not in json.dumps(public)

    calls.clear()
    assert llm_client.chat_json(system="test", user="test") == {"ok": True}
    assert [call[0] for call in calls] == ["backup-fake-key"]

    responses["backup-fake-key"] = (403, {"code": "AllocationQuota.FreeTierOnly"})
    with pytest.raises(llm_client.LlmQuotaError, match="所有 LLM 方案"):
        llm_client.chat_json(system="test", user="test")
    assert llm_settings.get_llm_settings_public()["active_profile_id"] is None
    count = len(calls)
    with pytest.raises(llm_client.LlmQuotaError):
        llm_client.chat_json(system="test", user="test")
    assert len(calls) == count

    responses["preferred-fake-key"] = (200, success)
    tested = llm_settings.prepare_llm_test(profile_id=preferred, api_key="", base_url="", model_name="")
    llm_client.test_llm_connection(tested.config)
    restored = llm_settings.mark_llm_test_passed(tested)
    # An older in-flight rejection cannot exhaust a restored scheme.
    llm_settings.mark_llm_profile_exhausted(tested)
    assert restored["active_profile_id"] == preferred
    assert llm_settings.get_llm_settings_public()["active_profile_id"] == preferred


@pytest.mark.parametrize(
    ("status", "error", "attempts"),
    [
        (429, {"code": "rate_limit_exceeded", "message": "Rate limit exceeded"}, 3),
        (401, {"code": "invalid_api_key"}, 1),
        (500, {"message": "Internal server error"}, 3),
    ],
)
def test_non_billing_errors_keep_retrying_the_current_scheme(mock_provider, status, error, attempts):
    first = _save("First", "first-fake-key")
    _save("Backup", "backup-fake-key")
    calls = mock_provider({"first-fake-key": (status, {"error": error})})

    with pytest.raises(RuntimeError):
        llm_client.chat_json(system="test", user="test")
    assert [call[0] for call in calls] == ["first-fake-key"] * attempts
    assert llm_settings.get_llm_settings_public()["active_profile_id"] == first


def test_connection_test_requires_a_model_reply(mock_provider):
    profile_id = _save("First", "first-fake-key")
    mock_provider({"first-fake-key": (200, {"error": {"message": "Upstream unavailable"}})})
    profile = llm_settings.prepare_llm_test(profile_id=profile_id, api_key="", base_url="", model_name="")
    with pytest.raises(llm_client.LlmRequestError, match="未返回有效"):
        llm_client.test_llm_connection(profile.config)


def test_manual_primary_key_change_clears_exhaustion_and_keeps_backups(mock_provider):
    primary_id = _save("First", "first-fake-key")
    backup_id = _save("Backup", "backup-fake-key")
    llm_settings.mark_llm_profile_exhausted(llm_settings.list_llm_profiles()[0])
    assert llm_settings.get_llm_settings_public()["active_profile_id"] == backup_id

    path = llm_env_file()
    contents = path.read_text(encoding="utf-8")
    assert contents.count("first-fake-key") == 1
    path.write_text(contents.replace("first-fake-key", "replacement-fake-key"), encoding="utf-8")
    assert llm_settings.get_llm_settings_public()["active_profile_id"] == primary_id
    assert llm_settings.get_llm_config().api_key == "replacement-fake-key"


def test_environment_override_keeps_its_existing_precedence(mock_provider, monkeypatch):
    _save("Saved", "saved-fake-key")
    monkeypatch.setenv("LLM_API_KEY", "env-fake-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "env-model")
    calls = mock_provider({"env-fake-key": (402, {})})

    with pytest.raises(llm_client.LlmQuotaError):
        llm_client.chat_json(system="test", user="test")
    assert [call[0] for call in calls] == ["env-fake-key"]
    assert llm_settings.get_llm_settings_public()["environment_override"]
    assert not llm_settings.list_llm_profiles()[0].exhausted


def test_settings_routes_manage_legacy_and_new_schemes(isolated_home, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "get_account_profile", lambda: {"logged_in": True})
    key = "legacy-fake-key"
    fingerprint = llm_settings._config_fingerprint(key, "", "legacy-model")
    llm_env_file().write_text(
        f"LLM_API_KEY={key}\nLLM_MODEL_NAME=legacy-model\n"
        f"LLM_TEST_PASSED=true\nLLM_TEST_FINGERPRINT={fingerprint}\n",
        encoding="utf-8",
    )
    api = "/api/settings/llm"
    client = TestClient(web_app.app, headers={"X-Binggo-Dashboard": "1"})
    original = client.get(api).json()["llm"]
    assert original["ready"]
    assert original["profiles"][0]["id"] == "default"
    assert original["profiles"][0]["test_passed"]

    created = client.post(api + "/profiles", json={
        "name": "Backup", "api_key": "backup-fake-key", "model_name": "backup-model",
    })
    assert created.status_code == 200
    assert "backup-fake-key" not in created.text
    backup = created.json()["llm"]["saved_profile_id"]
    assert client.put(api + "/profiles/order", json={"profile_ids": [backup, backup]}).status_code == 400
    ordered = client.put(api + "/profiles/order", json={"profile_ids": [backup, "default"]}).json()["llm"]
    assert ordered["active_profile_id"] == backup
    assert ordered["profiles"][1]["test_passed"]

    tested = []
    monkeypatch.setattr(web_app, "test_llm_connection", lambda config: tested.append(config.model_name) or "ok")
    assert client.post(api + "/test", json={"profile_id": backup}).status_code == 200
    assert tested == ["backup-model"]
    assert client.post(api + "/profiles", json={
        "profile_id": backup, "name": "Changed", "base_url": "https://new.example/v1", "model_name": "backup-model",
    }).status_code == 400
    after_delete = client.delete(api + f"/profiles/{backup}").json()["llm"]
    assert [profile["id"] for profile in after_delete["profiles"]] == ["default"]
    assert "backup-fake-key" not in llm_env_file().read_text(encoding="utf-8")
    assert client.delete(api + "/profiles/default").json()["llm"]["profiles"] == []
