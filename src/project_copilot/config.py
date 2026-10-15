"""只读取新项目的配置，不在模块导入时实例化 Settings。"""

from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """应用、模型与独立Dense索引配置；导入时不连接外部服务。"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="PROJECT_COPILOT_",
        extra="ignore",
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="Project Copilot", min_length=1, max_length=80)
    environment: Literal["development", "test", "production"] = "development"

    zhipu_api_key: SecretStr | None = None
    zhipu_base_url: HttpUrl = "https://open.bigmodel.cn/api/paas/v4/"
    embedding_model: Literal["embedding-3"] = "embedding-3"
    # 这是本地响应校验预期，不是发送给模型的dimensions参数。
    embedding_expected_dimensions: int = Field(default=2048, ge=1)
    # 与文档切分长度不同：这里是一批请求包含的文本条数。
    embedding_batch_size: int = Field(default=64, ge=1, le=64)
    zhipu_timeout_seconds: float = Field(default=20.0, gt=0, le=120, allow_inf_nan=False)

    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: HttpUrl = "https://api.deepseek.com"
    deepseek_model: Literal["deepseek-flash", "deepseek-v4-pro"] = "deepseek-flash"
    deepseek_timeout_seconds: float = Field(default=30.0, gt=0, le=120, allow_inf_nan=False)
    deepseek_temperature: float = Field(default=0.0, ge=0, le=2, allow_inf_nan=False)
    deepseek_max_tokens: int = Field(default=1024, ge=1, le=4096)

    qdrant_url: HttpUrl = "http://127.0.0.1:6433"
    qdrant_collection_name: str = Field(
        default="pc_knowledge_dense_v0", pattern=r"^pc_[A-Za-z0-9_-]+$", max_length=120
    )
    qdrant_timeout_seconds: float = Field(default=10.0, gt=0, le=120, allow_inf_nan=False)
    manifest_dir: Path = PROJECT_ROOT / "data" / "manifests"
    rag_top_k: int = Field(default=5, ge=1, le=100)
    upload_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    tracing_enabled: bool = False

    @field_validator("tracing_enabled")
    @classmethod
    def require_available_tracing(cls, value: bool) -> bool:
        # D6仅预留开关，实际Phoenix导出到D33接入；不能静默宣称已启用。
        if value:
            raise ValueError("Phoenix导出尚未接入，请保持PROJECT_COPILOT_TRACING_ENABLED=false")
        return value

    @field_validator("zhipu_base_url")
    @classmethod
    def validate_zhipu_base_url(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("智谱base URL必须使用HTTPS")
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("智谱base URL不能包含凭据、查询参数或片段")
        return value

    @field_validator("deepseek_base_url")
    @classmethod
    def validate_deepseek_base_url(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("DeepSeek base URL必须使用HTTPS")
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("DeepSeek base URL不能包含凭据、查询参数或片段")
        return value

    @field_validator("qdrant_url")
    @classmethod
    def validate_qdrant_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Qdrant URL不能包含凭据、查询参数或片段")
        return value

    @field_validator("manifest_dir")
    @classmethod
    def validate_manifest_dir(cls, value: Path) -> Path:
        # uv可从其他目录执行，清单位置始终以新项目根目录为参照。
        expanded = value.expanduser()
        resolved = (expanded if expanded.is_absolute() else PROJECT_ROOT / expanded).resolve()
        reference_root = PROJECT_ROOT.parent / "conversational-agent-langchain"
        if resolved.is_relative_to(reference_root.resolve()):
            raise ValueError("成功清单不能写入只读参考项目")
        return resolved


def load_settings(*, env_file: Path | None = ENV_FILE) -> Settings:
    """读取配置；显式路径方便验证，None可禁用dotenv文件读取。"""
    return Settings(_env_file=env_file)
