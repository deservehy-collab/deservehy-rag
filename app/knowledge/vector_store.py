import threading
from typing import Protocol

from app.knowledge.embedding import DEFAULT_DIMENSION


class VectorStore(Protocol):
    def ensure_collection(self) -> None: ...

    def upsert(self, ids: list[int], vectors: list[list[float]]) -> None: ...

    def search(self, vector: list[float], limit: int) -> list[tuple[str, float]]: ...


class MilvusVectorStore:
    """Milvus 集合只存主键与向量；文本和状态以 MySQL 为准。"""

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
            return
        with self._lock:
            if self.client.has_collection(self.collection):
                return
            from pymilvus import DataType

            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("vector_id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self.dim)
            index_params = self.client.prepare_index_params()
            index_params.add_index(
                field_name="embedding",
                index_type="HNSW",
                metric_type="COSINE",
                params={"M": 16, "efConstruction": 200},
            )
            self.client.create_collection(
                collection_name=self.collection, schema=schema, index_params=index_params
            )
        try:
            self.client.load_collection(self.collection)
        except Exception:
            pass

    def upsert(self, ids: list[int], vectors: list[list[float]]) -> None:
        if not ids:
            return
        self.ensure_collection()
        rows = [
            {"vector_id": str(item_id), "embedding": [float(value) for value in vector]}
            for item_id, vector in zip(ids, vectors)
        ]
        self.client.upsert(collection_name=self.collection, data=rows)

    def search(self, vector: list[float], limit: int) -> list[tuple[str, float]]:
        self.ensure_collection()
        result = self.client.search(
            collection_name=self.collection,
            data=[[float(value) for value in vector]],
            limit=limit,
            output_fields=["vector_id"],
            consistency_level="Strong",
            search_params={"metric_type": "COSINE", "params": {"ef": 64}},
        )
        hits = result[0] if result else []
        pairs: list[tuple[str, float]] = []
        for hit in hits:
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
