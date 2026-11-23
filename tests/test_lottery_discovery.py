from datetime import datetime

import httpx
import pytest

from src import lottery_discovery as discovery
from src.bilibili_client import BilibiliClient


def date(year, month, day, hour=0):
    return int(datetime(year, month, day, hour, tzinfo=discovery.CST).timestamp())


@pytest.mark.parametrize(
    ("fields", "notice", "expected"),
    [
        ({"deadline": "11月26日晚上8点前报名"}, None, "open"),
        ({"deadline": "11月21日18:00截止", "draw": "11月27日20:00开奖"}, None, "closed"),
        ({"draw": "11月27日20:00开奖"}, None, "review"),
        ({"closed": "中奖名单已公布", "deadline": "11月26日20:00截止"}, None, "closed"),
        ({}, {"status": 0, "lottery_time": date(2026, 11, 25)}, "open"),
        ({}, {"status": 0, "lottery_time": date(2026, 11, 21)}, "closed"),
        ({}, {"status": 0, "lottery_time": date(2026, 11, 25), "participated": True}, "joined"),
        ({}, {"status": 1, "lottery_time": date(2026, 11, 25)}, "closed"),
        ({}, {"status": 0, "lottery_time": date(2026, 11, 25), "lottery_result": ["winner"]}, "closed"),
    ],
)
def test_current_participation_uses_deadline_and_draw_separately(fields, notice, expected):
    result = discovery.assess_candidate(
        {"prize": "键盘", "rules": "评论区留言", **fields}, notice=notice,
        now=date(2026, 11, 21, 20), published=date(2026, 11, 20),
    )
    assert result["status"] == expected


def test_year_inference_is_bound_to_recent_source_and_supports_rollover():
    assert discovery._date_from_quote("1月2日20:00", date(2026, 12, 30), date(2026, 12, 31)) == date(2027, 1, 2, 20)
    assert discovery._date_from_quote("11月26日20:00", date(2025, 11, 20), date(2026, 11, 21)) is None


def test_date_without_clock_is_not_lost():
    assert discovery._date_from_quote("11月25日截止", date(2026, 11, 20), date(2026, 11, 21)) == date(2026, 11, 26) - 1
    result = discovery.assess_candidate({"prize": "键盘", "rules": "转发评论", "draw": "11月20日开奖"},
        now=date(2026, 11, 22), published=date(2026, 11, 19))
    assert result["status"] == "closed"


def test_winner_announcement_is_not_home_participation():
    assert not discovery._home_participation_hint("参与方式是给本视频三连，抽奖结果会在后续我主页的动态公布")
    assert discovery._home_participation_hint("抽机械键盘，去主页置顶动态参与")


def test_discovery_reuses_link_and_keeps_only_author_comments():
    now = date(2026, 11, 21)
    bvid = "BV1234567890"
    dynamic_id = "1234567890123456789"
    link = discovery.opus_link(dynamic_id)

    class FakeClient:
        request_count = 0
        seen = []

        def read(self, url, params, **kwargs):
            self.request_count += 1
            self.seen.append(url)
            payloads = {
                discovery.SEARCH_URL: {"result": [{"bvid": bvid, "pubdate": now - 60}]},
                discovery.VIDEO_URL: {"aid": 5, "owner": {"mid": 2}, "title": "抽奖", "desc": link},
                discovery.REPLIES_URL: {"replies": [
                    {"member": {"mid": 3}, "content": {"message": "https://www.bilibili.com/opus/9876543210987654321"}},
                    {"member": {"mid": 2}, "content": {"message": link}},
                ]},
                discovery.DYNAMIC_DETAIL_URL: {"item": {"modules": {"module_dynamic": {"desc": {"text": "抽奖"}}}}},
                discovery.LOTTERY_NOTICE_URL: {"lottery_id": 6, "status": 0, "lottery_time": now + 86400},
            }
            return {"code": 0, "data": payloads[url]}

        def extract(self, text):
            return {}

    fake = FakeClient()
    result = discovery.discover(fake, ["抽奖", "抽奖福利"], video_limit=2, now=now)
    assert [row["url"] for row in result["open"]] == [link]
    assert fake.seen.count(discovery.DYNAMIC_DETAIL_URL) == 1
    assert fake.seen.count(discovery.VIDEO_URL) == 1


def test_video_comment_lottery_without_dynamic_link():
    now = date(2026, 11, 21)
    bvid = "BV1234567890"
    text = "在本视频评论区留言，抽奖送键盘，2026年11月25日20:00截止"

    class FakeClient:
        request_count = 0

        def read(self, url, params, **kwargs):
            self.request_count += 1
            if url == discovery.SEARCH_URL:
                return {"data": {"result": [{"bvid": bvid, "pubdate": now - 60}]}}
            assert url == discovery.VIDEO_URL
            return {"data": {"desc": text}}

        def extract(self, source):
            assert text in source
            return {"prize": "键盘", "rules": "在本视频评论区留言", "deadline": "2026年11月25日20:00截止"}

    result = discovery.discover(FakeClient(), ["评论区抽奖"], video_limit=1, now=now)
    assert [row["kind"] for row in result["open"]] == ["video_comment"]


def test_read_budget_cache_and_persistent_cooldown(monkeypatch, tmp_path):
    path = tmp_path / "cache.json"
    request = httpx.Request("GET", discovery.VIDEO_URL)
    replies = iter([
        httpx.Response(200, request=request, json={"code": 0, "data": {"aid": 1}}),
        httpx.Response(429, request=request, headers={"Retry-After": "1800"}),
    ])
    monkeypatch.setattr(BilibiliClient, "_http_get", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(discovery.time, "sleep", lambda value: None)
    with discovery.DiscoveryClient(path, budget=1) as client:
        for _ in range(2):
            client.read(discovery.VIDEO_URL, {"bvid": "one"}, referer="", ttl=600)
        assert client.request_count == 1
        with pytest.raises(discovery.DiscoveryStopped, match="预算"):
            client.read(discovery.VIDEO_URL, {"bvid": "two"}, referer="", ttl=600)
        client.save()
    with discovery.DiscoveryClient(path, budget=3) as client:
        client.read(discovery.VIDEO_URL, {"bvid": "one"}, referer="", ttl=600)
        assert client.request_count == 0
        with pytest.raises(discovery.DiscoveryStopped, match="429"):
            client.read(discovery.VIDEO_URL, {"bvid": "two"}, referer="", ttl=600)
        assert client.cache["paused_until"] >= discovery.time.time() + 1795
    with discovery.DiscoveryClient(path, budget=3) as client:
        with pytest.raises(discovery.DiscoveryStopped, match="暂停至"):
            client.read(discovery.VIDEO_URL, {"bvid": "two"}, referer="", ttl=600)
        assert client.request_count == 0


def test_model_evidence_must_exist_and_is_cached(monkeypatch, tmp_path):
    calls = []

    def chat(**kwargs):
        calls.append(kwargs)
        return {"prize": "机械键盘", "deadline": "2030年1月1日20:00", "assessment": "uncertain",
                "reason": "  原文没有说明\n报名截止时间  "}

    monkeypatch.setattr(discovery, "chat_json", chat)
    with discovery.DiscoveryClient(tmp_path / "cache.json", budget=1) as client:
        for _ in range(2):
            fields = client.extract("在评论区留言抽机械键盘")
            assert fields["prize"] == "机械键盘"
            assert not fields["deadline"]
            assert fields["model_reason"] == "原文没有说明 报名截止时间"
            assert fields["model_assessment"] == "uncertain"
        assert client.request_count == 0
    assert len(calls) == 1


def test_model_keyword_expansion_is_bounded(monkeypatch):
    monkeypatch.setattr(discovery, "chat_json", lambda **kwargs: {
        "keywords": ["评论区抽奖", "https://example.org", "动态转发送周边", "外设抽奖", "额外搜索词"],
    })
    assert discovery.expand_keywords(["评论区抽奖"]) == ["评论区抽奖", "动态转发送周边", "外设抽奖"]


def test_single_page_homepage_referral_and_report_metadata():
    now = date(2026, 11, 21, 20)
    bvids = [f"BV{idx:010d}" for idx in range(36)]
    dynamic_id = "1234567890123456789"
    old_dynamic_id = "1234567890123456790"
    source_text = "抽奖送机械键盘，去主页置顶动态参与。"
    open_text = "抽奖送机械键盘，在本动态评论区留言，11月25日20:00截止。"
    old_text = "抽奖送机械键盘，在本动态评论区留言，11月21日18:00截止。"

    class FakeClient:
        request_count = 0

        def __init__(self):
            self.requests = []

        def read(self, url, params, **kwargs):
            self.request_count += 1
            self.requests.append((url, params))
            if url == discovery.SEARCH_URL:
                assert params["order"] == "totalrank" and params["page"] == 1
                assert params["page_size"] == 36
                return {"code": 0, "data": {"result": [{"bvid": bvid, "pubdate": now - 60} for bvid in bvids]}}
            if url == discovery.VIDEO_URL:
                text = source_text if params["bvid"] in bvids[:2] else "普通视频"
                return {"code": 0, "data": {"desc": text, "owner": {"mid": 99, "name": "测试发布者"}}}
            if url == discovery.SPACE_FEED_URL:
                return {"code": 0, "data": {"items": [
                    {"id_str": post, "modules": {
                        "module_author": {"mid": 99, "name": "测试发布者", "pub_ts": now - 86400},
                        "module_dynamic": {"desc": {"text": text}},
                    }} for post, text in ((dynamic_id, open_text), (old_dynamic_id, old_text))
                ]}}
            if url == discovery.LOTTERY_NOTICE_URL:
                return {"code": 0, "data": {}}
            assert url == discovery.FOLLOWER_URL and params == {"vmid": "99"}
            return {"code": 0, "data": {"follower": 12345}}

        def extract(self, text):
            if text == source_text:
                return {"prize": "机械键盘", "rules": "去主页置顶动态参与"}
            deadline = "11月25日20:00截止" if text == open_text else "11月21日18:00截止"
            return {"prize": "机械键盘", "rules": "在本动态评论区留言", "deadline": deadline}

    client = FakeClient()
    result = discovery.discover(client, ["抽奖"], video_limit=36, now=now, comprehensive_page=True)
    assert result["search_video_count"] == result["videos_checked"] == 36
    assert result["errors"] == [] and not result["stopped"]
    assert len(result["open"]) == len(result["closed"]) == 1
    row = result["open"][0]
    assert (row["publisher"], row["publisher_uid"], row["followers"], row["prize"]) == ("测试发布者", "99", 12345, "机械键盘")
    assert row["url"] == discovery.opus_link(dynamic_id)
    assert row["found_via"] == "publisher_home"
    assert row["source_video"].endswith(bvids[0])
    for endpoint in (discovery.SEARCH_URL, discovery.SPACE_FEED_URL, discovery.FOLLOWER_URL):
        assert sum(url == endpoint for url, _ in client.requests) == 1


def test_discovery_recovers_empty_dynamic_body_from_opus():
    identifier = "1234567890123456789"
    body = "抽键盘，转发并评论，11月25日截止"
    now = date(2026, 11, 22)
    class FakeClient:
        request_count = 0
        def read(self, url, params, **kwargs):
            self.request_count += 1
            if url == discovery.SEARCH_URL:
                data = {"result": [{"bvid": "BV1234567890", "pubdate": now}]}
            elif url == discovery.VIDEO_URL:
                data = {"desc": discovery.opus_link(identifier)}
            elif url == discovery.DYNAMIC_DETAIL_URL:
                data = {"item": {"type": "DYNAMIC_TYPE_DRAW", "modules": {"module_author": {"pub_ts": now}, "module_dynamic": {"desc": None}}}}
            elif url == discovery.OPUS_DETAIL_URL:
                data = {"item": {"modules": [{"module_type": "MODULE_TYPE_CONTENT", "module_content": {
                    "paragraphs": [{"text": {"nodes": [{"type": "TEXT_NODE_TYPE_WORD", "word": {"words": body}}]}}]}}]}}
            else:
                assert url == discovery.LOTTERY_NOTICE_URL
                data = {}
            return {"code": 0, "data": data}
        def extract(self, text):
            assert body in text
            return {"prize": "键盘", "rules": "转发并评论", "deadline": "11月25日截止"}
    progress = []
    result = discovery.discover(FakeClient(), ["抽奖"], video_limit=1, now=now, on_progress=progress.append)
    assert len(result["open"]) == 1 and not result["errors"]
    assert result["open"][0]["rule_text"] == body
    assert any(p["eligible"] == 1 for p in progress)


def test_newest_collection_links_are_checked_before_old_first_three():
    now = date(2026, 11, 22)
    identifiers = [str(1234567890123456700 + i) for i in range(12)]
    requested = []
    class FakeClient:
        request_count = 0
        def read(self, url, params, **kwargs):
            self.request_count += 1
            if url == discovery.SEARCH_URL:
                data = {"result": [{"bvid": "BV1234567890", "pubdate": now}]}
            elif url == discovery.VIDEO_URL:
                data = {"desc": "\n".join(discovery.opus_link(i) for i in identifiers)}
            elif url == discovery.DYNAMIC_DETAIL_URL:
                requested.append(params["id"])
                data = {"item": {"modules": {"module_dynamic": {"desc": {"text": "抽键盘"}}}}}
            else:
                assert url == discovery.LOTTERY_NOTICE_URL
                data = {"lottery_id": 1, "status": 0, "lottery_time": now + 86400, "first_prize_cmt": "键盘"}
            return {"code": 0, "data": data}
        def extract(self, text):
            pytest.fail("Official notices don't require an LLM round trip")
    result = discovery.discover(FakeClient(), ["抽奖"], video_limit=1, now=now, candidate_limit=3)
    assert requested == list(reversed(identifiers))[:3]
    assert len(result["open"]) == 3 and result["completion_reason"] == "target_reached"
    assert not result["stopped"]


def test_article_referral_preserves_source_chain():
    now = date(2026, 11, 22)
    identifier = "1234567890123456789"
    article = "https://www.bilibili.com/read/cv12345"
    class FakeClient:
        request_count = 0
        def read(self, url, params, **kwargs):
            self.request_count += 1
            if url == discovery.SEARCH_URL:
                data = {"result": [{"bvid": "BV1234567890", "pubdate": now}]}
            elif url == discovery.VIDEO_URL:
                data = {"desc": "抽奖合集 " + article}
            elif url == discovery.ARTICLE_URL:
                data = {"opus": {"content": {"paragraphs": [{"text": {"nodes": [
                    {"node_type": 4, "link": {"biz_id": identifier}}]}}]}}}
            elif url == discovery.DYNAMIC_DETAIL_URL:
                data = {"item": {"modules": {"module_dynamic": {"desc": {"text": "抽键盘"}}}}}
            else:
                assert url == discovery.LOTTERY_NOTICE_URL
                data = {"lottery_id": 1, "status": 0, "lottery_time": now + 86400, "first_prize_cmt": "键盘"}
            return {"code": 0, "data": data}
    result = discovery.discover(FakeClient(), ["抽奖"], video_limit=1, now=now)
    assert len(result["open"]) == 1
    assert result["open"][0]["found_via"] == "article_link"
    assert {"source": article} in result["open"][0]["source_chain"]
