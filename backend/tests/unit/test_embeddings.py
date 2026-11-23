import json
import math
import uuid

import httpx2
import pytest

from documind.application.ports import EmbeddingBatch
from documind.application.processing.embedding_service import EmbeddingService, content_hash
from documind.domain.processing.errors import PermanentProcessingError, TransientProcessingError
from documind.infrastructure.embeddings.local_provider import LocalHashingEmbeddingProvider
from documind.infrastructure.embeddings.openai_provider import OpenAIEmbeddingProvider

DIMS = 8


def openai_body(
    vectors: list[list[float]], *, reverse: bool = False, tokens: int = 7
) -> dict[str, object]:
    data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    if reverse:
        data.reverse()
    return {
        "object": "list",
        "data": data,
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


def provider(handler: object, *, max_retries: int = 2) -> OpenAIEmbeddingProvider:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))  # type: ignore[arg-type]
    return OpenAIEmbeddingProvider(
        api_key="sk-test",
        model="text-embedding-3-small",
        dimensions=DIMS,
        max_retries=max_retries,
        http_client=client,
    )


class TestOpenAIProvider:
    async def test_request_shape_and_ordering(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(
                200, json=openai_body([[1.0] * DIMS, [2.0] * DIMS], reverse=True)
            )

        result = await provider(handler).embed(["first", "second"])
        assert result.vectors == [[1.0] * DIMS, [2.0] * DIMS]  # re-sorted by index
        assert result.input_tokens == 7
        body = json.loads(seen[0].content)
        assert body == {
            "model": "text-embedding-3-small",
            "input": ["first", "second"],
            "dimensions": DIMS,
            "encoding_format": "float",
        }
        assert seen[0].headers["authorization"] == "Bearer sk-test"
        assert seen[0].url.path.endswith("/embeddings")

    async def test_rate_limit_is_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx2.Response(
                    429, json={"error": {"message": "slow down"}}, headers={"retry-after-ms": "10"}
                )
            return httpx2.Response(200, json=openai_body([[0.5] * DIMS]))

        result = await provider(handler).embed(["x"])
        assert calls["n"] == 2
        assert result.vectors == [[0.5] * DIMS]

    @pytest.mark.parametrize(
        ("status", "error", "code"),
        [
            (429, TransientProcessingError, "embedding_rate_limited"),
            (503, TransientProcessingError, "embedding_unavailable"),
            (401, TransientProcessingError, "embedding_misconfigured"),
            (400, PermanentProcessingError, "embedding_rejected"),
        ],
    )
    async def test_error_classification(
        self, status: int, error: type[Exception], code: str
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                status, json={"error": {"message": "nope"}}, headers={"retry-after-ms": "1"}
            )

        with pytest.raises(error) as exc:
            await provider(handler, max_retries=1).embed(["x"])
        assert exc.value.code == code  # type: ignore[attr-defined]

    async def test_wrong_dimensions_rejected(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=openai_body([[1.0] * (DIMS + 1)]))

        with pytest.raises(PermanentProcessingError, match="dimensions"):
            await provider(handler).embed(["x"])


class TestLocalProvider:
    async def test_deterministic_normalised_and_lexically_similar(self) -> None:
        p = LocalHashingEmbeddingProvider(256)
        result = await p.embed(
            ["renewal notice period", "renewal notice period", "quarterly revenue growth", ""]
        )
        a, b, c, empty = result.vectors
        assert a == b
        assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0)
        assert any(empty)

        related = (await p.embed(["notice period for renewal"])).vectors[0]

        def cos(x: list[float], y: list[float]) -> float:
            return sum(i * j for i, j in zip(x, y, strict=True))

        assert cos(a, related) > cos(a, c)


class FakeProvider:
    model = "fake"
    dimensions = 3

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.calls.append(texts)
        return EmbeddingBatch(
            vectors=[[float(len(t)), 0.0, 1.0] for t in texts], input_tokens=len(texts)
        )


class FakeCache:
    def __init__(self, known: dict[str, list[float]]) -> None:
        self.known = known

    async def find_embeddings(
        self, workspace_id: uuid.UUID, hashes: set[str], model: str, dims: int
    ) -> dict[str, list[float]]:
        return {h: v for h, v in self.known.items() if h in hashes}


class TestEmbeddingService:
    async def test_reuses_cached_and_deduplicates(self) -> None:
        cached = content_hash("fake", 3, "already embedded")
        fake = FakeProvider()
        service = EmbeddingService(fake, FakeCache({cached: [9.0, 9.0, 9.0]}), batch_size=2)
        result = await service.embed(uuid.uuid4(), ["already embedded", "a", "bb", "a", "ccc"])
        assert result.vectors == [
            [9.0, 9.0, 9.0],
            [1.0, 0.0, 1.0],
            [2.0, 0.0, 1.0],
            [1.0, 0.0, 1.0],
            [3.0, 0.0, 1.0],
        ]
        assert fake.calls == [["a", "bb"], ["ccc"]]  # duplicates and cached inputs never sent
        assert (result.reused, result.embedded, result.input_tokens) == (1, 3, 3)

    async def test_batches_respect_token_budget(self) -> None:
        fake = FakeProvider()
        service = EmbeddingService(fake, FakeCache({}), batch_size=100, max_batch_tokens=30)
        await service.embed(uuid.uuid4(), ["word " * 20, "term " * 20, "short"])
        assert [len(call) for call in fake.calls] == [1, 2]

    def test_hash_depends_on_model_and_dims(self) -> None:
        assert content_hash("m", 3, "t") != content_hash("m2", 3, "t")
        assert content_hash("m", 3, "t") != content_hash("m", 4, "t")
