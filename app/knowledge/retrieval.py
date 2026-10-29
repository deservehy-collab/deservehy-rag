from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.knowledge.embedding import Embedder
from app.knowledge.vector_store import VectorStore
from app.models import KnowledgeChunk


class KnowledgeRetriever:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedder: Embedder,
        vector_store: VectorStore,
        *,
        candidates: int = 12,
        top_k: int = 5,
        min_score: float = 0.35,
    ):
        self.session_factory = session_factory
        self.embedder = embedder
        self.vector_store = vector_store
        self.candidates = candidates
        self.top_k = top_k
        self.min_score = min_score

    def search(self, query: str, limit: int | None = None) -> list[dict]:
        text = query.strip()
        if not text:
            return []
        vector = self.embedder.encode([text])[0]
        hits = self.vector_store.search(vector, self.candidates)
        if not hits:
            return []
        ids = [int(item_id) for item_id, _ in hits if str(item_id).isdigit()]
        if not ids:
            return []
        with self.session_factory() as session:
            rows = session.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.id.in_(ids), KnowledgeChunk.vectorize_status == "done"
                )
            ).all()
        mapping = {row.id: row for row in rows}
        results: list[dict] = []
        wanted = limit or self.top_k
        for item_id, score in hits:
            if score < self.min_score:
                continue
            row = mapping.get(int(item_id)) if str(item_id).isdigit() else None
            if row is None:
                continue
            results.append(
                {"id": row.id, "question": row.questions, "answer": row.answer, "category": row.category}
            )
            if len(results) >= wanted:
                break
        return results
