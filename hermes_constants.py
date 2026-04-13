"""Shared constants for Hermes Agent.

Import-safe module with no dependencies — can be imported from anywhere
without risk of circular imports.
"""

import os
import re
import contextvars
from contextlib import contextmanager
from pathlib import Path


DEFAULT_TENANT = "default"

# Context-local tenant binding. Defaults to None; callers use
# get_current_tenant() to fall back to env/default when unbound.
_CURRENT_TENANT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hermes_current_tenant", default=None
)

RUNTIME_KEY_DELIMITER = "::"


def get_current_tenant(user_id: str | None = None) -> str:
    """Return the effective tenant for the current execution context.

    Resolution order (normalized):
      1. Explicit ``user_id`` argument (if provided)
      2. Bound context-local tenant (set via :func:`tenant_context`)
      3. Environment variables ``HERMES_USER_ID`` / ``HERMES_SESSION_USER_ID``
      4. ``DEFAULT_TENANT``
    """
    if user_id is not None:
        return normalize_tenant(user_id)

    bound = _CURRENT_TENANT.get()
    if bound:
        return bound

    env_user = os.getenv("HERMES_USER_ID") or os.getenv("HERMES_SESSION_USER_ID")
    return normalize_tenant(env_user)




def normalize_task_id(task_id: str | None) -> str:
    """Normalize task identifiers for runtime caches.

    - ``None`` or blank → ``"default"``
    - Strips surrounding whitespace
    """
    if task_id is None:
        return DEFAULT_TENANT
    value = str(task_id).strip()
    return value or DEFAULT_TENANT


def derive_runtime_key(task_id: str | None = None, user_id: str | None = None) -> tuple[str, str, str]:
    """Return a tenant-aware runtime cache key.

    Returns (runtime_key, tenant, normalized_task_id).
    """
    tenant = get_current_tenant(user_id)
    normalized_task = normalize_task_id(task_id)
    return f"{tenant}{RUNTIME_KEY_DELIMITER}{normalized_task}", tenant, normalized_task


def split_runtime_key(runtime_key: str) -> tuple[str, str]:
    """Split a runtime cache key into (tenant, task_id).

    Accepts legacy plain task_ids (no delimiter) and falls back to
    ``DEFAULT_TENANT`` for missing/blank segments.
    """
    if not runtime_key:
        return DEFAULT_TENANT, DEFAULT_TENANT
    if RUNTIME_KEY_DELIMITER in runtime_key:
        tenant, task = runtime_key.split(RUNTIME_KEY_DELIMITER, 1)
    else:
        tenant, task = DEFAULT_TENANT, runtime_key
    return normalize_tenant(tenant), normalize_task_id(task)


def resolve_runtime_key(task_id: str | None = None, user_id: str | None = None) -> tuple[str, str, str]:
    """Coerce *task_id* into a tenant-aware runtime key.

    If *task_id* already looks like a runtime key (contains the delimiter),
    its tenant is preserved; otherwise the current tenant is applied.
    Returns (runtime_key, tenant, normalized_task_id).
    """
    if isinstance(task_id, str) and RUNTIME_KEY_DELIMITER in task_id:
        tenant, parsed_task = split_runtime_key(task_id)
        return derive_runtime_key(parsed_task, tenant)
    return derive_runtime_key(task_id, user_id)
@contextmanager
def tenant_context(user_id: str | None):
    """Bind *user_id* for the lifetime of a tool call.

    Restores the previous binding even if the caller raises.
    """
    resolved = get_current_tenant(user_id)
    token = _CURRENT_TENANT.set(resolved)
    try:
        yield resolved
    finally:
        try:
            _CURRENT_TENANT.reset(token)
        except Exception:
            _CURRENT_TENANT.set(None)


def get_hermes_home() -> Path:
    """Return the Hermes home directory (default: ~/.hermes).

    Reads HERMES_HOME env var, falls back to ~/.hermes.
    This is the single source of truth — all other copies should import this.
    """
    return Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))


def normalize_tenant(user_id: str | None) -> str:
    """Normalize tenant/user identity for storage and filesystem paths.

    - ``None`` or blank → ``"default"``
    - Strips surrounding whitespace
    - Replaces path separators and ``..`` segments with underscores to
      prevent directory traversal while preserving caller intent.
    """
    if user_id is None:
        return DEFAULT_TENANT
    value = str(user_id).strip()
    if not value:
        return DEFAULT_TENANT
    # Replace path separators and collapse traversal tokens
    value = re.sub(r"[\\/]+", "_", value)
    value = value.replace("..", "_")
    if not value:
        return DEFAULT_TENANT
    return value


def get_user_home(user_id: str | None) -> Path:
    """Return the tenant-scoped home directory under ``<HERMES_HOME>/users``."""
    return get_hermes_home() / "users" / normalize_tenant(user_id)


def get_user_subpath(user_id: str | None, *subpaths: str) -> Path:
    """Return a tenant-scoped subpath (e.g., sessions, processes files)."""
    return get_user_home(user_id).joinpath(*subpaths)


def get_optional_skills_dir(default: Path | None = None) -> Path:
    """Return the optional-skills directory, honoring package-manager wrappers.

    Packaged installs may ship ``optional-skills`` outside the Python package
    tree and expose it via ``HERMES_OPTIONAL_SKILLS``.
    """
    override = os.getenv("HERMES_OPTIONAL_SKILLS", "").strip()
    if override:
        return Path(override)
    if default is not None:
        return default
    return get_hermes_home() / "optional-skills"


def get_hermes_dir(new_subpath: str, old_name: str) -> Path:
    """Resolve a Hermes subdirectory with backward compatibility.

    New installs get the consolidated layout (e.g. ``cache/images``).
    Existing installs that already have the old path (e.g. ``image_cache``)
    keep using it — no migration required.

    Args:
        new_subpath: Preferred path relative to HERMES_HOME (e.g. ``"cache/images"``).
        old_name: Legacy path relative to HERMES_HOME (e.g. ``"image_cache"``).

    Returns:
        Absolute ``Path`` — old location if it exists on disk, otherwise the new one.
    """
    home = get_hermes_home()
    old_path = home / old_name
    if old_path.exists():
        return old_path
    return home / new_subpath


def display_hermes_home() -> str:
    """Return a user-friendly display string for the current HERMES_HOME.

    Uses ``~/`` shorthand for readability::

        default:  ``~/.hermes``
        profile:  ``~/.hermes/profiles/coder``
        custom:   ``/opt/hermes-custom``

    Use this in **user-facing** print/log messages instead of hardcoding
    ``~/.hermes``.  For code that needs a real ``Path``, use
    :func:`get_hermes_home` instead.
    """
    home = get_hermes_home()
    try:
        return "~/" + str(home.relative_to(Path.home()))
    except ValueError:
        return str(home)


VALID_REASONING_EFFORTS = ("xhigh", "high", "medium", "low", "minimal")


def parse_reasoning_effort(effort: str) -> dict | None:
    """Parse a reasoning effort level into a config dict.

    Valid levels: "xhigh", "high", "medium", "low", "minimal", "none".
    Returns None when the input is empty or unrecognized (caller uses default).
    Returns {"enabled": False} for "none".
    Returns {"enabled": True, "effort": <level>} for valid effort levels.
    """
    if not effort or not effort.strip():
        return None
    effort = effort.strip().lower()
    if effort == "none":
        return {"enabled": False}
    if effort in VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": effort}
    return None


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"
OPENROUTER_CHAT_URL = f"{OPENROUTER_BASE_URL}/chat/completions"

AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
AI_GATEWAY_MODELS_URL = f"{AI_GATEWAY_BASE_URL}/models"
AI_GATEWAY_CHAT_URL = f"{AI_GATEWAY_BASE_URL}/chat/completions"

NOUS_API_BASE_URL = "https://inference-api.nousresearch.com/v1"
NOUS_API_CHAT_URL = f"{NOUS_API_BASE_URL}/chat/completions"
