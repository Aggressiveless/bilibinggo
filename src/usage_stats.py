"""Local daily accounting for external requests and reported model usage."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from hashlib import sha256
from threading import Lock
from urllib.parse import urlsplit

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from src.app_logging import get_logger
from src.db.engine import get_engine
from src.db.models import BilibiliUsageDay, LlmUsageDay, LlmUsageRate
from src.llm_config import LlmConfig, load_llm_config

_logger = get_logger("usage")
_schema_lock = Lock()
_currencies = ("CNY", "USD")
_million = Decimal(1_000_000)
_nanos = Decimal(1_000_000_000)


@lru_cache(maxsize=4)
def _ensure_tables(engine) -> None:
    for model in (BilibiliUsageDay, LlmUsageDay, LlmUsageRate):
        model.__table__.create(engine, checkfirst=True)


def _engine():
    with _schema_lock:
        engine = get_engine()
        _ensure_tables(engine)
    return engine


def _increment(table, key: dict, values: dict) -> None:
    try:
        statement = insert(table).values(**key, **values)
        statement = statement.on_conflict_do_update(
            index_elements=list(key),
            set_={name: table.c[name] + statement.excluded[name] for name in values},
        )
        with _engine().begin() as connection:
            connection.execute(statement)
    except (OSError, SQLAlchemyError) as exc:
        # Usage collection must not prevent the original request from completing.
        _logger.warning("无法保存请求统计：%s", type(exc).__name__)


def record_bilibili_request(request) -> None:
    host = request.url.host.rstrip(".").lower()
    if host in {"bilibili.com", "b23.tv"} or host.endswith(".bilibili.com"):
        _increment(BilibiliUsageDay.__table__, {"day": date.today().isoformat()}, {"requests": 1})


def _model_key(base_url: str, model: str) -> str:
    return sha256(f"{base_url.rstrip('/')}\n{model}".encode()).hexdigest()


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _reported_cost(usage: dict, host: str) -> tuple[str, int] | None:
    raw = usage.get("cost")
    currency = str(usage.get("currency") or usage.get("cost_currency") or "").upper()
    if not currency and host == "openrouter.ai":
        currency = "USD"
    if currency not in _currencies or raw is None:
        return None
    try:
        amount = Decimal(str(raw))
        if amount.is_finite() and 0 <= amount <= 1_000_000:
            return currency, int(amount * _nanos)
    except (InvalidOperation, ValueError):
        pass
    return None


def record_llm_request(
    config: LlmConfig, day: date, response: object = None, *, completion: bool = True,
) -> None:
    data = response if isinstance(response, dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    input_tokens = _count(usage.get("prompt_tokens"))
    output_tokens = _count(usage.get("completion_tokens"))
    total = _count(usage.get("total_tokens"))
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    cached = _count(usage.get("prompt_cache_hit_tokens"))
    details = usage.get("prompt_tokens_details")
    if cached is None and isinstance(details, dict):
        cached = _count(details.get("cached_tokens"))
    reported = _reported_cost(usage, urlsplit(config.base_url).hostname or "")
    estimable = (
        completion and reported is None and input_tokens is not None and output_tokens is not None
    )
    values = {
        "requests": 1,
        "completions": int(completion),
        "token_calls": int(total is not None),
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "total_tokens": total if total is not None else (input_tokens or 0) + (output_tokens or 0),
        "reported_calls": int(reported is not None),
        "reported_cny_nanos": reported[1] if reported and reported[0] == "CNY" else 0,
        "reported_usd_nanos": reported[1] if reported and reported[0] == "USD" else 0,
        "estimable_calls": int(estimable),
        "estimable_input": input_tokens if estimable else 0,
        "estimable_cached_input": min(cached or 0, input_tokens or 0) if estimable else 0,
        "estimable_output": output_tokens if estimable else 0,
    }
    _increment(
        LlmUsageDay.__table__,
        {"day": day.isoformat(), "model_key": _model_key(config.base_url, config.model_name)},
        values,
    )


def _blank_totals() -> dict:
    return {
        "bilibili_requests": 0,
        "llm_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "missing_token_calls": 0,
        "unpriced_calls": 0,
        "priced_calls": 0,
        "costs": {currency: {"reported": Decimal(0), "estimated": Decimal(0)} for currency in _currencies},
    }


def _add_llm(totals: dict, row: LlmUsageDay, rate: LlmUsageRate | None) -> None:
    totals["llm_requests"] += row.requests
    totals["input_tokens"] += row.input_tokens
    totals["output_tokens"] += row.output_tokens
    totals["total_tokens"] += row.total_tokens
    totals["missing_token_calls"] += row.completions - row.token_calls
    totals["costs"]["CNY"]["reported"] += Decimal(row.reported_cny_nanos) / _nanos
    totals["costs"]["USD"]["reported"] += Decimal(row.reported_usd_nanos) / _nanos
    priced = row.reported_calls
    if rate and row.estimable_calls:
        input_rate = Decimal(rate.input_rate)
        cached_rate = Decimal(rate.cached_input_rate) if rate.cached_input_rate is not None else input_rate
        estimate = (
            (row.estimable_input - row.estimable_cached_input) * input_rate
            + row.estimable_cached_input * cached_rate
            + row.estimable_output * Decimal(rate.output_rate)
        ) / _million
        totals["costs"][rate.currency]["estimated"] += estimate
        priced += row.estimable_calls
    totals["priced_calls"] += priced
    totals["unpriced_calls"] += row.completions - priced


def _pricing_info(config: LlmConfig | None, rates: dict[str, LlmUsageRate]) -> dict | None:
    if config is None:
        return None
    key = _model_key(config.base_url, config.model_name)
    rate = rates.get(key)
    return {
        "model_key": key,
        "model": config.model_name,
        "host": urlsplit(config.base_url).hostname,
        "currency": rate.currency if rate else "CNY",
        "input_rate": rate.input_rate if rate else None,
        "cached_input_rate": rate.cached_input_rate if rate else None,
        "output_rate": rate.output_rate if rate else None,
    }


def get_usage_stats(days: int) -> dict:
    today = date.today()
    first = today - timedelta(days=days - 1)
    daily = {(first + timedelta(days=offset)).isoformat(): _blank_totals() for offset in range(days)}
    totals = _blank_totals()
    with Session(_engine()) as session:
        rates = {row.model_key: row for row in session.exec(select(LlmUsageRate))}
        bili_days = session.exec(select(BilibiliUsageDay)).all()
        llm_days = session.exec(select(LlmUsageDay)).all()
    for row in bili_days:
        totals["bilibili_requests"] += row.requests
        if row.day in daily:
            daily[row.day]["bilibili_requests"] += row.requests
    for row in llm_days:
        rate = rates.get(row.model_key)
        _add_llm(totals, row, rate)
        if row.day in daily:
            _add_llm(daily[row.day], row, rate)
    return {
        "recorded_from": min((row.day for row in (*bili_days, *llm_days)), default=None),
        "totals": totals,
        "daily": [{"day": day, **values} for day, values in daily.items()],
        "pricing": _pricing_info(load_llm_config(), rates),
    }


def save_usage_rate(
    model_key: str, currency: str, input_rate: Decimal, output_rate: Decimal,
    cached_input_rate: Decimal | None,
) -> None:
    config = load_llm_config()
    if config is None or model_key != _model_key(config.base_url, config.model_name):
        raise ValueError("当前模型已变更，请刷新后重新设置单价")
    values = {
        "currency": currency,
        "input_rate": str(input_rate),
        "output_rate": str(output_rate),
        "cached_input_rate": None if cached_input_rate is None else str(cached_input_rate),
    }
    table = LlmUsageRate.__table__
    statement = insert(table).values(model_key=model_key, **values)
    with _engine().begin() as connection:
        connection.execute(statement.on_conflict_do_update(index_elements=["model_key"], set_=values))
