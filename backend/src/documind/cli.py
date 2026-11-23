"""Operational commands: ``documind-manage <command>``.

Commands:
  bootstrap-storage   Create the private application bucket if it is missing.
  check               Run all dependency health checks; exit 1 if any fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from documind.container import build_container
from documind.core.config import get_settings
from documind.core.logging import configure_logging
from documind.infrastructure.storage.bootstrap import ensure_private_bucket
from documind.infrastructure.storage.client import create_s3_client


def _bootstrap_storage() -> int:
    settings = get_settings()
    client = create_s3_client(settings)
    try:
        result = ensure_private_bucket(client, settings.s3_bucket, settings.s3_region)
    finally:
        client.close()
    state = "created" if result.created else "already exists"
    print(f"Bucket {result.bucket!r} {state} and is private.")  # noqa: T201
    return 0


async def _check() -> int:
    settings = get_settings()
    container = build_container(settings)
    try:
        report = await container.health_service.check_readiness()
    finally:
        await container.aclose()
    output = {
        "status": report.status.value,
        "components": {
            c.name: {
                "status": c.status.value,
                "latency_ms": c.latency_ms,
                "error": c.error,
                **c.details,
            }
            for c in report.components
        },
    }
    print(json.dumps(output, indent=2))  # noqa: T201
    return 0 if report.is_healthy else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="documind-manage",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap-storage", help="create the private application bucket")
    sub.add_parser("check", help="check dependency health")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if args.command == "bootstrap-storage":
        return _bootstrap_storage()
    return asyncio.run(_check())


if __name__ == "__main__":
    sys.exit(main())
