from pathlib import Path

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.knowledge.dedupe import content_key
from app.knowledge.embedding import Embedder
from app.knowledge.splitter import Chunk, split_markdown
from app.knowledge.vector_store import VectorStore
from app.models import Faq, KnowledgeChunk

MINED_CATEGORY = "会话问答"


def embedding_text(category: str, questions: str, answer: str) -> str:
    return f"{category}\n{questions}\n{answer}"


class KnowledgeIngestor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedder: Embedder,
        vector_store: VectorStore,
        *,
        batch_size: int = 16,
    ):
        self.session_factory = session_factory
        self.embedder = embedder
        self.vector_store = vector_store
        self.batch_size = batch_size
        self.vector_store.ensure_collection()

    def ingest_chunks(self, chunks: list[Chunk], *, link_sequential: bool = True) -> int:
        if not chunks:
            return 0
        with self.session_factory() as session:
            existing = {
                content_key(row.category, row.questions, row.answer)
                for row in session.scalars(select(KnowledgeChunk))
            }
            created: list[KnowledgeChunk] = []
            for chunk in chunks:
                key = content_key(chunk.category, chunk.questions, chunk.answer)
                if key in existing:
                    continue
                existing.add(key)
                row = KnowledgeChunk(
                    category=chunk.category,
                    questions=chunk.questions,
                    answer=chunk.answer,
                    section_path=chunk.section_path,
                    content_type=chunk.content_type,
                    is_key_clause=chunk.is_key_clause,
                    vectorize_status="pending",
                )
                session.add(row)
                created.append(row)
            session.flush()
            if link_sequential:
                for previous, current in zip(created, created[1:]):
                    previous.next_chunk_id = current.id
                    current.prev_chunk_id = previous.id
            session.commit()
            return len(created)

    def ingest_documents(self, directory: str | Path) -> dict[str, int]:
        results: dict[str, int] = {}
        for path in sorted(Path(directory).glob("*.md")):
            content_type = "manual" if path.name[:2] == "03" else "policy"
            chunks = split_markdown(path.read_text(encoding="utf-8"), content_type=content_type)
            results[path.name] = self.ingest_chunks(chunks)
        return results

    def ingest_faq(self) -> int:
        with self.session_factory() as session:
            rows = session.scalars(select(Faq).order_by(Faq.id)).all()
            chunks = [
                Chunk(
                    category=row.category,
                    questions=row.question,
                    answer=row.answer,
                    section_path=None,
                    content_type="faq",
                    is_key_clause=False,
                    order=index,
                )
                for index, row in enumerate(rows)
            ]
        return self.ingest_chunks(chunks, link_sequential=False)

    def ingest_pairs(self, pairs: list[dict], *, source_ref: str | None = None) -> int:
        chunks = [
            Chunk(
                category=MINED_CATEGORY,
                questions=str(pair["question"]).strip(),
                answer=str(pair["answer"]).strip(),
                section_path=source_ref,
                content_type="mined_qa",
                is_key_clause=False,
                order=index,
            )
            for index, pair in enumerate(pairs)
            if str(pair.get("question", "")).strip() and str(pair.get("answer", "")).strip()
        ]
        return self.ingest_chunks(chunks, link_sequential=False)

    def vectorize_pending(self) -> int:
        total = 0
        while True:
            with self.session_factory() as session:
                rows = session.execute(
                    select(
                        KnowledgeChunk.id,
                        KnowledgeChunk.category,
                        KnowledgeChunk.questions,
                        KnowledgeChunk.answer,
                    )
                    .where(or_(KnowledgeChunk.vectorize_status == "pending", KnowledgeChunk.vector_id.is_(None)))
                    .order_by(KnowledgeChunk.id)
                    .limit(self.batch_size)
                ).all()
            if not rows:
                break
            texts = [embedding_text(row.category, row.questions, row.answer) for row in rows]
            vectors = self.embedder.encode(texts)
            ids = [row.id for row in rows]
            self.vector_store.upsert(ids, vectors)
            with self.session_factory() as session:
                for item_id in ids:
                    session.execute(
                        update(KnowledgeChunk)
                        .where(KnowledgeChunk.id == item_id)
                        .values(vector_id=str(item_id), vectorize_status="done")
                    )
                session.commit()
            total += len(ids)
            if len(ids) < self.batch_size:
                break
        return total

    def rebuild(self) -> None:
        with self.session_factory() as session:
            session.execute(
                update(KnowledgeChunk).values(prev_chunk_id=None, next_chunk_id=None)
            )
            session.execute(delete(KnowledgeChunk))
            session.commit()
        self.vector_store.drop()
        self.vector_store.ensure_collection()

    def stats(self) -> dict[str, int]:
        with self.session_factory() as session:
            total = session.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(KnowledgeChunk)
                    .where(or_(KnowledgeChunk.vectorize_status == "pending", KnowledgeChunk.vector_id.is_(None)))
                )
                or 0
            )
        return {"total": total, "pending": pending, "done": total - pending}
