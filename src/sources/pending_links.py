from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert
from sqlmodel import col, select

from src.db.models import SourcePendingLinkRow
from src.db.session import session_scope
from src.sources.common import CheckResult, normalize_activity_id, opus_link


def stage_source_links(
    source_ids: list[str], checks: list[CheckResult]
) -> dict[str, list[str]]:
    """Persist newly discovered work and return the pending links for these sources."""
    pending: dict[str, list[str]] = {source_id: [] for source_id in source_ids}
    with session_scope() as session:
        existing = session.exec(
            select(SourcePendingLinkRow)
            .where(col(SourcePendingLinkRow.source_id).in_(source_ids))
            .order_by(SourcePendingLinkRow.source_id, SourcePendingLinkRow.dynamic_id)
        ).all()
        for row in existing:
            pending[row.source_id].append(row.dynamic_id)

        new_rows = []
        for check in checks:
            if not check.updated:
                continue
            ids = pending[check.source_id]
            seen = set(ids)
            for url in check.activity_links:
                dynamic_id = normalize_activity_id(url)
                if dynamic_id and dynamic_id not in seen:
                    seen.add(dynamic_id)
                    ids.append(dynamic_id)
                    new_rows.append({"source_id": check.source_id, "dynamic_id": dynamic_id})
        if new_rows:
            session.execute(insert(SourcePendingLinkRow).on_conflict_do_nothing(), new_rows)
    return pending


def pending_activity_urls(pending: dict[str, list[str]]) -> list[str]:
    return list(dict.fromkeys(opus_link(did) for ids in pending.values() for did in ids))


def clear_processed_source_links(pending: dict[str, list[str]], deferred_ids: set[str]) -> None:
    """Acknowledge only links in this run's snapshot whose processing finished."""
    processed = {source_id: set(ids) - deferred_ids for source_id, ids in pending.items()}
    with session_scope() as session:
        rows = session.exec(
            select(SourcePendingLinkRow).where(col(SourcePendingLinkRow.source_id).in_(list(processed)))
        )
        for row in rows:
            if row.dynamic_id in processed[row.source_id]:
                session.delete(row)
