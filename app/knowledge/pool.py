import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import FaithCase, LowConfidenceQuestion

logger = logging.getLogger(__name__)


class LowConfidencePool:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def record(self, conversation_id, raw_question, source, reason=None) -> None:
        with self.session_factory() as session:
            session.add(LowConfidenceQuestion(
                conversation_id=conversation_id, raw_question=raw_question[:2000],
                source=source, reason=(reason or "")[:1000] or None,
            ))
            session.commit()


class FaithCaseLedger:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def record(self, eval_id, bucket, query, strategy, answer, reason, citations, judge_model) -> str:
        with self.session_factory() as session:
            row = session.scalar(select(FaithCase).where(FaithCase.eval_id == eval_id))
            if row is None:
                session.add(FaithCase(
                    eval_id=eval_id, bucket=bucket, query=query[:512], strategy=strategy,
                    answer=answer, reason=reason, citations=citations, judge_model=judge_model,
                    status="未解决", seen_count=1,
                ))
                session.commit()
                return "inserted"
            row.bucket, row.query, row.strategy = bucket, query[:512], strategy
            row.answer, row.reason, row.citations = answer, reason, citations
            row.judge_model = judge_model
            row.seen_count += 1
            row.status = "未解决"
            row.resolution = None
            session.commit()
            return "updated"

    def resolve(self, eval_id, resolution) -> bool:
        with self.session_factory() as session:
            row = session.scalar(select(FaithCase).where(FaithCase.eval_id == eval_id))
            if row is None:
                return False
            row.status = "已解决"
            row.resolution = resolution[:300]
            row.resolved_at = datetime.now()
            session.commit()
            return True
