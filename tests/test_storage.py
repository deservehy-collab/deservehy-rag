from datetime import date

import pytest
from sqlalchemy import create_engine, select

from app.database import build_session_factory
from app.models import Base, Conversation, Message, Ticket
from app.seed import seed_faq
from app.storage import ConversationStore


@pytest.fixture()
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'storage.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    yield factory, ConversationStore(factory)
    engine.dispose()


def test_start_turn_creates_conversation_and_user_message(database):
    factory, store = database
    conversation_id = store.start_turn(user_id="anon-1", message="订单 1001 的物流到哪了")

    with factory() as session:
        conversation = session.get(Conversation, conversation_id)
        rows = session.scalars(select(Message).where(Message.conversation_id == conversation_id)).all()
        assert conversation.user_id == "anon-1"
        assert conversation.status == "进行中"
        assert [(row.role, row.content) for row in rows] == [("user", "订单 1001 的物流到哪了")]


def test_start_turn_continues_existing_conversation(database):
    factory, store = database
    first = store.start_turn(user_id="anon-1", message="第一句")
    second = store.start_turn(user_id="anon-1", message="第二句", conversation_id=first)

    with factory() as session:
        assert second == first
        assert session.scalar(select(Conversation).with_only_columns(Conversation.id).order_by(Conversation.id)) == first
        assert session.scalars(select(Message.role).where(Message.conversation_id == first)).all() == ["user", "user"]


def test_start_turn_refuses_another_users_conversation(database):
    factory, store = database
    mine = store.start_turn(user_id="anon-1", message="我的会话")
    other = store.start_turn(user_id="anon-2", message="借用别人的会话", conversation_id=mine)

    assert other != mine
    with factory() as session:
        assert session.get(Conversation, other).user_id == "anon-2"


def test_add_message_round_trips_tool_details(database):
    factory, store = database
    conversation_id = store.start_turn(user_id="anon-1", message="查物流")
    calls = [{"name": "query_logistics", "args": {"order_id": "1001"}, "id": "call_1", "type": "tool_call"}]
    assistant_id = store.add_message(conversation_id, "assistant", content="", tool_calls=calls)
    store.add_message(conversation_id, "tool", content='{"ok": true}', tool_call_id="call_1")
    store.add_message(conversation_id, "assistant", content="已经到上海了。")

    with factory() as session:
        rows = session.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)).all()
        assert rows[1].id == assistant_id
        assert rows[1].tool_calls == calls
        assert rows[2].tool_call_id == "call_1"
        assert rows[3].content == "已经到上海了。"


def test_create_ticket_uses_daily_sequence_and_keeps_description(database):
    factory, store = database
    conversation_id = store.start_turn(user_id="anon-1", message="杯子碎了")
    first = store.create_ticket(conversation_id, "收到时杯子已经碎了，想换一个。", "售后")
    second = store.create_ticket(conversation_id, "希望尽快处理。", "售后")

    today = date.today().strftime("%Y%m%d")
    assert first == f"T{today}001"
    assert second == f"T{today}002"
    with factory() as session:
        ticket = session.get(Ticket, first)
        assert ticket.description.startswith("收到时杯子已经碎了")
        assert ticket.ticket_type == "售后"
        assert ticket.status == "待处理"


