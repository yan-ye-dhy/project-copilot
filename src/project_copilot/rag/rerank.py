"""本地Cross-Encoder最小适配；只重排给定候选，保留原始检索证据。"""

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from numbers import Real
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Protocol

from project_copilot.config import PROJECT_ROOT
from project_copilot.schemas.retrieval import RetrievedChunk


MODEL_NAME = "BAAI/bge-reranker-v2-m3"
MODEL_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


class RerankError(ValueError):
    """输入、缓存模型或重排执行不满足合同；不隐式回退到原排名。"""


@dataclass(frozen=True)
class RerankConfig:
    model_name: str = MODEL_NAME
    revision: str = MODEL_REVISION
    device: str = "cpu"
    dtype: str = "float32"
    # 512是本项目输入预算，并非该模型支持的最大上下文长度。
    max_length: int = 512
    batch_size: int = 2
    max_candidates: int = 10
    top_k: int = 5
    cpu_threads: int = 4
    local_files_only: bool = True

    def __post_init__(self) -> None:
        if self.model_name != MODEL_NAME:
            raise RerankError("D15仅支持已核对的BGE中文/多语言候选模型")
        if not isinstance(self.revision, str) or not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise RerankError("模型revision必须固定为完整40位commit SHA")
        if (
            not isinstance(self.device, str) or self.device not in {"cpu", "cuda"}
            or not isinstance(self.dtype, str) or self.dtype not in {"float32", "float16"}
        ):
            raise RerankError("device仅支持cpu/cuda，dtype仅支持float32/float16")
        if self.device == "cpu" and self.dtype != "float32":
            raise RerankError("本项目CPU重排必须使用float32")
        for name, value, upper in (
            ("max_length", self.max_length, 8192),
            ("batch_size", self.batch_size, 100),
            ("max_candidates", self.max_candidates, 100),
            ("top_k", self.top_k, 100),
            ("cpu_threads", self.cpu_threads, 256),
        ):
            if type(value) is not int or not 1 <= value <= upper:
                raise RerankError(f"{name}必须是1至{upper}的整数，不能使用布尔值")
        if self.top_k > self.max_candidates:
            raise RerankError("top_k不能超过max_candidates候选预算")
        if self.local_files_only is not True:
            raise RerankError("模型必须使用本地缓存，请先显式准备固定版本模型")


class PairScorer(Protocol):
    def score(self, query: str, passages: Sequence[str]) -> Sequence[float]: ...


@dataclass(frozen=True)
class RerankedHit:
    hit: RetrievedChunk
    candidate_rank: int
    rerank_score: float


def _validate_query(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise RerankError("重排查询必须是非空文本")


def _scores(values: object, expected: int) -> list[float]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or len(values) != expected:
        raise RerankError("重排分数数量必须与候选数量一致")
    result: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
            raise RerankError("重排分数必须是有限实数，不能使用布尔值")
        result.append(float(value))
    return result


class Reranker:
    def __init__(self, scorer: PairScorer, *, config: RerankConfig = RerankConfig()) -> None:
        self.scorer, self.config = scorer, config

    def rerank(self, query: str, candidates: Sequence[RetrievedChunk]) -> list[RerankedHit]:
        _validate_query(query)
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            raise RerankError("重排候选必须是RetrievedChunk序列")
        if len(candidates) > self.config.max_candidates:
            raise RerankError("重排候选数量超过max_candidates预算")
        seen = set()
        for hit in candidates:
            if not isinstance(hit, RetrievedChunk):
                raise RerankError("重排候选必须是RetrievedChunk")
            if hit.metadata.chunk_id in seen:
                raise RerankError("重排候选不能包含重复chunk_id")
            seen.add(hit.metadata.chunk_id)
        if not candidates:
            return []
        try:
            scores = _scores(self.scorer.score(query, [hit.text for hit in candidates]), len(candidates))
        except RerankError:
            raise
        except Exception as exc:
            raise RerankError("候选重排执行失败，没有返回替代排名") from exc
        ranked = [
            RerankedHit(hit=hit, candidate_rank=rank, rerank_score=score)
            for rank, (hit, score) in enumerate(zip(candidates, scores, strict=True), start=1)
        ]
        # 并列沿用输入候选次序；不混合原检索分数与Cross-Encoder原始logit。
        return sorted(ranked, key=lambda item: (-item.rerank_score, item.candidate_rank))[:self.config.top_k]


class LocalCrossEncoder:
    """首次非空评分才载入本地模型；不安装、不下载，不调用任何模型API。

    last_diagnostics与输入passages顺序相同。所有截断只发生于模型输入，
    查询完整保留，仅截断第二段；输入过长查询会在模型推断前拒绝。
    """

    def __init__(self, config: RerankConfig = RerankConfig(), *, cache_dir: str | Path | None = None) -> None:
        self.config = config
        self.cache_dir = str(cache_dir if cache_dir is not None else PROJECT_ROOT / "models" / "huggingface")
        self._torch: Any = None
        self._tokenizer: Any = None
        self._model: Any = None
        self.load_ms: float | None = None
        self.last_load_ms = 0.0
        self.last_inference_ms = 0.0
        self.last_total_ms = 0.0
        self.last_diagnostics: list[dict[str, int | bool]] = []

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> None:
        if self.loaded:
            return
        started = perf_counter()
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            if self.config.device == "cuda" and not torch.cuda.is_available():
                raise RerankError("所选CUDA设备不可用")
            if self.config.device == "cpu":
                torch.set_num_threads(self.config.cpu_threads)
            options = {
                "revision": self.config.revision, "cache_dir": self.cache_dir,
                "local_files_only": True, "trust_remote_code": False,
            }
            tokenizer = AutoTokenizer.from_pretrained(self.config.model_name, **options)
            model = AutoModelForSequenceClassification.from_pretrained(
                self.config.model_name, **options, use_safetensors=True,
                dtype=getattr(torch, self.config.dtype),
            )
            model.to(self.config.device)
            model.eval()
            self._torch, self._tokenizer, self._model = torch, tokenizer, model
        except RerankError:
            raise
        except Exception as exc:
            raise RerankError("本地重排模型加载失败，请检查固定版本缓存及运行环境") from exc
        finally:
            self.last_load_ms = (perf_counter() - started) * 1000
        self.load_ms = self.last_load_ms

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        self.last_load_ms = self.last_inference_ms = self.last_total_ms = 0.0
        self.last_diagnostics = []
        started = perf_counter()
        try:
            _validate_query(query)
            if not isinstance(passages, Sequence) or isinstance(passages, (str, bytes)):
                raise RerankError("待评分片段必须是文本序列")
            if len(passages) > self.config.max_candidates:
                raise RerankError("待评分片段数量超过max_candidates预算")
            if any(not isinstance(text, str) or not text.strip() for text in passages):
                raise RerankError("待评分片段必须是非空文本")
            if not passages:
                return []
            self._load()
            tokenizer = self._tokenizer
            query_tokens = len(tokenizer.encode(query, add_special_tokens=False))
            special_tokens = tokenizer.num_special_tokens_to_add(pair=True)
            if query_tokens + special_tokens >= self.config.max_length:
                raise RerankError("查询token加特殊token已用尽max_length，无法保留片段输入")
            originals = [
                query_tokens + len(tokenizer.encode(text, add_special_tokens=False)) + special_tokens
                for text in passages
            ]
            result: list[float] = []
            diagnostics: list[dict[str, int | bool]] = []
            for offset in range(0, len(passages), self.config.batch_size):
                batch = list(passages[offset:offset + self.config.batch_size])
                encoded = tokenizer(
                    [query] * len(batch), batch, padding=True, truncation="only_second",
                    max_length=self.config.max_length, return_tensors="pt", return_attention_mask=True,
                )
                lengths = encoded["attention_mask"].sum(dim=1).tolist()
                if len(lengths) != len(batch):
                    raise RerankError("Tokenizer返回的片段数量不一致")
                for index, length in enumerate(lengths, start=offset):
                    if type(length) is not int or length != min(originals[index], self.config.max_length):
                        raise RerankError("Tokenizer实际输入长度与only_second截断合同不一致")
                    diagnostics.append({
                        "original_pair_tokens": originals[index], "input_tokens": length,
                        "passage_truncated": originals[index] > length,
                    })
                inputs = {name: value.to(self.config.device) for name, value in encoded.items()}
                inference_started = perf_counter()
                try:
                    with self._torch.inference_mode():
                        logits = self._model(**inputs).logits
                        if tuple(logits.shape) != (len(batch), 1):
                            raise RerankError("Cross-Encoder必须为每个候选返回单个原始logit")
                        batch_scores = logits[:, 0].float().cpu().tolist()
                    result.extend(_scores(batch_scores, len(batch)))
                finally:
                    self.last_inference_ms += (perf_counter() - inference_started) * 1000
            self.last_diagnostics = diagnostics
            return result
        except RerankError:
            raise
        except Exception as exc:
            raise RerankError("本地Cross-Encoder评分失败，没有返回替代分数") from exc
        finally:
            self.last_total_ms = (perf_counter() - started) * 1000
