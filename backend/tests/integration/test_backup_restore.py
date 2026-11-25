# ruff: noqa: S603, S607 - runs the repository's own backup scripts and CLI
"""Backup security and a full restore drill (Phase 9).

Runs the operator scripts (``infra/backup/backup.sh`` and ``restore.sh``) against
the real stack, then starts a second app on the restored database and bucket and
checks that users, documents, search and encrypted AI keys all work.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from tests.integration.conftest import Api
from tests.integration.test_byok_api import KEY, WORKER, FakeFactory, add_key
from tests.integration.test_infrastructure import _admin, _admin_url, _s3
from tests.integration.test_processing_pipeline import ws
from tests.integration.test_usage_and_quotas import POLICY, run_metered_job

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
RESTORE_DB = "documind_restore_test"
RESTORE_BUCKET = "documind-restore-test"
PASSPHRASE = "correct horse battery staple, for backups"


def _env(settings: Any, **overrides: str) -> dict[str, str]:
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url.get_secret_value(),
        "REDIS_URL": settings.redis_url.get_secret_value(),
        "S3_ENDPOINT_URL": settings.s3_endpoint_url or "",
        "S3_ACCESS_KEY_ID": settings.s3_access_key_id.get_secret_value(),
        "S3_SECRET_ACCESS_KEY": settings.s3_secret_access_key.get_secret_value(),
        "S3_BUCKET": settings.s3_bucket,
        "JWT_SECRET_KEY": settings.jwt_secret_key.get_secret_value(),
        "MFA_ENCRYPTION_KEY": settings.mfa_encryption_key.get_secret_value(),
        "EMBEDDING_PROVIDER": "local",
        "LLM_PROVIDER": "local",
        "DOCUMIND_MANAGE": f"{sys.executable} -m documind.cli",
    }
    env.update(overrides)
    return env


def _libpq(settings: Any, database: str | None = None) -> str:
    url = _admin_url(settings)
    if database:
        url = url.set(database=database)
    return str(url.set(drivername="postgresql").render_as_string(hide_password=False))


def run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "infra" / "backup" / script), *args],
        env=env,
        cwd=ROOT / "backend",
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def restore_targets(identity_settings: Any) -> Any:
    url = _admin_url(identity_settings)
    asyncio.run(
        _admin(
            url,
            f"DROP DATABASE IF EXISTS {RESTORE_DB} WITH (FORCE)",
            f"CREATE DATABASE {RESTORE_DB}",
        )
    )
    client = _s3(identity_settings)
    with contextlib.suppress(client.exceptions.BucketAlreadyOwnedByYou):
        client.create_bucket(Bucket=RESTORE_BUCKET)
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=RESTORE_BUCKET):
        for obj in page.get("Contents", []):
            client.delete_object(Bucket=RESTORE_BUCKET, Key=obj["Key"])
    yield
    asyncio.run(_admin(url, f"DROP DATABASE IF EXISTS {RESTORE_DB} WITH (FORCE)"))


def test_encrypted_backup_and_full_restore(
    make_api: Any, tmp_path: Path, restore_targets: None
) -> None:
    factory = FakeFactory()
    WORKER.overrides = {"provider_factory": factory}
    api: Api = make_api({"provider_factory": factory})
    h = api.signup("owner@example.com")
    w = ws(api, h)
    from tests.integration.test_documents_api import upload

    upload(api, w, h, POLICY, "policy.md")
    assert run_metered_job(api, api.dispatcher.dispatched[-1], WORKER.get()).status == "succeeded"
    secret, _ = api.enable_mfa(h)
    add_key(api, w, api.step_up(h, secret))

    passphrase = tmp_path / "passphrase"
    passphrase.write_text(PASSPHRASE)
    backups = tmp_path / "backups"
    env = _env(
        api.settings,
        PGURL=_libpq(api.settings),
        BACKUP_DIR=str(backups),
        BACKUP_PASSPHRASE_FILE=str(passphrase),
        CREDENTIAL_ENCRYPTION_KEYS=api.settings.credential_encryption_keys.get_secret_value(),  # type: ignore[union-attr]
    )
    made = run("backup.sh", env)
    assert made.returncode == 0, made.stderr
    (archive,) = backups.glob("documind-*.tar.dmbk")
    assert oct(archive.stat().st_mode & 0o777) == "0o600"

    # Opaque: no document text, no user email, no key, no credential key ring.
    blob = archive.read_bytes()
    for needle in (b"fifty thousand", b"owner@example.com", KEY.encode(), b"dGVzdC1jcmVkZW50aWFs"):
        assert needle not in blob

    # Wrong passphrase and tampering are both detected.
    manage = [sys.executable, "-m", "documind.cli", "backup", "unseal"]
    wrong = tmp_path / "wrong"
    wrong.write_text("not the right passphrase at all")
    out = tmp_path / "out.tar"
    assert (
        subprocess.run(
            [*manage, str(archive), str(out), "--passphrase-file", str(wrong)],
            env=env,
            cwd=ROOT / "backend",
            capture_output=True,
            check=False,
        ).returncode
        == 2
    )
    tampered = tmp_path / "tampered.dmbk"
    data = bytearray(blob)
    data[len(data) // 2] ^= 0x01
    tampered.write_bytes(bytes(data))
    assert (
        subprocess.run(
            [*manage, str(tampered), str(out), "--passphrase-file", str(passphrase)],
            env=env,
            cwd=ROOT / "backend",
            capture_output=True,
            check=False,
        ).returncode
        == 2
    )
    truncated = tmp_path / "truncated.dmbk"
    truncated.write_bytes(blob[: len(blob) - 100])
    assert (
        subprocess.run(
            [*manage, str(truncated), str(out), "--passphrase-file", str(passphrase)],
            env=env,
            cwd=ROOT / "backend",
            capture_output=True,
            check=False,
        ).returncode
        == 2
    )

    # Restore into an empty database and bucket.
    restore_env = {**env, "PGURL": _libpq(api.settings, RESTORE_DB), "S3_BUCKET": RESTORE_BUCKET}
    restored = run("restore.sh", restore_env, str(archive))
    assert restored.returncode == 0, restored.stderr
    s3 = _s3(api.settings)

    def objects(bucket: str) -> dict[str, bytes]:
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])]
        return {k: s3.get_object(Bucket=bucket, Key=k)["Body"].read() for k in keys}

    original = objects(api.settings.s3_bucket)
    assert original
    assert objects(RESTORE_BUCKET) == original  # every object, byte for byte
    # Restoring over data is refused.
    again = run("restore.sh", restore_env, str(archive))
    assert again.returncode != 0
    assert "not empty" in again.stderr

    # A second app on the restored copy: sign in, read, search, and use the encrypted key.
    restored_db = make_url_str(api.settings.database_url.get_secret_value(), RESTORE_DB)
    copy: Api = make_api(
        {"provider_factory": factory},
        database_url=SecretStr(restored_db),
        s3_bucket=RESTORE_BUCKET,
        _skip_reset=True,
    )
    # The owner has 2FA: signing in needs a code, which also proves the encrypted
    # TOTP secret survived the restore (ADR 0026).
    challenge = copy.login("owner@example.com").json()["challenge_token"]
    hc = copy.client.post(
        "/api/v1/auth/login/mfa", json={"challenge_token": challenge, "code": copy.totp(secret)}
    ).json()
    headers = {"Authorization": f"Bearer {hc['access_token']}"}
    docs = copy.client.get(f"/api/v1/workspaces/{w}/documents", headers=headers).json()
    assert docs["total"] == 1
    doc = docs["items"][0]["id"]
    signed = copy.client.get(
        f"/api/v1/workspaces/{w}/documents/{doc}/download-url", headers=headers
    )
    assert signed.status_code == 200
    assert RESTORE_BUCKET in signed.json()["url"]
    factory.state["secrets"].clear()
    hits = copy.client.post(
        f"/api/v1/workspaces/{w}/search", json={"query": "renewals Legal"}, headers=headers
    )
    assert hits.json()["results"]
    assert factory.state["secrets"] == [KEY]  # the vault decrypted the restored key


def make_url_str(url: str, database: str) -> str:
    from sqlalchemy.engine import make_url

    return str(make_url(url).set(database=database).render_as_string(hide_password=False))
