"""Breadth-first page discovery with per-author keyword search and durable cursors."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime

import httpx

from src import lottery_discovery as d
from src.discovery_video_audit import audit_note, snapshot_video_checks, video_title

AUTHOR_SEARCH_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space/search"
AUTHOR_SEARCH_FEATURES = "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,forwardListHidden,decorationCard"
SESSION_TTL_SECONDS = 1800


def _legacy_scan_notes(session, entries, finished):
    def cached_data(url, params):
        key = json.dumps([url, params], sort_keys=True, ensure_ascii=False)
        entry = entries.get(key, {})
        if 0 < entry.get("fetched_at", 0) <= finished < entry.get("expires", 0):
            return (entry.get("value") or {}).get("data")
        return None

    notes = {}
    for row in session["videos"][:session["scan_index"]]:
        bvid = row["bvid"]
        video = cached_data(d.VIDEO_URL, {"bvid": bvid})
        if not video:
            continue
        title, owner = video.get("title"), (video.get("owner") or {}).get("mid")
        texts = [str(title or row.get("title") or ""), str(video.get("desc") or "")]
        notes[bvid] = {"title": video_title(title), "notes": []}
        if video.get("aid") and owner:
            replies = cached_data(d.REPLIES_URL, {"type": 1, "oid": video["aid"], "mode": 3})
            if replies is None:
                continue
            texts.extend(d._owner_comments(replies, str(owner)))
        text = "\n".join(texts)
        if not any(pattern.search(text) for pattern in (d.DYNAMIC_RE, d.ARTICLE_RE, d.LOTTERY_RE)):
            notes[bvid]["notes"].append(audit_note("rejected", "缓存的标题、简介和可读作者评论未见抽奖线索或入口，未送模型"))
    return notes


def restore_video_index(store, cache_path):
    if store.get("discovery_meta", "video_audit_restored"):
        return
    cache = d._read_cache(cache_path)
    sessions = cache.get("broad_sessions", {})
    pages = [session.get("videos", []) for session in sorted(sessions.values(), key=lambda s: s.get("last_used_at", 0), reverse=True)]
    for key, entry in cache.get("entries", {}).items():
        if key.startswith(json.dumps([d.SEARCH_URL])[:-1] + ","):
            pages.append(((entry.get("value") or {}).get("data") or {}).get("result") or [])
    titles = {}
    for page in pages:
        for row in page:
            bvid = str(row.get("bvid") or "")
            if d.BV_RE.fullmatch(bvid) and not titles.get(bvid):
                titles[bvid] = video_title(row.get("title"))
    store.remember_discovery_videos(({"bvid": bvid, "title": title} for bvid, title in titles.items()), backfill=True)
    # A cached cursor can only explain the job that last wrote it.
    remaining = set(sessions)
    for job in store.list_jobs(limit=1000) if remaining else []:
        if job["action"] != "control_discover" or not job.get("finished_at"):
            continue
        result = job.get("result") or {}
        key = result.get("session_id")
        if key not in remaining:
            continue
        session = sessions[key]
        finished = datetime.fromisoformat(job["finished_at"]).timestamp()
        if session.get("capture_id") or abs(finished - session.get("last_used_at", 0)) > 60:
            continue
        report = store.get("discovery", str(job["job_id"])) or {}
        saved = {row["dynamic_id"]: row["raw"] for row in report.get("candidates", []) if row.get("dynamic_id")}
        rows = [saved.get(row.get("dynamic_id"), row) for row in session.get("rows", [])]
        checked_at = (report.get("raw") or {}).get("checked_at")
        if checked_at and not result.get("resumed"):
            session = {**session, "scan_notes": _legacy_scan_notes(session, cache.get("entries", {}), finished)}
        store.save_discovery_video_checks(job["job_id"], snapshot_video_checks(session, rows, legacy=True))
        remaining.remove(key)
        if not remaining:
            break
    store.put("discovery_meta", "video_audit_restored", {"done": True})


def search_author_lotteries(client, uid: str, *, limit=2):
    """Use the same keyword-search endpoint as Bilibili's current space UI."""
    referer = f"https://space.bilibili.com/{uid}/search/dynamic?keyword=%E6%8A%BD%E5%A5%96"
    response = client.read(AUTHOR_SEARCH_URL, {"host_mid": uid, "page": 1, "offset": "", "keyword": "抽奖",
        "features": AUTHOR_SEARCH_FEATURES}, referer=referer, ttl=600)
    if d.api_code(response) != 0:
        raise RuntimeError(f"作者关键词检索失败（业务码 {d.api_code(response)}）")
    data = response.get("data") or {}
    items = []
    for item in data.get("items") or []:
        item = d._normalize_dynamic_item(item)
        author = (item.get("modules") or {}).get("module_author") or {}
        identifier = str(item.get("id_str") or "")
        if str(author.get("mid") or "") != uid or not re.fullmatch(r"\d{15,22}", identifier):
            continue
        if item.get("type") == "DYNAMIC_TYPE_FORWARD":
            continue
        items.append(item)
    # The UI search can place pinned/relevant posts before recent posts.
    items.sort(key=lambda item: (d.extract_feed_pub_ts(item) or 0, int(item["id_str"])), reverse=True)
    return items[:limit], {"search_url": referer, "returned": len(data.get("items") or []),
                           "original_matches": len(items), "has_more": bool(data.get("has_more"))}


def _fair_order(tasks):
    groups = defaultdict(deque)
    for task in tasks:
        groups[task["source_video"]].append(task)
    result = []
    while groups:
        for source in list(groups):
            result.append(groups[source].popleft())
            if not groups[source]:
                del groups[source]
    return result


def discover_page(client, keywords, *, video_limit=36, now=None, comprehensive_page=True,
                  candidate_limit=9, known_ids=None, joined_ids=None, author_dynamic_limit=2,
                  resume=True, replay_completed=True, on_progress=None, on_video_page=None, collect_followers=False):
    now = int(time.time()) if now is None else now
    known_ids, joined_ids = set(known_ids or []), set(joined_ids or [])
    cache = getattr(client, "cache", {})
    sessions = cache.setdefault("broad_sessions", {})
    key = hashlib.sha256(json.dumps([6, keywords, video_limit, comprehensive_page, author_dynamic_limit], ensure_ascii=False).encode()).hexdigest()[:24]
    previous = sessions.get(key)
    resumed = bool(resume and previous and previous.get("expires_at", 0) > time.time()
                   and (replay_completed or not previous.get("done")))
    state = previous if resumed else {"session_id": key, "expires_at": time.time() + SESSION_TTL_SECONDS,
        "search_index": 0, "videos": [], "scan_index": 0, "source_index": 0, "verify_index": 0,
        "source_tasks": [], "tasks": [], "rows": [], "errors": [], "author_queries": {},
        "phase": "search", "done": False, "verified": 0, "capture_id": uuid.uuid4().hex}
    sessions[key] = state
    legacy = state.setdefault("legacy_scan", "capture_id" not in state)
    capture_id = state.setdefault("capture_id", uuid.uuid4().hex)
    scan_notes = state.setdefault("scan_notes", {})
    # Bound storage without dropping this run's cursor.
    for old in sorted([k for k in sessions if k != key], key=lambda k: sessions[k].get("expires_at", 0))[:-3]:
        del sessions[old]
    stopped, completion = "", "search_complete"
    added_open = 0
    started = time.monotonic()
    follower_counts = state.setdefault("follower_counts", {}) if collect_followers else {}

    def scan_completed():
        return state["search_index"] == len(keywords) and state["scan_index"] == len(state["videos"])

    def current_rows():
        values = []
        for original in state["rows"]:
            row = dict(original)
            identifier = row.get("dynamic_id")
            uid = row.get("publisher_uid")
            if uid in follower_counts:
                row["followers"] = follower_counts[uid]
            if identifier in joined_ids:
                row.update(status="joined", reason="本地已有该账号的成功参与记录")
            elif identifier in known_ids:
                row.update(status="review", reason="本地活动库已有该活动，无需重复导入")
            elif row.get("status") == "open" and any(row.get(k) and row[k] <= now for k in ("deadline", "draw_time")):
                row.update(status="closed", reason="报名截止或预定开奖时间已过")
            values.append(row)
        return values

    def emit(message):
        if not on_progress:
            return
        rows = current_rows()
        on_progress({"phase": state["phase"], "message": message, "videos_checked": state["scan_index"],
            "search_video_count": len(state["videos"]), "requests": client.request_count,
            "eligible": sum(r["status"] == "open" and r.get("kind") == "dynamic" for r in rows), "rows": rows, "errors": state["errors"],
            "cache_hits": getattr(client, "cache_hits", 0), "llm_requests": getattr(client, "llm_requests", 0),
            "llm_cache_hits": getattr(client, "llm_cache_hits", 0), "session_id": key, "resumed": resumed,
            "scan_completed": scan_completed(), "verification_remaining": len(state["tasks"]) - state["verify_index"],
            "video_checks": snapshot_video_checks(state, rows, legacy=legacy)})

    def attach_source(existing, duplicate):
        sources = existing.setdefault("source_videos", [existing["source_video"]])
        for url in duplicate.get("source_videos", [duplicate["source_video"]]):
            if url not in sources:
                sources.append(url)

    def add_task(task):
        identifier = task.get("dynamic_id")
        existing = next((t for t in state["tasks"] if identifier and t.get("dynamic_id") == identifier), None)
        if existing:
            attach_source(existing, task)
            return
        if len(state["tasks"]) < 400:
            state["tasks"].append(task)
        else:
            for url in task.get("source_videos", [task["source_video"]]):
                detail = scan_notes.setdefault(url.rsplit("/", 1)[-1], {"title": "", "notes": []})
                detail["notes"].append(audit_note("pending", "达到本轮候选上限，部分入口未核验"))

    def add_source(task):
        identity = (task["kind"], task.get("uid") or task.get("cv_id"))
        existing = next((t for t in state["source_tasks"] if (t["kind"], t.get("uid") or t.get("cv_id")) == identity), None)
        if existing:
            attach_source(existing, task)
        else:
            state["source_tasks"].append(task)

    try:
        while state["search_index"] < len(keywords):
            state["phase"] = "search"
            word = keywords[state["search_index"]]
            emit(f"获取搜索第一页：{word}")
            params = {"search_type": "video", "keyword": word, "page": 1, "page_size": 36,
                      "order": "totalrank" if comprehensive_page else "pubdate"}
            response = client.read(d.SEARCH_URL, params, referer="https://search.bilibili.com/", ttl=1800, wbi=True)
            if d.api_code(response) != 0 or not isinstance(response.get("data"), dict):
                raise RuntimeError(f"视频搜索未返回有效结果（业务码 {d.api_code(response)}）")
            seen = {row["bvid"] for row in state["videos"]}
            page = [row for row in response["data"].get("result") or [] if d.BV_RE.fullmatch(str(row.get("bvid") or ""))]
            if on_video_page:
                on_video_page(({"bvid": row["bvid"], "title": video_title(row.get("title"))} for row in page),
                              page_id=f"{capture_id}:{state['search_index']}")
            for row in page:
                identifier = row["bvid"]
                if identifier not in seen and len(state["videos"]) < video_limit:
                    state["videos"].append(row)
                    seen.add(identifier)
            state["search_index"] += 1

        # No article, author lookup, target verification or LLM extraction is allowed
        # before the whole selected video page has been collected.
        while state["scan_index"] < len(state["videos"]):
            state["phase"] = "scan_videos"
            row = state["videos"][state["scan_index"]]
            bvid = row["bvid"]
            url = f"https://www.bilibili.com/video/{bvid}"
            emit(f"扫描视频 {state['scan_index'] + 1}/{len(state['videos'])}：{video_title(row.get('title'))[:60] or bvid}")
            detail = scan_notes[bvid] = {"title": "", "notes": []}
            try:
                response = client.read(d.VIDEO_URL, {"bvid": bvid}, referer=url, ttl=900)
                video = response.get("data") or {}
                if not video:
                    raise RuntimeError("视频详情不可读")
                detail["title"] = video_title(video.get("title"))
                owner = video.get("owner") or {}
                texts = [str(video.get("title") or row.get("title") or ""), str(video.get("desc") or "")]
                if video.get("aid") and owner.get("mid"):
                    try:
                        replies = client.read(d.REPLIES_URL, {"type": 1, "oid": video["aid"], "mode": 3}, referer=url, ttl=900, wbi=True)
                        if d.api_code(replies) != 0:
                            raise RuntimeError(f"评论接口返回业务码 {d.api_code(replies)}")
                        texts.extend(d._owner_comments(replies.get("data") or {}, str(owner["mid"])))
                    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                        state["errors"].append(f"{bvid}: 评论不可读：{str(exc)[:150]}")
                        detail["notes"].append(audit_note("pending", "作者评论未能读取，核验资料不完整"))
                text = "\n".join(texts)
                identifiers = sorted(set(d.DYNAMIC_RE.findall(text)), key=int, reverse=True)
                for identifier in identifiers[:24]:
                    add_task({"kind": "dynamic", "dynamic_id": identifier, "source_video": url, "found_via": "video_link"})
                articles = sorted(set(d.ARTICLE_RE.findall(text)), key=int, reverse=True)
                for cv in articles[:2]:
                    add_source({"kind": "article", "cv_id": cv, "source_video": url})
                lottery = bool(d.LOTTERY_RE.search(text))
                explicit_home = d._home_participation_hint(text)
                video_comment = lottery and bool(d.COMMENT_ENTRY_RE.search(text) or d.VIDEO_COMMENT_RE.search(text) and d.COMMENT_RE.search(text))
                home = explicit_home or lottery and not (identifiers or articles or video_comment)
                if home and str(owner.get("mid") or "").isdigit():
                    add_source({"kind": "author", "uid": str(owner["mid"]), "owner": owner,
                                "source_video": url, "text": text[:4800]})
                elif home:
                    detail["notes"].append(audit_note("pending", "有抽奖线索，但缺少发布者 UID，无法检索主页"))
                if video_comment and not (identifiers or articles or explicit_home):
                    add_task({"kind": "video_comment", "source_video": url, "text": text[:4800],
                              "published": video.get("pubdate") or row.get("pubdate") or 0, "owner": owner})
                elif not (identifiers or articles or home or detail["notes"]):
                    detail["notes"].append(audit_note("rejected", "标题、简介和可读的作者评论里未发现抽奖线索或入口，未送模型"))
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                state["errors"].append(f"{bvid}: {str(exc)[:200]}")
                detail["notes"].append(audit_note("pending", "视频详情未能读取，无法核验"))
            state["scan_index"] += 1
        emit(f"整页扫描完成：{state['scan_index']} 个视频，开始解析入口")

        while state["source_index"] < len(state["source_tasks"]):
            state["phase"] = "resolve_sources"
            source = state["source_tasks"][state["source_index"]]
            try:
                if source["kind"] == "article":
                    article_url = f"https://www.bilibili.com/read/cv{source['cv_id']}"
                    emit(f"解析合集专栏 cv{source['cv_id']}")
                    response = client.read(d.ARTICLE_URL, {"id": int(source["cv_id"])}, referer=article_url, ttl=600)
                    if d.api_code(response) != 0 or not response.get("data"):
                        raise RuntimeError("专栏内容不可读")
                    links = d.extract_opus_links_from_article(response.get("data") or {})
                    identifiers = sorted(set(d.DYNAMIC_RE.findall("\n".join(links))), key=int, reverse=True)[:24]
                    if not identifiers:
                        source["audit"] = audit_note("rejected", "关联专栏中没有可识别的抽奖动态链接")
                    for identifier in identifiers:
                        add_task({"kind": "dynamic", "dynamic_id": identifier, "source_video": source["source_video"],
                                  "source_videos": source.get("source_videos", [source["source_video"]]),
                                  "source_article": article_url, "found_via": "article_link"})
                else:
                    uid = source["uid"]
                    emit(f"作者 {uid} 主页搜索“抽奖”，取最近 {author_dynamic_limit} 条原创动态")
                    items, metadata = search_author_lotteries(client, uid, limit=author_dynamic_limit)
                    state["author_queries"][uid] = metadata
                    if not items:
                        source["audit"] = audit_note("rejected", "发布者主页搜索未返回相关的原创抽奖动态")
                    for item in items:
                        add_task({"kind": "dynamic", "dynamic_id": str(item["id_str"]), "item": item,
                                  "source_video": source["source_video"], "source_text": source["text"],
                                  "source_videos": source.get("source_videos", [source["source_video"]]),
                                  "source_home": metadata["search_url"], "home_group": uid, "found_via": "publisher_home"})
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                state["errors"].append(f"入口解析失败：{str(exc)[:200]}")
                source["audit"] = audit_note("pending", "关联专栏读取失败" if source["kind"] == "article" else "发布者主页检索失败")
            state["source_index"] += 1
        if state["verify_index"] == 0:
            state["tasks"] = _fair_order(state["tasks"])

        while state["verify_index"] < len(state["tasks"]):
            state["phase"] = "verify_candidates"
            task = state["tasks"][state["verify_index"]]
            identifier = task.get("dynamic_id")
            emit(f"核验候选 {state['verify_index'] + 1}/{len(state['tasks'])}：{identifier or task['source_video']}")
            try:
                if identifier in known_ids or identifier in joined_ids:
                    row = {"kind": "dynamic", "url": d.opus_link(identifier), "dynamic_id": identifier,
                           "status": "review", "reason": "本地活动库已有该活动，无需重复导入", "prize": "",
                           "deadline": None, "draw_time": None, "evidence": {}}
                elif task["kind"] == "video_comment":
                    fields = client.extract(task["text"])
                    owner = task.get("owner") or {}
                    row = {"kind": "video_comment", "url": task["source_video"], "source_video": task["source_video"],
                           "publisher": owner.get("name") or "", "publisher_uid": str(owner.get("mid") or ""),
                           "prize": fields.get("prize", ""), **d.assess_candidate(fields, now=now, published=task["published"])}
                else:
                    def note_skip(status, reason):
                        task["audit"] = audit_note(status, reason)
                    row = d.inspect_dynamic(client, identifier, now=now, item=task.get("item"), on_skip=note_skip)
                if row:
                    row.update(source_video=task["source_video"], found_via=task.get("found_via", "video_comment"),
                        source_chain=[{"source": task["source_video"]},
                                      *([{"source": task["source_article"]}] if task.get("source_article") else []),
                                      *([{"source": task["source_home"], "keyword": "抽奖"}] if task.get("source_home") else []),
                                      {"target": row["url"]}])
                    if task.get("home_group"):
                        row["home_group"] = task["home_group"]
                    state["rows"].append(row)
                    added_open += int(row["status"] == "open" and row["kind"] == "dynamic")
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                state["errors"].append(f"候选 {identifier or task['source_video']}: {str(exc)[:200]}")
                reason = "模型请求失败或未返回有效结果" if isinstance(exc, d.DiscoveryModelError) else "核验请求失败，未取得可用判定"
                task["audit"] = audit_note("pending", reason)
            state["verify_index"] += 1
            state["verified"] += 1
            if candidate_limit and added_open >= candidate_limit:
                completion = "target_reached"
                break
        state["done"] = False
        if collect_followers:
            publishers = list(dict.fromkeys(row["publisher_uid"] for row in current_rows()
                if row["status"] == "open" and row.get("kind") == "dynamic"
                and str(row.get("publisher_uid") or "").isdigit()))
            for index, uid in enumerate(publishers, 1):
                if uid in follower_counts:
                    continue
                state["phase"] = "publisher_stats"
                emit(f"读取发布者粉丝数 {index}/{len(publishers)}")
                try:
                    follower_counts[uid] = d.read_follower_count(client, uid)
                except (httpx.HTTPError, RuntimeError, ValueError):
                    follower_counts[uid] = None
        state["done"] = state["verify_index"] == len(state["tasks"])
    except d.DiscoveryStopped as exc:
        stopped = str(exc)
        completion = "request_budget" if "预算" in stopped else ("cancelled" if "取消" in stopped else "platform_paused")
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        state["errors"].append(str(exc)[:200])
        completion = "error"

    rows = current_rows()
    # Each independently verified original post is a separate discovery, even
    # when a single video led to both. The video is only the discovery source.
    # A completed snapshot must expire even when polled repeatedly.
    state["last_used_at"] = time.time()
    if not state["done"]:
        state["expires_at"] = state["last_used_at"] + SESSION_TTL_SECONDS
    state["phase"] = "finished" if state["done"] else state["phase"]
    emit(f"本轮结束：视频 {state['scan_index']}/{len(state['videos'])}，剩余候选 {len(state['tasks']) - state['verify_index']}，进度已保存")
    result = {status: [r for r in rows if r["status"] == status] for status in ("open", "review", "closed", "joined")}
    return {"checked_at": now, "requests": client.request_count, "stopped": stopped, "errors": state["errors"],
        "completion_reason": completion, "search_video_count": len(state["videos"]), "videos_checked": state["scan_index"],
        "session_id": key, "resumed": resumed, "scan_completed": scan_completed(), "resume_expires_at": state["expires_at"],
        "has_more": not state["done"], "verification_remaining": len(state["tasks"]) - state["verify_index"],
        "sources_remaining": len(state["source_tasks"]) - state["source_index"], "author_queries": state["author_queries"],
        "cache_hits": getattr(client, "cache_hits", 0), "llm_requests": getattr(client, "llm_requests", 0),
        "llm_cache_hits": getattr(client, "llm_cache_hits", 0), "elapsed_seconds": round(time.monotonic() - started, 3),
        "min_interval_seconds": getattr(client, "min_interval_seconds", 2.0),
        "request_start_offsets": getattr(client, "request_start_offsets", []), **result}
