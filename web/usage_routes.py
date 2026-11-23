from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from src.usage_stats import get_usage_stats, save_usage_rate
from web.api_errors import AppError, ErrorCode
from web.schemas import OkResponse

router = APIRouter(prefix="/api/usage", tags=["stable"])
UsageRate = Annotated[Decimal, Field(ge=0, le=1_000_000, decimal_places=6)]


class UsagePricingRequest(BaseModel):
    model_key: str = Field(min_length=64, max_length=64)
    currency: Literal["CNY", "USD"]
    input_rate: UsageRate
    cached_input_rate: UsageRate | None = None
    output_rate: UsageRate


@router.get("")
def api_usage(days: int = Query(30, ge=7, le=90)) -> dict:
    return get_usage_stats(days)


@router.put("/pricing")
def api_save_usage_pricing(request: UsagePricingRequest) -> OkResponse:
    try:
        save_usage_rate(**request.model_dump())
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    return OkResponse(ok=True)
