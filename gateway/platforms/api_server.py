"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        — OpenAI Chat Completions format (stateless; opt-in session continuity via X-Hermes-Session-Id header)
- POST /v1/responses               — OpenAI Responses API format (stateful via previous_response_id)
- GET  /v1/responses/{response_id} — Retrieve a stored response
- DELETE /v1/responses/{response_id} — Delete a stored response
- GET  /v1/models                  — lists hermes-agent as an available model
- POST /v1/runs                    — start a run, returns run_id immediately (202)
- GET  /v1/runs/{run_id}/events    — SSE stream of structured lifecycle events
- GET  /health                     — health check

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat,
AnythingLLM, NextChat, ChatBox, etc.) can connect to hermes-agent
through this adapter by pointing at http://localhost:8642/v1.

Requires:
- aiohttp (already available in the gateway)
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path
import shutil
import socket as _socket
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    is_network_accessible,
)
from hermes_constants import DEFAULT_TENANT, get_user_subpath, normalize_tenant


logger = logging.getLogger(__name__)

# Default settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100
MAX_REQUEST_BYTES = 1_000_000  # 1 MB default limit for POST bodies
MAX_OUTPUT_FILE_BYTES = 50 * 1024 * 1024  # 50 MB cap for copied response artifacts
MAX_FILE_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB cap for direct uploads
CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0
MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 64 KB cap for normalized content parts
MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array

SUPPORTED_UPLOAD_MIME_PREFIXES = ("image/", "text/")
SUPPORTED_UPLOAD_MIME_TYPES = {"application/pdf"}
SUPPORTED_UPLOAD_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rtf",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".heic",
}


def _normalize_chat_content(
    content: Any, *, _max_depth: int = 10, _depth: int = 0,
) -> str:
    """Normalize OpenAI chat message content into a plain text string.

    Some clients (Open WebUI, LobeChat, etc.) send content as an array of
    typed parts instead of a plain string::

        [{"type": "text", "text": "hello"}, {"type": "input_text", "text": "..."}]

    This function flattens those into a single string so the agent pipeline
    (which expects strings) doesn't choke.

    Defensive limits prevent abuse: recursion depth, list size, and output
    length are all bounded.
    """
    if _depth > _max_depth:
        return ""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content

    if isinstance(content, list):
        parts: List[str] = []
        items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
        for item in items:
            if isinstance(item, str):
                if item:
                    parts.append(item[:MAX_NORMALIZED_TEXT_LENGTH])
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"text", "input_text", "output_text"}:
                    text = item.get("text", "")
                    if text:
                        try:
                            parts.append(str(text)[:MAX_NORMALIZED_TEXT_LENGTH])
                        except Exception:
                            pass
                # Silently skip image_url / other non-text parts
            elif isinstance(item, list):
                nested = _normalize_chat_content(item, _max_depth=_max_depth, _depth=_depth + 1)
                if nested:
                    parts.append(nested)
            # Check accumulated size
            if sum(len(p) for p in parts) >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        result = "\n".join(parts)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result

    # Fallback for unexpected types (int, float, bool, etc.)
    try:
        result = str(content)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result
    except Exception:
        return ""


def check_api_server_requirements() -> bool:
    """Check if API server dependencies are available."""
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    SQLite-backed LRU store for Responses API state with tenant scoping.

    Each stored response includes the full internal conversation history
    (with tool calls and results) so it can be reconstructed on subsequent
    requests via previous_response_id.

    Persists across gateway restarts. Falls back to in-memory SQLite
    if the on-disk path is unavailable.
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES, db_path: str = None):
        self._max_size = max_size
        if db_path is None:
            try:
                from hermes_cli.config import get_hermes_home
                db_path = str(get_hermes_home() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    @staticmethod
    def _tenant(user_id: Optional[str]) -> str:
        return normalize_tenant(user_id)

    def _table_info(self, name: str) -> list[sqlite3.Row]:
        return self._conn.execute(f"PRAGMA table_info({name})").fetchall()

    def _init_schema(self) -> None:
        """Create or migrate tables to include tenant scoping."""
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Create tables if missing
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                user_id TEXT NOT NULL,
                response_id TEXT NOT NULL,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL,
                PRIMARY KEY (user_id, response_id)
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                response_id TEXT NOT NULL,
                PRIMARY KEY (user_id, name)
            )"""
        )
        self._conn.commit()
        # Migrate legacy tables lacking user_id/composite PK
        self._migrate_table(
            table="responses",
            create_sql="""CREATE TABLE responses (
                user_id TEXT NOT NULL,
                response_id TEXT NOT NULL,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL,
                PRIMARY KEY (user_id, response_id)
            )""",
            copy_sql_with_user="INSERT INTO responses (user_id, response_id, data, accessed_at) SELECT COALESCE(user_id, 'default'), response_id, data, accessed_at FROM responses_old",
            copy_sql_without_user="INSERT INTO responses (user_id, response_id, data, accessed_at) SELECT 'default', response_id, data, accessed_at FROM responses_old",
            required_pk=["user_id", "response_id"],
        )
        self._migrate_table(
            table="conversations",
            create_sql="""CREATE TABLE conversations (
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                response_id TEXT NOT NULL,
                PRIMARY KEY (user_id, name)
            )""",
            copy_sql_with_user="INSERT INTO conversations (user_id, name, response_id) SELECT COALESCE(user_id, 'default'), name, response_id FROM conversations_old",
            copy_sql_without_user="INSERT INTO conversations (user_id, name, response_id) SELECT 'default', name, response_id FROM conversations_old",
            required_pk=["user_id", "name"],
        )
        # Helpful index for eviction by tenant
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_responses_user_accessed ON responses(user_id, accessed_at)")
        self._conn.commit()

    def _migrate_table(
        self,
        *,
        table: str,
        create_sql: str,
        copy_sql_with_user: str,
        copy_sql_without_user: str,
        required_pk: list[str],
    ) -> None:
        info = self._table_info(table)
        if not info:
            # Table was just created above
            return
        columns = [row[1] for row in info]
        pk_cols = [row[1] for row in info if row[5] > 0]
        needs_user = "user_id" not in columns
        needs_pk = pk_cols != required_pk
        if not needs_user and not needs_pk:
            return

        self._conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        self._conn.execute(create_sql)
        if not needs_user:
            self._conn.execute(copy_sql_with_user)
        else:
            self._conn.execute(copy_sql_without_user)
        self._conn.execute(f"DROP TABLE {table}_old")
        self._conn.commit()

    def get(self, response_id: str, user_id: str | None = None) -> Optional[Dict[str, Any]]:
        """Retrieve a stored response by ID (updates access time for LRU)."""
        tenant = self._tenant(user_id)
        row = self._conn.execute(
            "SELECT data FROM responses WHERE user_id = ? AND response_id = ?",
            (tenant, response_id),
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE responses SET accessed_at = ? WHERE user_id = ? AND response_id = ?",
            (time.time(), tenant, response_id),
        )
        self._conn.commit()
        return json.loads(row[0] if not isinstance(row, sqlite3.Row) else row["data"])

    def _evict_tenant(self, tenant: str) -> None:
        count_row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM responses WHERE user_id = ?", (tenant,)
        ).fetchone()
        count = count_row[0] if not isinstance(count_row, sqlite3.Row) else count_row["cnt"]
        if count > self._max_size:
            self._conn.execute(
                "DELETE FROM responses WHERE rowid IN ("
                "SELECT rowid FROM responses WHERE user_id = ? ORDER BY accessed_at ASC LIMIT ?"
                ")",
                (tenant, count - self._max_size),
            )

    def put(self, response_id: str, data: Dict[str, Any], user_id: str | None = None) -> None:
        """Store a response, evicting the oldest within the tenant if at capacity."""
        tenant = self._tenant(user_id)
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (user_id, response_id, data, accessed_at) VALUES (?, ?, ?, ?)",
            (tenant, response_id, json.dumps(data, default=str), time.time()),
        )
        self._evict_tenant(tenant)
        self._conn.commit()

    def delete(self, response_id: str, user_id: str | None = None) -> bool:
        """Remove a response from the store. Returns True if found and deleted."""
        tenant = self._tenant(user_id)
        cursor = self._conn.execute(
            "DELETE FROM responses WHERE user_id = ? AND response_id = ?",
            (tenant, response_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_conversation(self, name: str, user_id: str | None = None) -> Optional[str]:
        """Get the latest response_id for a conversation name scoped to a tenant."""
        tenant = self._tenant(user_id)
        row = self._conn.execute(
            "SELECT response_id FROM conversations WHERE user_id = ? AND name = ?",
            (tenant, name),
        ).fetchone()
        if row is None:
            return None
        return row[0] if not isinstance(row, sqlite3.Row) else row["response_id"]

    def set_conversation(self, name: str, response_id: str, user_id: str | None = None) -> None:
        """Map a conversation name to its latest response_id for a tenant."""
        tenant = self._tenant(user_id)
        self._conn.execute(
            "INSERT OR REPLACE INTO conversations (user_id, name, response_id) VALUES (?, ?, ?)",
            (tenant, name, response_id),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return row[0] if row else 0


class OutputFileStore:
    """Tenant-scoped file registry for uploads and response artifacts."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            try:
                from hermes_cli.config import get_hermes_home
                db_path = str(get_hermes_home() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    @staticmethod
    def _tenant(user_id: Optional[str]) -> str:
        return normalize_tenant(user_id)

    @staticmethod
    def _sanitize_filename(name: str, *, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._")
        return cleaned or fallback

    def _init_schema(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS output_files (
                user_id TEXT NOT NULL,
                file_id TEXT NOT NULL,
                storage_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at REAL NOT NULL,
                purpose TEXT NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY (user_id, file_id)
            )"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_output_files_user_created "
            "ON output_files(user_id, created_at)"
        )
        self._ensure_columns()
        self._conn.commit()

    def _ensure_columns(self) -> None:
        info = self._conn.execute("PRAGMA table_info(output_files)").fetchall()
        cols = {row[1] for row in info}
        if "purpose" not in cols:
            self._conn.execute(
                "ALTER TABLE output_files ADD COLUMN purpose TEXT NOT NULL DEFAULT 'uploads'"
            )
        if "source" not in cols:
            self._conn.execute(
                "ALTER TABLE output_files ADD COLUMN source TEXT NOT NULL DEFAULT 'upload'"
            )
        self._conn.commit()

    def _write_metadata_file(self, meta: Dict[str, Any], tenant: str, *, target_dir: Path | None = None) -> None:
        try:
            target = target_dir or get_user_subpath(tenant, "api_server", "files")
            target.mkdir(parents=True, exist_ok=True)
            metadata_path = target / f"{meta['file_id']}.json"
            metadata = {
                "file_id": meta["file_id"],
                "user_id": tenant,
                "filename": meta["filename"],
                "mime_type": meta["mime_type"],
                "size_bytes": meta["size_bytes"],
                "purpose": meta.get("purpose"),
                "created_at": meta["created_at"],
                "path": meta["path"],
                "source": meta.get("source"),
            }
            metadata_path.write_text(json.dumps(metadata, default=str))
        except Exception:
            logger.debug("Failed to write file metadata for %s", meta.get("file_id"), exc_info=True)

    def _build_meta(
        self,
        *,
        file_id: str,
        tenant: str,
        storage_name: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        created_at: float,
        purpose: str,
        source: str,
    ) -> Dict[str, Any]:
        stored_path = get_user_subpath(tenant, "api_server", "files", storage_name)
        return {
            "file_id": file_id,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "created_at": int(created_at),
            "path": str(stored_path),
            "purpose": purpose,
            "source": source,
        }

    def store_bytes(
        self,
        filename: str,
        data: bytes,
        *,
        user_id: str | None = None,
        purpose: str = "uploads",
        mime_type: str | None = None,
        source: str = "upload",
    ) -> Dict[str, Any]:
        tenant = self._tenant(user_id)
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("File content must be bytes")
        size_bytes = len(data)
        if size_bytes > MAX_OUTPUT_FILE_BYTES:
            raise ValueError(
                f"Output file exceeds {MAX_OUTPUT_FILE_BYTES} bytes"
            )

        file_id = f"file_{uuid.uuid4().hex[:24]}"
        safe_name = self._sanitize_filename(filename, fallback=f"{file_id}.bin")
        storage_name = f"{file_id}_{safe_name}"
        target_dir = get_user_subpath(tenant, "api_server", "files")
        target_dir.mkdir(parents=True, exist_ok=True)
        stored_path = target_dir / storage_name
        stored_path.write_bytes(bytes(data))

        resolved_mime = mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        created_at = time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO output_files "
            "(user_id, file_id, storage_name, filename, mime_type, size_bytes, created_at, purpose, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tenant,
                file_id,
                storage_name,
                safe_name,
                resolved_mime,
                size_bytes,
                created_at,
                purpose or "uploads",
                source or "upload",
            ),
        )
        self._conn.commit()
        meta = self._build_meta(
            file_id=file_id,
            tenant=tenant,
            storage_name=storage_name,
            filename=safe_name,
            mime_type=resolved_mime,
            size_bytes=size_bytes,
            created_at=created_at,
            purpose=purpose or "uploads",
            source=source or "upload",
        )
        self._write_metadata_file(meta, tenant, target_dir=target_dir)
        return meta

    def put_from_path(
        self,
        file_path: str,
        *,
        user_id: str | None = None,
        purpose: str = "output",
        source: str = "output_file",
    ) -> Dict[str, Any]:
        tenant = self._tenant(user_id)
        source_path = Path(os.path.expanduser(file_path)).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Output file not found: {source_path}")

        size_bytes = source_path.stat().st_size
        if size_bytes > MAX_OUTPUT_FILE_BYTES:
            raise ValueError(
                f"Output file exceeds {MAX_OUTPUT_FILE_BYTES} bytes: {source_path}"
            )

        file_id = f"file_{uuid.uuid4().hex[:24]}"
        filename = self._sanitize_filename(source_path.name, fallback=f"{file_id}.bin")
        storage_name = f"{file_id}_{filename}"
        target_dir = get_user_subpath(tenant, "api_server", "files")
        target_dir.mkdir(parents=True, exist_ok=True)
        stored_path = target_dir / storage_name
        shutil.copy2(source_path, stored_path)

        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        created_at = time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO output_files "
            "(user_id, file_id, storage_name, filename, mime_type, size_bytes, created_at, purpose, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tenant,
                file_id,
                storage_name,
                filename,
                mime_type,
                size_bytes,
                created_at,
                purpose or "output",
                source or "output_file",
            ),
        )
        self._conn.commit()
        meta = self._build_meta(
            file_id=file_id,
            tenant=tenant,
            storage_name=storage_name,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            created_at=created_at,
            purpose=purpose or "output",
            source=source or "output_file",
        )
        self._write_metadata_file(meta, tenant, target_dir=target_dir)
        return meta

    def list(self, user_id: str | None = None) -> List[Dict[str, Any]]:
        tenant = self._tenant(user_id)
        rows = self._conn.execute(
            "SELECT file_id, storage_name, filename, mime_type, size_bytes, created_at, purpose, source "
            "FROM output_files WHERE user_id = ? ORDER BY created_at DESC",
            (tenant,),
        ).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            stored_path = get_user_subpath(tenant, "api_server", "files", row["storage_name"])
            if not stored_path.is_file():
                continue
            results.append(
                self._build_meta(
                    file_id=row["file_id"],
                    tenant=tenant,
                    storage_name=row["storage_name"],
                    filename=row["filename"],
                    mime_type=row["mime_type"],
                    size_bytes=row["size_bytes"],
                    created_at=row["created_at"],
                    purpose=row["purpose"],
                    source=row["source"],
                )
            )
        return results

    def get(self, file_id: str, user_id: str | None = None) -> Optional[Dict[str, Any]]:
        tenant = self._tenant(user_id)
        row = self._conn.execute(
            "SELECT storage_name, filename, mime_type, size_bytes, created_at, purpose, source "
            "FROM output_files WHERE user_id = ? AND file_id = ?",
            (tenant, file_id),
        ).fetchone()
        if row is None:
            return None
        stored_path = get_user_subpath(tenant, "api_server", "files", row["storage_name"])
        if not stored_path.is_file():
            return None
        return self._build_meta(
            file_id=file_id,
            tenant=tenant,
            storage_name=row["storage_name"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            created_at=row["created_at"],
            purpose=row["purpose"],
            source=row["source"],
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """Add CORS headers for explicitly allowed origins; handle OPTIONS preflight."""
        adapter = request.app.get("api_server_adapter")
        origin = request.headers.get("Origin", "")
        cors_headers = None
        if adapter is not None:
            if not adapter._origin_allowed(origin):
                return web.Response(status=403)
            cors_headers = adapter._cors_headers_for_origin(origin)

        if request.method == "OPTIONS":
            if cors_headers is None:
                return web.Response(status=403)
            return web.Response(status=200, headers=cors_headers)

        response = await handler(request)
        if cors_headers is not None:
            response.headers.update(cors_headers)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


def _openai_error(message: str, err_type: str = "invalid_request_error", param: str = None, code: str = None) -> Dict[str, Any]:
    """OpenAI-style error envelope."""
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


class _TenantValidationError(Exception):
    """Raised when an invalid tenant/user identifier is supplied."""


class _InputFileNormalizationError(Exception):
    """Raised when an input_file part cannot be resolved."""

    def __init__(self, message: str, *, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or "invalid_request_error"


class _UploadTooLarge(Exception):
    """Raised when an upload exceeds configured limits."""


class _MultipartParseError(Exception):
    """Raised when a multipart payload is malformed or missing required parts."""


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def body_limit_middleware(request, handler):
        """Reject overly large request bodies early based on Content-Length."""
        if request.method in ("POST", "PUT", "PATCH"):
            cl = request.headers.get("Content-Length")
            if cl is not None:
                try:
                    limit = MAX_FILE_UPLOAD_BYTES if request.path.startswith("/v1/files") else MAX_REQUEST_BYTES
                    if int(cl) > limit:
                        code = "file_too_large" if request.path.startswith("/v1/files") else "body_too_large"
                        return web.json_response(_openai_error("Request body too large.", code=code), status=413)
                except ValueError:
                    return web.json_response(_openai_error("Invalid Content-Length header.", code="invalid_content_length"), status=400)
        return await handler(request)
else:
    body_limit_middleware = None  # type: ignore[assignment]

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def security_headers_middleware(request, handler):
        """Add security headers to all responses (including errors)."""
        response = await handler(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response
else:
    security_headers_middleware = None  # type: ignore[assignment]


class _IdempotencyCache:
    """In-memory idempotency cache with TTL and basic LRU semantics."""
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 300):
        from collections import OrderedDict
        self._store = OrderedDict()
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        import time as _t
        now = _t.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_set(self, key: str, fingerprint: str, compute_coro):
        self._purge()
        item = self._store.get(key)
        if item and item["fp"] == fingerprint:
            return item["resp"]
        resp = await compute_coro()
        import time as _t
        self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
        self._purge()
        return resp


_idem_cache = _IdempotencyCache()


def _make_request_fingerprint(body: Dict[str, Any], keys: List[str]) -> str:
    from hashlib import sha256
    subset = {k: body.get(k) for k in keys}
    return sha256(repr(subset).encode("utf-8")).hexdigest()


def _derive_chat_session_id(
    system_prompt: Optional[str],
    first_user_message: str,
) -> str:
    """Derive a stable session ID from the conversation's first user message.

    OpenAI-compatible frontends (Open WebUI, LibreChat, etc.) send the full
    conversation history with every request.  The system prompt and first user
    message are constant across all turns of the same conversation, so hashing
    them produces a deterministic session ID that lets the API server reuse
    the same Hermes session (and therefore the same Docker container sandbox
    directory) across turns.
    """
    seed = f"{system_prompt or ''}\n{first_user_message}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI-compatible HTTP API server adapter.

    Runs an aiohttp web server that accepts OpenAI-format requests
    and routes them through hermes-agent's AIAgent.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.API_SERVER)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        self._port: int = int(extra.get("port", os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))))
        self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
        self._cors_origins: tuple[str, ...] = self._parse_cors_origins(
            extra.get("cors_origins", os.getenv("API_SERVER_CORS_ORIGINS", "")),
        )
        self._model_name: str = self._resolve_model_name(
            extra.get("model_name", os.getenv("API_SERVER_MODEL_NAME", "")),
        )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._response_store = ResponseStore()
        self._output_file_store = OutputFileStore()
        # Active run streams: run_id -> asyncio.Queue of SSE event dicts
        self._run_streams: Dict[str, "asyncio.Queue[Optional[Dict]]"] = {}
        # Creation timestamps for orphaned-run TTL sweep
        self._run_streams_created: Dict[str, float] = {}
        self._session_db: Optional[Any] = None  # Lazy-init SessionDB for session continuity

    @staticmethod
    def _parse_cors_origins(value: Any) -> tuple[str, ...]:
        """Normalize configured CORS origins into a stable tuple."""
        if not value:
            return ()

        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = [str(value)]

        return tuple(str(item).strip() for item in items if str(item).strip())

    @staticmethod
    def _resolve_model_name(explicit: str) -> str:
        """Derive the advertised model name for /v1/models.

        Priority:
        1. Explicit override (config extra or API_SERVER_MODEL_NAME env var)
        2. Active profile name (so each profile advertises a distinct model)
        3. Fallback: "hermes-agent"
        """
        if explicit and explicit.strip():
            return explicit.strip()
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile = get_active_profile_name()
            if profile and profile not in ("default", "custom"):
                return profile
        except Exception:
            pass
        return "hermes-agent"

    def _cors_headers_for_origin(self, origin: str) -> Optional[Dict[str, str]]:
        """Return CORS headers for an allowed browser origin."""
        if not origin or not self._cors_origins:
            return None

        if "*" in self._cors_origins:
            headers = dict(_CORS_HEADERS)
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Max-Age"] = "600"
            return headers

        if origin not in self._cors_origins:
            return None

        headers = dict(_CORS_HEADERS)
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Max-Age"] = "600"
        return headers

    def _origin_allowed(self, origin: str) -> bool:
        """Allow non-browser clients and explicitly configured browser origins."""
        if not origin:
            return True

        if not self._cors_origins:
            return False

        return "*" in self._cors_origins or origin in self._cors_origins

    # ------------------------------------------------------------------
    # Tenant helper
    # ------------------------------------------------------------------

    def _extract_tenant(self, request: "web.Request", body: Optional[Dict[str, Any]] = None) -> str:
        """Extract and validate tenant/user identifier from the request."""
        raw = None
        if isinstance(body, dict):
            raw = body.get("user_id")
            if raw is None:
                raw = body.get("user")
        if raw is None and request is not None:
            raw = request.headers.get("X-Hermes-User-Id") or request.headers.get("X-OpenAI-User")
        if raw is None and request is not None:
            raw = request.query.get("user_id") or request.query.get("user")
        if raw is None:
            return DEFAULT_TENANT

        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            raw = str(raw)
        if not isinstance(raw, str):
            raise _TenantValidationError("Invalid user_id: must be a non-empty string.")
        raw = raw.strip()
        if not raw:
            raise _TenantValidationError("Invalid user_id: must be a non-empty string.")
        return normalize_tenant(raw)

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        Validate Bearer token from Authorization header.

        Returns None if auth is OK, or a 401 web.Response on failure.
        If no API key is configured, all requests are allowed (only when API
        server is local).
        """
        if not self._api_key:
            return None  # No key configured — allow all (local-only use)

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if hmac.compare_digest(token, self._api_key):
                return None  # Auth OK

        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status=401,
        )

    # ------------------------------------------------------------------
    # Session DB helper
    # ------------------------------------------------------------------

    def _ensure_session_db(self):
        """Lazily initialise and return the shared SessionDB instance.

        Sessions are persisted to ``state.db`` so that ``hermes sessions list``
        shows API-server conversations alongside CLI and gateway ones.
        """
        if self._session_db is None:
            try:
                from hermes_state import SessionDB
                self._session_db = SessionDB()
            except Exception as e:
                logger.debug("SessionDB unavailable for API server: %s", e)
        return self._session_db

    # ------------------------------------------------------------------
    # Agent creation helper
    # ------------------------------------------------------------------

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        user_id: Optional[str] = None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.  Toolsets are resolved
        from config.yaml platform_toolsets.api_server (same as all other
        gateway platforms), falling back to the hermes-api-server default.
        """
        from run_agent import AIAgent
        from gateway.run import _resolve_runtime_agent_kwargs, _resolve_gateway_model, _load_gateway_config
        from hermes_cli.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        model = _resolve_gateway_model()

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))

        max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))

        # Load fallback provider chain so the API server platform has the
        # same fallback behaviour as Telegram/Discord/Slack (fixes #4954).
        from gateway.run import GatewayRunner
        fallback_model = GatewayRunner._load_fallback_model()

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="api_server",
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
            user_id=user_id,
        )
        return agent

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "hermes-agent"})

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models — return hermes-agent as an available model."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": self._model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "hermes",
                    "permission": [],
                    "root": self._model_name,
                    "parent": None,
                }
            ],
        })

    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions — OpenAI Chat Completions format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(_openai_error("Invalid JSON in request body"), status=400)

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return web.json_response(
                {"error": {"message": "Missing or invalid 'messages' field", "type": "invalid_request_error"}},
                status=400,
            )

        stream = body.get("stream", False)

        try:
            tenant = self._extract_tenant(request, body)
        except _TenantValidationError as exc:
            return web.json_response(_openai_error(str(exc), param="user", code="invalid_user"), status=400)

        # Extract system message (becomes ephemeral system prompt layered ON TOP of core)
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []

        for msg in messages:
            role = msg.get("role", "")
            raw_content = msg.get("content", "")
            if role == "system":
                # Accumulate system messages
                content = _normalize_chat_content(raw_content)
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in ("user", "assistant"):
                if role == "user" and isinstance(raw_content, list):
                    try:
                        content = await self._normalize_chat_user_content(raw_content, tenant)
                    except _InputFileNormalizationError as exc:
                        return web.json_response(
                            _openai_error(exc.message, code=exc.code),
                            status=exc.status,
                        )
                else:
                    content = _normalize_chat_content(raw_content)
                conversation_messages.append({"role": role, "content": content})

        # Extract the last user message as the primary input
        user_message = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not user_message:
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        # Allow caller to continue an existing session by passing X-Hermes-Session-Id.
        # When provided, history is loaded from state.db instead of from the request body.
        #
        # Security: session continuation exposes conversation history, so it is
        # only allowed when the API key is configured and the request is
        # authenticated.  Without this gate, any unauthenticated client could
        # read arbitrary session history by guessing/enumerating session IDs.
        provided_session_id = request.headers.get("X-Hermes-Session-Id", "").strip()
        if provided_session_id:
            if not self._api_key:
                logger.warning(
                    "Session continuation via X-Hermes-Session-Id rejected: "
                    "no API key configured.  Set API_SERVER_KEY to enable "
                    "session continuity."
                )
                return web.json_response(
                    _openai_error(
                        "Session continuation requires API key authentication. "
                        "Configure API_SERVER_KEY to enable this feature."
                    ),
                    status=403,
                )
            # Sanitize: reject control characters that could enable header injection.
            if re.search(r'[\r\n\x00]', provided_session_id):
                return web.json_response(
                    {"error": {"message": "Invalid session ID", "type": "invalid_request_error"}},
                    status=400,
                )
            session_id = provided_session_id
            try:
                db = self._ensure_session_db()
                if db is not None:
                    history = db.get_messages_as_conversation(session_id, user_id=tenant)
            except Exception as e:
                logger.warning("Failed to load session history for %s: %s", session_id, e)
                history = []
        else:
            # Derive a stable session ID from the conversation fingerprint so
            # that consecutive messages from the same Open WebUI (or similar)
            # conversation map to the same Hermes session.  The first user
            # message + system prompt are constant across all turns.
            first_user = ""
            for cm in conversation_messages:
                if cm.get("role") == "user":
                    first_user = cm.get("content", "")
                    break
            session_id = _derive_chat_session_id(system_prompt, first_user)
            # history already set from request body above

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", self._model_name)
        created = int(time.time())

        if stream:
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # Filter out None — the agent fires stream_delta_callback(None)
                # to signal the CLI display to close its response box before
                # tool execution, but the SSE writer uses None as end-of-stream
                # sentinel.  Forwarding it would prematurely close the HTTP
                # response, causing Open WebUI (and similar frontends) to miss
                # the final answer after tool calls.  The SSE loop detects
                # completion via agent_task.done() instead.
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                """Send tool progress as a separate SSE event.

                Previously, progress markers like ``⏰ list`` were injected
                directly into ``delta.content``.  OpenAI-compatible frontends
                (Open WebUI, LobeChat, …) store ``delta.content`` verbatim as
                the assistant message and send it back on subsequent requests.
                After enough turns the model learns to *emit* the markers as
                plain text instead of issuing real tool calls — silently
                hallucinating tool results.  See #6972.

                The fix: push a tagged tuple ``("__tool_progress__", payload)``
                onto the stream queue.  The SSE writer emits it as a custom
                ``event: hermes.tool.progress`` line that compliant frontends
                can render for UX but will *not* persist into conversation
                history.  Clients that don't understand the custom event type
                silently ignore it per the SSE specification.
                """
                if event_type != "tool.started":
                    return
                if name.startswith("_"):
                    return
                from agent.display import get_tool_emoji
                emoji = get_tool_emoji(name)
                label = preview or name
                _stream_q.put(("__tool_progress__", {
                    "tool": name,
                    "emoji": emoji,
                    "label": label,
                }))

            # Start agent in background.  agent_ref is a mutable container
            # so the SSE writer can interrupt the agent on client disconnect.
            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                agent_ref=agent_ref,
                user_id=tenant,
            ))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=session_id,
            )

        # Non-streaming: run the agent (with optional Idempotency-Key)
        async def _compute_completion():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                user_id=tenant,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(body, keys=["model", "messages", "tools", "tool_choice", "stream"])
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_completion)
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_completion()
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_data = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_response,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        return web.json_response(response_data, headers={"X-Hermes-Session-Id": session_id})

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None, session_id: str = None,
    ) -> "web.StreamResponse":
        """Write real streaming SSE from agent's stream_delta_callback queue.

        If the client disconnects mid-stream (network drop, browser tab close),
        the agent is interrupted via ``agent.interrupt()`` so it stops making
        LLM API calls, and the asyncio task wrapper is cancelled.
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        # CORS middleware can't inject headers into StreamResponse after
        # prepare() flushes them, so resolve CORS headers up front.
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        try:
            last_activity = time.monotonic()

            # Role chunk
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())
            last_activity = time.monotonic()

            # Helper — route a queue item to the correct SSE event.
            async def _emit(item):
                """Write a single queue item to the SSE stream.

                Plain strings are sent as normal ``delta.content`` chunks.
                Tagged tuples ``("__tool_progress__", payload)`` are sent
                as a custom ``event: hermes.tool.progress`` SSE event so
                frontends can display them without storing the markers in
                conversation history.  See #6972.
                """
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "__tool_progress__":
                    event_data = json.dumps(item[1])
                    await response.write(
                        f"event: hermes.tool.progress\ndata: {event_data}\n\n".encode()
                    )
                else:
                    content_chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"content": item}, "finish_reason": None}],
                    }
                    await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())
                return time.monotonic()

            # Stream content chunks as they arrive from the agent
            loop = asyncio.get_event_loop()
            while True:
                try:
                    delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain any remaining items
                        while True:
                            try:
                                delta = stream_q.get_nowait()
                                if delta is None:
                                    break
                                last_activity = await _emit(delta)
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if delta is None:  # End of stream sentinel
                    break

                last_activity = await _emit(delta)

            # Get usage from completed agent
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
            except Exception:
                pass

            # Finish chunk
            finish_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
            await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # Client disconnected mid-stream.  Interrupt the agent so it
            # stops making LLM API calls at the next loop iteration, then
            # cancel the asyncio task wrapper.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", completion_id)

        return response

    def _render_input_file(self, meta: Dict[str, Any]) -> str:
        header = (
            f"[input_file:{meta.get('filename', 'file')} ("
            f"{meta.get('mime_type', 'unknown')}, {meta.get('size_bytes', 0)} bytes)]"
        )
        try:
            data = Path(meta["path"]).read_bytes()
        except Exception as exc:
            logger.warning("[api_server] failed to read input_file %s: %s", meta.get("file_id"), exc)
            return header

        if not data:
            return header

        preview = ""
        mime = (meta.get("mime_type") or "").lower()
        suffix = Path(meta.get("filename", "")).suffix.lower()
        text_like_suffixes = {".txt", ".md", ".markdown", ".rtf", ".json", ".csv", ".tsv", ".py", ".js", ".html", ".mdx"}
        is_text_like = mime.startswith("text/") or mime in {"application/json"} or suffix in text_like_suffixes
        if is_text_like:
            try:
                preview = data.decode("utf-8", errors="replace")
            except Exception:
                try:
                    preview = data.decode(errors="replace")
                except Exception:
                    preview = ""

        if preview:
            preview = preview[:MAX_NORMALIZED_TEXT_LENGTH]
            return f"{header}\n{preview}"
        return header

    @staticmethod
    def _strip_local_paths(text: Any) -> Any:
        if not isinstance(text, str):
            return text
        cleaned = re.sub(r"\[local_path:[^\]\n]+\]", "", text)
        cleaned = re.sub(r"Local path:\s+\S+", "", cleaned)
        _paths, cleaned = BasePlatformAdapter.extract_local_files(cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    def _sanitize_history_local_paths(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sanitized: List[Dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            new_msg = dict(msg)
            new_msg["content"] = self._strip_local_paths(msg.get("content"))
            sanitized.append(new_msg)
        return sanitized

    async def _vision_summary_for_input_file(self, meta: Dict[str, Any]) -> str:
        mime = (meta.get("mime_type") or "").lower()
        if not mime.startswith("image/"):
            return ""
        path = meta.get("path")
        if not path:
            return ""
        try:
            from tools.vision_tools import vision_analyze_tool
            result_json = await vision_analyze_tool(
                image_url=path,
                user_prompt=(
                    "Describe this image concisely so a text-only model can understand "
                    "objects, text, and layout. Include any visible text content."
                ),
            )
            parsed = json.loads(result_json or "{}")
            if parsed.get("success") and parsed.get("analysis"):
                return str(parsed.get("analysis")).strip()
        except Exception as exc:
            logger.debug("[api_server] vision enrichment failed for %s: %s", path, exc)
        return ""

    async def _build_input_file_context(
        self,
        meta: Dict[str, Any],
        *,
        include_path: bool = True,
    ) -> tuple[str, str]:
        base = self._render_input_file(meta)
        path_hint = f"[local_path:{meta['path']}]" if include_path and meta.get("path") else ""
        vision_summary = await self._vision_summary_for_input_file(meta)

        agent_parts = [part for part in (base, path_hint) if part]
        storage_parts = [base]
        if vision_summary:
            vision_line = f"[vision_summary] {vision_summary}"
            agent_parts.append(vision_line)
            storage_parts.append(vision_line)

        agent_text = "\n".join(agent_parts)
        storage_text = "\n".join(storage_parts)

        if len(agent_text) > MAX_NORMALIZED_TEXT_LENGTH:
            agent_text = agent_text[:MAX_NORMALIZED_TEXT_LENGTH]
        if len(storage_text) > MAX_NORMALIZED_TEXT_LENGTH:
            storage_text = storage_text[:MAX_NORMALIZED_TEXT_LENGTH]
        return agent_text, storage_text

    async def _normalize_response_content_parts(
        self,
        parts: list[Any],
        tenant: str,
        *,
        _depth: int = 0,
        _max_depth: int = 10,
    ) -> tuple[str, str]:
        if _depth > _max_depth:
            return "", ""
        agent_parts: list[str] = []
        storage_parts: list[str] = []
        items = parts[:MAX_CONTENT_LIST_SIZE] if len(parts) > MAX_CONTENT_LIST_SIZE else parts
        for part in items:
            if isinstance(part, str):
                if part:
                    text_val = part[:MAX_NORMALIZED_TEXT_LENGTH]
                    agent_parts.append(text_val)
                    storage_parts.append(text_val)
                continue
            if isinstance(part, dict):
                part_type = str(part.get("type") or "").strip().lower()
                if part_type in {"text", "input_text", "output_text"}:
                    text_val = part.get("text", "")
                    if text_val:
                        normalized = str(text_val)[:MAX_NORMALIZED_TEXT_LENGTH]
                        agent_parts.append(normalized)
                        storage_parts.append(normalized)
                elif part_type == "input_file":
                    file_id = str(part.get("file_id") or "").strip()
                    if not file_id:
                        raise _InputFileNormalizationError("Missing file_id for input_file part")
                    meta = self._output_file_store.get(file_id, user_id=tenant)
                    if meta is None:
                        raise _InputFileNormalizationError(
                            f"File not found: {file_id}",
                            status=404,
                            code="file_not_found",
                        )
                    agent_ctx, storage_ctx = await self._build_input_file_context(meta)
                    if agent_ctx:
                        agent_parts.append(agent_ctx)
                    if storage_ctx:
                        storage_parts.append(storage_ctx)
                else:
                    continue
            elif isinstance(part, list):
                nested_agent, nested_storage = await self._normalize_response_content_parts(
                    part, tenant, _depth=_depth + 1, _max_depth=_max_depth
                )
                if nested_agent:
                    agent_parts.append(nested_agent)
                if nested_storage:
                    storage_parts.append(nested_storage)
            if sum(len(p) for p in agent_parts) >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        combined_agent = "\n".join([p for p in agent_parts if p])
        combined_storage = "\n".join([p for p in storage_parts if p])
        if len(combined_agent) > MAX_NORMALIZED_TEXT_LENGTH:
            combined_agent = combined_agent[:MAX_NORMALIZED_TEXT_LENGTH]
        if len(combined_storage) > MAX_NORMALIZED_TEXT_LENGTH:
            combined_storage = combined_storage[:MAX_NORMALIZED_TEXT_LENGTH]
        return combined_agent, combined_storage

    async def _normalize_chat_user_content(self, content: Any, tenant: str) -> str:
        """Normalize chat user content, resolving retained file references."""
        if isinstance(content, list):
            agent_content, _ = await self._normalize_response_content_parts(content, tenant)
            return agent_content
        return _normalize_chat_content(content)

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — OpenAI Responses API format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        raw_input = body.get("input")
        if raw_input is None:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = body.get("store", True)

        try:
            tenant = self._extract_tenant(request, body)
        except _TenantValidationError as exc:
            return web.json_response(_openai_error(str(exc), param="user", code="invalid_user"), status=400)

        # conversation and previous_response_id are mutually exclusive
        if conversation and previous_response_id:
            return web.json_response(_openai_error("Cannot use both 'conversation' and 'previous_response_id'"), status=400)

        # Resolve conversation name to latest response_id
        if conversation:
            previous_response_id = self._response_store.get_conversation(conversation, user_id=tenant)
            # No error if conversation doesn't exist yet — it's a new conversation

        # Normalize input to message list (agent and stored variants)
        input_messages: List[Dict[str, str]] = []
        storage_messages: List[Dict[str, str]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
            storage_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for item in raw_input:
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                    storage_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    raw_content = item.get("content", "")
                    if isinstance(raw_content, list):
                        try:
                            agent_content, stored_content = await self._normalize_response_content_parts(raw_content, tenant)
                        except _InputFileNormalizationError as exc:
                            return web.json_response(
                                _openai_error(exc.message, code=exc.code),
                                status=exc.status,
                            )
                    else:
                        agent_content = _normalize_chat_content(raw_content)
                        stored_content = agent_content
                    input_messages.append({"role": role, "content": agent_content})
                    storage_messages.append({"role": role, "content": stored_content})
                else:
                    return web.json_response(_openai_error("'input' array items must be strings or objects"), status=400)
        else:
            return web.json_response(_openai_error("'input' must be a string or array"), status=400)

        # Accept explicit conversation_history from the request body.
        # This lets stateless clients supply their own history instead of
        # relying on server-side response chaining via previous_response_id.
        # Precedence: explicit conversation_history > previous_response_id.
        agent_history: List[Dict[str, str]] = []
        storage_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                content_val = str(entry["content"])
                agent_history.append({"role": str(entry["role"]), "content": content_val})
                storage_history.append({"role": str(entry["role"]), "content": self._strip_local_paths(content_val)})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        if not agent_history and previous_response_id:
            stored = self._response_store.get(previous_response_id, user_id=tenant)
            if stored is None:
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            base_history = list(stored.get("conversation_history", []))
            agent_history = list(base_history)
            storage_history = list(base_history)
            # If no instructions provided, carry forward from previous
            if instructions is None:
                instructions = stored.get("instructions")

        # Append new input messages to history (all but the last become history)
        for idx, msg in enumerate(input_messages[:-1]):
            agent_history.append(msg)
            storage_history.append(storage_messages[idx])

        # Last input message is the user_message
        user_message = input_messages[-1].get("content", "") if input_messages else ""
        stored_user_message = storage_messages[-1].get("content", "") if storage_messages else ""
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        # Truncation support
        if body.get("truncation") == "auto":
            if len(agent_history) > 100:
                agent_history = agent_history[-100:]
            if len(storage_history) > 100:
                storage_history = storage_history[-100:]

        # Run the agent (with Idempotency-Key support)
        session_id = str(uuid.uuid4())

        async def _compute_response():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=agent_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                user_id=tenant,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(
                body,
                keys=["input", "instructions", "previous_response_id", "conversation", "model", "tools"],
            )
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_response)
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_response()
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())

        # Build output items first so the stored history can use the same
        # cleaned assistant text returned to the client.
        output_items, cleaned_final_response = self._extract_output_items(result, user_id=tenant)

        # Build the full conversation history for storage
        # (includes tool calls from the agent run)
        full_history = list(storage_history)
        full_history.append({"role": "user", "content": stored_user_message})
        # Add agent's internal messages if available
        agent_messages = result.get("messages", [])
        if agent_messages:
            sanitized_agent = self._sanitize_messages_for_storage(
                agent_messages,
                original_final=final_response,
                cleaned_final=cleaned_final_response,
            )
            sanitized_agent = self._sanitize_history_local_paths(sanitized_agent)
            full_history.extend(sanitized_agent)
        else:
            full_history.append({"role": "assistant", "content": cleaned_final_response})
        full_history = self._sanitize_history_local_paths(full_history)

        response_data = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "created_at": created_at,
            "model": body.get("model", self._model_name),
            "output": output_items,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        # Store the complete response object for future chaining / GET retrieval
        if store:
            self._response_store.put(response_id, {
                "response": response_data,
                "conversation_history": full_history,
                "instructions": instructions,
            }, user_id=tenant)
            # Update conversation mapping so the next request with the same
            # conversation name automatically chains to this response
            if conversation:
                self._response_store.set_conversation(conversation, response_id, user_id=tenant)

        return web.json_response(response_data)

    # ------------------------------------------------------------------
    # GET / DELETE response endpoints
    # ------------------------------------------------------------------

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} — retrieve a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            tenant = self._extract_tenant(request)
        except _TenantValidationError as exc:
            return web.json_response(_openai_error(str(exc), param="user", code="invalid_user"), status=400)

        response_id = request.match_info["response_id"]
        stored = self._response_store.get(response_id, user_id=tenant)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} — delete a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            tenant = self._extract_tenant(request)
        except _TenantValidationError as exc:
            return web.json_response(_openai_error(str(exc), param="user", code="invalid_user"), status=400)

        response_id = request.match_info["response_id"]
        deleted = self._response_store.delete(response_id, user_id=tenant)
        if not deleted:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    @staticmethod
    def _resolve_upload_mime_type(filename: str | None, provided: str | None) -> str:
        candidate = provided or ""
        if not candidate or candidate == "application/octet-stream":
            guessed = mimetypes.guess_type(filename or "")[0]
            candidate = guessed or candidate or "application/octet-stream"
        return candidate

    @staticmethod
    def _upload_type_allowed(filename: str | None, mime_type: str | None) -> bool:
        ext = (Path(filename).suffix.lower() if filename else "")
        if mime_type:
            if mime_type in SUPPORTED_UPLOAD_MIME_TYPES:
                return True
            if any(mime_type.startswith(prefix) for prefix in SUPPORTED_UPLOAD_MIME_PREFIXES):
                return True
        if ext in SUPPORTED_UPLOAD_EXTENSIONS:
            return True
        return False

    async def _parse_multipart_file_upload(self, request: "web.Request") -> tuple[str | None, str | None, str, str, bytes]:
        try:
            reader = await request.multipart()
        except Exception as exc:  # malformed payload
            raise _MultipartParseError("Invalid multipart payload") from exc

        filename: str | None = None
        purpose = "uploads"
        source_kind = "upload"
        mime_type: str | None = None
        data: bytes | None = None
        file_seen = False

        async for part in reader:
            if part.name == "file":
                file_seen = True
                if part.filename:
                    filename = part.filename
                if not mime_type:
                    mime_type = part.headers.get("Content-Type")
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_FILE_UPLOAD_BYTES:
                        raise _UploadTooLarge("Upload exceeds limit")
                    chunks.append(chunk)
                data = b"".join(chunks)
            elif part.name == "purpose":
                purpose = (await part.text()) or purpose
            elif part.name == "source":
                source_kind = (await part.text()) or source_kind
            elif part.name == "filename" and not filename:
                candidate = (await part.text()).strip()
                if candidate:
                    filename = candidate
            # Ignore other fields to remain compatible with OpenAI clients

        if not file_seen:
            raise _MultipartParseError("Missing file field")
        if data is None:
            raise _MultipartParseError("Missing file content")

        return filename, mime_type, purpose, source_kind, data

    def _serialize_file(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        """Shape file metadata for API responses."""
        return {
            "id": meta["file_id"],
            "object": "file",
            "bytes": meta["size_bytes"],
            "created_at": meta["created_at"],
            "filename": meta["filename"],
            "purpose": meta.get("purpose", "uploads"),
            "mime_type": meta.get("mime_type"),
            "source": meta.get("source"),
            "download_url": f"/v1/files/{meta['file_id']}/content",
        }

    async def _handle_create_file(self, request: "web.Request") -> "web.Response":
        """POST /v1/files — create a tenant-scoped file record and store content."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            tenant = self._extract_tenant(request)
        except _TenantValidationError as exc:
            return web.json_response(_openai_error(str(exc), param="user", code="invalid_user"), status=400)

        content_type = request.headers.get("Content-Type", "")
        ct_lower = content_type.lower()
        filename: str | None = None
        purpose = "uploads"
        mime_type: str | None = None
        source_kind = "upload"
        data: bytes | None = None

        if ct_lower.startswith("multipart/"):
            try:
                filename, mime_type, purpose, source_kind, data = await self._parse_multipart_file_upload(request)
            except _UploadTooLarge:
                return web.json_response(
                    _openai_error(f"File too large (max {MAX_FILE_UPLOAD_BYTES} bytes)", code="file_too_large"),
                    status=413,
                )
            except _MultipartParseError as exc:
                return web.json_response(
                    _openai_error(str(exc) or "Invalid multipart payload", code="invalid_request_error"),
                    status=400,
                )
        elif ct_lower.startswith("application/json"):
            try:
                body = await request.json()
            except (json.JSONDecodeError, Exception):
                return web.json_response(
                    _openai_error("Invalid JSON in request body", code="invalid_request_error"), status=400,
                )
            filename = body.get("filename")
            purpose = body.get("purpose") or purpose
            mime_type = body.get("mime_type")
            source_kind = body.get("source") or source_kind
            if "content_base64" in body:
                try:
                    data = base64.b64decode(body.get("content_base64") or "")
                except Exception:
                    return web.json_response(
                        _openai_error("Invalid base64 content", code="invalid_request_error"), status=400,
                    )
            elif "content" in body:
                data = str(body.get("content") or "").encode()
            else:
                data = None
        else:
            data = await request.read()
            filename = request.query.get("filename") or request.headers.get("X-Filename")
            purpose = request.query.get("purpose") or purpose
            mime_type = request.headers.get("Content-Type")

        if not data:
            return web.json_response(
                _openai_error("Missing file content", code="invalid_request_error"), status=400,
            )
        if len(data) > MAX_FILE_UPLOAD_BYTES:
            return web.json_response(
                _openai_error(f"File too large (max {MAX_FILE_UPLOAD_BYTES} bytes)", code="file_too_large"),
                status=413,
            )
        if not filename:
            return web.json_response(
                _openai_error("Missing filename", param="filename", code="invalid_request_error"), status=400,
            )

        resolved_mime = self._resolve_upload_mime_type(filename, mime_type)
        if not self._upload_type_allowed(filename, resolved_mime):
            return web.json_response(
                _openai_error(
                    "Unsupported file type. Only images and documents are allowed.",
                    code="unsupported_file_type",
                ),
                status=400,
            )

        try:
            meta = self._output_file_store.store_bytes(
                filename,
                data,
                user_id=tenant,
                purpose=purpose,
                mime_type=resolved_mime,
                source=source_kind,
            )
        except ValueError as exc:
            message = str(exc)
            status = 413 if "exceeds" in message.lower() else 400
            return web.json_response(_openai_error(message, code="invalid_request_error"), status=status)
        except Exception as exc:
            logger.error("Failed to store file: %s", exc, exc_info=True)
            return web.json_response(_openai_error("Failed to store file", err_type="server_error"), status=500)

        return web.json_response(self._serialize_file(meta), status=201)

    async def _handle_list_files(self, request: "web.Request") -> "web.Response":
        """GET /v1/files — list files for the tenant."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            tenant = self._extract_tenant(request)
        except _TenantValidationError as exc:
            return web.json_response(_openai_error(str(exc), param="user", code="invalid_user"), status=400)

        files = self._output_file_store.list(user_id=tenant)
        return web.json_response({"object": "list", "data": [self._serialize_file(f) for f in files]})

    async def _handle_get_file_metadata(self, request: "web.Request") -> "web.Response":
        """GET /v1/files/{file_id} — retrieve file metadata for the tenant."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            tenant = self._extract_tenant(request)
        except _TenantValidationError as exc:
            return web.json_response(_openai_error(str(exc), param="user", code="invalid_user"), status=400)

        file_id = request.match_info["file_id"]
        stored = self._output_file_store.get(file_id, user_id=tenant)
        if stored is None:
            return web.json_response(_openai_error(f"File not found: {file_id}", code="file_not_found"), status=404)

        return web.json_response(self._serialize_file(stored))

    async def _handle_get_file_content(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/files/{file_id}/content — download file content for the tenant."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            tenant = self._extract_tenant(request)
        except _TenantValidationError as exc:
            return web.json_response(_openai_error(str(exc), param="user", code="invalid_user"), status=400)

        file_id = request.match_info["file_id"]
        stored = self._output_file_store.get(file_id, user_id=tenant)
        if stored is None:
            return web.json_response(_openai_error(f"File not found: {file_id}", code="file_not_found"), status=404)

        return web.FileResponse(
            path=stored["path"],
            headers={"Content-Type": stored["mime_type"]},
        )

    # ------------------------------------------------------------------
    # Cron jobs API
    # ------------------------------------------------------------------

    # Check cron module availability once (not per-request)
    _CRON_AVAILABLE = False
    try:
        from cron.jobs import (
            list_jobs as _cron_list,
            get_job as _cron_get,
            create_job as _cron_create,
            update_job as _cron_update,
            remove_job as _cron_remove,
            pause_job as _cron_pause,
            resume_job as _cron_resume,
            trigger_job as _cron_trigger,
        )
        # Wrap as staticmethod to prevent descriptor binding — these are plain
        # module functions, not instance methods.  Without this, self._cron_*()
        # injects ``self`` as the first positional argument and every call
        # raises TypeError.
        _cron_list = staticmethod(_cron_list)
        _cron_get = staticmethod(_cron_get)
        _cron_create = staticmethod(_cron_create)
        _cron_update = staticmethod(_cron_update)
        _cron_remove = staticmethod(_cron_remove)
        _cron_pause = staticmethod(_cron_pause)
        _cron_resume = staticmethod(_cron_resume)
        _cron_trigger = staticmethod(_cron_trigger)
        _CRON_AVAILABLE = True
    except ImportError:
        pass

    _JOB_ID_RE = __import__("re").compile(r"[a-f0-9]{12}")
    # Allowed fields for update — prevents clients injecting arbitrary keys
    _UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    _MAX_NAME_LENGTH = 200
    _MAX_PROMPT_LENGTH = 5000

    def _check_jobs_available(self) -> Optional["web.Response"]:
        """Return error response if cron module isn't available."""
        if not self._CRON_AVAILABLE:
            return web.json_response(
                {"error": "Cron module not available"}, status=501,
            )
        return None

    def _check_job_id(self, request: "web.Request") -> tuple:
        """Validate and extract job_id. Returns (job_id, error_response)."""
        job_id = request.match_info["job_id"]
        if not self._JOB_ID_RE.fullmatch(job_id):
            return job_id, web.json_response(
                {"error": "Invalid job ID format"}, status=400,
            )
        return job_id, None

    async def _handle_list_jobs(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs — list all cron jobs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            include_disabled = request.query.get("include_disabled", "").lower() in ("true", "1")
            jobs = self._cron_list(include_disabled=include_disabled)
            return web.json_response({"jobs": jobs})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs — create a new cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            schedule = (body.get("schedule") or "").strip()
            prompt = body.get("prompt", "")
            deliver = body.get("deliver", "local")
            skills = body.get("skills")
            repeat = body.get("repeat")

            if not name:
                return web.json_response({"error": "Name is required"}, status=400)
            if len(name) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if not schedule:
                return web.json_response({"error": "Schedule is required"}, status=400)
            if len(prompt) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
                return web.json_response({"error": "Repeat must be a positive integer"}, status=400)

            kwargs = {
                "prompt": prompt,
                "schedule": schedule,
                "name": name,
                "deliver": deliver,
            }
            if skills:
                kwargs["skills"] = skills
            if repeat is not None:
                kwargs["repeat"] = repeat

            job = self._cron_create(**kwargs)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_job(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs/{job_id} — get a single cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update_job(self, request: "web.Request") -> "web.Response":
        """PATCH /api/jobs/{job_id} — update a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            body = await request.json()
            # Whitelist allowed fields to prevent arbitrary key injection
            sanitized = {k: v for k, v in body.items() if k in self._UPDATE_ALLOWED_FIELDS}
            if not sanitized:
                return web.json_response({"error": "No valid fields to update"}, status=400)
            # Validate lengths if present
            if "name" in sanitized and len(sanitized["name"]) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if "prompt" in sanitized and len(sanitized["prompt"]) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            job = self._cron_update(job_id, sanitized)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_job(self, request: "web.Request") -> "web.Response":
        """DELETE /api/jobs/{job_id} — delete a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            success = self._cron_remove(job_id)
            if not success:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pause_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/pause — pause a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_pause(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_resume_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/resume — resume a paused cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_resume(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_run_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/run — trigger immediate execution."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_trigger(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ------------------------------------------------------------------
    # Output extraction helper
    # ------------------------------------------------------------------

    _EXPLICIT_OUTPUT_FILE_RE = re.compile(
        r'(?m)^[ \t]*(?P<label>MEDIA:|FILE:|📎 File:|🖼️ Image:|🎬 Video:|🔊 Audio:)[ \t]*(?P<path>`[^`\n]+`|"[^"\n]+"|\'[^\'\n]+\'|(?:~/|/).+?)[ \t]*$'
    )

    @staticmethod
    def _normalize_output_path(path: str) -> str:
        candidate = (path or "").strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "`\"'":
            candidate = candidate[1:-1].strip()
        return candidate.lstrip("`\"'").rstrip("`\"',.;:)}]")

    def _store_output_artifacts(
        self,
        final_text: str,
        *,
        user_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Extract downloadable files from assistant text and copy them into tenant storage."""
        text = final_text or ""
        output_files: list[dict[str, Any]] = []
        seen_paths: set[str] = set()

        def _append_file(path_text: str) -> None:
            candidate = self._normalize_output_path(path_text)
            if not candidate:
                return
            expanded = os.path.expanduser(candidate)
            if expanded in seen_paths:
                return
            try:
                meta = self._output_file_store.put_from_path(
                    candidate,
                    user_id=user_id,
                    purpose="output",
                    source="assistant_output",
                )
            except Exception as exc:
                logger.warning("[api_server] failed to stage output artifact %s: %s", candidate, exc)
                return
            seen_paths.add(expanded)
            output_files.append({
                "type": "output_file",
                "file_id": meta["file_id"],
                "filename": meta["filename"],
                "mime_type": meta["mime_type"],
                "size_bytes": meta["size_bytes"],
                "download_url": f"/v1/files/{meta['file_id']}/content",
            })

        successful_spans: list[tuple[int, int]] = []
        for match in self._EXPLICIT_OUTPUT_FILE_RE.finditer(text):
            before = len(output_files)
            _append_file(match.group("path"))
            if len(output_files) > before:
                successful_spans.append(match.span())

        if successful_spans:
            rebuilt: list[str] = []
            cursor = 0
            for start, end in successful_spans:
                rebuilt.append(text[cursor:start])
                cursor = end
            rebuilt.append(text[cursor:])
            text = "".join(rebuilt)

        local_paths, cleaned_text = BasePlatformAdapter.extract_local_files(text)
        for local_path in local_paths:
            _append_file(local_path)
        if local_paths:
            text = cleaned_text

        text = text.replace("[[audio_as_voice]]", "")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return output_files, text

    @staticmethod
    def _sanitize_messages_for_storage(
        messages: List[Dict[str, Any]],
        *,
        original_final: str,
        cleaned_final: str,
    ) -> List[Dict[str, Any]]:
        if not messages:
            return []
        sanitized = [dict(msg) for msg in messages]
        for idx in range(len(sanitized) - 1, -1, -1):
            msg = sanitized[idx]
            if msg.get("role") != "assistant":
                continue
            if idx == len(sanitized) - 1 or msg.get("content") == original_final:
                msg["content"] = cleaned_final
                break
        return sanitized

    def _extract_output_items(
        self,
        result: Dict[str, Any],
        *,
        user_id: str | None = None,
    ) -> tuple[List[Dict[str, Any]], str]:
        """
        Build the full output item array from the agent's messages.

        Walks *result["messages"]* and emits:
        - ``function_call`` items for each tool_call on assistant messages
        - ``function_call_output`` items for each tool-role message
        - any extracted ``output_file`` items backed by tenant-scoped copies
        - a final ``message`` item with the assistant's text reply
        """
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])

        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", ""),
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        final = result.get("final_response", "")
        if not final:
            final = result.get("error", "(No response generated)")

        output_files, cleaned_final = self._store_output_artifacts(final, user_id=user_id)
        items.extend(output_files)
        items.append({
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": cleaned_final,
                }
            ],
        })
        return items, cleaned_final

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        agent_ref: Optional[list] = None,
    ) -> tuple:
        """
        Create an agent and run a conversation in a thread executor.

        Returns ``(result_dict, usage_dict)`` where *usage_dict* contains
        ``input_tokens``, ``output_tokens`` and ``total_tokens``.

        If *agent_ref* is a one-element list, the AIAgent instance is stored
        at ``agent_ref[0]`` before ``run_conversation`` begins.  This allows
        callers (e.g. the SSE writer) to call ``agent.interrupt()`` from
        another thread to stop in-progress LLM calls.
        """
        loop = asyncio.get_event_loop()

        def _run():
            agent = self._create_agent(
                ephemeral_system_prompt=ephemeral_system_prompt,
                session_id=session_id,
                stream_delta_callback=stream_delta_callback,
                tool_progress_callback=tool_progress_callback,
                user_id=user_id,
            )
            if agent_ref is not None:
                agent_ref[0] = agent
            result = agent.run_conversation(
                user_message=user_message,
                conversation_history=conversation_history,
                task_id="default",
            )
            usage = {
                "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
            }
            return result, usage

        return await loop.run_in_executor(None, _run)

    # ------------------------------------------------------------------
    # /v1/runs — structured event streaming
    # ------------------------------------------------------------------

    _MAX_CONCURRENT_RUNS = 10  # Prevent unbounded resource allocation
    _RUN_STREAM_TTL = 300  # seconds before orphaned runs are swept

    def _make_run_event_callback(self, run_id: str, loop: "asyncio.AbstractEventLoop"):
        """Return a tool_progress_callback that pushes structured events to the run's SSE queue."""
        def _push(event: Dict[str, Any]) -> None:
            q = self._run_streams.get(run_id)
            if q is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "preview": preview,
                })
            elif event_type == "tool.completed":
                _push({
                    "event": "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": round(kwargs.get("duration", 0), 3),
                    "error": kwargs.get("is_error", False),
                })
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            # _thinking and subagent_progress are intentionally not forwarded

        return _callback

    async def _handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs — start an agent run, return run_id immediately."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Enforce concurrency limit
        if len(self._run_streams) >= self._MAX_CONCURRENT_RUNS:
            return web.json_response(
                _openai_error(f"Too many concurrent runs (max {self._MAX_CONCURRENT_RUNS})", code="rate_limit_exceeded"),
                status=429,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        try:
            tenant = self._extract_tenant(request, body)
        except _TenantValidationError as exc:
            return web.json_response(_openai_error(str(exc), param="user", code="invalid_user"), status=400)

        raw_input = body.get("input")
        if not raw_input:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        user_message = raw_input if isinstance(raw_input, str) else (raw_input[-1].get("content", "") if isinstance(raw_input, list) else "")
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        run_id = f"run_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        self._run_streams[run_id] = q
        self._run_streams_created[run_id] = time.time()

        event_cb = self._make_run_event_callback(run_id, loop)

        # Also wire stream_delta_callback so message.delta events flow through
        def _text_cb(delta: Optional[str]) -> None:
            if delta is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        # Accept explicit conversation_history from the request body.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id, user_id=tenant)
            if stored:
                conversation_history = list(stored.get("conversation_history", []))
                if instructions is None:
                    instructions = stored.get("instructions")

        # When input is a multi-message array, extract all but the last
        # message as conversation history (the last becomes user_message).
        # Only fires when no explicit history was provided.
        if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
            for msg in raw_input[:-1]:
                if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list):
                        # Flatten multi-part content blocks to text
                        content = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                    conversation_history.append({"role": msg["role"], "content": str(content)})

        session_id = body.get("session_id") or run_id
        ephemeral_system_prompt = instructions

        async def _run_and_close():
            try:
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                    user_id=tenant,
                )
                def _run_sync():
                    r = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=conversation_history,
                        task_id="default",
                    )
                    u = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                output_files, cleaned_final_response = self._store_output_artifacts(final_response, user_id=tenant)
                q.put_nowait({
                    "event": "run.completed",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "output": cleaned_final_response,
                    "files": output_files,
                    "usage": usage,
                })
            except Exception as exc:
                logger.exception("[api_server] run %s failed", run_id)
                try:
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": str(exc),
                    })
                except Exception:
                    pass
            finally:
                # Sentinel: signal SSE stream to close
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

        task = asyncio.create_task(_run_and_close())
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        return web.json_response({"run_id": run_id, "status": "started"}, status=202)

    async def _handle_run_events(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/runs/{run_id}/events — SSE stream of structured agent lifecycle events."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]

        # Allow subscribing slightly before the run is registered (race condition window)
        for _ in range(20):
            if run_id in self._run_streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        q = self._run_streams[run_id]

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    # Run finished — send final SSE comment and close
                    await response.write(b": stream closed\n\n")
                    break
                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except Exception as exc:
            logger.debug("[api_server] SSE stream error for run %s: %s", run_id, exc)
        finally:
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)

        return response

    async def _sweep_orphaned_runs(self) -> None:
        """Periodically clean up run streams that were never consumed."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            stale = [
                run_id
                for run_id, created_at in list(self._run_streams_created.items())
                if now - created_at > self._RUN_STREAM_TTL
            ]
            for run_id in stale:
                logger.debug("[api_server] sweeping orphaned run %s", run_id)
                self._run_streams.pop(run_id, None)
                self._run_streams_created.pop(run_id, None)

    # ------------------------------------------------------------------
    # BasePlatformAdapter interface
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the aiohttp web server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        try:
            mws = [mw for mw in (cors_middleware, body_limit_middleware, security_headers_middleware) if mw is not None]
            self._app = web.Application(middlewares=mws)
            self._app["api_server_adapter"] = self
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get("/v1/health", self._handle_health)
            self._app.router.add_get("/v1/models", self._handle_models)
            self._app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
            self._app.router.add_post("/v1/responses", self._handle_responses)
            self._app.router.add_get("/v1/responses/{response_id}", self._handle_get_response)
            self._app.router.add_delete("/v1/responses/{response_id}", self._handle_delete_response)
            self._app.router.add_post("/v1/files", self._handle_create_file)
            self._app.router.add_get("/v1/files", self._handle_list_files)
            self._app.router.add_get("/v1/files/{file_id}", self._handle_get_file_metadata)
            self._app.router.add_get("/v1/files/{file_id}/content", self._handle_get_file_content)
            # Cron jobs management API
            self._app.router.add_get("/api/jobs", self._handle_list_jobs)
            self._app.router.add_post("/api/jobs", self._handle_create_job)
            self._app.router.add_get("/api/jobs/{job_id}", self._handle_get_job)
            self._app.router.add_patch("/api/jobs/{job_id}", self._handle_update_job)
            self._app.router.add_delete("/api/jobs/{job_id}", self._handle_delete_job)
            self._app.router.add_post("/api/jobs/{job_id}/pause", self._handle_pause_job)
            self._app.router.add_post("/api/jobs/{job_id}/resume", self._handle_resume_job)
            self._app.router.add_post("/api/jobs/{job_id}/run", self._handle_run_job)
            # Structured event streaming
            self._app.router.add_post("/v1/runs", self._handle_runs)
            self._app.router.add_get("/v1/runs/{run_id}/events", self._handle_run_events)
            # Start background sweep to clean up orphaned (unconsumed) run streams
            sweep_task = asyncio.create_task(self._sweep_orphaned_runs())
            try:
                self._background_tasks.add(sweep_task)
            except TypeError:
                pass
            if hasattr(sweep_task, "add_done_callback"):
                sweep_task.add_done_callback(self._background_tasks.discard)

            # Refuse to start network-accessible without authentication
            if is_network_accessible(self._host) and not self._api_key:
                logger.error(
                    "[%s] Refusing to start: binding to %s requires API_SERVER_KEY. "
                    "Set API_SERVER_KEY or use the default 127.0.0.1.",
                    self.name, self._host,
                )
                return False

            # Refuse to start network-accessible with a placeholder key.
            # Ported from openclaw/openclaw#64586.
            if is_network_accessible(self._host) and self._api_key:
                try:
                    from hermes_cli.auth import has_usable_secret
                    if not has_usable_secret(self._api_key, min_length=8):
                        logger.error(
                            "[%s] Refusing to start: API_SERVER_KEY is set to a "
                            "placeholder value. Generate a real secret "
                            "(e.g. `openssl rand -hex 32`) and set API_SERVER_KEY "
                            "before exposing the API server on %s.",
                            self.name, self._host,
                        )
                        return False
                except ImportError:
                    pass

            # Port conflict detection — fail fast if port is already in use
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                    _s.settimeout(1)
                    _s.connect(('127.0.0.1', self._port))
                logger.error('[%s] Port %d already in use. Set a different port in config.yaml: platforms.api_server.port', self.name, self._port)
                return False
            except (ConnectionRefusedError, OSError):
                pass  # port is free

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            self._mark_connected()
            if not self._api_key:
                logger.warning(
                    "[%s] ⚠️  No API key configured (API_SERVER_KEY / platforms.api_server.key). "
                    "All requests will be accepted without authentication. "
                    "Set an API key for production deployments to prevent "
                    "unauthorized access to sessions, responses, and cron jobs.",
                    self.name,
                )
            logger.info(
                "[%s] API server listening on http://%s:%d (model: %s)",
                self.name, self._host, self._port, self._model_name,
            )
            return True

        except Exception as e:
            logger.error("[%s] Failed to start API server: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        """Stop the aiohttp web server."""
        self._mark_disconnected()
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._response_store.close()
        self._output_file_store.close()
        self._app = None
        logger.info("[%s] API server stopped", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Not used — HTTP request/response cycle handles delivery directly.
        """
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the API server."""
        return {
            "name": "API Server",
            "type": "api",
            "host": self._host,
            "port": self._port,
        }
