"""只读取新项目的配置，不在模块导入时实例化 Settings。"""

from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


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
    phoenix_endpoint: HttpUrl = "http://127.0.0.1:6007/v1/traces"
    phoenix_project: str = Field(default="project-copilot", pattern=r"^[A-Za-z0-9_.-]+$", max_length=100)
    trace_export_timeout_seconds: float = Field(default=2.0, gt=0, le=10, allow_inf_nan=False)

    # Optional: RAG/health remain usable without the ticket database.
    database_url: SecretStr | None = None
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    database_pool_timeout_seconds: int = Field(default=5, ge=1, le=30)

    agent_max_model_calls: int = Field(default=4, ge=1, le=8)
    agent_max_tool_calls: int = Field(default=6, ge=1, le=12)
    agent_tool_timeout_seconds: float = Field(default=240.0, gt=0, le=300, allow_inf_nan=False)
    agent_model_timeout_seconds: float = Field(default=30.0, gt=0, le=120, allow_inf_nan=False)
    agent_total_timeout_seconds: float = Field(default=300.0, gt=0, le=300, allow_inf_nan=False)
    final_rerank_device: Literal["cuda", "cpu"] = "cuda"
    final_rerank_dtype: Literal["float16", "float32"] = "float16"
    final_rerank_timeout_seconds: float = Field(default=90.0, gt=0, le=120, allow_inf_nan=False)
    final_model_cache_dir: Path = PROJECT_ROOT / "models" / "huggingface"

    @model_validator(mode="after")
    def validate_final_retrieval(self):
        if (self.final_rerank_device, self.final_rerank_dtype) not in {("cuda", "float16"), ("cpu", "float32")}:
            raise ValueError("最终重排须显式选择cuda/float16或cpu/float32")
        if self.agent_tool_timeout_seconds <= self.final_rerank_timeout_seconds:
            raise ValueError("Agent工具时限必须长于最终重排时限，确保首次加载有预算")
        return self

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
            valid = (url.drivername == "postgresql+psycopg" and url.host and url.username
                     and url.database and not url.query and url.port is not None and 1 <= url.port <= 65535)
        except (ArgumentError, ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("数据库URL须为含主机、端口、用户和库名的postgresql+psycopg地址，不能含查询参数")
        return value

    @field_validator("phoenix_endpoint")
    @classmethod
    def validate_phoenix_endpoint(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment or not value.path.endswith("/v1/traces"):
            raise ValueError("Phoenix endpoint须为无凭据/查询参数的OTLP HTTP /v1/traces地址")
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
