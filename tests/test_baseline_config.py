"""D10冻结配置的离线契约、变更门禁和密钥隔离。"""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from evals.baseline_config import (
    BASELINE_COLLECTION, BaselineConfigError, assert_runtime_matches,
    build_baseline_config, fingerprint, validate_baseline_config,
)
from project_copilot.config import Settings
from project_copilot.rag import answering
from project_copilot.schemas.rag import ModelAnswer


DATASET = Path(__file__).resolve().parents[1] / "evals" / "datasets" / "copilot_v0"


@pytest.fixture
def settings() -> Settings:
    # 显式禁用dotenv并换成测试专属环境前缀；绝不读取开发者真实密钥。
    return Settings(
        _env_file=None, _env_prefix="TEST_D10_BASELINE_CONFIG_",
        zhipu_api_key="test-zhipu-secret", deepseek_api_key="test-deepseek-secret",
    )


@pytest.fixture
def config(settings: Settings) -> dict:
    return build_baseline_config(DATASET, settings)


def _rehash(config: dict) -> None:
    payload = {key: value for key, value in config.items() if key != "configuration_fingerprint"}
    config["configuration_fingerprint"] = "baseline-config-v1-" + fingerprint(payload)


def test_build_freezes_all_dev_and_eight_prespecified_categories(config: dict, settings: Settings):
    assert validate_baseline_config(config, DATASET) is config
    assert_runtime_matches(config, settings)
    assert config["evaluation_config"]["split"] == "dev"
    assert config["evaluation_config"]["question_ids"] is None
    assert config["evaluation_config"]["ks"] == [1, 3, 5, 10]
    assert config["evaluation_config"]["ranking_origin"] == "recorded_retrieval"
    assert config["answer_sample_ids"] == [
        "pc-d7-001", "pc-d7-010", "pc-d7-002", "pc-d7-003",
        "pc-d7-009", "pc-d7-013", "pc-d7-017", "pc-d7-020",
    ]
    assert config["answer_sample_selection"] == "first-dev-question-per-category-in-split-order-v1"
    assert config["index"]["collection_name"] == BASELINE_COLLECTION == "pc_eval_dense_v0"
    assert settings.qdrant_collection_name == "pc_knowledge_dense_v0"


def test_config_is_deterministic_and_never_serializes_secrets_or_local_paths(settings: Settings):
    first = build_baseline_config(DATASET, settings)
    second_settings = settings.model_copy(update={
        "zhipu_api_key": "different-secret", "deepseek_api_key": "other-secret",
        "app_name": "Unrelated application label", "manifest_dir": Path("/tmp/private-manifest"),
        "qdrant_collection_name": "pc_knowledge_other",
    })
    second = build_baseline_config(DATASET, second_settings)
    assert first == second
    encoded = json.dumps(first, ensure_ascii=False)
    for sensitive in ("test-zhipu-secret", "test-deepseek-secret", "different-secret", "other-secret",
                      "api_key", "/tmp/private-manifest", "Unrelated application label"):
        assert sensitive not in encoded


def test_prompt_contract_contains_actual_messages_schema_and_binding_rules(config: dict):
    prompt = config["prompt"]
    assert prompt["version"] == "rag-answer-v1"
    assert prompt["system_message"] == answering.SYSTEM_MESSAGE
    assert prompt["user_template"] == answering.USER_TEMPLATE
    assert prompt["mode"] == "zero-shot" and prompt["examples"] == []
    assert prompt["fingerprint"] == answering.prompt_info().fingerprint
    schema = ModelAnswer.model_json_schema()
    assert prompt["answer_schema"] == schema
    assert prompt["answer_schema_hash"] == sha256(
        json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert prompt["context_policy"]["max_chars"] == 6000
    assert prompt["context_policy"]["truncation"] == "whole-chunks;skip-over-budget"
    assert "format once" in prompt["variables"]["payload"]


def test_canonical_fingerprint_ignores_dict_order_and_rejects_non_json_numbers():
    assert fingerprint({"中文": [1, 2], "a": 1}) == fingerprint({"a": 1, "中文": [1, 2]})
    assert fingerprint({"a": 1}) != fingerprint({"a": True})
    with pytest.raises(ValueError):
        fingerprint({"invalid": float("nan")})


@pytest.mark.parametrize("section,key,value", [
    ("generation", "temperature", 0.4),
    ("embedding", "batch_size", 1),
    ("prompt", "system_message", "Silently changed prompt"),
])
def test_stale_fingerprint_rejected(config: dict, section: str, key: str, value: object):
    changed = deepcopy(config)
    changed[section][key] = value
    with pytest.raises(BaselineConfigError):
        validate_baseline_config(changed, DATASET)


@pytest.mark.parametrize("section,key,value", [
    ("evaluation_config", "split", "holdout"),
    ("evaluation_config", "question_ids", ["pc-d7-001"]),
    ("evaluation_config", "ks", [1, 5]),
    ("evaluation_config", "ranking_origin", "synthetic_fixture"),
    ("prompt", "system_message", "Different system message"),
    ("prompt", "examples", [{"question": "example", "answer": "example"}]),
    ("prompt", "answer_schema_hash", "0" * 64),
    ("generation", "max_retries", 1),
    ("generation", "thinking", "enabled"),
    ("generation", "stream", True),
    ("generation", "max_tokens", True),
    ("generation", "provider_url", "http://insecure.example/"),
    ("generation", "provider_url", "https://secret@example.com/"),
    ("embedding", "encoding_format", "base64"),
    ("embedding", "dimensions", "2048"),
    ("embedding", "batch_size", 65),
    ("index", "collection_name", "pc_knowledge_dense_v0"),
    ("retrieval", "top_k", 20),
])
def test_rehashed_contract_tampering_still_rejected(config: dict, section: str, key: str, value: object):
    changed = deepcopy(config)
    changed[section][key] = value
    _rehash(changed)
    with pytest.raises(BaselineConfigError):
        validate_baseline_config(changed, DATASET)


def test_unknown_fields_and_holdout_answer_samples_cannot_be_smuggled_in(config: dict):
    for update in ({"unexpected": "field"}, {"answer_sample_ids": ["pc-d7-005"]}):
        changed = deepcopy(config)
        changed.update(update)
        _rehash(changed)
        with pytest.raises(BaselineConfigError):
            validate_baseline_config(changed, DATASET)


@pytest.mark.parametrize("setting,value", [
    ("deepseek_temperature", 0.2), ("deepseek_max_tokens", 512),
    ("deepseek_model", "deepseek-v4-pro"), ("embedding_expected_dimensions", 1024),
    ("embedding_batch_size", 8), ("zhipu_timeout_seconds", 18.0),
    ("deepseek_timeout_seconds", 25.0), ("rag_top_k", 3),
])
def test_runtime_parameter_drift_rejected(config: dict, settings: Settings, setting: str, value: object):
    with pytest.raises(BaselineConfigError):
        assert_runtime_matches(config, settings.model_copy(update={setting: value}))


def test_provider_url_drift_requires_a_new_config(config: dict, settings: Settings):
    changed = Settings(
        _env_file=None, _env_prefix="TEST_D10_BASELINE_CONFIG_",
        deepseek_base_url="https://other-deepseek.example/v1/",
    )
    with pytest.raises(BaselineConfigError):
        assert_runtime_matches(config, changed)
    replacement = build_baseline_config(DATASET, changed)
    assert replacement["configuration_fingerprint"] != config["configuration_fingerprint"]
    assert_runtime_matches(replacement, changed)


def test_runtime_prompt_change_rejected_before_requests(config: dict, settings: Settings, monkeypatch):
    monkeypatch.setattr(answering, "SYSTEM_MESSAGE", answering.SYSTEM_MESSAGE + "\nChanged instruction")
    with pytest.raises(BaselineConfigError):
        assert_runtime_matches(config, settings)


def test_schema_change_does_not_silently_reuse_import_time_schema_hash(config: dict, settings: Settings, monkeypatch):
    old_schema = ModelAnswer.model_json_schema()
    changed_schema = {**old_schema, "description": "New response contract"}
    monkeypatch.setattr(ModelAnswer, "model_json_schema", lambda: changed_schema)
    with pytest.raises(BaselineConfigError):
        assert_runtime_matches(config, settings)


def test_dataset_version_change_invalidates_config(config: dict, tmp_path: Path):
    copied = tmp_path / "dataset"
    shutil.copytree(DATASET, copied)
    questions = copied / "questions.jsonl"
    questions.write_bytes(questions.read_bytes() + b"\n")
    with pytest.raises(BaselineConfigError):
        validate_baseline_config(config, copied)


def test_build_does_not_modify_dataset_or_output_files(settings: Settings):
    paths = [path for path in DATASET.rglob("*") if path.is_file()]
    before = {str(path): path.read_bytes() for path in paths}
    build_baseline_config(DATASET, settings)
    assert {str(path): path.read_bytes() for path in paths} == before
