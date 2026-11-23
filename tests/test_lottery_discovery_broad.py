"""Verify page coverage, source selection and durable budget resumption."""
import json

import httpx

from src import lottery_discovery as d
from src import lottery_discovery_broad as broad
from src.bilibili_client import BilibiliClient
from src.control_store import ControlStore
from src.discovery_video_audit import display_video_check


class PageClient:
    def __init__(self, cache=None, budget=1000):
        self.cache = cache if cache is not None else {}
        self.request_count = 0
        self.budget = budget
        self.calls = []

    def read(self, url, params, **kwargs):
        key = json.dumps([url, params], sort_keys=True)
        if key in self.cache.setdefault("responses", {}):
            return self.cache["responses"][key]
        if self.request_count >= self.budget:
            raise d.DiscoveryStopped("已达到本轮 B 站请求预算")
        self.request_count += 1
        self.calls.append((url, params))
        if url == d.SEARCH_URL:
            data = {"result": [{"bvid": f"BV{index:010d}"} for index in range(36)]}
        elif url == d.VIDEO_URL:
            index = int(params["bvid"][2:])
            data = {"aid": index + 1, "owner": {"mid": index + 1}, "title": "抽奖",
                    "desc": "\n".join(d.opus_link(str(1234567890000000 + index * 10 + k)) for k in range(2))}
        else:
            assert url == d.REPLIES_URL
            data = {}
        response = {"code": 0, "data": data}
        self.cache["responses"][key] = response
        return response


def test_all_36_videos_precede_target_verification_and_candidates_are_fair(monkeypatch):
    client = PageClient()
    targets = []
    def inspect(client, identifier, **kwargs):
        assert len([url for url, _ in client.calls if url == d.VIDEO_URL]) == 36
        targets.append(identifier)
        return {"dynamic_id": identifier, "kind": "dynamic", "url": d.opus_link(identifier), "status": "review"}
    monkeypatch.setattr(d, "inspect_dynamic", inspect)
    report = broad.discover_page(client, ["抽奖"], candidate_limit=0)
    assert report["videos_checked"] == 36 and report["scan_completed"]
    assert report["requests"] == 73
    assert not report["has_more"] and len(report["review"]) == 72
    # Each video gets one target checked before any video gets a second.
    assert len({(int(value) - 1234567890000000) // 10 for value in targets[:36]}) == 36


def test_budget_resumes_without_rereading_completed_videos(monkeypatch):
    cache = {}
    inspected = []
    def inspect(client, identifier, **kwargs):
        inspected.append(identifier)
        return {"dynamic_id": identifier, "kind": "dynamic", "url": d.opus_link(identifier), "status": "review"}
    monkeypatch.setattr(d, "inspect_dynamic", inspect)
    first = PageClient(cache, budget=10)
    partial = broad.discover_page(first, ["抽奖"], candidate_limit=0)
    assert partial["videos_checked"] == 4 and not partial["scan_completed"]
    assert partial["completion_reason"] == "request_budget" and not inspected
    second = PageClient(cache)
    finished = broad.discover_page(second, ["抽奖"], candidate_limit=0)
    assert finished["resumed"] and finished["session_id"] == partial["session_id"]
    assert finished["scan_completed"] and not finished["has_more"]
    assert first.request_count + second.request_count == 73
    assert len(inspected) == len(set(inspected)) == 72
    third = PageClient(cache)
    cached = broad.discover_page(third, ["抽奖"], known_ids={inspected[0]}, joined_ids={inspected[1]})
    assert cached["resumed"] and third.request_count == 0
    assert len(cached["joined"]) == 1


def test_verification_cursor_retains_progress_after_budget_stop(monkeypatch):
    client = PageClient()
    first = True
    ids = []
    def inspect(client, identifier, **kwargs):
        nonlocal first
        if first and len(ids) == 3:
            first = False
            raise d.DiscoveryStopped("已达到本轮 B 站请求预算")
        ids.append(identifier)
        return {"dynamic_id": identifier, "kind": "dynamic", "url": d.opus_link(identifier), "status": "review"}
    monkeypatch.setattr(d, "inspect_dynamic", inspect)
    partial = broad.discover_page(client, ["抽奖"], candidate_limit=0)
    assert partial["scan_completed"] and partial["verification_remaining"] == 69
    finished = broad.discover_page(PageClient(client.cache), ["抽奖"], candidate_limit=0)
    assert not finished["has_more"] and len(ids) == len(set(ids)) == 72


def test_author_keyword_search_selects_latest_two_own_originals():
    def item(identifier, ts, *, kind="DYNAMIC_TYPE_WORD", author="42"):
        return {"id_str": str(identifier), "type": kind,
                "modules": {"module_author": {"mid": author, "pub_ts": ts}}}
    class AuthorClient:
        def read(self, url, params, **kwargs):
            assert url == broad.AUTHOR_SEARCH_URL
            assert params["keyword"] == "抽奖" and params["host_mid"] == "42"
            return {"code": 0, "data": {"items": [
                item(1234567890000001, 100), item(1234567890000004, 400, kind="DYNAMIC_TYPE_FORWARD"),
                item(1234567890000003, 300), item(1234567890000002, 200), item(1234567890000005, 500, author="43")],
                "has_more": True}}
    items, metadata = broad.search_author_lotteries(AuthorClient(), "42")
    assert [value["id_str"] for value in items] == ["1234567890000003", "1234567890000002"]
    assert metadata["original_matches"] == 3 and metadata["has_more"]


def test_two_second_request_start_spacing_and_cache_does_not_consume_budget(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(d.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(BilibiliClient, "_http_get", lambda self, url, **kwargs:
        httpx.Response(200, request=httpx.Request("GET", url), json={"code": 0, "data": {}}))
    with d.DiscoveryClient(tmp_path / "cache.json", budget=3) as client:
        for identifier in ("one", "one", "two", "three"):
            client.read(d.VIDEO_URL, {"bvid": identifier}, referer="", ttl=600)
        assert client.request_count == 3 and client.cache_hits == 1
        assert client.request_start_offsets == [0.0, 2.0, 4.0]


def test_two_independently_verified_home_posts_remain_eligible(monkeypatch):
    class HomeClient(PageClient):
        def read(self, url, params, **kwargs):
            if url == d.VIDEO_URL:
                return {"code": 0, "data": {"owner": {"mid": 42}, "desc": "抽奖请去主页置顶动态参与"}}
            return super().read(url, params, **kwargs)
    monkeypatch.setattr(broad, "search_author_lotteries", lambda *a, **kw:
        ([{"id_str": str(value)} for value in (1234567890000001, 1234567890000002)], {"search_url": "https://space.bilibili.com/42/search/dynamic"}))
    monkeypatch.setattr(d, "inspect_dynamic", lambda client, identifier, **kw:
        {"dynamic_id": identifier, "kind": "dynamic", "url": d.opus_link(identifier), "status": "open"})
    result = broad.discover_page(HomeClient(), ["抽奖"], video_limit=1)
    assert len(result["open"]) == 2 and not result["review"]
    assert all(row["found_via"] == "publisher_home" for row in result["open"])


def test_search_budget_stop_is_not_reported_as_complete_empty_page():
    result = broad.discover_page(PageClient(budget=0), ["抽奖"])
    assert not result["scan_completed"] and result["has_more"]


def test_browser_collects_followers_once_per_eligible_publisher(monkeypatch):
    monkeypatch.setattr(d, "inspect_dynamic", lambda client, identifier, **kw: {
        "dynamic_id": identifier, "kind": "dynamic", "url": d.opus_link(identifier),
        "publisher_uid": "42", "status": "open",
    })
    calls = []

    def followers(client, uid):
        calls.append(uid)
        return 0

    monkeypatch.setattr(d, "read_follower_count", followers)
    client = PageClient()
    result = broad.discover_page(client, ["抽奖"], video_limit=1, collect_followers=True)
    assert [row["followers"] for row in result["open"]] == [0, 0]
    result = broad.discover_page(PageClient(client.cache), ["抽奖"], video_limit=1, collect_followers=True)
    assert [row["followers"] for row in result["open"]] == [0, 0]
    assert calls == ["42"]


def test_video_comment_candidates_are_not_counted_as_importable_dynamics():
    class CommentClient(PageClient):
        def read(self, url, params, **kwargs):
            if url == d.VIDEO_URL:
                return {"code": 0, "data": {"desc": "评论区留言，抽奖送键盘，2030年11月25日20:00截止"}}
            return super().read(url, params, **kwargs)
        def extract(self, text):
            return {"prize": "键盘", "rules": "评论区留言", "deadline": "2030年11月25日20:00截止"}
    snapshots = []
    result = broad.discover_page(CommentClient(), ["抽奖"], video_limit=1, on_progress=snapshots.append)
    assert result["open"][0]["kind"] == "video_comment"
    assert snapshots[-1]["eligible"] == 0


def test_audit_shares_dynamic_results_and_preserves_only_distinct_sightings(tmp_path, monkeypatch):
    ids = [f"BV{index:010d}" for index in range(4)]
    target = "1234567890123456789"

    class AuditClient(PageClient):
        def read(self, url, params, **kwargs):
            if url == d.SEARCH_URL:
                return {"code": 0, "data": {"result": [{"bvid": bv, "title": "<em>视频</em>&amp;标题"} for bv in ids]}}
            if url == d.VIDEO_URL:
                if params["bvid"] == ids[3]:
                    raise httpx.ReadTimeout("details unavailable")
                desc = d.opus_link(target) if params["bvid"] in ids[:2] else "普通视频"
                return {"code": 0, "data": {"desc": desc}}
            raise AssertionError(url)

    inspected = []
    def inspect(client, identifier, **kwargs):
        inspected.append(identifier)
        return {"dynamic_id": identifier, "kind": "dynamic", "url": d.opus_link(identifier),
                "status": "open", "reason": "仍可参加", "model_reason": "明确给出参与方式和奖品",
                "model_assessment": "lottery"}
    monkeypatch.setattr(d, "inspect_dynamic", inspect)

    store, client = ControlStore(tmp_path), AuditClient()
    def run(job_id, resume=True):
        return broad.discover_page(client, ["抽奖", "送键盘"], video_limit=4, resume=resume,
            on_video_page=store.remember_discovery_videos,
            on_progress=lambda snapshot: store.save_discovery_video_checks(job_id, snapshot["video_checks"]))

    run(1)
    run(2)
    assert inspected == [target]
    total, rows = store.list_discovery_videos(limit=50, offset=0, job_id=2)
    assert total == 4
    results = [display_video_check(row, known=set(), joined=set(), outcomes={}, now=0) for row in rows]
    assert [row["status"] for row in results] == ["ready", "ready", "rejected", "pending"]
    assert all(row["repeat_count"] == 1 and not row["repeat_count_incomplete"] for row in results)
    assert results[0]["title"] == "视频&标题"
    assert results[1]["reasons"][0]["model_reason"] == "明确给出参与方式和奖品"
    assert "未送模型" in results[2]["reasons"][0]["message"]
    assert "未能读取" in results[3]["reasons"][0]["message"]
    imported = display_video_check(rows[0], known={target}, joined=set(), outcomes={target: "imported"}, now=0)
    assert imported["status"] == "handled" and imported["reasons"][0]["message"] == "关联活动已导入"
    run(3, resume=False)
    assert store.list_discovery_videos(limit=1, offset=0)[1][0]["sightings"] == 4
