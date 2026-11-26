"""Google Drive (REST v3) behind the DriveClient port (ADR 0035).

OAuth 2.0 authorization code flow with PKCE and offline access (refresh token),
scope ``drive.readonly``. Shared drives are included. Google Docs are exported
as .docx (Drive limits exports to 10 MB).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from documind.application.connectors.ports import (
    DOCX,
    FOLDER,
    GOOGLE_DOC,
    Change,
    ChangePage,
    ConnectorAuthError,
    ConnectorError,
    DriveFile,
    Tokens,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_FILE_FIELDS = "id,name,mimeType,parents,md5Checksum,modifiedTime,size,trashed"
_ALL_DRIVES = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}


def _file(raw: dict[str, Any]) -> DriveFile:
    size = raw.get("size")
    return DriveFile(
        id=str(raw["id"]),
        name=str(raw.get("name") or "Untitled"),
        mime_type=str(raw.get("mimeType") or ""),
        parents=tuple(raw.get("parents") or ()),
        md5=raw.get("md5Checksum"),
        modified=raw.get("modifiedTime"),
        size=int(size) if size is not None else None,
        trashed=bool(raw.get("trashed")),
    )


def _q(value: str) -> str:
    """Quote a value for a Drive query string."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class GoogleDriveClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 60.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._id = client_id
        self._secret = client_secret
        self._http = http or httpx.AsyncClient(timeout=timeout_seconds)

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",  # always return a refresh token
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    async def _token(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            r = await self._http.post(
                TOKEN_URL, data={**data, "client_id": self._id, "client_secret": self._secret}
            )
        except httpx.HTTPError as exc:
            raise ConnectorError("token endpoint unreachable") from exc
        if r.status_code == 400 and r.json().get("error") in ("invalid_grant", "invalid_request"):
            raise ConnectorAuthError(str(r.json().get("error")))
        if r.status_code == 401:
            raise ConnectorAuthError("invalid_client")
        if r.status_code >= 400:
            raise ConnectorError(f"token endpoint returned {r.status_code}")
        body: dict[str, Any] = r.json()
        return body

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> Tokens:
        body = await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            }
        )
        refresh = body.get("refresh_token")
        if not refresh:
            raise ConnectorAuthError("no refresh token returned")
        access = str(body["access_token"])
        about = await self._get(access, "/about", {"fields": "user(emailAddress)"})
        return Tokens(str(refresh), access, (about.get("user") or {}).get("emailAddress"))

    async def access_token(self, refresh_token: str) -> str:
        body = await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        return str(body["access_token"])

    async def revoke(self, token: str) -> None:
        try:
            await self._http.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError:
            logger.warning("could not revoke a Google token; it expires on its own")

    async def _get(self, token: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            r = await self._http.get(
                f"{API}{path}", params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise ConnectorError("Drive unreachable") from exc
        if r.status_code == 401:
            raise ConnectorAuthError("access token rejected")
        if r.status_code >= 400:
            raise ConnectorError(f"Drive returned {r.status_code}")
        body: dict[str, Any] = r.json()
        return body

    async def _list(self, token: str, query: str) -> list[DriveFile]:
        files: list[DriveFile] = []
        page: str | None = None
        while True:
            params = {
                "q": query,
                "fields": f"nextPageToken,files({_FILE_FIELDS})",
                "pageSize": "1000",
                **_ALL_DRIVES,
            }
            if page:
                params["pageToken"] = page
            body = await self._get(token, "/files", params)
            files += [_file(f) for f in body.get("files", [])]
            page = body.get("nextPageToken")
            if not page:
                return files

    async def folders(self, token: str, parent: str | None) -> list[DriveFile]:
        if parent is None:
            drives = await self._get(token, "/drives", {"pageSize": "100"})
            return [DriveFile("root", "My Drive", FOLDER)] + [
                DriveFile(str(d["id"]), str(d.get("name") or "Shared drive"), FOLDER)
                for d in drives.get("drives", [])
            ]
        found = await self._list(
            token, f"{_q(parent)} in parents and mimeType = {_q(FOLDER)} and trashed = false"
        )
        return sorted(found, key=lambda f: f.name.lower())

    async def children(self, token: str, folder_id: str) -> list[DriveFile]:
        return await self._list(token, f"{_q(folder_id)} in parents and trashed = false")

    async def start_page_token(self, token: str) -> str:
        body = await self._get(token, "/changes/startPageToken", {"supportsAllDrives": "true"})
        return str(body["startPageToken"])

    async def changes(self, token: str, page_token: str) -> ChangePage:
        body = await self._get(
            token,
            "/changes",
            {
                "pageToken": page_token,
                "pageSize": "1000",
                "fields": (
                    f"nextPageToken,newStartPageToken,changes(fileId,removed,file({_FILE_FIELDS}))"
                ),
                **_ALL_DRIVES,
            },
        )
        return ChangePage(
            changes=[
                Change(
                    str(c.get("fileId")),
                    bool(c.get("removed")),
                    _file(c["file"]) if c.get("file") else None,
                )
                for c in body.get("changes", [])
            ],
            next_page_token=body.get("nextPageToken"),
            new_start_page_token=body.get("newStartPageToken"),
        )

    async def download(self, token: str, file: DriveFile) -> bytes:
        if file.mime_type == GOOGLE_DOC:
            url, params = f"{API}/files/{file.id}/export", {"mimeType": DOCX}
        elif file.mime_type == FOLDER:
            raise ConnectorError("folders have no content")
        else:
            url, params = f"{API}/files/{file.id}", {"alt": "media", "supportsAllDrives": "true"}
        try:
            r = await self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise ConnectorError("Drive unreachable") from exc
        if r.status_code == 401:
            raise ConnectorAuthError("access token rejected")
        if r.status_code >= 400:
            raise ConnectorError(f"download returned {r.status_code}")
        return r.content
