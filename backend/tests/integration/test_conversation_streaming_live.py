"""SSE cancellation and client disconnects against a real uvicorn server.

TestClient buffers whole responses, so these tests run the app in a uvicorn
thread and talk to it over a socket.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import uvicorn

from documind.application.ports import (
    ChatMessage,
    LLMDelta,
    LLMResult,
    LLMStreamEnd,
    LLMStreamEvent,
    LLMUsage,
)
from documind.container import Container, build_container
from documind.core.config import Settings
from documind.main import create_app
from tests.integration.conftest import Api, Mailbox, RecordingDispatcher
from tests.integration.test_conversations_api import base, create
from tests.integration.test_processing_pipeline import sql, ws
from tests.integration.test_rag_api import POLICY, ingest

pytestmark = pytest.mark.integration


class SlowLLM:
    """Streams 200 words, 50 ms apart (10 s in total)."""

    model = "slow"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished_cleanly = threading.Event()
        self.closed = threading.Event()

    async def generate(self, messages: list[ChatMessage], **_: Any) -> LLMResult:
        return LLMResult("", LLMUsage(), self.model)  # condenser: falls back

    def stream(self, messages: list[ChatMessage], **_: Any) -> AsyncIterator[LLMStreamEvent]:
        async def gen() -> AsyncIterator[LLMStreamEvent]:
            self.started.set()
            try:
                for i in range(200):
                    await asyncio.sleep(0.05)
                    yield LLMDelta(f"word{i} ")
                self.finished_cleanly.set()
                yield LLMStreamEnd(LLMUsage(1, 200), self.model)
            finally:
                self.closed.set()

        return gen()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@contextmanager
def live_server(settings: Settings, mailbox: Mailbox, llm: SlowLLM) -> Iterator[str]:
    def factory(s: Settings) -> Container:
        c = build_container(s)
        c.email_sender = mailbox
        c.job_dispatcher = RecordingDispatcher()
        c.llm_provider = llm
        return c

    port = _free_port()
    config = uvicorn.Config(
        create_app(settings, container_factory=factory),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def wait_for(predicate: Any, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def setup(api: Api) -> tuple[Api, dict[str, str], str, str]:
    h = api.signup("ada@example.com")
    w = ws(api, h)
    ingest(api, w, h, POLICY, "policy.md")
    conv = create(api, w, h)
    return api, h, w, conv["id"]


def _events(response: httpx.Response) -> Iterator[tuple[str, Any]]:
    name = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: ") and name:
            yield name, json.loads(line[6:])
            name = None


def test_cancel_mid_stream(setup: tuple[Api, dict[str, str], str, str], mailbox: Mailbox) -> None:
    api, h, w, cid = setup
    llm = SlowLLM()
    with live_server(api.settings, mailbox, llm) as url, httpx.Client(base_url=url) as client:
        headers = {**h, "Accept": "text/event-stream"}
        seen: list[str] = []
        with client.stream(
            "POST", f"{base(w)}/{cid}/messages", json={"content": "Who reviews?"}, headers=headers
        ) as r:
            assert r.status_code == 200
            mid = None
            for name, data in _events(r):
                seen.append(name)
                if name == "message.created":
                    mid = data["assistant_message"]["id"]
                if name == "answer.delta" and seen.count("answer.delta") == 3:
                    c = client.post(f"{base(w)}/{cid}/messages/{mid}/cancel", headers=h)
                    assert c.status_code == 202
                if name == "answer.cancelled":
                    final = data["message"]
                    break
        assert final["status"] == "cancelled"
        assert final["error_code"] == "cancelled_by_user"
        assert final["content"].startswith("word0 word1 word2 ")
        assert "answer.completed" not in seen
        assert llm.closed.wait(5)
        assert not llm.finished_cleanly.is_set()
        stored = client.get(f"{base(w)}/{cid}/messages/{mid}", headers=h).json()
        assert stored["status"] == "cancelled"
        # The conversation accepts the next question right away.
        r2 = client.post(f"{base(w)}/{cid}/messages", json={"content": "x"}, headers=h, timeout=5)
        assert r2.status_code != 409


def test_client_disconnect_stops_generation(
    setup: tuple[Api, dict[str, str], str, str], mailbox: Mailbox
) -> None:
    api, h, w, cid = setup
    llm = SlowLLM()
    with live_server(api.settings, mailbox, llm) as url, httpx.Client(base_url=url) as client:
        headers = {**h, "Accept": "text/event-stream"}
        with client.stream(
            "POST", f"{base(w)}/{cid}/messages", json={"content": "Who reviews?"}, headers=headers
        ) as r:
            deltas = 0
            for name, _ in _events(r):
                if name == "answer.delta":
                    deltas += 1
                    if deltas == 2:
                        break  # leaving the block closes the connection
        assert llm.closed.wait(5), "generation kept running after the client left"
        assert not llm.finished_cleanly.is_set()
        row = wait_for(
            lambda: next(
                iter(
                    sql(
                        api,
                        "SELECT status, error_code, content FROM messages "
                        "WHERE role = 'assistant' AND status <> 'streaming'",
                    )
                ),
                None,
            )
        )
        assert (row.status, row.error_code) == ("cancelled", "client_disconnected")
        assert row.content.startswith("word0 word1 ")
