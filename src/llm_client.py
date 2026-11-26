from __future__ import annotations

import json
import re
import threading
from datetime import date
from typing import Any

import httpx

from src.llm_config import LlmConfig, get_environment_llm_config
from src.llm_settings import list_llm_profiles, mark_llm_profile_exhausted
from src.retry_utils import is_transient_http_error, retry_call
from src.usage_stats import record_llm_request

_llm_lock = threading.Semaphore(3)
_LLM_TIMEOUT = 90.0
_LLM_RETRY_ATTEMPTS = 3
# 连接测试只需极短回复，但 DeepSeek V4 等模型在部分网关下 max_tokens=1 会触发 400。
_LLM_TEST_MAX_TOKENS = 64


class LlmRequestError(RuntimeError):
    """A completion or its response could not be processed."""


class LlmQuotaError(LlmRequestError):
    """The provider rejected the request because its billing quota is exhausted."""


_QUOTA_CODES = {
    "insufficient_quota",
    "insufficient_balance",
    "credit_balance_exhausted",
    "billing_hard_limit_reached",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "budgetlimitexceeded",
    "arrearage",
    "allocationquota.freetieronly",
}
_QUOTA_MESSAGE = re.compile(
    r"\binsufficient (?:account )?(?:balance|credits)\b|"
    r"\b(?:credit balance|free tier|billing quota) (?:has been |is )?exhausted\b|"
    r"余额不足|额度(?:已)?(?:耗尽|用完)|用完(?:免费)?额度"
)


def _is_quota_error(response: httpx.Response) -> bool:
    if response.status_code == 402:
        return True
    if response.status_code not in {400, 403, 429}:
        return False
    try:
        body = response.json()
    except ValueError:
        return bool(_QUOTA_MESSAGE.search(response.text[:250].lower()))
    if not isinstance(body, dict):
        return False
    for fields in (body, body.get("error"), body.get("detail")):
        if isinstance(fields, dict):
            if any(str(fields.get(key, "")).strip().lower() in _QUOTA_CODES for key in ("code", "type")):
                return True
            if _QUOTA_MESSAGE.search(str(fields.get("message", "")).lower()):
                return True
        elif isinstance(fields, str) and _QUOTA_MESSAGE.search(fields.lower()):
            return True
    return False


def _format_llm_http_error(exc: httpx.HTTPStatusError, config: LlmConfig) -> str:
    status = exc.response.status_code
    message = ""
    try:
        body = exc.response.json()
        if isinstance(body.get("error"), dict):
            message = str(body["error"].get("message") or "").strip()
        else:
            message = str(body.get("message") or body.get("detail") or "").strip()
    except Exception:
        message = (exc.response.text or "").strip()[:200]
    if config.api_key:
        message = message.replace(config.api_key, "****")
    if status == 401:
        return f"API Key 无效或已过期{('：' + message) if message else ''}"
    if status == 404:
        return f"接口地址或模型不存在{('：' + message) if message else ''}"
    if status in {408, 429, 500, 502, 503, 504}:
        return f"LLM 服务暂时不可用（HTTP {status}）{('：' + message) if message else ''}"
    return f"连接失败（HTTP {status}）{('：' + message) if message else ''}"


def _llm_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError) and _is_quota_error(exc.response):
        return False
    if isinstance(exc, (json.JSONDecodeError, RuntimeError)):
        message = str(exc)
        if "无法解析为 JSON" in message or "不是对象" in message:
            return True
    return is_transient_http_error(exc)


def _supports_thinking_toggle(config: LlmConfig) -> bool:
    base = (config.base_url or "").lower()
    model = (config.model_name or "").lower()
    return "deepseek" in base or "deepseek" in model


def _build_chat_payload(
    config: LlmConfig,
    *,
    messages: list[dict[str, str]],
    temperature: float = 0,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model_name,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    # DeepSeek V4 系列默认开启 thinking，结构化抽取不需要链式推理。
    if _supports_thinking_toggle(config):
        payload["thinking"] = {"type": "disabled"}
    return payload


def _post_chat_completion(
    *, config: LlmConfig, headers: dict[str, str], payload: dict[str, Any],
) -> dict[str, Any]:
    def _call() -> dict[str, Any]:
        with httpx.Client(timeout=_LLM_TIMEOUT) as client:
            day = date.today()
            data = None
            try:
                resp = client.post(f"{config.base_url}/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data
            finally:
                record_llm_request(config, day, data)

    return retry_call(_call, attempts=_LLM_RETRY_ATTEMPTS, retry_on=_llm_retryable)


def test_llm_connection(config: LlmConfig) -> str:
    payload = _build_chat_payload(
        config,
        messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        temperature=0,
        max_tokens=_LLM_TEST_MAX_TOKENS,
    )
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    try:
        data = _post_chat_completion(config=config, headers=headers, payload=payload)
    except httpx.HTTPStatusError as exc:
        if _is_quota_error(exc.response):
            raise LlmQuotaError("该方案的余额或额度已用尽") from exc
        raise RuntimeError(_format_llm_http_error(exc, config)) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"无法连接接口地址：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise LlmRequestError("LLM 接口返回的响应不是有效 JSON") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LlmRequestError("LLM 接口未返回有效的模型回复")
    if not isinstance(choices[0].get("message"), dict):
        raise LlmRequestError("LLM 接口未返回有效的模型回复")
    return f"{config.model_name} @ {config.base_url}"


def _chat_completion(config: LlmConfig, *, system: str, user: str) -> dict[str, Any]:
    payload = _build_chat_payload(
        config,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    try:
        return _post_chat_completion(config=config, headers=headers, payload=payload)
    except httpx.HTTPStatusError as exc:
        if _is_quota_error(exc.response):
            raise LlmQuotaError("该方案的余额或额度已用尽") from exc
        error = _format_llm_http_error(exc, config)
        if exc.response.status_code in {401, 403, 404}:
            raise RuntimeError(error) from exc
        raise LlmRequestError(error) from exc
    except httpx.RequestError as exc:
        raise LlmRequestError(f"无法连接 LLM 接口：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise LlmRequestError("LLM 接口返回的响应不是有效 JSON") from exc


def _chat_with_saved_profiles(*, system: str, user: str) -> dict[str, Any]:
    attempted = set()
    while True:
        profiles = list_llm_profiles()
        profile = next((item for item in profiles if not item.exhausted and item.profile_id not in attempted), None)
        if profile is None:
            if profiles or attempted:
                raise LlmQuotaError("所有 LLM 方案的余额或额度已用尽。请充值后测试连接，或新增方案")
            raise RuntimeError("未配置 LLM，请检查 config/llm.env")
        try:
            return _chat_completion(profile.config, system=system, user=user)
        except LlmQuotaError:
            attempted.add(profile.profile_id)
            mark_llm_profile_exhausted(profile)


def chat_json(*, system: str, user: str, config: LlmConfig | None = None) -> dict[str, Any]:
    with _llm_lock:
        explicit = config or get_environment_llm_config()
        if explicit:
            data = _chat_completion(explicit, system=system, user=user)
        else:
            data = _chat_with_saved_profiles(system=system, user=user)

    try:
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return retry_call(lambda: _extract_json_object(content), attempts=2, base_delay=0.5, multiplier=1.5)
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise LlmRequestError("LLM 接口返回的响应格式不完整") from exc
    except json.JSONDecodeError as exc:
        raise LlmRequestError("LLM 返回无法解析为 JSON") from exc
    except RuntimeError as exc:
        raise LlmRequestError(str(exc)) from exc


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise RuntimeError(f"LLM 返回无法解析为 JSON: {text[:200]}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM 返回的 JSON 不是对象")
    return parsed
