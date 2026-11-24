"""DocuMind load test (Phase 9).

    python loadtest/run.py --users 25 --duration 60 [--base http://localhost:8000]

Each virtual user signs up (verifying through Mailpit), uploads two documents
and waits for them to be indexed, then loops over a weighted mix of real API
calls for ``--duration`` seconds. Prints per-endpoint throughput, latency
percentiles and errors, plus time-to-ready for uploads under load.

Answers use whatever providers the stack is configured with (``local`` in
development), so the figures measure DocuMind itself, not an AI provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

PASSWORD = "load test password 123"
TOPICS = ["renewal", "termination", "security", "privacy", "liability", "payment", "audit", "notice"]


def document(seed: int) -> bytes:
    rnd = random.Random(seed)
    sections = []
    for topic in rnd.sample(TOPICS, 5):
        days = rnd.choice([30, 45, 60, 90])
        sections.append(
            f"## {topic.title()}\n\nThe {topic} clause requires {days} days written notice. "
            f"Exceptions to the {topic} terms need approval from Legal and Finance. "
            + " ".join(f"Detail {i} about {topic} obligations and reporting." for i in range(rnd.randint(20, 60)))
        )
    return (f"# Agreement {seed}\n\n" + "\n\n".join(sections)).encode()


@dataclass
class Stats:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    errors: dict[str, dict[int, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    ready_seconds: list[float] = field(default_factory=list)

    def record(self, name: str, started: float, status: int) -> None:
        self.latencies[name].append(time.perf_counter() - started)
        if status >= 400:
            self.errors[name][status] += 1


async def call(client: httpx.AsyncClient, stats: Stats, name: str, method: str, url: str, **kw: object) -> httpx.Response:
    started = time.perf_counter()
    try:
        response = await client.request(method, url, **kw)  # type: ignore[arg-type]
    except httpx.HTTPError:
        stats.record(name, started, 599)
        raise
    stats.record(name, started, response.status_code)
    return response


async def verify_link(mail: httpx.AsyncClient, email: str) -> str:
    for _ in range(60):
        found = (await mail.get("/api/v1/search", params={"query": f"to:{email}"})).json()
        for message in found.get("messages", []):
            body = (await mail.get(f"/api/v1/message/{message['ID']}")).json()
            match = re.search(r"token=([\w\-]+)", body.get("Text", ""))
            if match:
                return match.group(1)
        await asyncio.sleep(0.5)
    raise RuntimeError(f"no verification email for {email}")


async def setup_user(base: str, mail_url: str, index: int, stats: Stats) -> tuple[httpx.AsyncClient, str]:
    client = httpx.AsyncClient(base_url=f"{base}/api/v1", timeout=120)
    email = f"load-{uuid.uuid4().hex[:10]}@example.com"
    await call(client, stats, "auth.register", "POST", "/auth/register", json={"email": email, "password": PASSWORD, "display_name": f"Load {index}"})
    async with httpx.AsyncClient(base_url=mail_url, timeout=30) as mail:
        token = await verify_link(mail, email)
    await call(client, stats, "auth.verify", "POST", "/auth/verify-email", json={"token": token})
    login = await call(client, stats, "auth.login", "POST", "/auth/login", json={"email": email, "password": PASSWORD})
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    workspace = (await call(client, stats, "me.workspaces", "GET", "/me/workspaces")).json()[0]["id"]

    uploaded: list[tuple[str, float]] = []
    for n in range(2):
        files = {"file": (f"agreement-{index}-{n}.md", document(index * 10 + n), "text/markdown")}
        response = await call(client, stats, "documents.upload", "POST", f"/workspaces/{workspace}/documents", files=files)
        uploaded.append((response.json()["id"], time.perf_counter()))
    for doc_id, started in uploaded:
        while True:
            status = (await call(client, stats, "documents.get", "GET", f"/workspaces/{workspace}/documents/{doc_id}")).json()["status"]
            if status in ("ready", "failed"):
                stats.ready_seconds.append(time.perf_counter() - started)
                break
            await asyncio.sleep(0.5)
    return client, workspace


async def virtual_user(client: httpx.AsyncClient, workspace: str, until: float, stats: Stats) -> None:
    ws = f"/workspaces/{workspace}"
    actions = [
        (40, "documents.list", "GET", f"{ws}/documents", {}),
        (25, "search", "POST", f"{ws}/search", {"json_factory": lambda: {"query": f"{random.choice(TOPICS)} notice"}}),
        (20, "ask", "POST", f"{ws}/ask", {"json_factory": lambda: {"question": f"How much notice does the {random.choice(TOPICS)} clause need?"}}),
        (10, "usage.plan", "GET", f"{ws}/plan", {}),
        (5, "conversations.list", "GET", f"{ws}/conversations", {}),
    ]
    weights = [a[0] for a in actions]
    while time.perf_counter() < until:
        _, name, method, url, extra = random.choices(actions, weights)[0]
        kw = {"json": extra["json_factory"]()} if "json_factory" in extra else {}
        try:
            await call(client, stats, name, method, url, **kw)
        except httpx.HTTPError:
            pass


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--mail", default="http://localhost:8025")
    parser.add_argument("--users", type=int, default=25)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--json", help="write results to this file")
    args = parser.parse_args()

    stats = Stats()
    setup_started = time.perf_counter()
    users = await asyncio.gather(*(setup_user(args.base, args.mail, i, stats) for i in range(args.users)))
    setup_seconds = time.perf_counter() - setup_started
    load = Stats()
    until = time.perf_counter() + args.duration
    started = time.perf_counter()
    await asyncio.gather(*(virtual_user(c, w, until, load) for c, w in users))
    elapsed = time.perf_counter() - started
    for client, _ in users:
        await client.aclose()

    report = {
        "users": args.users,
        "duration_s": round(elapsed, 1),
        "setup_s": round(setup_seconds, 1),
        "documents_indexed": len(stats.ready_seconds),
        "time_to_ready_s": {
            "p50": round(statistics.median(stats.ready_seconds), 2),
            "p95": round(pct(stats.ready_seconds, 95), 2),
            "max": round(max(stats.ready_seconds), 2),
        },
        "total_rps": round(sum(len(v) for v in load.latencies.values()) / elapsed, 1),
        "endpoints": {
            name: {
                "requests": len(v),
                "rps": round(len(v) / elapsed, 1),
                "p50_ms": round(pct(v, 50) * 1000),
                "p95_ms": round(pct(v, 95) * 1000),
                "p99_ms": round(pct(v, 99) * 1000),
                "errors": dict(load.errors.get(name, {})),
            }
            for name, v in sorted(load.latencies.items())
        },
    }
    print(json.dumps(report, indent=2))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
