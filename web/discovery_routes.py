"""Browser controls for the same discovery jobs used by MCP."""
from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.control_store import ControlError
from web.api_errors import AppError
from web.control_service import get_control_service


class DiscoveryStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(default_factory=lambda: ["转发抽奖"], min_length=1, max_length=3)

    @field_validator("keywords")
    @classmethod
    def clean_keywords(cls, values):
        words = list(dict.fromkeys(word.strip() for word in values))
        if any(not word or len(word) > 80 for word in words):
            raise ValueError("请输入 1–3 个关键词，每个不超过 80 字")
        return words


class DiscoveryStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: int = Field(ge=1)


def discovery_status(service, job=None):
    if job is None:
        job = next((service.job(row["job_id"]) for row in service.store.list_jobs(limit=1000)
                    if row["action"] == "control_discover"), None)
    if job is None:
        return {"job": None, "keywords": ["转发抽奖"], "summary": {}, "candidates": [], "resume_available": False}
    report = service.store.get("discovery", str(job["job_id"])) or {}
    raw = report.get("raw") or {}
    summary = {**{key: value for key, value in raw.items() if key in {
        "completion_reason", "videos_checked", "search_video_count", "verification_remaining",
        "sources_remaining", "has_more", "requests", "resumed", "phase",
    }}, **(job.get("result") or {})}
    summary.setdefault("requests_used", report.get("requests_used", 0))
    summary.setdefault("verification_remaining", report.get("verification_remaining", 0))
    candidates = [row for row in report.get("candidates", []) if row.get("eligible")]
    summary["eligible"] = len(candidates)
    expires = raw.get("resume_expires_at", raw.get("checked_at", 0) + 1800)
    return {"job": job, "keywords": report.get("keywords") or job.get("keywords") or ["转发抽奖"],
            "summary": summary, "candidates": candidates, "stop_message": raw.get("stopped", ""),
            "resume_available": bool(raw.get("has_more") and expires > time.time())}


def install_discovery(app):
    router = APIRouter(prefix="/api/discovery", tags=["stable"])

    @router.get("/current")
    def current():
        return discovery_status(get_control_service())

    @router.post("/start")
    def start(request: DiscoveryStartRequest):
        service = get_control_service()
        try:
            with service.lock:
                job = service.start("control_discover", {"keywords": request.keywords, "mode": "standard",
                    "video_limit": 36, "request_budget": 180, "resume": True}, require_llm=True, source="ui")
                return discovery_status(service, job)
        except ControlError as exc:
            raise AppError(exc.code, str(exc)) from exc

    @router.post("/stop")
    def stop(request: DiscoveryStopRequest):
        service = get_control_service()
        try:
            with service.lock:
                job = service.job(request.job_id)
                if job["action"] != "control_discover":
                    raise ControlError("JOB_NOT_CANCELLABLE", "该任务不是检索任务")
                if job["state"] == "running":
                    job = service.dispatch("job_cancel", {"job_id": request.job_id})
                return discovery_status(service, job)
        except ControlError as exc:
            raise AppError(exc.code, str(exc)) from exc

    app.include_router(router)
