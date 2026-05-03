"""On-disk OAuth credential storage.

Stores one JSON file per provider at
`~/.deepagents/.state/oauth-tokens/<provider_id>.json`. Atomic writes via
`os.open(O_CREAT|O_EXCL, 0o600)` + `Path.replace`, plus an advisory
`fcntl.flock` lock on a sidecar `<provider_id>.lock` file so concurrent
processes serialize on rotating refresh tokens (Anthropic mints a new
`refresh_token` on every refresh — without the lock, a losing racer
would persist its now-invalidated copy and force the user to re-login).

On Windows `fcntl` isn't available; the lock degrades to a no-op and we
rely on atomic write alone. Single-machine multi-process safety on
Windows is best-effort until a `msvcrt.locking`-based shim is added.

Mirrors the storage shape of `mcp_auth.py:FileTokenStorage` so admins
auditing `~/.deepagents/.state/` see a consistent layout (mode 0600,
schema-version field, atomic tmp+rename).

Schema (`_STORAGE_VERSION = 1`):

    {
      "version": 1,
      "type": "oauth",
      "provider": "anthropic",
      "access": "...",
      "refresh": "...",
      "expires": 1747500000.0,
      "extras": {"enterpriseUrl": "..."}
    }
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import stat
from typing import TYPE_CHECKING, Any

from deepagents_cli.oauth.types import OAuthCredentials

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - only excluded on Windows CI
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

_STORAGE_VERSION = 1
"""Bumped on incompatible on-disk schema changes; older payloads are rejected."""

_TOKENS_SUBDIR = "oauth-tokens"
"""Subdirectory under `DEFAULT_STATE_DIR` that holds per-provider files."""

_SAFE_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
"""Provider IDs that are safe to embed in a filename (no path separators)."""


def _tokens_dir() -> Path:
    """Return `~/.deepagents/.state/oauth-tokens/`.

    The deferred import lets tests redirect token storage by patching
    `deepagents_cli.model_config.DEFAULT_STATE_DIR` (matches the pattern
    used by `mcp_auth._tokens_dir`).
    """
    from deepagents_cli.model_config import DEFAULT_STATE_DIR

    return DEFAULT_STATE_DIR / _TOKENS_SUBDIR


def _path_for(provider_id: str) -> Path:
    """Return the on-disk token file for *provider_id*.

    Raises:
        ValueError: If *provider_id* contains characters that would let
            it escape the tokens directory.
    """
    if not _SAFE_PROVIDER_ID_RE.fullmatch(provider_id):
        msg = (
            f"Invalid OAuth provider id {provider_id!r}: must match "
            "[A-Za-z0-9_-]+ to keep the on-disk path inside "
            "~/.deepagents/.state/oauth-tokens/."
        )
        raise ValueError(msg)
    return _tokens_dir() / f"{provider_id}.json"


def _lock_path_for(provider_id: str) -> Path:
    """Return the sidecar lock-file path for *provider_id*."""
    if not _SAFE_PROVIDER_ID_RE.fullmatch(provider_id):
        msg = f"Invalid OAuth provider id {provider_id!r}: must match [A-Za-z0-9_-]+."
        raise ValueError(msg)
    return _tokens_dir() / f"{provider_id}.lock"


def _open_lock_fd(provider_id: str) -> int:
    """Open (and lazily create) the lock file for *provider_id*.

    The lock file has mode `0600` and lives in the same directory as
    the token JSON, so a single `chmod 700` on the parent directory
    covers both. The file's contents are irrelevant — we only ever
    `flock` on its file descriptor.
    """
    path = _lock_path_for(provider_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "chmod"):
        with contextlib.suppress(OSError):
            path.parent.chmod(stat.S_IRWXU)
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)


def _acquire_flock(fd: int) -> None:
    if not _HAS_FCNTL or fcntl is None:
        return
    fcntl.flock(fd, fcntl.LOCK_EX)


def _release_flock(fd: int) -> None:
    if _HAS_FCNTL and fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


@contextlib.contextmanager
def lock_provider(provider_id: str) -> Iterator[None]:
    """Acquire an exclusive cross-process lock for *provider_id*.

    Used by `save_credentials` and any sync caller that needs to
    serialize against in-flight refreshes. Holds an `fcntl.flock`-based
    advisory lock on a sidecar `<provider_id>.lock` file (no-op on
    platforms without `fcntl`, e.g. Windows).
    """
    fd = _open_lock_fd(provider_id)
    try:
        _acquire_flock(fd)
        yield
    finally:
        _release_flock(fd)


@contextlib.asynccontextmanager
async def alock_provider(provider_id: str) -> AsyncIterator[None]:
    """Async variant of `lock_provider` for use inside coroutines.

    The acquisition syscall is offloaded to a worker thread so the
    asyncio event loop stays responsive while we wait on a refresh
    happening in another process. Once acquired, the lock is held for
    the duration of the `async with` block — typically wrapping a
    network refresh round-trip plus the on-disk write.
    """
    fd = _open_lock_fd(provider_id)
    try:
        await asyncio.to_thread(_acquire_flock, fd)
        yield
    finally:
        await asyncio.to_thread(_release_flock, fd)


def _read_raw(path: Path) -> dict[str, Any] | None:
    """Read and validate the on-disk payload for *path*.

    Returns:
        The decoded payload, or `None` if the file does not exist.

    Raises:
        RuntimeError: If the file is malformed or carries an unsupported
            schema version. Tells the user how to recover.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        msg = (
            f"Failed to read OAuth token file {path}: {exc}. "
            "Delete the file and re-run `deepagents login <provider>` "
            "if it is corrupt."
        )
        raise RuntimeError(msg) from exc
    if not isinstance(data, dict):
        msg = (
            f"OAuth token file {path} has invalid shape (expected a "
            "JSON object). Delete it and re-run `deepagents login`."
        )
        raise RuntimeError(msg)
    if data.get("version") != _STORAGE_VERSION:
        msg = (
            f"OAuth token file {path} has unsupported version "
            f"{data.get('version')!r} (expected {_STORAGE_VERSION}). "
            "Delete it and re-run `deepagents login`."
        )
        raise RuntimeError(msg)
    return data


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* to *path* atomically with mode 0600.

    Uses the same tmp+rename + `os.open(O_EXCL, 0o600)` recipe as
    `mcp_auth._write` so the token file is never visible at the default
    umask between create and chmod.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "chmod"):
        try:
            path.parent.chmod(stat.S_IRWXU)
        except OSError as exc:
            logger.warning(
                "Could not lock down OAuth tokens dir %s (mode 0700): %s. "
                "Tokens may be readable by other local users.",
                path.parent,
                exc,
            )

    tmp = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    try:
        tmp.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    if hasattr(os, "chmod"):
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            logger.warning(
                "Could not set mode 0600 on OAuth token file %s: %s. "
                "Stored refresh/access tokens may be world-readable.",
                path,
                exc,
            )


def load_credentials(provider_id: str) -> OAuthCredentials | None:
    """Return stored credentials for *provider_id*, or `None` if absent.

    Raises:
        RuntimeError: If the on-disk file is corrupt or carries an
            unsupported schema version. The message tells the user how
            to recover.
    """
    path = _path_for(provider_id)
    data = _read_raw(path)
    if data is None:
        return None
    try:
        return OAuthCredentials(
            access=str(data["access"]),
            refresh=str(data["refresh"]),
            expires=float(data["expires"]),
            extras=dict(data.get("extras") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        msg = (
            f"OAuth token file {path} is missing required fields: {exc}. "
            "Delete it and re-run `deepagents login`."
        )
        raise RuntimeError(msg) from exc


def save_credentials_locked(provider_id: str, credentials: OAuthCredentials) -> None:
    """Persist *credentials* assuming the provider lock is already held.

    Internal helper for callers (e.g. `_api.refresh_credentials`) that
    have already taken `alock_provider`/`lock_provider`. Re-acquiring
    `fcntl.flock` on the same process succeeds but burns a syscall;
    splitting the API keeps the hot path obvious.
    """
    path = _path_for(provider_id)
    payload: dict[str, Any] = {
        "version": _STORAGE_VERSION,
        "type": "oauth",
        "provider": provider_id,
        "access": credentials.access,
        "refresh": credentials.refresh,
        "expires": credentials.expires,
    }
    if credentials.extras:
        payload["extras"] = dict(credentials.extras)
    _write_atomic(path, payload)


def save_credentials(provider_id: str, credentials: OAuthCredentials) -> None:
    """Persist *credentials* under *provider_id* atomically.

    Acquires the provider lock so concurrent writers (e.g. two
    `deepagents login` processes) serialize. Inside an existing locked
    region, call `save_credentials_locked` directly instead.
    """
    with lock_provider(provider_id):
        save_credentials_locked(provider_id, credentials)


def delete_credentials(provider_id: str) -> bool:
    """Delete the credentials file for *provider_id*.

    `Path.unlink` is the source of truth — checking `exists()` first
    races against another process (or a parallel `deepagents logout`
    invocation) that could remove the file between the check and the
    unlink, which would leak a `FileNotFoundError` to the caller. We
    let `unlink()` itself report whether the file was there.

    Returns:
        `True` if a file was removed, `False` if there was nothing to
        delete.
    """
    path = _path_for(provider_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def list_logged_in_providers() -> list[str]:
    """Return the IDs of providers that currently have stored credentials.

    Sorted for stable display. Filenames that don't match
    `<provider_id>.json` are ignored — they may be tmp leftovers from a
    crashed write.
    """
    directory = _tokens_dir()
    if not directory.is_dir():
        return []
    out: list[str] = []
    for entry in directory.iterdir():
        if entry.suffix != ".json":
            continue
        stem = entry.stem
        if not _SAFE_PROVIDER_ID_RE.fullmatch(stem):
            continue
        out.append(stem)
    out.sort()
    return out
