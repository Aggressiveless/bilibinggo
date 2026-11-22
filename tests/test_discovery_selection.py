import time

from web import coin_balance as coins
from web.discovery_filters import coin_requirement, preview_filters


def test_publisher_threshold_and_unknown_account_age_are_independent_of_bvs(isolated_home):
    from web.control_service import get_control_service

    service = get_control_service()
    now = int(time.time())
    ids = ["1234567890123456781", "1234567890123456782", "1234567890123456783", "1234567890123456784"]
    rows = [{"dynamic_id": did, "eligible": True, "author_mid": str(i),
             "raw": {"rule_text": "视频一键三连" if i == 3 else "无需投币，关注后转发"}}
            for i, did in enumerate(ids)]
    service.store.put("discovery", "8", {"candidates": rows})
    service.store.remember_discovery_videos(["BV11a1111111", "BV11a1111112"])

    class Client:
        def read(self, url, params, **kwargs):
            if "follower" in url or url.endswith("/stat"):
                uid = params["vmid"]
                return {"code": 0, "data": {"follower": 5 if uid == "1" else 1000}}
            uid = params["mid"]
            return {"code": 0, "data": {"card": {"regtime": now - 400 * 86400 if uid != "2" else 0}}}

    settings = {"coins": "exclude_required", "min_followers": 100, "min_account_days": 30, "include_unknown": False}
    result = preview_filters(service, 8, settings, Client(), lambda **kw: None)
    assert (result["matched"], result["unknown_metadata"]) == (1, 1)
    assert service.store.get("discovery_filter", "8")["dynamic_ids"] == [ids[0]]
    result = preview_filters(service, 8, {**settings, "include_unknown": True}, Client(), lambda **kw: None)
    assert result["matched"] == 2
    assert service.store.discovery_video_count() == 2
    assert service.store.get("discovery", "8")["candidates"] == rows
    assert coin_requirement({"raw": {"rule_text": "不用投币，转发即可"}}) == "not_required"


def test_coin_cache_estimates_only_confirmed_cost_and_refreshes_on_opt_in(isolated_home, monkeypatch):
    from web.control_service import get_control_service

    store = get_control_service().store
    now = [100_000]
    readings = [50, 48, 51]
    records = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def request_json(self, *_args, **_kwargs):
            return {"code": 0, "data": {"isLogin": True, "mid": 42, "money": readings.pop(0)}}

    monkeypatch.setattr(coins, "get_login_uid", lambda: 42)
    monkeypatch.setattr(coins, "BilibiliClient", Client)
    monkeypatch.setattr(coins.time, "time", lambda: now[0])
    monkeypatch.setattr(coins, "load_action_entries_for_uid", lambda uid: records)
    assert coins.coin_balance(store)["balance"] == 50
    now[0] += 10
    records.append({"recorded_at": now[0], "status": "failed", "actions": [
        {"action": "coin", "ok": True, "coins_used": 2}, {"action": "coin", "ok": False, "coins_used": 1},
        {"action": "coin", "ok": True}, {"action": "like", "ok": True, "coins_used": 1},
    ]})
    estimate = coins.coin_balance(store)
    assert estimate["balance"] == 48 and estimate["estimated"] and len(readings) == 2
    live = coins.coin_balance(store, refresh_after_coin=True)
    assert live["balance"] == 48 and not live["estimated"] and len(readings) == 1
    assert coins.coin_balance(store)["balance"] == 48 and len(readings) == 1
    now[0] += coins.REFRESH_SECONDS
    assert coins.coin_balance(store)["balance"] == 51 and not readings
