import dataclasses
from collections.abc import Iterator

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from documind.application.health_service import HealthService
from documind.container import Container, build_container
from documind.core.config import Settings
from documind.domain.health import DependencyCheckError
from documind.main import create_app
from tests.conftest import SettingsFactory
from tests.unit.test_health_service import FakeCheck


def _client(settings: Settings, service: HealthService) -> TestClient:
    def factory(s: Settings) -> Container:
        return dataclasses.replace(build_container(s), health_service=service)

    return TestClient(create_app(settings, container_factory=factory))


@pytest.fixture
def healthy_client(settings: Settings) -> Iterator[TestClient]:
    service = HealthService(
        [FakeCheck("database", details={"pgvector_version": "0.8.7"}), FakeCheck("redis")],
        timeout_seconds=1,
    )
    with _client(settings, service) as client:
        yield client


def test_liveness(healthy_client: TestClient) -> None:
    response = healthy_client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_healthy(healthy_client: TestClient) -> None:
    response = healthy_client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"]["environment"] == "test"
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["database"]["details"] == {"pgvector_version": "0.8.7"}
    assert set(body["components"]) == {"database", "redis"}


def test_readiness_degraded_returns_503(settings: Settings) -> None:
    service = HealthService(
        [FakeCheck("database"), FakeCheck("redis", error=DependencyCheckError("down"))],
        timeout_seconds=1,
    )
    with _client(settings, service) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["redis"] == {
        "status": "error",
        "latency_ms": body["components"]["redis"]["latency_ms"],
        "details": {},
        "error": "down",
    }


def test_production_hides_details(make_settings: SettingsFactory) -> None:
    service = HealthService(
        [
            FakeCheck("database", details={"server_version": "16"}),
            FakeCheck("redis", error=DependencyCheckError("down")),
        ],
        timeout_seconds=1,
    )
    with _client(make_settings(environment="production"), service) as client:
        body = client.get("/api/v1/health").json()
    assert body["components"]["database"]["details"] is None
    assert body["components"]["redis"]["error"] is None
    assert body["components"]["redis"]["status"] == "error"


def test_request_id_generated_and_echoed(healthy_client: TestClient) -> None:
    generated = healthy_client.get("/api/v1/health/live").headers["x-request-id"]
    assert len(generated) == 32

    echoed = healthy_client.get("/api/v1/health/live", headers={"X-Request-ID": "client-abc.1"})
    assert echoed.headers["x-request-id"] == "client-abc.1"


def test_unsafe_request_id_is_replaced(healthy_client: TestClient) -> None:
    response = healthy_client.get("/api/v1/health/live", headers={"X-Request-ID": "bad id\r\nx"})
    assert response.headers["x-request-id"] != "bad id\r\nx"
    assert len(response.headers["x-request-id"]) == 32


def test_unhandled_error_returns_generic_json(settings: Settings) -> None:
    service = HealthService([], timeout_seconds=1)

    def factory(s: Settings) -> Container:
        return dataclasses.replace(build_container(s), health_service=service)

    app = create_app(settings, container_factory=factory)
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Internal server error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert "secret" not in response.text


def test_docs_toggle(make_settings: SettingsFactory) -> None:
    service = HealthService([], timeout_seconds=1)
    with _client(make_settings(), service) as client:
        assert client.get("/api/openapi.json").status_code == 200
    with _client(make_settings(environment="production"), service) as client:
        assert client.get("/api/openapi.json").status_code == 404
        assert client.get("/api/docs").status_code == 404


def test_cors_only_when_configured(make_settings: SettingsFactory) -> None:
    service = HealthService([], timeout_seconds=1)
    headers = {"Origin": "http://app.test", "Access-Control-Request-Method": "GET"}
    with _client(make_settings(), service) as client:
        r = client.options("/api/v1/health/live", headers=headers)
        assert "access-control-allow-origin" not in r.headers
    with _client(make_settings(cors_allowed_origins="http://app.test"), service) as client:
        r = client.options("/api/v1/health/live", headers=headers)
        assert r.headers["access-control-allow-origin"] == "http://app.test"
