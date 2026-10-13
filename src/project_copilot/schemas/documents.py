"""原始文件身份、按页文本和带来源的片段；不依赖向量库。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DocumentPage(BaseModel):
    """TXT/MD没有页码；PDF的page为从0开始的真实页索引。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    text: str
    page: int | None = Field(default=None, ge=0, strict=True)


class LoadedDocument(BaseModel):
    """保留整份文件的身份和解析文本，包括PDF空白页。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    document_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_name: str = Field(min_length=1)
    pages: tuple[DocumentPage, ...]

    @property
    def has_text(self) -> bool:
        return any(page.text.strip() for page in self.pages)


class ChunkMetadata(BaseModel):
    """稳定身份与展示名称分开；index_version在D3表示切分配置指纹。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    document_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_name: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=0, strict=True)
    chunk_id: UUID
    chunk_index: int = Field(ge=0, strict=True)
    index_version: str = Field(min_length=1)


class DocumentChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    text: str = Field(min_length=1)
    metadata: ChunkMetadata
