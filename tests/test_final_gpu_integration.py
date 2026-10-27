"""真实CUDA固定缓存模型复用；只读取历史候选，不调用外部模型。"""

import json
import os
from pathlib import Path
import weakref

import pytest

from project_copilot.config import Settings
from project_copilot.rag.final_retrieval import SharedReranker
from project_copilot.schemas.retrieval import RetrievedChunk


pytestmark = pytest.mark.skipif(os.environ.get("PROJECT_COPILOT_RUN_FINAL_GPU") != "1",
                                reason="显式启用本地CUDA模型测试")


def test_cuda_fixed_model_warmup_reuse_and_rerank_identity():
    path = Path("evals/results/hybrid-v2-20261022-01/hybrid_rankings.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = next(item for item in rows if item["question_id"] == "pc-d7-012")
    candidates = [RetrievedChunk.model_validate(item) for item in row["hits"]]
    assert len(candidates) == 10
    runtime = SharedReranker(Settings(_env_file=None))
    try:
        runtime.prepare()
        model = weakref.ref(runtime.scorer._model)
        first = runtime.rerank(row["query"], candidates)
        second = runtime.rerank(row["query"], candidates)
        assert runtime.scorer._model is model() and runtime._warm
        assert runtime.scorer.config.device == "cuda" and runtime.scorer.config.dtype == "float16"
        assert runtime.scorer.config.revision == "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
        assert len(first) == len(second) == 5
        assert [item.hit.metadata.chunk_id for item in first] == [item.hit.metadata.chunk_id for item in second]
        assert first[0].candidate_rank != 1
        print("CUDA rerank original candidate ranks:", [item.candidate_rank for item in first])
        assert all(item.hit.text == candidates[item.candidate_rank-1].text for item in first)
    finally:
        runtime.close()
    assert not runtime.scorer.loaded
    assert model() is None
