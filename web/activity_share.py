from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from src.activity_store import activity_file_lock, append_activities, known_activity_ids, load_activities
from src.lottery_classifier import PARTICIPATABLE_TYPES
from src.lottery_time import lottery_time_unix
from src.pipeline.status_step import apply_initial_status
from src.sources.common import is_valid_dynamic_id, opus_link
from web.activity_service import _filtered_activity_rows
from web.api_errors import AppError, ErrorCode

router = APIRouter(prefix="/api/activities", tags=["stable"])
MAX_ACTIVITIES = 2000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_LOTTERY_TIME = 253402271999


class SharedActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    dynamic_id: str = Field(pattern=r"^[0-9]{18,19}$")
    lottery_type: Literal["互动抽奖", "预约抽奖", "转发抽奖"]
    lottery_time: int = Field(gt=0, le=MAX_LOTTERY_TIME)
    prize: str = Field(max_length=300)
    source_url: str = Field(max_length=64)

    @model_validator(mode="after")
    def validate_source_url(self):
        if self.source_url != opus_link(self.dynamic_id):
            raise ValueError("活动链接必须与 B 站动态 ID 一致")
        return self


class ActivityShareFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format: Literal["binggo.activities.v1"]
    activities: list[SharedActivity] = Field(min_length=1, max_length=MAX_ACTIVITIES)


@router.get("/export", response_model=ActivityShareFile)
def export_activities(
    status: str | None = None,
    type: str | None = None,
    draw: str | None = None,
    draw_window: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> dict:
    now = int(time.time())
    eligible = {}
    for item in load_activities():
        if (item.get("lottery_type") not in PARTICIPATABLE_TYPES or item.get("draw_status") != "active"
                or item.get("skipped") or item.get("lottery_time_inferred")
                or (item.get("conditions") or {}).get("lottery_time_inferred")):
            continue
        timestamp = lottery_time_unix(item)
        dynamic_id = str(item.get("dynamic_id") or "")
        if (timestamp and now < timestamp <= MAX_LOTTERY_TIME
                and dynamic_id.isascii() and is_valid_dynamic_id(dynamic_id)):
            eligible[dynamic_id] = (item, timestamp)

    rows = _filtered_activity_rows(
        status=status, lottery_type=type, draw=draw, draw_window=draw_window, q=q, sort=sort, order=order,
        items=[item for item, _ in eligible.values()],
    )
    activities = [
        SharedActivity(
            dynamic_id=row["dynamic_id"], lottery_type=row["lottery_type"],
            lottery_time=eligible[row["dynamic_id"]][1], prize=row["prize"][:300],
            source_url=opus_link(row["dynamic_id"]),
        )
        for row in rows if row["activity_status"] != "已结束"
    ]
    if not activities:
        raise AppError(ErrorCode.VALIDATION_ERROR, "当前筛选没有仍开放且开奖时间明确的活动")
    if len(activities) > MAX_ACTIVITIES:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"每份文件最多 {MAX_ACTIVITIES} 条活动，请缩小筛选范围")
    return {"format": "binggo.activities.v1", "activities": activities}


async def _read_share_file(request: Request) -> ActivityShareFile:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_FILE_BYTES:
            raise AppError(ErrorCode.VALIDATION_ERROR, "活动分享文件不能超过 4 MB", status_code=413)
        data.extend(chunk)
    try:
        return ActivityShareFile.model_validate_json(data)
    except ValidationError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, "文件格式无效，请选择由 Binggo 导出的活动 JSON 文件") from exc


def _import_share_file(bundle: ActivityShareFile, *, preview: bool) -> dict:
    now = int(time.time())
    with activity_file_lock():
        seen = known_activity_ids()
        duplicates = expired = 0
        new = []
        for activity in bundle.activities:
            if activity.lottery_time <= now:
                expired += 1
            elif activity.dynamic_id in seen:
                duplicates += 1
            else:
                seen.add(activity.dynamic_id)
                new.append(activity)

        inserted = 0
        if not preview:
            rows = []
            for activity in new:
                row = apply_initial_status({
                    "dynamic_id": activity.dynamic_id,
                    "source_url": opus_link(activity.dynamic_id),
                    "lottery_type": activity.lottery_type,
                    "lottery_time": activity.lottery_time,
                    "draw_status": "active",
                    "prizes": [{"description": activity.prize}] if activity.prize else [],
                }, now=now)
                # Read participation from the recipient's history.
                row["status_classified"] = False
                rows.append(row)
            inserted = append_activities(rows)

    return {
        "total": len(bundle.activities), "new": len(new), "duplicates": duplicates, "expired": expired,
        "imported": inserted, "examples": [activity.prize or activity.dynamic_id for activity in new[:4]],
    }


@router.post("/import")
async def import_activities(request: Request, preview: bool = True) -> dict:
    bundle = await _read_share_file(request)
    return await run_in_threadpool(_import_share_file, bundle, preview=preview)
