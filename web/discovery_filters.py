"""Post-search filtering of verified, unjoined activities."""
from __future__ import annotations

import re
import time
import uuid

import httpx

from src.activity_store import known_activity_ids
from src.control_store import ControlError
from src.lottery_discovery import FOLLOWER_URL, api_code
from src.participation_store import load_participations
from src.watch_user_profile import SPACE_CARD_URL
from web.discovery_queue import verified_candidates

COIN = re.compile(r"投币|一键三连|视频.{0,8}三连|投\s*[一二两１２12]\s*(?:个)?(?:硬)?币")
NO_COIN = re.compile(r"(?:不需要|无需|不用|不必|不要求|不强制|可以不|不)(?:再)?(?:投币|一键三连)|(?:投币|一键三连)(?:可选|随意|非必[须要]|不强制)")


def coin_requirement(row):
    raw = row.get("raw") or {}
    rules = (raw.get("evidence") or {}).get("rules") or raw.get("rule_text") or ""
    if not rules:
        return "unknown"
    if COIN.search(NO_COIN.sub("", rules)):
        return "required"
    return "not_required" if NO_COIN.search(rules) else "not_mentioned"


def pending_candidates(service, job_id, report):
    outcomes = (service.store.get("discovery_queue", str(job_id)) or {}).get("outcomes", {})
    known = known_activity_ids()
    joined = {str(key) for key, value in load_participations().items() if value.user_status == "已参加"}
    now = time.time()
    return {identifier: row for identifier, row in verified_candidates(report).items()
            if identifier not in outcomes and identifier not in known and identifier not in joined
            and not any((row.get("raw") or {}).get(key) and row["raw"][key] <= now for key in ("deadline", "draw_time"))}


def _public_stats(client, uid, settings, cache):
    if uid in cache:
        return cache[uid]
    values = {}
    if uid:
        profile = f"https://space.bilibili.com/{uid}"
        queries = []
        if settings["min_followers"]:
            queries.append(("followers", FOLLOWER_URL, {"vmid": uid}, lambda data: data.get("follower")))
        if settings["min_account_days"]:
            queries.append(("joined_at", SPACE_CARD_URL, {"mid": uid, "photo": 0},
                            lambda data: (data.get("card") or {}).get("regtime") or (data.get("card") or {}).get("jointime")))
        for field, url, params, extract in queries:
            try:
                payload = client.read(url, params, referer=profile, ttl=43200)
                value = extract(payload.get("data") or {}) if api_code(payload) == 0 else None
                if type(value) is int and value >= 0:
                    values[field] = value
            except (httpx.HTTPError, RuntimeError, ValueError):
                continue
    cache[uid] = values
    return values


def preview_filters(service, job_id, settings, client, progress):
    report = service.store.get("discovery", str(job_id))
    if not report:
        raise ControlError("NOT_FOUND", "检索报告不存在")
    candidates = pending_candidates(service, job_id, report)
    matches, metadata = [], {}
    unknown = 0
    now = time.time()
    for step, (identifier, row) in enumerate(candidates.items(), 1):
        progress(step=step, total=len(candidates), message=f"正在筛选 {step}/{len(candidates)}")
        coins = coin_requirement(row)
        if settings["coins"] == "required" and coins != "required":
            continue
        if settings["coins"] == "exclude_required" and coins == "required":
            continue
        if settings["coins"] == "not_required" and coins != "not_required":
            continue
        stats = _public_stats(client, row.get("author_mid"), settings, metadata)
        joined = stats.get("joined_at")
        age = int((now - joined) // 86400) if joined and 1104537600 <= joined <= now else None
        thresholds = [(stats.get("followers"), settings["min_followers"]), (age, settings["min_account_days"])]
        missing = any(minimum and value is None for value, minimum in thresholds)
        unknown += int(missing)
        if any(minimum and value is not None and value < minimum for value, minimum in thresholds):
            continue
        if not missing or settings["include_unknown"]:
            matches.append(identifier)
    result = {"id": uuid.uuid4().hex, "settings": settings, "matched": len(matches), "total": len(candidates),
              "unknown_metadata": unknown, "dynamic_ids": matches}
    service.store.put("discovery_filter", str(job_id), result)
    return {key: val for key, val in result.items() if key != "dynamic_ids"}
