from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from src.bilibili_client import BilibiliClient
from src.dead_links import is_dynamic_deleted
from src.lottery_api import DYNAMIC_DETAIL_URL, LOTTERY_NOTICE_URL
from src.pipeline.classify_fetch_context import ClassifyFetchContext
from src.pipeline.refresh_all_pipeline import run_new_links_pipeline
from src.sources.common import opus_link


def test_is_deleted_link_reuses_detail_probe(monkeypatch) -> None:
    calls = {"n": 0}

    class _Client:
        pass

    def fake_probe(client, dynamic_id, *, retries=2):
        calls["n"] += 1
        return None, 0, "0"

    monkeypatch.setattr(
        "src.pipeline.classify_fetch_context.probe_dynamic_detail_api",
        fake_probe,
    )
    monkeypatch.setattr(
        "src.pipeline.classify_fetch_context.is_detail_api_enabled",
        lambda: True,
    )

    ctx = ClassifyFetchContext(_Client(), "123")
    assert ctx.unavailable_reason() == "链接失效"
    assert ctx.get_additional() is None
    assert calls["n"] == 1


def test_source_update_skips_invisible_and_404_links_then_saves_healthy_link(isolated_home, monkeypatch):
    invisible = "1252602820816797718"
    missing = "1216563483908243495"
    healthy = "1252602820816797720"

    def http_get(url, *, params=None, headers=None):
        request = httpx.Request("GET", url)
        if url == DYNAMIC_DETAIL_URL:
            if params["id"] == invisible:
                return httpx.Response(200, request=request, json={"code": 4101152, "message": "动态不可见"})
            if params["id"] == missing:
                return httpx.Response(200, request=request, json={"code": -500, "message": "详情暂不可用"})
            assert params["id"] == healthy
            return httpx.Response(200, request=request, json={"code": 0, "data": {"item": {"modules": {}}}})
        if url == LOTTERY_NOTICE_URL:
            assert params["business_id"] != invisible, "不可见的链接应直接跳过"
            notice = {"lottery_id": "1"} if params["business_id"] == healthy else {}
            return httpx.Response(200, request=request, json={"code": 0, "data": notice})
        assert url == opus_link(missing)
        return httpx.Response(404, request=request)

    def enrich(client, *, dynamic_id, **kwargs):
        assert dynamic_id == healthy
        return SimpleNamespace(skipped=False, to_dict=lambda: {"dynamic_id": dynamic_id})

    saved = []
    monkeypatch.setattr("src.bilibili_client.time.sleep", lambda seconds: None)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.enrich_activity", enrich)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.apply_initial_status", lambda row: row)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.append_activities", lambda rows: saved.extend(rows) or len(rows))

    with BilibiliClient(warmup=False) as client:
        monkeypatch.setattr(client, "_http_get", http_get)
        assert not is_dynamic_deleted(client, invisible)
        result = run_new_links_pipeline([opus_link(i) for i in (invisible, missing, healthy)], client=client)

    assert result.ok and result.persisted_count == 1
    assert result.skip_reasons == {"动态不可见": 1, "活动页 404": 1}
    assert saved == [{"dynamic_id": healthy}]


def test_reserve_fallback_does_not_treat_rate_limit_as_missing():
    def get_text(url, **kwargs):
        httpx.Response(429, request=httpx.Request("GET", url)).raise_for_status()

    ctx = ClassifyFetchContext(SimpleNamespace(get_text=get_text), "1252602820816797718")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        ctx.classify_reserve_candidate(None)
    assert exc.value.response.status_code == 429
