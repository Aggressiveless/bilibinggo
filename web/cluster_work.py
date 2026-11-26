
"""Read-only search and verification jobs. No account actions are performed here."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import deque
from pathlib import Path

import httpx

from src import lottery_discovery as d
from src.bilibili_client import BilibiliRequestError
from src.llm_settings import get_llm_config
from src.lottery_discovery_broad import has_direct_video_entry, has_video_clue, search_author_lotteries
from src.sources.common import opus_link
from src.watch_feed import SPACE_FEED_URL, extract_feed_pub_ts, extract_forward_orig_id
from web.cluster_protocol import Activity, Reference, Report, Task

MAX_VIDEO_REFERENCES = 16
MAX_REFERENCE_DEPTH = 4
WATCH_LOOKBACK = 24 * 3600
WATCH_SCAN_PAGES = 3


class RetryCheck(Exception):
    def __init__(self, message, reason="platform"):
        super().__init__(message)
        self.reason = reason


class WorkClient(d.DiscoveryClient):
    def __init__(self, cache_path, get_analysis, put_analysis):
        super().__init__(cache_path, budget=None)
        self.get_analysis = get_analysis
        self.put_analysis = put_analysis

    def extract(self, text, *, source="关联动态", followers=None, context=""):
        config = get_llm_config()
        fingerprint = json.dumps([d.PROMPT, source, followers, context[:240], text,
                                  config.base_url if config else "", config.model_name if config else ""],
                                 ensure_ascii=False)
        key = hashlib.sha256(fingerprint.encode()).hexdigest()
        cached = self.get_analysis(key)
        if cached is not None:
            return cached
        fields = super().extract(text, source=source, followers=followers, context=context)
        if not fields.get("model_assessment"):
            raise RetryCheck("模型未返回有效判定", reason="model")
        return self.put_analysis(key, fields)


def read_data(client, url, params, *, referer, wbi=False, ttl=600):
    payload = client.read(url, params, referer=referer, ttl=ttl, wbi=wbi)
    if d.api_code(payload) != 0 or not isinstance(payload.get("data"), dict):
        raise RetryCheck("平台未返回可读取结果", reason="source_response")
    return payload["data"]


def references(text):
    refs = []
    for kind, regex in (("dynamic", d.DYNAMIC_RE), ("article", d.ARTICLE_RE), ("short", d.SHORT_LINK_RE)):
        for key in dict.fromkeys(regex.findall(text)):
            if kind != "dynamic" or len(key) in (18, 19):
                refs.append(Reference(kind=kind, key=key))
    return refs[:120]


def _author_ref(uid):
    return [Reference(kind="author", key=uid)] if uid.isascii() and uid.isdigit() and not uid.startswith("0") and len(uid) <= 20 else []


def _search(client, task):
    data = read_data(client, d.SEARCH_URL, {"search_type": "video", "keyword": task.keyword, "page": task.page,
                                           "page_size": 36, "order": task.order},
                     referer="https://search.bilibili.com/", wbi=True, ttl=1800)
    seen = dict.fromkeys(str(row.get("bvid") or "") for row in data.get("result") or [] if isinstance(row, dict))
    refs = [Reference(kind="video", key=key) for key in seen if d.BV_RE.fullmatch(key)]
    pages = data.get("numPages")
    return {"references": refs[:36], "more_pages": bool(refs) and (type(pages) is not int or task.page < pages)}


def _video(client, key):
    url = f"https://www.bilibili.com/video/{key}"
    data = read_data(client, d.VIDEO_URL, {"bvid": key}, referer=url)
    texts = [str(data.get("title") or ""), str(data.get("desc") or "")]
    uid = str((data.get("owner") or {}).get("mid") or "")
    if uid and data.get("aid") and not has_direct_video_entry("\n".join(texts)):
        replies = read_data(client, d.REPLIES_URL, {"type": 1, "oid": data["aid"], "mode": 3},
                            referer=url, wbi=True)
        texts.extend(d._owner_comments(replies, uid))
    text = "\n".join(texts)
    links = references(text)
    if not links and _author_ref(uid) and has_video_clue(text):
        if d._home_participation_hint(text) or client.extract(
            text[:8000], source="视频标题、简介及作者评论"
        ).get("model_assessment") in {"lottery", "entry", "collection"}:
            links = _author_ref(uid)
    return links


def _dynamic(client, key, now):
    linked = []
    unreadable = []

    def on_links(text):
        linked.extend(ref for ref in references(text) if ref.key != key)

    def on_skip(status, message):
        if status == "pending":
            unreadable.append("dynamic_detail" if "详情" in message else "dynamic_body")

    row = d.inspect_dynamic(client, key, now=now, on_links=on_links,
                            on_skip=on_skip, skip_video_cards=True)
    if not row and unreadable:
        raise RetryCheck("动态暂时无法核验", reason=unreadable[0])
    if not row:
        return linked, []
    notice = row.get("official_notice") or {}
    status = row["status"]
    # "joined" only describes the worker's account. Reassess the public evidence.
    if status == "joined":
        public_notice = {k: v for k, v in notice.items() if k != "participated"}
        status = d.assess_candidate(row["evidence"], now=now, notice=public_notice, require_deadline=True)["status"]
    draw = row.get("draw_time")
    if status not in {"open", "closed"} or not draw:
        return linked, []
    activity = Activity(dynamic_id=key, source_url=opus_link(key),
                        lottery_type="互动抽奖" if notice else "转发抽奖",
                        lottery_time=draw, deadline=row.get("deadline"),
                        prize=str(row.get("prize") or "")[:300], closed=status == "closed")
    return linked, [activity]


def _follow_reference(client, ref, now):
    if ref.kind == "dynamic":
        return _dynamic(client, ref.key, now)
    if ref.kind == "article":
        url = f"https://www.bilibili.com/read/cv{ref.key}"
        data = read_data(client, d.ARTICLE_URL, {"id": int(ref.key)}, referer=url)
        return references("\n".join(d.extract_opus_links_from_article(data))), []
    if ref.kind == "short":
        return references(client.resolve_short_link(f"https://b23.tv/{ref.key}") or ""), []
    rows, _metadata = search_author_lotteries(client, ref.key, limit=4)
    return references("\n".join(opus_link(str(row.get("id_str") or "")) for row in rows)), []


def _detect_video(client, key, activities):
    pending = deque((ref, 1) for ref in _video(client, key))
    seen = set()
    retry = None
    while pending and len(seen) < MAX_VIDEO_REFERENCES:
        ref, depth = pending.popleft()
        identity = ref.kind, ref.key
        if identity in seen:
            continue
        seen.add(identity)
        try:
            links, verified = _follow_reference(client, ref, int(time.time()))
        except (RetryCheck, httpx.HTTPError, BilibiliRequestError) as exc:
            if retry is None:
                retry = exc
            continue
        activities.extend(verified)
        if depth < MAX_REFERENCE_DEPTH:
            pending.extend((link, depth + 1) for link in links)
    if retry is not None:
        raise retry


def _check_watched_author(client, mid, activities, *, publish=None):
    now = int(time.time())
    offset = ""
    seen_offsets, candidates = set(), set()
    for _ in range(WATCH_SCAN_PAGES):
        data = read_data(client, SPACE_FEED_URL,
                         {"host_mid": int(mid), "offset": offset, "type": "all", "platform": "web",
                          "timezone_offset": -480, "web_location": "333.1365"},
                         referer=f"https://space.bilibili.com/{mid}/dynamic", wbi=True, ttl=90)
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            published = extract_feed_pub_ts(item)
            if published and not now - WATCH_LOOKBACK <= published <= now:
                continue
            original = extract_forward_orig_id(item)
            if original and len(original) in (18, 19) and original.isascii():
                candidates.add(original)
            # The official lottery marker can be present even when the body is short.
            text = d._extract_from_detail_item(d._normalize_dynamic_item(item))
            key = str(item.get("id_str") or "")
            if (len(key) in (18, 19) and key.isascii() and key.isdecimal()
                    and (d.LOTTERY_RE.search(text) or "RICH_TEXT_NODE_TYPE_LOTTERY" in json.dumps(item))):
                candidates.add(key)
        offset = str(data.get("offset") or "")
        if not data.get("has_more") or not offset or offset in seen_offsets:
            break
        seen_offsets.add(offset)
    retry = None
    for key in sorted(candidates, key=int, reverse=True)[:MAX_VIDEO_REFERENCES]:
        try:
            _, matches = _dynamic(client, key, now)
        except (RetryCheck, httpx.HTTPError, BilibiliRequestError) as exc:
            retry = retry or exc
            continue
        if publish and matches:
            publish(matches)
        else:
            activities.extend(matches)
    if retry is not None:
        raise retry


def _check_collection(client, mid, publish):
    from web.actions import DS_HANDLER_BY_ID
    from web.activity_service import SOURCE_SPACE_URLS

    source = next((key for key, url in SOURCE_SPACE_URLS.items()
                   if url.startswith(f"https://space.bilibili.com/{mid}/")), None)
    if source is None:
        return
    check, _ = DS_HANDLER_BY_ID[source]
    result = check(force=True)
    pending = deque((ref, 1) for link in result.activity_links for ref in references(link))
    seen = set()
    retry = None
    while pending:
        ref, depth = pending.popleft()
        if (ref.kind, ref.key) in seen:
            continue
        seen.add((ref.kind, ref.key))
        try:
            links, matches = _follow_reference(client, ref, int(time.time()))
            if matches:
                publish(matches)
        except (RetryCheck, httpx.HTTPError, BilibiliRequestError) as exc:
            retry = retry or exc
            continue
        if depth < MAX_REFERENCE_DEPTH:
            pending.extend((link, depth + 1) for link in links)
    if retry is not None:
        raise retry


def execute(task: Task, cache_path: Path, get_analysis, put_analysis, *, two_stage=True, publish=None) -> Report:
    result = {"task_id": task.task_id, "lease": task.lease, "outcome": "done"}
    client = WorkClient(cache_path, get_analysis, put_analysis)
    try:
        if task.kind == "search":
            result.update(_search(client, task))
        elif task.kind == "watch":
            result["activities"] = activities = []
            if publish:
                source_error = None
                try:
                    _check_collection(client, task.key, publish)
                except (RetryCheck, httpx.HTTPError, RuntimeError) as exc:
                    source_error = exc
                _check_watched_author(client, task.key, activities, publish=publish)
                if source_error:
                    raise source_error
            else:
                _check_watched_author(client, task.key, activities)
        elif task.kind == "video" and two_stage:
            activities = []
            result["activities"] = activities
            _detect_video(client, task.key, activities)
        elif task.kind == "video":  # Legacy v1 host verifies the returned links separately.
            result["references"] = _video(client, task.key)
        else:
            ref = Reference(kind=task.kind, key=task.key)
            result["references"], result["activities"] = _follow_reference(client, ref, int(time.time()))
    except d.DiscoveryStopped:
        result.update(outcome="cooldown", cooldown=min(86400, max(900, math.ceil(
            client.cache.get("paused_until", 0) - time.time()))))
    except RetryCheck as exc:
        result.update(outcome="retry", reason=exc.reason)
    except d.DiscoveryModelError:
        result.update(outcome="retry", reason="model")
    except (httpx.HTTPError, BilibiliRequestError):
        result.update(outcome="retry", reason="network")
    except (RuntimeError, ValueError):
        result.update(outcome="retry", reason="worker")
    finally:
        result["model_calls"] = client.llm_requests
        client.close()
        client.save()
    return Report(**result)
