from app.storage import ConversationStore
from tests.support import sqlite_factory


def test_history_excludes_tool_and_empty_rows():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        cid, uid = store.start_turn(user_id="u", message="问", with_message_id=True)
        store.add_message(cid, "assistant", content="答")
        store.add_message(cid, "assistant", tool_calls=[{"id": "c1", "name": "query_order", "args": {}}])
        store.add_message(cid, "tool", content="{}", tool_call_id="c1")
        rows = store.context_messages(cid)
        assert [(r["role"], r["content"]) for r in rows] == [("user", "问"), ("assistant", "答")]
        assert rows[0]["id"] == uid


def test_messages_between_is_exclusive_left_inclusive_right():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        cid, first = store.start_turn(user_id="u", message="一", with_message_id=True)
        second = store.add_message(cid, "assistant", content="二")
        third = store.add_message(cid, "user", content="三")
        rows = store.messages_between(cid, first, third)
        assert [row["id"] for row in rows] == [second, third]


def test_summary_append_advances_anchor_and_rejects_stale():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        cid, uid = store.start_turn(user_id="u", message="订单1001退款", with_message_id=True)
        store.add_message(cid, "assistant", content="好的")
        assert store.anchors(cid) == (0, 0)
        seq = store.append_summary(cid, from_msg_id=0, upto_msg_id=uid, content="问过订单1001，要求退款。")
        assert seq == 1 and store.anchors(cid)[0] == uid
        assert store.append_summary(cid, from_msg_id=0, upto_msg_id=uid, content="重复") is None
        assert [item["seq"] for item in store.summaries(cid)] == [1]


def test_list_conversations_preview_order_and_summarized_flag():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        first = store.start_turn(user_id="u", message="最开始问订单 1001 的退款")
        second = store.start_turn(user_id="u", message="后来问物流")
        store.append_summary(first, from_msg_id=0, upto_msg_id=1, content="订单 1001 退款诉求。")
        items = store.list_conversations("u")
        assert [item["id"] for item in items] == [second, first]
        assert items[0]["title"] == "后来问物流"
        assert items[1]["summarized"] is True


def test_conversation_messages_checks_owner():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        cid = store.start_turn(user_id="owner", message="问题")
        assert store.conversation_messages(cid, "other") is None
        assert [m["role"] for m in store.conversation_messages(cid, "owner")] == ["user"]


def test_set_layer1_from_does_not_reorder_conversations():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        first = store.start_turn(user_id="u", message="早")
        second = store.start_turn(user_id="u", message="晚")
        store.set_layer1_from(first, 99)
        assert store.anchors(first) == (0, 99)
        assert [item["id"] for item in store.list_conversations("u")] == [second, first]
