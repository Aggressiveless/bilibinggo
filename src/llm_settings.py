from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from src.llm_config import LlmConfig, _parse_env_file, get_environment_llm_config
from src.secure_files import write_text_secret

DEFAULT_LLM_BASE_URL = "https://www.autodl.art/api/v1"
DEFAULT_LLM_MODEL_NAME = "DeepSeek-V4-Flash"

_MODEL_NAME_MAX_LEN = 200
_PROFILE_NAME_MAX_LEN = 80
_SETTINGS_LOCK = RLock()


@dataclass(frozen=True)
class LlmProfile:
    profile_id: str
    name: str
    config: LlmConfig
    test_passed: bool
    exhausted: bool
    revision: str


def _llm_env_path() -> Path:
    from src import app_paths

    return app_paths.llm_env_file()


def _read_values() -> dict[str, str]:
    return _parse_env_file(_llm_env_path())


def _config_fingerprint(api_key: str, base_url: str, model_name: str) -> str:
    payload = f"{api_key}|{base_url}|{model_name}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _legacy_test_passed(values: dict[str, str]) -> bool:
    if values.get("LLM_TEST_PASSED", "").strip().lower() not in {"1", "true", "yes"}:
        return False
    return values.get("LLM_TEST_FINGERPRINT", "").strip() == _config_fingerprint(
        values.get("LLM_API_KEY", "").strip(),
        values.get("LLM_BASE_URL", "").strip(),
        values.get("LLM_MODEL_NAME", "").strip(),
    )


def _legacy_profile(values: dict[str, str]) -> LlmProfile | None:
    key = values.get("LLM_API_KEY", "").strip()
    model = values.get("LLM_MODEL_NAME", "").strip()
    if not key or not model:
        return None
    try:
        base = _resolve_base_url(values.get("LLM_BASE_URL", ""))
    except ValueError:
        return None
    return LlmProfile(
        "default", "原有配置", LlmConfig(key, base, model),
        test_passed=_legacy_test_passed(values), exhausted=False, revision="legacy",
    )


def _read_profiles(values: dict[str, str]) -> list[LlmProfile]:
    primary = _legacy_profile(values)
    encoded = values.get("LLM_PROFILES_JSON")
    if not encoded:
        return [primary] if primary else []
    try:
        rows = json.loads(encoded)
        if not isinstance(rows, list):
            raise ValueError
        backups = [
            LlmProfile(**{**row, "config": LlmConfig(**row["config"])})
            for row in rows[1:]
        ]
        if primary and rows:
            metadata = rows[0]
            config = primary.config
            fingerprint = _config_fingerprint(config.api_key, config.base_url, config.model_name)
            primary = replace(
                primary,
                profile_id=metadata["profile_id"],
                name=metadata["name"],
                exhausted=metadata["exhausted"] and metadata["fingerprint"] == fingerprint,
                revision=metadata["revision"],
            )
        return [primary, *backups] if primary else backups
    except (KeyError, TypeError, ValueError):
        raise ValueError("LLM 方案文件格式无效") from None


def _write_profiles(profiles: list[LlmProfile]) -> None:
    # Keep the first scheme in the original fields so existing llm.env edits still apply.
    primary = profiles[0] if profiles else None
    config = primary.config if primary else LlmConfig("", "", "")
    tested = primary is not None and primary.test_passed
    fingerprint = _config_fingerprint(config.api_key, config.base_url, config.model_name)
    rows = [asdict(profile) for profile in profiles]
    if rows:
        rows[0].pop("config")
        rows[0].pop("test_passed")
        rows[0]["fingerprint"] = fingerprint
    lines = [
        f"LLM_API_KEY={config.api_key}",
        f"LLM_BASE_URL={config.base_url}",
        f"LLM_MODEL_NAME={config.model_name}",
        f"LLM_TEST_PASSED={'true' if tested else 'false'}",
        f"LLM_TEST_FINGERPRINT={fingerprint if tested else ''}",
        f"LLM_PROFILES_JSON={json.dumps(rows, separators=(',', ':'))}",
        "",
    ]
    write_text_secret(_llm_env_path(), "\n".join(lines))


def mask_api_key(api_key: str) -> str:
    cleaned = (api_key or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "****"
    return f"{cleaned[:4]}****{cleaned[-4:]}"


def load_llm_values() -> dict[str, str]:
    values = _read_values()
    values.pop("LLM_PROFILES_JSON", None)
    return values


def list_llm_profiles() -> list[LlmProfile]:
    return _read_profiles(_read_values())


def is_llm_configured() -> bool:
    return bool(list_llm_profiles())


def is_llm_tested() -> bool:
    return any(profile.test_passed for profile in list_llm_profiles())


def is_llm_ready() -> bool:
    return is_llm_tested()


def _resolve_base_url(raw: str) -> str:
    cleaned = (raw or "").strip().rstrip("/")
    if not cleaned:
        return DEFAULT_LLM_BASE_URL
    if any(char.isspace() for char in cleaned):
        raise ValueError("LLM 接口地址格式无效")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("LLM 接口地址仅支持 http 或 https")
    if not parsed.hostname or parsed.username is not None or parsed.query or parsed.fragment:
        raise ValueError("LLM 接口地址格式无效")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("远程 LLM 接口须使用 HTTPS")
    _ = parsed.port
    return cleaned


def _origin(url: str) -> tuple[str, str | None, int]:
    parsed = urlparse(_resolve_base_url(url))
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _build_config(
    current: LlmConfig,
    *,
    api_key: str | None,
    base_url: str | None,
    model_name: str,
) -> LlmConfig:
    base = _resolve_base_url((base_url or "").strip() or current.base_url)
    key = (api_key or "").strip()
    if not key and current.api_key:
        if _origin(current.base_url) != _origin(base):
            raise ValueError("更换 LLM 服务地址时，请填写该服务的 API Key")
        key = current.api_key
    model = model_name.strip()
    if not key or not model:
        raise ValueError("请填写 API Key 与模型名称")
    if re.search(r"\s|[\x00-\x1f\x7f]", key):
        raise ValueError("API Key 格式无效")
    if len(model) > _MODEL_NAME_MAX_LEN or len(model.splitlines()) != 1:
        raise ValueError(f"模型名称须为一行，最多 {_MODEL_NAME_MAX_LEN} 字符")
    return LlmConfig(key, base, model)


def _legacy_config(values: dict[str, str]) -> LlmConfig:
    return LlmConfig(values.get("LLM_API_KEY", ""), values.get("LLM_BASE_URL", ""), values.get("LLM_MODEL_NAME", ""))


def _find_profile(profiles: list[LlmProfile], profile_id: str) -> LlmProfile:
    for profile in profiles:
        if profile.profile_id == profile_id:
            return profile
    raise ValueError("该 LLM 方案已不存在，请刷新配置")


def get_llm_config() -> LlmConfig | None:
    return next((profile.config for profile in list_llm_profiles() if not profile.exhausted), None)


def _public_settings(values: dict[str, str], profiles: list[LlmProfile]) -> dict[str, Any]:
    env_override = get_environment_llm_config() is not None
    active = next((profile.profile_id for profile in profiles if not profile.exhausted), None)
    tested = any(profile.test_passed for profile in profiles)
    primary = profiles[0].config if profiles else _legacy_config(values)
    return {
        "configured": bool(profiles),
        "test_passed": tested,
        "ready": tested,
        "base_url": primary.base_url,
        "model_name": primary.model_name,
        "api_key_hint": mask_api_key(primary.api_key),
        "active_profile_id": None if env_override else active,
        "environment_override": env_override,
        "profiles": [
            {
                "id": profile.profile_id,
                "name": profile.name,
                "base_url": profile.config.base_url,
                "model_name": profile.config.model_name,
                "api_key_hint": mask_api_key(profile.config.api_key),
                "test_passed": profile.test_passed,
                "exhausted": profile.exhausted,
            }
            for profile in profiles
        ],
    }


def get_llm_settings_public() -> dict[str, Any]:
    values = _read_values()
    return _public_settings(values, _read_profiles(values))


def build_llm_config_from_inputs(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model_name: str | None = None,
    current: LlmConfig | None = None,
) -> LlmConfig:
    current = current or _legacy_config(_read_values())
    return _build_config(current, api_key=api_key, base_url=base_url,
                         model_name=(model_name or "").strip() or current.model_name)


def _save_profile(
    *,
    profile_id: str | None,
    name: str | None,
    api_key: str | None,
    base_url: str,
    model_name: str,
    legacy: bool,
) -> dict[str, Any]:
    with _SETTINGS_LOCK:
        values = _read_values()
        profiles = _read_profiles(values)
        previous = (profiles[0] if profiles else None) if legacy else (
            _find_profile(profiles, profile_id) if profile_id else None
        )
        current = previous.config if previous else (
            _legacy_config(values) if not profiles else LlmConfig("", "", "")
        )
        config = _build_config(current, api_key=api_key, base_url=base_url, model_name=model_name)
        label = (name or "").strip()
        if len(label) > _PROFILE_NAME_MAX_LEN or len(label.splitlines()) > 1:
            raise ValueError(f"方案名称须为一行，最多 {_PROFILE_NAME_MAX_LEN} 字符")
        label = label or (previous.name if previous else config.model_name[:_PROFILE_NAME_MAX_LEN])
        if previous:
            profile = replace(previous, name=label)
            if previous.config != config:
                profile = replace(profile, config=config, test_passed=False, exhausted=False, revision=uuid4().hex)
            profiles[profiles.index(previous)] = profile
        else:
            profile = LlmProfile(uuid4().hex, label, config, False, False, uuid4().hex)
            profiles.append(profile)
        _write_profiles(profiles)
        return {**_public_settings(values, profiles), "saved_profile_id": profile.profile_id}


def save_llm_settings(*, api_key: str | None, base_url: str, model_name: str) -> dict[str, Any]:
    return _save_profile(
        profile_id=None, name=None, api_key=api_key, base_url=base_url, model_name=model_name, legacy=True,
    )


def save_llm_profile(
    *, profile_id: str | None, name: str, api_key: str | None, base_url: str, model_name: str,
) -> dict[str, Any]:
    return _save_profile(
        profile_id=profile_id, name=name, api_key=api_key, base_url=base_url, model_name=model_name, legacy=False,
    )


def reorder_llm_profiles(profile_ids: list[str]) -> dict[str, Any]:
    with _SETTINGS_LOCK:
        profiles = {profile.profile_id: profile for profile in list_llm_profiles()}
        if set(profile_ids) != profiles.keys() or len(profile_ids) != len(profiles):
            raise ValueError("方案列表已变更，请刷新配置后重新排序")
        ordered = [profiles[profile_id] for profile_id in profile_ids]
        _write_profiles(ordered)
        return _public_settings({}, ordered)


def delete_llm_profile(profile_id: str) -> dict[str, Any]:
    with _SETTINGS_LOCK:
        profiles = list_llm_profiles()
        profiles.remove(_find_profile(profiles, profile_id))
        _write_profiles(profiles)
        return _public_settings({}, profiles)


def prepare_llm_test(
    *, profile_id: str | None, api_key: str, base_url: str, model_name: str,
) -> LlmProfile:
    profiles = list_llm_profiles()
    if not profiles:
        raise ValueError("请先保存完整的 LLM 配置")
    profile = _find_profile(profiles, profile_id) if profile_id else profiles[0]
    config = build_llm_config_from_inputs(
        api_key=api_key, base_url=base_url, model_name=model_name, current=profile.config,
    )
    if config != profile.config:
        raise ValueError("请先保存配置，再执行连接测试")
    return profile


def mark_llm_test_passed(tested: LlmProfile) -> dict[str, Any]:
    with _SETTINGS_LOCK:
        profiles = list_llm_profiles()
        profile = _find_profile(profiles, tested.profile_id)
        if (profile.revision, profile.config) != (tested.revision, tested.config):
            raise ValueError("测试期间方案已变更，请重新测试")
        profiles[profiles.index(profile)] = replace(
            profile, test_passed=True, exhausted=False, revision=uuid4().hex,
        )
        _write_profiles(profiles)
        return _public_settings({}, profiles)


def mark_llm_profile_exhausted(attempted: LlmProfile) -> None:
    with _SETTINGS_LOCK:
        profiles = list_llm_profiles()
        for index, profile in enumerate(profiles):
            if (profile.profile_id, profile.revision, profile.config) == (
                attempted.profile_id, attempted.revision, attempted.config,
            ) and not profile.exhausted:
                profiles[index] = replace(profile, exhausted=True)
                _write_profiles(profiles)
                break
