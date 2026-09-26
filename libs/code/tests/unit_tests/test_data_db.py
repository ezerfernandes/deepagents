"""Tests for the `data.db` connection and migration layer."""

from __future__ import annotations

import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from deepagents_code import data_db
from deepagents_code._paths import _capture_paths
from deepagents_code.data_db import (
    DataDBUnavailableError,
    DataDBVersionError,
    default_data_db_path,
    open_data_db,
)

if TYPE_CHECKING:
    from pathlib import Path


def _scripts(count: int) -> tuple[str, ...]:
    """Return test migrations that each record their number when applied.

    A script that runs twice shows up twice in `_applied`.
    """
    return tuple(
        "CREATE TABLE IF NOT EXISTS applied (script INTEGER NOT NULL);"
        f"INSERT INTO applied VALUES ({number});"
        for number in range(1, count + 1)
    )


def _open(path: Path, migrations: tuple[str, ...]) -> None:
    with open_data_db(path, migrations=migrations):
        pass


def _applied(path: Path) -> list[int]:
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT script FROM applied ORDER BY rowid")
        return [row[0] for row in rows]


def _user_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


class TestDefaultPath:
    """`data.db` follows the selected profile like the other state files."""

    def test_follows_default_and_configured_profiles(self, tmp_path: Path) -> None:
        default = _capture_paths(None, launch_home=tmp_path)
        configured = _capture_paths(str(tmp_path / "work"), launch_home=tmp_path)

        assert default_data_db_path(default.profile) == (
            tmp_path / ".deepagents" / ".state" / "data.db"
        )
        assert default_data_db_path(configured.profile) == (
            tmp_path / "work" / ".state" / "data.db"
        )


class TestMigrations:
    """Each migration runs exactly once per file."""

    def test_new_file_gets_every_migration(self, tmp_path: Path) -> None:
        path = tmp_path / "data.db"

        _open(path, _scripts(2))

        assert path.is_file()
        assert _user_version(path) == 2
        assert _applied(path) == [1, 2]

    def test_second_open_applies_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "data.db"

        _open(path, _scripts(2))
        _open(path, _scripts(2))

        assert _applied(path) == [1, 2]

    def test_appended_script_is_the_only_one_applied(self, tmp_path: Path) -> None:
        path = tmp_path / "data.db"
        _open(path, _scripts(2))

        _open(path, _scripts(3))

        assert _user_version(path) == 3
        assert _applied(path) == [1, 2, 3]

    def test_failed_script_rolls_back_the_whole_upgrade(self, tmp_path: Path) -> None:
        """A half-applied upgrade would run its first scripts again next time."""
        path = tmp_path / "data.db"
        broken = (*_scripts(1), "INSERT INTO missing VALUES (1);")

        with pytest.raises(DataDBUnavailableError):
            _open(path, broken)

        assert _user_version(path) == 0
        _open(path, _scripts(1))
        assert _applied(path) == [1]

    def test_scripts_applied_during_the_lock_wait_are_not_repeated(
        self, tmp_path: Path
    ) -> None:
        """The version is read again once the write lock is held.

        Another dcode window can apply the same scripts after this open reads
        the version and before it gets the lock. The raw connection plays that
        window: it holds the lock, migrates, and commits while the open waits.
        """
        path = tmp_path / "data.db"
        _open(path, ())
        migrations = _scripts(2)

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            closing(sqlite3.connect(path, autocommit=True)) as other,
        ):
            other.execute("BEGIN IMMEDIATE")
            waiter = pool.submit(_open, path, migrations)
            # Let the waiter read version 0 and block on the lock. If it is
            # slower than this, it reads version 2 and the test still passes.
            time.sleep(0.2)
            for script in migrations:
                other.executescript(script)
            other.execute(f"PRAGMA user_version = {len(migrations)}")
            other.execute("COMMIT")
            waiter.result(timeout=10)

        assert _applied(path) == [1, 2]


class TestRefusedFiles:
    """A file that cannot be used raises a typed error and stays unchanged."""

    def test_newer_schema_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "data.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA user_version = 3")
        before = path.read_bytes()

        with pytest.raises(DataDBVersionError) as excinfo:
            _open(path, _scripts(2))

        assert excinfo.value.path == path
        assert path.read_bytes() == before

    def test_non_database_file_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "data.db"
        junk = bytes(range(256)) * 16
        path.write_bytes(junk)

        with pytest.raises(DataDBUnavailableError) as excinfo:
            _open(path, _scripts(1))

        assert excinfo.value.reason == "not a SQLite database"
        assert path.read_bytes() == junk

    def test_lock_held_past_the_timeout_reports_busy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Errors raised inside the block are translated too."""
        monkeypatch.setattr(data_db, "_BUSY_TIMEOUT", 0.05)
        path = tmp_path / "data.db"
        _open(path, ())

        with closing(sqlite3.connect(path, autocommit=True)) as other:
            other.execute("BEGIN IMMEDIATE")
            with (
                pytest.raises(DataDBUnavailableError) as excinfo,
                open_data_db(path, migrations=()) as conn,
            ):
                conn.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")

        assert excinfo.value.reason == "busy"


class TestConnection:
    """The yielded connection is ready for one unit of work."""

    def test_foreign_keys_are_enforced(self, tmp_path: Path) -> None:
        schema = (
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child (parent_id INTEGER NOT NULL REFERENCES parent(id));"
        )

        with (
            open_data_db(tmp_path / "data.db", migrations=(schema,)) as conn,
            pytest.raises(sqlite3.IntegrityError),
        ):
            conn.execute("INSERT INTO child VALUES (1)")

    def test_block_commits_on_exit_and_rolls_back_on_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "data.db"
        schema = ("CREATE TABLE notes (body TEXT NOT NULL);",)

        def insert(body: str, *, fail: bool) -> None:
            with open_data_db(path, migrations=schema) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO notes VALUES (?)", (body,))
                if fail:
                    raise RuntimeError

        insert("kept", fail=False)
        with pytest.raises(RuntimeError):
            insert("dropped", fail=True)

        with open_data_db(path, migrations=schema) as conn:
            bodies = [row["body"] for row in conn.execute("SELECT body FROM notes")]
        assert bodies == ["kept"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_first_open_creates_a_private_directory(self, tmp_path: Path) -> None:
        """The directory mode is what keeps other local users out of the file."""
        path = tmp_path / "profile" / ".state" / "data.db"

        _open(path, ())

        assert path.parent.stat().st_mode & 0o777 == 0o700
