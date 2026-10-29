import threading
from typing import Protocol

from app.knowledge.embedding import DEFAULT_DIMENSION

FIELDS = ["vector_id", "embedding", "text", "bm25_sparse", "category", "content_type", "section_path", "is_key_clause"]


class CollectionSchemaMismatch(RuntimeError):
    pass


class VectorStore(Protocol):
    def ensure_collection(self) -> None: ...

    def upsert(self, rows: list[dict]) -> None: ...

    def dense_search(self, vector: list[float], limit: int, expr: str | None = None) -> list[tuple[str, float]]: ...

    def sparse_search(self, text: str, limit: int, expr: str | None = None) -> list[tuple[str, float]]: ...


class MilvusVectorStore:
    """Milvus 集合存向量、BM25 文本与过滤字段；文本与状态以 MySQL 为准。"""

    def __init__(self, uri: str, collection: str, *, dim: int = DEFAULT_DIMENSION):
        self.uri = uri
        self.collection = collection
        self.dim = dim
        self._client = None
        self._lock = threading.RLock()

    @property
    def client(self):
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                from pymilvus import MilvusClient

                self._client = MilvusClient(uri=self.uri)
            return self._client

    def ensure_collection(self) -> None:
        if self.client.has_collection(self.collection):
            self._validate_schema()
            self._load()
            return
        with self._lock:
            if self.client.has_collection(self.collection):
                self._validate_schema()
                self._load()
                return
            self._create_collection()
        self._load()

    def _create_collection(self) -> None:
        from pymilvus import DataType, Function, FunctionType

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("vector_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self.dim)
        schema.add_field(
            "text", DataType.VARCHAR, max_length=8192,
            enable_analyzer=True, analyzer_params={"type": "chinese"},
        )
        schema.add_field("bm25_sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("category", DataType.VARCHAR, max_length=255)
        schema.add_field("content_type", DataType.VARCHAR, max_length=32)
        schema.add_field("section_path", DataType.VARCHAR, max_length=512)
        schema.add_field("is_key_clause", DataType.BOOL)
        schema.add_function(Function(
            name="bm25_fn", function_type=FunctionType.BM25,
            input_field_names=["text"], output_field_names=["bm25_sparse"],
        ))
        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="embedding", index_type="HNSW", metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        index_params.add_index(
            field_name="bm25_sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25",
            params={"bm25_k1": 1.2, "bm25_b": 0.75},
        )
        self.client.create_collection(
            collection_name=self.collection, schema=schema, index_params=index_params
        )

    def _load(self) -> None:
        try:
            self.client.load_collection(self.collection)
        except Exception:
            pass

    def _validate_schema(self) -> None:
        description = self.client.describe_collection(self.collection)
        names = {field["name"] for field in description.get("fields", [])}
        functions = description.get("functions")
        # 部分 pymilvus 版本不回传 functions；有该键时校验函数名，没有时以字段存在性为准。
        has_bm25 = any(item.get("name") == "bm25_fn" for item in functions) if functions is not None else True
        if not set(FIELDS) <= names or not has_bm25:
            raise CollectionSchemaMismatch(
                "知识库集合结构是 ch03 版本，缺少 text/bm25_sparse/BM25 函数。"
                "请执行 python -m scripts.ingest_knowledge --rebuild --yes 重建集合（只影响知识库）。"
            )

    def upsert(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.ensure_collection()
        payload = [
            {
                "vector_id": str(row["vector_id"]),
                "embedding": [float(value) for value in row["embedding"]],
                "text": row["text"],
                "category": row["category"],
                "content_type": row.get("content_type") or "",
                "section_path": row.get("section_path") or "",
                "is_key_clause": bool(row.get("is_key_clause", False)),
            }
            for row in rows
        ]
        self.client.upsert(collection_name=self.collection, data=payload)

    def dense_search(self, vector: list[float], limit: int, expr: str | None = None) -> list[tuple[str, float]]:
        self.ensure_collection()
        result = self.client.search(
            collection_name=self.collection,
            data=[[float(value) for value in vector]],
            anns_field="embedding",
            limit=limit,
            filter=expr or "",
            output_fields=["vector_id"],
            consistency_level="Strong",
            search_params={"metric_type": "COSINE", "params": {"ef": 64}},
        )
        return self._pairs(result)

    def sparse_search(self, text: str, limit: int, expr: str | None = None) -> list[tuple[str, float]]:
        self.ensure_collection()
        result = self.client.search(
            collection_name=self.collection,
            data=[text],
            anns_field="bm25_sparse",
            limit=limit,
            filter=expr or "",
            output_fields=["vector_id"],
            consistency_level="Strong",
            search_params={"metric_type": "BM25"},
        )
        return self._pairs(result)

    def hybrid_search(
        self, vector: list[float], bm25_text: str, limit: int, rrf_k: int, expr: str | None = None
    ) -> list[tuple[str, float]]:
        from pymilvus import AnnSearchRequest, RRFRanker

        self.ensure_collection()
        dense = AnnSearchRequest(
            data=[[float(value) for value in vector]], anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"ef": 64}}, limit=limit, expr=expr,
        )
        sparse = AnnSearchRequest(
            data=[bm25_text], anns_field="bm25_sparse",
            param={"metric_type": "BM25"}, limit=limit, expr=expr,
        )
        result = self.client.hybrid_search(
            collection_name=self.collection, reqs=[dense, sparse], ranker=RRFRanker(k=rrf_k),
            limit=limit, output_fields=["vector_id"], consistency_level="Strong",
        )
        return self._pairs(result)

    def search(self, vector: list[float], limit: int) -> list[tuple[str, float]]:
        return self.dense_search(vector, limit)

    @staticmethod
    def _pairs(result) -> list[tuple[str, float]]:
        pairs: list[tuple[str, float]] = []
        for hit in (result[0] if result else []):
            entity = hit.get("entity") or {}
            vector_id = entity.get("vector_id") or hit.get("vector_id") or hit.get("id")
            if vector_id is not None:
                pairs.append((str(vector_id), float(hit.get("distance", 0.0))))
        return pairs

    def drop(self) -> None:
        if self.client.has_collection(self.collection):
            self.client.drop_collection(self.collection)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
