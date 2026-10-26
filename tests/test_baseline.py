"""D10采集编排的离线依赖验收；固定fake排名不代表真实Dense效果。"""

import json
import socket
import sys
from collections.abc import Sequence
from pathlib import Path
from shutil import copytree
from hashlib import sha256
from math import ceil

import pytest

from evals import baseline
from evals.baseline_config import BASELINE_COLLECTION, build_baseline_config
from project_copilot.config import Settings
from project_copilot.infrastructure.models import ChatGeneration
from project_copilot.infrastructure.qdrant import QdrantRequestError
from project_copilot.rag import answering
from project_copilot.schemas.documents import DocumentChunk
from project_copilot.schemas.retrieval import RetrievedChunk


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class FakeEmbeddings:
    model = "embedding-3"
    expected_dimensions = 2048
    batch_size = 64

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.5] * self.expected_dimensions for _ in texts]


class FakeStore:
    """保持Dense SDK适配器的方法签名，按稳定chunk_id保存合成点。"""

    vector_name = "dense"
    dimensions = 2048

    def __init__(self, collection_name: str, *, fail_queries: set[int] | None = None) -> None:
        self.collection_name = collection_name
        self.points: dict[str, DocumentChunk] = {}
        self.ensure_calls = 0
        self.query_calls: list[dict] = []
        self.upsert_calls: list[dict] = []
        self.fail_queries = fail_queries or set()

    def ensure_collection(self) -> None:
        self.ensure_calls += 1

    def upsert(self, chunks, vectors, *, dense_index_version: str) -> None:
        assert len(chunks) == len(vectors)
        assert all(len(vector) == self.dimensions for vector in vectors)
        self.upsert_calls.append({"count": len(chunks), "version": dense_index_version})
        for chunk in chunks:
            self.points[str(chunk.metadata.chunk_id)] = chunk

    def verify_chunks(self, chunks, *, dense_index_version: str) -> None:
        assert dense_index_version
        assert all(self.points.get(str(c.metadata.chunk_id)) == c for c in chunks)

    def query(self, vector, *, document_ids, dense_index_version: str, limit: int = 5):
        assert len(vector) == self.dimensions
        assert len(set(document_ids)) == 5
        self.query_calls.append({"document_ids": document_ids, "version": dense_index_version, "limit": limit})
        if len(self.query_calls) in self.fail_queries:
            raise QdrantRequestError("固定fake检索异常，无真实Qdrant请求")
        allowed = set(document_ids)
        selected = [chunk for chunk in self.points.values() if chunk.metadata.document_id in allowed][:limit]
        return [RetrievedChunk(**chunk.model_dump(), score=1.0 - index * 0.01) for index, chunk in enumerate(selected)]


class FakeChat:
    model = "deepseek-flash"
    temperature = 0.0
    max_tokens = 1024

    def __init__(self, *, content: str | None = None) -> None:
        self.messages: list[list[dict[str, str]]] = []
        self.content = content or '{"status":"answered","answer":"固定离线回答。","citation_numbers":[1]}'

    def generate_json(self, messages: list[dict[str, str]]) -> ChatGeneration:
        self.messages.append(messages)
        return ChatGeneration(content=self.content, model=self.model, usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})


@pytest.fixture(autouse=True)
def no_network_or_dotenv(monkeypatch):
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path.name != ".env", "离线D10测试禁止读取真实.env"
        return original_open(path, *args, **kwargs)

    def no_connect(*args, **kwargs):
        raise AssertionError("离线D10测试禁止网络请求")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(socket.socket, "connect", no_connect)


@pytest.fixture
def case(tmp_path: Path):
    dataset = tmp_path / "dataset"
    copytree(PROJECT_ROOT / "evals/datasets/copilot_v0", dataset)
    settings = Settings(
        _env_file=None, _env_prefix="COPILOT_D10_OFFLINE_ONLY_",
        zhipu_api_key=None, deepseek_api_key=None,
        qdrant_collection_name=BASELINE_COLLECTION,
        manifest_dir=tmp_path / "manifests",
    )
    config = build_baseline_config(dataset, settings)
    config_path = tmp_path / "config.json"
    write_json(config_path, config)
    return {
        "dataset": dataset, "config": config, "config_path": config_path,
        "settings": settings, "cache_dir": tmp_path / "cache",
        "output": tmp_path / "baseline-run",
        "embeddings": FakeEmbeddings(),
        "store": FakeStore(settings.qdrant_collection_name),
    }


def collect(case, *, chat=None, output: Path | None = None):
    return baseline.collect_baseline(
        case["dataset"], case["config_path"], output or case["output"],
        case["cache_dir"], case["settings"],
        embeddings=case["embeddings"], store=case["store"], chat=chat,
    )


def test_offline_plan_reports_exact_scope_without_output_or_factories(case, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("离线plan不构造客户端或加载配置")

    for name in ("load_settings", "create_embedding_client", "create_qdrant_store", "create_chat_client"):
        monkeypatch.setattr(baseline, name, forbidden)
    plan = baseline.plan_baseline(case["dataset"], case["config_path"])
    assert plan["mode"] == "offline_plan"
    assert (plan["documents"], plan["chunks"], plan["questions"]) == (5, 16, 30)
    assert (plan["cold_embedding_batches"], plan["smoke_questions"], plan["max_answer_calls"]) == (3, 2, 8)
    assert plan["model_calls"] == 0
    assert plan["answer_sample_ids"] == case["config"]["answer_sample_ids"]
    assert not case["output"].exists()
    assert not case["cache_dir"].exists()


def test_plan_batch_estimate_respects_configured_supplier_batch_limit(case) -> None:
    settings = case["settings"].model_copy(update={"embedding_batch_size": 8})
    write_json(case["config_path"], build_baseline_config(case["dataset"], settings))
    plan = baseline.plan_baseline(case["dataset"], case["config_path"])
    assert plan["cold_embedding_batches"] == ceil(16 / 8) + ceil(2 / 8) + ceil(28 / 8)


def test_fresh_collect_batches_once_then_reuses_cache_for_ingestion_and_all_queries(case) -> None:
    result = collect(case)
    assert result["status"] == "ok"
    assert result["complete"] is True
    assert result["day10_accepted"] is False
    assert result["model_calls"] == {"embedding": 3, "chat": 0}
    assert [len(batch) for batch in case["embeddings"].calls] == [16, 2, 28]
    assert len(case["store"].points) == 16
    assert len(case["store"].upsert_calls) == 5
    assert len(case["store"].query_calls) == 30
    assert {item["limit"] for item in case["store"].query_calls} == {10}
    rankings = read_jsonl(case["output"] / "rankings.jsonl")
    assert len(rankings) == 30
    assert all(row["error"] is None for row in rankings)
    summary = read_json(case["output"] / "retrieval/summary.json")
    assert (summary["question_count"], summary["answerable_count"], summary["unanswerable_count"]) == (30, 26, 4)
    assert len(read_jsonl(case["output"] / "query_vectors.jsonl")) == 30
    assert len(read_json(case["output"] / "index_manifest.json")["documents"]) == 5
    batches = read_json(case["output"] / "embedding_batches.json")
    assert [event["cache"]["miss_texts"] for event in batches] == [16, 2, 28]
    assert all(event["elapsed_ms"] >= 0 for event in batches)
    manifest = read_json(case["output"] / "manifest.json")
    assert manifest["cache_stats"]["api_calls"] == 3
    assert manifest["actual_billed_cost"] is None
    assert manifest["embedding_usage"] is None
    assert result["human_review"] == "not_collected"


def test_warm_new_run_uses_no_embedding_requests_and_preserves_ranking_metrics(case) -> None:
    first = collect(case)
    case["embeddings"] = FakeEmbeddings()
    case["store"] = FakeStore(case["settings"].qdrant_collection_name)
    second_output = case["output"].with_name("warm-run")
    second = collect(case, output=second_output)

    assert case["embeddings"].calls == []
    assert second["model_calls"] == {"embedding": 0, "chat": 0}
    assert second["at_k"] == first["at_k"]
    manifest = read_json(second_output / "manifest.json")
    assert manifest["cache_stats"]["miss_texts"] == 0
    assert manifest["cache_stats"]["hit_texts"] > 0
    assert len(read_jsonl(second_output / "rankings.jsonl")) == 30
    assert read_json(second_output / "embedding_batches.json")[0]["cache"]["hit_texts"] == 16


def test_smoke_failure_retains_partial_records_and_skips_remaining_paid_batches_and_answers(case) -> None:
    case["store"].fail_queries = {1}
    chat = FakeChat()
    with pytest.raises(ValueError, match="smoke"):
        collect(case, chat=chat)
    assert [len(batch) for batch in case["embeddings"].calls] == [16, 2]
    assert len(case["store"].query_calls) == 2
    assert chat.messages == []
    status = read_json(case["output"] / "status.json")
    assert status["state"] == "failed"
    assert status["complete"] is False
    assert status["model_calls"] == {"embedding": 2, "chat": 0}
    rows = read_jsonl(case["output"] / "rankings.jsonl")
    assert len(rows) == 2
    assert rows[0]["error"]["type"] == "retrieval_failed"
    assert rows[1]["error"] is None
    assert not (case["output"] / "retrieval").exists()
    assert not (case["output"] / "manifest.json").exists()


def test_failure_after_smoke_stays_in_full_dev_denominator(case) -> None:
    case["store"].fail_queries = {3}
    result = collect(case)
    rows = read_jsonl(case["output"] / "rankings.jsonl")
    scored = read_jsonl(case["output"] / "retrieval/retrieval.jsonl")
    summary = read_json(case["output"] / "retrieval/summary.json")
    assert len(rows) == len(scored) == 30
    assert rows[2]["hits"] == []
    assert rows[2]["error"]["type"] == "retrieval_failed"
    assert scored[2]["status"] == "retrieval_error"
    assert result["retrieval_errors"] == summary["retrieval_error_count"] == 1
    assert summary["answerable_count"] == 26
    assert summary["question_count"] == 30
    assert set(scored[2]["metrics"]["at_k"]["10"].values()) == {0.0, False}


def test_existing_output_is_refused_before_models_or_storage_and_marker_survives(case) -> None:
    case["output"].mkdir()
    marker = case["output"] / "existing.txt"
    marker.write_text("旧实验不覆盖", encoding="utf-8")
    with pytest.raises(ValueError, match="覆盖"):
        collect(case)
    assert marker.read_text(encoding="utf-8") == "旧实验不覆盖"
    assert list(case["output"].iterdir()) == [marker]
    assert case["embeddings"].calls == []
    assert case["store"].ensure_calls == 0
    assert not case["cache_dir"].exists()


def test_prompt_drift_is_rejected_before_output_or_requests(case, monkeypatch) -> None:
    monkeypatch.setattr(answering, "SYSTEM_MESSAGE", answering.SYSTEM_MESSAGE + "\n离线测试漂移")
    with pytest.raises(ValueError, match="Prompt|契约"):
        collect(case)
    assert not case["output"].exists()
    assert case["embeddings"].calls == []
    assert case["store"].ensure_calls == 0


def test_answer_samples_reuse_saved_retrieval_and_preserve_raw_messages_and_pending_review(case) -> None:
    chat = FakeChat()
    result = collect(case, chat=chat)
    rows = read_jsonl(case["output"] / "answers.jsonl")
    review = read_jsonl(case["output"] / "review.jsonl")
    assert len(rows) == len(review) == len(chat.messages) == 8
    assert {row["category"] for row in rows} == {"knowledge", "terminology", "exact_match", "paraphrase", "chinese_long", "cross_passage", "unanswerable", "follow_up"}
    assert [row["question_id"] for row in rows] == case["config"]["answer_sample_ids"]
    assert result["model_calls"] == {"embedding": 3, "chat": 8}
    assert len(case["store"].query_calls) == 30
    assert [len(batch) for batch in case["embeddings"].calls] == [16, 2, 28]
    for index, row in enumerate(rows):
        assert row["raw_content"] == chat.content
        assert row["messages"] == chat.messages[index]
        assert [message["role"] for message in row["messages"]] == ["system", "user"]
        assert row["actual_model"] == chat.model
        assert row["usage"]["total_tokens"] == 20
        assert row["answer"]["citations"][0]["number"] == 1
        assert row["error"] is None
        assert row["gold_answerable"] in (True, False)
    assert all(row["review_status"] == "pending" and row["answer_correct"] is None and row["claims"] == [] for row in review)
    assert result["human_review"] == "pending"
    manifest = read_json(case["output"] / "manifest.json")
    assert "review.jsonl" not in manifest["files"]
    assert "answers.jsonl" in manifest["files"]
    assert manifest["day10_accepted"] is False


def test_invalid_generated_json_keeps_raw_failure_and_review_pending_without_retry(case) -> None:
    chat = FakeChat(content="this is not JSON")
    result = collect(case, chat=chat)
    rows = read_jsonl(case["output"] / "answers.jsonl")
    assert result["answer_errors"] == 8
    assert result["model_calls"]["chat"] == 8
    assert all(row["raw_content"] == chat.content and row["answer"] is None and row["error"]["type"] == "answer_failed" for row in rows)
    assert len(chat.messages) == 8
    assert all(row["review_status"] == "pending" for row in read_jsonl(case["output"] / "review.jsonl"))


def test_machine_artifacts_verify_and_tampering_fails_while_review_remains_editable(case) -> None:
    collect(case, chat=FakeChat())
    verified = baseline.verify_baseline(case["dataset"], case["output"])
    assert verified["state"] == "verified"
    assert verified["human_review"] == "pending"
    assert verified["day10_accepted"] is False
    review_path = case["output"] / "review.jsonl"
    review_path.write_text(review_path.read_text() + "\n", encoding="utf-8")
    assert baseline.verify_baseline(case["dataset"], case["output"])["state"] == "verified"
    answers_path = case["output"] / "answers.jsonl"
    answers_path.write_text(answers_path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="变化"):
        baseline.verify_baseline(case["dataset"], case["output"])


def test_collection_rejects_writing_results_or_cache_inside_frozen_dataset(case) -> None:
    case["cache_dir"] = case["dataset"] / "bad-cache"
    with pytest.raises(ValueError, match="冻结"):
        collect(case)
    assert not case["output"].exists()
    assert case["embeddings"].calls == []
    assert case["store"].ensure_calls == 0


def test_default_collection_uses_frozen_follow_up_queries_and_preserves_all_inputs(case) -> None:
    before = {path: sha256(path.read_bytes()).hexdigest() for path in case["dataset"].rglob("*") if path.is_file()}
    collect(case)
    questions = {row["question_id"]: row for row in read_jsonl(case["dataset"] / "questions.jsonl")}
    rankings = read_jsonl(case["output"] / "rankings.jsonl")
    assert all(row["query"] == (questions[row["question_id"]]["standalone_question"] or questions[row["question_id"]]["question"]) for row in rankings)
    assert any(questions[row["question_id"]]["standalone_question"] for row in rankings)
    assert {path: sha256(path.read_bytes()).hexdigest() for path in before} == before


def test_cli_collect_without_live_refuses_before_settings_or_clients(case, monkeypatch, capsys) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("未授权collect禁止加载.env或创建客户端")

    for name in ("load_settings", "create_embedding_client", "create_qdrant_store", "create_chat_client"):
        monkeypatch.setattr(baseline, name, forbidden)
    monkeypatch.setattr(sys, "argv", [
        "baseline", "collect", "--dataset", str(case["dataset"]),
        "--config", str(case["config_path"]), "--output-dir", str(case["output"]),
        "--cache-dir", str(case["cache_dir"]),
    ])
    with pytest.raises(SystemExit) as exited:
        baseline.main()
    assert exited.value.code == 2
    assert "--live" in capsys.readouterr().err
    assert not case["output"].exists()


def test_verify_rejects_manifest_that_omits_a_required_artifact(case) -> None:
    collect(case)
    manifest_path = case["output"] / "manifest.json"
    manifest = read_json(manifest_path)
    del manifest["files"]["rankings.jsonl"]
    write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match="遗漏"):
        baseline.verify_baseline(case["dataset"], case["output"])


def test_verify_rejects_incomplete_status_even_if_its_hash_is_updated(case) -> None:
    collect(case)
    status_path = case["output"] / "status.json"
    status = read_json(status_path)
    status.update(state="failed", complete=False)
    write_json(status_path, status)
    manifest_path = case["output"] / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["files"]["status.json"] = sha256(status_path.read_bytes()).hexdigest()
    write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match="未完成"):
        baseline.verify_baseline(case["dataset"], case["output"])
