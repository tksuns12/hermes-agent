"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        — OpenAI Chat Completions format (stateless; opt-in session continuity via X-Hermes-Session-Id header; opt-in long-term memory scoping via X-Hermes-Session-Key header)
- POST /v1/responses               — OpenAI Responses API format (stateful via previous_response_id; X-Hermes-Session-Key supported)
- GET  /v1/responses/{response_id} — Retrieve a stored response
- DELETE /v1/responses/{response_id} — Delete a stored response
- GET  /v1/models                  — lists hermes-agent as an available model
- GET  /v1/capabilities            — machine-readable API capabilities for external UIs
- POST /v1/runs                    — start a run, returns run_id immediately (202)
- GET  /v1/runs/{run_id}           — retrieve current run status
- GET  /v1/runs/{run_id}/events    — SSE stream of structured lifecycle events
- POST /v1/runs/{run_id}/stop    — interrupt a running agent
- GET  /health                     — health check
- GET  /health/detailed            — rich status for cross-container dashboard probing

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
MAX_REQUEST_BYTES = 10_000_000  # 10 MB — accommodates long conversations with tool calls/uploads
MAX_OUTPUT_FILE_BYTES = 50 * 1024 * 1024  # 50 MB cap for copied response artifacts
MAX_FILE_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB cap for direct uploads
MAX_INPUT_FILES_PER_REQUEST = 20  # Cap number of input_file references per request
CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0
MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 64 KB cap for normalized content parts
MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array

SUPPORTED_UPLOAD_MIME_PREFIXES = ("image/", "text/")
SUPPORTED_UPLOAD_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
SUPPORTED_UPLOAD_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rtf",
    ".pdf",
    ".docx",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".heic",
}


def _coerce_port(value: Any, default: int = DEFAULT_PORT) -> int:
    """Parse a listen port without letting malformed env/config values crash startup."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


# Content part type aliases used by the OpenAI Chat Completions and Responses
# APIs.  We accept both spellings on input and emit a single canonical internal
# shape (``{"type": "text", ...}`` / ``{"type": "image_url", ...}``) that the
# rest of the agent pipeline already understands.
_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})
_FILE_PART_TYPES = frozenset({"file", "input_file"})


def _normalize_multimodal_content(content: Any) -> Any:
    """Validate and normalize multimodal content for the API server.

    Returns a plain string when the content is text-only, or a list of
    ``{"type": "text"|"image_url", ...}`` parts when images are present.
    The output shape is the native OpenAI Chat Completions vision format,
    which the agent pipeline accepts verbatim (OpenAI-wire providers) or
    converts (``_preprocess_anthropic_content`` for Anthropic).

    Raises ``ValueError`` with an OpenAI-style code on invalid input:
      * ``unsupported_content_type`` — file/input_file/file_id parts, or
        non-image ``data:`` URLs.
      * ``invalid_image_url`` — missing URL or unsupported scheme.
      * ``invalid_content_part`` — malformed text/image objects.

    Callers translate the ValueError into a 400 response.
    """
    # Scalar passthrough mirrors ``_normalize_chat_content``.
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content
    if not isinstance(content, list):
        # Mirror the legacy text-normalizer's fallback so callers that
        # pre-existed image support still get a string back.
        return _normalize_chat_content(content)

    items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
    normalized_parts: List[Dict[str, Any]] = []
    text_accum_len = 0

    for part in items:
        if isinstance(part, str):
            if part:
                trimmed = part[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if not isinstance(part, dict):
            # Ignore unknown scalars for forward compatibility with future
            # Responses API additions (e.g. ``refusal``).  The same policy
            # the text normalizer applies.
            continue

        raw_type = part.get("type")
        part_type = str(raw_type or "").strip().lower()

        if part_type in _TEXT_PART_TYPES:
            text = part.get("text")
            if text is None:
                continue
            if not isinstance(text, str):
                text = str(text)
            if text:
                trimmed = text[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if part_type in _IMAGE_PART_TYPES:
            detail = part.get("detail")
            image_ref = part.get("image_url")
            # OpenAI Responses sends ``input_image`` with a top-level
            # ``image_url`` string; Chat Completions sends ``image_url`` as
            # ``{"url": "...", "detail": "..."}``.  Support both.
            if isinstance(image_ref, dict):
                url_value = image_ref.get("url")
                detail = image_ref.get("detail", detail)
            else:
                url_value = image_ref
            if not isinstance(url_value, str) or not url_value.strip():
                raise ValueError("invalid_image_url:Image parts must include a non-empty image URL.")
            url_value = url_value.strip()
            lowered = url_value.lower()
            if lowered.startswith("data:"):
                if not lowered.startswith("data:image/") or "," not in url_value:
                    raise ValueError(
                        "unsupported_content_type:Only image data URLs are supported. "
                        "Non-image data payloads are not supported."
                    )
            elif not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise ValueError(
                    "invalid_image_url:Image inputs must use http(s) URLs or data:image/... URLs."
                )
            image_part: Dict[str, Any] = {"type": "image_url", "image_url": {"url": url_value}}
            if detail is not None:
                if not isinstance(detail, str) or not detail.strip():
                    raise ValueError("invalid_content_part:Image detail must be a non-empty string when provided.")
                image_part["image_url"]["detail"] = detail.strip()
            normalized_parts.append(image_part)
            continue

        if part_type in _FILE_PART_TYPES:
            raise ValueError(
                "unsupported_content_type:Inline image inputs are supported, "
                "but uploaded files and document inputs are not supported on this endpoint."
            )

        # Unknown part type — reject explicitly so clients get a clear error
        # instead of a silently dropped turn.
        raise ValueError(
            f"unsupported_content_type:Unsupported content part type {raw_type!r}. "
            "Only text and image_url/input_image parts are supported."
        )

    if not normalized_parts:
        return ""

    # Text-only: collapse to a plain string so downstream logging/trajectory
    # code sees the native shape and prompt caching on text-only turns is
    # unaffected.
    if all(p.get("type") == "text" for p in normalized_parts):
        return "\n".join(p["text"] for p in normalized_parts if p.get("text"))

    return normalized_parts


def _content_has_visible_payload(content: Any) -> bool:
    """True when content has any text or image attachment.  Used to reject empty turns."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = str(part.get("type") or "").strip().lower()
                if ptype in _TEXT_PART_TYPES and str(part.get("text") or "").strip():
                    return True
                if ptype in _IMAGE_PART_TYPES:
                    return True
    return False


def _multimodal_validation_error(exc: ValueError, *, param: str) -> "web.Response":
    """Translate a ``_normalize_multimodal_content`` ValueError into a 400 response."""
    raw = str(exc)
    code, _, message = raw.partition(":")
    if not message:
        code, message = "invalid_content_part", raw
    return web.json_response(
        _openai_error(message, code=code, param=param),
        status=400,
    )


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
                source_run_id TEXT,
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
        if "source_run_id" not in cols:
            self._conn.execute(
                "ALTER TABLE output_files ADD COLUMN source_run_id TEXT"
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
                "source_run_id": meta.get("source_run_id"),
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
        source_run_id: str | None = None,
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
            "source_run_id": source_run_id,
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
        source_run_id: str | None = None,
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
            "(user_id, file_id, storage_name, filename, mime_type, size_bytes, created_at, purpose, source, source_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                source_run_id,
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
            source_run_id=source_run_id,
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
        source_run_id: str | None = None,
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
            "(user_id, file_id, storage_name, filename, mime_type, size_bytes, created_at, purpose, source, source_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                source_run_id,
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
            source_run_id=source_run_id,
        )
        self._write_metadata_file(meta, tenant, target_dir=target_dir)
        return meta

    def list(self, user_id: str | None = None) -> List[Dict[str, Any]]:
        tenant = self._tenant(user_id)
        rows = self._conn.execute(
            "SELECT file_id, storage_name, filename, mime_type, size_bytes, created_at, purpose, source, source_run_id "
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
                    source_run_id=row["source_run_id"],
                )
            )
        return results

    def get(self, file_id: str, user_id: str | None = None) -> Optional[Dict[str, Any]]:
        tenant = self._tenant(user_id)
        row = self._conn.execute(
            "SELECT storage_name, filename, mime_type, size_bytes, created_at, purpose, source, source_run_id "
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
            source_run_id=row["source_run_id"],
        )

    def delete(self, file_id: str, user_id: str | None = None) -> bool:
        tenant = self._tenant(user_id)
        row = self._conn.execute(
            "SELECT storage_name FROM output_files WHERE user_id = ? AND file_id = ?",
            (tenant, file_id),
        ).fetchone()
        if row is None:
            return False
        stored_path = get_user_subpath(tenant, "api_server", "files", row["storage_name"])
        try:
            if stored_path.exists():
                stored_path.unlink()
        except Exception:
            logger.debug("[api_server] failed to remove stored file %s", stored_path, exc_info=True)
        self._conn.execute(
            "DELETE FROM output_files WHERE user_id = ? AND file_id = ?",
            (tenant, file_id),
        )
        self._conn.commit()
        return True

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


class _OutputArtifactError(Exception):
    """Raised when assistant output artifacts cannot be staged safely."""

    def __init__(self, message: str, *, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or "invalid_request_error"


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
        self._inflight: Dict[tuple[str, str], "asyncio.Task[Any]"] = {}
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        now = time.time()
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

        inflight_key = (key, fingerprint)
        task = self._inflight.get(inflight_key)
        if task is None:
            async def _compute_and_store():
                resp = await compute_coro()
                import time as _t
                self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
                self._purge()
                return resp

            task = asyncio.create_task(_compute_and_store())
            self._inflight[inflight_key] = task

            def _clear_inflight(done_task: "asyncio.Task[Any]") -> None:
                if self._inflight.get(inflight_key) is done_task:
                    self._inflight.pop(inflight_key, None)

            task.add_done_callback(_clear_inflight)

        return await asyncio.shield(task)


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
    _CRON_AVAILABLE = True
except ImportError:
    _cron_list = None
    _cron_get = None
    _cron_create = None
    _cron_update = None
    _cron_remove = None
    _cron_pause = None
    _cron_resume = None
    _cron_trigger = None


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
        raw_port = extra.get("port")
        if raw_port is None:
            raw_port = os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))
        self._port: int = _coerce_port(raw_port, DEFAULT_PORT)
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
        # Active run agent/task references for stop support
        self._active_run_agents: Dict[str, Any] = {}
        self._active_run_tasks: Dict[str, "asyncio.Task"] = {}
        # Pollable run status for dashboards and external control-plane UIs.
        self._run_statuses: Dict[str, Dict[str, Any]] = {}
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
    # Session header helpers
    # ------------------------------------------------------------------

    # Soft length cap for session identifiers.  Headers are bounded in
    # aggregate by aiohttp (``client_max_size`` / default 8 KiB per
    # header), but we impose a tighter limit on the session headers so a
    # caller can't burn memory by passing a multi-kilobyte "session key".
    # 256 chars is well above any realistic stable channel identifier
    # (e.g. ``agent:main:webui:dm:user-42``) while staying small enough
    # that the sanitized form is safe to pass into Honcho / state.db.
    _MAX_SESSION_HEADER_LEN = 256

    def _parse_session_key_header(
        self, request: "web.Request"
    ) -> tuple[Optional[str], Optional["web.Response"]]:
        """Extract and validate the ``X-Hermes-Session-Key`` header.

        The session key is a stable per-channel identifier that scopes
        long-term memory (e.g. Honcho sessions) across transcripts.  It
        is independent of ``X-Hermes-Session-Id``: callers may send
        either, both, or neither.

        Returns ``(session_key, None)`` on success (with an empty/absent
        header yielding ``None`` for the key), or ``(None, error_response)``
        on validation failure.

        Security: like session continuation, accepting a caller-supplied
        memory scope requires API-key authentication so that an
        unauthenticated client on a local-only server can't inject itself
        into another user's long-term memory scope by guessing a key.
        """
        raw = request.headers.get("X-Hermes-Session-Key", "").strip()
        if not raw:
            return None, None

        if not self._api_key:
            logger.warning(
                "X-Hermes-Session-Key rejected: no API key configured. "
                "Set API_SERVER_KEY to enable long-term memory scoping."
            )
            return None, web.json_response(
                _openai_error(
                    "X-Hermes-Session-Key requires API key authentication. "
                    "Configure API_SERVER_KEY to enable this feature."
                ),
                status=403,
            )

        # Reject control characters that could enable header injection on
        # the echo path.
        if re.search(r'[\r\n\x00]', raw):
            return None, web.json_response(
                {"error": {"message": "Invalid session key", "type": "invalid_request_error"}},
                status=400,
            )

        if len(raw) > self._MAX_SESSION_HEADER_LEN:
            return None, web.json_response(
                {"error": {"message": "Session key too long", "type": "invalid_request_error"}},
                status=400,
            )

        return raw, None

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
        tool_start_callback=None,
        tool_complete_callback=None,
        gateway_session_key: Optional[str] = None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.  Toolsets are resolved
        from config.yaml platform_toolsets.api_server (same as all other
        gateway platforms), falling back to the hermes-api-server default.

        ``gateway_session_key`` is a stable per-channel identifier supplied
        by the client (via ``X-Hermes-Session-Key``).  Unlike ``session_id``
        which scopes the short-term transcript and rotates on /new, this
        key is meant to persist across transcripts so long-term memory
        providers (e.g. Honcho) can scope their per-chat state correctly
        — matching the semantics of the native gateway's ``session_key``.
        """
        from run_agent import AIAgent
        from gateway.run import _resolve_runtime_agent_kwargs, _resolve_gateway_model, _load_gateway_config, GatewayRunner
        from hermes_cli.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        reasoning_config = GatewayRunner._load_reasoning_config()
        model = _resolve_gateway_model()

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))

        max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))

        # Load fallback provider chain so the API server platform has the
        # same fallback behaviour as Telegram/Discord/Slack (fixes #4954).
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
            tool_start_callback=tool_start_callback,
            tool_complete_callback=tool_complete_callback,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
            user_id=user_id,
            reasoning_config=reasoning_config,
            gateway_session_key=gateway_session_key,
        )
        return agent

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "hermes-agent"})

    async def _handle_health_detailed(self, request: "web.Request") -> "web.Response":
        """GET /health/detailed — rich status for cross-container dashboard probing.

        Returns gateway state, connected platforms, PID, and uptime so the
        dashboard can display full status without needing a shared PID file or
        /proc access.  No authentication required.
        """
        from gateway.status import read_runtime_status

        runtime = read_runtime_status() or {}
        return web.json_response({
            "status": "ok",
            "platform": "hermes-agent",
            "gateway_state": runtime.get("gateway_state"),
            "platforms": runtime.get("platforms", {}),
            "active_agents": runtime.get("active_agents", 0),
            "exit_reason": runtime.get("exit_reason"),
            "updated_at": runtime.get("updated_at"),
            "pid": os.getpid(),
        })

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

    async def _handle_capabilities(self, request: "web.Request") -> "web.Response":
        """GET /v1/capabilities — advertise the stable API surface.

        External UIs and orchestrators use this endpoint to discover the API
        server's plugin-safe contract without scraping docs or assuming that
        every Hermes version exposes the same endpoints.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "hermes.api_server.capabilities",
            "platform": "hermes-agent",
            "model": self._model_name,
            "auth": {
                "type": "bearer",
                "required": bool(self._api_key),
            },
            "features": {
                "chat_completions": True,
                "chat_completions_streaming": True,
                "responses_api": True,
                "responses_streaming": True,
                "run_submission": True,
                "run_status": True,
                "run_events_sse": True,
                "run_stop": True,
                "tool_progress_events": True,
                "session_continuity_header": "X-Hermes-Session-Id",
                "session_key_header": "X-Hermes-Session-Key",
                "cors": bool(self._cors_origins),
            },
            "endpoints": {
                "health": {"method": "GET", "path": "/health"},
                "health_detailed": {"method": "GET", "path": "/health/detailed"},
                "models": {"method": "GET", "path": "/v1/models"},
                "chat_completions": {"method": "POST", "path": "/v1/chat/completions"},
                "responses": {"method": "POST", "path": "/v1/responses"},
                "runs": {"method": "POST", "path": "/v1/runs"},
                "run_status": {"method": "GET", "path": "/v1/runs/{run_id}"},
                "run_events": {"method": "GET", "path": "/v1/runs/{run_id}/events"},
                "run_stop": {"method": "POST", "path": "/v1/runs/{run_id}/stop"},
            },
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
        file_counter = [0]

        for idx, msg in enumerate(messages):
            role = msg.get("role", "")
            raw_content = msg.get("content", "")
            if role == "system":
                # System messages don't support images (Anthropic rejects, OpenAI
                # text-model systems don't render them).  Flatten to text.
                content = _normalize_chat_content(raw_content)
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in ("user", "assistant"):
                try:
                    if role == "user" and isinstance(raw_content, list):
                        has_file_parts = any(
                            isinstance(part, dict)
                            and str(part.get("type") or "").strip().lower() in _FILE_PART_TYPES
                            for part in raw_content
                        )
                        if has_file_parts:
                            content = await self._normalize_chat_user_content(
                                raw_content,
                                tenant,
                                file_counter=file_counter,
                            )
                        else:
                            content = _normalize_multimodal_content(raw_content)
                    else:
                        content = _normalize_multimodal_content(raw_content)
                except _InputFileNormalizationError as exc:
                    return web.json_response(
                        _openai_error(exc.message, code=exc.code),
                        status=exc.status,
                    )
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"messages[{idx}].content")
                conversation_messages.append({"role": role, "content": content})

        # Extract the last user message as the primary input
        user_message: Any = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not _content_has_visible_payload(user_message):
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        # Allow caller to scope long-term memory (e.g. Honcho) with a
        # stable per-channel identifier via X-Hermes-Session-Key.  This
        # is independent of X-Hermes-Session-Id: the key persists across
        # transcripts while the id rotates when the caller starts a new
        # transcript (i.e. /new semantics).  See _parse_session_key_header.
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

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
            history = []
            try:
                db = self._ensure_session_db()
                if db is not None:
                    history = db.get_messages_as_conversation(session_id, user_id=tenant) or []
                    history = self._sanitize_history_local_paths(history)
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

            # Track which tool_call_ids we've emitted a "running" lifecycle
            # event for, so a "completed" event without a matching "running"
            # (e.g. internal/filtered tools) is silently dropped instead of
            # producing an orphaned event clients can't correlate.
            _started_tool_call_ids: set[str] = set()

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Emit ``hermes.tool.progress`` with ``status: running``.

                Replaces the old ``tool_progress_callback("tool.started",
                ...)`` emit so SSE consumers receive a single event per
                tool start, carrying both the legacy ``tool``/``emoji``/
                ``label`` payload (for #6972 frontends) and the new
                ``toolCallId``/``status`` correlation fields (#16588).

                Skips tools whose names start with ``_`` so internal
                events (``_thinking``, …) stay off the wire — matching
                the prior ``_on_tool_progress`` filter exactly.
                """
                if not tool_call_id or function_name.startswith("_"):
                    return
                _started_tool_call_ids.add(tool_call_id)
                from agent.display import build_tool_preview, get_tool_emoji
                label = build_tool_preview(function_name, function_args) or function_name
                _stream_q.put(("__tool_progress__", {
                    "tool": function_name,
                    "emoji": get_tool_emoji(function_name),
                    "label": label,
                    "toolCallId": tool_call_id,
                    "status": "running",
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Emit the matching ``status: completed`` event.

                Dropped if the start was filtered (internal tool, missing
                id, or never seen) so clients never get an orphaned
                ``completed`` they can't correlate to a prior ``running``.
                """
                if not tool_call_id or tool_call_id not in _started_tool_call_ids:
                    return
                _started_tool_call_ids.discard(tool_call_id)
                _stream_q.put(("__tool_progress__", {
                    "tool": function_name,
                    "toolCallId": tool_call_id,
                    "status": "completed",
                }))

            # Start agent in background.  agent_ref is a mutable container
            # so the SSE writer can interrupt the agent on client disconnect.
            #
            # ``tool_progress_callback`` is intentionally not wired here:
            # it would duplicate every emit because ``run_agent`` fires it
            # side-by-side with ``tool_start_callback``/``tool_complete_callback``.
            # The structured callbacks are strictly richer (they carry the
            # tool_call id), so they own the chat-completions SSE channel.
            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                agent_ref=agent_ref,
                user_id=tenant,
                gateway_session_key=gateway_session_key,
            ))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=session_id,
                gateway_session_key=gateway_session_key,
            )

        # Non-streaming: run the agent (with optional Idempotency-Key)
        async def _compute_completion():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                user_id=tenant,
                gateway_session_key=gateway_session_key,
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

        try:
            output_files, cleaned_final_response = self._store_output_artifacts(final_response, user_id=tenant)
        except _OutputArtifactError as exc:
            logger.warning("[api_server] output artifact failure (chat): %s", exc.message)
            return web.json_response(_openai_error(exc.message, code=exc.code), status=exc.status)

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
                        "content": cleaned_final_response,
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
        if output_files:
            response_data["output"] = output_files
            response_data["files"] = [item.get("file") for item in output_files if item.get("file")]

        response_headers = {
            "X-Hermes-Session-Id": result.get("session_id", session_id),
        }
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key
        return web.json_response(response_data, headers=response_headers)

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None, session_id: str = None,
        gateway_session_key: str = None,
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
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
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
                conversation history.  See #6972 for the original event,
                #16588 for the ``toolCallId``/``status`` lifecycle fields.
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
            loop = asyncio.get_running_loop()
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
        except Exception as _exc:
            # Agent crashed mid-stream.  Try to emit an error chunk
            # so the client gets a proper response instead of a
            # TransferEncodingError from incomplete chunked encoding.
            import traceback as _tb
            logger.error("Agent crashed mid-stream for %s: %s", completion_id, _tb.format_exc()[:300])
            try:
                error_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
                await response.write(f"data: {json.dumps(error_chunk)}\n\n".encode())
                await response.write(b"data: [DONE]\n\n")
            except Exception:
                pass

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
        _file_counter: list[int] | None = None,
    ) -> tuple[str, str]:
        if _depth > _max_depth:
            return "", ""
        agent_parts: list[str] = []
        storage_parts: list[str] = []
        items = parts[:MAX_CONTENT_LIST_SIZE] if len(parts) > MAX_CONTENT_LIST_SIZE else parts
        if _file_counter is None:
            _file_counter = [0]
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
                    _file_counter[0] += 1
                    if _file_counter[0] > MAX_INPUT_FILES_PER_REQUEST:
                        raise _InputFileNormalizationError(
                            f"Too many input files (max {MAX_INPUT_FILES_PER_REQUEST})",
                            status=400,
                            code="file_limit_exceeded",
                        )
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
                    part, tenant, _depth=_depth + 1, _max_depth=_max_depth, _file_counter=_file_counter
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

    async def _normalize_chat_user_content(self, content: Any, tenant: str, *, file_counter: list[int] | None = None) -> str:
        """Normalize chat user content, resolving retained file references."""
        if isinstance(content, list):
            if any(
                isinstance(part, dict)
                and str(part.get("type") or "").strip().lower() == "file"
                for part in content
            ):
                raise ValueError(
                    "unsupported_content_type:Inline image inputs are supported, "
                    "but uploaded files and document inputs are not supported on this endpoint."
                )
            counter = file_counter if file_counter is not None else [0]
            agent_content, _ = await self._normalize_response_content_parts(content, tenant, _file_counter=counter)
            return agent_content
        return _normalize_chat_content(content)

    async def _write_sse_responses(
        self,
        request: "web.Request",
        response_id: str,
        model: str,
        created_at: int,
        stream_q,
        agent_task,
        agent_ref,
        conversation_history: List[Dict[str, str]],
        storage_history: List[Dict[str, Any]],
        user_message: str,
        stored_user_message: Any,
        instructions: Optional[str],
        conversation: Optional[str],
        store: bool,
        session_id: str,
        tenant: str,
        gateway_session_key: Optional[str] = None,
    ) -> "web.StreamResponse":
        """Write an SSE stream for POST /v1/responses (OpenAI Responses API).

        Emits spec-compliant event types as the agent runs:

        - ``response.created`` — initial envelope (status=in_progress)
        - ``response.output_text.delta`` / ``response.output_text.done`` —
          streamed assistant text
        - ``response.output_item.added`` / ``response.output_item.done``
          with ``item.type == "function_call"`` — when the agent invokes a
          tool (both events fire; the ``done`` event carries the finalized
          ``arguments`` string)
        - ``response.output_item.added`` with
          ``item.type == "function_call_output"`` — tool result with
          ``{call_id, output, status}``
        - ``response.completed`` — terminal event carrying the full
          response object with all output items + usage (same payload
          shape as the non-streaming path for parity)
        - ``response.failed`` — terminal event on agent error

        If the client disconnects mid-stream, ``agent.interrupt()`` is
        called so the agent stops issuing upstream LLM calls, then the
        asyncio task is cancelled.  When ``store=True`` an initial
        ``in_progress`` snapshot is persisted immediately after
        ``response.created`` and disconnects update it to an
        ``incomplete`` snapshot so GET /v1/responses/{id} and
        ``previous_response_id`` chaining still have something to
        recover from.
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        # State accumulated during the stream
        final_text_parts: List[str] = []
        # Track open function_call items by name so we can emit a matching
        # ``done`` event when the tool completes.  Order preserved.
        pending_tool_calls: List[Dict[str, Any]] = []
        # Output items we've emitted so far (used to build the terminal
        # response.completed payload).  Kept in the order they appeared.
        emitted_items: List[Dict[str, Any]] = []
        # Monotonic counter for output_index (spec requires it).
        output_index = 0
        # Monotonic counter for call_id generation if the agent doesn't
        # provide one (it doesn't, from tool_progress_callback).
        call_counter = 0
        # Canonical Responses SSE events include a monotonically increasing
        # sequence_number. Add it server-side for every emitted event so
        # clients that validate the OpenAI event schema can parse our stream.
        sequence_number = 0
        # Track the assistant message item id + content index for text
        # delta events — the spec ties deltas to a specific item.
        message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        message_output_index: Optional[int] = None
        message_opened = False

        async def _write_event(event_type: str, data: Dict[str, Any]) -> None:
            nonlocal sequence_number
            if "sequence_number" not in data:
                data["sequence_number"] = sequence_number
            sequence_number += 1
            payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            await response.write(payload.encode())

        def _envelope(status: str) -> Dict[str, Any]:
            env: Dict[str, Any] = {
                "id": response_id,
                "object": "response",
                "status": status,
                "created_at": created_at,
                "model": model,
            }
            return env

        final_response_text = ""
        agent_error: Optional[str] = None
        usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        terminal_snapshot_persisted = False

        def _persist_response_snapshot(
            response_env: Dict[str, Any],
            *,
            conversation_history_snapshot: Optional[List[Dict[str, Any]]] = None,
        ) -> None:
            if not store:
                return
            if conversation_history_snapshot is None:
                conversation_history_snapshot = list(conversation_history)
                conversation_history_snapshot.append({"role": "user", "content": user_message})
            self._response_store.put(response_id, {
                "response": response_env,
                "conversation_history": conversation_history_snapshot,
                "instructions": instructions,
                "session_id": session_id,
            })
            if conversation:
                self._response_store.set_conversation(conversation, response_id)

        def _persist_incomplete_if_needed() -> None:
            """Persist an ``incomplete`` snapshot if no terminal one was written.

            Called from both the client-disconnect (``ConnectionResetError``)
            and server-cancellation (``asyncio.CancelledError``) paths so
            GET /v1/responses/{id} and ``previous_response_id`` chaining keep
            working after abrupt stream termination.
            """
            if not store or terminal_snapshot_persisted:
                return
            incomplete_text = "".join(final_text_parts) or final_response_text
            incomplete_items: List[Dict[str, Any]] = list(emitted_items)
            if incomplete_text:
                incomplete_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": incomplete_text}],
                })
            incomplete_env = _envelope("incomplete")
            incomplete_env["output"] = incomplete_items
            incomplete_env["usage"] = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            incomplete_history = list(conversation_history)
            incomplete_history.append({"role": "user", "content": user_message})
            if incomplete_text:
                incomplete_history.append({"role": "assistant", "content": incomplete_text})
            _persist_response_snapshot(
                incomplete_env,
                conversation_history_snapshot=incomplete_history,
            )

        try:
            # response.created — initial envelope, status=in_progress
            created_env = _envelope("in_progress")
            created_env["output"] = []
            await _write_event("response.created", {
                "type": "response.created",
                "response": created_env,
            })
            _persist_response_snapshot(created_env)
            last_activity = time.monotonic()

            async def _open_message_item() -> None:
                """Emit response.output_item.added for the assistant message
                the first time any text delta arrives."""
                nonlocal message_opened, message_output_index, output_index
                if message_opened:
                    return
                message_opened = True
                message_output_index = output_index
                output_index += 1
                item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": message_output_index,
                    "item": item,
                })

            async def _emit_text_delta(delta_text: str) -> None:
                await _open_message_item()
                final_text_parts.append(delta_text)
                await _write_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "delta": delta_text,
                    "logprobs": [],
                })

            async def _emit_tool_started(payload: Dict[str, Any]) -> str:
                """Emit response.output_item.added for a function_call.

                Returns the call_id so the matching completion event can
                reference it.  Prefer the real ``tool_call_id`` from the
                agent when available; fall back to a generated call id for
                safety in tests or older code paths.
                """
                nonlocal output_index, call_counter
                call_counter += 1
                call_id = payload.get("tool_call_id") or f"call_{response_id[5:]}_{call_counter}"
                args = payload.get("arguments", {})
                if isinstance(args, dict):
                    arguments_str = json.dumps(args)
                else:
                    arguments_str = str(args)
                item = {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "type": "function_call",
                    "status": "in_progress",
                    "name": payload.get("name", ""),
                    "call_id": call_id,
                    "arguments": arguments_str,
                }
                idx = output_index
                output_index += 1
                pending_tool_calls.append({
                    "call_id": call_id,
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "item_id": item["id"],
                    "output_index": idx,
                })
                emitted_items.append({
                    "type": "function_call",
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "call_id": call_id,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": item,
                })
                return call_id

            async def _emit_tool_completed(payload: Dict[str, Any]) -> None:
                """Emit response.output_item.done (function_call) followed
                by response.output_item.added (function_call_output)."""
                nonlocal output_index
                call_id = payload.get("tool_call_id")
                result = payload.get("result", "")
                pending = None
                if call_id:
                    for i, p in enumerate(pending_tool_calls):
                        if p["call_id"] == call_id:
                            pending = pending_tool_calls.pop(i)
                            break
                if pending is None:
                    # Completion without a matching start — skip to avoid
                    # emitting orphaned done events.
                    return

                # function_call done
                done_item = {
                    "id": pending["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "name": pending["name"],
                    "call_id": pending["call_id"],
                    "arguments": pending["arguments"],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": pending["output_index"],
                    "item": done_item,
                })

                # function_call_output added (result)
                result_str = result if isinstance(result, str) else json.dumps(result)
                output_parts = [{"type": "input_text", "text": result_str}]
                output_item = {
                    "id": f"fco_{uuid.uuid4().hex[:24]}",
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                    "status": "completed",
                }
                idx = output_index
                output_index += 1
                emitted_items.append({
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": output_item,
                })
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": idx,
                    "item": output_item,
                })

            # Main drain loop — thread-safe queue fed by agent callbacks.
            async def _dispatch(it) -> None:
                """Route a queue item to the correct SSE emitter.

                Plain strings are text deltas — they are batched (50ms)
                to reduce Open WebUI re-render storms.  Tagged tuples
                with ``__tool_started__`` / ``__tool_completed__``
                prefixes are tool lifecycle events and flush the buffer
                before emitting.
                """
                nonlocal _batch_timer
                if isinstance(it, tuple) and len(it) == 2 and isinstance(it[0], str):
                    tag, payload = it
                    # Flush batched text before tool events
                    if _batch_buf:
                        await _flush_batch()
                    if tag == "__tool_started__":
                        await _emit_tool_started(payload)
                    elif tag == "__tool_completed__":
                        await _emit_tool_completed(payload)
                elif isinstance(it, str):
                    # Batch text deltas — append to buffer, flush on timer
                    _batch_buf.append(it)
                    if _batch_timer is None:
                        _batch_timer = asyncio.create_task(_batch_flush_after(0.05))
                # Other types are silently dropped.

            # ── Batching state ──
            _batch_buf: List[str] = []
            _batch_timer: Optional[asyncio.Task] = None
            _batch_lock = asyncio.Lock()

            async def _batch_flush_after(delay: float) -> None:
                """Wait delay seconds, then flush accumulated text deltas."""
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                # Clear timer reference BEFORE flush so new deltas
                # can start a fresh timer while we emit
                nonlocal _batch_buf, _batch_timer
                _batch_timer = None
                await _flush_batch()

            async def _flush_batch() -> None:
                """Emit a single SSE delta for all accumulated text."""
                nonlocal _batch_buf
                async with _batch_lock:
                    if _batch_buf:
                        combined = "".join(_batch_buf)
                        _batch_buf = []
                        await _emit_text_delta(combined)

            loop = asyncio.get_running_loop()
            while True:
                try:
                    item = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain remaining
                        while True:
                            try:
                                item = stream_q.get_nowait()
                                if item is None:
                                    break
                                await _dispatch(item)
                                last_activity = time.monotonic()
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if item is None:  # EOS sentinel
                    # Cancel pending timer and flush remaining batched text
                    if _batch_timer and not _batch_timer.done():
                        _batch_timer.cancel()
                        _batch_timer = None
                    if _batch_buf:
                        await _flush_batch()
                    break

                await _dispatch(item)
                last_activity = time.monotonic()

            # Flush any final batched text before processing result
            if _batch_buf:
                await _flush_batch()

            # Pick up agent result + usage from the completed task
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
                # If the agent produced a final_response but no text
                # deltas were streamed (e.g. some providers only emit
                # the full response at the end), emit a single fallback
                # delta so Responses clients still receive a live text part.
                agent_final = result.get("final_response", "") if isinstance(result, dict) else ""
                if agent_final and not final_text_parts:
                    await _emit_text_delta(agent_final)
                if agent_final and not final_response_text:
                    final_response_text = agent_final
                if isinstance(result, dict) and result.get("error") and not final_response_text:
                    agent_error = result["error"]
            except Exception as e:  # noqa: BLE001
                logger.error("Error running agent for streaming responses: %s", e, exc_info=True)
                agent_error = str(e)

            # Close the message item if it was opened
            final_response_text = "".join(final_text_parts) or final_response_text
            if message_opened:
                await _write_event("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "text": final_response_text,
                    "logprobs": [],
                })
                msg_done_item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": final_response_text}
                    ],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": message_output_index,
                    "item": msg_done_item,
                })

            # Always append a final message item in the completed
            # response envelope so clients that only parse the terminal
            # payload still see the assistant text.  This mirrors the
            # shape produced by _extract_output_items in the batch path.
            final_items: List[Dict[str, Any]] = list(emitted_items)

            # Trim large content from tool call arguments to keep the
            # response.completed event under ~100KB.  Clients already
            # received full details via incremental events.
            for _item in final_items:
                if _item.get("type") == "function_call":
                    try:
                        _args = json.loads(_item.get("arguments", "{}")) if isinstance(_item.get("arguments"), str) else _item.get("arguments", {})
                        if isinstance(_args, dict):
                            for _k in ("content", "query", "pattern", "old_string", "new_string"):
                                if isinstance(_args.get(_k), str) and len(_args[_k]) > 500:
                                    _args[_k] = "[" + str(len(_args[_k])) + " chars — truncated for response.completed]"
                            _item["arguments"] = json.dumps(_args)
                    except Exception:
                        pass
                elif _item.get("type") == "function_call_output":
                    _output = _item.get("output", [])
                    if isinstance(_output, list) and _output:
                        _first = _output[0]
                        if isinstance(_first, dict) and _first.get("type") == "input_text":
                            _text = _first.get("text", "")
                            if len(_text) > 1000:
                                _first["text"] = _text[:500] + "...[" + str(len(_text) - 500) + " more chars]"
                                _item["output"] = [_first]

            final_items.append({
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": final_response_text or (agent_error or "")}
                ],
            })

            if agent_error:
                failed_env = _envelope("failed")
                failed_env["output"] = final_items
                failed_env["error"] = {"message": agent_error, "type": "server_error"}
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                _failed_history = list(conversation_history)
                _failed_history.append({"role": "user", "content": user_message})
                if final_response_text or agent_error:
                    _failed_history.append({
                        "role": "assistant",
                        "content": final_response_text or agent_error,
                    })
                _persist_response_snapshot(
                    failed_env,
                    conversation_history_snapshot=_failed_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            else:
                completed_env = _envelope("completed")
                completed_env["output"] = final_items
                completed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                full_history = list(conversation_history)
                full_history.append({"role": "user", "content": user_message})
                if isinstance(result, dict) and result.get("messages"):
                    full_history.extend(result["messages"])
                else:
                    full_history.append({"role": "assistant", "content": final_response_text})
                _persist_response_snapshot(
                    completed_env,
                    conversation_history_snapshot=full_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.completed", {
                    "type": "response.completed",
                    "response": completed_env,
                })

                # Persist for future chaining / GET retrieval, mirroring
                # the batch path behavior.
                if store:
                    cleaned_final_response = self._strip_local_paths(final_response_text)
                    full_history = list(storage_history)
                    full_history.append({"role": "user", "content": stored_user_message})
                    if isinstance(result, dict) and result.get("messages"):
                        sanitized_agent = self._sanitize_messages_for_storage(
                            result["messages"],
                            original_final=final_response_text,
                            cleaned_final=cleaned_final_response,
                        )
                        sanitized_agent = self._sanitize_history_local_paths(sanitized_agent)
                        full_history.extend(sanitized_agent)
                    else:
                        full_history.append({"role": "assistant", "content": cleaned_final_response})
                    full_history = self._sanitize_history_local_paths(full_history)
                    self._response_store.put(
                        response_id,
                        {
                            "response": completed_env,
                            "conversation_history": full_history,
                            "instructions": instructions,
                            "session_id": session_id,
                        },
                        user_id=tenant,
                    )
                    if conversation:
                        self._response_store.set_conversation(conversation, response_id, user_id=tenant)

        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            _persist_incomplete_if_needed()
            # Client disconnected — interrupt the agent so it stops
            # making upstream LLM calls, then cancel the task.
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
            logger.info("SSE client disconnected; interrupted agent task %s", response_id)
        except asyncio.CancelledError:
            # Server-side cancellation (e.g. shutdown, request timeout) —
            # persist an incomplete snapshot so GET /v1/responses/{id} and
            # previous_response_id chaining still work, then re-raise so the
            # runtime's cancellation semantics are respected.
            _persist_incomplete_if_needed()
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE task cancelled")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
            logger.info("SSE task cancelled; persisted incomplete snapshot for %s", response_id)
            raise
        except Exception as _exc:
            # Agent crashed with an unhandled error (e.g. model API error like
            # BadRequestError, AuthenticationError).  Emit a response.failed
            # event and properly terminate the SSE stream so the client doesn't
            # get a TransferEncodingError from incomplete chunked encoding.
            import traceback as _tb
            _persist_incomplete_if_needed()
            agent_error = _tb.format_exc()
            try:
                failed_env = _envelope("failed")
                failed_env["output"] = list(emitted_items)
                failed_env["error"] = {"message": str(_exc)[:500], "type": "server_error"}
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            except Exception:
                pass
            logger.error("Agent crashed mid-stream for %s: %s", response_id, str(agent_error)[:300])

        return response

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — OpenAI Responses API format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Long-term memory scope header (see chat_completions for details).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

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

        file_counter = [0]

        # conversation and previous_response_id are mutually exclusive
        if conversation and previous_response_id:
            return web.json_response(_openai_error("Cannot use both 'conversation' and 'previous_response_id'"), status=400)

        # Resolve conversation name to latest response_id
        if conversation:
            previous_response_id = self._response_store.get_conversation(conversation, user_id=tenant)
            # No error if conversation doesn't exist yet — it's a new conversation

        # Normalize input to message list (agent and stored variants)
        input_messages: List[Dict[str, Any]] = []
        storage_messages: List[Dict[str, Any]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
            storage_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for idx, item in enumerate(raw_input):
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                    storage_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    raw_content = item.get("content", "")
                    if isinstance(raw_content, list):
                        try:
                            has_file_parts = any(
                                isinstance(part, dict)
                                and str(part.get("type") or "").strip().lower() in _FILE_PART_TYPES
                                for part in raw_content
                            )
                            if has_file_parts:
                                agent_content, stored_content = await self._normalize_response_content_parts(
                                    raw_content,
                                    tenant,
                                    _file_counter=file_counter,
                                )
                            else:
                                normalized_content = _normalize_multimodal_content(raw_content)
                                agent_content = normalized_content
                                stored_content = normalized_content
                        except _InputFileNormalizationError as exc:
                            return web.json_response(
                                _openai_error(exc.message, code=exc.code),
                                status=exc.status,
                            )
                        except ValueError as exc:
                            return _multimodal_validation_error(exc, param=f"input[{idx}].content")
                    else:
                        try:
                            agent_content = _normalize_multimodal_content(raw_content)
                        except ValueError as exc:
                            return _multimodal_validation_error(exc, param=f"input[{idx}].content")
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
        agent_history: List[Dict[str, Any]] = []
        storage_history: List[Dict[str, Any]] = []
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
                raw_entry_content = entry["content"]
                if isinstance(raw_entry_content, list):
                    try:
                        has_file_parts = any(
                            isinstance(part, dict)
                            and str(part.get("type") or "").strip().lower() in _FILE_PART_TYPES
                            for part in raw_entry_content
                        )
                        if has_file_parts:
                            agent_content, stored_content = await self._normalize_response_content_parts(
                                raw_entry_content,
                                tenant,
                                _file_counter=file_counter,
                            )
                        else:
                            normalized_content = _normalize_multimodal_content(raw_entry_content)
                            agent_content = normalized_content
                            stored_content = normalized_content
                    except _InputFileNormalizationError as exc:
                        return web.json_response(
                            _openai_error(exc.message, code=exc.code),
                            status=exc.status,
                        )
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"conversation_history[{i}].content")
                else:
                    try:
                        agent_content = _normalize_multimodal_content(raw_entry_content)
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"conversation_history[{i}].content")
                    stored_content = agent_content
                if isinstance(stored_content, str):
                    stored_content = self._strip_local_paths(stored_content)
                agent_history.append({"role": str(entry["role"]), "content": agent_content})
                storage_history.append({"role": str(entry["role"]), "content": stored_content})
                if previous_response_id:
                    logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")
        stored_session_id = None

        if not agent_history and previous_response_id:
            stored = self._response_store.get(previous_response_id, user_id=tenant)
            if stored is None:
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            base_history = list(stored.get("conversation_history", []))
            agent_history = list(base_history)
            storage_history = list(base_history)
            stored_session_id = stored.get("session_id")
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
        if not _content_has_visible_payload(user_message):
            return web.json_response(_openai_error("No user message found in input"), status=400)

        # Truncation support
        if body.get("truncation") == "auto":
            if len(agent_history) > 100:
                agent_history = agent_history[-100:]
            if len(storage_history) > 100:
                storage_history = storage_history[-100:]

        # Reuse session from previous_response_id chain so the dashboard
        # groups the entire conversation under one session entry.
        session_id = stored_session_id or str(uuid.uuid4())

        stream = bool(body.get("stream", False))
        if stream:
            # Streaming branch — emit OpenAI Responses SSE events as the
            # agent runs so frontends can render text deltas and tool
            # calls in real time.  See _write_sse_responses for details.
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # None from the agent is a CLI box-close signal, not EOS.
                # Forwarding would kill the SSE stream prematurely; the
                # SSE writer detects completion via agent_task.done().
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                """Queue non-start tool progress events if needed in future.

                The structured Responses stream uses ``tool_start_callback``
                and ``tool_complete_callback`` for exact call-id correlation,
                so progress events are currently ignored here.
                """
                return

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Queue a started tool for live function_call streaming."""
                _stream_q.put(("__tool_started__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Queue a completed tool result for live function_call_output streaming."""
                _stream_q.put(("__tool_completed__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                    "result": function_result,
                }))

            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=agent_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                user_id=tenant,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                agent_ref=agent_ref,
                gateway_session_key=gateway_session_key,
            ))

            response_id = f"resp_{uuid.uuid4().hex[:28]}"
            model_name = body.get("model", self._model_name)
            created_at = int(time.time())

            return await self._write_sse_responses(
                request=request,
                response_id=response_id,
                model=model_name,
                created_at=created_at,
                stream_q=_stream_q,
                agent_task=agent_task,
                agent_ref=agent_ref,
                conversation_history=agent_history,
                storage_history=storage_history,
                user_message=user_message,
                stored_user_message=stored_user_message,
                instructions=instructions,
                conversation=conversation,
                store=store,
                session_id=session_id,
                tenant=tenant,
                gateway_session_key=gateway_session_key,
            )

        async def _compute_response():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=agent_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                user_id=tenant,
                gateway_session_key=gateway_session_key,
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
        try:
            output_items, cleaned_final_response = self._extract_output_items(
                result,
                user_id=tenant,
                source_run_id=response_id,
            )
        except _OutputArtifactError as exc:
            logger.warning("[api_server] output artifact failure: %s", exc.message)
            return web.json_response(_openai_error(exc.message, code=exc.code), status=exc.status)

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
                "session_id": session_id,
            }, user_id=tenant)
            # Update conversation mapping so the next request with the same
            # conversation name automatically chains to this response
            if conversation:
                self._response_store.set_conversation(conversation, response_id, user_id=tenant)

        response_headers = {"X-Hermes-Session-Id": session_id}
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key
        return web.json_response(response_data, headers=response_headers)

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
            "source_run_id": meta.get("source_run_id"),
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
            code = "file_too_large" if status == 413 else "invalid_request_error"
            return web.json_response(_openai_error(message, code=code), status=status)
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

    _JOB_ID_RE = __import__("re").compile(r"[a-f0-9]{12}")
    # Allowed fields for update — prevents clients injecting arbitrary keys
    _UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    _MAX_NAME_LENGTH = 200
    _MAX_PROMPT_LENGTH = 5000

    @staticmethod
    def _check_jobs_available() -> Optional["web.Response"]:
        """Return error response if cron module isn't available."""
        if not _CRON_AVAILABLE:
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
            jobs = _cron_list(include_disabled=include_disabled)
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

            job = _cron_create(**kwargs)
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
            job = _cron_get(job_id)
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
            job = _cron_update(job_id, sanitized)
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
            success = _cron_remove(job_id)
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
            job = _cron_pause(job_id)
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
            job = _cron_resume(job_id)
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
            job = _cron_trigger(job_id)
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
        source_run_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Extract downloadable files from assistant text and copy them into tenant storage."""
        text = final_text or ""
        output_files: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        spans: list[tuple[int, int]] = []
        candidates: list[str] = []

        def _register_path(path_text: str, span: tuple[int, int] | None = None) -> None:
            candidate = self._normalize_output_path(path_text)
            if not candidate:
                return
            expanded = os.path.expanduser(candidate)
            if expanded in seen_paths:
                if span:
                    spans.append(span)
                return
            path_obj = Path(expanded).expanduser()
            if not path_obj.is_file():
                raise _OutputArtifactError(
                    f"Output file not found: {candidate}",
                    status=400,
                    code="file_not_found",
                )
            try:
                size_bytes = path_obj.stat().st_size
            except Exception as exc:
                raise _OutputArtifactError(
                    f"Failed to read output file: {candidate}",
                    status=400,
                    code="invalid_request_error",
                ) from exc
            if size_bytes > MAX_OUTPUT_FILE_BYTES:
                raise _OutputArtifactError(
                    f"Output file too large (max {MAX_OUTPUT_FILE_BYTES} bytes)",
                    status=413,
                    code="output_file_too_large",
                )
            seen_paths.add(expanded)
            candidates.append(candidate)
            if span:
                spans.append(span)

        for match in self._EXPLICIT_OUTPUT_FILE_RE.finditer(text):
            _register_path(match.group("path"), span=match.span())

        if spans:
            rebuilt: list[str] = []
            cursor = 0
            for start, end in spans:
                rebuilt.append(text[cursor:start])
                cursor = end
            rebuilt.append(text[cursor:])
            text = "".join(rebuilt)

        local_paths, cleaned_text = BasePlatformAdapter.extract_local_files(text)
        for local_path in local_paths:
            _register_path(local_path)
        if local_paths:
            text = cleaned_text

        staged_ids: list[str] = []
        last_candidate: str | None = None
        try:
            for candidate in candidates:
                last_candidate = candidate
                meta = self._output_file_store.put_from_path(
                    candidate,
                    user_id=user_id,
                    purpose="output",
                    source="assistant_output",
                    source_run_id=source_run_id,
                )
                staged_ids.append(meta["file_id"])
                file_obj = self._serialize_file(meta)
                output_files.append({
                    "type": "output_file",
                    "file_id": meta["file_id"],
                    "filename": meta["filename"],
                    "mime_type": meta["mime_type"],
                    "size_bytes": meta["size_bytes"],
                    "source_run_id": meta.get("source_run_id"),
                    "download_url": file_obj["download_url"],
                    "file": file_obj,
                })
        except Exception as exc:
            for fid in staged_ids:
                try:
                    self._output_file_store.delete(fid, user_id=user_id)
                except Exception:
                    pass
            if isinstance(exc, _OutputArtifactError):
                raise
            logger.warning("[api_server] failed to stage output artifact %s: %s", last_candidate, exc)
            raise _OutputArtifactError("Failed to store output file", status=500, code="server_error") from exc

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
        source_run_id: str | None = None,
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

        output_files, cleaned_final = self._store_output_artifacts(
            final,
            user_id=user_id,
            source_run_id=source_run_id,
        )
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
        tool_start_callback=None,
        tool_complete_callback=None,
        agent_ref: Optional[list] = None,
        gateway_session_key: Optional[str] = None,
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
        loop = asyncio.get_running_loop()

        def _run():
            agent = self._create_agent(
                ephemeral_system_prompt=ephemeral_system_prompt,
                session_id=session_id,
                stream_delta_callback=stream_delta_callback,
                tool_progress_callback=tool_progress_callback,
                user_id=user_id,
                tool_start_callback=tool_start_callback,
                tool_complete_callback=tool_complete_callback,
                gateway_session_key=gateway_session_key,
            )
            if agent_ref is not None:
                agent_ref[0] = agent
            effective_task_id = session_id or str(uuid.uuid4())
            result = agent.run_conversation(
                user_message=user_message,
                conversation_history=conversation_history,
                task_id=effective_task_id,
            )
            usage = {
                "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
            }
            # Include the effective session ID in the result so callers
            # (e.g. X-Hermes-Session-Id header) can track compression-
            # triggered session rotations. (#16938)
            _eff_sid = getattr(agent, "session_id", session_id)
            if isinstance(_eff_sid, str) and _eff_sid:
                result["session_id"] = _eff_sid
            return result, usage

        return await loop.run_in_executor(None, _run)

    # ------------------------------------------------------------------
    # /v1/runs — structured event streaming
    # ------------------------------------------------------------------

    _MAX_CONCURRENT_RUNS = 10  # Prevent unbounded resource allocation
    _RUN_STREAM_TTL = 300  # seconds before orphaned runs are swept
    _RUN_STATUS_TTL = 3600  # seconds to retain terminal run status for polling

    def _set_run_status(self, run_id: str, status: str, **fields: Any) -> Dict[str, Any]:
        """Update pollable run status without exposing private agent objects."""
        now = time.time()
        current = self._run_statuses.get(run_id, {})
        current.update({
            "object": "hermes.run",
            "run_id": run_id,
            "status": status,
            "updated_at": now,
        })
        current.setdefault("created_at", fields.pop("created_at", now))
        current.update(fields)
        self._run_statuses[run_id] = current
        return current

    def _make_run_event_callback(self, run_id: str, loop: "asyncio.AbstractEventLoop"):
        """Return a tool_progress_callback that pushes structured events to the run's SSE queue."""
        def _push(event: Dict[str, Any]) -> None:
            self._set_run_status(
                run_id,
                self._run_statuses.get(run_id, {}).get("status", "running"),
                last_event=event.get("event"),
            )
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

        # Long-term memory scope header (see chat_completions for details).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

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
        if raw_input is None:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        file_counter = [0]
        input_messages: List[Dict[str, Any]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            if not raw_input:
                return web.json_response(_openai_error("No user message found in input"), status=400)
            for idx, item in enumerate(raw_input):
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                    continue
                if not isinstance(item, dict):
                    return web.json_response(
                        _openai_error("'input' array items must be strings or objects"),
                        status=400,
                    )

                role = str(item.get("role") or "user")
                raw_content = item.get("content", "")
                if isinstance(raw_content, list):
                    try:
                        has_file_parts = any(
                            isinstance(part, dict)
                            and str(part.get("type") or "").strip().lower() in _FILE_PART_TYPES
                            for part in raw_content
                        )
                        if has_file_parts:
                            normalized_content, _ = await self._normalize_response_content_parts(
                                raw_content,
                                tenant,
                                _file_counter=file_counter,
                            )
                        else:
                            normalized_content = _normalize_multimodal_content(raw_content)
                    except _InputFileNormalizationError as exc:
                        return web.json_response(
                            _openai_error(exc.message, code=exc.code),
                            status=exc.status,
                        )
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"input[{idx}].content")
                else:
                    try:
                        normalized_content = _normalize_multimodal_content(raw_content)
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"input[{idx}].content")

                input_messages.append({"role": role, "content": normalized_content})
        else:
            return web.json_response(_openai_error("'input' must be a string or array"), status=400)

        user_message = input_messages[-1].get("content", "") if input_messages else ""
        if not _content_has_visible_payload(user_message):
            return web.json_response(_openai_error("No user message found in input"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        # Accept explicit conversation_history from the request body.
        # Precedence: explicit conversation_history > previous_response_id > session_id replay.
        conversation_history: List[Dict[str, Any]] = []
        history_supplied = "conversation_history" in body
        raw_history = body.get("conversation_history")
        if history_supplied:
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
                entry_content = entry.get("content")
                if isinstance(entry_content, list):
                    try:
                        has_file_parts = any(
                            isinstance(part, dict)
                            and str(part.get("type") or "").strip().lower() in _FILE_PART_TYPES
                            for part in entry_content
                        )
                        if has_file_parts:
                            normalized_history_content, _ = await self._normalize_response_content_parts(
                                entry_content,
                                tenant,
                                _file_counter=file_counter,
                            )
                        else:
                            normalized_history_content = _normalize_multimodal_content(entry_content)
                    except _InputFileNormalizationError as exc:
                        return web.json_response(
                            _openai_error(exc.message, code=exc.code),
                            status=exc.status,
                        )
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"conversation_history[{i}].content")
                else:
                    try:
                        normalized_history_content = _normalize_multimodal_content(entry_content)
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"conversation_history[{i}].content")
                conversation_history.append({"role": str(entry["role"]), "content": normalized_history_content})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id, user_id=tenant)
            if stored:
                raw_stored_history = stored.get("conversation_history", [])
                if not isinstance(raw_stored_history, list):
                    return web.json_response(
                        _openai_error("Stored conversation history is malformed", code="invalid_session_history"),
                        status=400,
                    )
                for i, entry in enumerate(raw_stored_history):
                    if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                        return web.json_response(
                            _openai_error(
                                f"Stored conversation history is malformed at index {i}",
                                code="invalid_session_history",
                            ),
                            status=400,
                        )
                conversation_history = self._sanitize_history_local_paths(list(raw_stored_history))
                stored_session_id = stored.get("session_id")
                if instructions is None:
                    instructions = stored.get("instructions")

        session_id_value = body.get("session_id")
        session_id: Optional[str] = None
        if session_id_value is not None:
            if isinstance(session_id_value, (int, float)) and not isinstance(session_id_value, bool):
                session_id_value = str(session_id_value)
            if not isinstance(session_id_value, str) or not session_id_value.strip():
                return web.json_response(
                    _openai_error("Invalid session_id: must be a non-empty string.", code="invalid_session_id"),
                    status=400,
                )
            if re.search(r"[\r\n\x00]", session_id_value):
                return web.json_response(
                    _openai_error("Invalid session_id", code="invalid_session_id"),
                    status=400,
                )
            session_id = session_id_value.strip()

        # When continuing a known session without explicit history, replay tenant-scoped
        # SessionDB history server-side so browser clients don't need to send it back.
        if session_id and not history_supplied and not conversation_history:
            db = self._ensure_session_db()
            if db is None:
                return web.json_response(
                    _openai_error(
                        "Session history is unavailable for this request.",
                        err_type="server_error",
                        code="session_history_unavailable",
                    ),
                    status=503,
                )
            try:
                db_history = db.get_messages_as_conversation(session_id, user_id=tenant)
            except Exception as exc:
                logger.warning("Failed to load run session history for %s: %s", session_id, exc)
                return web.json_response(
                    _openai_error(
                        "Failed to load session history.",
                        err_type="server_error",
                        code="session_history_unavailable",
                    ),
                    status=500,
                )
            if db_history is None:
                db_history = []
            if not isinstance(db_history, list):
                return web.json_response(
                    _openai_error("Stored conversation history is malformed", code="invalid_session_history"),
                    status=400,
                )
            for i, entry in enumerate(db_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(
                            f"Stored conversation history is malformed at index {i}",
                            code="invalid_session_history",
                        ),
                        status=400,
                    )
            conversation_history = self._sanitize_history_local_paths(db_history)

        # Include all prior input items as immediate history context, preserving order.
        for msg in input_messages[:-1]:
            conversation_history.append(msg)

        run_id = f"run_{uuid.uuid4().hex}"
        session_id = session_id or stored_session_id or run_id
        ephemeral_system_prompt = instructions

        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        self._run_streams[run_id] = q
        created_at = time.time()
        self._run_streams_created[run_id] = time.time()

        event_cb = self._make_run_event_callback(run_id, loop)

        # Also wire stream_delta_callback so message.delta events flow through.
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

        self._set_run_status(
            run_id,
            "queued",
            created_at=created_at,
            session_id=session_id,
            model=body.get("model", self._model_name),
        )

        async def _run_and_close():
            try:
                self._set_run_status(run_id, "running")
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                    user_id=tenant,
                    gateway_session_key=gateway_session_key,
                )
                self._active_run_agents[run_id] = agent
                def _run_sync():
                    effective_task_id = session_id or run_id
                    r = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=conversation_history,
                        task_id=effective_task_id,
                    )
                    u = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                # Check for structured failure (non-retryable client errors like
                # 401/400 return failed=True instead of raising, so the except
                # block below never fires — issue #15561).
                if isinstance(result, dict) and result.get("failed"):
                    error_msg = result.get("error") or "agent run failed"
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": error_msg,
                    })
                    self._set_run_status(
                        run_id,
                        "failed",
                        error=error_msg,
                        last_event="run.failed",
                    )
                else:
                    final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                    output_files, cleaned_final_response = self._store_output_artifacts(
                        final_response,
                        user_id=tenant,
                        source_run_id=run_id,
                    )
                    q.put_nowait({
                        "event": "run.completed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "output": cleaned_final_response,
                        "files": output_files,
                        "usage": usage,
                    })
                    self._set_run_status(
                        run_id,
                        "completed",
                        output=cleaned_final_response,
                        usage=usage,
                        last_event="run.completed",
                    )
            except asyncio.CancelledError:
                self._set_run_status(
                    run_id,
                    "cancelled",
                    last_event="run.cancelled",
                )
                try:
                    q.put_nowait({
                        "event": "run.cancelled",
                        "run_id": run_id,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
                raise
            except Exception as exc:
                logger.exception("[api_server] run %s failed", run_id)
                self._set_run_status(
                    run_id,
                    "failed",
                    error=str(exc),
                    last_event="run.failed",
                )
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
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)

        task = asyncio.create_task(_run_and_close())
        self._active_run_tasks[run_id] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        response_headers = (
            {"X-Hermes-Session-Key": gateway_session_key} if gateway_session_key else {}
        )
        return web.json_response(
            {"run_id": run_id, "status": "started"},
            status=202,
            headers=response_headers,
        )

    async def _handle_get_run(self, request: "web.Request") -> "web.Response":
        """GET /v1/runs/{run_id} — return pollable run status for external UIs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )
        return web.json_response(status)

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

    async def _handle_stop_run(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/stop — interrupt a running agent."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        agent = self._active_run_agents.get(run_id)
        task = self._active_run_tasks.get(run_id)

        if agent is None and task is None:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        self._set_run_status(run_id, "stopping", last_event="run.stopping")

        if agent is not None:
            try:
                agent.interrupt("Stop requested via API")
            except Exception:
                pass

        if task is not None and not task.done():
            task.cancel()
            # Bounded wait: run_conversation() executes in the default
            # executor thread which task.cancel() cannot preempt — we rely on
            # agent.interrupt() above to break the loop. Cap the wait so a
            # slow/unresponsive interrupt can't hang this handler.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[api_server] stop for run %s timed out after 5s; "
                    "agent may still be finishing the current step",
                    run_id,
                )
            except (asyncio.CancelledError, Exception):
                pass

        return web.json_response({"run_id": run_id, "status": "stopping"})

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
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)

            stale_statuses = [
                run_id
                for run_id, status in list(self._run_statuses.items())
                if status.get("status") in {"completed", "failed", "cancelled"}
                and now - float(status.get("updated_at", 0) or 0) > self._RUN_STATUS_TTL
            ]
            for run_id in stale_statuses:
                self._run_statuses.pop(run_id, None)

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
            self._app = web.Application(middlewares=mws, client_max_size=MAX_REQUEST_BYTES)
            self._app["api_server_adapter"] = self
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get("/health/detailed", self._handle_health_detailed)
            self._app.router.add_get("/v1/health", self._handle_health)
            self._app.router.add_get("/v1/models", self._handle_models)
            self._app.router.add_get("/v1/capabilities", self._handle_capabilities)
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
            self._app.router.add_get("/v1/runs/{run_id}", self._handle_get_run)
            self._app.router.add_get("/v1/runs/{run_id}/events", self._handle_run_events)
            self._app.router.add_post("/v1/runs/{run_id}/stop", self._handle_stop_run)
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
