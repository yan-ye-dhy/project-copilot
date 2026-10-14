"""智谱Embedding与DeepSeek对话适配：显式请求、脱敏错误与响应契约。"""

from collections.abc import Sequence
from dataclasses import dataclass
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


class ChatConfigError(ValueError):
    """创建对话客户端所需的配置不完整。"""


class ChatRequestError(RuntimeError):
    """对话供应商请求错误，只对外提供脱敏信息。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ChatResponseError(RuntimeError):
    """HTTP成功但响应不满足本项目的对话协议。"""


@dataclass(frozen=True)
class ChatGeneration:
    """原始回答文本、供应商实际模型标识与白名单Token计数。"""

    content: str
    model: str
    usage: dict[str, int] | None


class DeepSeekChat:
    """同步JSON输出适配器；回答Schema与引用由回答层继续校验。"""

    def __init__(
        self,
        client: OpenAI,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> None:
        self._client = client
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def generate_json(self, messages: Sequence[dict[str, str]]) -> ChatGeneration:
        """每次只有一个System和一个User消息；不重试或修复模型输出。"""
        if (
            not isinstance(messages, Sequence)
            or isinstance(messages, (str, bytes))
            or len(messages) != 2
        ):
            raise ValueError("对话输入必须包含一条System和一条User消息")
        for message, role in zip(messages, ("system", "user"), strict=True):
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message.get("role") != role
                or not isinstance(message.get("content"), str)
                or not message["content"].strip()
            ):
                raise ValueError("对话消息必须依次为System和User，且仅含角色与非空文本")

        try:
            raw_response = self._client.chat.completions.with_raw_response.create(
                model=self.model,
                messages=[dict(message) for message in messages],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=False,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            # 校验原始JSON，避免SDK宽松转换把bool或字符串Token计数变成int。
            response = raw_response.http_response.json()
        except APITimeoutError:
            raise ChatRequestError("DeepSeek对话请求超时") from None
        except APIConnectionError:
            raise ChatRequestError("无法连接DeepSeek对话服务") from None
        except APIStatusError as exc:
            status = exc.status_code
            if status in (401, 403):
                message = "DeepSeek对话认证或权限失败，请检查新项目密钥"
            elif status == 429:
                message = "DeepSeek对话请求受限，请检查限流与账号状态"
            elif status >= 500:
                message = "DeepSeek对话服务暂时不可用"
            else:
                message = "DeepSeek对话请求被拒绝，请检查模型和输入配置"
            raise ChatRequestError(message, status_code=status) from None
        except (APIResponseValidationError, ValueError, TypeError):
            raise ChatResponseError("DeepSeek对话响应无法解析") from None

        return self._validate_response(response)

    @staticmethod
    def _validate_response(response: object) -> ChatGeneration:
        if not isinstance(response, dict):
            raise ChatResponseError("对话响应必须是JSON对象")
        model = response.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ChatResponseError("对话响应缺少有效模型标识")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ChatResponseError("对话响应必须包含且仅包含一个回答")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
            raise ChatResponseError("对话回答未正常完成")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ChatResponseError("对话回答必须是Assistant消息")
        tool_calls = message.get("tool_calls")
        if (
            "tools" in message
            or (tool_calls is not None and (not isinstance(tool_calls, list) or tool_calls))
            or message.get("function_call") is not None
        ):
            raise ChatResponseError("对话回答不允许工具调用")
        if message.get("refusal") is not None:
            raise ChatResponseError("DeepSeek拒绝生成回答")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ChatResponseError("对话回答文本为空或非法")
        usage = DeepSeekChat._validate_usage(response["usage"]) if "usage" in response else None
        # content保留原文，JSON语法及本地回答Schema由调用者负责。
        return ChatGeneration(content=content, model=model, usage=usage)

    @staticmethod
    def _validate_usage(value: object) -> dict[str, int]:
        if not isinstance(value, dict):
            raise ChatResponseError("对话Token用量必须是JSON对象")
        usage: dict[str, int] = {}
        required = ("prompt_tokens", "completion_tokens", "total_tokens")
        optional = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens")
        for field in (*required, *optional):
            if field not in value and field in optional:
                continue
            count = value.get(field)
            if type(count) is not int or count < 0:
                raise ChatResponseError("对话Token用量必须是非负整数")
            usage[field] = count
        if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
            raise ChatResponseError("对话Token总量与输入输出计数不符")
        if "completion_tokens_details" in value:
            details = value["completion_tokens_details"]
            if not isinstance(details, dict):
                raise ChatResponseError("对话输出Token明细必须是JSON对象")
            if "reasoning_tokens" in details:
                reasoning = details["reasoning_tokens"]
                if type(reasoning) is not int or reasoning < 0:
                    raise ChatResponseError("对话推理Token用量必须是非负整数")
                if "reasoning_tokens" in usage and usage["reasoning_tokens"] != reasoning:
                    raise ChatResponseError("对话推理Token计数不一致")
                usage["reasoning_tokens"] = reasoning
        return usage

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def create_chat_client(
    settings: Settings, *, http_client: httpx.Client | None = None
) -> DeepSeekChat:
    """独立DeepSeek密钥、显式参数与零自动重试；构造不发模型请求。"""
    key = settings.deepseek_api_key.get_secret_value().strip() if settings.deepseek_api_key else ""
    if not key or not key.isascii() or any(character.isspace() for character in key):
        raise ChatConfigError("请在新项目配置有效的PROJECT_COPILOT_DEEPSEEK_API_KEY")
    client = OpenAI(
        api_key=key,
        base_url=str(settings.deepseek_base_url),
        timeout=settings.deepseek_timeout_seconds,
        max_retries=0,
        http_client=http_client,
    )
    return DeepSeekChat(
        client,
        model=settings.deepseek_model,
        temperature=settings.deepseek_temperature,
        max_tokens=settings.deepseek_max_tokens,
    )
