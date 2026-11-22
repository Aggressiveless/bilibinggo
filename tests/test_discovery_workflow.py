from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

import pytest

from src.control_store import ControlStore
from web.control_actions import discovery_candidates, workflow


def test_direct_links_are_not_reported_as_author_home():
    rows = discovery_candidates([{"url": "https://www.bilibili.com/opus/1234567890123456789",
        "source_video": "https://www.bilibili.com/video/BV1234567890", "found_via": "video_link",
        "status": "open", "evidence": {"rules": "关注转发评论"}}])
    assert rows[0]["resolution"] == "direct"
    assert rows[0]["rule_quotes"] == ["关注转发评论"]
    assert "author_mid" not in rows[0]  # Don't invent a publisher ID.


@pytest.mark.parametrize("partial", [False, True])
def test_workflow_imports_verified_partial_results_and_owns_its_plan(tmp_path, monkeypatch, partial):
    store = ControlStore(tmp_path)
    store.put("discovery", "7", {"candidates": [{"candidate_id": "one", "eligible": True, "dynamic_id": "1234567890123456789"}]})
    service = SimpleNamespace(store=store, _make_plan=Mock(return_value={"plan_id": "frozen"}), make_plan=Mock(side_effect=AssertionError("Public idle gate rejects its own worker")))
    discover = Mock(return_value={"partial": partial, "eligible": 1})
    importer = Mock(return_value={"imported": 1})
    monkeypatch.setattr("web.control_actions.discover", discover)
    monkeypatch.setattr("web.control_actions.import_candidates", importer)
    config = {"stages": ["discover", "plan"], "request_budget": 60, "max_activities_per_run": 3}
    result = workflow(service, 7, config, lambda **kw: None, None)
    assert result["import"]["imported"] == 1
    assert discover.call_args.args[2]["request_budget"] == 40
    if partial:
        assert result["paused"] and result["platform_execution"] == "not_run"
        service._make_plan.assert_not_called()
    else:
        assert result["plan_id"] == "frozen"
        service._make_plan.assert_called_once_with(["1234567890123456789"], 300)


def test_enrichment_threads_keep_read_only_request_guard(monkeypatch):
    from src.control_telemetry import network_span, record_request
    from src.pipeline.classify_step import ClassifyOutcome
    from src.pipeline.refresh_all_pipeline import run_new_links_pipeline
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.known_activity_ids", lambda: set())
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.load_participations", lambda: {})
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.BilibiliClient", MagicMock())
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.classify_new_link", lambda client, did: ClassifyOutcome(did, "转发抽奖", False))
    def enrich(*args, **kwargs):
        record_request("POST", "https://api.bilibili.com/x/dynamic/feed/create/dyn")
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.enrich_activity", enrich)
    with network_span(read_only=True):
        with pytest.raises(RuntimeError, match="禁止平台写"):
            run_new_links_pipeline(["https://www.bilibili.com/opus/1234567890123456789"], workers=1)
