"""Same-origin UI delivery, configuration privacy and static-directory boundary."""

from fastapi.testclient import TestClient
import pytest

from project_copilot.api import agent, documents, rag
from project_copilot.config import Settings
from project_copilot import main


@pytest.fixture
def client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Loading the browser UI must not construct a database or model service")

    monkeypatch.setattr(main, "Database", forbidden)
    monkeypatch.setattr(rag, "create_answer_service", forbidden)
    monkeypatch.setattr(agent, "create_agent_service", forbidden)
    monkeypatch.setattr(documents, "create_ingestion_service", forbidden)
    settings = Settings(
        _env_file=None, database_url=None, tracing_enabled=False,
        zhipu_api_key="private-zhipu-test-value", deepseek_api_key="private-chat-test-value",
        upload_max_bytes=7 * 1024 * 1024,
    )
    with TestClient(main.create_app(settings)) as active:
        yield active


def test_home_is_a_real_chinese_interface_with_same_origin_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Project Copilot" in response.text
    assert 'id="question-input"' in response.text
    assert 'id="upload-button"' in response.text
    assert 'id="tickets-list"' in response.text
    assert "/ui-assets/app.js" in response.text
    assert "/ui-assets/styles.css" in response.text
    assert "private-zhipu-test-value" not in response.text
    assert "private-chat-test-value" not in response.text


def test_home_has_content_and_referrer_boundaries(client):
    response = client.get("/")
    policy = response.headers["content-security-policy"]
    for directive in (
        "default-src 'self'", "script-src 'self'", "style-src 'self'", "connect-src 'self'",
        "object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'", "form-action 'self'",
    ):
        assert directive in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("asset,content_type", [
    ("app.js", "javascript"), ("styles.css", "text/css"),
])
def test_shipped_assets_are_served_as_files(client, asset, content_type):
    response = client.get("/ui-assets/" + asset)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert len(response.content) > 100
    assert response.headers["x-run-id"]


def test_public_config_exposes_only_the_actual_upload_limit(client):
    response = client.get("/ui/config")
    assert response.status_code == 200
    assert response.json() == {"upload_max_bytes": 7 * 1024 * 1024}
    assert response.headers["cache-control"] == "no-store"
    assert "private-zhipu-test-value" not in response.text
    assert "private-chat-test-value" not in response.text


@pytest.mark.parametrize("path", [
    "/.env", "/src/project_copilot/config.py", "/ui-assets/.env",
    "/ui-assets/%2e%2e/index.html", "/ui-assets/%2e%2e/%2e%2e/config.py",
    "/ui-assets/nonexistent.js",
])
def test_ui_does_not_expose_repository_or_parent_files(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert "private-zhipu-test-value" not in response.text


def test_health_docs_and_api_schema_remain_available(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/rag/ask" in schema["paths"]
    assert "/agent/ask" in schema["paths"]
    assert "/actions/{action_id}/confirm" in schema["paths"]
    assert "/tickets" in schema["paths"]
    assert "/" not in schema["paths"]
    assert "/ui/config" not in schema["paths"]


def test_missing_api_path_is_not_replaced_with_frontend_html(client):
    response = client.get("/agent/unknown")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
