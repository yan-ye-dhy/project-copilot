"""D15 CLI门禁、公开候选身份和记录合同；mock评分，不下载或推断真实模型。"""

import builtins
from hashlib import sha256
import json
from pathlib import Path
from shutil import copytree
import socket
import subprocess
import sys

import pytest

from scripts import rerank_smoke as smoke


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts/rerank_smoke.py"


@pytest.fixture(autouse=True)
def no_network_dotenv_or_real_model(monkeypatch: pytest.MonkeyPatch) -> None:
    original_open = Path.open
    original_import = builtins.__import__

    def guarded_open(path, *args, **kwargs):
        assert path.name != ".env", "D15本地smoke不得读取真实.env"
        return original_open(path, *args, **kwargs)

    def guarded_import(name, *args, **kwargs):
        assert name.split(".")[0] not in {"torch", "transformers"}, "mock测试不得加载真实模型依赖"
        return original_import(name, *args, **kwargs)

    def no_connect(*args, **kwargs):
        raise AssertionError("D15本地smoke不得访问网络")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(socket.socket, "connect", no_connect)


@pytest.fixture
def fake_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    from project_copilot.rag import rerank

    cache = tmp_path / "cache"
    config = rerank.RerankConfig()
    snapshot = cache / "models--BAAI--bge-reranker-v2-m3" / "snapshots" / config.revision
    snapshot.mkdir(parents=True)
    state = {"instances": [], "calls": [], "cache": cache}

    class FakeCrossEncoder:
        def __init__(self, config, *, cache_dir):
            self.config, self.cache_dir = config, cache_dir
            self.load_ms = None
            self.last_load_ms = self.last_inference_ms = self.last_total_ms = 0.0
            self.last_diagnostics = []
            state["instances"].append(self)

        def score(self, query, passages):
            state["calls"].append((query, list(passages)))
            self.last_load_ms = 17.0 if self.load_ms is None else 0.0
            self.load_ms = 17.0
            self.last_inference_ms = 9.0 if len(state["calls"]) == 1 else 6.0
            self.last_total_ms = self.last_load_ms + self.last_inference_ms + 2.0
            self.last_diagnostics = [{
                "original_pair_tokens": 600 if index == 9 else 100 + index,
                "input_tokens": 512 if index == 9 else 100 + index,
                "passage_truncated": index == 9,
            } for index in range(len(passages))]
            return [float(index) - 5.0 for index in range(len(passages))]

    monkeypatch.setattr(rerank, "LocalCrossEncoder", FakeCrossEncoder)
    return state


def test_cli_without_local_needs_only_standard_library() -> None:
    result = subprocess.run(
        [sys.executable, "-S", str(SCRIPT)], cwd=PROJECT_ROOT,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 2
    assert "需显式添加--local" in result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.stderr


def test_missing_local_gate_precedes_input_reads_and_project_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def forbid_imports(name, *args, **kwargs):
        assert name.split(".")[0] not in {"evals", "project_copilot", "torch", "transformers"}
        return original_import(name, *args, **kwargs)

    def forbid_reads(*args, **kwargs):
        pytest.fail("缺少--local时不得读取输入或模型缓存")

    monkeypatch.setattr(builtins, "__import__", forbid_imports)
    monkeypatch.setattr(Path, "open", forbid_reads)
    monkeypatch.setattr(Path, "is_dir", forbid_reads)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--cache-dir", "/private/not-read"])
    with pytest.raises(SystemExit) as result:
        smoke.main()
    assert result.value.code == 2


def test_real_archive_is_verified_and_exact_original_candidates_are_used(fake_model: dict) -> None:
    result = smoke.run_smoke(cache_dir=fake_model["cache"], device="cpu", dtype="float32")
    archive_rows = smoke._rows(smoke.ARCHIVE_DIR / "hybrid_rankings.jsonl")
    archived = next(row for row in archive_rows if row["question_id"] == smoke.QUESTION_ID)
    assert fake_model["calls"] == [(archived["query"], [hit["text"] for hit in archived["hits"]])] * 2
    assert len(fake_model["instances"]) == 1
    assert fake_model["instances"][0].cache_dir == fake_model["cache"]
    assert result["question_id"] == "pc-d7-012"
    assert result["source"]["archive_verified"] is True
    assert result["source"]["run_id"] == "hybrid-v2-20261022-01"
    assert result["source"]["hybrid_rankings_sha256"] == sha256(
        (smoke.ARCHIVE_DIR / "hybrid_rankings.jsonl").read_bytes()
    ).hexdigest()
    assert result["external_model_api_calls"] == result["qdrant_requests"] == 0
    assert result["local_rerank_invocations"] == 2 and result["scored_pairs"] == 20
    assert result["config"] == {
        "model_name": "BAAI/bge-reranker-v2-m3", "revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        "device": "cpu", "dtype": "float32", "max_length": 512, "batch_size": 2,
        "max_candidates": 10, "top_k": 5, "cpu_threads": 4, "local_files_only": True,
    }
    assert result["model_load_ms"] == 17.0
    assert result["first_pass"]["timings_ms"]["model_load"] == 17.0
    assert result["warm_repeat"]["timings_ms"]["model_load"] == 0.0
    assert result["first_pass"]["timings_ms"]["model_inference"] == 9.0
    assert result["warm_repeat"]["timings_ms"]["model_inference"] == 6.0
    for pass_name in ("first_pass", "warm_repeat"):
        current = result[pass_name]
        assert current["truncated_pairs"] == 1
        assert len(current["scored_candidates"]) == 10
        for rank, (item, hit) in enumerate(zip(current["scored_candidates"], archived["hits"], strict=True), 1):
            assert item["candidate_rank"] == rank
            assert item["chunk_id"] == hit["metadata"]["chunk_id"]
            assert item["original_score"] == hit["score"]
            assert item["text_sha256"] == sha256(hit["text"].encode("utf-8")).hexdigest()
            assert item["raw_logit"] == float(rank - 1) - 5.0
        for rerank_rank, (item, candidate_rank) in enumerate(zip(current["selected_hits"], range(10, 5, -1), strict=True), 1):
            assert item["rerank_rank"] == rerank_rank
            assert item["candidate_rank"] == candidate_rank
            assert item["hit"] == archived["hits"][candidate_rank - 1]
            assert item["rerank_score"] == float(candidate_rank - 1) - 5.0
    assert "recall" not in json.dumps(result).lower()
    assert "mrr" not in json.dumps(result).lower()


def test_local_cli_emits_one_json_result(
    fake_model: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--local", "--cache-dir", str(fake_model["cache"])])
    assert smoke.main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "ok"
    assert len(captured.out.splitlines()) == 1


def test_missing_snapshot_returns_safe_error_without_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from project_copilot.rag import rerank

    def forbid_model(*args, **kwargs):
        pytest.fail("模型快照不存在时不得创建评分器")

    monkeypatch.setattr(rerank, "LocalCrossEncoder", forbid_model)
    secret_path = tmp_path / "private-missing-cache"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--local", "--cache-dir", str(secret_path)])
    assert smoke.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "本地模型快照缺失" in json.loads(captured.err)["message"]
    assert str(secret_path) not in captured.err
    assert "Traceback" not in captured.err


def test_direct_script_local_path_can_import_verifier_and_report_missing_model(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--local", "--cache-dir", str(tmp_path / "no-snapshot")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "本地模型快照缺失" in json.loads(result.stderr)["message"]
    assert str(tmp_path) not in result.stderr
    assert "Traceback" not in result.stderr


def test_changed_public_archive_fails_before_model_construction(
    fake_model: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "changed-archive"
    copytree(smoke.ARCHIVE_DIR, archive)
    rankings = archive / "hybrid_rankings.jsonl"
    rankings.write_bytes(rankings.read_bytes() + b"\n")
    monkeypatch.setattr(smoke, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--local", "--cache-dir", str(fake_model["cache"])])
    assert smoke.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err)["status"] == "error"
    assert str(archive) not in captured.err
    assert fake_model["instances"] == fake_model["calls"] == []


def test_inference_failure_is_safe_and_not_retried(
    fake_model: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from project_copilot.rag import rerank

    attempts = []
    original = rerank.LocalCrossEncoder.score

    def fail_once(self, query, passages):
        attempts.append(1)
        original(self, query, passages)
        raise RuntimeError("SECRET /private/model-path " + query + passages[0])

    monkeypatch.setattr(rerank.LocalCrossEncoder, "score", fail_once)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--local", "--cache-dir", str(fake_model["cache"])])
    assert smoke.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert attempts == [1]
    assert len(fake_model["calls"]) == 1
    assert json.loads(captured.err)["status"] == "error"
    assert "SECRET" not in captured.err and "private" not in captured.err
    assert fake_model["calls"][0][0] not in captured.err


def test_incomplete_diagnostics_fail_instead_of_claiming_success(
    fake_model: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from project_copilot.rag import rerank

    original = rerank.LocalCrossEncoder.score

    def incomplete(self, query, passages):
        scores = original(self, query, passages)
        self.last_diagnostics.pop()
        return scores

    monkeypatch.setattr(rerank.LocalCrossEncoder, "score", incomplete)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--local", "--cache-dir", str(fake_model["cache"])])
    assert smoke.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "诊断不完整" in json.loads(captured.err)["message"]
    assert len(fake_model["calls"]) == 1
