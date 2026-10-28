from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory, ensure_database, validate_database_name
from app.models import Base, Conversation, Faq, Message, Ticket
from app.seed import FAQ_SEED, seed_faq

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sqlite_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield build_session_factory(engine)
    engine.dispose()


def test_schema_has_the_four_business_tables(sqlite_factory):
    with sqlite_factory() as session:
        engine = session.get_bind()
        assert set(inspect(engine).get_table_names()) == {"conversations", "messages", "faq", "tickets"}


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
    for row in FAQ_SEED:
        assert row["question"] in sql
    for table in ("conversations", "messages", "faq", "tickets"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema


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
            for table in ("conversations", "messages", "faq", "tickets"):
                ddl[table] = connection.execute(text(f"show create table {table}")).one()[1]
        assert "enum('user','assistant','tool')" in ddl["messages"]
        assert "`tool_calls` json" in ddl["messages"]
        assert "bigint unsigned" in ddl["conversations"]
        assert "ENGINE=InnoDB" in ddl["faq"]

        # 中文 ENUM、中文种子都要能真正写进去再读回来，防止连接字符集把值截断。
        factory = build_session_factory(engine)
        assert seed_faq(factory) == len(FAQ_SEED)
        with factory() as session:
            conversation = Conversation(user_id="anon-汉字-user")
            session.add(conversation)
            session.commit()
            assert conversation.status == "进行中"
            assert session.scalar(select(Faq.question).where(Faq.question == "退货政策是什么")) == "退货政策是什么"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
