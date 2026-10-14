"""D5问题、模型回答契约与后端生成的真实引用。"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_copilot.schemas.documents import DocumentChunk


class RagQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    question: str = Field(strict=True, min_length=1, max_length=2000)


class ModelAnswer(BaseModel):
    """只让模型生成答案和编号，不让模型编造文件名、页码或原文。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    status: Literal["answered", "insufficient_evidence"]
    answer: str = Field(strict=True, min_length=1, max_length=4000)
    citation_numbers: tuple[Annotated[int, Field(strict=True, ge=1)], ...]

    @model_validator(mode="after")
    def check_citations(self) -> Self:
        if len(set(self.citation_numbers)) != len(self.citation_numbers):
            raise ValueError("引用编号不能重复")
        if self.status == "answered" and not self.citation_numbers:
            raise ValueError("回答必须包含引用编号")
        if self.status == "insufficient_evidence" and self.citation_numbers:
            raise ValueError("证据不足时不应返回引用")
        return self


class Citation(DocumentChunk):
    """原文和完整元数据来自本次发送的片段，number从1开始。"""

    number: int = Field(strict=True, ge=1)


class PromptInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    fingerprint: str
    answer_schema_hash: str
    context_max_chars: int


class GenerationInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_model: str
    actual_model: str
    temperature: float
    max_tokens: int
    thinking: Literal["disabled"] = "disabled"
    usage: dict[str, Annotated[int, Field(strict=True, ge=0)]] | None


class RagAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str
    status: Literal["answered", "insufficient_evidence"]
    answer: str
    citations: tuple[Citation, ...]
    evidence_count: int = Field(ge=0)
    prompt: PromptInfo
    index_version: str | None
    # 没有证据时不调用对话模型，也不把未采集的usage伪造为0。
    generation: GenerationInfo | None
