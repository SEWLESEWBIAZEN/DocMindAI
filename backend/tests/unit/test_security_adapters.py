import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from documind.domain.errors import AuthenticationError
from documind.domain.identity.tokens import generate_token, hash_token
from documind.infrastructure.security.jwt_tokens import JwtAccessTokenService
from documind.infrastructure.security.passwords import Argon2PasswordHasher

SECRET = "unit-test-secret-key-0123456789abcdefghijkl"


@pytest.fixture
def hasher() -> Argon2PasswordHasher:
    return Argon2PasswordHasher(time_cost=1, memory_kib=8, parallelism=1)


@pytest.fixture
def tokens() -> JwtAccessTokenService:
    return JwtAccessTokenService(
        secret=SECRET, issuer="documind", audience="documind-api", ttl_seconds=900
    )


class TestPasswords:
    async def test_hash_and_verify(self, hasher: Argon2PasswordHasher) -> None:
        h = await hasher.hash("correct horse battery")
        assert h.startswith("$argon2id$")
        assert await hasher.verify(h, "correct horse battery")
        assert not await hasher.verify(h, "wrong password!!")

    async def test_garbage_hash_is_rejected_not_raised(self, hasher: Argon2PasswordHasher) -> None:
        assert not await hasher.verify("not-a-hash", "anything")
        assert hasher.needs_rehash("not-a-hash")

    async def test_needs_rehash_when_parameters_increase(
        self, hasher: Argon2PasswordHasher
    ) -> None:
        h = await hasher.hash("correct horse battery")
        assert not hasher.needs_rehash(h)
        stronger = Argon2PasswordHasher(time_cost=2, memory_kib=16, parallelism=1)
        assert stronger.needs_rehash(h)

    async def test_dummy_verify_runs(self, hasher: Argon2PasswordHasher) -> None:
        await hasher.verify_dummy("anything")


class TestOpaqueTokens:
    def test_tokens_are_random_and_hash_is_stable(self) -> None:
        a, b = generate_token(), generate_token()
        assert a != b
        assert len(a) >= 43
        assert hash_token(a) == hash_token(a)
        assert hash_token(a) != hash_token(b)
        assert len(hash_token(a)) == 64


class TestJwt:
    def test_round_trip(self, tokens: JwtAccessTokenService) -> None:
        user_id = uuid.uuid4()
        issued = tokens.issue(user_id, auth_version=3)
        assert issued.expires_in == 900
        claims = tokens.decode(issued.token)
        assert claims.user_id == user_id
        assert claims.auth_version == 3

    def _forge(self, **overrides: object) -> str:
        now = datetime.now(UTC)
        claims: dict[str, object] = {
            "iss": "documind",
            "aud": "documind-api",
            "sub": str(uuid.uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
            "typ": "access",
            "ver": 1,
        }
        claims.update(overrides)
        return jwt.encode(
            {k: v for k, v in claims.items() if v is not None}, SECRET, algorithm="HS256"
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"exp": datetime.now(UTC) - timedelta(minutes=5)},
            {"aud": "someone-else"},
            {"iss": "evil"},
            {"typ": "refresh"},
            {"ver": None},
            {"sub": "not-a-uuid"},
        ],
    )
    def test_invalid_claims_rejected(
        self, tokens: JwtAccessTokenService, overrides: dict[str, object]
    ) -> None:
        with pytest.raises(AuthenticationError):
            tokens.decode(self._forge(**overrides))

    def test_mfa_claim_round_trip(self, tokens: JwtAccessTokenService) -> None:
        verified = datetime(2026, 11, 23, 12, 0, 5, tzinfo=UTC)
        claims = tokens.decode(tokens.issue(uuid.uuid4(), 1, mfa_verified_at=verified).token)
        assert claims.mfa_verified_at == verified
        assert tokens.decode(tokens.issue(uuid.uuid4(), 1).token).mfa_verified_at is None

    def test_wrong_key_and_tampering_rejected(self, tokens: JwtAccessTokenService) -> None:
        other = JwtAccessTokenService(
            secret="another-secret-key-0123456789abcdefghijkl",
            issuer="documind",
            audience="documind-api",
            ttl_seconds=900,
        )
        with pytest.raises(AuthenticationError):
            tokens.decode(other.issue(uuid.uuid4(), 1).token)
        token = tokens.issue(uuid.uuid4(), 1).token
        header, payload, signature = token.split(".")
        with pytest.raises(AuthenticationError):
            tokens.decode(f"{header}.{payload}x.{signature}")

    def test_alg_none_rejected(self, tokens: JwtAccessTokenService) -> None:
        unsigned = jwt.encode({"sub": str(uuid.uuid4())}, key="", algorithm="none")
        with pytest.raises(AuthenticationError):
            tokens.decode(unsigned)

    def test_garbage_rejected(self, tokens: JwtAccessTokenService) -> None:
        with pytest.raises(AuthenticationError):
            tokens.decode("not.a.jwt")
