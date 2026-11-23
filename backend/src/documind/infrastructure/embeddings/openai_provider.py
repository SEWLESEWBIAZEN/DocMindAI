"""OpenAI embeddings (text-embedding-3 family) behind the EmbeddingProvider port."""

from __future__ import annotations

import logging

import httpx2
import openai

from documind.application.ports import EmbeddingBatch
from documind.domain.processing.errors import PermanentProcessingError, TransientProcessingError

logger = logging.getLogger(__name__)


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 5,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        # The SDK retries 408/409/429/5xx and connection errors with exponential
        # backoff, honouring Retry-After; we classify what still fails.
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=texts,
                dimensions=self._dimensions,
                encoding_format="float",
            )
        except openai.RateLimitError as exc:
            raise TransientProcessingError(
                "The embedding service is rate limiting requests.", code="embedding_rate_limited"
            ) from exc
        except (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.InternalServerError,
        ) as exc:
            raise TransientProcessingError(
                "The embedding service is temporarily unavailable.", code="embedding_unavailable"
            ) from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            # An operator can fix credentials, so retry rather than fail the document.
            logger.error("Embedding provider rejected our credentials")
            raise TransientProcessingError(
                "The embedding service is misconfigured.", code="embedding_misconfigured"
            ) from exc
        except openai.BadRequestError as exc:
            raise PermanentProcessingError(
                "The embedding service rejected this content.", code="embedding_rejected"
            ) from exc
        except openai.APIStatusError as exc:
            raise TransientProcessingError(
                f"Unexpected embedding service response ({exc.status_code}).",
                code="embedding_error",
            ) from exc

        items = sorted(response.data, key=lambda item: item.index)
        if len(items) != len(texts):
            raise TransientProcessingError(
                "Embedding response was incomplete.", code="embedding_error"
            )
        vectors = [list(item.embedding) for item in items]
        if any(len(v) != self._dimensions for v in vectors):
            raise PermanentProcessingError(
                "Embedding dimensions don't match the configured index.",
                code="embedding_dimensions",
            )
        usage = getattr(response, "usage", None)
        return EmbeddingBatch(
            vectors=vectors, input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0)
        )
