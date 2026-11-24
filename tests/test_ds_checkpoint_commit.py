from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.activity_store import known_activity_ids
from src.db.engine import reset_engine_for_tests
from src.forward_parser import DynamicContentFetchError, fetch_dynamic_content_with_retry
from src.lottery_api import ActivityDetailUnavailable
from src.lottery_enricher import EnrichedActivity, PrizeTier
from src.pipeline.classify_step import ClassifyOutcome
from src.pipeline.refresh_all_pipeline import PipelineResult
from src.sources.common import CheckResult, commit_source_checkpoint, opus_link
from src.sources.pending_links import stage_source_links
from src.state_store import get_last_container, get_last_cv_id, save_state
from web.actions import run_action


def _updated_result(*, source_id: str = "DS-2", cv_id: str | None = None) -> CheckResult:
    return CheckResult(
        source_id=source_id,
        updated=True,
        container_url="https://www.bilibili.com/read/cv999001",
        container_id="999001" if cv_id is None else "opus-123",
        title="新专栏",
        published_at=1_700_000_000,
        previous_container_url="https://www.bilibili.com/read/cv888001",
        activity_links=["https://www.bilibili.com/opus/1220000000000000001"],
        checked_at=1_700_000_100,
        link_hints={},
        cv_id=cv_id,
    )


def test_commit_source_checkpoint_writes_only_when_updated(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = isolated_home
    save_state({"sources": {}})

    unchanged = _updated_result()
    unchanged.updated = False
    commit_source_checkpoint(unchanged)
    assert get_last_container("DS-2") is None

    commit_source_checkpoint(_updated_result())
    assert get_last_container("DS-2") == "https://www.bilibili.com/read/cv999001"


def test_commit_source_checkpoint_keeps_cv_id(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = isolated_home
    save_state({"sources": {}})

    commit_source_checkpoint(
        _updated_result(source_id="DS-5", cv_id="555001"),
    )
    assert get_last_container("DS-5") == "https://www.bilibili.com/read/cv999001"
    assert get_last_cv_id("DS-5") == "555001"


def test_ds2_check_update_does_not_write_checkpoint(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sources.ds2_fanqiao.DATA_DIR", isolated_home / "data")
    monkeypatch.setattr(
        "src.sources.ds2_fanqiao.OUTPUT_PATH", isolated_home / "data" / "ds2_latest.json"
    )
    save_state(
        {
            "sources": {
                "DS-2": {"container_url": "https://www.bilibili.com/read/cv888001"},
            }
        }
    )

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get_latest_article.return_value = {
        "id": 999001,
        "title": "新专栏",
        "publish_time": 1_700_000_000,
    }
    fake_client.get_article_detail.return_value = {
        "title": "新专栏",
        "publish_time": 1_700_000_000,
        "opus": {"content": {"paragraphs": []}},
    }

    with (
        patch("src.sources.ds2_fanqiao.BilibiliClient", return_value=fake_client),
        patch(
            "src.sources.ds2_fanqiao.extract_opus_links_with_hints",
            return_value=(["https://www.bilibili.com/opus/1220000000000000001"], {}),
        ),
    ):
        from src.sources import ds2_fanqiao

        assert "set_last_container" not in ds2_fanqiao.__dict__
        result = ds2_fanqiao.check_update()

    assert result.updated is True
    assert result.container_url == "https://www.bilibili.com/read/cv999001"
    assert get_last_container("DS-2") == "https://www.bilibili.com/read/cv888001"


def test_refresh_source_commits_checkpoint_only_after_pipeline_success(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = isolated_home
    save_state(
        {
            "sources": {
                "DS-3": {"container_url": "https://www.bilibili.com/read/cv888001"},
            }
        }
    )

    check_result = _updated_result(source_id="DS-3")
    pipeline_ok = PipelineResult(
        ok=True,
        pipeline_skipped=False,
        raw_link_count=1,
        new_link_count=1,
        classified_count=1,
        skipped_count=0,
        enriched_count=1,
        persisted_count=1,
        message="流水线完成",
    )

    def fake_run_ds_check(index, source_id, check_update, save_result):
        payload = {
            "source_id": source_id,
            "updated": True,
            "link_count": 1,
            "saved": True,
            "status_text": "发现新专栏，已爬取",
        }
        return index, payload, "log", check_result

    with (
        patch("web.actions._run_ds_check", side_effect=fake_run_ds_check),
        patch("web.actions.run_refresh_all_pipeline", return_value=pipeline_ok) as pipeline_mock,
        patch("web.actions.invalidate_activity_cache"),
        patch("web.actions.set_last_pipeline_persisted"),
    ):
        payload = run_action("refresh_source", {"source_id": "DS-3"})

    assert payload["ok"] is True
    pipeline_mock.assert_called_once()
    assert get_last_container("DS-3") == "https://www.bilibili.com/read/cv999001"


def test_refresh_source_keeps_checkpoint_when_pipeline_fails(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = isolated_home
    save_state(
        {
            "sources": {
                "DS-3": {"container_url": "https://www.bilibili.com/read/cv888001"},
            }
        }
    )

    check_result = _updated_result(source_id="DS-3")

    def fake_run_ds_check(index, source_id, check_update, save_result):
        payload = {
            "source_id": source_id,
            "updated": True,
            "link_count": 1,
            "saved": True,
            "status_text": "发现新专栏，已爬取",
        }
        return index, payload, "log", check_result

    with (
        patch("web.actions._run_ds_check", side_effect=fake_run_ds_check),
        patch(
            "web.actions.run_refresh_all_pipeline",
            side_effect=RuntimeError("无法获取动态正文: 1220000000000000001"),
        ),
        patch("web.actions.invalidate_activity_cache"),
        patch("web.actions.set_last_pipeline_persisted"),
    ):
        with pytest.raises(RuntimeError, match="无法获取动态正文"):
            run_action("refresh_source", {"source_id": "DS-3"})

    assert get_last_container("DS-3") == "https://www.bilibili.com/read/cv888001"
    assert stage_source_links(["DS-3"], []) == {"DS-3": ["1220000000000000001"]}


def test_refresh_keeps_going_then_retries_only_pending_links(isolated_home, monkeypatch):
    body_failure, request_failure, unavailable, detail_failure, healthy = (
        "1220000000000000001", "1220000000000000002", "1220000000000000003",
        "1220000000000000004", "1220000000000000005",
    )
    save_state({"sources": {"DS-2": {"container_url": "https://www.bilibili.com/read/cv888001"}}})
    results = {"DS-1": _updated_result(source_id="DS-1"), "DS-3": _updated_result(source_id="DS-3")}
    wave = [body_failure, request_failure, unavailable, detail_failure, healthy]
    results["DS-1"].activity_links = [opus_link(did) for did in wave]
    # 两个来源引用同一条动态，统一分类并保存重试状态。
    results["DS-3"].activity_links = [opus_link(body_failure)]
    network_restored = False
    classified = []

    def check(index, source_id, *_):
        if source_id == "DS-2":
            raise RuntimeError("来源请求超时")
        result = results.get(source_id) or _updated_result(source_id=source_id)
        if source_id not in results or network_restored:
            result.updated = False
        payload = {
            "source_id": source_id, "updated": result.updated, "link_count": len(result.activity_links),
            "saved": True, "status_text": "检查完成",
        }
        return index, payload, "检查完成", result

    def no_body(*_, **__):
        raise DynamicContentFetchError("各抓取路径均未获得正文")

    def time_out(*_, **__):
        raise httpx.ReadTimeout("活动接口超时")

    def classify(client, dynamic_id):
        classified.append(dynamic_id)
        if dynamic_id == body_failure and not network_restored:
            fetch_dynamic_content_with_retry(client, dynamic_id)
        if dynamic_id == request_failure and not network_restored:
            client.request_json("https://api.bilibili.com/lottery", retries=0)
        if dynamic_id == unavailable:
            return ClassifyOutcome(dynamic_id, None, True, "动态不可见")
        return ClassifyOutcome(dynamic_id, "转发抽奖", False)

    def enrich(client, *, dynamic_id, **_):
        if dynamic_id == detail_failure and not network_restored:
            raise ActivityDetailUnavailable("活动详情暂不可用")
        return EnrichedActivity(
            dynamic_id=dynamic_id, source_url=opus_link(dynamic_id), lottery_type="转发抽奖",
            enriched_at=1, business_id=dynamic_id, business_type=0, draw_status="active",
            lottery_time=9_999_999_999, prizes=[PrizeTier("first", 1, "奖品")], participants=0,
            conditions={}, winners=None, platform_participated=None, repost_fetched=True,
        )

    monkeypatch.setattr("web.actions._run_ds_check", check)
    monkeypatch.setattr("src.bilibili_client.BilibiliClient._warmup", lambda self: None)
    monkeypatch.setattr("src.bilibili_client.BilibiliClient._http_get", time_out)
    monkeypatch.setattr("src.forward_parser.fetch_dynamic_content", no_body)
    monkeypatch.setattr("src.forward_parser.CLASSIFY_CONTENT_ATTEMPTS", 1)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.classify_new_link", classify)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.enrich_activity", enrich)

    first = run_action("refresh_all")

    assert first["ok"] is True
    assert first["result"]["source_errors"] == {"DS-2": "来源请求超时"}
    assert first["result"]["pipeline"]["skip_reasons"] == {"动态不可见": 1}
    assert {item["dynamic_id"]: item["stage"] for item in first["result"]["pipeline"]["deferred"]} == {
        body_failure: "分类", request_failure: "分类", detail_failure: "详情",
    }
    assert "3 条待重试" in first["message"]
    assert "无法获取动态正文" in first["log"]
    assert classified == wave
    assert known_activity_ids() == {healthy}
    assert get_last_container("DS-1") == results["DS-1"].container_url
    assert get_last_container("DS-2") == "https://www.bilibili.com/read/cv888001"
    assert stage_source_links(["DS-1", "DS-3"], []) == {
        "DS-1": [body_failure, request_failure, detail_failure], "DS-3": [body_failure],
    }

    reset_engine_for_tests()
    network_restored = True
    classified.clear()
    second = run_action("refresh_source", {"source_id": "DS-1"})

    assert second["result"]["sources_updated"] == 0
    assert second["result"]["pipeline"]["deferred"] == []
    assert second["result"]["pipeline"]["persisted_count"] == 3
    assert classified == [body_failure, request_failure, detail_failure]
    assert known_activity_ids() == {body_failure, request_failure, detail_failure, healthy}

    # 再检查另一来源时，重复链接视为已处理，无需再次抓取。
    third = run_action("refresh_source", {"source_id": "DS-3"})
    assert third["result"]["pipeline"]["pipeline_skipped"] is True
    assert stage_source_links(["DS-1", "DS-3"], []) == {"DS-1": [], "DS-3": []}
