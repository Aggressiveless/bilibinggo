from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.activity_store import append_activities, load_activities
from src.sources.common import opus_link
from web.app import app


def _shared(number, timestamp, *, prize="键盘"):
    dynamic_id = str(1234567890123456700 + number)
    return {
        "dynamic_id": dynamic_id,
        "source_url": opus_link(dynamic_id),
        "lottery_type": "互动抽奖",
        "lottery_time": timestamp,
        "prize": prize,
    }


def _stored(number, timestamp, **fields):
    activity = _shared(number, timestamp)
    return {
        **activity,
        "draw_status": "active",
        "prizes": [{"description": activity["prize"], "winner_count": 2}],
        **fields,
    }


def test_export_respects_filters_and_only_shares_public_open_activity_fields(isolated_home):
    now = int(time.time())
    append_activities([
        _stored(1, now + 3600, platform_participated=True, account_uid=999, winners={"first": [999]},
                source_url="https://www.bilibili.com/opus/1234567890123456701?private=1",
                conditions={"private": "sensitive"}),
        _stored(2, now - 60),
        _stored(3, now + 3600, lottery_type="充电抽奖"),
        _stored(4, now + 3600, conditions={"lottery_time_inferred": True}),
        _stored(5, now + 3600, lottery_type="转发抽奖"),
        _stored(6, now + 3600, draw_status="ended"),
    ])
    client = TestClient(app)
    response = client.get("/api/activities/export", params={"type": "互动抽奖", "q": "键盘"})
    assert response.status_code == 200
    assert response.json() == {
        "format": "binggo.activities.v1",
        "activities": [_shared(1, now + 3600, prize="键盘 ×2")],
    }
    assert client.get("/api/activities/export", params={"status": "已结束"}).status_code == 400


def test_import_previews_and_rechecks_duplicates_without_overwriting(isolated_home):
    now = int(time.time())
    append_activities([_stored(1, now + 3600, platform_participated=True)])
    bundle = {"format": "binggo.activities.v1", "activities": [
        _shared(1, now + 3600, prize="替换现有活动"),
        _shared(2, now - 1),
        _shared(3, now + 7200, prize="新奖品 ×3"),
        _shared(3, now + 7500, prize="同文件重复"),
        _shared(4, now + 7500),
    ]}
    client = TestClient(app)
    preview = client.post("/api/activities/import", json=bundle)
    assert preview.status_code == 200
    assert preview.json() == {
        "total": 5, "new": 2, "duplicates": 2, "expired": 1, "imported": 0,
        "examples": ["新奖品 ×3", "键盘"],
    }
    assert len(load_activities()) == 1
    # A background refresh can add activities between preview and confirmation.
    append_activities([_stored(4, now + 8000)])
    result = client.post("/api/activities/import?preview=false", json=bundle)
    assert result.status_code == 200
    assert result.json()["imported"] == 1 and result.json()["duplicates"] == 3
    result = client.post("/api/activities/import?preview=false", json=bundle)
    assert result.json()["imported"] == 0

    activities = client.get("/api/activities").json()["items"]
    by_id = {activity["dynamic_id"]: activity for activity in activities}
    assert by_id[_shared(1, now)["dynamic_id"]]["activity_status"] == "已参加"
    added = by_id[_shared(3, now)["dynamic_id"]]
    assert added["prize"] == "新奖品 ×3" and added["activity_status"] == "未参加"
    reexported = client.get("/api/activities/export", params={"q": "新奖品"}).json()
    assert reexported["activities"] == [bundle["activities"][2]]


@pytest.mark.parametrize("bad_fields", [
    {"dynamic_id": 1234567890123456701},
    {"source_url": "https://example.invalid/"},
    {"lottery_time": "9999999999"},
    {"platform_participated": True},
])
def test_invalid_share_file_cannot_write_a_partial_import(isolated_home, bad_fields):
    future = int(time.time()) + 3600
    bundle = {"format": "binggo.activities.v1", "activities": [
        _shared(1, future), {**_shared(2, future), **bad_fields},
    ]}
    response = TestClient(app).post("/api/activities/import?preview=false", json=bundle)
    assert response.status_code == 400
    assert load_activities() == []
