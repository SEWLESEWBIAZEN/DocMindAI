# Load tests (Phase 9)

Target: the whole stack on **4 vCPU / 8 GB** (ADR deployment target).

```bash
docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml up -d postgres redis minio mailpit backend worker beat
backend/.venv/bin/python loadtest/run.py --users 25 --duration 60 --json loadtest/results.json
docker compose up -d backend worker beat        # back to the development settings
```

The profile caps memory at 8 GB in total and splits the 4 vCPUs: API 2, PostgreSQL 1.5, worker 1, and the
rest small. The API runs 3 uvicorn workers. Auth and AI rate limits are raised so the generator isn't
throttled; everything else is as in production.

Each virtual user does the following, with no think time (every user is always busy):

1. Signs up through the real email verification.
2. Uploads two Markdown agreements and waits for them to be indexed.
3. Loops over a weighted mix of 40% document list, 25% search, 20% grounded answer (`/ask`),
   10% plan and quota view, and 5% conversation list.

## Results (2026-11-24)

| Users | Requests/s | Errors | list p50/p95 | search p50/p95 | answer p50/p95/p99 | upload → ready p95 |
|-------|-----------|--------|--------------|----------------|--------------------|--------------------|
| 25 | 139 | 0 | 38 / 395 ms | 58 / 545 ms | 81 / 779 / 908 ms | 1.4 s (50 docs) |
| 50 | 134 | 0 | 279 / 455 ms | 395 / 659 ms | 584 / 879 / 1030 ms | 1.6 s (100 docs) |

Raw output: `results-2026-11-24-*.json`.

Reading the numbers:

- The API is CPU-bound at its 2-vCPU limit. Throughput plateaus at about 135 requests/s, and doubling
  the busy users mostly raises median latency. p95 stays under about 0.9 s and p99 around 1 s, with no
  errors and no failed processing.
- Peak memory was about 1.2 GB for the API container and under 0.4 GB for the worker, well within the
  limits.
- Busy users with no think time are a heavy load. A typical user makes a request every 10–30 s, so 135
  requests/s corresponds to roughly 1,000–4,000 active users.
- **Caveats:**
  - The load generator ran on the same 4 vCPUs, which makes the results slightly pessimistic.
  - The answers use the offline `local` providers. With OpenAI, answer latency is dominated by the
    provider, roughly 1–5 s, while DocuMind's own CPU cost per answer goes down.
  - Documents were small Markdown files. Large scanned PDFs make OCR the processing bottleneck (one
    worker with concurrency 2).

Next steps (Phase 10):

- Put Nginx in front of the API (TLS and static files off the API).
- Size the API workers per vCPU.
- Run OCR-heavy jobs on a separate queue.
- Re-run the test on the production hosts.
