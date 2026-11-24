"""Per-video evidence from discovery and the current import status of its targets."""
from __future__ import annotations

import html
import re
from collections import Counter

from src.lottery_discovery import DRAW_ONLY_INVITATION, MISSING_DYNAMIC_DEADLINE


def video_title(value) -> str:
    # Search responses wrap matched text in <em> tags.
    return " ".join(html.unescape(re.sub(r"</?em(?:\s[^>]*)?>", "", str(value or ""), flags=re.I)).split())[:300]


def audit_note(status, reason, **details):
    return {"status": status, "reason": reason, **details}


def _source_ids(item):
    return [url.rsplit("/", 1)[-1] for url in item.get("source_videos", [item["source_video"]])]


def snapshot_video_checks(state, rows, *, legacy=False, video_range=None):
    start, stop = video_range if video_range is not None else (0, len(state["videos"]))
    videos = state["videos"][start:stop]
    checks = {video["bvid"]: {"title": video_title(video.get("title")), "notes": []} for video in videos}
    for bvid, check in checks.items():
        data = state.get("scan_notes", {}).get(bvid, {})
        if data.get("title"):
            check["title"] = data["title"]
        check["notes"].extend(data.get("notes", []))
        if data.get("source_reason"):
            check["source_reason"] = data["source_reason"]

    dynamics = {row["dynamic_id"]: row for row in rows if row.get("dynamic_id")}
    comments = {row.get("source_video"): row for row in rows if row.get("kind") == "video_comment"}

    def add(item, note):
        for bvid in _source_ids(item):
            if bvid in checks:
                checks[bvid]["notes"].append(note)

    for index, source in enumerate(state.get("source_tasks", [])):
        if index >= state.get("source_index", 0):
            label = {"article": "等待读取专栏", "short": "等待展开抽奖传送门", "video": "等待模型判断视频线索",
                     "author": "等待检索发布者主页"}[source["kind"]]
            add(source, audit_note("pending", label))
        elif source.get("audit"):
            add(source, source["audit"])

    for index, task in enumerate(state.get("tasks", [])):
        identifier = task.get("dynamic_id")
        row = dynamics.get(identifier) if identifier else comments.get(task["source_video"])
        if index >= state.get("verify_index", 0):
            note = audit_note("pending", "等待核验关联动态" if identifier else "等待核验视频评论区规则")
        elif row:
            note = audit_note(row["status"], row.get("reason") or "已核验候选", kind=row["kind"],
                              dynamic_id=identifier, deadline=row.get("deadline"), draw_time=row.get("draw_time"),
                              model_reason=row.get("model_reason") or None, model_assessment=row.get("model_assessment"),
                              model_confidence=row.get("model_confidence"), rules_summary=row.get("rules_summary"),
                              followers=row.get("followers"), prize=row.get("prize"))
            if task.get("audit"):
                add(task, task["audit"])
        else:
            note = task.get("audit") or audit_note("pending", "旧版未保留该候选的核验依据")
        add(task, note)

    for index, check in enumerate(checks.values(), start=start):
        if not check["notes"]:
            reason = "旧版未记录该视频的逐条判定" if legacy and index < state["scan_index"] else "等待扫描视频标题、简介和作者评论"
            check["notes"].append(audit_note("pending", reason))
    return checks


def display_video_check(row, *, known, joined, outcomes, now):
    check = row["payload"]
    legacy = bool(row["legacy_history"])
    fallback = "旧版只记录了收录时间，等下次扫描补齐" if legacy else "已收录，尚未进入视频扫描范围"
    source_notes = check.get("notes") or [audit_note("pending", fallback)]
    reasons, status_counts, activities = Counter(), Counter(), []
    for note in source_notes:
        status, reason = note["status"], note["reason"]
        identifier = note.get("dynamic_id")
        outcome = outcomes.get(identifier)
        if outcome == "imported":
            status, reason = "handled", "关联活动已导入"
        elif status == "joined" or identifier in joined:
            status, reason = "handled", "关联活动已参与"
        elif outcome == "existing" or identifier in known:
            status, reason = "handled", "活动列表已有该动态"
        elif status == "open" and identifier:
            if outcome == "expired" or any(note.get(key) and note[key] <= now for key in ("deadline", "draw_time")):
                status, reason = "rejected", "关联活动已超过报名截止或开奖时间"
            elif outcome == "skipped":
                status, reason = "rejected", "导入时复核未通过"
            elif reason == DRAW_ONLY_INVITATION:
                status, reason = "rejected", MISSING_DYNAMIC_DEADLINE
            else:
                status = "ready"
        elif status == "open":
            status, reason = "video_only", "已确认是视频评论区抽奖，可从原视频查看参与方式"
        elif status == "review" and identifier:
            status = "source" if reason.startswith("关联动态包含") else "rejected"
            if reason == "缺少可核验的报名截止或进行中状态":
                reason = "动态缺少可核验的报名截止时间或平台有效期"
        elif status == "review" and reason == "视频提供了抽奖线索，待核验关联活动":
            status, reason = "source", "旧版记录了视频抽奖线索，关联结果见下方动态"
        elif status not in {"pending", "source"}:
            status = "rejected"
        status_counts[status] += 1
        reasons[(status, reason, note.get("model_reason"), note.get("model_assessment"), note.get("kind"),
                 note.get("model_confidence"), note.get("rules_summary"))] += 1
        if identifier:
            activities.append({"dynamic_id": identifier, "status": status, "reason": reason,
                               **{key: note.get(key) for key in ("prize", "rules_summary", "model_reason", "model_confidence", "followers")}})
    status = next(value for value in ("ready", "pending", "video_only", "handled", "rejected", "source") if status_counts[value])
    ordered = sorted(reasons.items(), key=lambda entry: (entry[0][0] != status, not entry[0][2]))
    return {"bvid": row["bvid"], "title": check.get("title") or row["title"],
            "first_seen": row["first_seen"], "last_seen": row["last_seen"], "status": status,
            "repeat_count": max(0, row["sightings"] - int(not legacy)), "repeat_count_incomplete": legacy,
            "source_reason": check.get("source_reason"), "activities": activities,
            "reasons": [{"message": reason, "model_reason": model, "model_assessment": assessment, "kind": kind,
                         "model_confidence": confidence, "rules_summary": summary, "count": count}
                        for (_, reason, model, assessment, kind, confidence, summary), count in ordered]}
