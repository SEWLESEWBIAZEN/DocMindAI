"""Live checks against real AWS S3, Stripe, Slack and Google, without accounts (opt-in).

Run:  uv run pytest -m live tests/live/test_providers_live.py -v -s

No credentials are needed or sent: every check uses deliberately invalid ones, so
the services answer with their real error responses. That proves the network path,
the request format DocuMind sends, and how each real error is reported to people.
It does not prove the happy path; see docs/production-readiness/02 for what does.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from documind.application.billing.gateway import BillingGatewayError
from documind.core.config import Settings
from documind.domain.infrastructure.profiles import StorageConfig
from documind.infrastructure.billing.stripe_gateway import StripeGateway
from documind.infrastructure.connectors.google_drive import (
    ConnectorAuthError,
    GoogleDriveClient,
)
from documind.infrastructure.integrations.slack import SlackClient, SlackError
from documind.infrastructure.sso.oidc import GOOGLE_ISSUER, OidcClient, OidcError
from documind.infrastructure.storage.factory import validate_storage

pytestmark = pytest.mark.live

FAKE_SECRET = "documind-invalid-" + uuid.uuid4().hex  # never a real credential


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_aws_s3_rejects_an_unknown_access_key_and_we_say_so() -> None:
    config = StorageConfig(
        provider="aws_s3",
        bucket=f"documind-verify-{uuid.uuid4().hex[:12]}",
        region="us-east-1",
        access_key_id="AKIAIOSFODNN7INVALID",
    )
    check = run(validate_storage(Settings(), config, FAKE_SECRET))
    print("aws s3 ->", check)
    assert check.ok is False
    assert check.code == "storage_rejected"
    assert check.message == "The access key isn't valid."
    assert FAKE_SECRET not in str(check)


def test_stripe_rejects_an_invalid_key_without_leaking_it() -> None:
    gateway = StripeGateway(secret_key="sk_test_" + FAKE_SECRET, webhook_secret="whsec_unused")
    with pytest.raises(BillingGatewayError) as caught:
        run(
            gateway.create_customer(
                email="probe@example.com", name="Probe", workspace_id=uuid.uuid4()
            )
        )
    print("stripe ->", type(caught.value).__name__, caught.value)
    assert str(caught.value) == "AuthenticationError"
    assert FAKE_SECRET not in str(caught.value)


def test_slack_rejects_invalid_app_credentials_and_tokens() -> None:
    async def go() -> tuple[str, str]:
        client = SlackClient(client_id="0000000000.0000000000", client_secret=FAKE_SECRET)
        out: list[str] = []
        for call in (
            client.exchange_code(code="invalid", redirect_uri="https://example.com/cb"),
            client.post_message("xoxb-" + FAKE_SECRET, channel="C000", text="probe"),
        ):
            try:
                await call
            except SlackError as exc:
                out.append(str(exc))
        return out[0], out[1]

    install, post = run(go())
    print("slack oauth.v2.access ->", install)
    print("slack chat.postMessage ->", post)
    assert install.startswith("oauth.v2.access: invalid_")
    assert post == "chat.postMessage: invalid_auth"


def test_google_openid_discovery_and_keys_are_real_and_bad_codes_fail() -> None:
    oidc = OidcClient()
    config = run(oidc.discover(GOOGLE_ISSUER))
    print("google discovery ->", config)
    assert config.token_endpoint.startswith("https://oauth2.googleapis.com/")
    with pytest.raises(OidcError) as caught:
        run(
            oidc.identity(
                config,
                client_id="000000000000-invalid.apps.googleusercontent.com",
                client_secret=FAKE_SECRET,
                code="invalid",
                code_verifier="x" * 43,
                redirect_uri="https://example.com/cb",
                nonce="n",
            )
        )
    print("google token exchange ->", caught.value)


def test_google_drive_rejects_bad_clients_and_tokens() -> None:
    drive = GoogleDriveClient(
        client_id="000000000000-invalid.apps.googleusercontent.com", client_secret=FAKE_SECRET
    )
    with pytest.raises(ConnectorAuthError) as refresh:
        run(drive.access_token("1//invalid-refresh-token"))
    print("drive refresh ->", refresh.value)
    with pytest.raises(ConnectorAuthError) as listing:
        run(drive.folders("ya29.invalid-access-token", None))
    print("drive files.list ->", listing.value)
