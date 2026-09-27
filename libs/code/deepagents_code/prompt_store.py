"""Saved chat prompts with append-only versions, stored in `data.db`.

A prompt is a name plus numbered versions of its body. Saving a changed body
adds the next version. No code path edits a version, and only deleting its
prompt removes one. Restoring an old version copies that body into a new
version, so the history always reads forward.

Every method opens its own connection through `open_data_db` and runs
synchronously. The TUI calls them with `asyncio.to_thread`, so the message pump
never waits on SQLite. `DataDBError` from `data_db` passes through unchanged.
The errors defined here carry messages that are safe to show the user.

Like `data_db`, this module must not load at startup. Callers import it lazily.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from deepagents_code.data_db import open_data_db

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

MAX_NAME_LENGTH = 80
"""Longest prompt name in characters, after surrounding whitespace is stripped."""

MAX_BODY_BYTES = 256 * 1024
"""Largest prompt body in UTF-8 bytes."""

_FORBIDDEN_NAME_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})
"""Unicode categories that a name cannot contain.

Control characters and line or paragraph separators break the one-line rows
that list prompts. A lone surrogate cannot be stored as UTF-8.
"""

_MISSING_PROMPT = "This prompt no longer exists. Another window may have deleted it."

_LATEST_SQL = """
SELECT p.id, p.name, p.created_at, p.updated_at, v.version, v.body
FROM prompts AS p
JOIN prompt_versions AS v ON v.prompt_id = p.id
WHERE v.version = (SELECT MAX(version) FROM prompt_versions WHERE prompt_id = p.id)
"""
_LIST_SQL = _LATEST_SQL + "ORDER BY p.updated_at DESC, p.id DESC"
_GET_SQL = _LATEST_SQL + "AND p.id = ?"


class PromptStoreError(Exception):
    """Base class for prompt library failures, with a message fit to show the user."""


class InvalidPromptError(PromptStoreError):
    """A name or body breaks a validation rule."""


class DuplicatePromptNameError(PromptStoreError):
    """Another prompt has the same name, compared without regard to case."""


class PromptNotFoundError(PromptStoreError):
    """The prompt or version does not exist, perhaps deleted in another window."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptSummary:
    """A saved prompt with its latest version.

    Attributes:
        prompt_id: Row id of the prompt. It never changes.
        name: Display name, unique without regard to case.
        latest_version: Number of the newest version. The first one is 1.
        body: Body of the newest version.
        created_at: When the prompt was created, as a UTC ISO-8601 string.
        updated_at: When the prompt was last renamed or got a new version, as
            a UTC ISO-8601 string.
    """

    prompt_id: int
    name: str
    latest_version: int
    body: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptVersion:
    """One saved body of a prompt.

    Attributes:
        prompt_id: Row id of the prompt that owns the version.
        version: Version number, counting from 1.
        body: The text saved in this version.
        created_at: When the version was saved, as a UTC ISO-8601 string.
    """

    prompt_id: int
    version: int
    body: str
    created_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EditOutcome:
    """What an `edit` or a `restore` changed.

    Attributes:
        prompt: The prompt after the change.
        renamed: Whether the name changed.
        added_version: Number of the version that was added, or `None` when
            the body stayed the same.
    """

    prompt: PromptSummary
    renamed: bool
    added_version: int | None

    @property
    def changed(self) -> bool:
        """Whether the prompt changed at all."""
        return self.renamed or self.added_version is not None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PromptStore:
    """Read and write the saved prompts in one `data.db` file."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        """Create a store for a database file.

        Nothing reads or writes the file until the first method call.

        Args:
            path: The database file, normally `data_db.default_data_db_path()`.
            clock: Source of the current time for timestamps. Tests pass a
                fake clock to control the order of updates.
        """
        self._path = path
        self._clock = clock

    @property
    def path(self) -> Path:
        """The database file, for messages that name it."""
        return self._path

    def list_prompts(self) -> list[PromptSummary]:
        """Return every prompt with its latest version, most recently updated first.

        Returns:
            One summary per prompt.
        """
        with open_data_db(self._path) as conn:
            rows = conn.execute(_LIST_SQL).fetchall()
        return [_summary(row) for row in rows]

    def get_prompt(self, prompt_id: int) -> PromptSummary:
        """Return one prompt with its latest version.

        Args:
            prompt_id: The prompt to read.

        Returns:
            The summary of the prompt.

        Raises:
            PromptNotFoundError: If the prompt does not exist.
        """
        with open_data_db(self._path) as conn:
            summary = _find_summary(conn, prompt_id)
        if summary is None:
            raise PromptNotFoundError(_MISSING_PROMPT)
        return summary

    def list_versions(self, prompt_id: int) -> list[PromptVersion]:
        """Return every version of a prompt, newest first.

        Args:
            prompt_id: The prompt to read.

        Returns:
            The versions of the prompt. A prompt always has at least one.

        Raises:
            PromptNotFoundError: If the prompt does not exist.
        """
        with open_data_db(self._path) as conn:
            rows = conn.execute(
                "SELECT prompt_id, version, body, created_at FROM prompt_versions "
                "WHERE prompt_id = ? ORDER BY version DESC",
                (prompt_id,),
            ).fetchall()
        if not rows:
            raise PromptNotFoundError(_MISSING_PROMPT)
        return [_version(row) for row in rows]

    def create(self, name: str, body: str) -> PromptSummary:
        """Save a new prompt as version 1.

        Args:
            name: The name. Surrounding whitespace is stripped.
            body: The text of the prompt, stored exactly as given.

        Returns:
            The new prompt.

        Raises:
            InvalidPromptError: If the name or the body breaks a rule.
            DuplicatePromptNameError: If another prompt has the name.
        """  # noqa: DOC502 - the validation helpers and `_unique_name` raise these
        clean = _clean_name(name)
        _check_body(body)
        with open_data_db(self._path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self._now()
            with _unique_name(clean):
                cursor = conn.execute(
                    "INSERT INTO prompts (name, name_key, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (clean, _name_key(clean), now, now),
                )
            # `lastrowid` is `None` only before a connection's first INSERT.
            prompt_id = cast("int", cursor.lastrowid)
            _insert_version(conn, prompt_id, 1, body, now)
            return _require_summary(conn, prompt_id)

    def edit(
        self, prompt_id: int, *, name: str | None = None, body: str | None = None
    ) -> EditOutcome:
        """Rename a prompt, save a new version of its body, or both.

        Both changes happen in one transaction, so an error leaves the prompt
        exactly as it was.

        Args:
            prompt_id: The prompt to change.
            name: The new name, or `None` to keep the current one.
            body: The new body, or `None` to keep the current one. A body that
                equals the latest version adds nothing.

        Returns:
            What changed, and the prompt after the change.

        Raises:
            InvalidPromptError: If the new name or body breaks a rule.
            DuplicatePromptNameError: If another prompt has the new name.
            PromptNotFoundError: If the prompt does not exist.
        """  # noqa: DOC502 - the validation and lookup helpers raise these
        new_name = None if name is None else _clean_name(name)
        if body is not None:
            _check_body(body)
        with open_data_db(self._path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _require_summary(conn, prompt_id)
            now = self._now()
            renamed = False
            if new_name is not None and new_name != current.name:
                _rename(conn, prompt_id, new_name, now)
                renamed = True
            added = None if body is None else _add_version(conn, current, body, now)
            return _outcome(conn, prompt_id, renamed=renamed, added=added)

    def restore(self, prompt_id: int, version: int) -> EditOutcome:
        """Save the body of an earlier version as the next version.

        Existing versions never change. Restoring a body that equals the latest
        version adds nothing.

        Args:
            prompt_id: The prompt to change.
            version: The version whose body to copy.

        Returns:
            What changed, and the prompt after the change.

        Raises:
            PromptNotFoundError: If the prompt or the version does not exist.
        """
        with open_data_db(self._path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _require_summary(conn, prompt_id)
            row = conn.execute(
                "SELECT body FROM prompt_versions WHERE prompt_id = ? AND version = ?",
                (prompt_id, version),
            ).fetchone()
            if row is None:
                msg = f"Version {version} of this prompt does not exist."
                raise PromptNotFoundError(msg)
            added = _add_version(conn, current, row["body"], self._now())
            return _outcome(conn, prompt_id, renamed=False, added=added)

    def delete(self, prompt_id: int) -> None:
        """Delete a prompt and all of its versions.

        Args:
            prompt_id: The prompt to delete.

        Raises:
            PromptNotFoundError: If the prompt does not exist.
        """
        with open_data_db(self._path) as conn:
            deleted = conn.execute(
                "DELETE FROM prompts WHERE id = ?", (prompt_id,)
            ).rowcount
        if deleted == 0:
            raise PromptNotFoundError(_MISSING_PROMPT)

    def _now(self) -> str:
        """Return the current time as a UTC ISO-8601 string.

        The fixed microsecond precision keeps the strings in time order when
        they are sorted as text.

        Returns:
            The timestamp to store.
        """
        return self._clock().astimezone(UTC).isoformat(timespec="microseconds")


def _clean_name(name: str) -> str:
    """Return a name the way it is stored.

    Surrounding whitespace is stripped and the name is normalized to NFC, so a
    visible name always maps to the same key.

    Returns:
        The stripped, normalized name.

    Raises:
        InvalidPromptError: If the name is empty, too long, or contains a line
            break or a control character.
    """
    clean = unicodedata.normalize("NFC", name.strip())
    if not clean:
        msg = "Enter a name for the prompt."
        raise InvalidPromptError(msg)
    if len(clean) > MAX_NAME_LENGTH:
        msg = f"Prompt names can have at most {MAX_NAME_LENGTH} characters."
        raise InvalidPromptError(msg)
    if any(unicodedata.category(char) in _FORBIDDEN_NAME_CATEGORIES for char in clean):
        msg = "Prompt names cannot contain line breaks or control characters."
        raise InvalidPromptError(msg)
    return clean


def _name_key(name: str) -> str:
    """Return the key that makes names unique without regard to case.

    Returns:
        The case-folded name, normalized to NFC.
    """
    return unicodedata.normalize("NFC", name.casefold())


def _check_body(body: str) -> None:
    """Check that a body can be saved.

    Raises:
        InvalidPromptError: If the body is blank, too large, or has characters
            that UTF-8 cannot encode.
    """
    if not body.strip():
        msg = "Enter the text of the prompt."
        raise InvalidPromptError(msg)
    try:
        size = len(body.encode("utf-8"))
    except UnicodeEncodeError as exc:
        msg = "The prompt contains characters that cannot be saved."
        raise InvalidPromptError(msg) from exc
    if size > MAX_BODY_BYTES:
        msg = f"Prompts can be at most {MAX_BODY_BYTES // 1024} KiB."
        raise InvalidPromptError(msg)


@contextmanager
def _unique_name(name: str) -> Iterator[None]:
    """Report a name collision in the block as `DuplicatePromptNameError`.

    `open_data_db` turns every other SQLite error into `DataDBUnavailableError`,
    so this is the only one caught inside the block.

    Yields:
        Control while the block writes the name.

    Raises:
        DuplicatePromptNameError: If another prompt already has the name.
        IntegrityError: If the block breaks any other constraint. It passes
            through for `open_data_db` to report.
    """
    try:
        yield
    except sqlite3.IntegrityError as exc:
        if exc.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
            raise
        msg = f'A prompt named "{name}" already exists.'
        raise DuplicatePromptNameError(msg) from exc


def _rename(conn: sqlite3.Connection, prompt_id: int, name: str, now: str) -> None:
    with _unique_name(name):
        conn.execute(
            "UPDATE prompts SET name = ?, name_key = ?, updated_at = ? WHERE id = ?",
            (name, _name_key(name), now, prompt_id),
        )


def _add_version(
    conn: sqlite3.Connection, current: PromptSummary, body: str, now: str
) -> int | None:
    """Save a body as the next version unless it equals the latest one.

    The caller holds the write lock and read `current` under it, so
    `current.latest_version` is the highest version in the table.

    Returns:
        The new version number, or `None` when the body is unchanged.
    """
    if body == current.body:
        return None
    version = current.latest_version + 1
    _insert_version(conn, current.prompt_id, version, body, now)
    conn.execute(
        "UPDATE prompts SET updated_at = ? WHERE id = ?", (now, current.prompt_id)
    )
    return version


def _insert_version(
    conn: sqlite3.Connection, prompt_id: int, version: int, body: str, now: str
) -> None:
    conn.execute(
        "INSERT INTO prompt_versions (prompt_id, version, body, created_at) "
        "VALUES (?, ?, ?, ?)",
        (prompt_id, version, body, now),
    )


def _outcome(
    conn: sqlite3.Connection, prompt_id: int, *, renamed: bool, added: int | None
) -> EditOutcome:
    """Read the prompt back after a change.

    Returns:
        The outcome of the change.
    """
    prompt = _require_summary(conn, prompt_id)
    return EditOutcome(prompt=prompt, renamed=renamed, added_version=added)


def _find_summary(conn: sqlite3.Connection, prompt_id: int) -> PromptSummary | None:
    """Look up one prompt with its latest version.

    Returns:
        The summary, or `None` when the prompt does not exist.
    """
    row = conn.execute(_GET_SQL, (prompt_id,)).fetchone()
    return None if row is None else _summary(row)


def _require_summary(conn: sqlite3.Connection, prompt_id: int) -> PromptSummary:
    """Look up one prompt with its latest version.

    Returns:
        The summary.

    Raises:
        PromptNotFoundError: If the prompt does not exist.
    """
    summary = _find_summary(conn, prompt_id)
    if summary is None:
        raise PromptNotFoundError(_MISSING_PROMPT)
    return summary


def _summary(row: sqlite3.Row) -> PromptSummary:
    return PromptSummary(
        prompt_id=row["id"],
        name=row["name"],
        latest_version=row["version"],
        body=row["body"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _version(row: sqlite3.Row) -> PromptVersion:
    return PromptVersion(
        prompt_id=row["prompt_id"],
        version=row["version"],
        body=row["body"],
        created_at=row["created_at"],
    )
