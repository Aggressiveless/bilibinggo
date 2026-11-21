from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from src.lottery_actions import (
    FAVORITE_NOT_REQUIRED,
    ActionResult,
    execute_full_participation,
    favorite_supported,
)
from src.participation_log import participation_succeeded


def test_favorite_supported_when_module_stat_has_favorite() -> None:
    item = {
        "modules": {
            "module_stat": {
                "like": {"status": False},
                "favorite": {"status": False},
            }
        }
    }
    assert favorite_supported(item) is True


def test_favorite_not_supported_when_module_stat_missing_favorite() -> None:
    item = {
        "modules": {
            "module_stat": {
                "like": {"status": True},
                "forward": {"count": 1},
                "comment": {"count": 1},
            }
        }
    }
    assert favorite_supported(item) is False


def test_favorite_supported_when_module_stat_incomplete() -> None:
    item = {
        "modules": {
            "module_stat": {
                "like": {"status": True},
            }
        }
    }
    assert favorite_supported(item) is True


def test_favorite_supported_when_module_stat_missing() -> None:
    assert favorite_supported({"modules": {}}) is True


def test_favorite_supported_uses_opus_detail_when_dynamic_stat_lacks_favorite() -> None:
    dynamic_item = {
        "modules": {
            "module_stat": {
                "like": {"status": True},
                "forward": {"count": 1},
                "comment": {"count": 1},
            }
        }
    }
    opus_item = {
        "modules": {
            "module_stat": {
                "like": {"status": True},
                "forward": {"count": 1},
                "comment": {"count": 1},
                "favorite": {"status": False},
            }
        }
    }
    with patch("src.lottery_actions.fetch_opus_detail_item", return_value=opus_item):
        assert favorite_supported(dynamic_item, client=object(), dynamic_id="123") is True


def test_favorite_not_supported_when_opus_detail_also_lacks_favorite() -> None:
    item = {
        "modules": {
            "module_stat": {
                "like": {"status": True},
                "forward": {"count": 1},
                "comment": {"count": 1},
            }
        }
    }
    with patch("src.lottery_actions.fetch_opus_detail_item", return_value=item):
        assert favorite_supported(item, client=object(), dynamic_id="123") is False


def test_forward_lottery_succeeds_when_favorite_unavailable_skipped() -> None:
    actions = [
        ActionResult("like", True, ""),
        ActionResult("follow", True, ""),
        ActionResult("favorite", True, "无收藏入口，跳过"),
        ActionResult("repost", True, ""),
        ActionResult("comment", True, ""),
    ]
    assert participation_succeeded(actions, lottery_type="转发抽奖") is True


def test_interact_lottery_succeeds_when_favorite_unavailable_skipped() -> None:
    actions = [
        ActionResult("like", True, ""),
        ActionResult("follow", True, ""),
        ActionResult("favorite", True, "无收藏入口，跳过"),
        ActionResult("repost", True, ""),
        ActionResult("comment", False, "code=12078 关注UP主7天以上的人可发评论"),
    ]
    assert participation_succeeded(actions, lottery_type="互动抽奖") is True


@pytest.mark.parametrize(
    ("lottery_type", "description", "expects_favorite"),
    [
        ("转发抽奖", "关注＆转发＆点赞＆评论此条动态，11月25日抽奖", False),
        ("转发抽奖", "转发 + 点赞 + 评论 + 关注，参与抽奖", False),
        ("转发抽奖", "关注＆转发＆点赞＆评论此条动态，并收藏", True),
        ("转发抽奖", "欢迎参加抽奖", True),
        ("互动抽奖", "关注＆转发＆点赞＆评论此条动态，11月25日抽奖", True),
    ],
)
def test_favorite_step_respects_forward_lottery_rules(
    monkeypatch: pytest.MonkeyPatch, lottery_type: str, description: str, expects_favorite: bool,
) -> None:
    item = {
        "modules": {
            "module_dynamic": {"desc": {"rich_text_nodes": [{"text": description}]}},
            "module_stat": {"like": {"status": True}, "favorite": {"status": False}},
        },
        "orig": {"modules": {"module_dynamic": {"desc": {"text": "请收藏原动态"}}}},
    }
    favorite = Mock(return_value=ActionResult("favorite", False, "0"))
    monkeypatch.setattr("src.lottery_actions.fetch_dynamic_detail", Mock(return_value=item))
    monkeypatch.setattr("src.lottery_actions.is_following", Mock(return_value=True))
    monkeypatch.setattr("src.lottery_actions.is_favorited", Mock(return_value=False))
    monkeypatch.setattr("src.lottery_actions.is_reposted", Mock(return_value=True))
    monkeypatch.setattr("src.lottery_actions.has_comment", Mock(return_value=True))
    monkeypatch.setattr("src.lottery_actions.require_login", Mock(return_value=("csrf", 1)))
    monkeypatch.setattr("src.lottery_actions.favorite_dynamic", favorite)
    monkeypatch.setattr("src.lottery_actions.time.sleep", Mock())

    actions, _ = execute_full_participation(
        Mock(), dynamic_id="123", sender_uid=2, lottery_type=lottery_type,
    )

    assert favorite.called is expects_favorite
    assert participation_succeeded(actions, lottery_type=lottery_type) is not expects_favorite
    if not expects_favorite:
        assert ActionResult("favorite", True, FAVORITE_NOT_REQUIRED) in actions
