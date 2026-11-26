"""A Model Context Protocol server for one workspace (ADR 0037).

Stateless Streamable HTTP: each POST carries one JSON-RPC message (or, for
older clients, a batch) and gets a JSON reply. There are no sessions and no
server-initiated messages, so GET has nothing to stream.

Tools go through the same services, permissions and quotas as the REST API,
so a key or person sees through MCP exactly what they would in the app.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from documind.application.documents.collection_service import CollectionService
from documind.application.documents.document_service import DocumentService
from documind.application.processing.queries import ProcessingQueries
from documind.application.rag.orchestrator import RAGOrchestrator
from documind.application.rag.retrieval import RetrievalService
from documind.domain.errors import DomainError
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.repositories.documents import DocumentFilter

# Newest first. We answer with the client's version when we support it.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "documind", "title": "DocuMind", "version": "1.0.0"}
INSTRUCTIONS = (
    "Search and ask questions about this DocuMind workspace's documents. Answers cite "
    "their sources; use read_document to see a page in full."
)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
_SNIPPET = 1200
_PAGE_TEXT = 8000

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}


def _uuid_schema(description: str) -> dict[str, Any]:
    return {"type": "string", "format": "uuid", "description": description}


TOOLS: dict[str, dict[str, Any]] = {
    "search": {
        "title": "Search documents",
        "description": (
            "Find the passages most relevant to a query (meaning and keywords). Returns "
            "each passage with its document, pages and a link."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                "collection_id": _uuid_schema("Only search this collection"),
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "permission": Permission.DOCUMENTS_READ,
    },
    "ask": {
        "title": "Ask a question",
        "description": (
            "Answer a question from the workspace's documents. The answer cites passages "
            "as [n]; sources lists them. If the documents don't say, it says so."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1, "maxLength": 2000},
                "document_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "maxItems": 20,
                    "description": "Only use these documents",
                },
                "collection_id": _uuid_schema("Only use this collection"),
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        "permission": Permission.CONVERSATIONS_USE,
    },
    "list_documents": {
        "title": "List documents",
        "description": "List documents, newest first, optionally filtered by title or collection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 200, "description": "Title contains"},
                "collection_id": _uuid_schema("Only this collection"),
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "additionalProperties": False,
        },
        "permission": Permission.DOCUMENTS_READ,
    },
    "read_document": {
        "title": "Read a document",
        "description": "Read a document's extracted text, a few pages at a time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": _uuid_schema("From search or list_documents"),
                "start": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "First page (or section) to read",
                },
                "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        "permission": Permission.DOCUMENTS_READ,
    },
    "list_collections": {
        "title": "List collections",
        "description": "List the collections documents are organized in.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "permission": Permission.DOCUMENTS_READ,
    },
}


class ToolInputError(Exception):
    pass


@dataclass
class WorkspaceTools:
    ctx: WorkspaceContext
    retrieval: RetrievalService
    rag: RAGOrchestrator
    documents: DocumentService
    collections: CollectionService
    queries: ProcessingQueries
    app_url: str
    #: Rate limit for AI-backed calls, and the answer quota.
    admit_ai: Callable[[], Awaitable[None]]
    admit_answer: Callable[[], Awaitable[None]]

    def link(self, document_id: uuid.UUID) -> str:
        return f"{self.app_url.rstrip('/')}/w/{self.ctx.workspace_id}/documents/{document_id}"

    def available(self) -> list[str]:
        return [name for name, t in TOOLS.items() if self.ctx.can(t["permission"])]

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        handler = getattr(self, f"_tool_{name}")
        result: tuple[str, dict[str, Any]] = await handler(args)
        return result

    async def _tool_search(self, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        query = _text(a, "query", 500)
        await self.admit_ai()
        collection = _opt_uuid(a, "collection_id")
        result = await self.retrieval.retrieve(
            self.ctx,
            query,
            top_k=_int(a, "limit", 8, 1, 20),
            collection_ids=[collection] if collection else None,
        )
        hits = [
            {
                "document_id": str(c.document_id),
                "document_title": c.document_title,
                "pages": _pages(c.page_start, c.page_end),
                "text": c.text[:_SNIPPET],
                "score": round(c.score, 4),
                "link": self.link(c.document_id),
            }
            for c in result.chunks
        ]
        if not hits:
            return "No matching passages.", {"results": []}
        lines = [
            f"{i}. {h['document_title']}{_where(h['pages'])}\n{h['text']}\n{h['link']}"
            for i, h in enumerate(hits, 1)
        ]
        return "\n\n".join(lines), {"results": hits}

    async def _tool_ask(self, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        question = _text(a, "question", 2000)
        ids = a.get("document_ids") or []
        if not isinstance(ids, list) or len(ids) > 20:
            raise ToolInputError("document_ids must be a list of up to 20 ids.")
        await self.admit_ai()
        await self.admit_answer()
        collection = _opt_uuid(a, "collection_id")
        result = await self.rag.answer(
            self.ctx,
            question,
            document_ids=[_as_uuid(i, "document_ids") for i in ids] or None,
            collection_ids=[collection] if collection else None,
        )
        sources = [
            {
                "number": c.number,
                "document_id": str(c.document_id),
                "document_title": c.document_name,
                "pages": _pages(c.page_start, c.page_end),
                "excerpt": c.excerpt,
                "link": self.link(c.document_id),
            }
            for c in result.citations
        ]
        text = result.answer
        if sources:
            text += "\n\nSources:\n" + "\n".join(
                f"[{s['number']}] {s['document_title']}{_where(s['pages'])} {s['link']}"
                for s in sources
            )
        return text, {"answer": result.answer, "status": result.status, "sources": sources}

    async def _tool_list_documents(self, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        query = a.get("query")
        if query is not None and not isinstance(query, str):
            raise ToolInputError("query must be text.")
        page = await self.documents.search(
            self.ctx,
            DocumentFilter(
                query=(query or "").strip()[:200] or None,
                collection_id=_opt_uuid(a, "collection_id"),
                limit=_int(a, "limit", 20, 1, 50),
                offset=_int(a, "offset", 0, 0, 100_000),
            ),
        )
        items = [
            {
                "id": str(d.id),
                "title": d.title,
                "status": str(d.status),
                "pages": d.page_count,
                "link": self.link(d.id),
            }
            for d in page.items
        ]
        if not items:
            return "No documents.", {"documents": [], "total": page.total}
        text = f"{page.total} document(s)" + "".join(
            f"\n- {d['title']} ({d['status']}) id={d['id']}" for d in items
        )
        return text, {"documents": items, "total": page.total}

    async def _tool_read_document(self, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        document_id = _as_uuid(a.get("document_id"), "document_id")
        start = _int(a, "start", 1, 1, 100_000)
        count = _int(a, "count", 3, 1, 10)
        info = await self.documents.get(self.ctx, document_id)
        pages, total = await self.queries.pages(
            self.ctx, document_id, offset=start - 1, limit=count
        )
        budget, parts, out = _PAGE_TEXT, [], []
        for p in pages:
            body = p.text[:budget]
            budget -= len(body)
            label = f"Page {p.page_number}" if p.page_number else f"Section {p.position + 1}"
            parts.append(f"## {label}\n{body}")
            out.append({"page": p.page_number, "position": p.position + 1, "text": body})
            if budget <= 0:
                break
        following = start + len(out)
        more = following <= total
        text = f"# {info.title}\n\n" + ("\n\n".join(parts) or "No text for these pages.")
        if more:
            text += f"\n\n(Continue with start={following}; {total} in total.)"
        return text, {
            "title": info.title,
            "pages": out,
            "total": total,
            "next_start": following if more else None,
            "link": self.link(document_id),
        }

    async def _tool_list_collections(self, a: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        del a
        items = [
            {"id": str(c.id), "name": c.name, "documents": c.document_count}
            for c in await self.collections.list(self.ctx)
        ]
        if not items:
            return "No collections.", {"collections": []}
        text = "\n".join(f"- {c['name']} ({c['documents']}) id={c['id']}" for c in items)
        return text, {"collections": items}


class McpServer:
    def __init__(self, tools: WorkspaceTools) -> None:
        self._tools = tools

    async def handle(self, message: Any) -> dict[str, Any] | None:
        """One JSON-RPC message in; the response, or None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message.")
        msg_id = message.get("id")
        method = message.get("method")
        if method is None:
            return None  # a response to us; we never send requests
        if not isinstance(method, str):
            return _error(msg_id, INVALID_REQUEST, "method must be a string.")
        if "id" not in message:
            return None  # notifications (initialized, cancelled, ...) need no reply
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object.")
        if method == "initialize":
            return _ok(msg_id, self._initialize(params))
        if method == "ping":
            return _ok(msg_id, {})
        if method == "tools/list":
            return _ok(msg_id, {"tools": [_describe(n) for n in self._tools.available()]})
        if method == "tools/call":
            return await self._call(msg_id, params)
        if method in ("resources/list", "prompts/list"):
            return _ok(msg_id, {method.split("/")[0]: []})
        return _error(msg_id, METHOD_NOT_FOUND, f"Unknown method {method!r}.")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    async def _call(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or name not in TOOLS:
            return _error(msg_id, INVALID_PARAMS, f"Unknown tool {name!r}.")
        if not isinstance(args, dict):
            return _error(msg_id, INVALID_PARAMS, "arguments must be an object.")
        if name not in self._tools.available():
            return _ok(msg_id, _tool_error("This key or account can't use that tool."))
        try:
            text, data = await self._tools.call(name, args)
        except ToolInputError as exc:
            return _ok(msg_id, _tool_error(str(exc)))
        except DomainError as exc:
            return _ok(msg_id, _tool_error(exc.message))
        return _ok(
            msg_id,
            {"content": [{"type": "text", "text": text}], "structuredContent": data},
        )


# ----------------------------------------------------------------- helpers


def _describe(name: str) -> dict[str, Any]:
    t = TOOLS[name]
    return {
        "name": name,
        "title": t["title"],
        "description": t["description"],
        "inputSchema": t["inputSchema"],
        "annotations": {"title": t["title"], **_READ_ONLY},
    }


def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _text(a: dict[str, Any], key: str, limit: int) -> str:
    value = a.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{key} is required.")
    if len(value) > limit:
        raise ToolInputError(f"{key} is too long (at most {limit} characters).")
    return value.strip()


def _int(a: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = a.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolInputError(f"{key} must be a whole number from {low} to {high}.")
    return value


def _as_uuid(value: Any, key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ToolInputError(f"{key} must be an id from search or list_documents.") from exc


def _opt_uuid(a: dict[str, Any], key: str) -> uuid.UUID | None:
    value = a.get(key)
    return None if value in (None, "") else _as_uuid(value, key)


def _pages(start: int | None, end: int | None) -> str | None:
    if start is None:
        return None
    return str(start) if end in (None, start) else f"{start}-{end}"


def _where(pages: object) -> str:
    return f", p. {pages}" if pages else ""
