"""D2回归：增加模型配置后，健康接口仍不依赖模型密钥或客户端。"""

import asyncio

import httpx
import pytest

from project_copilot.config import Settings
from project_copilot.infrastructure import models
from project_copilot.main import create_app


def test_health_without_key_does_not_create_model_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_model_client(*_args: object, **_kwargs: object) -> None:
        pytest.fail("健康接口不应创建模型客户端")

    monkeypatch.setattr(models, "OpenAI", forbid_model_client)
    settings = Settings(_env_file=None, zhipu_api_key=None, environment="test")
    application = create_app(settings)

    async def check_health() -> None:
        # ASGITransport不自动启动lifespan，因此显式进入相同生命周期。
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://testserver"
            ) as client:
                response = await client.get("/health")
                assert response.status_code == 200
                assert response.json() == {
                    "status": "ok",
                    "service": settings.app_name,
                    "environment": "test",
                }

    asyncio.run(check_health())
