from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory, ensure_database, validate_database_name
from app.models import Base, Conversation, Faq, KnowledgeChunk, Message, Ticket
from app.seed import FAQ_SEED, seed_faq

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sqlite_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield build_session_factory(engine)
    engine.dispose()


def test_schema_has_the_business_and_knowledge_tables(sqlite_factory):
    with sqlite_factory() as session:
        engine = session.get_bind()
        assert set(inspect(engine).get_table_names()) == {
            "conversations",
            "messages",
            "faq",
            "tickets",
            "knowledge_chunks",
            "qa_extraction_staging",
            "low_confidence_questions",
            "faith_cases",
        }


def test_message_row_keeps_tool_round_trip(sqlite_factory):
    with sqlite_factory() as session:
        conversation = Conversation(user_id="anon-test")
        session.add(conversation)
        session.flush()
        calls = [{"name": "query_logistics", "args": {"order_id": "1001"}, "id": "call_1", "type": "tool_call"}]
        assistant = Message(conversation_id=conversation.id, role="assistant", content="", tool_calls=calls)
        result = Message(conversation_id=conversation.id, role="tool", content='{"ok": true}', tool_call_id="call_1")
        session.add_all([assistant, result])
        session.commit()

        stored = session.scalars(select(Message).order_by(Message.id)).all()
        assert [item.role for item in stored] == ["assistant", "tool"]
        assert stored[0].tool_calls == calls
        assert stored[1].tool_call_id == "call_1"
        assert conversation.status == "进行中"


def test_ticket_no_is_primary_key_and_links_conversation(sqlite_factory):
    with sqlite_factory() as session:
        conversation = Conversation(user_id="anon-test")
        session.add(conversation)
        session.flush()
        session.add(Ticket(ticket_no="T20261028001", conversation_id=conversation.id, description="杯子碎了", ticket_type="售后"))
        session.commit()

        ticket = session.get(Ticket, "T20261028001")
        assert ticket is not None
        assert ticket.status == "待处理"
        assert ticket.conversation_id == conversation.id


def test_seed_faq_is_idempotent(sqlite_factory):
    seed_faq(sqlite_factory)
    seed_faq(sqlite_factory)
    with sqlite_factory() as session:
        assert session.scalar(select(func.count()).select_from(Faq)) == len(FAQ_SEED)
        questions = session.scalars(select(Faq.question)).all()
        assert "退货政策是什么" in questions
        assert not any("邮费" in question or "运费" in question for question in questions)


def test_docker_seed_sql_matches_python_seed():
    sql = (ROOT / "scripts/init_sql/002_seed_faq.sql").read_text(encoding="utf-8")
    schema = (ROOT / "scripts/init_sql/001_schema.sql").read_text(encoding="utf-8")
    rag = (ROOT / "scripts/init_sql/003_rag.sql").read_text(encoding="utf-8")
    for row in FAQ_SEED:
        assert row["question"] in sql
    for table in ("conversations", "messages", "faq", "tickets"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema
    for table in ("knowledge_chunks", "qa_extraction_staging"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in rag
    assert "ENUM('pending','done')" in rag
    assert "ON DELETE SET NULL" in rag
    assert "KEY idx_category (category)" in rag
    assert "KEY idx_vectorize_status (vectorize_status)" in rag
    advanced = (ROOT / "scripts/init_sql/004_rag_advanced.sql").read_text(encoding="utf-8")
    assert advanced.startswith("SET NAMES utf8mb4;")
    for token in (
        "fk_lcq_conversation",
        "ENUM('retrieval_low_conf','self_check','user_feedback')",
        "uk_eval_id",
        "ENUM('未解决','已解决','无需解决')",
        "COMMENT='低置信度问题池'",
        "COMMENT='ch04 忠实度编造个案台账'",
    ):
        assert token in advanced


def test_database_name_guard_rejects_injection():
    with pytest.raises(ValueError):
        validate_database_name("xiaolin_ch02; DROP DATABASE mysql")
    assert validate_database_name("xiaolin_ch02_test") == "xiaolin_ch02_test"


def test_database_url_keeps_password_out_of_plain_repr():
    settings = Settings(
        _env_file=None,
        openai_base_url="http://upstream.invalid/v1",
        openai_model="test-model",
        openai_api_key="test-secret-not-real",
        db_password="p@ss:word/with#chars",
    )
    url = build_database_url(settings)
    assert "p@ss" not in str(url)
    assert url.password == "p@ss:word/with#chars"
    assert url.database == "xiaolin_ch02"


def mysql_engine_or_skip():
    try:
        settings = Settings.from_env()
    except Exception:
        pytest.skip("没有可用的 .env，跳过 MySQL 集成检查")
    if not settings.db_password.get_secret_value():
        pytest.skip("未配置数据库密码，跳过 MySQL 集成检查")
    try:
        ensure_database(settings, settings.db_name_test)
        engine = build_engine(build_database_url(settings, settings.db_name_test))
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except SQLAlchemyError:
        pytest.skip("MySQL 不可达，跳过集成检查")
    return engine


def test_mysql_schema_uses_real_enum_and_json():
    engine = mysql_engine_or_skip()
    try:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        ddl = {}
        with engine.connect() as connection:
            for table in (
                "conversations", "messages", "faq", "tickets", "knowledge_chunks", "qa_extraction_staging",
                "low_confidence_questions", "faith_cases",
            ):
                ddl[table] = connection.execute(text(f"show create table {table}")).one()[1]
        assert "enum('user','assistant','tool')" in ddl["messages"]
        assert "`tool_calls` json" in ddl["messages"]
        assert "bigint unsigned" in ddl["conversations"]
        assert "ENGINE=InnoDB" in ddl["faq"]
        assert "enum('pending','done')" in ddl["knowledge_chunks"]
        assert "ON DELETE SET NULL" in ddl["knowledge_chunks"]
        assert "idx_knowledge_category" in ddl["knowledge_chunks"]
        assert "enum('extracted','kept','discarded')" in ddl["qa_extraction_staging"]
        assert "enum('retrieval_low_conf','self_check','user_feedback')" in ddl["low_confidence_questions"]
        assert "idx_low_confidence_source" in ddl["low_confidence_questions"]
        assert "fk_lcq_conversation" in ddl["low_confidence_questions"]
        assert "`citations` json" in ddl["faith_cases"]
        assert "int unsigned" in ddl["faith_cases"]
        assert "uk_eval_id" in ddl["faith_cases"]
        assert "idx_faith_status" in ddl["faith_cases"]
        assert "bigint unsigned" in ddl["faith_cases"]

        # 中文 ENUM、中文种子都要能真正写进去再读回来，防止连接字符集把值截断。
        factory = build_session_factory(engine)
        assert seed_faq(factory) == len(FAQ_SEED)
        with factory() as session:
            conversation = Conversation(user_id="anon-汉字-user")
            session.add(conversation)
            session.commit()
            assert conversation.status == "进行中"
            assert session.scalar(select(Faq.question).where(Faq.question == "退货政策是什么")) == "退货政策是什么"

            first = KnowledgeChunk(category="配送与运费", questions="邮费是多少？", answer="普通地区邮费 10 元。")
            session.add(first)
            session.commit()
            second = KnowledgeChunk(
                category="配送与运费", questions="多久发货？", answer="48 小时内发货。", prev_chunk_id=first.id
            )
            session.add(second)
            session.commit()
            first.next_chunk_id = second.id
            session.commit()
            assert second.vectorize_status == "pending"

            session.delete(first)
            session.commit()
            session.refresh(second)
            assert second.prev_chunk_id is None
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
