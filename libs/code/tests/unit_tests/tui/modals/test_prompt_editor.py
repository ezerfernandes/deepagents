"""Tests for the modal that creates and edits saved prompts."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container
from textual.widgets import Input, Static, TextArea

from deepagents_code.prompt_store import PromptStore
from deepagents_code.tui.modals.prompt_editor import (
    EditorResult,
    NewPrompt,
    PromptEditorScreen,
)

if TYPE_CHECKING:
    from pathlib import Path

    from textual.content import Content
    from textual.pilot import Pilot

    from deepagents_code.prompt_store import PromptSummary


class _EditorHost(App[None]):
    """Minimal host that records the editor result.

    Like `DeepAgentsApp`, it takes `shift+tab` with a priority binding and
    hands it to the active screen's `action_move_up`.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("shift+tab", "reverse_nav", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.results: list[EditorResult | None] = []

    def compose(self) -> ComposeResult:
        yield Container()

    def open(self, screen: PromptEditorScreen) -> PromptEditorScreen:
        self.push_screen(screen, self.results.append)
        return screen

    def action_reverse_nav(self) -> None:
        move_up = getattr(self.screen, "action_move_up", None)
        if move_up is not None:
            move_up()


class _RecordingStore(PromptStore):
    """Store that records the thread of each create and can hold it open."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.threads: list[int] = []
        self.proceed = threading.Event()
        self.proceed.set()

    def create(self, name: str, body: str) -> PromptSummary:
        self.threads.append(threading.get_ident())
        self.proceed.wait(timeout=5)
        return super().create(name, body)


@pytest.fixture
def store(tmp_path: Path) -> PromptStore:
    return PromptStore(tmp_path / "data.db")


def _name(screen: PromptEditorScreen) -> Input:
    return screen.query_one("#prompt-name", Input)


def _body(screen: PromptEditorScreen) -> TextArea:
    return screen.query_one("#prompt-body", TextArea)


def _status(screen: PromptEditorScreen) -> str:
    return _plain(screen, "#prompt-editor-status")


def _plain(screen: PromptEditorScreen, selector: str) -> str:
    return cast("Content", screen.query_one(selector, Static).render()).plain


async def _save(pilot: Pilot[None]) -> None:
    """Press `ctrl+s` and wait for the save, which runs in a worker thread."""
    await pilot.press("ctrl+s")
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def _type_body(pilot: Pilot[None], screen: PromptEditorScreen, text: str) -> None:
    _body(screen).focus()
    await pilot.pause()
    await pilot.press(*text)


class TestNewPrompt:
    """New mode creates version 1."""

    async def test_save_creates_version_one_and_dismisses(
        self, store: PromptStore
    ) -> None:
        app = _EditorHost()
        async with app.run_test() as pilot:
            app.open(PromptEditorScreen(store, NewPrompt()))
            await pilot.pause()

            await pilot.press(*"review")
            await pilot.press("tab")
            await pilot.press(*"body")
            await _save(pilot)

        [prompt] = store.list_prompts()
        assert (prompt.name, prompt.body, prompt.latest_version) == (
            "review",
            "body",
            1,
        )
        assert app.results == [EditorResult(prompt=prompt)]

    async def test_caller_can_prefill_the_name_and_body(
        self, store: PromptStore
    ) -> None:
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(
                PromptEditorScreen(store, NewPrompt(name="from history", body="text"))
            )
            await pilot.pause()

            assert _name(screen).value == "from history"
            assert _body(screen).text == "text"
            assert _plain(screen, "#prompt-editor-title") == "New prompt"

    async def test_store_runs_off_the_message_pump(self, tmp_path: Path) -> None:
        store = _RecordingStore(tmp_path / "data.db")
        app = _EditorHost()
        async with app.run_test() as pilot:
            app.open(PromptEditorScreen(store, NewPrompt(name="a", body="b")))
            await pilot.pause()

            await _save(pilot)

        assert len(store.threads) == 1
        assert store.threads[0] != threading.get_ident()

    async def test_second_save_while_saving_is_ignored(self, tmp_path: Path) -> None:
        store = _RecordingStore(tmp_path / "data.db")
        store.proceed.clear()
        app = _EditorHost()
        async with app.run_test() as pilot:
            app.open(PromptEditorScreen(store, NewPrompt(name="a", body="b")))
            await pilot.pause()

            await pilot.press("ctrl+s", "ctrl+s")
            store.proceed.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

        assert len(store.threads) == 1
        assert [result is not None for result in app.results] == [True]

    async def test_duplicate_name_shows_the_error_and_keeps_the_text(
        self, store: PromptStore
    ) -> None:
        store.create("review", "first")
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, NewPrompt()))
            await pilot.pause()

            await pilot.press(*"Review")
            await _type_body(pilot, screen, "second")
            await _save(pilot)

            assert app.results == []
            assert _status(screen) == 'A prompt named "Review" already exists.'
            assert _name(screen).value == "Review"
            assert _body(screen).text == "second"
        assert len(store.list_prompts()) == 1

    async def test_invalid_prompt_shows_the_store_message(
        self, store: PromptStore
    ) -> None:
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, NewPrompt(body="text")))
            await pilot.pause()

            await _save(pilot)

            assert app.results == []
            assert _status(screen) == "Enter a name for the prompt."

    async def test_database_error_shows_its_reason_and_keeps_the_text(
        self, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        store = PromptStore(blocker / "data.db")
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, NewPrompt(name="a", body="b")))
            await pilot.pause()

            await _save(pilot)

            assert app.results == []
            assert _status(screen) == (
                "Could not save the prompt: cannot create its directory."
            )
            assert (_name(screen).value, _body(screen).text) == ("a", "b")


class TestEditPrompt:
    """Edit mode adds versions and renames."""

    async def test_changed_body_adds_the_next_version(self, store: PromptStore) -> None:
        prompt = store.create("review", "v1")
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, prompt))
            await pilot.pause()

            await _type_body(pilot, screen, "x")
            await _save(pilot)

        [result] = app.results
        assert result is not None
        assert result.outcome is not None
        assert result.outcome.added_version == 2
        assert result.prompt.body == "xv1"
        assert [v.version for v in store.list_versions(prompt.prompt_id)] == [2, 1]

    async def test_rename_adds_no_version(self, store: PromptStore) -> None:
        prompt = store.create("review", "body")
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, prompt))
            await pilot.pause()

            _name(screen).focus()
            await pilot.press("end", *"ed")
            await _save(pilot)

        [result] = app.results
        assert result is not None
        assert result.outcome is not None
        assert result.outcome.renamed
        assert result.outcome.added_version is None
        assert store.get_prompt(prompt.prompt_id).name == "reviewed"

    async def test_title_names_the_prompt_and_version_literally(
        self, store: PromptStore
    ) -> None:
        prompt = store.create("[bold]x[/bold]", "v1")
        prompt = store.edit(prompt.prompt_id, body="v2").prompt
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, prompt))
            await pilot.pause()

            title = _plain(screen, "#prompt-editor-title")

        assert title == "Edit [bold]x[/bold], from v2"

    async def test_save_without_changes_stays_open(self, store: PromptStore) -> None:
        prompt = store.create("review", "body")
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, prompt))
            await pilot.pause()

            await _save(pilot)

            assert app.results == []
            assert _status(screen) == "No changes to save"
        assert store.get_prompt(prompt.prompt_id).latest_version == 1

    async def test_name_that_only_gains_spaces_saves_nothing(
        self, store: PromptStore
    ) -> None:
        """The store strips the name, so it reports that nothing changed."""
        prompt = store.create("review", "body")
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, prompt))
            await pilot.pause()

            _name(screen).focus()
            await pilot.press("end", "space")
            await _save(pilot)

            assert app.results == []
            assert _status(screen) == "No changes to save"

    async def test_body_edit_keeps_a_rename_from_another_window(
        self, store: PromptStore
    ) -> None:
        """Only the fields the user changed are sent to the store."""
        prompt = store.create("review", "body")
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, prompt))
            await pilot.pause()
            store.edit(prompt.prompt_id, name="renamed elsewhere")

            await _type_body(pilot, screen, "x")
            await _save(pilot)

        latest = store.get_prompt(prompt.prompt_id)
        assert (latest.name, latest.body) == ("renamed elsewhere", "xbody")


class TestCancel:
    """`escape` never loses typed text without a second press."""

    async def test_escape_without_changes_dismisses(self, store: PromptStore) -> None:
        app = _EditorHost()
        async with app.run_test() as pilot:
            app.open(PromptEditorScreen(store, NewPrompt(body="prefilled")))
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()

        assert app.results == [None]

    async def test_escape_after_an_edit_needs_a_second_press(
        self, store: PromptStore
    ) -> None:
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, NewPrompt()))
            await pilot.pause()

            await pilot.press(*"draft")
            await pilot.press("escape")
            await pilot.pause()

            assert app.results == []
            assert _status(screen) == "Unsaved changes. Press Esc again to discard."

            await pilot.press("escape")
            await pilot.pause()

        assert app.results == [None]
        assert store.list_prompts() == []

    async def test_typing_after_the_warning_asks_again(
        self, store: PromptStore
    ) -> None:
        app = _EditorHost()
        async with app.run_test() as pilot:
            app.open(PromptEditorScreen(store, NewPrompt()))
            await pilot.pause()

            await pilot.press(*"draft", "escape", *"more", "escape")
            await pilot.pause()
            assert app.results == []

            await pilot.press("escape")
            await pilot.pause()

        assert app.results == [None]


class TestFocus:
    """Keys move focus between the prompt name field and body editor."""

    async def test_shift_tab_moves_from_the_body_to_the_name(
        self, store: PromptStore
    ) -> None:
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, NewPrompt()))
            await pilot.pause()
            assert app.focused is _name(screen)

            await pilot.press("tab")
            assert app.focused is _body(screen)

            await pilot.press("shift+tab")
            assert app.focused is _name(screen)

    async def test_edit_mode_starts_in_the_body(self, store: PromptStore) -> None:
        prompt = store.create("review", "body")
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, prompt))
            await pilot.pause()

            assert app.focused is _body(screen)

    async def test_footer_lists_the_keys(self, store: PromptStore) -> None:
        app = _EditorHost()
        async with app.run_test() as pilot:
            screen = app.open(PromptEditorScreen(store, NewPrompt()))
            await pilot.pause()

            footer = _plain(screen, "#prompt-editor-help")

        assert "Ctrl+S save" in footer
        assert "Ctrl+G external editor" in footer
        assert "Esc cancel" in footer
