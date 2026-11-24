"""Infrastructure independence against real PostgreSQL and MinIO (MI-1 to MI-5, ADR 0021).

* External database: a separate database with a non-superuser role (the
  customer's), migrated by DocuMind because the customer allowed it.
* Dedicated database: provisioned by DocuMind on the same server.
* Customer storage: a separate MinIO bucket under a prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from documind.application.infrastructure.resolver import InfrastructureTargets
from documind.application.infrastructure.service import authenticate_worker
from documind.application.infrastructure.sweeps import sweep_processing
from documind.application.processing.orchestrator import JobOutcome, ProcessingOrchestrator
from documind.container import build_container, processing_ai
from documind.infrastructure.database.dataplane import DataPlaneBase, dataplane_metadata
from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_usage_and_quotas import POLICY, assign

pytestmark = pytest.mark.integration

EXT_DB, EXT_USER, EXT_PASSWORD = "documind_ext_test", "documind_ext_user", "ext-user-password-123"
CUSTOMER_BUCKET = "documind-customer-test"


# --------------------------------------------------------------------------- fixtures


def _admin_url(api_or_settings: Any) -> Any:
    settings = getattr(api_or_settings, "settings", api_or_settings)
    return make_url(settings.database_url.get_secret_value())


async def _admin(url: Any, *statements: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture
def external_db(identity_settings: Any) -> Iterator[dict[str, Any]]:
    """A customer's database: non-superuser owner, pgvector installed, no DocuMind schema yet."""
    url = _admin_url(identity_settings)
    asyncio.run(
        _admin(
            url,
            f"DROP DATABASE IF EXISTS {EXT_DB} WITH (FORCE)",
            f"DROP ROLE IF EXISTS {EXT_USER}",
            f"CREATE ROLE {EXT_USER} LOGIN NOSUPERUSER PASSWORD '{EXT_PASSWORD}'",
            f"CREATE DATABASE {EXT_DB} OWNER {EXT_USER}",
        )
    )
    asyncio.run(_admin(url.set(database=EXT_DB), "CREATE EXTENSION vector"))
    yield {
        "mode": "external",
        "host": url.host,
        "port": url.port or 5432,
        "database": EXT_DB,
        "username": EXT_USER,
        "password": EXT_PASSWORD,
        "sslmode": "disable",
        "manage_schema": True,
    }
    asyncio.run(_admin(url, f"DROP DATABASE IF EXISTS {EXT_DB} WITH (FORCE)"))


def _s3(settings: Any) -> Any:
    return boto3.session.Session().client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
    )


@pytest.fixture
def customer_bucket(identity_settings: Any) -> Iterator[dict[str, Any]]:
    client = _s3(identity_settings)
    with contextlib.suppress(client.exceptions.BucketAlreadyOwnedByYou):
        client.create_bucket(Bucket=CUSTOMER_BUCKET)
    _empty(client)
    yield {
        "provider": "minio",
        "bucket": CUSTOMER_BUCKET,
        "endpoint_url": identity_settings.s3_endpoint_url,
        "region": "us-east-1",
        "prefix": "acme/documind",
        "access_key_id": identity_settings.s3_access_key_id.get_secret_value(),
        "secret_access_key": identity_settings.s3_secret_access_key.get_secret_value(),
    }
    _empty(client)


def _empty(client: Any) -> None:
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=CUSTOMER_BUCKET):
        for obj in page.get("Contents", []):
            client.delete_object(Bucket=CUSTOMER_BUCKET, Key=obj["Key"])


@pytest.fixture
def iapi(make_api: Any, identity_settings: Any) -> Iterator[Api]:
    url = _admin_url(identity_settings)
    api: Api = make_api(
        infrastructure_allowed_private_hosts=["localhost", "127.0.0.1"],
        dedicated_database_admin_url=SecretStr(url.render_as_string(hide_password=False)),
        dedicated_database_sslmode="disable",
    )
    yield api

    # Provisioned dedicated databases and roles from this test.
    async def cleanup() -> None:
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                names = (
                    await conn.execute(
                        text("SELECT datname FROM pg_database WHERE datname LIKE 'dm\\_%'")
                    )
                ).scalars()
                for name in list(names):
                    await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                roles = (
                    await conn.execute(
                        text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'dm\\_%\\_app'")
                    )
                ).scalars()
                for role in list(roles):
                    await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        finally:
            await engine.dispose()

    asyncio.run(cleanup())


def owner_org(
    api: Api, name: str = "Acme", plan: str = "enterprise"
) -> tuple[str, dict[str, str], dict[str, str]]:
    h = api.signup(f"owner-{uuid.uuid4().hex[:6]}@example.com")
    org = str(api.create_org(h, name)["id"])
    assign(api, org, plan)
    secret, _ = api.enable_mfa(h)
    return org, h, api.step_up(h, secret)


def url(w: str, tail: str = "") -> str:
    return f"/api/v1/workspaces/{w}/infrastructure{tail}"


def create(api: Api, w: str, headers: dict[str, str], **body: Any) -> Any:
    return api.client.post(url(w, "/versions"), json=body, headers=headers)


def activate_new(api: Api, w: str, elevated: dict[str, str], **body: Any) -> dict[str, Any]:
    created = create(api, w, elevated, **body)
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    validated = api.client.post(url(w, f"/versions/{vid}/validate"), headers=elevated).json()
    assert validated["status"] == "validated", validated
    active = api.client.post(url(w, f"/versions/{vid}/activate"), headers=elevated)
    assert active.status_code == 200, active.text
    result: dict[str, Any] = active.json()
    return result


def run_routed_job(api: Api, job_id: uuid.UUID, workspace_id: str) -> JobOutcome:
    """Run a job like the worker: routed to the workspace's data plane and storage."""

    async def go() -> JobOutcome:
        c = build_container(api.settings)
        try:
            return await ProcessingOrchestrator(
                c.session_factory,
                api.settings,
                c.storage,
                c.extractor,
                worker="test",
                ai=processing_ai(c),
                targets=InfrastructureTargets(c.infrastructure()),
            ).run(job_id, workspace_id=uuid.UUID(workspace_id))
        finally:
            await c.aclose()

    return asyncio.run(go())


def plane_sql(api: Api, database: str, statement: str, **params: Any) -> list[Any]:
    target = _admin_url(api).set(database=database)

    async def go() -> list[Any]:
        engine = create_async_engine(
            target, connect_args={"server_settings": {"search_path": "documind,public"}}
        )
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(statement), params)
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(go())


def ready(
    api: Api, w: str, h: dict[str, str], content: bytes = POLICY, name: str = "policy.md"
) -> str:
    response = upload(api, w, h, content, name)
    assert response.status_code == 201, response.text
    assert api.dispatcher.calls[-1][1] == uuid.UUID(w)  # the job carries its workspace
    assert run_routed_job(api, api.dispatcher.dispatched[-1], w).status == "succeeded"
    return str(response.json()["id"])


# --------------------------------------------------------------------------- tests


def test_new_organizations_use_platform_defaults(iapi: Api) -> None:
    w, h, _ = owner_org(iapi)
    overview = iapi.client.get(url(w), headers=h).json()
    assert overview["active_version_id"] is None
    assert overview["active_config"]["database"]["mode"] == "shared"
    assert overview["active_config"]["storage"]["provider"] == "documind"
    providers = {p["key"]: p["status"] for p in overview["storage_providers"]}
    assert providers["azure_blob"] == "planned"
    assert overview["dedicated_available"] is True


def test_access_rules(iapi: Api) -> None:
    api = iapi
    w, owner, elevated = owner_org(api, plan="free")
    # Plan: Free has no custom storage.
    blocked = create(
        api,
        w,
        elevated,
        storage={
            "provider": "minio",
            "bucket": "x-bucket",
            "endpoint_url": "https://s3.example.com",
            "access_key_id": "AKIA0",
            "secret_access_key": "s",
        },
    )
    assert blocked.status_code == 422
    assert "plan" in blocked.json()["detail"]
    # Step-up required.
    assert create(api, w, owner).json()["code"] == "mfa_step_up_required"
    # Owners only: admins get 403, outsiders 404.
    admin = api.add_member(w, owner, "admin@example.com", "admin")
    assert api.client.get(url(w), headers=admin).status_code == 403
    assert api.client.get(url(w), headers=api.signup("eve@example.com")).status_code == 404
    # Personal workspaces keep the platform defaults.
    personal = str(api.client.get("/api/v1/me/workspaces", headers=owner).json()[0]["id"])
    kinds = {
        x["id"]: x["kind"] for x in api.client.get("/api/v1/me/workspaces", headers=owner).json()
    }
    if kinds[personal] != "personal":
        personal = next(k for k, v in kinds.items() if v == "personal")
    assert create(api, personal, elevated).json()["code"] == "personal_workspace"


def test_external_database_and_customer_storage(
    iapi: Api, external_db: dict[str, Any], customer_bucket: dict[str, Any]
) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    created = create(
        api, w, elevated, database=external_db, storage=customer_bucket, note="Acme on-prem"
    )
    assert created.status_code == 201, created.text
    version = created.json()
    assert EXT_PASSWORD not in created.text
    assert customer_bucket["secret_access_key"] not in created.text
    assert version["config"]["database"]["password_credential_id"]
    vid = version["id"]

    # Activation needs a validation first; validation migrates the customer's schema.
    assert (
        api.client.post(url(w, f"/versions/{vid}/activate"), headers=elevated).json()["code"]
        == "version_not_validated"
    )
    validated = api.client.post(url(w, f"/versions/{vid}/validate"), headers=elevated).json()
    assert validated["status"] == "validated", validated["validation"]
    assert validated["validation"]["database"]["schema_revision"] == "dp0001"
    assert (
        api.client.post(url(w, f"/versions/{vid}/activate"), headers=elevated).json()["status"]
        == "active"
    )

    doc_id = ready(api, w, h)
    # Data lives in the customer's database and bucket, not DocuMind's.
    assert plane_sql(api, EXT_DB, "SELECT count(*) AS n FROM document_chunks")[0].n > 0
    assert (
        sql(api, "SELECT count(*) AS n FROM documents WHERE workspace_id = :w", w=uuid.UUID(w))[0].n
        == 0
    )
    keys = [
        o["Key"]
        for o in _s3(api.settings).list_objects_v2(Bucket=CUSTOMER_BUCKET).get("Contents", [])
    ]
    assert keys
    assert all(k.startswith("acme/documind/workspaces/") for k in keys)

    # Every feature works through the routed data plane.
    hits = api.client.post(
        f"/api/v1/workspaces/{w}/search", json={"query": "terminate notice"}, headers=h
    )
    assert hits.json()["results"]
    assert (
        api.client.post(
            f"/api/v1/workspaces/{w}/ask", json={"question": "How much notice?"}, headers=h
        ).status_code
        == 200
    )
    conv = api.client.post(f"/api/v1/workspaces/{w}/conversations", json={}, headers=h).json()
    sent = api.client.post(
        f"/api/v1/workspaces/{w}/conversations/{conv['id']}/messages",
        json={"content": "How much notice?"},
        headers=h,
    )
    assert sent.status_code in (200, 201), sent.text
    assert plane_sql(api, EXT_DB, "SELECT count(*) AS n FROM messages")[0].n == 2
    download = api.client.get(f"/api/v1/workspaces/{w}/documents/{doc_id}/download-url", headers=h)
    assert download.status_code == 200, download.text
    signed = download.json()["url"]
    assert CUSTOMER_BUCKET in signed  # signed for the customer's bucket
    assert "acme/documind/" in signed.replace("%2F", "/")
    usage = api.client.get(f"/api/v1/workspaces/{w}/usage", headers=h).json()
    assert usage["documents"]["total"] == 1
    assert usage["members"][0]["questions"] >= 1

    # Audit entries for data-plane changes reach the control plane exactly once.
    relayed = sql(
        api,
        "SELECT action, event_id FROM audit_logs WHERE workspace_id = :w AND event_id IS NOT NULL",
        w=uuid.UUID(w),
    )
    assert "document.uploaded" in {r.action for r in relayed}
    assert plane_sql(api, EXT_DB, "SELECT count(*) AS n FROM audit_outbox")[0].n == 0

    # The customer's database only accepts its registered workspace.
    with pytest.raises(IntegrityError):
        plane_sql(
            api,
            EXT_DB,
            "INSERT INTO document_collections (id, workspace_id, name) VALUES (:i, :w, 'x')",
            i=uuid.uuid4(),
            w=uuid.uuid4(),
        )


def test_dedicated_databases_isolate_organizations(iapi: Api) -> None:
    api = iapi
    a, ha, ea = owner_org(api, "Alpha")
    b, hb, eb = owner_org(api, "Beta")
    va = activate_new(api, a, ea, database={"mode": "dedicated"})
    vb = activate_new(api, b, eb, database={"mode": "dedicated"})
    db_a, db_b = va["config"]["database"]["database"], vb["config"]["database"]["database"]
    assert db_a != db_b
    assert va["config"]["database"]["username"].endswith("_app")
    ready(api, a, ha, POLICY, "alpha.md")
    ready(api, b, hb, POLICY + b"\nBeta appendix.", "beta.md")
    assert {r.title for r in plane_sql(api, db_a, "SELECT title FROM documents")} == {"alpha"}
    assert {r.title for r in plane_sql(api, db_b, "SELECT title FROM documents")} == {"beta"}

    # The provisioned role can't reach the other organization's database.
    async def connect_as_a_to_b() -> None:
        password_row = va["config"]["database"]
        engine = create_async_engine(
            _admin_url(api).set(database=db_b, username=password_row["username"], password="wrong")
        )
        try:
            async with engine.connect():
                pass
        finally:
            await engine.dispose()

    with pytest.raises(Exception, match=r"(?i)password|authentication"):
        asyncio.run(connect_as_a_to_b())
    # Search in one org never sees the other.
    hits = api.client.post(
        f"/api/v1/workspaces/{a}/search", json={"query": "appendix"}, headers=ha
    ).json()
    assert all("Beta" not in r["text"] for r in hits["results"])


def test_moving_a_workspace_with_data_needs_a_migration(
    iapi: Api, external_db: dict[str, Any]
) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    upload(api, w, h, POLICY, "policy.md")  # data in the shared plane
    created = create(api, w, elevated, database=external_db).json()
    api.client.post(url(w, f"/versions/{created['id']}/validate"), headers=elevated)
    refused = api.client.post(url(w, f"/versions/{created['id']}/activate"), headers=elevated)
    assert refused.status_code == 409
    assert refused.json()["code"] == "migration_required"
    overview = api.client.get(url(w), headers=h).json()
    assert overview["active_version_id"] is None  # nothing moved
    (plan,) = overview["migrations"]
    assert plan["status"] == "planned"
    assert plan["plan"]["moves"] == ["database"]
    assert api.client.post(url(w, f"/migrations/{plan['id']}/cancel"), headers=h).status_code == 204
    # The documents are still served from the shared database.
    assert api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()["total"] == 1


def test_tampered_profile_is_refused(iapi: Api) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    activate_new(api, w, elevated, database={"mode": "dedicated"})
    sql(
        api,
        "UPDATE infrastructure_profile_versions "
        "SET config = jsonb_set(config, '{database,host}', '\"evil.example.com\"') "
        "WHERE workspace_id = :w",
        w=uuid.UUID(w),
    )
    response = api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h)
    assert response.status_code == 503
    assert response.json()["code"] == "infrastructure_integrity_error"


@pytest.mark.parametrize(
    ("body", "part", "code"),
    [
        (
            {
                "database": {
                    "mode": "external",
                    "host": "169.254.169.254",
                    "database": "d",
                    "username": "u",
                    "password": "p",
                    "sslmode": "disable",
                }
            },
            "database",
            "host_not_allowed",
        ),
        (
            {
                "database": {
                    "mode": "external",
                    "host": "10.1.2.3",
                    "database": "d",
                    "username": "u",
                    "password": "p",
                    "sslmode": "disable",
                }
            },
            "database",
            "host_not_allowed",
        ),
        (
            {
                "storage": {
                    "provider": "s3_compatible",
                    "bucket": "b-bucket",
                    "endpoint_url": "http://192.168.1.5",
                    "access_key_id": "AKIA0",
                    "secret_access_key": "s",
                }
            },
            "storage",
            "endpoint_not_allowed",
        ),
        ({"processing": {"mode": "customer"}}, "processing", "not_available"),
    ],
)
def test_validation_blocks_internal_hosts_and_unavailable_modes(
    make_api: Any, body: dict[str, Any], part: str, code: str
) -> None:
    api: Api = make_api()  # no private-host allowlist
    w, _, elevated = owner_org(api)
    created = create(api, w, elevated, **body)
    assert created.status_code == 201, created.text
    result = api.client.post(
        url(w, f"/versions/{created.json()['id']}/validate"), headers=elevated
    ).json()
    assert result["status"] == "failed"
    assert result["validation"][part]["code"] == code


def test_customer_worker_tokens_are_hashed_and_revocable(iapi: Api) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    issued = api.client.post(url(w, "/workers"), json={"name": "on-prem-1"}, headers=elevated)
    assert issued.status_code == 201
    token = issued.json()["token"]
    assert token.startswith("dmw_")
    stored = sql(api, "SELECT token_hash, token_prefix FROM worker_registrations")[0]
    assert token not in stored.token_hash
    assert stored.token_prefix == token[:12]

    async def auth() -> bool:
        c = build_container(api.settings)
        try:
            async with c.session_factory() as session:
                return await authenticate_worker(session, token) is not None
        finally:
            await c.aclose()

    assert asyncio.run(auth())
    wid = issued.json()["id"]
    assert api.client.delete(url(w, f"/workers/{wid}"), headers=h).status_code == 204
    assert not asyncio.run(auth())
    assert "on-prem-1" not in str(
        api.client.get(url(w), headers=h).json()["workers"][0].get("token")
    )


def test_sweeps_cover_isolated_planes_and_relay_the_outbox(iapi: Api) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    version = activate_new(api, w, elevated, database={"mode": "dedicated"})
    db = version["config"]["database"]["database"]
    event = uuid.uuid4()
    plane_sql(
        api,
        db,
        "INSERT INTO audit_outbox (event_id, workspace_id, action, details) "
        "VALUES (:e, :w, 'test.left_behind', '{}')",
        e=event,
        w=uuid.UUID(w),
    )
    upload(api, w, h, POLICY, "policy.md")
    plane_sql(api, db, "UPDATE processing_jobs SET dispatched_at = now() - interval '1 hour'")

    async def sweep() -> Any:
        c = build_container(api.settings)
        try:
            return await sweep_processing(c.infrastructure(), api.settings, api.dispatcher)
        finally:
            await c.aclose()

    result = asyncio.run(sweep())
    assert result.dispatched >= 1
    assert api.dispatcher.calls[-1][1] == uuid.UUID(w)
    assert (
        sql(api, "SELECT action FROM audit_logs WHERE event_id = :e", e=event)[0].action
        == "test.left_behind"
    )
    asyncio.run(sweep())  # relaying twice never duplicates
    assert len(sql(api, "SELECT 1 FROM audit_logs WHERE event_id = :e", e=event)) == 1


def test_dataplane_schema_matches_the_shared_schema(iapi: Api) -> None:
    """Every data table must be identical apart from references to control-plane tables."""
    api = iapi
    w, _, elevated = owner_org(api)
    db = activate_new(api, w, elevated, database={"mode": "dedicated"})["config"]["database"][
        "database"
    ]

    async def describe(database: str | None, schema: str) -> dict[str, Any]:
        target = _admin_url(api) if database is None else _admin_url(api).set(database=database)
        engine = create_async_engine(target)
        try:
            async with engine.connect() as conn:
                from documind.infrastructure.database.dataplane import DATA_PLANE_TABLES

                out: dict[str, Any] = {}
                for table in DATA_PLANE_TABLES:
                    cols = (
                        await conn.execute(
                            text(
                                "SELECT column_name, data_type, udt_name, is_nullable, "
                                "column_default, character_maximum_length, generation_expression "
                                "FROM information_schema.columns "
                                "WHERE table_schema = :s AND table_name = :t ORDER BY column_name"
                            ),
                            {"s": schema, "t": table},
                        )
                    ).all()
                    indexes = (
                        await conn.execute(
                            text(
                                "SELECT indexname, "
                                "regexp_replace(indexdef, ' ON [a-z_.]+', ' ON t') "
                                "FROM pg_indexes WHERE schemaname = :s AND tablename = :t "
                                "ORDER BY indexname"
                            ),
                            {"s": schema, "t": table},
                        )
                    ).all()
                    checks = (
                        await conn.execute(
                            text(
                                "SELECT conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
                                "JOIN pg_class r ON r.oid = c.conrelid "
                                "JOIN pg_namespace n ON n.oid = r.relnamespace "
                                "WHERE n.nspname = :s AND r.relname = :t "
                                "AND c.contype IN ('c', 'u', 'p') ORDER BY conname"
                            ),
                            {"s": schema, "t": table},
                        )
                    ).all()
                    out[table] = {
                        "columns": [tuple(c) for c in cols],
                        "indexes": [tuple(i) for i in indexes],
                        "constraints": [tuple(c) for c in checks],
                    }
                return out
        finally:
            await engine.dispose()

    shared = asyncio.run(describe(None, "public"))
    plane = asyncio.run(describe(db, "documind"))
    for table in shared:
        assert plane[table] == shared[table], table
    # The derived metadata knows the same tables.
    assert set(dataplane_metadata().tables) - set(DataPlaneBase.metadata.tables) == set(shared)
