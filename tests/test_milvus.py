import os

import pytest

from app.config import Settings
from app.knowledge.vector_store import MilvusVectorStore


@pytest.fixture(autouse=True)
def restore_environ():
    # pymilvus 首次连接时会 load_dotenv()，把 .env 灌进 os.environ，污染同进程后续用例
    # （例如 Settings(_env_file=None) 会读到本机窗口配置）。用完还原。
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


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
        store.upsert([
            {"vector_id": "1", "embedding": [1.0, 0.0, 0.0, 0.0], "text": "普通地区邮费",
             "category": "配送与运费", "content_type": "policy", "section_path": "配送与运费 › 运费标准 › 普通地区邮费", "is_key_clause": False},
            {"vector_id": "2", "embedding": [0.0, 1.0, 0.0, 0.0], "text": "七天无理由退货",
             "category": "退换货与退款", "content_type": "policy", "section_path": "退换货与退款 › 七天无理由退货", "is_key_clause": False},
        ])
        hits = store.search([0.9, 0.1, 0.0, 0.0], 2)
        assert [item[0] for item in hits] == ["1", "2"]
        assert hits[0][1] > hits[1][1]
    finally:
        store.drop()
        store.close()


def test_real_milvus_bm25_hybrid_and_category_filter():
    store = live_store_or_skip()
    try:
        store.drop()
        store.ensure_collection()
        store.upsert([
            {"vector_id": "1", "embedding": [1.0, 0.0, 0.0, 0.0], "text": "保温杯 G500 杯盖 适配 P1001",
             "category": "型号与兼容性", "content_type": "policy", "section_path": "型号与兼容性 › 保温杯系列 › 杯盖与配件适配", "is_key_clause": False},
            {"vector_id": "2", "embedding": [0.0, 1.0, 0.0, 0.0], "text": "普通地区订单满 99 元免运费",
             "category": "配送与运费", "content_type": "policy", "section_path": "配送与运费 › 运费标准 › 普通地区邮费", "is_key_clause": False},
        ])
        bm25 = store.sparse_search("G500 杯盖", 5)
        assert bm25 and bm25[0][0] == "1"
        hybrid = store.hybrid_search([0.9, 0.1, 0.0, 0.0], "G500 杯盖", 5, 60)
        assert [item[0] for item in hybrid][:1] == ["1"]
        filtered = store.dense_search([0.0, 1.0, 0.0, 0.0], 5, expr='category == "配送与运费"')
        assert [item[0] for item in filtered] == ["2"]
    finally:
        store.drop()
        store.close()
