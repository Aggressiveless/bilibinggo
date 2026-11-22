"""Move verified discovery results into the local activity list."""
from __future__ import annotations

import time
from collections import Counter

import httpx

from src.activity_store import known_activity_ids
from src.control_store import ControlError
from src.log_redact import redact_text
from src.lottery_discovery import DiscoveryStopped
from src.participation_store import load_participations
from src.pipeline.refresh_all_pipeline import run_new_links_pipeline
from web.activity_service import invalidate_activity_cache


def verified_candidates(report):
    return {row["dynamic_id"]: row for row in report.get("candidates", []) if row.get("eligible") and row.get("dynamic_id")}


def queue_summary(service, job_id, report):
    stored = service.store.get("discovery_queue", str(job_id)) or {}
    candidates = verified_candidates(report)
    outcomes = {identifier: outcome for identifier, outcome in stored.get("outcomes", {}).items() if identifier in candidates}
    counts = Counter(outcomes.values())
    return {"total": len(candidates), "imported": counts["imported"], "existing": counts["existing"],
            "expired": counts["expired"], "skipped": counts["skipped"], "remaining": len(candidates) - len(outcomes),
            "message": stored.get("message", "")}


def _still_open(activity):
    return (activity.draw_status == "active" and not activity.platform_participated and not activity.reserve_reserved
            and (not activity.lottery_time or activity.lottery_time > time.time()))


def enqueue_verified(service, job_id, client, cancel_event, on_progress, selected_ids=None):
    report = service.store.get("discovery", str(job_id))
    if not report:
        raise ControlError("NOT_FOUND", "检索报告不存在")
    candidates = verified_candidates(report)
    if selected_ids is not None:
        candidates = {identifier: candidates[identifier] for identifier in selected_ids if identifier in candidates}
    outcomes = (service.store.get("discovery_queue", str(job_id)) or {}).get("outcomes", {})
    known = known_activity_ids()
    joined = {str(key) for key, value in load_participations().items() if value.user_status == "已参加"}
    stop_message = ""

    def accept(activity):
        return not client.stop_reason and _still_open(activity)

    for index, (identifier, row) in enumerate(candidates.items()):
        if identifier in outcomes:
            continue
        if cancel_event is not None and cancel_event.is_set():
            stop_message = "已停止导入，剩余活动可继续导入"
            break
        on_progress(step=index, total=len(candidates), message=f"正在导入活动 {index + 1}/{len(candidates)}")
        raw = row.get("raw") or {}
        if identifier in known or identifier in joined:
            outcome = "existing"
        elif any(raw.get(key) and raw[key] <= time.time() for key in ("deadline", "draw_time")):
            outcome = "expired"
        else:
            try:
                result = run_new_links_pipeline([row["target_url"]], workers=1, client=client, accept=accept)
                if client.stop_reason:
                    raise DiscoveryStopped(client.stop_reason)
                if not result.ok:
                    raise RuntimeError(result.message)
            except (DiscoveryStopped, httpx.HTTPError, RuntimeError, ValueError) as exc:
                stop_message = "活动导入暂停：" + redact_text(str(exc))[:300]
                break
            outcome = "imported" if result.persisted_count else "skipped"
            if result.persisted_count:
                invalidate_activity_cache()
        outcomes[identifier] = outcome
        service.store.put("discovery_queue", str(job_id), {"outcomes": outcomes, "message": ""})
    service.store.put("discovery_queue", str(job_id), {"outcomes": outcomes, "message": stop_message})
    return queue_summary(service, job_id, report)
