import json
import math

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, select

from app.database import build_session_factory
from app.knowledge.ingest import KnowledgeIngestor
from app.knowledge.mining import ConversationMiner
from app.knowledge.splitter import Chunk
from app.models import Base, Conversation, KnowledgeChunk, Message, QaExtractionStaging


class FakeEmbedder:
    def encode(self, texts):
        vectors = []
        for text in texts:
            if "邮" in text or "运费" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "退" in text:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class FakeVectorStore:
    def __init__(self):
        self.rows = {}

    def ensure_collection(self):
        pass

    def upsert(self, ids, vectors):
        for item_id, vector in zip(ids, vectors):
            self.rows[str(item_id)] = vector

    def search(self, vector, limit):
        return []

    def drop(self):
        self.rows.clear()


class FakeModel:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return AIMessage(content=content)


@pytest.fixture()
def mining_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mining.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    embedder = FakeEmbedder()
    ingestor = KnowledgeIngestor(factory, embedder, FakeVectorStore())
    with factory() as session:
        conversation = Conversation(user_id="anon-mine")
        session.add(conversation)
        session.flush()
        session.add_all(
            [
                Message(conversation_id=conversation.id, role="user", content="你们邮费怎么收？"),
                Message(conversation_id=conversation.id, role="assistant", content="满 99 元包邮，未满收 8 元。"),
            ]
        )
        session.commit()
        conversation_id = conversation.id
    try:
        yield factory, embedder, ingestor, conversation_id
    finally:
        engine.dispose()


def test_mining_stages_dedupes_and_ingests(mining_env):
    factory, embedder, ingestor, conversation_id = mining_env
    model = FakeModel(
        [
            {"question": "邮费怎么收", "answer": "满 99 元包邮，未满收 8 元。"},
            {"question": "发票怎么开", "answer": "订单完成后在订单详情页申请电子发票。"},
        ]
    )
    miner = ConversationMiner(factory, model, ingestor, embedder, dedupe_threshold=0.95)
    result = miner.mine()
    assert result["conversations"] == 1
    assert result["extracted"] == 2
    assert result["kept"] == 2
    assert result["discarded"] == 0
    assert result["ingested"] == 2
    assert result["vectorized"] == 2
    with factory() as session:
        staged = session.scalars(select(QaExtractionStaging).order_by(QaExtractionStaging.id)).all()
        assert [row.status for row in staged] == ["kept", "kept"]
        assert all(row.source_ref == f"conversation:{conversation_id}" for row in staged)
        chunks = session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id)).all()
        assert [row.questions for row in chunks] == ["邮费怎么收", "发票怎么开"]
        assert all(row.content_type == "mined_qa" for row in chunks)
        assert all(row.vectorize_status == "done" for row in chunks)


def test_mining_skips_already_mined_conversation(mining_env):
    factory, embedder, ingestor, _ = mining_env
    model = FakeModel([{"question": "邮费怎么收", "answer": "满 99 元包邮。"}])
    miner = ConversationMiner(factory, model, ingestor, embedder)
    assert miner.mine()["extracted"] == 1
    assert miner.mine()["conversations"] == 0
    assert model.calls == 1


def test_mining_discards_exact_duplicate_of_knowledge(mining_env):
    factory, embedder, ingestor, _ = mining_env
    ingestor.ingest_chunks(
        [
            Chunk(
                category="会话问答",
                questions="邮费怎么收",
                answer="满 99 元包邮。",
                section_path=None,
                content_type="mined_qa",
                is_key_clause=False,
                order=0,
            )
        ],
        link_sequential=False,
    )
    model = FakeModel([{"question": "邮费怎么收", "answer": "满 99 元包邮。"}])
    result = ConversationMiner(factory, model, ingestor, embedder).mine()
    assert result["kept"] == 0
    assert result["discarded"] == 1
    assert result["ingested"] == 0
    with factory() as session:
        staged = session.scalars(select(QaExtractionStaging)).all()
        assert [row.status for row in staged] == ["discarded"]


def test_mining_discards_semantically_similar_question(mining_env):
    factory, embedder, ingestor, _ = mining_env
    ingestor.ingest_chunks(
        [
            Chunk(
                category="配送与运费 › 运费标准",
                questions="运费怎么计算",
                answer="满 99 元免运费。",
                section_path="配送与运费 › 运费标准",
                content_type="policy",
                is_key_clause=False,
                order=0,
            )
        ],
        link_sequential=False,
    )
    model = FakeModel([{"question": "邮费是多少", "answer": "满 99 元免邮费。"}])
    result = ConversationMiner(factory, model, ingestor, embedder).mine()
    assert result["discarded"] == 1
    with factory() as session:
        staged = session.scalars(select(QaExtractionStaging)).all()
        assert [row.status for row in staged] == ["discarded"]


def test_mining_tolerates_invalid_json(mining_env):
    factory, embedder, ingestor, _ = mining_env
    model = FakeModel("这不是 JSON")
    result = ConversationMiner(factory, model, ingestor, embedder).mine()
    assert result["extracted"] == 0
    with factory() as session:
        assert session.scalars(select(QaExtractionStaging)).all() == []
