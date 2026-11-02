from sqlalchemy import select

from app.models import LowConfidenceQuestion
from app.storage import ConversationStore
from tests.support import api_with_db, streamed

CLIENT = "client-abcdef12"


def seed(factory, *, snapshot=None):
    """建一个会话：一条用户提问 + 一条带快照的 assistant 回答。返回 (conversation_id, message_id)。

    snapshot=None 用默认片段；要「这一轮没检索」传 snapshot=[]（别用 `snapshot or [...]`，
    那样 None 和 [] 都会落成默认片段，`test_turn_without_retrieval_still_pools_with_empty_chunks` 永远绿不了）。
    """
    store = ConversationStore(factory)
    conversation_id = store.start_turn(user_id=CLIENT, message="运费怎么算")
    if snapshot is None:
        snapshot = [{"n": 1, "chunk_id": 3, "question": "普通地区邮费",
                     "answer": "满 99 元免运费", "section_path": "配送与运费 › 运费标准",
                     "scores": {"rerank": 0.9}}]
    message_id = store.add_message(conversation_id, "assistant", content="满99免运费",
                                   retrieval_snapshot=snapshot)
    return conversation_id, message_id


def post(client, conversation_id, message_id, value="down"):
    return client.post("/api/feedback", json={"conversation_id": conversation_id,
                                              "client_id": CLIENT, "message_id": message_id,
                                              "value": value})


def test_thumbs_down_pools_the_user_question_with_snapshot():
    with api_with_db(lambda request: streamed(), classifier=None) as (client, seen, factory):
        conversation_id, message_id = seed(factory)
        response = post(client, conversation_id, message_id)
        assert response.status_code == 200 and response.json()["status"] == "recorded"
        with factory() as session:
            row = session.scalars(select(LowConfidenceQuestion)).one()
        assert row.source == "user_feedback"
        assert row.raw_question == "运费怎么算"
        assert row.reason == f"feedback:message_id={message_id}"
        assert row.retrieved_chunks[0]["chunk_id"] == 3


def test_thumbs_up_does_not_pool():
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        conversation_id, message_id = seed(factory)
        assert post(client, conversation_id, message_id, "up").json()["status"] == "ignored"
        with factory() as session:
            assert session.scalars(select(LowConfidenceQuestion)).all() == []


def test_repeat_thumbs_down_is_idempotent_not_duplicated():
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        conversation_id, message_id = seed(factory)
        assert post(client, conversation_id, message_id).json()["status"] == "recorded"
        assert post(client, conversation_id, message_id).json()["status"] == "duplicate"
        with factory() as session:
            assert len(session.scalars(select(LowConfidenceQuestion)).all()) == 1


def test_message_id_prefix_does_not_match_longer_reason():
    """幂等用精确等号，不用 LIKE（规格 §8）。

    造真正的「前缀碰撞」：库里已有的 reason 是本次 message_id 再拼两位数字（`=223` 之于 `=2`）。
    LIKE '%feedback:message_id=2%' 会误命中它，`==` 不会；如果只拿 `=123` 当诱饵，
    当前 message_id 是 2，`=2` 根本不是 `=123` 的子串，测试变成永绿（评审 Critical）。
    """
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        from app.knowledge.pool import LowConfidencePool

        conversation_id, message_id = seed(factory)
        LowConfidencePool(factory).record(conversation_id, "别的", "user_feedback",
                                          f"feedback:message_id={message_id}23")
        assert post(client, conversation_id, message_id).json()["status"] == "recorded"
        with factory() as session:
            assert len(session.scalars(select(LowConfidenceQuestion)).all()) == 2


def test_pool_lookup_db_error_is_503(monkeypatch):
    """幂等查询在 try 外会把 SQLAlchemyError 泄漏成 500；必须和 owner/message 一样映射成 503。"""
    from sqlalchemy.exc import SQLAlchemyError

    from app.knowledge.pool import LowConfidencePool

    def boom(self, conversation_id, reason):
        raise SQLAlchemyError("db down")

    monkeypatch.setattr(LowConfidencePool, "exists_feedback", boom)
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        conversation_id, message_id = seed(factory)
        response = post(client, conversation_id, message_id)
        assert response.status_code == 503 and response.json()["code"] == "database_unavailable"


def test_wrong_client_id_is_403():
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        conversation_id, message_id = seed(factory)
        response = client.post("/api/feedback", json={"conversation_id": conversation_id,
                                                      "client_id": "someone-else1",
                                                      "message_id": message_id, "value": "down"})
        assert response.status_code == 403 and response.json()["code"] == "forbidden"


def test_unknown_message_is_404():
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        conversation_id, _ = seed(factory)
        response = post(client, conversation_id, 999999)
        assert response.status_code == 404


def test_message_from_another_conversation_is_404():
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        conversation_id, message_id = seed(factory)
        other = ConversationStore(factory).start_turn(user_id=CLIENT, message="另一会话")
        response = post(client, other, message_id)
        assert response.status_code == 404


def test_turn_without_retrieval_still_pools_with_empty_chunks():
    """闲聊/业务数据类那轮本来没检索：快照留空是对的，不许编一份假的（规格 §8）。"""
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        conversation_id, message_id = seed(factory, snapshot=[])
        assert post(client, conversation_id, message_id).json()["status"] == "recorded"
        with factory() as session:
            assert session.scalars(select(LowConfidenceQuestion)).one().retrieved_chunks is None


def test_feedback_request_validation():
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        conversation_id, message_id = seed(factory)
        bad = client.post("/api/feedback", json={"conversation_id": conversation_id,
                                                 "client_id": CLIENT, "message_id": message_id,
                                                 "value": "sideways"})
        assert bad.status_code == 422 and bad.json()["code"] == "invalid_request"


def test_thumbs_down_schedules_flywheel():
    with api_with_db(lambda request: streamed()) as (client, seen, factory):
        conversation_id, message_id = seed(factory)

        class Trigger:
            def __init__(self):
                self.calls = 0

            def schedule(self):
                self.calls += 1
                return True

        trigger = Trigger()
        client.app.state.service.flywheel = trigger
        assert post(client, conversation_id, message_id).json()["status"] == "recorded"
        assert trigger.calls == 1
