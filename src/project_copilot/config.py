"""只读取新项目的配置，不在模块导入时实例化 Settings。"""

from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """应用与智谱配置；缺少模型密钥时仍可启动健康接口。"""

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

    @field_validator("zhipu_base_url")
    @classmethod
    def validate_zhipu_base_url(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("智谱base URL必须使用HTTPS")
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("智谱base URL不能包含凭据、查询参数或片段")
        return value


def load_settings(*, env_file: Path | None = ENV_FILE) -> Settings:
    """读取配置；显式路径方便验证，None可禁用dotenv文件读取。"""
    return Settings(_env_file=env_file)
