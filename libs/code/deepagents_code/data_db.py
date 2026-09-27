"""Local SQLite database for user data that is not a conversation.

`data.db` lives in the profile's state directory, beside `sessions.db`, so it
follows `DEEPAGENTS_HOME` like every other profile path. The prompt library is
its first user. The generic name leaves room for other local data later.

`default_data_db_path` resolves the file here instead of adding a field to
`ProfilePaths`. The feature then stays inside its own modules, and merges of
upstream changes to `_paths.py` cannot conflict with it.

Each operation opens its own short-lived connection with `open_data_db` and
closes it at the end. No connection stays open across screens, so another dcode
window never waits on a connection that this one forgot to close.

The schema is the append-only `MIGRATIONS` tuple, and `PRAGMA user_version`
counts how many of its scripts a file has run. A file with a higher count was
written by a newer dcode, so it is refused rather than read. An existing file
is never deleted, renamed, truncated, or recreated: a damaged database stays on
disk for the user to inspect.

This module must not load at startup, so callers import it lazily. Keep its
module-level imports to the standard library plus `_paths`, which is itself
standard-library only and already loaded by then.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING

from deepagents_code._paths import harden_state_dir

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from deepagents_code._paths import ProfilePaths

logger = logging.getLogger(__name__)

DATA_DB_FILENAME = "data.db"
"""File name of the database inside the profile's state directory."""

_PROMPTS_SCHEMA = """
CREATE TABLE prompts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE prompt_versions (
    id INTEGER PRIMARY KEY,
    prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (prompt_id, version)
);
"""
"""Saved prompts and their append-only versions, owned by `prompt_store`.

`name_key` is the case-folded name that makes names unique without regard to
case. SQLite's `NOCASE` collation folds only ASCII letters, so it would treat
`Revisão` and `REVISÃO` as two different names.
"""

MIGRATIONS: tuple[str, ...] = (_PROMPTS_SCHEMA,)
"""Schema scripts in apply order; a file that ran `n` of them has `user_version` `n`.

Append new scripts at the end. Never edit, reorder, or remove a released one:
the version records only how many scripts ran, so a changed script never runs
again on a file that already has it.

All pending scripts run in one `BEGIN IMMEDIATE` transaction with foreign keys
off, as SQLite's table-rebuild procedure requires. A script must not manage
its own transaction, and it must not rely on `ON DELETE` cascades.
"""

_BUSY_TIMEOUT = 5.0
"""Seconds to wait for another connection's lock before reporting `busy`."""

_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def default_data_db_path(profile: ProfilePaths | None = None) -> Path:
    """Return where `data.db` lives for a profile.

    Args:
        profile: The profile to resolve against, or `None` for the active one.

    Returns:
        The database file inside the profile's state directory.
    """
    if profile is None:
        # Imported per call, not bound at module import, so a replaced profile
        # snapshot, such as the one `install_profile_snapshot` installs in
        # tests, is the one that counts.
        from deepagents_code._paths import PATHS

        profile = PATHS.profile
    return profile.state_dir / DATA_DB_FILENAME


class DataDBError(Exception):
    """Base class for `data.db` failures, with a reason fit to show the user."""

    def __init__(self, path: Path, reason: str) -> None:
        """Initialize the error.

        Args:
            path: The database file.
            reason: Short description of the failure that is safe to show the
                user, such as `busy`.
        """
        super().__init__(f"Cannot use {path}: {reason}")
        self.path = path
        self.reason = reason


class DataDBUnavailableError(DataDBError):
    """The file cannot be opened, read, or written right now."""


class DataDBVersionError(DataDBError):
    """The file has a schema version this dcode does not know.

    Normally a newer dcode wrote it. The file is left exactly as it was.
    """


@contextmanager
def open_data_db(
    path: Path, *, migrations: Sequence[str] = MIGRATIONS
) -> Iterator[sqlite3.Connection]:
    """Open `data.db`, bring its schema up to date, and yield a connection.

    The block is one unit of work: it commits when the block exits normally and
    rolls back when the block raises. Start the block with `BEGIN IMMEDIATE`
    when it reads before it writes, so another dcode window cannot change the
    rows in between.

    Args:
        path: The database file, normally `default_data_db_path()`.
        migrations: Schema scripts to apply. Tests pass their own.

    Yields:
        A connection that returns `sqlite3.Row` rows and enforces foreign keys.

    Raises:
        DataDBUnavailableError: If the file cannot be opened, read, or written.
            This includes SQLite and OS errors raised in the block, so catch
            an expected `sqlite3.IntegrityError` inside the block.
        DataDBVersionError: If the file has a newer schema.
    """  # noqa: DOC502 - `DataDBVersionError` is raised by `_schema_version`
    if not harden_state_dir(path.parent):
        raise DataDBUnavailableError(path, "cannot create its directory")
    try:
        with closing(_connect(path)) as conn:
            _prepare(conn, path, migrations)
            # `_connect` chose SQLite's own autocommit mode for the setup above.
            # Callers get Python's default transaction handling, so `commit()`
            # and `with conn` behave as they do on any other connection.
            conn.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn
    except (sqlite3.DatabaseError, OSError) as exc:
        raise DataDBUnavailableError(path, _reason(exc)) from exc


def _connect(path: Path) -> sqlite3.Connection:
    """Open a connection in SQLite's own autocommit mode.

    `_migrate` needs that mode. Under Python's legacy transaction handling,
    `executescript` commits the open transaction first, which would release
    the `BEGIN IMMEDIATE` lock before the scripts run.

    Returns:
        The new connection.
    """
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT, autocommit=True)
    conn.row_factory = sqlite3.Row
    return conn


def _prepare(conn: sqlite3.Connection, path: Path, migrations: Sequence[str]) -> None:
    """Refuse a newer file, then enable WAL and apply missing scripts.

    The version check comes first because it only reads. Enabling WAL writes
    the file header, and a newer file must stay exactly as it was.
    """
    applied = _schema_version(conn, path, len(migrations))
    _enable_wal(conn, path)
    if applied < len(migrations):
        _migrate(conn, path, migrations)


def _schema_version(conn: sqlite3.Connection, path: Path, known: int) -> int:
    """Return how many migrations the file has run.

    Returns:
        The file's `user_version`.

    Raises:
        DataDBVersionError: If the version is outside `0` to `known`.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= known:
        msg = f"unknown schema version {version} (expected at most {known})"
        raise DataDBVersionError(path, msg)
    return version


def _enable_wal(conn: sqlite3.Connection, path: Path) -> None:
    """Switch the file to write-ahead logging when the filesystem allows it.

    WAL lets one dcode window read while another writes. Some filesystems, such
    as network mounts, cannot provide the shared memory that WAL needs. The file
    then keeps its rollback journal, which is slower under contention but still
    correct.
    """
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    except sqlite3.OperationalError:
        logger.debug("Could not enable WAL for %s", path, exc_info=True)
        return
    if mode != "wal":
        logger.debug("WAL refused for %s; journal mode stays %s", path, mode)


def _migrate(conn: sqlite3.Connection, path: Path, migrations: Sequence[str]) -> None:
    """Apply the scripts the file has not run, each exactly once.

    The version is read again after `BEGIN IMMEDIATE` takes the write lock,
    because another process may have applied the same scripts while this one
    waited for the lock.
    """
    with _write_transaction(conn):
        applied = _schema_version(conn, path, len(migrations))
        if applied == len(migrations):
            return
        for script in migrations[applied:]:
            conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {len(migrations)}")


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Hold the write lock for the block, then commit, or roll back on error.

    Yields:
        Control while the lock is held.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        # SQLite ends the transaction itself after some errors, such as a full
        # disk, and a second ROLLBACK would fail.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _reason(exc: sqlite3.DatabaseError | OSError) -> str:
    """Describe a failure in words that are safe to show the user.

    SQLite messages never include file paths or bound values. `strerror` is
    used for an `OSError` because its full text repeats the path.

    Returns:
        A short reason, such as `busy`.
    """
    if isinstance(exc, OSError):
        return exc.strerror or str(exc)
    code = getattr(exc, "sqlite_errorcode", None)
    primary = None if code is None else code & 0xFF
    if primary in _BUSY_CODES:
        return "busy"
    if primary == sqlite3.SQLITE_NOTADB:
        return "not a SQLite database"
    return str(exc) or type(exc).__name__
