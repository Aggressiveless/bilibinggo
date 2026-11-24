from __future__ import annotations

import pytest

from src.llm_settings import _resolve_base_url, mask_api_key


def test_mask_api_key() -> None:
    assert mask_api_key("") == ""
    assert mask_api_key("short") == "****"
    assert mask_api_key("abcdefghijklmnop") == "abcd****mnop"


def test_resolve_base_url_default() -> None:
    assert _resolve_base_url("") == "https://www.autodl.art/api/v1"


def test_resolve_base_url_valid() -> None:
    assert _resolve_base_url("https://api.example.com/v1") == "https://api.example.com/v1"


@pytest.mark.parametrize("bad_url", ["file:///etc/passwd", "ftp://example.com", "not-a-url"])
def test_resolve_base_url_rejects_invalid(bad_url: str) -> None:
    with pytest.raises(ValueError):
        _resolve_base_url(bad_url)


def test_saved_key_can_only_be_reused_for_the_same_origin(monkeypatch):
    from src.llm_settings import build_llm_config_from_inputs

    monkeypatch.setattr("src.llm_settings._read_values", lambda: {
        "LLM_API_KEY": "fake-test-key", "LLM_BASE_URL": "https://first.example/v1", "LLM_MODEL_NAME": "model",
    })
    assert build_llm_config_from_inputs(base_url="https://first.example:443/v2").api_key == "fake-test-key"
    with pytest.raises(ValueError, match="API Key"):
        build_llm_config_from_inputs(base_url="https://second.example/v1")
    with pytest.raises(ValueError, match="HTTPS"):
        build_llm_config_from_inputs(base_url="http://second.example/v1", api_key="new-test-key")
    assert build_llm_config_from_inputs(base_url="https://second.example/v1", api_key="new-test-key").api_key == "new-test-key"
