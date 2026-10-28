"""D6单文件上传成功契约；只有入库与成功清单均完成才返回indexed。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from project_copilot.schemas.retrieval import CompletedDocument


class DocumentUploadResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    status: Literal["indexed"] = "indexed"
    mode: Literal["HYBRID_RERANK"] = "HYBRID_RERANK"
    document: CompletedDocument
    chunk_count: int = Field(ge=1, strict=True)
    size_bytes: int = Field(ge=1, strict=True)
    index_version: str = Field(min_length=1)
    run_id: UUID
    # D6只预留关联约定，没有初始化或验证Trace导出。
    # HTTP middleware replaces this with the current trace's actual export acknowledgement.
    trace_exported: bool = False
