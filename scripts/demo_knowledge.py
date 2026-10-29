import argparse

from sqlalchemy import func, select

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.retrieval import KnowledgeRetriever
from app.knowledge.vector_store import MilvusVectorStore
from app.models import KnowledgeChunk

QUESTIONS = ("邮费是多少？", "退货要多久到账？")


def main() -> int:
    parser = argparse.ArgumentParser(description="验收知识库检索：邮费问题应召回运费内容。")
    parser.add_argument("--question", action="append", help="自定义问题，可重复")
    args = parser.parse_args()
    questions = args.question or list(QUESTIONS)

    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    factory = build_session_factory(engine)
    embedder = BgeM3Embedder(
        settings.embedding_model,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    store = MilvusVectorStore(settings.milvus_uri, settings.milvus_collection)
    retriever = KnowledgeRetriever(
        factory,
        embedder,
        store,
        candidates=settings.knowledge_candidates,
        top_k=settings.knowledge_top_k,
        min_score=settings.knowledge_min_score,
    )
    try:
        with factory() as session:
            total = session.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(KnowledgeChunk)
                    .where(KnowledgeChunk.vectorize_status == "pending")
                )
                or 0
            )
        print(f"知识库 {total} 段，待向量化 {pending} 段。")
        shipping_hit = False
        for question in questions:
            items = retriever.search(question)
            print(f"\n问：{question}")
            if not items:
                print("  未召回任何内容")
                continue
            for rank, item in enumerate(items, 1):
                answer = item["answer"].replace("\n", " ")
                print(f"  {rank}. [{item['category']}] {item['question']} → {answer[:80]}")
            if question == QUESTIONS[0]:
                shipping_hit = any(
                    word in item["question"] + item["answer"]
                    for item in items
                    for word in ("邮费", "运费")
                )
        if shipping_hit:
            print("\n验收通过：邮费问题召回了运费相关内容。")
            return 0
        print("\n验收未通过：邮费问题没有召回运费内容，请确认知识库已导入并向量化。")
        return 1
    finally:
        store.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
