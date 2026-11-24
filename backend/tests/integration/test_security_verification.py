# ruff: noqa: F811 - pytest fixtures imported from test_infrastructure
"""Phase 9 verification: secret leakage, backups, outbound limits, failure behaviour, rotation."""

from __future__ import annotations

import asyncio
import io
import logging
import subprocess
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from documind.core.logging import JsonFormatter
from tests.integration.conftest import Api
from tests.integration.test_byok_api import BAD, KEY, FakeFactory
from tests.integration.test_documents_api import upload
from tests.integration.test_infrastructure import (  # noqa: F401 - fixtures
    CUSTOMER_BUCKET,
    EXT_DB,
    EXT_PASSWORD,
    _admin,
    _admin_url,
    activate_new,
    create,
    customer_bucket,
    external_db,
    iapi,
    owner_org,
    ready,
    run_routed_job,
    url,
)
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_usage_and_quotas import POLICY

pytestmark = pytest.mark.integration


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    """Every log record from DEBUG up, formatted exactly as production does (JSON).

    Call ``attach()`` (stored on the stream) after the app starts: app start-up
    reconfigures logging.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    previous = root.level

    def attach() -> None:
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

    stream.attach = attach  # type: ignore[attr-defined]
    yield stream
    root.removeHandler(handler)
    root.setLevel(previous)


def pg_dump(api: Api, database: str | None = None) -> str:
    """A plain-SQL backup, as an operator would take one."""
    target = _admin_url(api)
    if database:
        target = target.set(database=database)
    libpq = target.set(drivername="postgresql").render_as_string(hide_password=False)
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["pg_dump", "--no-owner", "--format=plain", libpq],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_secrets_never_leak_to_logs_responses_audit_or_backups(
    make_api: Any,
    identity_settings: Any,
    external_db: dict[str, Any],
    customer_bucket: dict[str, Any],
    captured_logs: io.StringIO,
) -> None:
    factory = FakeFactory()
    api: Api = make_api(
        {"provider_factory": factory},
        infrastructure_allowed_private_hosts=["localhost", "127.0.0.1"],
    )
    captured_logs.attach()  # type: ignore[attr-defined]
    w, h, elevated = owner_org(api)
    responses: list[str] = []

    def call(method: str, path: str, **kw: Any) -> Any:
        response = api.client.request(method, path, **kw)
        responses.append(response.text)
        return response

    call(
        "PUT",
        f"/api/v1/workspaces/{w}/ai-providers/openai",
        json={"api_key": BAD},
        headers=elevated,
    )
    call(
        "PUT",
        f"/api/v1/workspaces/{w}/ai-providers/openai",
        json={"api_key": KEY, "reindex": True},
        headers=elevated,
    )
    call("GET", f"/api/v1/workspaces/{w}/ai-providers", headers=h)
    # Validation errors would once echo the submitted body.
    call(
        "PUT",
        f"/api/v1/workspaces/{w}/ai-providers/openai",
        json={"api_key": KEY, "bogus": 1},
        headers=elevated,
    )
    wrong = {**external_db, "password": "definitely-wrong-password-42"}
    v1 = call("POST", url(w, "/versions"), json={"database": wrong}, headers=elevated).json()
    call("POST", url(w, f"/versions/{v1['id']}/validate"), headers=elevated)
    v2 = call(
        "POST",
        url(w, "/versions"),
        json={"database": external_db, "storage": customer_bucket},
        headers=elevated,
    ).json()
    call("POST", url(w, f"/versions/{v2['id']}/validate"), headers=elevated)
    call("GET", url(w), headers=h)

    secrets = {
        "workspace AI key": KEY,
        "rejected AI key": BAD,
        "database password": EXT_PASSWORD,
        "wrong database password": "definitely-wrong-password-42",
        "storage secret": customer_bucket["secret_access_key"],
    }
    surfaces = {
        "logs": captured_logs.getvalue(),
        "API responses": "\n".join(responses),
        "audit log": " ".join(str(tuple(r)) for r in sql(api, "SELECT * FROM audit_logs")),
        "infrastructure history": " ".join(
            str(tuple(r)) for r in sql(api, "SELECT * FROM infrastructure_events")
        ),
        "version validation": " ".join(
            str(r.validation)
            for r in sql(api, "SELECT validation FROM infrastructure_profile_versions")
        ),
        "database backup": pg_dump(api),
    }
    # The capture really saw the activity (including the failed database login).
    assert "PUT /api/v1/workspaces/" in surfaces["logs"]
    assert "data-plane validation failed" in surfaces["logs"]
    for surface, content in surfaces.items():
        for name, value in secrets.items():
            assert value not in content, f"{name} leaked into {surface}"
    # The backup does contain the (encrypted) credentials: restorable, not readable.
    assert "credentials" in surfaces["database backup"]


def test_outbound_checks_are_rate_limited(make_api: Any, external_db: dict[str, Any]) -> None:
    api: Api = make_api(
        infrastructure_allowed_private_hosts=["localhost"],
        rate_limit_outbound_checks_per_workspace=2,
        rate_limit_enabled=True,
    )
    w, _, elevated = owner_org(api)
    draft = create(api, w, elevated, database=external_db)  # 1
    assert draft.status_code == 201
    vid = draft.json()["id"]
    assert (
        api.client.post(url(w, f"/versions/{vid}/validate"), headers=elevated).status_code == 200
    )  # 2
    limited = api.client.post(url(w, f"/versions/{vid}/validate"), headers=elevated)
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_one_organizations_database_outage_is_contained(
    iapi: Api,
    external_db: dict[str, Any],
) -> None:
    api = iapi
    down, hd, ed = owner_org(api, "Down Inc")
    activate_new(api, down, ed, database=external_db)
    ready(api, down, hd)
    healthy, hh, _ = owner_org(api, "Healthy Ltd")
    upload(api, healthy, hh, POLICY + b"\nHealthy.", "healthy.md")

    # The customer's database goes away (password changed, server gone...).
    asyncio.run(
        _admin(
            _admin_url(api),
            "ALTER ROLE documind_ext_user PASSWORD 'rotated-elsewhere'",
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{EXT_DB}'",  # noqa: S608
        )
    )
    blocked = api.client.get(f"/api/v1/workspaces/{down}/documents", headers=hd)
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "data_plane_unavailable"
    # Identity and the other organization are unaffected.
    assert api.client.get("/api/v1/me", headers=hd).status_code == 200
    assert (
        api.client.get(f"/api/v1/workspaces/{healthy}/documents", headers=hh).json()["total"] == 1
    )
    # A job for the unreachable plane is left for the sweeper, not crashed.
    assert run_routed_job(api, uuid.uuid4(), down).status == "unavailable"


def test_rotating_the_database_password_needs_no_migration(
    iapi: Api,
    external_db: dict[str, Any],
) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    first = activate_new(api, w, elevated, database=external_db)
    ready(api, w, h)
    old_credential = first["config"]["database"]["password_credential_id"]

    new_password = "rotated-password-9876"
    asyncio.run(_admin(_admin_url(api), f"ALTER ROLE documind_ext_user PASSWORD '{new_password}'"))
    second = activate_new(api, w, elevated, database={**external_db, "password": new_password})
    assert second["version"] == first["version"] + 1
    # Same location: no migration; the data is still there and served.
    assert api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()["total"] == 1
    statuses = {r.id: r.status for r in sql(api, "SELECT id, status FROM credentials")}
    assert statuses[uuid.UUID(old_credential)] == "revoked"
    versions = {
        r.version: r.status
        for r in sql(api, "SELECT version, status FROM infrastructure_profile_versions")
    }
    assert versions == {first["version"]: "retired", second["version"]: "active"}


def test_organizations_cannot_reach_each_others_documents(
    iapi: Api,
    customer_bucket: dict[str, Any],
) -> None:
    api = iapi
    a, ha, ea = owner_org(api, "Alpha")
    activate_new(api, a, ea, database={"mode": "dedicated"}, storage=customer_bucket)
    doc = ready(api, a, ha)
    b, hb, _ = owner_org(api, "Beta")
    for path in (
        f"/api/v1/workspaces/{a}/documents/{doc}",
        f"/api/v1/workspaces/{a}/documents/{doc}/download-url",
        f"/api/v1/workspaces/{b}/documents/{doc}",
        f"/api/v1/workspaces/{b}/documents/{doc}/download-url",
    ):
        assert api.client.get(path, headers=hb).status_code == 404, path
    assert api.client.get(f"/api/v1/workspaces/{a}/infrastructure", headers=hb).status_code == 404
