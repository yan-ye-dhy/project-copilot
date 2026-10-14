"""Dense索引配置、成功清单与检索证据；保留D3文档身份。"""

import hashlib
import json
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_copilot.schemas.documents import DocumentChunk


class DenseIndexSpec(BaseModel):
    """同一集合的向量与切分契约；变更时应使用新集合。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    collection_name: str = Field(pattern=r"^pc_[A-Za-z0-9_-]+$", max_length=120)
    embedding_model: str = Field(min_length=1)
    dimensions: int = Field(ge=1, strict=True)
    vector_name: Literal["dense"] = "dense"
    distance: Literal["Cosine"] = "Cosine"
    splitter_version: str = Field(min_length=1)

    @property
    def index_version(self) -> str:
        # D3的metadata.index_version仍是切分指纹；这里另含模型/维度等。
        encoded = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return "dense-v1-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CompletedDocument(BaseModel):
    """仅在Qdrant确认写入成功后登记，不保存密钥或完整向量。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    document_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_name: str = Field(min_length=1)
    chunk_ids: tuple[UUID, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_chunk_ids(self) -> Self:
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("完成清单不能包含重复chunk_id")
        return self


class IndexManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    format_version: Literal[1] = 1
    spec: DenseIndexSpec
    documents: tuple[CompletedDocument, ...] = ()

    @model_validator(mode="after")
    def unique_document_ids(self) -> Self:
        ids = [item.document_id for item in self.documents]
        chunks = [chunk_id for item in self.documents for chunk_id in item.chunk_ids]
        if len(set(ids)) != len(ids) or len(set(chunks)) != len(chunks):
            raise ValueError("完成清单不能包含重复文档或片段身份")
        return self


class RetrievedChunk(DocumentChunk):
    """实际检索证据；score是Cosine相关度，不是答案置信度。"""

    score: float = Field(strict=True, allow_inf_nan=False)
