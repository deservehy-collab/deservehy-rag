import logging
import threading
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import LowConfidenceQuestion, ReviewQueue

logger = logging.getLogger(__name__)

SOURCE_LIMIT = 50

# 同一行的通过流程串行化：双击 / 重试并发进来时，第二个直接回 in_progress，
# 不会两边都跑 ingest。进程内有效；多进程部署仍靠「已通过」状态兜底。
_APPROVING_LOCK = threading.Lock()
_APPROVING: set[int] = set()


@dataclass(frozen=True)
class ApproveResult:
    ok: bool
    review_id: int
    status: str = ""
    ingested: int = 0
    vectorized: int = 0
    message: str = ""


def approve(session_factory: sessionmaker, ingestor, review_id: int,
            answer: str | None = None) -> ApproveResult:
    with _APPROVING_LOCK:
        if review_id in _APPROVING:
            return ApproveResult(False, review_id, message="in_progress")
        _APPROVING.add(review_id)
    try:
        with session_factory() as session:
            row = session.get(ReviewQueue, review_id)
            if row is None:
                return ApproveResult(False, review_id, message="not_found")
            # 已通过的行再 approve 会造成同一条 FAQ 被灌进 Milvus 两遍，检索时自己跟自己竞争。
            if row.review_status == "通过":
                return ApproveResult(False, review_id, row.review_status, message="already_approved")
            final = (answer or row.approved_answer or row.ai_suggested_answer or "").strip()
            if not final:
                return ApproveResult(False, review_id, row.review_status, message="empty_answer")
            question = row.normalized_question
        ingested = ingestor.ingest_pairs([{"question": question, "answer": final}],
                                         source_ref=f"review_queue:{review_id}")
        vectorized = ingestor.vectorize_pending()
        with session_factory() as session:
            row = session.get(ReviewQueue, review_id)
            row.review_status = "通过"
            row.approved_answer = final
            session.commit()
        logger.info("审核通过 review_id=%s 入库 %s 条、补向量 %s 条。",
                    review_id, ingested, vectorized)
        return ApproveResult(True, review_id, "通过", ingested=ingested, vectorized=vectorized)
    finally:
        with _APPROVING_LOCK:
            _APPROVING.discard(review_id)


def reject(session_factory: sessionmaker, review_id: int, reason: str | None = None) -> bool:
    with session_factory() as session:
        row = session.get(ReviewQueue, review_id)
        if row is None or row.review_status == "通过":
            return False
        row.review_status = "驳回"
        session.commit()
    # review_queue 表（用户给的 DDL）没有存驳回理由的列，理由只进日志，不偷偷加列。
    logger.info("审核驳回 review_id=%s 理由=%s", review_id, reason or "未填写")
    return True


def list_queue(session_factory: sessionmaker, *, status: str | None = None,
               limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    with session_factory() as session:
        count = select(func.count()).select_from(ReviewQueue)
        query = select(ReviewQueue)
        if status:
            count = count.where(ReviewQueue.review_status == status)
            query = query.where(ReviewQueue.review_status == status)
        total = session.scalar(count) or 0
        rows = session.scalars(
            query.order_by(ReviewQueue.occurrence_count.desc(), ReviewQueue.id.desc())
                 .limit(limit).offset(offset)
        ).all()
        return [_brief(row) for row in rows], total


def queue_detail(session_factory: sessionmaker, review_id: int) -> dict | None:
    with session_factory() as session:
        row = session.get(ReviewQueue, review_id)
        if row is None:
            return None
        detail = _brief(row)
        sources = session.scalars(
            select(LowConfidenceQuestion)
            .where(LowConfidenceQuestion.matched_review_id == review_id)
            .order_by(LowConfidenceQuestion.id)
            .limit(SOURCE_LIMIT)
        ).all()
        detail["sources"] = [
            {"id": item.id, "raw_question": item.raw_question, "source": item.source,
             "reason": item.reason, "conversation_id": item.conversation_id,
             "created_at": _stamp(item.created_at), "retrieved_chunks": item.retrieved_chunks or []}
            for item in sources
        ]
        return detail


def _brief(row: ReviewQueue) -> dict:
    return {
        "id": row.id,
        "normalized_question": row.normalized_question,
        "ai_suggested_answer": row.ai_suggested_answer,
        "occurrence_count": row.occurrence_count,
        "review_status": row.review_status,
        "approved_answer": row.approved_answer,
        "created_at": _stamp(row.created_at),
        "updated_at": _stamp(row.updated_at),
    }


def _stamp(value) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if value is not None else None
