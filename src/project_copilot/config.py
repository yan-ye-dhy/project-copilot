"""只读取新项目的配置，不在模块导入时实例化 Settings。"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """D1只保留健康接口所需的配置，后续按任务添加模型等字段。"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="PROJECT_COPILOT_",
        extra="ignore",
        str_strip_whitespace=True,
    )

    app_name: str = Field(default="Project Copilot", min_length=1, max_length=80)
    environment: Literal["development", "test", "production"] = "development"


def load_settings(*, env_file: Path | None = ENV_FILE) -> Settings:
    """读取配置；显式路径方便验证，None可禁用dotenv文件读取。"""
    return Settings(_env_file=env_file)
