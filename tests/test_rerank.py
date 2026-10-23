"""D15离线合同：评分替身与轻量张量/Tokenizer替身，不下载或调用模型。"""

import builtins
from contextlib import nullcontext
from dataclasses import FrozenInstanceError, replace
from types import ModuleType, SimpleNamespace
import sys
from unittest.mock import Mock
from uuid import UUID

import pytest

from project_copilot.config import PROJECT_ROOT
from project_copilot.rag.rerank import (
    MODEL_NAME, MODEL_REVISION, LocalCrossEncoder, RerankConfig, RerankError,
    RerankedHit, Reranker,
)
from project_copilot.schemas.documents import ChunkMetadata
from project_copilot.schemas.retrieval import RetrievedChunk


def _hit(number: int, text: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        text=text or f"中文原文证据{number}，包含 /documents/upload 参数。",
        metadata=ChunkMetadata(
            document_id=UUID(int=100 + number), content_hash="a" * 64,
            source_name=f"中文资料{number}.pdf", page=number, chunk_id=UUID(int=number),
            chunk_index=number, index_version="split-test-750-150",
        ),
        score=1.0 / (60 + number),
    )


def test_scores_all_candidates_then_takes_top_k_and_retains_original_objects() -> None:
    candidates = [_hit(number) for number in (1, 2, 3)]
    before = [hit.model_dump() for hit in candidates]
    scorer = Mock()
    scorer.score.return_value = [-3.2, -1.0, -2.5]
    config = RerankConfig(max_candidates=3, top_k=2)

    result = Reranker(scorer, config=config).rerank("如何上传中文文件？", candidates)

    scorer.score.assert_called_once_with("如何上传中文文件？", [hit.text for hit in candidates])
    assert result == [RerankedHit(candidates[1], 2, -1.0), RerankedHit(candidates[2], 3, -2.5)]
    assert result[0].hit is candidates[1]
    assert result[0].hit.metadata is candidates[1].metadata
    assert [hit.model_dump() for hit in candidates] == before
    assert result[0].hit.score != result[0].rerank_score


def test_equal_scores_keep_candidate_order_and_fewer_candidates_are_valid() -> None:
    candidates = [_hit(number) for number in (3, 1, 2)]
    scorer = Mock()
    scorer.score.return_value = [0.0] * 3
    result = Reranker(scorer).rerank("相同得分", candidates)
    assert [item.hit.metadata.chunk_id.int for item in result] == [3, 1, 2]
    assert [item.candidate_rank for item in result] == [1, 2, 3]


def test_empty_candidates_do_not_call_scorer() -> None:
    scorer = Mock()
    assert Reranker(scorer).rerank("合法查询", []) == []
    scorer.score.assert_not_called()


@pytest.mark.parametrize("query", ["", " \n ", None, 2, True])
def test_invalid_query_is_rejected_before_scorer(query: object) -> None:
    scorer = Mock()
    with pytest.raises(RerankError, match="非空文本"):
        Reranker(scorer).rerank(query, [_hit(1)])
    scorer.score.assert_not_called()


@pytest.mark.parametrize("candidates", [None, "片段", [_hit(1), object()], [_hit(1), _hit(1)]])
def test_invalid_candidates_are_rejected_before_scorer(candidates: object) -> None:
    scorer = Mock()
    with pytest.raises(RerankError):
        Reranker(scorer).rerank("合法查询", candidates)
    scorer.score.assert_not_called()


def test_excess_candidates_are_rejected_instead_of_silently_truncated() -> None:
    scorer = Mock()
    with pytest.raises(RerankError, match="候选数量"):
        Reranker(scorer, config=RerankConfig(max_candidates=1, top_k=1)).rerank(
            "合法查询", [_hit(1), _hit(2)],
        )
    scorer.score.assert_not_called()


@pytest.mark.parametrize("values", [[1.0], [1.0, 2.0, 3.0], [1.0, True], [1.0, "2"],
                                    [float("nan"), 1.0], [1.0, float("inf")], None, "12"])
def test_invalid_scores_never_become_a_partial_or_original_ranking(values: object) -> None:
    scorer = Mock()
    scorer.score.return_value = values
    with pytest.raises(RerankError):
        Reranker(scorer).rerank("合法查询", [_hit(1), _hit(2)])


def test_scoring_failure_has_safe_message_and_original_cause() -> None:
    failure = RuntimeError("内部调试内容")
    scorer = Mock()
    scorer.score.side_effect = failure
    with pytest.raises(RerankError, match="没有返回替代排名") as caught:
        Reranker(scorer).rerank("合法查询", [_hit(1)])
    assert caught.value.__cause__ is failure
    assert "内部调试内容" not in str(caught.value)


@pytest.mark.parametrize("options", [
    {"model_name": "another-model"}, {"revision": "main"}, {"revision": "f" * 39},
    {"device": "mps"}, {"device": []}, {"dtype": {}}, {"dtype": "bfloat16"},
    {"device": "cpu", "dtype": "float16"},
    {"max_length": True}, {"max_length": 0}, {"max_length": 8193},
    {"batch_size": 0}, {"batch_size": 2.0}, {"batch_size": 101},
    {"max_candidates": 0}, {"max_candidates": 101}, {"top_k": False}, {"top_k": 11},
    {"cpu_threads": 0}, {"cpu_threads": True}, {"local_files_only": False},
])
def test_config_rejects_invalid_or_unbounded_options(options: dict[str, object]) -> None:
    with pytest.raises(RerankError):
        RerankConfig(**options)


def test_default_config_is_fixed_and_frozen_and_cuda_float16_is_explicit() -> None:
    config = RerankConfig()
    assert config.model_name == MODEL_NAME
    assert config.revision == MODEL_REVISION
    assert len(config.revision) == 40
    assert (config.max_length, config.batch_size, config.max_candidates, config.top_k) == (512, 2, 10, 5)
    assert config.local_files_only is True
    assert replace(config, device="cuda", dtype="float16").dtype == "float16"
    with pytest.raises(FrozenInstanceError):
        config.top_k = 2


class FakeTensor:
    """仅实现适配器需要的张量操作；测试不依赖torch安装。"""

    def __init__(self, data: list) -> None:
        self.data = data

    @property
    def shape(self) -> tuple[int, ...]:
        return (len(self.data), len(self.data[0])) if self.data and isinstance(self.data[0], list) else (len(self.data),)

    def sum(self, *, dim: int) -> "FakeTensor":
        assert dim == 1
        return FakeTensor([sum(row) for row in self.data])

    def tolist(self) -> list:
        return self.data

    def to(self, device: str) -> "FakeTensor":
        assert device in {"cpu", "cuda"}
        return self

    def float(self) -> "FakeTensor":
        return self

    def cpu(self) -> "FakeTensor":
        return self

    def __getitem__(self, key: tuple[slice, int]) -> "FakeTensor":
        return FakeTensor([row[key[1]] for row in self.data[key[0]]])


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        tokenizer_loads=[], model_loads=[], tokenizer_calls=[], threads=[], batches=[],
        load_error=None, inference_error=None, logits_width=1, values=[-1.0, 2.0, 0.5],
        consumed=0, eval_calls=0, devices=[], cuda_available=True,
    )

    class Tokenizer:
        def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
            assert add_special_tokens is False
            # 此处字符即fake token；不把这个替身计数当成真实模型分词结果。
            return list(range(len(text)))

        def num_special_tokens_to_add(self, *, pair: bool) -> int:
            assert pair is True
            return 4

        def __call__(self, queries: list[str], passages: list[str], **kwargs: object) -> dict[str, FakeTensor]:
            state.tokenizer_calls.append((queries, passages, kwargs))
            assert kwargs["truncation"] == "only_second"
            lengths = [min(len(query) + len(text) + 4, kwargs["max_length"])
                       for query, text in zip(queries, passages, strict=True)]
            width = max(lengths)
            return {
                "input_ids": FakeTensor([[1] * length + [0] * (width - length) for length in lengths]),
                "attention_mask": FakeTensor([[1] * length + [0] * (width - length) for length in lengths]),
            }

    class Model:
        def to(self, device: str) -> None:
            state.devices.append(device)

        def eval(self) -> None:
            state.eval_calls += 1

        def __call__(self, **inputs: FakeTensor) -> SimpleNamespace:
            if state.inference_error is not None:
                raise state.inference_error
            size = len(inputs["input_ids"].data)
            state.batches.append(size)
            values = [state.values[(state.consumed + index) % len(state.values)] for index in range(size)]
            state.consumed += size
            return SimpleNamespace(logits=FakeTensor([[value] * state.logits_width for value in values]))

    def tokenizer_load(model_name: str, **kwargs: object) -> Tokenizer:
        state.tokenizer_loads.append((model_name, kwargs))
        if state.load_error is not None:
            raise state.load_error
        return Tokenizer()

    def model_load(model_name: str, **kwargs: object) -> Model:
        state.model_loads.append((model_name, kwargs))
        return Model()

    torch = ModuleType("torch")
    torch.float32 = "fake-float32"
    torch.float16 = "fake-float16"
    torch.set_num_threads = state.threads.append
    torch.inference_mode = nullcontext
    torch.cuda = SimpleNamespace(is_available=lambda: state.cuda_available)
    transformers = ModuleType("transformers")
    transformers.AutoTokenizer = SimpleNamespace(from_pretrained=tokenizer_load)
    transformers.AutoModelForSequenceClassification = SimpleNamespace(from_pretrained=model_load)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    return state


def test_constructor_and_empty_score_neither_import_nor_load_heavy_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name in {"torch", "transformers"}:
            raise AssertionError("不应导入模型依赖")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    scorer = LocalCrossEncoder()
    assert scorer.cache_dir == str(PROJECT_ROOT / "models" / "huggingface")
    assert scorer.loaded is False
    assert scorer.load_ms is None
    assert scorer.score("合法查询", []) == []
    assert scorer.last_diagnostics == []
    assert scorer.last_load_ms == scorer.last_inference_ms == 0.0


def test_local_backend_loads_once_uses_fixed_safe_cache_and_batches(backend: SimpleNamespace, tmp_path) -> None:
    config = RerankConfig()
    scorer = LocalCrossEncoder(config, cache_dir=tmp_path)
    assert not scorer.loaded
    first = scorer.score("中文查询", ["甲", "乙", "丙"])
    initial_load = scorer.load_ms
    second = scorer.score("中文查询", ["甲", "乙", "丙"])

    assert first == second == [-1.0, 2.0, 0.5]
    assert scorer.loaded
    assert len(backend.tokenizer_loads) == len(backend.model_loads) == 1
    assert backend.batches == [2, 1, 2, 1]
    assert backend.threads == [4]
    assert backend.eval_calls == 1
    assert backend.devices == ["cpu"]
    assert scorer.load_ms == initial_load
    assert initial_load is not None and initial_load >= 0
    assert scorer.last_load_ms == 0
    assert scorer.last_total_ms >= scorer.last_inference_ms >= 0
    assert backend.tokenizer_loads[0] == (MODEL_NAME, {
        "revision": MODEL_REVISION, "cache_dir": str(tmp_path),
        "local_files_only": True, "trust_remote_code": False,
    })
    assert backend.model_loads[0][1] == {
        **backend.tokenizer_loads[0][1], "use_safetensors": True, "dtype": "fake-float32",
    }
    assert backend.tokenizer_calls[0][2] == {
        "padding": True, "truncation": "only_second", "max_length": 512,
        "return_tensors": "pt", "return_attention_mask": True,
    }


def test_only_passage_is_truncated_diagnostics_exclude_padding_and_original_is_kept(backend: SimpleNamespace) -> None:
    config = RerankConfig(max_length=12, top_k=2)
    scorer = LocalCrossEncoder(config)
    candidates = [_hit(1, "长" * 30), _hit(2, "短")]
    result = Reranker(scorer, config=config).rerank("查询", candidates)

    assert scorer.last_diagnostics == [
        {"original_pair_tokens": 36, "input_tokens": 12, "passage_truncated": True},
        {"original_pair_tokens": 7, "input_tokens": 7, "passage_truncated": False},
    ]
    assert backend.tokenizer_calls[0][0] == ["查询", "查询"]
    assert candidates[0].text == "长" * 30
    assert result[1].hit is candidates[0]
    assert result[1].hit.text == "长" * 30
    assert result[1].hit.metadata == candidates[0].metadata


def test_query_token_budget_is_rejected_before_any_inference(backend: SimpleNamespace) -> None:
    scorer = LocalCrossEncoder(RerankConfig(max_length=8))
    with pytest.raises(RerankError, match="查询token"):
        scorer.score("查询太长", ["中文片段"])
    assert backend.batches == []
    assert backend.tokenizer_calls == []
    assert scorer.last_diagnostics == []


@pytest.mark.parametrize("passages", [None, "片段", [""], [" \n "], [1], ["合法"] * 11])
def test_direct_local_score_checks_inputs_before_loading(backend: SimpleNamespace, passages: object) -> None:
    scorer = LocalCrossEncoder()
    with pytest.raises(RerankError):
        scorer.score("合法查询", passages)
    assert not scorer.loaded
    assert backend.tokenizer_loads == []


def test_missing_cache_fails_explicitly_without_replacement_scores_and_can_be_retried(backend: SimpleNamespace) -> None:
    scorer = LocalCrossEncoder()
    failure = OSError("缓存缺失的内部路径")
    backend.load_error = failure
    with pytest.raises(RerankError, match="加载失败") as caught:
        scorer.score("合法查询", ["中文片段"])
    assert caught.value.__cause__ is failure
    assert not scorer.loaded
    assert scorer.load_ms is None
    assert scorer.last_diagnostics == []
    backend.load_error = None
    assert scorer.score("合法查询", ["中文片段"]) == [-1.0]
    assert len(backend.tokenizer_loads) == 2


def test_inference_failure_keeps_model_but_returns_no_partial_diagnostics(backend: SimpleNamespace) -> None:
    scorer = LocalCrossEncoder()
    backend.inference_error = RuntimeError("内部模型错误")
    with pytest.raises(RerankError, match="没有返回替代分数") as caught:
        scorer.score("合法查询", ["中文片段"])
    assert caught.value.__cause__ is backend.inference_error
    assert scorer.loaded
    assert scorer.last_diagnostics == []


@pytest.mark.parametrize("width,values", [(2, [0.5]), (1, [float("nan")]), (1, [float("inf")])])
def test_model_must_return_one_finite_raw_logit_per_pair(backend: SimpleNamespace, width: int, values: list[float]) -> None:
    backend.logits_width, backend.values = width, values
    with pytest.raises(RerankError):
        LocalCrossEncoder().score("合法查询", ["中文片段"])


def test_unavailable_cuda_fails_before_loading_model(backend: SimpleNamespace) -> None:
    backend.cuda_available = False
    scorer = LocalCrossEncoder(RerankConfig(device="cuda", dtype="float16"))
    with pytest.raises(RerankError, match="CUDA设备不可用"):
        scorer.score("合法查询", ["中文片段"])
    assert backend.model_loads == backend.tokenizer_loads == []


def test_invalid_tokenizer_length_is_rejected_before_inference(backend: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    scorer = LocalCrossEncoder()
    scorer._load()
    monkeypatch.setattr(scorer._tokenizer, "encode", lambda *args, **kwargs: [1])
    with pytest.raises(RerankError, match="only_second截断合同"):
        scorer.score("中文查询", ["中文片段"])
    assert backend.batches == []
