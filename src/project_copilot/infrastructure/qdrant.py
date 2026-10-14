"""官方Qdrant SDK的Dense适配：显式集合契约、过滤与脱敏错误。"""

import re
from collections.abc import Sequence
from math import isfinite
from typing import Self
from uuid import UUID

import httpx
from pydantic import ValidationError
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from project_copilot.config import Settings
from project_copilot.schemas.documents import DocumentChunk
from project_copilot.schemas.retrieval import RetrievedChunk


class QdrantConfigError(ValueError):
    """输入或集合schema不符合当前Dense索引契约。"""


class QdrantRequestError(RuntimeError):
    """连接、HTTP或服务请求失败；不携带服务响应及连接凭据。"""


class QdrantResponseError(RuntimeError):
    """服务返回成功，但操作结果或片段不满足项目契约。"""


_REQUEST_ERRORS = (ResponseHandlingException, UnexpectedResponse, httpx.HTTPError)


class DenseQdrantStore:
    """仅使用一个命名Dense向量；调用者通过with/close关闭SDK客户端。"""

    def __init__(
        self,
        client: QdrantClient,
        *,
        collection_name: str,
        dimensions: int,
        vector_name: str = "dense",
    ) -> None:
        if not isinstance(collection_name, str) or not re.fullmatch(
            r"pc_[A-Za-z0-9_-]{1,117}", collection_name
        ):
            raise QdrantConfigError("Dense集合名称必须使用新项目的pc_前缀")
        if type(dimensions) is not int or dimensions <= 0:
            raise QdrantConfigError("Dense向量维度必须是正整数")
        if not isinstance(vector_name, str) or not vector_name.strip():
            raise QdrantConfigError("Dense向量字段名不能为空")
        self._client = client
        self.collection_name = collection_name
        self.dimensions = dimensions
        self.vector_name = vector_name

    def ensure_collection(self) -> None:
        """只创建缺失集合；已有不匹配集合明确拒绝，不重建或删除数据。"""
        try:
            exists = self._client.collection_exists(self.collection_name)
            if type(exists) is not bool:
                raise QdrantResponseError("Qdrant集合存在性响应无效")
            if not exists:
                created = self._client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config={
                        self.vector_name: models.VectorParams(
                            size=self.dimensions, distance=models.Distance.COSINE
                        )
                    },
                )
                if created is not True:
                    raise QdrantResponseError("Qdrant未确认集合创建成功")
            info = self._client.get_collection(self.collection_name)
        except _REQUEST_ERRORS:
            raise QdrantRequestError("无法读取或创建Qdrant Dense集合") from None
        except ValidationError:
            raise QdrantResponseError("Qdrant集合响应无法解析") from None

        try:
            params = info.config.params
            vectors = params.vectors
            sparse = params.sparse_vectors
        except AttributeError:
            raise QdrantResponseError("Qdrant集合响应缺少schema") from None
        if not isinstance(vectors, dict) or set(vectors) != {self.vector_name}:
            raise QdrantConfigError("Qdrant集合的命名Dense向量字段不匹配")
        vector_params = vectors[self.vector_name]
        if (
            not isinstance(vector_params, models.VectorParams)
            or vector_params.size != self.dimensions
            or vector_params.distance != models.Distance.COSINE
            or vector_params.multivector_config is not None
        ):
            raise QdrantConfigError("Qdrant集合的向量维度或距离不匹配")
        if sparse:
            raise QdrantConfigError("当前集合包含稀疏向量，不能用作Dense-only基线")

    def upsert(
        self,
        chunks: Sequence[DocumentChunk],
        vectors: Sequence[list[float]],
        *,
        dense_index_version: str,
    ) -> None:
        """全部校验后再写入；wait=True且只有completed才确认成功。"""
        self._validate_version(dense_index_version)
        if (
            not isinstance(chunks, Sequence)
            or isinstance(chunks, (str, bytes))
            or not isinstance(vectors, Sequence)
            or isinstance(vectors, (str, bytes))
            or len(chunks) != len(vectors)
        ):
            raise QdrantConfigError("入库片段与向量必须是条数相同的序列")
        if any(not isinstance(chunk, DocumentChunk) for chunk in chunks):
            raise QdrantConfigError("入库片段不符合文档契约")
        chunk_ids = [chunk.metadata.chunk_id for chunk in chunks]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise QdrantConfigError("同批入库包含重复片段标识")

        points = [
            models.PointStruct(
                id=str(chunk.metadata.chunk_id),
                vector={self.vector_name: self._validate_vector(vector)},
                payload={
                    "text": chunk.text,
                    "metadata": chunk.metadata.model_dump(mode="json"),
                    # D3的metadata.index_version仍然保留为切分指纹。
                    "dense_index_version": dense_index_version,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        if not points:
            return
        try:
            result = self._client.upsert(
                collection_name=self.collection_name, points=points, wait=True
            )
        except _REQUEST_ERRORS:
            raise QdrantRequestError("Qdrant Dense片段写入失败") from None
        except ValidationError:
            raise QdrantResponseError("Qdrant写入响应无法解析") from None
        if getattr(result, "status", None) != models.UpdateStatus.COMPLETED:
            raise QdrantResponseError("Qdrant尚未确认片段写入完成")

    def query(
        self,
        vector: list[float],
        *,
        document_ids: Sequence[UUID],
        dense_index_version: str,
        limit: int = 5,
    ) -> list[RetrievedChunk]:
        """仅检索成功清单中的文档和当前Dense版本，不暴露全库检索入口。"""
        query_vector = self._validate_vector(vector)
        self._validate_version(dense_index_version)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise QdrantConfigError("Dense检索数量必须在1到100之间")
        if (
            not isinstance(document_ids, Sequence)
            or isinstance(document_ids, (str, bytes))
            or any(not isinstance(document_id, UUID) for document_id in document_ids)
        ):
            raise QdrantConfigError("Dense检索范围必须是文档UUID序列")
        allowed_ids = set(document_ids)
        if not allowed_ids:
            return []
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="metadata.document_id",
                    match=models.MatchAny(any=[str(value) for value in dict.fromkeys(document_ids)]),
                ),
                models.FieldCondition(
                    key="dense_index_version", match=models.MatchValue(value=dense_index_version)
                ),
            ]
        )
        try:
            response = self._client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                using=self.vector_name,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except _REQUEST_ERRORS:
            raise QdrantRequestError("Qdrant Dense检索失败") from None
        except ValidationError:
            raise QdrantResponseError("Qdrant检索响应无法解析") from None
        points = getattr(response, "points", None)
        if not isinstance(points, list) or len(points) > limit:
            raise QdrantResponseError("Qdrant返回的片段数量无效")

        retrieved: list[RetrievedChunk] = []
        seen_ids: set[UUID] = set()
        for point in points:
            payload = getattr(point, "payload", None)
            score = getattr(point, "score", None)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"text", "metadata", "dense_index_version"}
                or payload["dense_index_version"] != dense_index_version
                or type(score) not in (int, float)
            ):
                raise QdrantResponseError("Qdrant片段payload或分数不符合Dense契约")
            try:
                numeric_score = float(score)
                if not isfinite(numeric_score):
                    raise QdrantResponseError("Qdrant片段分数必须是有限数值")
                chunk = RetrievedChunk.model_validate(
                    {"text": payload["text"], "metadata": payload["metadata"], "score": numeric_score}
                )
                point_id = UUID(str(point.id))
            except (ValidationError, ValueError, AttributeError, OverflowError):
                raise QdrantResponseError("Qdrant返回的片段元数据或标识无效") from None
            if (
                point_id != chunk.metadata.chunk_id
                or point_id in seen_ids
                or chunk.metadata.document_id not in allowed_ids
            ):
                raise QdrantResponseError("Qdrant返回了范围外或标识不匹配的片段")
            seen_ids.add(point_id)
            retrieved.append(chunk)
        return retrieved

    def _validate_vector(self, vector: list[float]) -> list[float]:
        if not isinstance(vector, list) or len(vector) != self.dimensions:
            raise QdrantConfigError("Dense向量维度与集合配置不匹配")
        normalized: list[float] = []
        for value in vector:
            if type(value) not in (int, float):
                raise QdrantConfigError("Dense向量必须包含有限数值")
            try:
                number = float(value)
            except (ValueError, OverflowError):
                raise QdrantConfigError("Dense向量必须包含有限数值") from None
            if not isfinite(number):
                raise QdrantConfigError("Dense向量必须包含有限数值")
            normalized.append(number)
        return normalized

    @staticmethod
    def _validate_version(version: str) -> None:
        if not isinstance(version, str) or not version.strip():
            raise QdrantConfigError("Dense索引版本不能为空")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def create_qdrant_store(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> DenseQdrantStore:
    """只配置HTTP客户端，不构造稀疏模型，也不在构造时发版本探测请求。"""
    kwargs: dict[str, object] = {"trust_env": False}
    if transport is not None:
        kwargs["transport"] = transport
    client = QdrantClient(
        url=str(settings.qdrant_url),
        prefer_grpc=False,
        timeout=settings.qdrant_timeout_seconds,
        check_compatibility=False,
        **kwargs,
    )
    return DenseQdrantStore(
        client,
        collection_name=settings.qdrant_collection_name,
        dimensions=settings.embedding_expected_dimensions,
    )
