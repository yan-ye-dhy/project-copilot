"""D10离线基线配置：冻结显式白名单参数，不读取或序列化密钥。"""

from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator

from evals.freeze_dataset import verify_freeze
from evals.validate_dataset import CATEGORIES, DatasetValidationError
from project_copilot.config import Settings
from project_copilot.rag import answering
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.rag import ModelAnswer
from project_copilot.schemas.retrieval import DenseIndexSpec


BASELINE_COLLECTION = "pc_eval_dense_v0"
SELECTION_RULE = "first-dev-question-per-category-in-split-order-v1"
FINGERPRINT_PREFIX = "baseline-config-v1-"


class BaselineConfigError(ValueError):
    """配置不一致时停止；错误文本不包含密钥或供应商响应。"""


class _Parameters(BaseModel):
    """白名单既用于构建，也用于校验从磁盘加载的配置。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    zhipu_base_url: HttpUrl
    embedding_model: Literal["embedding-3"]
    embedding_expected_dimensions: int = Field(ge=1)
    embedding_batch_size: int = Field(ge=1, le=64)
    zhipu_timeout_seconds: float = Field(gt=0, le=120, allow_inf_nan=False)
    deepseek_base_url: HttpUrl
    deepseek_model: Literal["deepseek-flash", "deepseek-v4-pro"]
    deepseek_temperature: float = Field(ge=0, le=2, allow_inf_nan=False)
    deepseek_max_tokens: int = Field(ge=1, le=4096)
    deepseek_timeout_seconds: float = Field(gt=0, le=120, allow_inf_nan=False)
    rag_top_k: int = Field(ge=1, le=10)

    @field_validator("zhipu_base_url", "deepseek_base_url")
    @classmethod
    def check_provider_url(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https" or value.username or value.password or value.query or value.fragment:
            raise ValueError("模型URL须为不含凭据、查询参数或片段的HTTPS地址")
        return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: object) -> str:
    """标准JSON对象的内容指纹；调用方负责明确是否包含自己的指纹字段。"""
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise BaselineConfigError(message)


def _settings_parameters(settings: Settings) -> _Parameters:
    # 不调用settings.model_dump()，防止未来添加字段时把SecretStr或本机路径带入实验配置。
    try:
        return _Parameters.model_validate({
            name: getattr(settings, name) for name in _Parameters.model_fields
        })
    except (ValidationError, AttributeError):
        raise BaselineConfigError("模型配置或回答top-k不适合D10固定基线") from None


def _prompt_contract() -> dict:
    schema = ModelAnswer.model_json_schema()
    schema_hash = sha256(
        json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    _check(schema_hash == answering.ANSWER_SCHEMA_HASH, "回答Schema与运行时Schema指纹不一致")
    info = answering.prompt_info().model_dump(mode="json")
    return {
        "prompt_id": "project-copilot-rag-answer",
        "version": answering.PROMPT_VERSION,
        "fingerprint": info["fingerprint"],
        "system_message": answering.SYSTEM_MESSAGE,
        "user_template": answering.USER_TEMPLATE,
        "system_message_sha256": sha256(answering.SYSTEM_MESSAGE.encode("utf-8")).hexdigest(),
        "user_template_sha256": sha256(answering.USER_TEMPLATE.encode("utf-8")).hexdigest(),
        "mode": "zero-shot",
        "examples": [],
        "variables": {
            "payload": "json.dumps({question,evidence}, ensure_ascii=False); format once",
            "question": "frozen standalone_question for follow_up; original question otherwise",
            "evidence": ["number", "source_name", "page", "chunk_id", "text"],
        },
        "context_policy": {
            "order": "retrieval-order",
            "deduplication": "first-unique-chunk-id",
            "truncation": "whole-chunks;skip-over-budget",
            "max_chars": answering.CONTEXT_MAX_CHARS,
            "length_unit": "unicode-codepoints;sum-of-selected-chunk-text-only",
            "citation_numbering": "1-based-after-selection",
            "skip_blank_chunks": True,
        },
        "answer_schema": schema,
        "answer_schema_hash": schema_hash,
    }


def _runtime_sections(parameters: _Parameters) -> dict:
    splitter = SplitConfig()
    spec = DenseIndexSpec(
        collection_name=BASELINE_COLLECTION,
        embedding_model=parameters.embedding_model,
        dimensions=parameters.embedding_expected_dimensions,
        splitter_version=splitter.index_version,
    )
    return {
        "prompt": _prompt_contract(),
        "generation": {
            "provider": "deepseek", "provider_url": str(parameters.deepseek_base_url),
            "model": parameters.deepseek_model, "temperature": parameters.deepseek_temperature,
            "max_tokens": parameters.deepseek_max_tokens,
            "response_format": {"type": "json_object"}, "thinking": "disabled", "stream": False,
            "timeout_seconds": parameters.deepseek_timeout_seconds, "max_retries": 0,
        },
        "embedding": {
            "provider": "zhipu", "provider_url": str(parameters.zhipu_base_url),
            "model": parameters.embedding_model, "dimensions": parameters.embedding_expected_dimensions,
            "encoding_format": "float", "batch_size": parameters.embedding_batch_size,
            "timeout_seconds": parameters.zhipu_timeout_seconds, "max_retries": 0,
        },
        "index": {**spec.model_dump(mode="json"), "index_version": spec.index_version},
        "retrieval": {"top_k": 10, "answer_top_k": parameters.rag_top_k},
        "splitter": splitter.model_dump(mode="json"),
    }


def _dataset_inputs(dataset_dir: Path) -> tuple[dict, dict, list[dict]]:
    try:
        verified = verify_freeze(dataset_dir)
        split = json.loads((dataset_dir / "split_manifest.json").read_text(encoding="utf-8"))
        questions = [json.loads(line) for line in
                     (dataset_dir / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
        return verified, split, questions
    except (DatasetValidationError, OSError, ValueError, TypeError):
        raise BaselineConfigError("D10输入须为有效的已冻结数据集") from None


def _make_config(dataset_dir: Path, parameters: _Parameters) -> dict:
    verified, split, questions = _dataset_inputs(dataset_dir)
    by_id = {question["question_id"]: question for question in questions}
    # 不按模型输出或分数选题；固定dev顺序下每类第一题，人工支持率仍须另外标注。
    first_by_category = {}
    for question_id in split["dev"]:
        first_by_category.setdefault(by_id[question_id]["category"], question_id)
    _check(set(first_by_category) == set(CATEGORIES), "开发集须覆盖全部八类回答抽检题型")
    runtime = _runtime_sections(parameters)
    config = {
        "schema_version": 1,
        "baseline_id": "dense-v0",
        "dataset_fingerprint": verified["fingerprint"],
        "split_id": split["split_id"],
        "evaluation_config": {
            "schema_version": 1, "dataset_fingerprint": verified["fingerprint"],
            "split": "dev", "question_ids": None, "ks": [1, 3, 5, 10],
            "splitter": runtime.pop("splitter"), "ranking_origin": "recorded_retrieval",
        },
        **runtime,
        "answer_sample_ids": [first_by_category[category] for category in CATEGORIES],
        "answer_sample_selection": SELECTION_RULE,
    }
    config["configuration_fingerprint"] = FINGERPRINT_PREFIX + fingerprint(config)
    return config


def build_baseline_config(dataset_dir: Path, settings: Settings) -> dict:
    """冻结全量dev检索与固定八题回答样本；不会发请求、写文件或读取.env。"""
    return _make_config(Path(dataset_dir), _settings_parameters(settings))


def _parameters_from_config(config: dict) -> _Parameters:
    try:
        generation, embedding, retrieval = config["generation"], config["embedding"], config["retrieval"]
        return _Parameters.model_validate({
            "zhipu_base_url": embedding["provider_url"],
            "embedding_model": embedding["model"],
            "embedding_expected_dimensions": embedding["dimensions"],
            "embedding_batch_size": embedding["batch_size"],
            "zhipu_timeout_seconds": embedding["timeout_seconds"],
            "deepseek_base_url": generation["provider_url"],
            "deepseek_model": generation["model"],
            "deepseek_temperature": generation["temperature"],
            "deepseek_max_tokens": generation["max_tokens"],
            "deepseek_timeout_seconds": generation["timeout_seconds"],
            "rag_top_k": retrieval["answer_top_k"],
        })
    except (KeyError, TypeError, ValidationError):
        raise BaselineConfigError("基线配置字段或模型参数无效") from None


def validate_baseline_config(config: dict, dataset_dir: Path) -> dict:
    """核对内容指纹、冻结数据、分割和完整契约；未知字段也不能静默忽略。"""
    _check(isinstance(config, dict), "基线配置必须为JSON对象")
    parameters = _parameters_from_config(config)
    expected = _make_config(Path(dataset_dir), parameters)
    try:
        matches = _canonical_json(config) == _canonical_json(expected)
    except (TypeError, ValueError, RecursionError):
        matches = False
    _check(matches, "基线配置与冻结数据、Prompt或模型契约不一致；不得沿用旧实验身份")
    return config


def assert_runtime_matches(config: dict, settings: Settings) -> None:
    """任何真实请求前检查代码和显式参数，阻止Prompt或供应商参数隐式漂移。"""
    _check(isinstance(config, dict), "基线配置必须为JSON对象")
    runtime = _runtime_sections(_settings_parameters(settings))
    splitter = runtime.pop("splitter")
    try:
        content = {key: value for key, value in config.items() if key != "configuration_fingerprint"}
        _check(config["configuration_fingerprint"] == FINGERPRINT_PREFIX + fingerprint(content),
               "基线配置内容指纹不匹配")
        _check(_canonical_json(config["evaluation_config"]["splitter"]) == _canonical_json(splitter),
               "运行时切分配置与冻结基线不一致")
        _check(all(_canonical_json(config[name]) == _canonical_json(value)
                   for name, value in runtime.items()), "运行时Prompt、Schema或模型参数与冻结基线不一致")
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, BaselineConfigError):
            raise
        raise BaselineConfigError("基线配置无法匹配运行时契约") from None
