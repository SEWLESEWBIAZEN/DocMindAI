"""WebAuthn relying party for passkeys (ADR 0029), on top of ``py_webauthn``.

The application layer only sees plain dicts (the browser's JSON) and bytes.
Every failure becomes ``PasskeyError`` with a message safe to show.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


class PasskeyError(ValueError):
    """The browser's response was malformed, or didn't verify."""


@dataclass(frozen=True, slots=True)
class NewPasskey:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    backed_up: bool
    transports: list[str]


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def client_challenge(credential: dict[str, Any]) -> bytes:
    """The challenge the browser signed, read from clientDataJSON (used to find ours)."""
    try:
        client_data = json.loads(_b64url_decode(credential["response"]["clientDataJSON"]))
        return _b64url_decode(str(client_data["challenge"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PasskeyError("The passkey response was malformed.") from exc


def credential_id(credential: dict[str, Any]) -> bytes:
    try:
        return _b64url_decode(str(credential["rawId"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PasskeyError("The passkey response was malformed.") from exc


def _transports(values: list[str]) -> list[AuthenticatorTransport]:
    known = {t.value for t in AuthenticatorTransport}
    return [AuthenticatorTransport(v) for v in values if v in known]


class RelyingParty:
    def __init__(self, rp_id: str, rp_name: str, origins: list[str]) -> None:
        self.rp_id = rp_id
        self._name = rp_name
        self._origins = origins

    def registration_options(
        self,
        *,
        user_handle: bytes,
        user_name: str,
        display_name: str,
        exclude: list[tuple[bytes, list[str]]],
    ) -> tuple[dict[str, Any], bytes]:
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name=self._name,
            user_id=user_handle,
            user_name=user_name,
            user_display_name=display_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
            # Don't register the same authenticator twice.
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=cid, transports=_transports(t))
                for cid, t in exclude
            ],
        )
        return json.loads(options_to_json(options)), options.challenge

    def verify_registration(self, credential: dict[str, Any], challenge: bytes) -> NewPasskey:
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self._origins,
            )
        except (WebAuthnException, KeyError, TypeError, ValueError) as exc:
            raise PasskeyError("The passkey couldn't be verified. Try again.") from exc
        transports = credential.get("response", {}).get("transports") or []
        return NewPasskey(
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            backed_up=bool(verified.credential_backed_up),
            transports=[str(t) for t in transports][:8],
        )

    def authentication_options(
        self, allow: list[tuple[bytes, list[str]]]
    ) -> tuple[dict[str, Any], bytes]:
        options = generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=cid, transports=_transports(t)) for cid, t in allow
            ],
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        return json.loads(options_to_json(options)), options.challenge

    def verify_authentication(
        self, credential: dict[str, Any], challenge: bytes, public_key: bytes, sign_count: int
    ) -> int:
        """Returns the new signature counter."""
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self._origins,
                credential_public_key=public_key,
                credential_current_sign_count=sign_count,
            )
        except (WebAuthnException, KeyError, TypeError, ValueError) as exc:
            raise PasskeyError("The passkey couldn't be verified. Try again.") from exc
        return int(verified.new_sign_count)
