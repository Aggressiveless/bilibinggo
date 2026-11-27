from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.participate_preflight import ActivityPreflightRejected
from web.actions import run_action
from web.activity_service import list_activities
from web.app import app


def targets(count):
    return [{"dynamic_id": f"100000000000000{i:03d}", "activity_title": f"活动 {i}",
             "lottery_type": "预约抽奖", "activity_status": "未参加", "can_participate": True}
            for i in range(count)]


def test_preview_covers_all_pages_and_keeps_filter_order(isolated_home):
    rows = targets(27)
    with patch("web.activity_service._filtered_activity_rows", return_value=rows) as filtered:
        response = TestClient(app).get("/api/activities/participation-targets?sort=heat&order=desc&q=键盘")
        assert response.status_code == 200
        assert response.json()["count"] == 27
        assert [v["dynamic_id"] for v in response.json()["items"]] == [v["dynamic_id"] for v in rows]
        assert filtered.call_args.kwargs["q"] == "键盘"
        assert filtered.call_args.kwargs["sort"] == "heat"
        page = list_activities(page=2)
    assert len(page["items"]) == 7 and page["sequential_targets"]["count"] == 27


@pytest.mark.parametrize("fail_at", [None, 4])
def test_executes_more_than_three_in_order_and_stops_on_failure(isolated_home, fail_at):
    rows = targets(7)
    attempted, marked = [], []
    def participate(identifier, *args, **kwargs):
        attempted.append(identifier)
        assert marked == attempted[:-1], "A later activity started before its predecessor finished"
        if fail_at is not None and len(attempted) == fail_at:
            raise RuntimeError("模拟失败")
        return {"dynamic_id": identifier, "lottery_type": "预约抽奖", "status": "joined", "message": "参与成功", "actions": []}
    with patch("web.actions.pick_triple_participate_targets", return_value=rows) as pick, \
         patch("web.actions.BilibiliClient"), \
         patch("web.actions.ensure_activity_participatable"), \
         patch("web.actions._participate_dynamic_payload", side_effect=participate), \
         patch("web.actions.mark_enriched_joined", side_effect=marked.append), \
         patch("web.actions.refresh_local_activity_statuses"), \
         patch("web.actions.invalidate_activity_cache"):
        if fail_at:
            with pytest.raises(RuntimeError, match="模拟失败") as error:
                run_action("participate_sequential", {"sort": "heat"})
            assert error.value.participation_result["joined"] == 3
            assert error.value.participation_result["failed"] == 1
            assert error.value.participation_result["remaining"] == 3
        else:
            result = run_action("participate_sequential", {"sort": "heat"})
            assert result["ok"] and result["result"]["joined"] == 7
        assert pick.call_args.kwargs["limit"] is None
    assert attempted == [v["dynamic_id"] for v in rows[:fail_at or 7]]


def test_cluster_sequential_continues_past_rejected_preflight(isolated_home):
    rows = targets(3)
    first = rows[0]["dynamic_id"]

    def preflight(_client, identifier, **kwargs):
        if identifier == first:
            raise ActivityPreflightRejected("无法获取抽奖信息")

    def participate(identifier, *args, **kwargs):
        return {"dynamic_id": identifier, "lottery_type": "预约抽奖", "status": "joined", "actions": []}

    with patch("web.actions.get_login_uid", return_value=42), \
         patch("web.actions.pick_triple_participate_targets", return_value=rows), \
         patch("web.actions.BilibiliClient"), \
         patch("web.actions.ensure_activity_participatable", side_effect=preflight), \
         patch("web.actions._participate_dynamic_payload", side_effect=participate) as submit, \
         patch("web.actions.mark_enriched_joined"), \
         patch("web.actions.refresh_local_activity_statuses"), \
         patch("web.actions.invalidate_activity_cache"):
        result = run_action("participate_sequential", {"_cluster_account": 42})
    assert result["result"]["joined"] == 2
    assert result["result"]["skipped"] == 1
    assert [call.args[0] for call in submit.call_args_list] == [item["dynamic_id"] for item in rows[1:]]
