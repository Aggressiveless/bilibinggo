from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.db.engine import reset_engine_for_tests
from src.llm_client import chat_json
from src.llm_config import LlmConfig
from src.usage_stats import get_usage_stats, record_bilibili_request, record_llm_request
from web.usage_routes import router


def test_bilibili_counts_redirects_and_concurrent_requests(isolated_home):
    def response(request):
        if request.url.host == "b23.tv":
            return httpx.Response(302, headers={"location": "https://www.bilibili.com/opus/1"})
        return httpx.Response(200)

    with httpx.Client(
        transport=httpx.MockTransport(response),
        event_hooks={"request": [record_bilibili_request]},
        follow_redirects=True,
    ) as client, ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(client.get, ["https://b23.tv/test"] * 12))
        client.get("https://example.net")
    reset_engine_for_tests()
    stats = get_usage_stats(7)
    assert stats["totals"]["bilibili_requests"] == 24
    assert stats["daily"][-1]["bilibili_requests"] == 24
    assert sum(day["bilibili_requests"] for day in stats["daily"][:-1]) == 0


def test_model_retries_reported_cost_estimates_and_date_range(isolated_home, monkeypatch):
    config = LlmConfig("test-key", "https://openrouter.ai/api/v1", "test-model")
    monkeypatch.setattr("src.usage_stats.load_llm_config", lambda: config)
    monkeypatch.setattr("src.retry_utils.time.sleep", lambda _: None)
    real_client = httpx.Client
    replies = iter([
        (503, {}),
        (200, {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70, "cost": 0.000006},
        }),
    ])

    def respond(_):
        code, body = next(replies)
        return httpx.Response(code, json=body)

    with monkeypatch.context() as scoped:
        scoped.setattr("src.llm_client.httpx.Client", lambda **kwargs: real_client(
            transport=httpx.MockTransport(respond), **kwargs,
        ))
        assert chat_json(config=config, system="test", user="test") == {"ok": True}
    record_llm_request(config, date.today(), completion=False)
    record_llm_request(config, date.today() - timedelta(days=10), {
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "prompt_cache_hit_tokens": 400},
    })

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    payload = client.get("/api/usage?days=7").json()
    assert payload["totals"]["unpriced_calls"] == 2
    rate = {
        "model_key": payload["pricing"]["model_key"], "currency": "CNY",
        "input_rate": "2", "cached_input_rate": "0.5", "output_rate": "4",
    }
    assert client.put("/api/usage/pricing", json=rate).status_code == 200
    assert client.put("/api/usage/pricing", json={**rate, "input_rate": "-1"}).status_code == 422
    stats = get_usage_stats(7)
    totals = stats["totals"]
    assert totals["llm_requests"] == 4
    assert totals["total_tokens"] == 1570
    assert totals["missing_token_calls"] == totals["unpriced_calls"] == 1
    assert totals["costs"]["USD"] == {"reported": Decimal("0.000006"), "estimated": 0}
    assert totals["costs"]["CNY"] == {"reported": 0, "estimated": Decimal("0.0034")}
    assert sum(day["total_tokens"] for day in stats["daily"]) == 70
    assert get_usage_stats(30)["daily"][-11]["total_tokens"] == 1500
