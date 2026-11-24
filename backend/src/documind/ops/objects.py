"""Object storage export and import for backups (Phase 9/10 runbooks).

Objects are written as files mirroring their keys, with a manifest of sizes and
SHA-256 digests, so a restore can verify every byte before anything is uploaded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST = "manifest.json"


def _safe(root: Path, key: str) -> Path:
    target = (root / key).resolve()
    if not str(target).startswith(str(root.resolve()) + "/"):
        raise ValueError(f"unsafe object key: {key!r}")
    return target


def export_objects(
    client: Any, bucket: str, destination: Path, *, prefix: str = ""
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            path = _safe(destination, key)
            path.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(path))
            head = client.head_object(Bucket=bucket, Key=key)
            entries.append(
                {
                    "key": key,
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "content_type": head.get("ContentType"),
                    "metadata": head.get("Metadata", {}),
                }
            )
    manifest = {"bucket": bucket, "prefix": prefix, "objects": entries}
    (destination / MANIFEST).write_text(json.dumps(manifest, indent=2))
    return manifest


def import_objects(client: Any, bucket: str, source: Path) -> int:
    """Verify every file against the manifest, then upload. Returns the object count."""
    manifest = json.loads((source / MANIFEST).read_text())
    entries = manifest["objects"]
    for entry in entries:
        path = _safe(source, entry["key"])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"checksum mismatch for {entry['key']}")
    for entry in entries:
        extra: dict[str, Any] = {"Metadata": entry.get("metadata") or {}}
        if entry.get("content_type"):
            extra["ContentType"] = entry["content_type"]
        client.upload_file(str(_safe(source, entry["key"])), bucket, entry["key"], ExtraArgs=extra)
    return len(entries)
