from __future__ import annotations

import contextvars
from contextlib import nullcontext

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal

import httpx

from src.activity_store import append_activities, known_activity_ids
from src.app_logging import get_logger
from src.bilibili_client import BilibiliAPIError, BilibiliClient, BilibiliRequestError
from src.forward_parser import DynamicContentFetchError
from src.llm_client import LlmRequestError
from src.log_redact import redact_text
from src.lottery_api import ActivityDetailUnavailable
from src.lottery_enricher import (
    ENRICH_SKIP_REASON,
    EnrichedActivity,
    EnrichSkippedError,
    enrich_activity,
    is_enrich_detail_skip_error,
)
from src.lottery_classifier import LotteryType
from src.participation_store import load_participations
from src.pipeline.classify_step import ClassifyOutcome, classify_new_link
from src.pipeline.status_step import apply_initial_status
from src.sources.common import CheckResult, normalize_activity_id

ProgressCallback = Callable[[int, int, str], None]
logger = get_logger("job")
_ITEM_ERRORS = (
    httpx.HTTPError,
    BilibiliAPIError,
    BilibiliRequestError,
    DynamicContentFetchError,
    LlmRequestError,
    ActivityDetailUnavailable,
)


@dataclass(frozen=True)
class DeferredLink:
    dynamic_id: str
    stage: Literal["分类", "详情"]
    reason: str


@dataclass
class PipelineResult:
    ok: bool
    pipeline_skipped: bool
    raw_link_count: int
    new_link_count: int
    classified_count: int
    skipped_count: int
    enriched_count: int
    persisted_count: int
    skip_reasons: dict[str, int] = field(default_factory=dict)
    message: str = ""
    deferred: list[DeferredLink] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _dedupe_new_dynamic_ids(raw_urls: list[str]) -> list[str]:
    known = known_activity_ids()
    seen: set[str] = set()
    ordered: list[str] = []
    for raw_url in raw_urls:
        dynamic_id = normalize_activity_id(str(raw_url))
        if not dynamic_id or dynamic_id in known or dynamic_id in seen:
            continue
        seen.add(dynamic_id)
        ordered.append(dynamic_id)
    return ordered


def _collect_updated_links(ds_results: list[CheckResult]) -> list[str]:
    links: list[str] = []
    for result in ds_results:
        if not result.updated:
            continue
        links.extend(result.activity_links or [])
    return links


def run_new_links_pipeline(
    raw_urls: list[str],
    *,
    workers: int = 4,
    on_progress: ProgressCallback | None = None,
    client: BilibiliClient | None = None,
    accept: Callable[[EnrichedActivity], bool] | None = None,
    defer_item_errors: bool = False,
) -> PipelineResult:
    """Step 2～5：仅处理新链接（内存流转，末步落库）。"""
    dynamic_ids = _dedupe_new_dynamic_ids(raw_urls)
    if not dynamic_ids:
        return PipelineResult(
            ok=True,
            pipeline_skipped=True,
            raw_link_count=len(raw_urls),
            new_link_count=0,
            classified_count=0,
            skipped_count=0,
            enriched_count=0,
            persisted_count=0,
            message="无新链接，流水线结束",
        )

    participations = load_participations()
    skip_reasons: dict[str, int] = {}
    deferred: list[DeferredLink] = []
    tasks: list[tuple[str, LotteryType]] = []
    classify_outcome_by_id: dict[str, ClassifyOutcome] = {}
    total = len(dynamic_ids)

    if on_progress:
        on_progress(0, total, f"正在分类 {total} 条新链接…")

    def defer(dynamic_id: str, stage: Literal["分类", "详情"], exc: Exception) -> None:
        reason = redact_text(str(exc)).splitlines()
        message = reason[0][:250] if reason else type(exc).__name__
        deferred.append(DeferredLink(dynamic_id, stage, message))
        logger.warning("活动 %s %s失败，留待下次更新：%s", dynamic_id, stage, message)

    with (nullcontext(client) if client is not None else BilibiliClient()) as shared_client:
        for done, dynamic_id in enumerate(dynamic_ids, start=1):
            try:
                outcome = classify_new_link(shared_client, dynamic_id)
            except _ITEM_ERRORS as exc:
                if not defer_item_errors:
                    raise
                defer(dynamic_id, "分类", exc)
            else:
                if outcome.skipped:
                    reason = outcome.skip_reason or "skipped"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    if not outcome.lottery_type:
                        logger.info("跳过活动 %s：%s", dynamic_id, reason)
                elif outcome.lottery_type in ("互动抽奖", "预约抽奖", "转发抽奖"):
                    tasks.append((outcome.dynamic_id, outcome.lottery_type))
                    classify_outcome_by_id[outcome.dynamic_id] = outcome
            if on_progress:
                on_progress(done, total, "分类进度")

    worker_count = max(1, min(workers, len(tasks) if tasks else 1))

    if not tasks:
        return PipelineResult(
            ok=True,
            pipeline_skipped=False,
            raw_link_count=len(raw_urls),
            new_link_count=len(dynamic_ids),
            classified_count=0,
            skipped_count=sum(skip_reasons.values()),
            enriched_count=0,
            persisted_count=0,
            skip_reasons=skip_reasons,
            message=f"本轮无新增活动，{len(deferred)} 条待重试" if deferred else "新链接均已跳过（非抽奖/充电/失效）",
            deferred=deferred,
        )

    if on_progress:
        on_progress(0, len(tasks), f"正在拉取 {len(tasks)} 条活动详情…")

    enriched_rows: list[dict] = []
    enrich_total = len(tasks)

    def _enrich_one(
        client: BilibiliClient,
        dynamic_id: str,
        lottery_type: LotteryType,
        outcome: ClassifyOutcome,
    ) -> EnrichedActivity:
        return enrich_activity(
            client,
            dynamic_id=dynamic_id,
            lottery_type=lottery_type,
            participation=participations.get(dynamic_id),
            classify_content=outcome.classify_content or None,
            classify_detail=outcome.detail_item,
            classify_notice=outcome.lottery_notice,
            classify_notice_business_type=outcome.notice_business_type,
            classify_notice_business_id=outcome.notice_business_id,
        )

    with (nullcontext(client) if client is not None else BilibiliClient()) as enrich_client:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    contextvars.copy_context().run,
                    _enrich_one,
                    enrich_client,
                    did,
                    lt,
                    classify_outcome_by_id[did],
                ): did
                for did, lt in tasks
            }
            for done, future in enumerate(as_completed(futures), start=1):
                try:
                    activity = future.result()
                except EnrichSkippedError:
                    skip_reasons[ENRICH_SKIP_REASON] = skip_reasons.get(ENRICH_SKIP_REASON, 0) + 1
                except (RuntimeError, httpx.HTTPError) as exc:
                    if is_enrich_detail_skip_error(exc):
                        skip_reasons[ENRICH_SKIP_REASON] = skip_reasons.get(ENRICH_SKIP_REASON, 0) + 1
                    elif defer_item_errors and isinstance(exc, _ITEM_ERRORS):
                        defer(futures[future], "详情", exc)
                    else:
                        raise
                else:
                    if activity.skipped:
                        raise RuntimeError(f"活动 {activity.dynamic_id} 不应为 skipped")
                    if accept is None or accept(activity):
                        enriched_rows.append(apply_initial_status(activity.to_dict()))
                    else:
                        skip_reasons["不再可参与"] = skip_reasons.get("不再可参与", 0) + 1
                if on_progress:
                    on_progress(done, enrich_total, "详情进度")

    if on_progress and enriched_rows:
        on_progress(0, len(enriched_rows), "正在写入活动库…")

    persisted = append_activities(enriched_rows)
    if on_progress and enriched_rows:
        on_progress(
            persisted,
            max(1, len(enriched_rows)),
            f"入库完成，新增 {persisted} 条活动",
        )

    return PipelineResult(
        ok=True,
        pipeline_skipped=False,
        raw_link_count=len(raw_urls),
        new_link_count=len(dynamic_ids),
        classified_count=len(tasks),
        skipped_count=sum(skip_reasons.values()),
        enriched_count=len(enriched_rows),
        persisted_count=persisted,
        skip_reasons=skip_reasons,
        message=f"新入库 {persisted} 条活动" + (f"，{len(deferred)} 条待重试" if deferred else ""),
        deferred=deferred,
    )


def run_refresh_all_pipeline(
    ds_results: list[CheckResult],
    *,
    workers: int = 4,
    on_progress: ProgressCallback | None = None,
    pending_links: list[str] | None = None,
) -> PipelineResult:
    """完整 refresh_all：Step 1 结果 → Step 2～5。"""
    if not pending_links and not any(result.updated for result in ds_results):
        return PipelineResult(
            ok=True,
            pipeline_skipped=True,
            raw_link_count=0,
            new_link_count=0,
            classified_count=0,
            skipped_count=0,
            enriched_count=0,
            persisted_count=0,
            message="所有数据源容器均未变更，流水线结束",
        )

    raw_links = _collect_updated_links(ds_results) if pending_links is None else pending_links
    return run_new_links_pipeline(raw_links, workers=workers, on_progress=on_progress, defer_item_errors=True)
