import argparse

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.ingest import KnowledgeIngestor
from app.knowledge.vector_store import MilvusVectorStore


def main() -> int:
    parser = argparse.ArgumentParser(description="把示例文档与 FAQ 写入 MySQL，并补齐 Milvus 向量。可重复执行。")
    parser.add_argument("--no-faq", action="store_true", help="跳过 FAQ 表，只导文档")
    parser.add_argument("--vectorize-only", action="store_true", help="不导新内容，只补 pending 向量")
    parser.add_argument("--rebuild", action="store_true", help="清空 knowledge_chunks 与 Milvus 集合后重建")
    parser.add_argument("--yes", action="store_true", help="与 --rebuild 一起使用，确认清空")
    args = parser.parse_args()
    if args.rebuild and not args.yes:
        print("--rebuild 会清空知识库，确认后请加 --yes。")
        return 2

    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    factory = build_session_factory(engine)
    embedder = BgeM3Embedder(
        settings.embedding_model,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    store = MilvusVectorStore(settings.milvus_uri, settings.milvus_collection)
    try:
        if args.rebuild:
            store.drop()
        ingestor = KnowledgeIngestor(
            factory, embedder, store, batch_size=settings.embedding_batch_size
        )
        if args.rebuild:
            ingestor.rebuild()
            print("已清空 knowledge_chunks 与 Milvus 集合。")
        if not args.vectorize_only:
            for name, count in ingestor.ingest_documents(settings.knowledge_doc_dir).items():
                print(f"文档 {name}：新增 {count} 段")
            if not args.no_faq:
                print(f"FAQ：新增 {ingestor.ingest_faq()} 条")
        written = ingestor.vectorize_pending()
        stats = ingestor.stats()
        print(
            f"本轮写入向量 {written} 条；知识库共 {stats['total']} 段，"
            f"已向量化 {stats['done']} 段，待处理 {stats['pending']} 段。"
        )
        return 0 if stats["pending"] == 0 else 1
    finally:
        store.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
