
"""Read-only search and verification jobs. No account actions are performed here."""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

import httpx

from src import lottery_discovery as d
from src.bilibili_client import BilibiliRequestError
from src.llm_settings import get_llm_config
from src.lottery_discovery_broad import has_direct_video_entry, has_video_clue, search_author_lotteries
from src.sources.common import opus_link
from web.cluster_protocol import Activity, Reference, Report, Task


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
        raise RetryCheck("平台未返回可读取结果")
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
    skip = []

    def on_links(text):
        linked.extend(ref for ref in references(text) if ref.key != key)

    row = d.inspect_dynamic(client, key, now=now, on_links=on_links,
                            on_skip=lambda status, _reason: skip.append(status))
    if not row and "pending" in skip:
        raise RetryCheck("动态暂时无法核验")
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


def execute(task: Task, cache_path: Path, get_analysis, put_analysis) -> Report:
    result = {"task_id": task.task_id, "lease": task.lease, "outcome": "done"}
    client = WorkClient(cache_path, get_analysis, put_analysis)
    try:
        if task.kind == "search":
            result.update(_search(client, task))
        elif task.kind == "video":
            result["references"] = _video(client, task.key)
        elif task.kind == "article":
            url = f"https://www.bilibili.com/read/cv{task.key}"
            data = read_data(client, d.ARTICLE_URL, {"id": int(task.key)}, referer=url)
            result["references"] = references("\n".join(d.extract_opus_links_from_article(data)))
        elif task.kind == "short":
            target = client.resolve_short_link(f"https://b23.tv/{task.key}")
            result["references"] = references(target or "")
        elif task.kind == "author":
            rows, _metadata = search_author_lotteries(client, task.key, limit=4)
            result["references"] = references("\n".join(opus_link(str(row.get("id_str") or "")) for row in rows))
        else:
            result["references"], result["activities"] = _dynamic(client, task.key, int(time.time()))
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
