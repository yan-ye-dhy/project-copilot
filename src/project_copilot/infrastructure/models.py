"""智谱Embedding适配：原文本请求、明确错误、向量契约与资源关闭。"""

from collections.abc import Sequence
from math import isfinite
from typing import Self

import httpx
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)

from project_copilot.config import Settings


class EmbeddingConfigError(ValueError):
    """创建模型客户端所需的配置不完整。"""


class EmbeddingRequestError(RuntimeError):
    """供应商请求错误，只对外提供脱敏信息。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EmbeddingResponseError(RuntimeError):
    """HTTP成功但响应不满足本项目的向量契约。"""


class ZhipuEmbeddings:
    """同步最小适配器；调用者通过with或close释放底层HTTP客户端。"""

    def __init__(
        self,
        client: OpenAI,
        *,
        model: str,
        expected_dimensions: int,
        batch_size: int,
    ) -> None:
        self._client = client
        self.model = model
        self.expected_dimensions = expected_dimensions
        self.batch_size = batch_size

    def embed_query(self, text: str) -> list[float]:
        """问题与文档使用同一模型和向量契约。"""
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """先验证全部输入，再按文本条数分批；保留原文而不自动切分。"""
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)):
            raise ValueError("Embedding输入必须是文本序列")
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding文本必须是非空、非全空白的字符串")

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            try:
                raw_response = self._client.embeddings.with_raw_response.create(
                    model=self.model,
                    input=batch,
                    encoding_format="float",
                )
                # SDK类型转换可能把bool/字符串转换成float；校验原始JSON才可识别。
                response = raw_response.http_response.json()
            except APITimeoutError:
                raise EmbeddingRequestError("智谱Embedding请求超时") from None
            except APIConnectionError:
                raise EmbeddingRequestError("无法连接智谱Embedding服务") from None
            except APIStatusError as exc:
                status = exc.status_code
                if status in (401, 403):
                    message = "智谱Embedding认证或权限失败，请检查新项目密钥"
                elif status == 429:
                    message = "智谱Embedding请求受限，请检查限流与账号状态"
                elif status >= 500:
                    message = "智谱Embedding服务暂时不可用"
                else:
                    message = "智谱Embedding请求被拒绝，请检查模型和输入配置"
                raise EmbeddingRequestError(message, status_code=status) from None
            except (APIResponseValidationError, ValueError, TypeError):
                raise EmbeddingResponseError("智谱Embedding响应无法解析") from None

            vectors.extend(self._validate_batch(response, count=len(batch)))
        return vectors

    def _validate_batch(
        self, response: object, *, count: int
    ) -> list[list[float]]:
        if not isinstance(response, dict):
            raise EmbeddingResponseError("Embedding响应必须是JSON对象")
        if response.get("model") != self.model:
            raise EmbeddingResponseError("Embedding响应的模型标识与请求不符")
        data = response.get("data")
        if not isinstance(data, list) or len(data) != count:
            raise EmbeddingResponseError("Embedding返回条数与输入不一致")

        ordered: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingResponseError("Embedding响应条目必须是JSON对象")
            index = item.get("index")
            if type(index) is not int or not 0 <= index < count or index in ordered:
                raise EmbeddingResponseError("Embedding响应包含重复或非法index")
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise EmbeddingResponseError("Embedding向量必须是数值列表")
            if len(embedding) != self.expected_dimensions:
                raise EmbeddingResponseError(
                    f"Embedding维度不匹配：预期{self.expected_dimensions}，实际{len(embedding)}"
                )

            vector: list[float] = []
            for value in embedding:
                if type(value) not in (int, float):
                    raise EmbeddingResponseError("Embedding向量包含非数值元素")
                try:
                    number = float(value)
                except (ValueError, OverflowError):
                    raise EmbeddingResponseError("Embedding向量包含非法数值") from None
                if not isfinite(number):
                    raise EmbeddingResponseError("Embedding向量包含非有限数值")
                vector.append(number)
            ordered[index] = vector
        # 使用供应商index恢复输入顺序，不能假定data数组总是有序。
        return [ordered[index] for index in range(count)]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def create_embedding_client(
    settings: Settings, *, http_client: httpx.Client | None = None
) -> ZhipuEmbeddings:
    """显式配置SDK；缺key不回退到OPENAI_API_KEY，也不自动发请求。"""
    key = settings.zhipu_api_key.get_secret_value().strip() if settings.zhipu_api_key else ""
    if not key or not key.isascii() or any(character.isspace() for character in key):
        raise EmbeddingConfigError("请在新项目配置有效的PROJECT_COPILOT_ZHIPU_API_KEY")
    client = OpenAI(
        api_key=key,
        base_url=str(settings.zhipu_base_url),
        timeout=settings.zhipu_timeout_seconds,
        max_retries=0,
        http_client=http_client,
    )
    return ZhipuEmbeddings(
        client,
        model=settings.embedding_model,
        expected_dimensions=settings.embedding_expected_dimensions,
        batch_size=settings.embedding_batch_size,
    )
