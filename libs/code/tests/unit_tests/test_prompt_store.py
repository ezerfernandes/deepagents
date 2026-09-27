"""Tests for saved prompts and their append-only versions."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from deepagents_code.data_db import DataDBUnavailableError
from deepagents_code.prompt_store import (
    MAX_BODY_BYTES,
    MAX_NAME_LENGTH,
    DuplicatePromptNameError,
    InvalidPromptError,
    PromptNotFoundError,
    PromptStore,
)

if TYPE_CHECKING:
    from pathlib import Path

_START = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class _Clock:
    """Fake clock that moves one second forward on each read."""

    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def store(tmp_path: Path) -> PromptStore:
    return PromptStore(tmp_path / "data.db", clock=_Clock())


def _bodies(store: PromptStore, prompt_id: int) -> list[str]:
    """Return the body of every version, oldest first."""
    return [version.body for version in reversed(store.list_versions(prompt_id))]


class TestCreate:
    """A new prompt starts at version 1."""

    def test_new_prompt_is_listed_at_version_one(self, store: PromptStore) -> None:
        created = store.create("code-review", "Review the diff.")

        assert created.latest_version == 1
        assert created.name == "code-review"
        assert created.body == "Review the diff."
        assert store.list_prompts() == [created]

    def test_timestamps_are_utc(self, store: PromptStore) -> None:
        created = store.create("review", "body")

        stamp = datetime.fromisoformat(created.created_at)
        assert stamp == _START + timedelta(seconds=1)
        assert stamp.utcoffset() == timedelta(0)

    def test_name_is_stripped_and_body_is_kept_exactly(
        self, store: PromptStore
    ) -> None:
        created = store.create("  review  ", "  indented\n")

        assert created.name == "review"
        assert created.body == "  indented\n"

    def test_names_are_unique_without_regard_to_case(self, store: PromptStore) -> None:
        store.create("Review", "first")

        with pytest.raises(DuplicatePromptNameError):
            store.create("rEVIEW", "second")

    def test_non_ascii_names_fold_case(self, store: PromptStore) -> None:
        """SQLite's `NOCASE` would miss this, because it folds only ASCII."""
        store.create("Revisão", "first")

        with pytest.raises(DuplicatePromptNameError):
            store.create("REVISÃO", "second")

    def test_composed_and_decomposed_names_are_the_same_name(
        self, store: PromptStore
    ) -> None:
        store.create("Revis\u00e3o", "first")

        with pytest.raises(DuplicatePromptNameError):
            store.create("Revisa\u0303o", "second")


class TestEdit:
    """A changed body adds a version; an unchanged one adds nothing."""

    def test_changed_body_adds_the_next_version(self, store: PromptStore) -> None:
        prompt = store.create("review", "one")

        outcome = store.edit(prompt.prompt_id, body="two")

        assert outcome.added_version == 2
        assert not outcome.renamed
        assert outcome.prompt.latest_version == 2
        assert outcome.prompt.body == "two"
        assert outcome.prompt.updated_at > prompt.updated_at
        assert _bodies(store, prompt.prompt_id) == ["one", "two"]

    def test_unchanged_body_adds_nothing(self, store: PromptStore) -> None:
        prompt = store.create("review", "same")

        outcome = store.edit(prompt.prompt_id, body="same")

        assert not outcome.changed
        assert outcome.prompt == prompt
        assert _bodies(store, prompt.prompt_id) == ["same"]

    def test_rename_keeps_the_versions(self, store: PromptStore) -> None:
        prompt = store.create("review", "body")

        outcome = store.edit(prompt.prompt_id, name="code-review")

        assert outcome.renamed
        assert outcome.added_version is None
        assert outcome.prompt.name == "code-review"
        assert outcome.prompt.latest_version == 1
        assert outcome.prompt.updated_at > prompt.updated_at

    def test_case_only_rename_is_a_rename(self, store: PromptStore) -> None:
        prompt = store.create("review", "body")

        outcome = store.edit(prompt.prompt_id, name="Review")

        assert outcome.renamed
        assert outcome.prompt.name == "Review"

    def test_rename_to_a_taken_name_changes_nothing(self, store: PromptStore) -> None:
        """The body in the same call is not saved either."""
        store.create("taken", "theirs")
        prompt = store.create("mine", "old body")

        with pytest.raises(DuplicatePromptNameError):
            store.edit(prompt.prompt_id, name="TAKEN", body="new body")

        assert store.get_prompt(prompt.prompt_id) == prompt

    def test_missing_prompt_is_reported(self, store: PromptStore) -> None:
        with pytest.raises(PromptNotFoundError):
            store.edit(42, body="body")

    def test_saves_from_two_stores_get_consecutive_versions(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "data.db"
        first, second = PromptStore(path), PromptStore(path)
        prompt = first.create("shared", "one")

        first.edit(prompt.prompt_id, body="from first")
        second.edit(prompt.prompt_id, body="from second")

        versions = second.list_versions(prompt.prompt_id)
        assert [version.version for version in versions] == [3, 2, 1]

    def test_concurrent_saves_are_all_kept(self, tmp_path: Path) -> None:
        """Two dcode windows saving at once must not lose or reuse a version."""
        path = tmp_path / "data.db"
        prompt = PromptStore(path).create("shared", "start")
        saves = 5

        def save(label: str) -> None:
            window = PromptStore(path)
            for number in range(saves):
                window.edit(prompt.prompt_id, body=f"{label} {number}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(save, ["first", "second"]))

        versions = PromptStore(path).list_versions(prompt.prompt_id)
        assert [version.version for version in versions] == list(
            range(2 * saves + 1, 0, -1)
        )
        assert {version.body for version in versions} == {
            "start",
            *(f"{label} {n}" for label in ("first", "second") for n in range(saves)),
        }


class TestRestore:
    """Restore copies an old body forward and never rewrites history."""

    def test_restore_adds_a_version_with_the_old_body(self, store: PromptStore) -> None:
        prompt = store.create("review", "one")
        store.edit(prompt.prompt_id, body="two")
        store.edit(prompt.prompt_id, body="three")
        before = store.list_versions(prompt.prompt_id)

        outcome = store.restore(prompt.prompt_id, 1)

        assert outcome.added_version == 4
        assert outcome.prompt.body == "one"
        after = store.list_versions(prompt.prompt_id)
        assert after[0].version == 4
        assert after[0].body == "one"
        assert after[1:] == before

    def test_restoring_the_latest_body_adds_nothing(self, store: PromptStore) -> None:
        prompt = store.create("review", "one")

        outcome = store.restore(prompt.prompt_id, 1)

        assert not outcome.changed
        assert _bodies(store, prompt.prompt_id) == ["one"]

    def test_missing_version_is_reported(self, store: PromptStore) -> None:
        prompt = store.create("review", "one")

        with pytest.raises(PromptNotFoundError):
            store.restore(prompt.prompt_id, 7)


class TestDelete:
    """Delete removes a prompt with all of its versions."""

    def test_delete_removes_the_prompt_and_its_versions(
        self, store: PromptStore
    ) -> None:
        prompt = store.create("review", "one")
        store.edit(prompt.prompt_id, body="two")

        store.delete(prompt.prompt_id)

        assert store.list_prompts() == []
        with pytest.raises(PromptNotFoundError):
            store.list_versions(prompt.prompt_id)
        with closing(sqlite3.connect(store.path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM prompt_versions").fetchone()[0]
        assert count == 0

    def test_deleting_a_missing_prompt_is_reported(self, store: PromptStore) -> None:
        with pytest.raises(PromptNotFoundError):
            store.delete(42)


class TestListing:
    """The list shows the most recently updated prompt first."""

    def test_most_recently_updated_prompt_comes_first(self, store: PromptStore) -> None:
        older = store.create("older", "a")
        store.create("newer", "b")
        assert [prompt.name for prompt in store.list_prompts()] == ["newer", "older"]

        store.edit(older.prompt_id, body="changed")

        assert [prompt.name for prompt in store.list_prompts()] == ["older", "newer"]

    def test_empty_database_lists_nothing(self, store: PromptStore) -> None:
        assert store.list_prompts() == []

    def test_missing_prompt_is_reported(self, store: PromptStore) -> None:
        with pytest.raises(PromptNotFoundError):
            store.get_prompt(42)


class TestValidation:
    """Bad names and bodies are refused before anything is written."""

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "   ",
            "x" * (MAX_NAME_LENGTH + 1),
            "two\nlines",
            "tab\there",
            "para\u2029graph",
            "lone \ud800",
        ],
    )
    def test_bad_names_are_refused(self, store: PromptStore, name: str) -> None:
        with pytest.raises(InvalidPromptError):
            store.create(name, "body")

    def test_longest_name_is_accepted(self, store: PromptStore) -> None:
        name = "x" * MAX_NAME_LENGTH

        assert store.create(name, "body").name == name

    @pytest.mark.parametrize(
        "body",
        ["", " \n\t ", "x" * (MAX_BODY_BYTES + 1), "lone \ud800 surrogate"],
    )
    def test_bad_bodies_are_refused(self, store: PromptStore, body: str) -> None:
        with pytest.raises(InvalidPromptError):
            store.create("review", body)

    def test_body_limit_counts_utf8_bytes(self, store: PromptStore) -> None:
        """`é` takes two bytes, so this body is under the limit in characters."""
        body = "é" * (MAX_BODY_BYTES // 2 + 1)

        with pytest.raises(InvalidPromptError):
            store.create("review", body)

    def test_refused_edit_leaves_the_prompt_unchanged(self, store: PromptStore) -> None:
        prompt = store.create("review", "body")

        with pytest.raises(InvalidPromptError):
            store.edit(prompt.prompt_id, body="   ")

        assert store.get_prompt(prompt.prompt_id) == prompt


class TestDatabaseErrors:
    """Errors from `data_db` reach the caller unchanged."""

    def test_damaged_file_raises_data_db_error(self, tmp_path: Path) -> None:
        path = tmp_path / "data.db"
        path.write_bytes(b"not a database " * 100)

        with pytest.raises(DataDBUnavailableError):
            PromptStore(path).list_prompts()
