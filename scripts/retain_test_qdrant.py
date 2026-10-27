"""pytest插件：本次验收保留测试集合，禁止调用任何Qdrant删除操作。"""

from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.local.qdrant_local import QdrantLocal


def pytest_sessionstart(session):
    original = QdrantClient.delete_collection
    def retain(client, collection_name, *args, **kwargs):
        if isinstance(client._client, QdrantLocal):
            return original(client, collection_name, *args, **kwargs)
        if not collection_name.startswith(("pc_test_d4_", "pc_test_d24_")):
            raise RuntimeError("本次验收不允许删除Qdrant集合")
        path = Path("data/auto-d24-final/retained-collections.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(collection_name + "\n")
        return True

    QdrantClient.delete_collection = retain
