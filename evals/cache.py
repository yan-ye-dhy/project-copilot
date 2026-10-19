"""评测侧持久Embedding缓存；不保存原文/密钥，损坏时停止而不隐式重收费。"""

import json
import math
import os
import tempfile
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from project_copilot.rag.ingestion import Embeddings


class EmbeddingCacheError(ValueError):
    """缓存契约、内容校验或持久化失败；调用者必须显式处理。"""


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class CachedEmbeddings:
    """复用D2向量契约；顺序评测使用，客户端关闭由外层负责。

    hit_texts/miss_texts按每次调用内的唯一文本累计，重复输入只查一次；
    api_calls记录实际调用delegate的批次数，包括失败请求。
    """

    def __init__(
        self, delegate: "Embeddings", cache_dir: Path, *, base_url: str,
    ) -> None:
        self.delegate = delegate
        self.model = delegate.model
        self.expected_dimensions = delegate.expected_dimensions
        self.batch_size = getattr(delegate, "batch_size", 64)
        if not isinstance(self.model, str) or not self.model.strip():
            raise EmbeddingCacheError("缓存模型标识必须是非空字符串")
        if type(self.expected_dimensions) is not int or self.expected_dimensions <= 0:
            raise EmbeddingCacheError("缓存向量维度必须是正整数")
        if type(self.batch_size) is not int or self.batch_size <= 0:
            raise EmbeddingCacheError("缓存请求批量必须是正整数")
        if not isinstance(base_url, str) or not base_url.strip():
            raise EmbeddingCacheError("缓存供应商地址不合法")
        try:
            parsed = urlsplit(base_url)
            valid_url = (
                parsed.scheme in ("http", "https") and parsed.hostname
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and not any(character.isspace() for character in base_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise EmbeddingCacheError("缓存供应商地址不合法或包含凭据/查询参数")
        self.cache_dir = Path(cache_dir)
        self._contract = {
            "base_url": base_url.rstrip("/"), "model": self.model,
            "dimensions": self.expected_dimensions, "encoding_format": "float",
        }
        self._counts = {"hit_texts": 0, "miss_texts": 0, "api_calls": 0}

    def stats(self) -> dict[str, int]:
        """返回独立快照，外层可取前后差值记录各阶段缓存条件。"""
        return dict(self._counts)

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)):
            raise ValueError("Embedding输入必须是文本序列")
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding文本必须是非空、非全空白的字符串")
        unique_texts = list(dict.fromkeys(texts))
        vectors: dict[str, list[float]] = {}
        missing: list[str] = []
        # 所有既有文件先验证，避免后面的损坏文件导致前面先发生收费请求。
        for text in unique_texts:
            identity, path = self._identity(text)
            vector = self._load(path, identity)
            if vector is None:
                missing.append(text)
            else:
                vectors[text] = vector
        self._counts["hit_texts"] += len(vectors)
        self._counts["miss_texts"] += len(missing)
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            self._counts["api_calls"] += 1
            response = self.delegate.embed_documents(batch)
            if not isinstance(response, list) or len(response) != len(batch):
                raise EmbeddingCacheError("Embedding缓存收到的向量条数不匹配")
            # 整批通过后才保存，不能把同批里的畸形响应部分登记为成功。
            validated = [self._vector(vector) for vector in response]
            for text, vector in zip(batch, validated, strict=True):
                identity, path = self._identity(text)
                self._save(path, identity, vector)
                vectors[text] = vector
        # 返回副本；调用方修改一个重复位置不能污染另一个位置或持久缓存。
        return [list(vectors[text]) for text in texts]

    def _identity(self, text: str) -> tuple[dict, Path]:
        identity = {
            "schema_version": 1, "contract": self._contract,
            "text_sha256": sha256(text.encode("utf-8")).hexdigest(),
        }
        key = sha256(_json_bytes(identity)).hexdigest()
        return identity, self.cache_dir / f"{key}.json"

    def _vector(self, raw: object) -> list[float]:
        if not isinstance(raw, list) or len(raw) != self.expected_dimensions:
            raise EmbeddingCacheError("缓存向量维度或类型不匹配")
        result: list[float] = []
        for value in raw:
            if type(value) not in (int, float):
                raise EmbeddingCacheError("缓存向量包含非数值元素")
            try:
                number = float(value)
            except (ValueError, OverflowError):
                raise EmbeddingCacheError("缓存向量包含非法数值") from None
            if not math.isfinite(number):
                raise EmbeddingCacheError("缓存向量包含非有限数值")
            result.append(number)
        return result

    def _load(self, path: Path, identity: dict) -> list[float] | None:
        if path.is_symlink():
            raise EmbeddingCacheError("缓存文件不能是符号链接")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError):
            raise EmbeddingCacheError("Embedding缓存文件损坏或无法读取") from None
        required = {*identity, "vector", "content_sha256"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise EmbeddingCacheError("Embedding缓存文件结构不合法")
        try:
            stored_identity = _json_bytes({key: payload[key] for key in identity})
        except (ValueError, TypeError, UnicodeError):
            raise EmbeddingCacheError("Embedding缓存身份格式不合法") from None
        if stored_identity != _json_bytes(identity):
            raise EmbeddingCacheError("Embedding缓存身份与当前请求不匹配")
        vector = self._vector(payload["vector"])
        content = {key: value for key, value in payload.items() if key != "content_sha256"}
        expected_hash = sha256(_json_bytes(content)).hexdigest()
        if payload["content_sha256"] != expected_hash:
            raise EmbeddingCacheError("Embedding缓存内容哈希不匹配")
        return vector

    def _save(self, path: Path, identity: dict, vector: list[float]) -> None:
        content = {**identity, "vector": vector}
        payload = {**content, "content_sha256": sha256(_json_bytes(content)).hexdigest()}
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".embedding-", suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(_json_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError:
            raise EmbeddingCacheError("无法原子保存Embedding缓存；请勿直接重跑收费任务") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
