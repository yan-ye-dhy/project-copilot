"""FastAPI入口：配置在启动时加载，问答客户端在请求时创建。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from project_copilot.api.documents import router as documents_router
from project_copilot.api.rag import router as rag_router
from project_copilot.api.tickets import router as tickets_router
from project_copilot.api.agent import router as agent_router
from project_copilot.api.actions import router as actions_router
from project_copilot.config import Settings, load_settings
from project_copilot.db.session import Database
from project_copilot.infrastructure.tracing import RequestContextMiddleware, create_trace_runtime


logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建应用；调用时不连接模型、数据库或追踪服务。"""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = settings if settings is not None else load_settings()
        active = application.state.settings
        database = Database(
            active.database_url.get_secret_value(), connect_timeout=active.database_connect_timeout_seconds,
            pool_timeout=active.database_pool_timeout_seconds,
        ) if active.database_url is not None else None
        application.state.database = database
        application.state.tracing = create_trace_runtime(active)
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s %(message)s")
        logger.info("Starting %s", application.state.settings.app_name)
        try:
            yield
        finally:
            if database is not None:
                database.close()
            if application.state.tracing is not None:
                application.state.tracing.close()
            from project_copilot.rag.final_retrieval import close_final_retriever
            close_final_retriever(application.state.settings)
            logger.info("Stopping %s", application.state.settings.app_name)

    application = FastAPI(title="Project Copilot", version="0.1.0", lifespan=lifespan)
    application.add_middleware(RequestContextMiddleware)
    application.include_router(documents_router)
    application.include_router(rag_router)
    application.include_router(tickets_router)
    application.include_router(agent_router)
    application.include_router(actions_router)

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        run_id = getattr(request.state, "run_id", None) or str(uuid4())
        logger.error("Unhandled request error run_id=%s error_type=%s", run_id, type(exc).__name__)
        headers = {"X-Run-ID": run_id}
        if getattr(request.state, "trace_id", None):
            headers["X-Trace-ID"] = request.state.trace_id
            headers["X-Trace-Exported"] = str(getattr(request.state, "trace_exported", False)).lower()
        return JSONResponse(
            status_code=500, content={"detail": "服务器内部处理失败，请根据run_id排查", "run_id": run_id},
            headers=headers,
        )

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
