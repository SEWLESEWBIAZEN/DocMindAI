from collections.abc import Iterator
from typing import Any

import pytest
from botocore.stub import Stubber

from documind.core.config import Settings
from documind.domain.health import DependencyCheckError
from documind.infrastructure.storage.bootstrap import PublicBucketError, ensure_private_bucket
from documind.infrastructure.storage.client import create_s3_client
from documind.infrastructure.storage.health import ObjectStorageHealthCheck

BUCKET = "documind-test"


@pytest.fixture
def stubbed(settings: Settings) -> Iterator[tuple[Any, Stubber]]:
    client = create_s3_client(settings)
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def test_bootstrap_creates_missing_bucket(stubbed: tuple[Any, Stubber]) -> None:
    client, stub = stubbed
    stub.add_client_error("head_bucket", "404", expected_params={"Bucket": BUCKET})
    stub.add_response("create_bucket", {}, expected_params={"Bucket": BUCKET})
    stub.add_client_error("get_bucket_policy", "NoSuchBucketPolicy")
    result = ensure_private_bucket(client, BUCKET, "us-east-1")
    assert result.created is True


def test_bootstrap_uses_location_constraint_outside_us_east_1(stubbed: tuple[Any, Stubber]) -> None:
    client, stub = stubbed
    stub.add_client_error("head_bucket", "404")
    stub.add_response(
        "create_bucket",
        {},
        expected_params={
            "Bucket": BUCKET,
            "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"},
        },
    )
    stub.add_client_error("get_bucket_policy", "NoSuchBucketPolicy")
    ensure_private_bucket(client, BUCKET, "eu-west-1")


def test_bootstrap_is_idempotent(stubbed: tuple[Any, Stubber]) -> None:
    client, stub = stubbed
    stub.add_response("head_bucket", {})
    stub.add_client_error("get_bucket_policy", "NoSuchBucketPolicy")
    assert ensure_private_bucket(client, BUCKET, "us-east-1").created is False


def test_bootstrap_rejects_bucket_with_policy(stubbed: tuple[Any, Stubber]) -> None:
    client, stub = stubbed
    stub.add_response("head_bucket", {})
    stub.add_response("get_bucket_policy", {"Policy": "{}"})
    with pytest.raises(PublicBucketError):
        ensure_private_bucket(client, BUCKET, "us-east-1")


def test_bootstrap_propagates_unexpected_errors(stubbed: tuple[Any, Stubber]) -> None:
    from botocore.exceptions import ClientError

    client, stub = stubbed
    stub.add_client_error("head_bucket", "403")
    with pytest.raises(ClientError):
        ensure_private_bucket(client, BUCKET, "us-east-1")


async def test_health_ok(stubbed: tuple[Any, Stubber]) -> None:
    client, stub = stubbed
    stub.add_response("head_bucket", {}, expected_params={"Bucket": BUCKET})
    assert await ObjectStorageHealthCheck(client, BUCKET).check() == {"bucket": BUCKET}


@pytest.mark.parametrize(("code", "message"), [("404", "does not exist"), ("403", "denied")])
async def test_health_maps_known_errors(
    stubbed: tuple[Any, Stubber], code: str, message: str
) -> None:
    client, stub = stubbed
    stub.add_client_error("head_bucket", code)
    with pytest.raises(DependencyCheckError, match=message):
        await ObjectStorageHealthCheck(client, BUCKET).check()
