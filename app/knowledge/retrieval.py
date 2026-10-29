from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.knowledge.embedding import Embedder
from app.knowledge.quality import number_evidence
from app.knowledge.synonyms import expand_for_bm25
from app.knowledge.vector_store import VectorStore
from app.models import KnowledgeChunk

STRATEGIES = ("dense", "bm25", "hybrid", "hybrid_rerank")


@dataclass
class Evidence:
    id: int
    question: str
    answer: str
    category: str
    section_path: str | None
    content_type: str | None
    scores: dict = field(default_factory=dict)
    n: int = 0

    def rerank_text(self) -> str:
        return f"{self.section_path or ''}\n{self.question}\n{self.answer}"

    def item(self) -> dict:
        return {"n": self.n, "id": self.id, "question": self.question, "answer": self.answer,
                "category": self.category, "section_path": self.section_path,
                "content_type": self.content_type, "scores": self.scores}

    def citation(self) -> dict:
        return {"n": self.n, "chunk_id": self.id, "question": self.question, "answer": self.answer,
                "category": self.category, "section_path": self.section_path}


@dataclass
class RetrievalResult:
    strategy: str
    canonical_query: str
    items: list[Evidence] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    low_confidence: bool = False
    reason: str = ""


class KnowledgeRetriever:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedder: Embedder,
        vector_store: VectorStore,
        *,
        candidates: int = 50,
        top_k: int = 10,
        rrf_k: int = 60,
        rewrite=None,
        reranker=None,
        synonyms: bool = True,
        strategy: str = "hybrid_rerank",
        rerank_min_score: float = 0.30,
    ):
        if strategy not in STRATEGIES:
            raise ValueError(f"未知检索策略：{strategy}")
        self.session_factory = session_factory
        self.embedder = embedder
        self.vector_store = vector_store
        self.candidates = candidates
        self.top_k = top_k
        self.rrf_k = rrf_k
        self.rewrite_service = rewrite
        self.reranker = reranker
        self.synonyms = synonyms
        self.strategy = strategy
        self.rerank_min_score = rerank_min_score
        if self.strategy == "hybrid_rerank" and self.reranker is None:
            raise ValueError("hybrid_rerank 需要重排模型。")

    def _rows(self, ids):
        numeric = [int(item_id) for item_id in ids if str(item_id).isdigit()]
        if not numeric:
            return {}
        with self.session_factory() as session:
            rows = session.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.id.in_(numeric), KnowledgeChunk.vectorize_status == "done"
                )
            ).all()
        return {str(row.id): row for row in rows}

    @staticmethod
    def _evidence(row, scores):
        return Evidence(
            id=row.id, question=row.questions, answer=row.answer, category=row.category,
            section_path=row.section_path, content_type=row.content_type, scores=dict(scores),
        )

    def rewrite(self, query: str) -> str:
        return self.rewrite_service.rewrite(query) if self.rewrite_service is not None else query.strip()

    def retrieve(self, query, *, category=None, strategy=None, canonical=None):
        raw = query.strip()
        strategy = strategy or self.strategy
        if not raw:
            return RetrievalResult(strategy=strategy, canonical_query="", low_confidence=True, reason="empty_query")
        canonical = (canonical or self.rewrite(raw)).strip()
        expr = _category_expr(category)
        if strategy == "dense":
            hits = self.vector_store.dense_search(self.embedder.encode([canonical])[0], self.candidates, expr)
            scores = {item_id: {"dense": score} for item_id, score in hits}
        elif strategy == "bm25":
            hits = self.vector_store.sparse_search(
                expand_for_bm25(canonical) if self.synonyms else canonical, self.candidates, expr)
            scores = {item_id: {"bm25": score} for item_id, score in hits}
        else:
            sparse_query = expand_for_bm25(canonical) if self.synonyms else canonical
            hits = self.vector_store.hybrid_search(
                self.embedder.encode([canonical])[0], sparse_query, self.candidates, self.rrf_k, expr)
            scores = {item_id: {"rrf": score} for item_id, score in hits}
        rows = self._rows([item_id for item_id, _ in hits])
        ordered = [(rows[item_id], scores[item_id]) for item_id, _ in hits
                   if item_id in rows and (not category or rows[item_id].category == category)]
        if not ordered:
            return RetrievalResult(strategy=strategy, canonical_query=canonical, low_confidence=True, reason="no_hits")
        items = [self._evidence(row, score) for row, score in ordered]
        low, reason = False, ""
        if strategy == "hybrid_rerank":
            ranked_scores = self.reranker.score(canonical, [item.rerank_text() for item in items])
            for item, value in zip(items, ranked_scores):
                item.scores["rerank"] = value
            items.sort(key=lambda item: item.scores["rerank"], reverse=True)
            top = items[0].scores["rerank"]
            if top < self.rerank_min_score:
                low, reason = True, f"top1={top:.3f}<{self.rerank_min_score:.2f}"
        items = items[:self.top_k]
        evidence: list[Evidence] = []
        if not low:
            for numbered in number_evidence([item.item() for item in items]):
                evidence.append(Evidence(**numbered))
        return RetrievalResult(strategy=strategy, canonical_query=canonical, items=items,
                               evidence=evidence, low_confidence=low, reason=reason)

    def search(self, query: str, limit: int | None = None) -> list[dict]:
        result = self.retrieve(query)
        if result.low_confidence:
            return []
        return [
            {"id": item.id, "question": item.question, "answer": item.answer, "category": item.category}
            for item in result.evidence[:limit or self.top_k]
        ]


def _category_expr(category: str | None) -> str | None:
    if not category:
        return None
    escaped = category.replace("\\", "\\\\").replace('"', '\\"')
    return f'category == "{escaped}"'
