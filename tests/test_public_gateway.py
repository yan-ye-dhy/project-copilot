"""Public gateway's fail-closed security contract; no sockets, model or database."""

import asyncio
import logging

import httpx
import pytest
from pydantic import ValidationError

from project_copilot.config import Settings
from project_copilot.main import create_app


HEADER = "X-Project-Copilot-Proxy-Secret"
SECRET = "test_only_" + "a" * 48
ORIGIN = "https://copilot.example"


def settings(**overrides):
    values = {
        "_env_file": None,
        "database_url": None,
        "tracing_enabled": False,
        "zhipu_api_key": None,
        "deepseek_api_key": None,
        **overrides,
    }
    return Settings(**values)


def public_settings(**overrides):
    return settings(public_demo=True, public_proxy_secret=SECRET, auth_public_origin=ORIGIN, **overrides)


def request(app, method, path, **kwargs):
    async def run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)

    return asyncio.run(run())


@pytest.mark.parametrize("changes", [
    {"public_proxy_secret": None},
    {"public_proxy_secret": ""},
    {"public_proxy_secret": "too-short"},
    {"auth_public_origin": None},
    {"auth_public_origin": ""},
    {"auth_public_origin": "http://insecure.example"},
    {"auth_public_origin": "https://user:password@copilot.example"},
    {"auth_public_origin": "https://copilot.example/path"},
])
def test_invalid_public_configuration_fails_before_startup_without_exposing_secret(changes):
    args = {"public_demo": True, "public_proxy_secret": SECRET, "auth_public_origin": ORIGIN, **changes}
    with pytest.raises(ValidationError) as error:
        settings(**args)
    assert SECRET not in str(error.value)


def test_production_cannot_bypass_proxy_gate_by_disabling_public_demo():
    with pytest.raises(ValidationError):
        settings(environment="production", public_demo=False)
    app = create_app(settings(environment="production", public_demo=False,
                              public_proxy_secret=SECRET, auth_public_origin=ORIGIN))
    assert request(app, "GET", "/health").status_code == 403
    assert request(app, "GET", "/health", headers={HEADER: SECRET}).status_code == 503


@pytest.mark.parametrize("headers", [
    [],
    [(HEADER, "")],
    [(HEADER, "wrong_secret_" + "b" * 48)],
    [(HEADER, SECRET), (HEADER, SECRET)],
    [(HEADER, SECRET), (HEADER.lower(), "other")],
    [(HEADER, SECRET + ", " + SECRET)],
    [(HEADER, "a" * 10000)],
    [(HEADER.encode(), b"\xff")],
])
def test_missing_wrong_malformed_or_duplicate_secret_is_rejected(headers):
    response = request(create_app(public_settings()), "GET", "/health", headers=headers)
    assert response.status_code == 403
    assert SECRET not in response.text


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/health", None),
    ("GET", "/tickets", None),
    ("POST", "/tickets", {"title": "test", "description": "test"}),
    ("POST", "/agent/contexts", None),
    ("POST", "/agent/ask", {"question": "test"}),
    ("POST", "/rag/ask", {"question": "test"}),
    ("POST", "/documents/upload", None),
    ("POST", "/actions/preview", {"title": "test", "description": "test"}),
])
def test_valid_gateway_secret_cannot_enable_business_before_security_stage(method, path, body):
    app = create_app(public_settings())
    reached = []

    @app.get("/__test_probe")
    async def probe():
        reached.append(True)
        return {"unexpected": True}

    response = request(app, method, path, headers={HEADER: SECRET}, json=body)
    assert response.status_code == 503
    assert SECRET not in response.text
    probe_response = request(app, "GET", "/__test_probe", headers={HEADER: SECRET})
    assert probe_response.status_code == 503
    assert not reached


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_public_mode_does_not_expose_debug_schema_even_with_gateway_secret(path):
    response = request(create_app(public_settings()), "GET", path, headers={HEADER: SECRET})
    assert response.status_code in {404, 503}
    assert "swagger-ui" not in response.text.lower()
    assert '"openapi"' not in response.text.lower()


def test_local_default_remains_compatible_and_does_not_require_proxy_secret():
    app = create_app(settings())
    assert request(app, "GET", "/health").status_code == 200
    assert request(app, "GET", "/docs").status_code == 200
    schema = request(app, "GET", "/openapi.json")
    assert schema.status_code == 200
    assert "/rag/ask" in schema.json()["paths"]
    assert "/tickets" in schema.json()["paths"]


def test_gateway_does_not_log_credentials_or_request_content(caplog):
    secret_in_request = "wrong-secret-not-for-logs"
    cookie = "session-test-not-for-logs"
    payload = "private-body-not-for-logs"
    app = create_app(public_settings())
    with caplog.at_level(logging.INFO):
        response = request(app, "POST", "/agent/ask?private=query-not-for-logs", headers={
            HEADER: secret_in_request,
            "Cookie": f"session={cookie}",
        }, json={"question": payload})
    assert response.status_code == 403
    # httpx's test client logs its own full URL; only app logs are in scope.
    app_logs = (record.getMessage() for record in caplog.records
                if record.name.startswith("project_copilot."))
    texts = response.text + "\n" + "\n".join(app_logs)
    assert all(value not in texts for value in [SECRET, secret_in_request, cookie, payload, "query-not-for-logs"])
