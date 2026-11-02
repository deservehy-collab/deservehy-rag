import asyncio

from sqlalchemy import select

from app.knowledge.pool import LowConfidencePool
from app.models import LowConfidenceQuestion, Message
from app.storage import ConversationStore
from tests.support import (FakeKnowledge, FixedClassifier, api_with_db, events, sqlite_factory,
                           streamed)


def test_pool_records_snapshot():
    with sqlite_factory() as factory:
        LowConfidencePool(factory).record(
            None, "邮费怎么算", "retrieval_low_conf", "confidence=0.080<0.350",
            retrieved_chunks=[{"n": 1, "chunk_id": 3, "question": "运费", "answer": "满99免运费",
                               "section_path": "配送与运费 › 运费标准",
                               "scores": {"rerank": 0.1, "rrf": 0.03}}])
        with factory() as session:
            row = session.scalars(select(LowConfidenceQuestion)).one()
        assert row.retrieved_chunks[0]["chunk_id"] == 3
        assert row.retrieved_chunks[0]["scores"]["rerank"] == 0.1


def test_pool_records_at_most_ten_chunks():
    with sqlite_factory() as factory:
        chunks = [{"n": index, "chunk_id": index, "question": "q", "answer": "a",
                   "section_path": "s", "scores": {}} for index in range(1, 30)]
        LowConfidencePool(factory).record(None, "邮费", "retrieval_low_conf", "r",
                                          retrieved_chunks=chunks)
        with factory() as session:
            assert len(session.scalars(select(LowConfidenceQuestion)).one().retrieved_chunks) == 10


def test_pool_snapshot_defaults_to_none():
    with sqlite_factory() as factory:
        LowConfidencePool(factory).record(None, "闲聊", "self_check", "self_check")
        with factory() as session:
            assert session.scalars(select(LowConfidenceQuestion)).one().retrieved_chunks is None


def test_pool_empty_snapshot_also_normalizes_to_none():
    """`[]`（这轮没检索/没片段）与 `None` 同义，列里必须是 NULL，不是空 JSON 数组：
    下游按 "is null" 判断「这轮本来就没快照」，空数组会让它看起来像「检索过但零命中」。"""
    with sqlite_factory() as factory:
        LowConfidencePool(factory).record(None, "闲聊", "self_check", "self_check",
                                         retrieved_chunks=[])
        with factory() as session:
            assert session.scalars(select(LowConfidenceQuestion)).one().retrieved_chunks is None


def test_add_message_stores_retrieval_snapshot():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="u-1", message="邮费")
        message_id = store.add_message(
            conversation_id, "assistant", content="满99免运费",
            retrieval_snapshot=[{"n": 1, "chunk_id": 9, "question": "运费",
                                 "answer": "满99免运费", "section_path": "s", "scores": {"rerank": 0.9}}])
        with factory() as session:
            assert session.get(Message, message_id).retrieval_snapshot[0]["chunk_id"] == 9


def test_done_frame_carries_message_id():
    with api_with_db(lambda request: streamed(parts=("好",))) as (client, seen, factory):
        with client.stream("POST", "/api/chat", json={"conversation_id": 1, "message": "你好"}) as r:
            r.read()  # 流式响应要先读到底，events() 走的是 .text
            frames = events(r)
    done = [payload for name, payload in frames if name == "done"][-1]
    assert isinstance(done["message_id"], int) and done["message_id"] > 0


def test_answerable_turn_stores_snapshot_for_later_thumbs_down():
    """👎 要能事后回捞：assistant 行必须有当轮 Top-N 片段（验收④的数据前提）。"""
    # 走 knowledge 路由（商品咨询）才会跑强制检索节点；并换成 tests.support 的 FakeKnowledge，
    # 默认 app 挂的是真检索器（Milvus + 重排），单测里跑不起来。
    with api_with_db(lambda request: streamed(parts=("满99免运费",)),
                     classifier=FixedClassifier("商品咨询")) as (client, seen, factory):
        client.app.state.workflow.knowledge = FakeKnowledge()
        with client.stream("POST", "/api/chat",
                           json={"conversation_id": 1, "message": "邮费怎么算"}) as r:
            r.read()  # 流式响应要先读到底，events() 走的是 .text
            frames = events(r)
        message_id = [payload for name, payload in frames if name == "done"][-1]["message_id"]
        with factory() as session:
            snapshot = session.get(Message, message_id).retrieval_snapshot
    assert snapshot and snapshot[0]["question"] == "普通地区邮费"


def test_retrieve_node_state_carries_confidence_and_snapshot():
    from types import SimpleNamespace

    from app.knowledge.confidence import score
    from app.knowledge.retrieval import Evidence, RetrievalResult
    from app.workflow.knowledge_node import make_retrieve_node

    item = Evidence(7, "G500 杯盖适配", "G500 适配 P1001。", "型号与兼容性",
                    "型号与兼容性 › 保温杯系列", "policy", {"rerank": 0.9}, 1)
    result = RetrievalResult("hybrid_rerank", "问题", [item], [item],
                             confidence=score([item]),
                             snapshot=[{"n": 1, "chunk_id": 7, "question": item.question,
                                        "answer": item.answer, "section_path": item.section_path,
                                        "scores": {"rerank": 0.9}}])
    deps = SimpleNamespace(knowledge=SimpleNamespace(retrieve=lambda question: result),
                           expander=None,
                           settings=SimpleNamespace(knowledge_tool_timeout_seconds=5))
    update = asyncio.run(make_retrieve_node(deps)({"question": "问题"}))
    assert update["retrieval_snapshot"][0]["chunk_id"] == 7
    # state 里放的是 as_dict()（value 已 round 到 4 位），所以比较用同一份 as_dict。
    assert update["retrieval"]["confidence"]["value"] == score([item]).as_dict()["value"]


def test_policy_retrieve_node_state_carries_confidence_and_snapshot():
    from types import SimpleNamespace

    from app.knowledge.confidence import score
    from app.knowledge.retrieval import Evidence, RetrievalResult
    from app.workflow.policy_nodes import make_policy_retrieve_node

    item = Evidence(7, "G500 杯盖适配", "G500 适配 P1001。", "型号与兼容性",
                    "型号与兼容性 › 保温杯系列", "policy", {"rerank": 0.9}, 1)
    result = RetrievalResult("hybrid_rerank", "问题", [item], [item],
                             confidence=score([item]),
                             snapshot=[{"n": 1, "chunk_id": 7, "question": item.question,
                                        "answer": item.answer, "section_path": item.section_path,
                                        "scores": {"rerank": 0.9}}])
    deps = SimpleNamespace(knowledge=SimpleNamespace(retrieve_expanded=lambda queries: result),
                           expander=None,
                           settings=SimpleNamespace(knowledge_tool_timeout_seconds=5))
    update = asyncio.run(make_policy_retrieve_node(deps)({"question": "问题", "conversation_id": 1}))
    assert update["retrieval_snapshot"][0]["chunk_id"] == 7
    # state 里放的是 as_dict()（value 已 round 到 4 位），所以比较用同一份 as_dict。
    assert update["retrieval"]["confidence"]["value"] == score([item]).as_dict()["value"]
