from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))


@pytest.mark.parametrize("entry", json.loads((ROOT / "mcp/contracts/control-v2.tools.json").read_text(encoding="utf-8"))["tools"], ids=lambda e: e["name"])
def test_tool_contract_is_valid(entry):
    Draft202012Validator.check_schema(entry["inputSchema"])
    Draft202012Validator.check_schema(entry["outputSchema"])
    assert entry["inputSchema"]["additionalProperties"] is False


def test_legacy_coverage():
    from src.control_contract import catalog
    legacy = json.loads((ROOT / "mcp/contracts/legacy-v1.tools.json").read_text(encoding="utf-8"))
    aliases = [a for v in catalog().values() for a in v["x-legacyAliases"]]
    assert len(set(aliases)) == 34
    assert set(aliases) == {v["name"] for v in legacy}


def test_every_registered_offline_suite_is_callable():
    from binggo_mcp.runtime import Runtime
    from src.control_contract import catalog
    schema = catalog()["binggo_test_run"]["inputSchema"]
    permitted = schema["properties"]["suite_id"]["enum"]
    assert set(Runtime.suites()).issubset(permitted)


def test_local_control_client_ignores_broken_environment_proxy(tmp_path, monkeypatch):
    from binggo_mcp.runtime import Runtime
    import httpx
    home = tmp_path / "user"
    (home / "config").mkdir(parents=True)
    (home / "config/control-access.json").write_text(json.dumps({"token": "fixture", "scopes": ["app:read"]}))
    profile = {"user_home": str(home), "source_root": str(ROOT), "instance_id": "test", "scratch_root": str(tmp_path / "scratch")}
    runtime = Runtime(profile)
    descriptor = {"instance_id": "test", "boot_id": "boot", "endpoint": "http://127.0.0.1:8182"}
    (runtime.store.root / "instance.json").write_text(json.dumps(descriptor))
    original = httpx.AsyncClient
    def fake_client(*args, **kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json=descriptor))
        return original(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert asyncio.run(runtime.request("GET", "/api/control/v2/identity")) == descriptor
