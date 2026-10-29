from sqlalchemy import create_engine

from app.database import build_session_factory
from app.knowledge.retrieval import KnowledgeRetriever
from app.models import Base, KnowledgeChunk


class FakeEmbedder:
    def encode(self, texts):
        return [[1.0, 0.0] for _ in texts]


class FakeStore:
    def __init__(self):
        self.dense = []
        self.sparse = []
        self.hybrid = []

    def dense_search(self, vector, limit, expr=None):
        return self.dense[:limit]

    def sparse_search(self, text, limit, expr=None):
        return self.sparse[:limit]

    def hybrid_search(self, vector, text, limit, rrf_k, expr=None):
        return self.hybrid[:limit]


class FakeReranker:
    def __init__(self, scores):
        self.scores = scores

    def score(self, query, passages):
        return self.scores[:len(passages)]


def make_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        for i in range(1, 4):
            session.add(KnowledgeChunk(category=f"类{i}", questions=f"问{i}", answer=f"答{i}",
                                       section_path=f"根 › 节{i}", content_type="policy", vectorize_status="done"))
        session.commit()
    return factory


def test_hybrid_rerank_orders_by_rerank_and_numbers_sandwich(tmp_path):
    store = FakeStore()
    store.hybrid = [("2", 0.03), ("1", 0.02), ("3", 0.01)]
    retriever = KnowledgeRetriever(make_db(tmp_path), FakeEmbedder(), store, candidates=3, top_k=3,
                                   reranker=FakeReranker([0.2, 0.9, 0.5]), rerank_min_score=0.3)
    result = retriever.retrieve("问题", canonical="规范问题")
    assert result.low_confidence is False
    assert [item.id for item in result.items] == [1, 3, 2]
    assert [item.id for item in result.evidence] == [1, 2, 3]
    assert [item.n for item in result.evidence] == [1, 2, 3]


def test_gate_refuses_when_top1_below_threshold(tmp_path):
    store = FakeStore()
    store.hybrid = [("1", 0.03)]
    retriever = KnowledgeRetriever(make_db(tmp_path), FakeEmbedder(), store, candidates=3, top_k=3,
                                   reranker=FakeReranker([0.1]), rerank_min_score=0.3)
    result = retriever.retrieve("问题", canonical="规范")
    assert result.low_confidence is True
    assert result.reason.startswith("top1=")


def test_dense_and_bm25_strategies_use_their_own_path(tmp_path):
    store = FakeStore()
    store.dense = [("1", 0.8)]
    store.sparse = [("2", 12.0)]
    retriever = KnowledgeRetriever(make_db(tmp_path), FakeEmbedder(), store, candidates=3, top_k=3, strategy="dense")
    assert retriever.retrieve("q", strategy="dense", canonical="q").items[0].id == 1
    bm25 = retriever.retrieve("q", strategy="bm25", canonical="q")
    assert bm25.items[0].id == 2 and bm25.items[0].scores["bm25"] == 12.0


def test_category_filter_is_checked_after_fetch(tmp_path):
    store = FakeStore()
    store.dense = [("1", 0.8), ("2", 0.7)]
    retriever = KnowledgeRetriever(make_db(tmp_path), FakeEmbedder(), store, candidates=3, top_k=3, strategy="dense")
    result = retriever.retrieve("q", strategy="dense", canonical="q", category="类2")
    assert [item.id for item in result.items] == [2]


def test_blank_query_does_not_call_models(tmp_path):
    retriever = KnowledgeRetriever(make_db(tmp_path), FakeEmbedder(), FakeStore(), strategy="dense")
    result = retriever.retrieve("   ")
    assert result.low_confidence is True and result.items == []
