import pytest
from sqlalchemy import create_engine, select

from app.database import build_session_factory
from app.knowledge.pool import FaithCaseLedger, LowConfidencePool
from app.models import Base, Conversation, FaithCase, LowConfidenceQuestion


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    Base.metadata.create_all(engine)
    yield build_session_factory(engine)
    engine.dispose()


def test_pool_records_source_and_reason(factory):
    with factory() as session:
        conversation = Conversation(user_id="anon")
        session.add(conversation)
        session.commit()
        conversation_id = conversation.id
    LowConfidencePool(factory).record(conversation_id, "支持货到付款吗", "retrieval_low_conf", "top1=0.1")
    with factory() as session:
        row = session.scalar(select(LowConfidenceQuestion))
        assert row.source == "retrieval_low_conf" and row.raw_question == "支持货到付款吗"


def test_faith_ledger_inserts_then_counts_recurrence(factory):
    ledger = FaithCaseLedger(factory)
    assert ledger.record("F01", "A_policy", "退款多久到账", "hybrid_rerank", "3 天", "证据没有 3 天",
                         [{"n": 1, "chunk_id": 1}], "test-model") == "inserted"
    assert ledger.record("F01", "A_policy", "退款多久到账", "hybrid_rerank", "5 天", "证据没有 5 天",
                         [{"n": 1, "chunk_id": 2}], "test-model") == "updated"
    with factory() as session:
        row = session.scalar(select(FaithCase).where(FaithCase.eval_id == "F01"))
        assert row.seen_count == 2 and row.answer == "5 天" and row.status == "未解决"


def test_faith_ledger_reopens_resolved_case_on_recurrence(factory):
    ledger = FaithCaseLedger(factory)
    ledger.record("F02", "B_model", "q", "hybrid_rerank", "a", "r", [], "m")
    assert ledger.resolve("F02", "补充型号文档") is True
    ledger.record("F02", "B_model", "q2", "hybrid_rerank", "a2", "r2", [], "m")
    with factory() as session:
        row = session.scalar(select(FaithCase).where(FaithCase.eval_id == "F02"))
        assert row.status == "未解决" and row.resolution is None and row.resolved_at is not None
        assert row.seen_count == 2
