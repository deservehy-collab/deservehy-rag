import json

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import Conversation, Message
from app.storage import ConversationStore
from tests.support import api_with_db, events, streamed, streamed_tool_call


def test_tool_round_emits_status_frames_and_persists_every_message():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return streamed_tool_call("query_logistics", {"order_id": "1001"})
        return streamed(("包裹", "已到上海分拨中心，准备派送。"))

    with api_with_db(handler) as (client, seen, factory):
        response = client.post("/api/chat", json={"message": "订单 1001 的物流到哪了", "client_id": "anon-test-1"})
        assert response.status_code == 200
        frames = events(response)
        assert [name for name, _ in frames] == ["meta", "status", "status", "token", "token", "done"]
        assert frames[0][1]["conversation_id"] >= 1
        assert frames[1][1] == {"phase": "start", "tool": "query_logistics", "label": "查询物流"}
        assert frames[2][1]["ok"] is True
        assert "2 条物流记录" in frames[2][1]["summary"]
        assert frames[2][1]["label"] == "查询物流"

        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
            assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]
            assert rows[0].content == "订单 1001 的物流到哪了"
            assert rows[1].tool_calls[0]["name"] == "query_logistics"
            assert rows[2].tool_call_id == "call_1"
            assert json.loads(rows[2].content)["data"]["carrier"] == "顺丰速运"
            assert rows[3].content == "包裹已到上海分拨中心，准备派送。"

        assert len(calls) == 2
        second = calls[1]["messages"]
        assert second[0]["role"] == "system"
        assert second[-2]["role"] == "assistant"
        assert second[-2]["tool_calls"][0]["function"]["name"] == "query_logistics"
        assert second[-1]["role"] == "tool"
        assert "上海分拨中心" in second[-1]["content"]
        assert second[-1]["tool_call_id"] == "call_1"


def test_plain_answer_keeps_ch01_event_shape_and_persists_pair():
    with api_with_db(lambda request: streamed(("你好", "，有什么可以帮你？"))) as (client, seen, factory):
        response = client.post("/api/chat", json={"message": "保温杯多少钱？", "client_id": "anon-test-2"})
        frames = events(response)
        assert [name for name, _ in frames] == ["meta", "token", "token", "done"]
        assert frames[-1][1] == {"finish_reason": "stop"}
        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
            assert [row.role for row in rows] == ["user", "assistant"]
            assert rows[1].content == "你好，有什么可以帮你？"
            assert rows[1].tool_calls is None


def test_second_turn_reuses_conversation_id():
    with api_with_db(lambda request: streamed(("好的",))) as (client, seen, factory):
        first = client.post("/api/chat", json={"message": "第一句", "client_id": "anon-test-3"})
        conversation_id = events(first)[0][1]["conversation_id"]
        second = client.post(
            "/api/chat",
            json={"message": "第二句", "client_id": "anon-test-3", "conversation_id": conversation_id},
        )
        assert events(second)[0][1]["conversation_id"] == conversation_id
        with factory() as session:
            assert session.scalars(select(Conversation.user_id)).all() == ["anon-test-3"]
            assert [row.role for row in session.scalars(select(Message).order_by(Message.id))] == [
                "user", "assistant", "user", "assistant",
            ]


def test_tool_failure_is_fed_back_to_the_model_and_the_page():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return streamed_tool_call("query_order", {"order_id": "9999"})
        return streamed(("抱歉，没有查到订单 9999，", "可以核对一下订单号吗？"))

    with api_with_db(handler) as (client, seen, factory):
        response = client.post("/api/chat", json={"message": "帮我查订单 9999", "client_id": "anon-test-4"})
        frames = events(response)
        assert [name for name, _ in frames] == ["meta", "status", "status", "token", "token", "done"]
        assert frames[2][1]["ok"] is False
        assert "没有找到订单 9999" in frames[2][1]["summary"]
        tool_payload = json.loads(seen[1][1]["messages"][-1]["content"])
        assert tool_payload == {"ok": False, "error": "not_found", "message": "没有找到订单 9999。"}
        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
            assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]


class BrokenStore(ConversationStore):
    def __init__(self):
        pass

    def start_turn(self, **kwargs):
        raise OperationalError("select 1", {}, Exception("database is down"))


def test_database_failure_returns_friendly_error():
    with api_with_db(lambda request: streamed(("不应该被调用",))) as (client, seen, factory):
        client.app.state.service.store = BrokenStore()
        response = client.post("/api/chat", json={"message": "你好", "client_id": "anon-test-5"})
        assert response.status_code == 200
        frames = events(response)
        assert [name for name, _ in frames] == ["error"]
        assert frames[0][1]["code"] == "database_unavailable"
        assert "稍后重试" in frames[0][1]["message"]
        assert seen == []


def test_client_id_and_conversation_id_are_validated():
    with api_with_db(lambda request: streamed(("x",))) as (client, seen, factory):
        bad_client = client.post("/api/chat", json={"message": "你好", "client_id": "x"})
        bad_conversation = client.post("/api/chat", json={"message": "你好", "client_id": "anon-test-6", "conversation_id": 0})
        assert bad_client.status_code == 422
        assert bad_client.json()["code"] == "invalid_request"
        assert bad_conversation.status_code == 422


LEAKED_TOOL_MARKUP = (
    '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="query_faq">\n'
    '<｜｜DSML｜｜ parameter name="keyword" string="true">运费</｜｜DSML｜｜ parameter>\n'
    "</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>"
)


def test_leaked_tool_markup_in_second_pass_never_reaches_the_page():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return streamed_tool_call("query_faq", {"keyword": "邮费"})
        return streamed(("<｜｜DSM", LEAKED_TOOL_MARKUP[6:]))

    with api_with_db(handler) as (client, seen, factory):
        response = client.post("/api/chat", json={"message": "邮费是多少？", "client_id": "anon-test-7"})
        frames = events(response)
        tokens = "".join(payload["content"] for name, payload in frames if name == "token")
        assert "DSML" not in tokens and "｜" not in tokens
        assert "请再问一次" in tokens
        assert [name for name, _ in frames][-1] == "done"
        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
            assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]
            assert rows[-1].content == tokens


def test_leaked_markup_after_a_valid_answer_keeps_the_valid_part():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return streamed_tool_call("query_faq", {"keyword": "邮费"})
        return streamed(("抱歉，店铺政策里没有运费说明。", LEAKED_TOOL_MARKUP))

    with api_with_db(handler) as (client, seen, factory):
        response = client.post("/api/chat", json={"message": "邮费是多少？", "client_id": "anon-test-8"})
        frames = events(response)
        tokens = "".join(payload["content"] for name, payload in frames if name == "token")
        assert tokens == "抱歉，店铺政策里没有运费说明。"
        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
            assert rows[-1].content == tokens


def test_leaked_markup_in_a_plain_answer_is_stripped():
    with api_with_db(lambda request: streamed(("你好", LEAKED_TOOL_MARKUP))) as (client, seen, factory):
        response = client.post("/api/chat", json={"message": "保温杯多少钱？", "client_id": "anon-test-9"})
        frames = events(response)
        assert [name for name, _ in frames] == ["meta", "token", "done"]
        assert frames[1][1]["content"] == "你好"
        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
            assert rows[-1].content == "你好"
