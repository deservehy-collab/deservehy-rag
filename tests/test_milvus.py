import pytest

from app.config import Settings
from app.knowledge.vector_store import MilvusVectorStore


def live_store_or_skip() -> MilvusVectorStore:
    try:
        settings = Settings.from_env()
    except Exception:
        pytest.skip("没有可用的 .env，跳过 Milvus 集成检查")
    store = MilvusVectorStore(settings.milvus_uri, "knowledge_test_smoke", dim=4)
    try:
        store.client.list_collections()
    except Exception:
        store.close()
        pytest.skip("Milvus 不可达，跳过集成检查")
    return store


def test_real_milvus_upsert_and_search():
    store = live_store_or_skip()
    try:
        store.drop()
        store.ensure_collection()
        store.upsert([1, 2], [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
        hits = store.search([0.9, 0.1, 0.0, 0.0], 2)
        assert [item[0] for item in hits] == ["1", "2"]
        assert hits[0][1] > hits[1][1]
    finally:
        store.drop()
        store.close()
