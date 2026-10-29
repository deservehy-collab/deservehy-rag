import argparse

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.rerank import BgeReranker
from app.knowledge.retrieval import KnowledgeRetriever, STRATEGIES
from app.knowledge.rewrite import QueryRewriter
from app.knowledge.vector_store import MilvusVectorStore
from app.model import build_model

DEFAULT_QUESTION = "G500 杯盖能装在 B750 上吗？"
EXPECTED_SECTION = "杯盖与配件适配"


def score_of(item, strategy):
    if strategy == "hybrid_rerank":
        return item.scores.get("rerank", item.scores.get("rrf", 0.0))
    if strategy == "hybrid":
        return item.scores.get("rrf", 0.0)
    return item.scores.get(strategy, 0.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="打印改写结果与四策略 Top-5，验收 BM25 型号命中。")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--no-rewrite", action="store_true", help="跳过 LLM 改写，直接用原话检索")
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
    reranker = BgeReranker(
        settings.knowledge_rerank_model,
        device=settings.knowledge_rerank_device,
        batch_size=settings.knowledge_rerank_batch_size,
    )
    model = None
    rewrite = None
    if not args.no_rewrite:
        model = build_model(settings)
        rewrite = QueryRewriter(model)
    retriever = KnowledgeRetriever(
        factory,
        embedder,
        store,
        candidates=settings.knowledge_candidates,
        top_k=settings.knowledge_top_k,
        rrf_k=settings.knowledge_rrf_k,
        rewrite=rewrite,
        reranker=reranker,
    )
    try:
        canonical = retriever.rewrite(args.question)
        print(f"问：{args.question}")
        print(f"改写：{canonical}")
        bm25_hit = False
        for strategy in STRATEGIES:
            result = retriever.retrieve(args.question, strategy=strategy, canonical=canonical)
            print(f"\n[{strategy}]")
            if not result.items:
                print("  未召回任何内容" + (f"（{result.reason}）" if result.reason else ""))
                continue
            for rank, item in enumerate(result.items[:5], 1):
                print(f"  {rank}. {score_of(item, strategy):.4f} {item.section_path or '（无章节）'}")
            if strategy == "bm25":
                bm25_hit = any(
                    EXPECTED_SECTION in (item.section_path or "") for item in result.items[:5]
                )
        if bm25_hit:
            print("\n验收通过：BM25 单路命中型号条款。")
            return 0
        print(f"\n验收未通过：BM25 Top-5 没有命中「{EXPECTED_SECTION}」，请确认 04 文档已导入。")
        return 1
    finally:
        store.close()
        if model is not None:
            model.root_client.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
