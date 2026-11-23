"""Document management against real PostgreSQL, Redis and MinIO."""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from documind.application.ports import ScannerUnavailableError, ScanResult
from documind.infrastructure.storage.client import create_s3_client
from documind.infrastructure.storage.s3_provider import S3ObjectStorage
from tests.integration.conftest import Api

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
WORD_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def docx(body: str = "hello", *, macro: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            f'<Types><Override PartName="/word/document.xml" ContentType="{WORD_MAIN}"/></Types>',
        )
        z.writestr("word/document.xml", f"<w:document>{body}</w:document>")
        if macro:
            z.writestr("word/vbaProject.bin", b"macro")
    return buf.getvalue()


def pdf(marker: str) -> bytes:
    return PDF + f"% {marker}\n".encode()


@pytest.fixture
def s3(api: Api) -> Iterator[Any]:
    client = create_s3_client(api.settings)
    yield client
    client.close()


def ws_of(api: Api, headers: dict[str, str], kind: str = "personal") -> str:
    workspaces = api.client.get("/api/v1/me/workspaces", headers=headers).json()
    return str(next(w["id"] for w in workspaces if w["kind"] == kind))


def upload(
    api: Api,
    ws: str,
    headers: dict[str, str],
    content: bytes,
    filename: str,
    **form: str,
) -> Any:
    response = api.client.post(
        f"/api/v1/workspaces/{ws}/documents",
        files={"file": (filename, content, "application/octet-stream")},
        data=form,
        headers=headers,
    )
    return response


def object_exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError:
        return False
    return True


def storage_key(api: Api, document_id: str) -> list[str]:
    async def run() -> list[str]:
        engine = create_async_engine(api.settings.database_url.get_secret_value())
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT storage_key FROM document_versions"
                        " WHERE document_id = :d ORDER BY version_number"
                    ),
                    {"d": document_id},
                )
                return [r[0] for r in rows]
        finally:
            await engine.dispose()

    return asyncio.run(run())


# --- upload ---


def test_upload_stores_private_object_and_metadata(api: Api, s3: Any) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    response = upload(api, ws, ada, pdf("a"), "Q3 Board Report.pdf")
    assert response.status_code == 201, response.text
    doc = response.json()
    assert doc["title"] == "Q3 Board Report"
    assert doc["original_filename"] == "Q3 Board Report.pdf"
    assert doc["format"] == "pdf"
    assert doc["media_type"] == "application/pdf"
    assert doc["size_bytes"] == len(pdf("a"))
    assert doc["status"] == "pending"
    assert doc["version_count"] == 1
    assert len(doc["checksum_sha256"]) == 64

    (key,) = storage_key(api, doc["id"])
    assert key == f"workspaces/{ws}/documents/{doc['id']}/versions/{doc['current_version_id']}"
    stored = s3.get_object(Bucket=api.settings.s3_bucket, Key=key)
    assert stored["Body"].read() == pdf("a")
    assert stored["ContentType"] == "application/pdf"
    assert stored["Metadata"]["sha256"] == doc["checksum_sha256"]


@pytest.mark.parametrize(
    ("content", "filename", "fmt"),
    [
        (docx(), "contract.docx", "docx"),
        ("Notes ✓\n".encode(), "notes.txt", "txt"),
        (b"# Guide\n", "guide.md", "markdown"),
    ],
)
def test_upload_supported_formats(api: Api, content: bytes, filename: str, fmt: str) -> None:
    ada = api.signup("ada@example.com")
    response = upload(api, ws_of(api, ada), ada, content, filename)
    assert response.status_code == 201, response.text
    assert response.json()["format"] == fmt


def test_upload_with_custom_title_and_collection(api: Api) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    collection = api.client.post(
        f"/api/v1/workspaces/{ws}/collections", json={"name": "Contracts"}, headers=ada
    ).json()
    doc = upload(
        api,
        ws,
        ada,
        pdf("b"),
        "x.pdf",
        title="  Master   Agreement ",
        collection_id=collection["id"],
    ).json()
    assert doc["title"] == "Master Agreement"
    assert doc["collection_ids"] == [collection["id"]]


# --- validation and security ---


@pytest.mark.parametrize(
    ("content", "filename", "status", "code"),
    [
        (b"MZ\x90\x00 executable", "invoice.pdf", 415, "unsupported_file"),
        (PDF, "script.exe", 415, "unsupported_file"),
        (PDF, "page.html", 415, "unsupported_file"),
        (docx(macro=True), "macro.docx", 415, "unsupported_file"),
        ("text".encode("utf-16"), "notes.txt", 415, "unsupported_file"),
        (b"", "empty.pdf", 422, "validation_failed"),
    ],
)
def test_upload_validation(
    api: Api, s3: Any, content: bytes, filename: str, status: int, code: str
) -> None:
    ada = api.signup("ada@example.com")
    response = upload(api, ws_of(api, ada), ada, content, filename)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    listed = s3.list_objects_v2(
        Bucket=api.settings.s3_bucket, Prefix=f"workspaces/{ws_of(api, ada)}/"
    )
    assert listed.get("KeyCount", 0) == 0  # nothing reached storage


def test_filenames_cannot_traverse_paths(api: Api) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    doc = upload(api, ws, ada, pdf("c"), "../../../etc/evil.pdf").json()
    assert doc["original_filename"] == "evil.pdf"
    (key,) = storage_key(api, doc["id"])
    assert ".." not in key
    assert key.startswith(f"workspaces/{ws}/documents/")


def test_duplicate_content_rejected(api: Api) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    assert upload(api, ws, ada, pdf("d"), "one.pdf").status_code == 201
    dup = upload(api, ws, ada, pdf("d"), "renamed copy.pdf")
    assert dup.status_code == 409
    assert dup.json()["code"] == "duplicate_document"
    # The same file is fine in another tenant.
    bob = api.signup("bob@example.com")
    assert upload(api, ws_of(api, bob), bob, pdf("d"), "one.pdf").status_code == 201


def test_size_limit_enforced_by_service(make_api: Any) -> None:
    api: Api = make_api(max_upload_bytes=2048)
    ada = api.signup("ada@example.com")
    response = upload(api, ws_of(api, ada), ada, b"a" * 4096, "big.txt")
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_size_limit_enforced_before_body_is_read(make_api: Any) -> None:
    api: Api = make_api(max_upload_bytes=2048)  # middleware cap = 2 KiB + 1 MiB framing
    ada = api.signup("ada@example.com")
    response = upload(api, ws_of(api, ada), ada, b"a" * (1024 * 1024 + 4096), "huge.txt")
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert response.headers["x-request-id"]


class InfectedScanner:
    async def scan(self, file: Any) -> ScanResult:
        return ScanResult(clean=False, scanner="test", signature="Eicar-Test-Signature")


class DownScanner:
    async def scan(self, file: Any) -> ScanResult:
        raise ScannerUnavailableError("down")


def test_malware_is_blocked(make_api: Any, s3: Any) -> None:
    api: Api = make_api(container_overrides={"malware_scanner": InfectedScanner()})
    ada = api.signup("ada@example.com")
    response = upload(api, ws_of(api, ada), ada, pdf("e"), "invoice.pdf")
    assert response.status_code == 415
    assert response.json()["code"] == "malware_detected"
    assert (
        s3.list_objects_v2(
            Bucket=api.settings.s3_bucket, Prefix=f"workspaces/{ws_of(api, ada)}/"
        ).get("KeyCount", 0)
        == 0
    )


def test_scanner_outage_fails_closed(make_api: Any) -> None:
    api: Api = make_api(container_overrides={"malware_scanner": DownScanner()})
    ada = api.signup("ada@example.com")
    response = upload(api, ws_of(api, ada), ada, pdf("f"), "invoice.pdf")
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"


# --- signed downloads ---


def test_signed_download_url(api: Api) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    doc = upload(api, ws, ada, pdf("g"), "Résumé; final.pdf").json()
    response = api.client.get(
        f"/api/v1/workspaces/{ws}/documents/{doc['id']}/download-url", headers=ada
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    signed = response.json()
    assert signed["expires_in"] == api.settings.signed_url_ttl_seconds
    assert "X-Amz-Signature" in signed["url"]

    fetched = httpx.get(signed["url"])
    assert fetched.status_code == 200
    assert fetched.content == pdf("g")
    disposition = fetched.headers["content-disposition"]
    # The ";" can't break out of the header: it's replaced in the ASCII
    # fallback and percent-encoded in the UTF-8 form.
    assert disposition.startswith('attachment; filename="R_sum__ final.pdf";')
    assert disposition.endswith("filename*=UTF-8''R%C3%A9sum%C3%A9%3B%20final.pdf")
    assert fetched.headers["content-type"] == "application/pdf"


def test_unsigned_tampered_and_expired_urls_are_rejected(api: Api) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    doc = upload(api, ws, ada, pdf("h"), "secret.pdf").json()
    (key,) = storage_key(api, doc["id"])
    url = api.client.get(
        f"/api/v1/workspaces/{ws}/documents/{doc['id']}/download-url", headers=ada
    ).json()["url"]
    parts = urlsplit(url)

    assert (
        httpx.get(f"{parts.scheme}://{parts.netloc}{parts.path}").status_code == 403
    )  # bucket is private
    assert httpx.get(url.replace("X-Amz-Signature=", "X-Amz-Signature=0")).status_code == 403

    storage = S3ObjectStorage(create_s3_client(api.settings), api.settings.s3_bucket)
    short = storage.generate_signed_url(
        key, expires_in=1, download_filename="x.pdf", content_type="application/pdf"
    )
    time.sleep(2.5)
    assert httpx.get(short).status_code == 403


# --- tenant isolation ---


def test_documents_are_isolated_between_workspaces(api: Api) -> None:
    ada = api.signup("ada@example.com")
    ada_ws = ws_of(api, ada)
    doc = upload(api, ada_ws, ada, pdf("i"), "confidential.pdf").json()
    eve = api.signup("eve@example.com")
    eve_ws = ws_of(api, eve)
    upload(api, eve_ws, eve, pdf("j"), "mine.pdf")

    # Eve can't reach Ada's workspace at all...
    for method, path in [
        ("GET", "/documents"),
        ("GET", f"/documents/{doc['id']}"),
        ("PATCH", f"/documents/{doc['id']}"),
        ("DELETE", f"/documents/{doc['id']}"),
        ("GET", f"/documents/{doc['id']}/download-url"),
        ("GET", f"/documents/{doc['id']}/versions"),
        ("GET", "/collections"),
    ]:
        r = api.client.request(
            method, f"/api/v1/workspaces/{ada_ws}{path}", json={"title": "x"}, headers=eve
        )
        assert r.status_code == 404, (method, path)
    # ...nor use Ada's document ID through her own workspace (IDOR), even with
    # every privilege there, including a fresh 2FA step-up.
    eve_elevated = api.elevated(eve)
    for method, path in [
        ("GET", f"/documents/{doc['id']}"),
        ("PATCH", f"/documents/{doc['id']}"),
        ("DELETE", f"/documents/{doc['id']}"),
        ("POST", f"/documents/{doc['id']}/archive"),
        ("POST", f"/documents/{doc['id']}/restore"),
        ("GET", f"/documents/{doc['id']}/download-url"),
    ]:
        r = api.client.request(
            method, f"/api/v1/workspaces/{eve_ws}{path}", json={"title": "x"}, headers=eve_elevated
        )
        assert r.status_code == 404, (method, path)
    titles = [
        d["title"]
        for d in api.client.get(f"/api/v1/workspaces/{eve_ws}/documents", headers=eve).json()[
            "items"
        ]
    ]
    assert titles == ["mine"]
    collection = api.client.post(
        f"/api/v1/workspaces/{eve_ws}/collections", json={"name": "Loot"}, headers=eve
    ).json()
    stolen = api.client.put(
        f"/api/v1/workspaces/{eve_ws}/collections/{collection['id']}/documents/{doc['id']}",
        headers=eve,
    )
    assert stolen.status_code == 404
    # Ada's document is untouched.
    assert (
        api.client.get(f"/api/v1/workspaces/{ada_ws}/documents/{doc['id']}", headers=ada).json()[
            "title"
        ]
        == "confidential"
    )


def test_database_rejects_cross_workspace_links(api: Api) -> None:
    """Defence in depth: composite FKs stop cross-tenant rows even if app code were wrong."""
    ada = api.signup("ada@example.com")
    ada_ws = ws_of(api, ada)
    doc = upload(api, ada_ws, ada, pdf("k"), "a.pdf").json()
    eve = api.signup("eve@example.com")
    eve_ws = ws_of(api, eve)
    collection = api.client.post(
        f"/api/v1/workspaces/{eve_ws}/collections", json={"name": "C"}, headers=eve
    ).json()

    async def run() -> None:
        engine = create_async_engine(api.settings.database_url.get_secret_value())
        try:
            for workspace in (eve_ws, ada_ws):
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO document_collection_items"
                            " (collection_id, document_id, workspace_id) VALUES (:c, :d, :w)"
                        ),
                        {"c": collection["id"], "d": doc["id"], "w": workspace},
                    )
        finally:
            await engine.dispose()

    with pytest.raises(IntegrityError):
        asyncio.run(run())


# --- permissions ---


def test_role_permissions_for_documents(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)["id"]
    viewer = api.add_member(org, ada, "vic@example.com", "viewer")
    member = api.add_member(org, ada, "mia@example.com", "member")
    doc = upload(api, org, ada, pdf("l"), "policy.pdf").json()
    base = f"/api/v1/workspaces/{org}"

    assert api.client.get(f"{base}/documents", headers=viewer).status_code == 200
    assert (
        api.client.get(f"{base}/documents/{doc['id']}/download-url", headers=viewer).status_code
        == 200
    )
    assert upload(api, org, viewer, pdf("m"), "v.pdf").status_code == 403
    assert (
        api.client.patch(
            f"{base}/documents/{doc['id']}", json={"title": "x"}, headers=viewer
        ).status_code
        == 403
    )
    assert api.client.delete(f"{base}/documents/{doc['id']}", headers=viewer).status_code == 403
    assert (
        api.client.post(f"{base}/collections", json={"name": "X"}, headers=viewer).status_code
        == 403
    )

    assert upload(api, org, member, pdf("n"), "m.pdf").status_code == 201
    assert (
        api.client.patch(
            f"{base}/documents/{doc['id']}", json={"title": "Policy v2"}, headers=member
        ).status_code
        == 200
    )


# --- metadata search, rename, delete ---


def test_search_filter_sort_and_paginate(api: Api) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    upload(api, ws, ada, pdf("1"), "Alpha contract.pdf")
    upload(api, ws, ada, docx("2"), "Beta contract.docx")
    upload(api, ws, ada, b"# 3", "Gamma notes.md")
    upload(api, ws, ada, b"100% done", "Progress 100%.txt")
    base = f"/api/v1/workspaces/{ws}/documents"

    def titles(**params: Any) -> list[str]:
        return [
            d["title"] for d in api.client.get(base, params=params, headers=ada).json()["items"]
        ]

    assert titles(q="contract", sort="title", order="asc") == ["Alpha contract", "Beta contract"]
    assert titles(q="CONTRACT.DOCX") == ["Beta contract"]  # filename match, case-insensitive
    assert titles(format="markdown") == ["Gamma notes"]
    assert titles(q="%") == ["Progress 100%"]  # wildcards are literal
    assert titles(q="_") == []
    assert titles(status="pending", sort="title", order="asc", limit=2, offset=1) == [
        "Beta contract",
        "Gamma notes",
    ]
    page = api.client.get(base, params={"limit": 2}, headers=ada).json()
    assert page["total"] == 4
    assert len(page["items"]) == 2
    assert api.client.get(base, params={"limit": 101}, headers=ada).status_code == 422
    assert api.client.get(base, params={"sort": "password_hash"}, headers=ada).status_code == 422


def test_rename_and_delete(api: Api, s3: Any) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    doc = upload(api, ws, ada, pdf("o"), "old.pdf").json()
    base = f"/api/v1/workspaces/{ws}/documents/{doc['id']}"
    renamed = api.client.patch(base, json={"title": "  New   name "}, headers=ada)
    assert renamed.json()["title"] == "New name"
    assert api.client.patch(base, json={"title": "   "}, headers=ada).status_code == 422

    (key,) = storage_key(api, doc["id"])
    assert api.client.delete(base, headers=api.elevated(ada)).status_code == 204
    assert api.client.get(base, headers=ada).status_code == 404
    assert not object_exists(s3, api.settings.s3_bucket, key)
    # The content can be uploaded again once deleted.
    assert upload(api, ws, ada, pdf("o"), "old.pdf").status_code == 201


# --- versions ---


def test_versions(api: Api, s3: Any) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    doc = upload(api, ws, ada, pdf("v1"), "policy.pdf").json()
    base = f"/api/v1/workspaces/{ws}/documents/{doc['id']}"

    same = api.client.post(
        f"{base}/versions", files={"file": ("policy.pdf", pdf("v1"))}, headers=ada
    )
    assert same.status_code == 409
    v2 = api.client.post(
        f"{base}/versions", files={"file": ("policy-2026.docx", docx("v2"))}, headers=ada
    )
    assert v2.status_code == 201, v2.text
    updated = v2.json()
    assert updated["version_count"] == 2
    assert updated["format"] == "docx"
    assert updated["original_filename"] == "policy-2026.docx"
    assert updated["status"] == "pending"
    assert updated["title"] == "policy"  # title is kept

    versions = api.client.get(f"{base}/versions", headers=ada).json()
    assert [(v["version_number"], v["is_current"]) for v in versions] == [(2, True), (1, False)]
    old_url = api.client.get(
        f"{base}/download-url", params={"version_id": versions[1]["id"]}, headers=ada
    ).json()["url"]
    assert httpx.get(old_url).content == pdf("v1")
    bogus = api.client.get(f"{base}/download-url", params={"version_id": doc["id"]}, headers=ada)
    assert bogus.status_code == 404

    keys = storage_key(api, doc["id"])
    assert len(keys) == 2
    assert api.client.delete(base, headers=api.elevated(ada)).status_code == 204
    assert not any(object_exists(s3, api.settings.s3_bucket, k) for k in keys)


# --- collections ---


def test_collections(api: Api) -> None:
    ada = api.signup("ada@example.com")
    ws = ws_of(api, ada)
    base = f"/api/v1/workspaces/{ws}"
    a = upload(api, ws, ada, pdf("p"), "a.pdf").json()
    b = upload(api, ws, ada, pdf("q"), "b.pdf").json()

    contracts = api.client.post(
        f"{base}/collections", json={"name": "Contracts", "description": "Signed"}, headers=ada
    )
    assert contracts.status_code == 201
    cid = contracts.json()["id"]
    dup = api.client.post(f"{base}/collections", json={"name": "  contracts "}, headers=ada)
    assert dup.status_code == 409

    for doc_id in (a["id"], b["id"], a["id"]):  # adding twice is idempotent
        assert (
            api.client.put(f"{base}/collections/{cid}/documents/{doc_id}", headers=ada).status_code
            == 204
        )
    assert api.client.get(f"{base}/collections/{cid}", headers=ada).json()["document_count"] == 2
    in_collection = api.client.get(
        f"{base}/documents", params={"collection_id": cid}, headers=ada
    ).json()
    assert in_collection["total"] == 2
    assert api.client.get(f"{base}/documents/{a['id']}", headers=ada).json()["collection_ids"] == [
        cid
    ]

    assert (
        api.client.delete(f"{base}/collections/{cid}/documents/{a['id']}", headers=ada).status_code
        == 204
    )
    assert (
        api.client.delete(f"{base}/collections/{cid}/documents/{a['id']}", headers=ada).status_code
        == 404
    )
    renamed = api.client.patch(
        f"{base}/collections/{cid}", json={"name": "Agreements", "description": None}, headers=ada
    )
    assert renamed.json()["name"] == "Agreements"
    assert renamed.json()["description"] is None

    assert api.client.delete(f"{base}/collections/{cid}", headers=ada).status_code == 204
    assert api.client.get(f"{base}/documents", headers=ada).json()["total"] == 2  # documents kept
    assert (
        api.client.get(f"{base}/documents", params={"collection_id": cid}, headers=ada).status_code
        == 404
    )


def test_document_audit_events(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)["id"]
    doc = upload(api, org, ada, pdf("r"), "a.pdf").json()
    api.client.get(f"/api/v1/workspaces/{org}/documents/{doc['id']}/download-url", headers=ada)
    api.client.delete(f"/api/v1/workspaces/{org}/documents/{doc['id']}", headers=api.elevated(ada))
    actions = [
        e["action"]
        for e in api.client.get(f"/api/v1/workspaces/{org}/audit-logs", headers=ada).json()
    ]
    assert actions[:3] == ["document.deleted", "document.download_url_issued", "document.uploaded"]
