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
    engine = create_engine(f"sqlite:///{tmp_path / 'gate.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        for i in range(1, 4):
            session.add(KnowledgeChunk(category=f"类{i}", questions=f"问{i}", answer=f"答{i}",
                                       section_path=f"根 › 节{i}", content_type="policy",
                                       vectorize_status="done"))
        session.commit()
    return factory


HITS3 = (("2", 0.03), ("1", 0.02), ("3", 0.01))


def build(db, scores, *, strategy="hybrid_rerank", hits=(("1", 0.03),), top_n=5):
    store = FakeStore()
    store.hybrid = list(hits)
    reranker = FakeReranker(scores) if scores is not None else None
    return KnowledgeRetriever(db, FakeEmbedder(), store, candidates=3, top_k=3,
                              reranker=reranker, strategy=strategy,
                              confidence_min=0.35, confidence_effective_floor=0.20,
                              snapshot_top_n=top_n)


def test_low_confidence_reason_carries_confidence_and_signals(tmp_path):
    result = build(make_db(tmp_path), [0.1]).retrieve("邮费")       # 0.7*0.1+0.1*0.1=0.08 → 必拦
    assert result.low_confidence is True
    assert result.reason.startswith("confidence=0.0")
    assert "top1=0.100" in result.reason and "eff=0" in result.reason
    assert result.confidence is not None and result.confidence.value < 0.35


def test_high_confidence_result_is_not_blocked(tmp_path):
    result = build(make_db(tmp_path), [0.2, 0.9, 0.5], hits=HITS3).retrieve("邮费")   # 0.87 → 放行
    assert result.low_confidence is False and result.reason == ""
    assert result.confidence.value > 0.35
    assert result.confidence.signals["effective_count"] == 3


def test_snapshot_kept_even_when_blocked(tmp_path):
    """低置信时 items 仍在（retrieval.py:139-143 只清 evidence），快照必须能拿到内容。"""
    result = build(make_db(tmp_path), [0.1]).retrieve("邮费")
    assert result.low_confidence is True
    assert result.evidence == []
    assert result.snapshot and result.snapshot[0]["n"] == 1
    assert set(result.snapshot[0]) == {"n", "chunk_id", "question", "answer", "section_path", "scores"}


def test_snapshot_respects_top_n(tmp_path):
    result = build(make_db(tmp_path), [0.2, 0.9, 0.5], hits=HITS3, top_n=2).retrieve("邮费")
    assert len(result.snapshot) == 2


def test_no_hits_still_reports_empty_snapshot(tmp_path):
    result = build(make_db(tmp_path), [0.9], hits=()).retrieve("不存在的问题")
    assert result.low_confidence is True and result.reason == "no_hits"
    assert result.snapshot == [] and result.confidence is None


def test_non_rerank_strategy_is_never_blocked_by_confidence(tmp_path):
    result = build(make_db(tmp_path), None, strategy="hybrid").retrieve("邮费")   # 只有 rrf 分
    assert result.low_confidence is False
    assert result.confidence.reason == "no_rerank"


def test_retrieve_expanded_uses_the_same_gate(tmp_path):
    result = build(make_db(tmp_path), [0.1]).retrieve_expanded(["邮费", "运费"])
    assert result.low_confidence is True and result.reason.startswith("confidence=")
