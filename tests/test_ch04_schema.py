from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select

from app.database import build_session_factory
from app.models import Base, Conversation, FaithCase, LowConfidenceQuestion


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ch04.db'}")
    Base.metadata.create_all(engine)
    yield build_session_factory(engine)
    engine.dispose()


def test_tables_and_defaults(factory):
    with factory() as session:
        assert {"low_confidence_questions", "faith_cases"} <= set(inspect(session.get_bind()).get_table_names())
        conversation = Conversation(user_id="anon")
        session.add(conversation)
        session.flush()
        session.add(LowConfidenceQuestion(
            conversation_id=conversation.id, raw_question="支持货到付款吗", source="retrieval_low_conf", reason="top1=0.1"
        ))
        session.add(FaithCase(
            eval_id="F01", bucket="A_policy", query="退款多久到账", answer="3 天", reason="证据没有 3 天",
            citations=[{"n": 1, "chunk_id": 1, "section_path": "退换货与退款 › 退款到账时间"}],
            judge_model="test-model",
        ))
        session.commit()
        low = session.scalar(select(LowConfidenceQuestion))
        case = session.scalar(select(FaithCase))
        assert low.created_at is not None and low.source == "retrieval_low_conf"
        assert case.status == "未解决" and case.seen_count == 1
        assert case.strategy == "hybrid_rerank" and case.citations[0]["n"] == 1


def test_sql_file_matches_user_ddl():
    sql = Path(__file__).resolve().parents[1].joinpath("scripts/init_sql/004_rag_advanced.sql").read_text(encoding="utf-8")
    assert sql.startswith("SET NAMES utf8mb4;")
    for token in (
        "fk_lcq_conversation", "ENUM('retrieval_low_conf','self_check','user_feedback')",
        "KEY idx_source", "KEY idx_created_at", "uk_eval_id", "ENUM('未解决','已解决','无需解决')",
        "first_seen_at", "last_seen_at", "resolved_at", "COMMENT='低置信度问题池'", "COMMENT='ch04 忠实度编造个案台账'",
    ):
        assert token in sql
