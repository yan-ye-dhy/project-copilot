"""按页递归切分；原文、切分版本和片段位置共同形成稳定身份。"""

from hashlib import sha256
from importlib.metadata import version
import json
from uuid import uuid5

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_copilot.schemas.documents import ChunkMetadata, DocumentChunk, LoadedDocument


class SplitConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    chunk_size: int = Field(default=750, ge=1, strict=True)
    chunk_overlap: int = Field(default=150, ge=0, strict=True)
    separators: tuple[str, ...] = ("\n\n", "\n", " ", "")

    @model_validator(mode="after")
    def validate_boundaries(self) -> "SplitConfig":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap必须小于chunk_size。")
        if not self.separators or self.separators[-1] != "" or "" in self.separators[:-1]:
            raise ValueError("separators必须以唯一的空字符串结尾，保证字符回退。")
        return self

    @property
    def index_version(self) -> str:
        """D3的切分指纹；后续索引还需记录Embedding模型与维度。"""
        payload = {
            "algorithm": "recursive-character-v1",
            "implementation": version("langchain-text-splitters"),
            **self.model_dump(mode="json"),
            "length_function": "len",
            "keep_separator": True,
            "strip_whitespace": False,
            "is_separator_regex": False,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"split-v1-{sha256(encoded.encode('utf-8')).hexdigest()}"


class DocumentSplitError(RuntimeError):
    """第三方切分结果违反本项目的字符长度契约。"""


def split_document(document: LoadedDocument, config: SplitConfig | None = None) -> list[DocumentChunk]:
    """空文档返回[]；不跨PDF页合并，不改变TXT/MD的page=None。"""
    config = config if config is not None else SplitConfig()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        separators=list(config.separators),
        length_function=len,
        keep_separator=True,
        strip_whitespace=False,
        is_separator_regex=False,
    )
    index_version = config.index_version
    chunks = []
    for page in document.pages:
        if not page.text.strip():
            continue
        for text in splitter.split_text(page.text):
            if not text.strip():
                continue
            if len(text) > config.chunk_size:
                raise DocumentSplitError("切分结果超过当前字符上限。")
            chunk_index = len(chunks)
            text_hash = sha256(text.encode("utf-8")).hexdigest()
            # 包含位置，重复原文不会合并；包含文本指纹，解析变化不会误用旧ID。
            chunk_id = uuid5(document.document_id, f"{index_version}:{page.page}:{chunk_index}:{text_hash}")
            chunks.append(
                DocumentChunk(
                    text=text,
                    metadata=ChunkMetadata(
                        document_id=document.document_id,
                        content_hash=document.content_hash,
                        source_name=document.source_name,
                        page=page.page,
                        chunk_id=chunk_id,
                        chunk_index=chunk_index,
                        index_version=index_version,
                    ),
                )
            )
    return chunks
