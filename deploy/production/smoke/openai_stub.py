"""OpenAI-compatible stand-in for the production smoke test (no network, no key).

Serves /v1/embeddings and /v1/chat/completions (JSON and streamed) using
DocuMind's own offline models, so production mode (which refuses the
``local`` providers) can be exercised end to end.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from documind.application.ports import ChatMessage, LLMDelta, LLMStreamEnd
from documind.infrastructure.embeddings.local_provider import LocalHashingEmbeddingProvider
from documind.infrastructure.llm.local_provider import LocalExtractiveLLM

app = FastAPI()
llm = LocalExtractiveLLM()


@app.get("/v1/models/{model}")
async def model(model: str) -> JSONResponse:
    """The start-up model check (documind-manage ai check): every model is "available"."""
    return JSONResponse({"id": model, "object": "model", "created": 0, "owned_by": "smoke-stub"})


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    body = await request.json()
    texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
    batch = await LocalHashingEmbeddingProvider(int(body.get("dimensions", 1536))).embed(texts)
    return JSONResponse(
        {
            "object": "list",
            "model": body["model"],
            "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(batch.vectors)],
            "usage": {"prompt_tokens": batch.input_tokens, "total_tokens": batch.input_tokens},
        }
    )


def _messages(body: dict[str, Any]) -> list[ChatMessage]:
    return [ChatMessage(m["role"], m["content"]) for m in body["messages"]]


@app.post("/v1/chat/completions", response_model=None)
async def chat(request: Request) -> JSONResponse | StreamingResponse:
    body = await request.json()
    base = {"id": "chatcmpl-smoke", "created": int(time.time()), "model": body["model"]}
    if not body.get("stream"):
        result = await llm.generate(_messages(body), max_output_tokens=1000, temperature=None)
        return JSONResponse(
            {
                **base,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": result.text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": result.usage.input_tokens, "completion_tokens": result.usage.output_tokens, "total_tokens": 0},
            }
        )

    async def events() -> AsyncIterator[str]:
        async for event in llm.stream(_messages(body), max_output_tokens=1000, temperature=None):
            if isinstance(event, LLMDelta):
                chunk = {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": event.text}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
            elif isinstance(event, LLMStreamEnd):
                done = {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(done)}\n\n"
                usage = {**base, "object": "chat.completion.chunk", "choices": [], "usage": {"prompt_tokens": event.usage.input_tokens, "completion_tokens": event.usage.output_tokens, "total_tokens": 0}}
                yield f"data: {json.dumps(usage)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
