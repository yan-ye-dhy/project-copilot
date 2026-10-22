"""中文词项检索：只读Dense完成片段，本地快照不包含向量或模型客户端。"""

from collections.abc import Sequence
from hashlib import sha256
from importlib.metadata import version
import json
import logging
from math import isfinite
from pathlib import Path
import re
from typing import Literal
import unicodedata
from uuid import UUID, uuid5

import httpx
import jieba
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from rank_bm25 import BM25L

from project_copilot.schemas.documents import DocumentChunk
from project_copilot.schemas.retrieval import IndexManifest, RetrievedChunk


class BM25Error(ValueError):
    """词项索引缺失、陈旧或与成功清单不一致，必须显式修复/重建。"""


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


class ChineseTokenizer:
    """NFKC/casefold后分开处理中文与技术标识；两端共用同一实例规则。"""

    _parts = re.compile(
        r"[\u3400-\u4dbf\u4e00-\u9fff]+|(?:--?|/)?[a-z0-9_]+(?:[./:-][a-z0-9_]+)*"
    )

    def __init__(self) -> None:
        # 独立词典实例；不使用全局add_word、用户词典或HMM动态推断。
        self._jieba = jieba.Tokenizer()
        self._jieba.set_dictionary(str(Path(jieba.__file__).with_name("dict.txt")))
        jieba.setLogLevel(logging.WARNING)
        dictionary = Path(jieba.__file__).with_name("dict.txt")
        self.spec = {
            "rule": "zh-tech-nfkc-casefold-search-v1",
            "jieba": version("jieba"),
            "dictionary_sha256": sha256(dictionary.read_bytes()).hexdigest(),
            "mode": "search", "hmm": False, "stopwords": False,
            "unicode_version": unicodedata.unidata_version,
        }

    def tokenize(self, text: str) -> list[str]:
        if not isinstance(text, str):
            raise BM25Error("分词输入必须是文本")
        tokens: list[str] = []
        for part in self._parts.findall(unicodedata.normalize("NFKC", text).casefold()):
            if "\u3400" <= part[0] <= "\u9fff":
                tokens.extend(self._jieba.cut_for_search(part, HMM=False))
            else:
                # 错误码/下划线/连字符/API路径整体保留，避免embedding-3被拆成3。
                tokens.append(part)
        return tokens


class BM25Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    algorithm: Literal["BM25L"] = "BM25L"
    implementation: str = Field(default_factory=lambda: version("rank-bm25"))
    numpy_version: str = Field(default_factory=lambda: version("numpy"))
    k1: float = Field(default=1.5, gt=0, allow_inf_nan=False)
    b: float = Field(default=0.75, ge=0, le=1, allow_inf_nan=False)
    delta: float = Field(default=0.5, ge=0, allow_inf_nan=False)


class BM25Snapshot(BaseModel):
    """保存可重建词频的原文和完整Dense清单；不反序列化pickle。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    format_version: Literal[1] = 1
    manifest: IndexManifest
    tokenizer: dict[str, str | bool]
    config: BM25Config = Field(default_factory=BM25Config)
    chunks: tuple[DocumentChunk, ...]


def _aligned_chunks(
    manifest: IndexManifest, chunks: Sequence[DocumentChunk],
) -> tuple[DocumentChunk, ...]:
    allowed = {chunk_id: doc for doc in manifest.documents for chunk_id in doc.chunk_ids}
    by_id: dict[UUID, DocumentChunk] = {}
    for chunk in chunks:
        meta = chunk.metadata
        doc = allowed.get(meta.chunk_id)
        if doc is None or meta.chunk_id in by_id:
            raise BM25Error("BM25片段包含未完成或重复的chunk_id")
        if (
            (meta.document_id, meta.content_hash, meta.source_name)
            != (doc.document_id, doc.content_hash, doc.source_name)
            or meta.index_version != manifest.spec.splitter_version
        ):
            raise BM25Error("BM25片段的文档身份、来源或切分版本与完成清单不一致")
        text_hash = sha256(chunk.text.encode("utf-8")).hexdigest()
        expected_id = uuid5(
            meta.document_id, f"{meta.index_version}:{meta.page}:{meta.chunk_index}:{text_hash}"
        )
        if meta.chunk_id != expected_id:
            raise BM25Error("BM25片段原文与稳定chunk_id不匹配")
        by_id[meta.chunk_id] = chunk
    if set(by_id) != set(allowed):
        raise BM25Error("BM25缺少完成清单中的片段，不允许只构建部分词项索引")
    # 并列分数按UUID字符串排序；不受Qdrant返回顺序影响。
    return tuple(by_id[key] for key in sorted(by_id, key=str))


class BM25Index:
    def __init__(self, snapshot: BM25Snapshot) -> None:
        self.tokenizer = ChineseTokenizer()
        if (
            snapshot.tokenizer != self.tokenizer.spec
            or snapshot.config.implementation != version("rank-bm25")
            or snapshot.config.numpy_version != version("numpy")
        ):
            raise BM25Error("BM25分词规则、词典或依赖版本已变化，请显式重建")
        chunks = _aligned_chunks(snapshot.manifest, snapshot.chunks)
        self.snapshot = snapshot.model_copy(update={"chunks": chunks})
        self._tokens = [self.tokenizer.tokenize(chunk.text) for chunk in chunks]
        if snapshot.config.b == 1 and any(self._tokens) and not all(self._tokens):
            raise BM25Error("存在空词项片段时不支持b=1；请保留默认b=0.75")
        # 标点-only片段仍保留其身份；完全无词或空清单不交给库除以零。
        self._model = (
            BM25L(self._tokens, k1=snapshot.config.k1, b=snapshot.config.b, delta=snapshot.config.delta)
            if any(self._tokens) else None
        )

    @classmethod
    def build(cls, manifest: IndexManifest, chunks: Sequence[DocumentChunk]) -> "BM25Index":
        return cls(BM25Snapshot(
            manifest=manifest, chunks=_aligned_chunks(manifest, chunks),
            tokenizer=ChineseTokenizer().spec,
        ))

    @property
    def fingerprint(self) -> str:
        return "bm25-v1-" + _digest(self.snapshot.model_dump(mode="json"))

    def search(self, query: str, *, top_k: int = 5) -> list[RetrievedChunk]:
        if not isinstance(query, str) or not query.strip():
            raise BM25Error("BM25查询必须是非空文本")
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise BM25Error("top_k必须是1～100的整数")
        terms = self.tokenizer.tokenize(query)
        if not terms or self._model is None:
            return []
        scores = self._model.get_scores(terms)
        shared = set(terms)
        ranked = []
        for i, score in enumerate(scores):
            value = float(score)
            if not isfinite(value):
                raise BM25Error("BM25返回了非有限分数")
            if shared.intersection(self._tokens[i]):
                ranked.append((i, value))
        ranked.sort(key=lambda item: (-item[1], str(self.snapshot.chunks[item[0]].metadata.chunk_id)))
        return [
            RetrievedChunk(**self.snapshot.chunks[i].model_dump(), score=score)
            for i, score in ranked[:top_k]
        ]

    def save(self, path: Path) -> None:
        payload = {"snapshot": self.snapshot.model_dump(mode="json"), "fingerprint": self.fingerprint}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 独立快照：不用旧路径覆盖已有索引，重建时显式选择新文件。
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            raise BM25Error("无法保存BM25快照；目标文件须不存在且目录可写") from None

    @classmethod
    def load(cls, path: Path, manifest: IndexManifest) -> "BM25Index":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"snapshot", "fingerprint"}:
                raise BM25Error("BM25快照格式不合法")
            snapshot = BM25Snapshot.model_validate(payload["snapshot"])
            if snapshot.manifest != manifest:
                raise BM25Error("成功清单已变化，旧BM25快照不可使用，请显式重建")
            if payload["fingerprint"] != "bm25-v1-" + _digest(snapshot.model_dump(mode="json")):
                raise BM25Error("BM25快照内容指纹不匹配")
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, BM25Error):
                raise
            raise BM25Error("无法读取BM25快照或内容不合法") from None
        return cls(snapshot)


def read_completed_chunks(client: QdrantClient, manifest: IndexManifest) -> list[DocumentChunk]:
    """只读集合和精确ID；不建集合、不scroll未完成点、不读取向量。"""
    try:
        info = client.get_collection(manifest.spec.collection_name)
        params = info.config.params
        vectors = params.vectors
        if not isinstance(vectors, dict) or set(vectors) != {manifest.spec.vector_name}:
            raise BM25Error("Qdrant命名向量字段与完成清单不一致")
        dense = vectors[manifest.spec.vector_name]
        if (
            dense.size != manifest.spec.dimensions or dense.distance != models.Distance.COSINE
            or dense.multivector_config is not None or params.sparse_vectors
        ):
            raise BM25Error("Qdrant集合维度、距离或Dense-only契约与完成清单不一致")
        ids = [str(cid) for doc in manifest.documents for cid in doc.chunk_ids]
        chunks = []
        seen: set[UUID] = set()
        for start in range(0, len(ids), 64):
            records = client.retrieve(
                collection_name=manifest.spec.collection_name, ids=ids[start:start + 64],
                with_payload=True, with_vectors=False,
            )
            requested = set(ids[start:start + 64])
            for record in records:
                payload = record.payload
                if (
                    not isinstance(payload, dict) or set(payload) != {"text", "metadata", "dense_index_version"}
                    or payload["dense_index_version"] != manifest.spec.index_version
                ):
                    raise BM25Error("Qdrant片段payload或Dense版本不匹配")
                chunk = DocumentChunk.model_validate({"text": payload["text"], "metadata": payload["metadata"]})
                if str(record.id) not in requested or UUID(str(record.id)) != chunk.metadata.chunk_id:
                    raise BM25Error("Qdrant点ID与请求或片段身份不匹配")
                if chunk.metadata.chunk_id in seen:
                    raise BM25Error("Qdrant返回重复片段")
                seen.add(chunk.metadata.chunk_id)
                chunks.append(chunk)
        return list(_aligned_chunks(manifest, chunks))
    except (UnexpectedResponse, ResponseHandlingException, httpx.HTTPError):
        raise BM25Error("无法只读获取Qdrant完成片段，请检查服务和集合") from None
    except (ValidationError, AttributeError, TypeError):
        raise BM25Error("Qdrant集合或片段响应不符合契约") from None
