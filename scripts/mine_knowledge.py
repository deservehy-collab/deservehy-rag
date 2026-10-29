import argparse

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.ingest import KnowledgeIngestor
from app.knowledge.mining import ConversationMiner
from app.knowledge.vector_store import MilvusVectorStore
from app.model import build_model


def main() -> int:
    parser = argparse.ArgumentParser(description="从历史会话里挖掘可复用问答，去重后写入知识库。")
    parser.add_argument("--limit", type=int, default=None, help="本次最多处理多少个会话")
    args = parser.parse_args()

    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    factory = build_session_factory(engine)
    embedder = BgeM3Embedder(
        settings.embedding_model,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    store = MilvusVectorStore(settings.milvus_uri, settings.milvus_collection)
    model = build_model(settings)
    try:
        ingestor = KnowledgeIngestor(
            factory, embedder, store, batch_size=settings.embedding_batch_size
        )
        miner = ConversationMiner(
            factory,
            model,
            ingestor,
            embedder,
            lookback_days=settings.knowledge_mining_lookback_days,
            dedupe_threshold=settings.knowledge_dedupe_threshold,
        )
        result = miner.mine(limit=args.limit)
        print(
            f"扫描会话 {result['conversations']} 个，抽取问答 {result['extracted']} 条，"
            f"保留 {result['kept']} 条，去重丢弃 {result['discarded']} 条，"
            f"入库 {result['ingested']} 条，补向量 {result['vectorized']} 条。"
        )
        return 0
    finally:
        store.close()
        engine.dispose()
        model.root_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
