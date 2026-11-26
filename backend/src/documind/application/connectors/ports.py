"""Port for cloud-drive connectors (ADR 0035). Google Drive first."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: Drive types DocuMind imports, and the file extension to give them.
SUPPORTED: dict[str, str] = {
    "application/pdf": ".pdf",
    DOCX: ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    GOOGLE_DOC: ".docx",  # exported
}


class ConnectorError(Exception):
    """The provider failed; try again later."""


class ConnectorAuthError(ConnectorError):
    """The token was revoked or expired: the connection must be made again."""


@dataclass(frozen=True, slots=True)
class Tokens:
    refresh_token: str
    access_token: str
    account_email: str | None


@dataclass(frozen=True, slots=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    parents: tuple[str, ...] = ()
    md5: str | None = None
    modified: str | None = None
    size: int | None = None
    trashed: bool = False

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER

    @property
    def version(self) -> str:
        return self.md5 or self.modified or ""


@dataclass(frozen=True, slots=True)
class Change:
    file_id: str
    removed: bool
    file: DriveFile | None


@dataclass(frozen=True, slots=True)
class ChangePage:
    changes: list[Change] = field(default_factory=list)
    next_page_token: str | None = None
    new_start_page_token: str | None = None


class DriveClient(Protocol):
    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> Tokens: ...

    async def access_token(self, refresh_token: str) -> str: ...

    async def revoke(self, token: str) -> None: ...

    async def folders(self, token: str, parent: str | None) -> list[DriveFile]: ...

    async def children(self, token: str, folder_id: str) -> list[DriveFile]: ...

    async def start_page_token(self, token: str) -> str: ...

    async def changes(self, token: str, page_token: str) -> ChangePage: ...

    async def download(self, token: str, file: DriveFile) -> bytes: ...
