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
        ("转发抽奖", "关&转&赞&评本动态，11月25日抽", False),
        ("转发抽奖", "关注@东方补者 和@COLG玩家社区，转发评论点赞本条动态，11月25日抽", False),
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


def test_null_favorite_is_an_unavailable_entry():
    item = {"modules": {"module_stat": {"forward": {}, "comment": {}, "favorite": None}}}
    with patch("src.lottery_actions.fetch_opus_detail_item", return_value=item):
        assert favorite_supported(item, client=Mock(), dynamic_id="123") is False


@pytest.mark.parametrize("modules", [
    {"module_stat": {"favorite": {"status": True}}},
    [{"module_type": "MODULE_TYPE_STAT", "module_stat": {"favorite": {"status": True}}}],
])
def test_favorite_status_reads_both_platform_module_shapes(modules):
    from src.lottery_actions import _opus_favorite_status
    client = Mock()
    client.get_json.return_value = {"code": 0, "data": {"item": {"modules": modules}}}
    assert _opus_favorite_status(client, dynamic_id="123", referer="test") is True


def test_favorite_missing_state_stays_unknown():
    from src.lottery_actions import _opus_favorite_status
    client = Mock()
    client.get_json.return_value = {"code": 0, "data": {"item": {"modules": {"module_stat": {"favorite": None}}}}}
    assert _opus_favorite_status(client, dynamic_id="123", referer="test") is None


def test_unverified_favorite_never_returns_a_bare_zero(monkeypatch):
    from src.lottery_actions import favorite_dynamic
    client = Mock()
    client.post_json.return_value = {"code": 0, "message": "0"}
    monkeypatch.setattr("src.lottery_actions.is_favorited", lambda *a, **k: False)
    monkeypatch.setattr("src.lottery_actions.time.sleep", lambda _: None)
    result = favorite_dynamic(client, dynamic_id="123", csrf="test", referer="test")
    assert not result.ok and "尚未确认" in result.detail
    assert client.post_json.call_count == 1


def test_required_favorite_without_entry_remains_failed(monkeypatch):
    from src.lottery_actions import DynamicContext
    context = DynamicContext("123", 2, "test", "123", 17, True, False, False, True, True, True,
                             favorite_required=True)
    monkeypatch.setattr("src.lottery_actions.build_dynamic_context", lambda *a, **k: context)
    monkeypatch.setattr("src.lottery_actions.require_login", lambda: ("csrf", 1))
    monkeypatch.setattr("src.lottery_actions.time.sleep", lambda _: None)
    actions, _ = execute_full_participation(Mock(), dynamic_id="123", lottery_type="转发抽奖")
    assert not participation_succeeded(actions, lottery_type="转发抽奖")
    assert "明确要求收藏" in next(a for a in actions if a.action == "favorite").detail


def test_frozen_plan_cannot_add_a_previously_skipped_write(monkeypatch):
    from src.lottery_actions import DynamicContext
    from src.control_store import ControlError
    context = DynamicContext("123", 2, "test", "123", 17, False, False, False, True, True, True)
    monkeypatch.setattr("src.lottery_actions.build_dynamic_context", lambda *a, **k: context)
    client = Mock()
    with pytest.raises(ControlError, match="原计划之外"):
        execute_full_participation(client, dynamic_id="123", allowed_actions=set())
    client.post_json.assert_not_called()
    client.post_form.assert_not_called()


def test_retry_reuses_successful_posts_from_failed_overall_attempt(isolated_home, monkeypatch):
    from src.lottery_actions import build_dynamic_context
    from src.participation_log import append_action_record, ParticipationActionRecord
    append_action_record(ParticipationActionRecord(1, "123", "转发抽奖", "failed", "收藏失败", "test text",
        [{"action": "favorite", "ok": False, "detail": "0"},
         {"action": "repost", "ok": True, "detail": "test text"},
         {"action": "comment", "ok": True, "detail": "test text"}], {}))
    item = {"modules": {"module_stat": {}, "module_dynamic": {"desc": {"text": "关&转&赞&评本动态"}}}}
    for name, value in {"fetch_dynamic_detail": item, "is_following": True,
                        "is_reposted": False, "has_comment": False}.items():
        monkeypatch.setattr("src.lottery_actions." + name, Mock(return_value=value))
    context = build_dynamic_context(Mock(), dynamic_id="123", sender_uid=2, action_text="test text", lottery_type="转发抽奖")
    assert context.reposted and context.commented and not context.favorite_available
    verified = build_dynamic_context(Mock(), dynamic_id="123", sender_uid=2, action_text="test text", lottery_type="转发抽奖", reuse_local_receipts=False)
    assert not verified.reposted and not verified.commented
