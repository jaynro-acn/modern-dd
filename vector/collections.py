import uuid
from pathlib import Path


class QdrantManager:
    def __init__(self, collection_name: str, storage_path: str = "data/qdrant"):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        self.collection_name = collection_name
        self._client = QdrantClient(path=str(Path(storage_path)))
        self._Distance = Distance
        self._VectorParams = VectorParams

    def ensure_collection(self, vector_size: int = 384):
        existing = [c.name for c in self._client.get_collections().collections]
        if self.collection_name not in existing:
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=self._VectorParams(size=vector_size, distance=self._Distance.COSINE),
            )

    def upsert(self, entity_id: str, vector: list, payload: dict):
        from qdrant_client.models import PointStruct

        # Use a deterministic integer id derived from the UUID string
        point_id = str(uuid.UUID(entity_id)) if self._is_uuid(entity_id) else entity_id
        self._client.upsert(
            collection_name=self.collection_name,
            points=[PointStruct(id=entity_id, vector=vector, payload=payload)],
        )

    def search(self, query_vector: list, top_k: int = 5, filter_project: str = None) -> list:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = None
        if filter_project:
            query_filter = Filter(
                must=[FieldCondition(key="project", match=MatchValue(value=filter_project))]
            )

        # Support both old search() and new query_points() API shapes
        try:
            results = self._client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                limit=top_k,
                query_filter=query_filter,
            ).points
        except AttributeError:
            results = self._client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                limit=top_k,
                query_filter=query_filter,
            )

        return [{"id": str(r.id), "score": r.score, "payload": r.payload} for r in results]

    def count(self) -> int:
        return self._client.get_collection(self.collection_name).points_count

    @staticmethod
    def _is_uuid(value: str) -> bool:
        try:
            uuid.UUID(str(value))
            return True
        except ValueError:
            return False
