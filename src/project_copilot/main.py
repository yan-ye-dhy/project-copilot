"""FastAPI最小入口：配置在启动时加载，当前仅开放健康接口。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from project_copilot.config import Settings, load_settings


logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建应用；调用时不连接模型、数据库或追踪服务。"""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = settings if settings is not None else load_settings()
        logger.info("Starting %s", application.state.settings.app_name)
        try:
            yield
        finally:
            logger.info("Stopping %s", application.state.settings.app_name)

    application = FastAPI(title="Project Copilot", version="0.1.0", lifespan=lifespan)

    @application.get("/health", tags=["health"])
    async def health(request: Request) -> dict[str, str]:
        active_settings: Settings = request.app.state.settings
        return {
            "status": "ok",
            "service": active_settings.app_name,
            "environment": active_settings.environment,
        }

    return application


app = create_app()
