import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from documind.domain.identity import totp
from documind.infrastructure.security.secret_box import SecretBox, SecretDecryptionError, key_id

# RFC 6238 Appendix B test secret (ASCII "12345678901234567890"), SHA-1.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize(
    ("unix", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc6238_vectors(unix: int, expected: str) -> None:
    assert totp.code_at_step(RFC_SECRET, totp.time_step(unix), digits=8) == expected


class TestVerify:
    secret = totp.generate_secret()
    now = 1_800_000_000.0

    def code(self, offset_steps: int = 0) -> str:
        return totp.code_at_step(self.secret, totp.time_step(self.now) + offset_steps)

    def test_accepts_current_and_adjacent_steps(self) -> None:
        current = totp.time_step(self.now)
        for offset in (-1, 0, 1):
            assert (
                totp.verify(self.secret, self.code(offset), self.now, last_used_step=None)
                == current + offset
            )

    def test_rejects_outside_window_and_garbage(self) -> None:
        for bad in (self.code(-2), self.code(2), "12345", "abcdef", "", "1234567"):
            assert totp.verify(self.secret, bad, self.now, last_used_step=None) is None

    def test_allows_spaces(self) -> None:
        c = self.code()
        assert (
            totp.verify(self.secret, f"{c[:3]} {c[3:]}", self.now, last_used_step=None) is not None
        )

    def test_replay_and_older_codes_rejected(self) -> None:
        step = totp.verify(self.secret, self.code(), self.now, last_used_step=None)
        assert step is not None
        assert totp.verify(self.secret, self.code(), self.now, last_used_step=step) is None
        assert totp.verify(self.secret, self.code(-1), self.now, last_used_step=step) is None
        assert totp.verify(self.secret, self.code(1), self.now, last_used_step=step) == step + 1

    def test_secret_format(self) -> None:
        assert len(self.secret) == 32
        assert base64.b32decode(self.secret)


def test_provisioning_uri() -> None:
    uri = totp.provisioning_uri("JBSWY3DPEHPK3PXP", "ada+test@example.com", "DocuMind")
    parts = urlsplit(uri)
    assert parts.scheme == "otpauth"
    assert parts.netloc == "totp"
    assert parts.path == "/DocuMind%3Aada%2Btest%40example.com"
    query = parse_qs(parts.query)
    assert query["secret"] == ["JBSWY3DPEHPK3PXP"]
    assert query["issuer"] == ["DocuMind"]
    assert query["digits"] == ["6"]


class TestRecoveryCodes:
    def test_format_and_uniqueness(self) -> None:
        codes = totp.generate_recovery_codes()
        assert len(codes) == 10
        assert len(set(codes)) == 10
        assert all(len(c) == 14 and c.count("-") == 2 for c in codes)

    def test_hash_normalizes_and_depends_on_pepper(self) -> None:
        h = totp.hash_recovery_code("ABCD-efgh-2345", b"pepper")
        assert h == totp.hash_recovery_code(" abcd efgh 2345 ", b"pepper")
        assert h != totp.hash_recovery_code("abcd-efgh-2345", b"other")


class TestSecretBox:
    box = SecretBox(b"k" * 32)

    def test_round_trip_is_randomised(self) -> None:
        a = self.box.encrypt("JBSWY3DPEHPK3PXP", context="user-1")
        b = self.box.encrypt("JBSWY3DPEHPK3PXP", context="user-1")
        assert a != b
        assert a.startswith(f"v2:{key_id(b'k' * 32)}:")
        assert "JBSWY3DPEHPK3PXP" not in a
        assert self.box.decrypt(a, context="user-1") == "JBSWY3DPEHPK3PXP"

    def test_bound_to_context_key_and_integrity(self) -> None:
        token = self.box.encrypt("secret", context="user-1")
        with pytest.raises(SecretDecryptionError):
            self.box.decrypt(token, context="user-2")
        with pytest.raises(SecretDecryptionError):
            SecretBox(b"x" * 32).decrypt(token, context="user-1")
        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with pytest.raises(SecretDecryptionError):
            self.box.decrypt(tampered, context="user-1")
        with pytest.raises(SecretDecryptionError):
            self.box.decrypt("v0:abc", context="user-1")

    def test_key_length(self) -> None:
        with pytest.raises(ValueError, match="32-byte"):
            SecretBox(b"short")
        with pytest.raises(ValueError, match="32-byte"):
            SecretBox(b"k" * 32, [b"short"])


def _legacy_v1(key: bytes, plaintext: str, context: str) -> str:
    """The format written before key rotation existed (no key id)."""
    import base64
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode(), context.encode())
    return "v1:" + base64.urlsafe_b64encode(nonce + sealed).decode()


class TestKeyRotation:
    old, new = b"o" * 32, b"n" * 32

    def test_legacy_ciphertexts_still_decrypt_with_the_old_key_as_previous(self) -> None:
        legacy = _legacy_v1(self.old, "SECRET", "totp:1")
        assert SecretBox(self.old).decrypt(legacy, context="totp:1") == "SECRET"
        rotated = SecretBox(self.new, [self.old])
        assert rotated.decrypt(legacy, context="totp:1") == "SECRET"
        assert rotated.is_current(legacy) is False
        # Without the old key it can't be read: rewrap before retiring it.
        with pytest.raises(SecretDecryptionError):
            SecretBox(self.new).decrypt(legacy, context="totp:1")

    def test_new_ciphertexts_name_their_key(self) -> None:
        old_box = SecretBox(self.old)
        token = old_box.encrypt("SECRET", context="totp:1")
        rotated = SecretBox(self.new, [self.old])
        assert rotated.decrypt(token, context="totp:1") == "SECRET"
        assert rotated.is_current(token) is False
        rewrapped = rotated.encrypt(rotated.decrypt(token, context="totp:1"), context="totp:1")
        assert rotated.is_current(rewrapped) is True
        assert SecretBox(self.new).decrypt(rewrapped, context="totp:1") == "SECRET"
        with pytest.raises(SecretDecryptionError, match="not configured"):
            SecretBox(self.new).decrypt(token, context="totp:1")
