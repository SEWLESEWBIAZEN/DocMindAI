"""Infrastructure profiles and the outbound host policy (ADR 0021)."""

from __future__ import annotations

import uuid

import pytest

from documind.domain.billing.plans import PLANS, PlanKey
from documind.domain.infrastructure.profiles import (
    PLATFORM_DEFAULT,
    ProfileError,
    check_entitlements,
    from_stored,
    parse_config,
    requires_migration,
)
from documind.infrastructure.network.ssrf import (
    BlockedHostError,
    blocked_reason,
    check_host,
    check_url,
)

EXTERNAL = {
    "mode": "external",
    "host": "DB.Example.com",
    "database": "docs",
    "username": "documind",
}
MINIO = {
    "provider": "minio",
    "bucket": "acme-docs",
    "endpoint_url": "https://minio.example.com",
    "access_key_id": "AKIAEXAMPLE",
    "prefix": "/documind/",
}


def test_defaults_are_the_platform() -> None:
    config = parse_config({})
    assert config == PLATFORM_DEFAULT
    assert config.is_platform_default


def test_external_database_defaults_to_verified_tls() -> None:
    db = parse_config({"database": EXTERNAL}).database
    assert (db.host, db.port, db.sslmode) == ("db.example.com", 5432, "verify-full")


@pytest.mark.parametrize(
    "raw",
    [
        {"database": {"mode": "external", "host": "db.example.com"}},
        {"database": {**EXTERNAL, "host": "bad host"}},
        {"database": {**EXTERNAL, "port": 70000}},
        {"storage": {**MINIO, "bucket": "Bad_Bucket"}},
        {"storage": {**MINIO, "prefix": "../escape"}},
        {"storage": {"provider": "aws_s3", "bucket": "acme-docs", "access_key_id": "AKIA0"}},
        {"storage": {"provider": "azure_blob", "bucket": "x-container"}},
        {"processing": {"mode": "cluster"}},
    ],
)
def test_invalid_configurations_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ProfileError):
        parse_config(raw)


def test_checksum_is_stable_and_detects_changes() -> None:
    config = parse_config({"database": EXTERNAL, "storage": MINIO})
    assert config.storage.prefix == "documind"
    assert from_stored(config.to_dict()).checksum() == config.checksum()
    tampered = config.to_dict()
    tampered["database"]["host"] = "evil.example.com"
    assert from_stored(tampered).checksum() != config.checksum()


def test_entitlements_gate_each_choice() -> None:
    free = PLANS[PlanKey.FREE].entitlements
    with pytest.raises(ProfileError, match="storage"):
        check_entitlements(parse_config({"storage": MINIO}), free)
    with pytest.raises(ProfileError, match="dedicated"):
        check_entitlements(parse_config({"database": {"mode": "dedicated"}}), free)
    business = PLANS[PlanKey.BUSINESS].entitlements
    check_entitlements(parse_config({"database": EXTERNAL, "storage": MINIO}), business)
    with pytest.raises(ProfileError, match="processing"):
        check_entitlements(parse_config({"processing": {"mode": "customer"}}), business)


def test_migration_is_needed_only_when_data_would_move() -> None:
    external = parse_config({"database": EXTERNAL})
    assert requires_migration(PLATFORM_DEFAULT, external) == ["database"]
    rotated = from_stored(
        {
            **external.to_dict(),
            "database": {
                **external.to_dict()["database"],
                "password_credential_id": str(uuid.uuid4()),
            },
        }
    )
    assert requires_migration(external, rotated) == []  # same location, new password
    assert requires_migration(PLATFORM_DEFAULT, parse_config({"storage": MINIO})) == ["storage"]
    assert (
        requires_migration(PLATFORM_DEFAULT, parse_config({"processing": {"mode": "hybrid"}})) == []
    )


@pytest.mark.parametrize(
    ("address", "blocked"),
    [
        ("127.0.0.1", True),
        ("10.0.0.5", True),
        ("172.16.3.4", True),
        ("192.168.1.1", True),
        ("169.254.169.254", True),
        ("100.64.0.1", True),
        ("::1", True),
        ("fd00:ec2::254", True),
        ("::ffff:10.0.0.1", True),
        ("0.0.0.0", True),  # noqa: S104 - a test value, nothing binds
        ("8.8.8.8", False),
        ("2606:4700:4700::1111", False),
    ],
)
def test_blocked_addresses(address: str, blocked: bool) -> None:
    assert (blocked_reason(address) is not None) is blocked


async def test_literal_private_hosts_are_blocked_unless_allowlisted() -> None:
    with pytest.raises(BlockedHostError):
        await check_host("169.254.169.254", 5432, allowed=frozenset())
    assert await check_host("127.0.0.1", 5432, allowed=frozenset({"127.0.0.1"})) == ["127.0.0.1"]


async def test_endpoint_urls_must_be_plain_https() -> None:
    for url in (
        "ftp://example.com",
        "https://user:pw@example.com",
        "https://example.com/path",
        "http://8.8.8.8",
        "https://10.0.0.1",
    ):
        with pytest.raises(BlockedHostError):
            await check_url(url, allowed=frozenset())
    assert await check_url("http://localhost:9000", allowed=frozenset({"localhost"})) == (
        "localhost",
        9000,
    )
