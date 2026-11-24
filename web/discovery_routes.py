"""Browser controls for the same discovery jobs used by MCP."""
from __future__ import annotations

import re
import time
from collections import Counter
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.control_store import ControlError
from src.discovery_video_audit import display_video_check
from web.api_errors import AppError, ErrorCode
from web.control_service import get_control_service
from web.discovery_filters import pending_candidates, triple_requirement
from web.discovery_queue import queue_summary, verified_candidates
from web.coin_balance import coin_balance


class DiscoveryStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(default_factory=lambda: ["转发抽奖"], min_length=1, max_length=3)
    keyword_match: Literal["combined", "separate"] = "separate"
    mode: Literal["standard", "comprehensive_page"] = "standard"
    target_new_videos: int = Field(default=100, ge=1, le=10000, strict=True)

    @model_validator(mode="after")
    def clean_keywords(self):
        if self.keyword_match == "combined":
            query = re.sub(r"[,，、\s]+", " ", " ".join(self.keywords)).strip()
            if not query or len(query) > 242:
                raise ValueError("请输入不超过 242 字的组合查询")
            self.keywords = [query]
        else:
            words = list(dict.fromkeys(word.strip() for word in self.keywords))
            if any(not word or len(word) > 80 for word in words):
                raise ValueError("请输入 1–3 个独立查询词，每个不超过 80 字")
            self.keywords = words
        return self


def discovery_job_params(request: DiscoveryStartRequest):
    if request.mode == "comprehensive_page" and len(request.keywords) != 1:
        raise AppError(ErrorCode.VALIDATION_ERROR, "综合排序每轮只运行一个查询，请使用组合搜索")
    return {"keywords": request.keywords, "mode": request.mode, "video_limit": request.target_new_videos,
            "keyword_match": request.keyword_match,
            "target_new_videos": request.target_new_videos,
            "request_budget": None, "candidate_limit": 0, "resume": True, "replay_completed": False,
            "collect_followers": True}


class DiscoveryStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: int = Field(ge=1)


class DiscoveryFilterSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coins: Literal["all", "required", "exclude_required", "not_required"] = "all"
    min_followers: int = Field(default=0, ge=0, le=100_000_000)
    min_account_days: int = Field(default=0, ge=0, le=20_000)
    include_unknown: bool = False


class DiscoveryFilterRequest(DiscoveryStopRequest):
    settings: DiscoveryFilterSettings


class DiscoveryImportRequest(DiscoveryStopRequest):
    selection: Literal["all", "filtered", "selected"]
    filter_id: str | None = Field(default=None, max_length=64)
    selected_ids: list[str] = Field(default_factory=list, max_length=400)


class CoinBalanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_after_coin: bool


def video_records(service, *, limit, offset, job_id=None):
    from src.activity_store import known_activity_ids
    from src.participation_store import load_participations

    total, rows = service.store.list_discovery_videos(limit=limit, offset=offset, job_id=job_id)
    if not rows:
        return total, []
    known = known_activity_ids()
    joined = {str(key) for key, value in load_participations().items() if value.user_status == "已参加"}
    queue_outcomes = {job: (service.store.get("discovery_queue", str(job)) or {}).get("outcomes", {})
                      for job in {row["job_id"] for row in rows} if job is not None}
    now = time.time()
    return total, [display_video_check(row, known=known, joined=joined,
                                       outcomes=queue_outcomes.get(row["job_id"], {}), now=now) for row in rows]


def discovery_status(service, job=None):
    bv_count = service.store.discovery_video_count()
    if job is None:
        job = next((service.job(row["job_id"]) for row in service.store.list_jobs(limit=1000)
                    if row["action"] == "control_discover"), None)
    if job is None:
        return {"job": None, "keywords": ["转发抽奖"], "summary": {}, "bv_count": bv_count, "resume_available": False}
    report = service.store.get("discovery", str(job["job_id"])) or {}
    raw = report.get("raw") or {}
    summary = {**{key: value for key, value in raw.items() if key in {
        "completion_reason", "videos_checked", "search_video_count", "verification_remaining",
        "sources_remaining", "has_more", "requests", "resumed", "phase", "target_new_videos",
        "new_videos_checked", "duplicates_skipped", "search_exhausted", "deferred_videos", "comment_reads_saved",
    }}, **(job.get("result") or {})}
    summary.setdefault("requests_used", report.get("requests_used", 0))
    summary.setdefault("verification_remaining", report.get("verification_remaining", 0))
    summary["eligible"] = len(verified_candidates(report))
    expires = raw.get("resume_expires_at", raw.get("checked_at", 0) + 1800)
    queue_job = service.store.get("discovery_queue_job", str(job["job_id"]))
    secondary = service.job(queue_job["id"]) if queue_job else None
    preview = service.store.get("discovery_filter", str(job["job_id"])) or {}
    can_select = job["state"] == "success" and summary.get("completion_reason") in {"search_complete", "target_reached", "search_exhausted"}
    pending = pending_candidates(service, job["job_id"], report) if can_select else {}
    selectable = []
    for identifier, row in pending.items():
        details = row.get("raw") or {}
        selectable.append({
            "dynamic_id": identifier,
            "prize": details.get("prize") or "",
            "publisher": details.get("publisher") or "",
            "publisher_uid": row.get("author_mid"),
            "followers": details.get("followers"),
            "triple_requirement": triple_requirement(row),
        })
    video_total, video_checks = video_records(service, limit=100, offset=0, job_id=job["job_id"])
    video_summary = Counter(row["status"] for row in video_checks)
    return {"job": job, "keywords": report.get("keywords") or job.get("keywords") or ["转发抽奖"],
            "keyword_match": job.get("keyword_match"),
            "mode": job.get("discovery_mode", "standard"),
            "target_new_videos": job.get("target_new_videos"),
            "summary": summary, "bv_count": bv_count, "stop_message": raw.get("stopped", ""),
            "pending_selection": len(pending),
            "candidates": selectable,
            "video_checks": {"total": video_total, "counts": dict(video_summary)},
            "filter": {key: val for key, val in preview.items() if key != "dynamic_ids"},
            "queue": queue_summary(service, job["job_id"], report),
            "queue_job": secondary,
            "resume_available": bool(raw.get("has_more") and expires > time.time()),
            "review_deferred_available": bool(can_select and raw.get("deferred_videos") and expires > time.time())}


def install_discovery(app):
    router = APIRouter(prefix="/api/discovery", tags=["stable"])

    @router.get("/current")
    def current():
        return discovery_status(get_control_service())

    @router.get("/videos")
    def videos(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
               job_id: int | None = Query(default=None, ge=1)):
        count, rows = video_records(get_control_service(), limit=limit, offset=offset, job_id=job_id)
        return {"total": count, "items": rows}

    @router.get("/coins")
    def current_coin_balance():
        return coin_balance(get_control_service().store)

    @router.post("/coins")
    def set_coin_balance_settings(request: CoinBalanceSettings):
        return coin_balance(get_control_service().store, refresh_after_coin=request.refresh_after_coin)

    @router.post("/start")
    def start(request: DiscoveryStartRequest, enqueue: bool = False):
        params = discovery_job_params(request)
        service = get_control_service()
        if enqueue:
            from src.llm_settings import is_llm_ready
            from web.task_queue import get_task_queue

            if not service.account()["credentials_present"]:
                raise AppError(ErrorCode.AUTH_REQUIRED, "请先扫码登录")
            if not is_llm_ready():
                raise AppError(ErrorCode.LLM_NOT_READY, "请先完成 LLM 配置与连接测试")
            entry = get_task_queue().submit("control_discover", params)
            job = service.job(entry["control_job_id"]) if entry["control_job_id"] else None
            return {**discovery_status(service, job), "queue_entry": entry}
        try:
            with service.lock:
                job = service.start("control_discover", params, require_llm=True, source="ui")
                return discovery_status(service, job)
        except ControlError as exc:
            raise AppError(exc.code, str(exc)) from exc

    def selectable_report(service, job_id):
        job = service.job(job_id)
        report = service.store.get("discovery", str(job_id))
        if job["action"] != "control_discover" or job["state"] != "success" or not report:
            raise AppError(ErrorCode.VALIDATION_ERROR, "请等待检索完成后选择活动")
        if (job.get("result") or {}).get("completion_reason") not in {"search_complete", "target_reached", "search_exhausted"}:
            raise AppError(ErrorCode.VALIDATION_ERROR, "请先继续检索并完成核验")
        return job, report

    @router.post("/review-deferred")
    def review_deferred(request: DiscoveryStopRequest):
        from src.llm_settings import is_llm_ready
        from web.task_queue import get_task_queue

        service = get_control_service()
        if not service.account()["credentials_present"]:
            raise AppError(ErrorCode.AUTH_REQUIRED, "请先扫码登录")
        if not is_llm_ready():
            raise AppError(ErrorCode.LLM_NOT_READY, "请先完成 LLM 配置与连接测试")
        try:
            job, report = selectable_report(service, request.job_id)
            context = report.get("review_context") or {}
            key = context.get("checkpoint_id")
            state = service.store.discovery_checkpoint(key, max_age=7 * 86400) if key else None
            if not state or state.get("capture_id") != context.get("capture_id") or not state.get("deferred_video_sources"):
                raise AppError(ErrorCode.VALIDATION_ERROR, "本轮没有可补查的低信号视频")
            params = {**context["params"], "_review_deferred": True, "_discovery_checkpoint_id": key,
                      "_capture_id": context["capture_id"], "resume": True, "replay_completed": False}
            entry = get_task_queue().submit("control_discover", params)
            current = service.job(entry["control_job_id"]) if entry["control_job_id"] else job
            return {**discovery_status(service, current), "queue_entry": entry}
        except ControlError as exc:
            raise AppError(exc.code, str(exc)) from exc

    @router.post("/filter")
    def filter_activities(request: DiscoveryFilterRequest):
        service = get_control_service()
        try:
            with service.lock:
                job, report = selectable_report(service, request.job_id)
                if not pending_candidates(service, request.job_id, report):
                    return discovery_status(service, job)
                service.ensure_idle()
                service.store.put("discovery_filter", str(request.job_id), {})
                filtering = service.start("control_discovery_filter", {"discovery_job_id": request.job_id,
                    "settings": request.settings.model_dump()}, source="ui")
                service.store.put("discovery_queue_job", str(request.job_id), {"id": filtering["job_id"]})
                return discovery_status(service, job)
        except ControlError as exc:
            raise AppError(exc.code, str(exc)) from exc

    @router.post("/import")
    def import_remaining(request: DiscoveryImportRequest):
        service = get_control_service()
        try:
            with service.lock:
                job, report = selectable_report(service, request.job_id)
                pending = pending_candidates(service, request.job_id, report)
                selected_ids = list(pending)
                if request.selection == "filtered":
                    preview = service.store.get("discovery_filter", str(request.job_id)) or {}
                    if not request.filter_id or preview.get("id") != request.filter_id:
                        raise AppError(ErrorCode.VALIDATION_ERROR, "请先预览当前筛选条件")
                    selected_ids = [identifier for identifier in preview["dynamic_ids"] if identifier in pending]
                elif request.selection == "selected":
                    if not request.selected_ids or not set(request.selected_ids).issubset(verified_candidates(report)):
                        raise AppError(ErrorCode.VALIDATION_ERROR, "请选择当前报告中的候选活动")
                    selected_ids = [identifier for identifier in dict.fromkeys(request.selected_ids) if identifier in pending]
                if not selected_ids:
                    return discovery_status(service, job)
                queued = service.start("control_import", {"discovery_job_id": request.job_id, "queue_verified": True,
                                       "selected_ids": selected_ids},
                                       require_llm=True, source="ui")
                service.store.put("discovery_queue_job", str(request.job_id), {"id": queued["job_id"]})
                return discovery_status(service, job)
        except ControlError as exc:
            raise AppError(exc.code, str(exc)) from exc

    @router.post("/stop")
    def stop(request: DiscoveryStopRequest):
        service = get_control_service()
        try:
            with service.lock:
                job = service.job(request.job_id)
                if job["action"] != "control_discover" and not job.get("discovery_queue"):
                    raise ControlError("JOB_NOT_CANCELLABLE", "该任务不是检索任务")
                if job["state"] == "running":
                    job = service.dispatch("job_cancel", {"job_id": request.job_id})
                return discovery_status(service, service.job(job["discovery_queue"]) if job.get("discovery_queue") else job)
        except ControlError as exc:
            raise AppError(exc.code, str(exc)) from exc

    app.include_router(router)
