import math
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from app.database import build_session_factory
from app.knowledge import dedupe
from app.knowledge.ingest import KnowledgeIngestor, embedding_text
from app.knowledge.retrieval import KnowledgeRetriever
from app.knowledge.splitter import Chunk, split_markdown
from app.models import Base, KnowledgeChunk
from app.seed import seed_faq


class FakeEmbedder:
    def __init__(self, dimension=4):
        self.dimension = dimension
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            raw = [
                1.0 if "邮" in text else 0.1,
                1.0 if "退" in text else 0.1,
                float(len(text) % 7),
                0.5,
            ]
            norm = math.sqrt(sum(value * value for value in raw))
            vectors.append([value / norm for value in raw])
        return vectors


class FakeVectorStore:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.payloads = []
        self.dense = []

    def ensure_collection(self):
        pass

    def upsert(self, rows):
        self.calls.append([row["vector_id"] for row in rows])
        for row in rows:
            self.rows[str(row["vector_id"])] = row["embedding"]
            self.payloads.append(row)

    def dense_search(self, vector, limit, expr=None):
        return self.dense[:limit]

    def sparse_search(self, text, limit, expr=None):
        return []

    def hybrid_search(self, vector, text, limit, rrf_k, expr=None):
        return []

    def search(self, vector, limit):
        return self.dense[:limit]

    def drop(self):
        self.rows.clear()


@pytest.fixture()
def knowledge_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    yield factory
    engine.dispose()


def make_ingestor(factory):
    embedder = FakeEmbedder()
    store = FakeVectorStore()
    return KnowledgeIngestor(factory, embedder, store), embedder, store


def chunks_of(count=3):
    return [
        Chunk(
            category="配送与运费 › 运费标准",
            questions=f"小节{i}",
            answer=f"第{i}条运费说明。",
            section_path=f"配送与运费 › 运费标准 › 小节{i}",
            content_type="policy",
            is_key_clause=False,
            order=i,
        )
        for i in range(count)
    ]


def test_ingest_is_idempotent_and_links_neighbours(knowledge_db):
    ingestor, _, _ = make_ingestor(knowledge_db)
    assert ingestor.ingest_chunks(chunks_of(3)) == 3
    assert ingestor.ingest_chunks(chunks_of(3)) == 0
    with knowledge_db() as session:
        rows = session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id)).all()
        assert len(rows) == 3
        assert rows[0].prev_chunk_id is None
        assert rows[0].next_chunk_id == rows[1].id
        assert rows[1].prev_chunk_id == rows[0].id
        assert rows[1].next_chunk_id == rows[2].id
        assert rows[2].next_chunk_id is None


def test_vectorize_pending_backfills_and_clears_pending(knowledge_db):
    ingestor, _, store = make_ingestor(knowledge_db)
    ingestor.ingest_chunks(chunks_of(3))
    assert ingestor.stats() == {"total": 3, "pending": 3, "done": 0}
    assert ingestor.vectorize_pending() == 3
    assert ingestor.stats() == {"total": 3, "pending": 0, "done": 3}
    with knowledge_db() as session:
        rows = session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id)).all()
        assert [row.vector_id for row in rows] == [str(row.id) for row in rows]
        assert all(row.vectorize_status == "done" for row in rows)
    assert len(store.rows) == 3


def test_vectorized_rows_carry_bm25_text_and_metadata(knowledge_db):
    ingestor, _, store = make_ingestor(knowledge_db)
    ingestor.ingest_chunks(chunks_of(1))
    ingestor.vectorize_pending()
    row = store.payloads[0]
    assert row["text"].startswith("配送与运费 › 运费标准\n小节0\n")
    assert row["category"] == "配送与运费 › 运费标准"
    assert row["section_path"] == "配送与运费 › 运费标准 › 小节0"
    assert row["content_type"] == "policy"
    assert row["is_key_clause"] is False


def test_resume_after_milvus_write_without_mysql_backfill(knowledge_db):
    ingestor, embedder, store = make_ingestor(knowledge_db)
    ingestor.ingest_chunks(chunks_of(2))
    with knowledge_db() as session:
        first = session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id)).first()
        first_id = first.id
    cluster = FakeVectorStore()
    cluster.upsert([{"vector_id": str(first_id), "embedding": embedder.encode([embedding_text("a", "b", "c")])[0],
                     "text": "a", "category": "a"}])
    ingestor.vector_store = cluster
    assert ingestor.stats()["pending"] == 2
    assert ingestor.vectorize_pending() == 2
    assert ingestor.stats()["pending"] == 0
    assert len(cluster.rows) == 2
    assert cluster.calls[0][0] == str(first_id)


def test_partial_batch_failure_keeps_rows_pending(knowledge_db):
    ingestor, _, store = make_ingestor(knowledge_db)
    ingestor.ingest_chunks(chunks_of(3))

    def fail(rows):
        raise RuntimeError("Milvus 挂了")

    store.upsert = fail
    with pytest.raises(RuntimeError):
        ingestor.vectorize_pending()
    assert ingestor.stats()["pending"] == 3


def test_documents_and_faq_are_ingested_with_types(knowledge_db, tmp_path):
    doc = tmp_path / "01-配送.md"
    doc.write_text("# 配送\n\n欢迎光临。\n\n## 运费\n\n满 99 元包邮。\n", encoding="utf-8")
    ingestor, _, _ = make_ingestor(knowledge_db)
    assert ingestor.ingest_documents(tmp_path) == {"01-配送.md": 2}
    assert ingestor.ingest_faq() == 8
    with knowledge_db() as session:
        rows = session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id)).all()
        assert {row.content_type for row in rows} == {"policy", "faq"}
        faq = next(row for row in rows if row.content_type == "faq")
        assert faq.section_path is None
        assert faq.questions == "退货政策是什么"


def test_rebuild_clears_rows_and_vectors(knowledge_db):
    ingestor, _, store = make_ingestor(knowledge_db)
    ingestor.ingest_chunks(chunks_of(2))
    ingestor.vectorize_pending()
    ingestor.rebuild()
    assert ingestor.stats() == {"total": 0, "pending": 0, "done": 0}
    assert store.rows == {}


def test_retrieval_filters_pending_and_low_scores(knowledge_db):
    ingestor, embedder, store = make_ingestor(knowledge_db)
    ingestor.ingest_chunks(chunks_of(3))
    ingestor.vectorize_pending()
    with knowledge_db() as session:
        rows = session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id)).all()
        done_a, done_b = rows[0], rows[1]
        rows[2].vectorize_status = "pending"
        rows[2].vector_id = None
        session.commit()
    store.dense = [(str(done_b.id), 0.92), (str(done_a.id), 0.55), (str(rows[2].id), 0.99)]
    retriever = KnowledgeRetriever(knowledge_db, embedder, store, candidates=5, top_k=2, strategy="dense")
    results = retriever.retrieve("运费怎么算", strategy="dense").items
    assert [item.id for item in results] == [done_b.id, done_a.id]
    assert results[0].question == done_b.questions


def test_retrieval_skips_blank_query(knowledge_db):
    ingestor, embedder, store = make_ingestor(knowledge_db)
    retriever = KnowledgeRetriever(knowledge_db, embedder, store, strategy="dense")
    result = retriever.retrieve("   ")
    assert result.low_confidence is True
    assert embedder.calls == []


def test_dedupe_normalizes_text_and_compares_vectors():
    assert dedupe.normalize("  退货，政策！") == "退货政策"
    assert dedupe.content_key("售后", "退货政策", "7 天可退") == dedupe.content_key("售后", "退货政策", "7天可退")
    assert dedupe.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert dedupe.is_semantic_duplicate([1.0, 0.0], [[0.99, 0.01]], 0.95) is True
    assert dedupe.is_semantic_duplicate([1.0, 0.0], [[0.0, 1.0]], 0.95) is False
