"""Tests for saving a prompt from history into the library."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Input, TextArea

from deepagents_code.prompt_store import MAX_NAME_LENGTH, PromptStore
from deepagents_code.tui.modals.prompt_clipboard import PromptClipboardScreen
from deepagents_code.tui.modals.prompt_editor import NewPrompt, PromptEditorScreen
from deepagents_code.tui.modals.prompt_library import PromptLibraryScreen
from deepagents_code.tui.modals.prompt_library.from_history import draft_from_history
from deepagents_code.tui.modals.prompt_library.prompt_list import SavedPromptList

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from textual.pilot import Pilot

HISTORY = ("Explain this error", "Review the diff\nand list bugs")


class _LibraryHost(App[None]):
    """Minimal host that records the library result."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield Container()

    def open(
        self,
        store: PromptStore,
        recent_prompts: Callable[[], tuple[str, ...]] | None,
    ) -> PromptLibraryScreen:
        screen = PromptLibraryScreen(store, recent_prompts=recent_prompts)
        self.push_screen(screen, self.results.append)
        return screen


@pytest.fixture
def store(tmp_path: Path) -> PromptStore:
    return PromptStore(tmp_path / "data.db")


async def _settle(pilot: Pilot[None]) -> None:
    """Wait until no worker runs, since one store call can start the next."""
    await pilot.pause()
    while pilot.app.workers:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def _open(
    app: _LibraryHost,
    pilot: Pilot[None],
    store: PromptStore,
    recent_prompts: Callable[[], tuple[str, ...]] | None = lambda: HISTORY,
) -> PromptLibraryScreen:
    screen = app.open(store, recent_prompts)
    await _settle(pilot)
    return screen


class TestFromHistory:
    """`h` saves a prompt from history into the library."""

    async def test_h_opens_the_history_modal_with_the_recent_prompts(
        self, store: PromptStore
    ) -> None:
        app = _LibraryHost()
        async with app.run_test() as pilot:
            await _open(app, pilot, store)

            await pilot.press("h")
            await pilot.pause()

            assert isinstance(app.screen, PromptClipboardScreen)
            assert app.screen._prompts == HISTORY

    async def test_choice_opens_the_editor_with_that_prompt(
        self, store: PromptStore
    ) -> None:
        app = _LibraryHost()
        async with app.run_test() as pilot:
            await _open(app, pilot, store)

            await pilot.press("h")
            await pilot.pause()
            await pilot.press("down", "enter")
            await _settle(pilot)

            editor = app.screen
            assert isinstance(editor, PromptEditorScreen)
            assert editor.query_one("#prompt-body", TextArea).text == HISTORY[1]
            assert editor.query_one("#prompt-name", Input).value == "Review the diff"

    async def test_save_adds_the_prompt_and_selects_it(
        self, store: PromptStore
    ) -> None:
        store.create("existing", "body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            library = await _open(app, pilot, store)

            await pilot.press("h")
            await pilot.pause()
            await pilot.press("enter")
            await _settle(pilot)
            await pilot.press("ctrl+s")
            await _settle(pilot)

            assert app.screen is library
            selected = library.query_one(SavedPromptList).selected
            assert selected is not None
            assert (selected.name, selected.body) == (HISTORY[0], HISTORY[0])
        assert len(store.list_prompts()) == 2

    async def test_cancelled_history_leaves_the_library_as_it_was(
        self, store: PromptStore
    ) -> None:
        store.create("existing", "body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            library = await _open(app, pilot, store)

            await pilot.press("h")
            await pilot.pause()
            await pilot.press("escape")
            await _settle(pilot)

            assert app.screen is library
            assert app.results == []
        assert [p.name for p in store.list_prompts()] == ["existing"]

    @pytest.mark.parametrize(
        "recent_prompts", [None, lambda: ()], ids=["no-history", "empty-history"]
    )
    async def test_without_history_h_only_says_so(
        self,
        store: PromptStore,
        recent_prompts: Callable[[], tuple[str, ...]] | None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app = _LibraryHost()
        notes: list[str] = []
        monkeypatch.setattr(app, "notify", lambda message, **_: notes.append(message))
        async with app.run_test() as pilot:
            library = await _open(app, pilot, store, recent_prompts)

            await pilot.press("h")
            await pilot.pause()

            assert app.screen is library
        assert notes == ["No prompts in history yet."]


class TestDraft:
    """The suggested name comes from the first line of the prompt."""

    def test_name_is_the_first_nonblank_line_with_spaces_collapsed(self) -> None:
        text = "\n\n  Fix\tthe   parser  \nthen run the tests"

        assert draft_from_history(text) == NewPrompt(name="Fix the parser", body=text)

    def test_long_first_line_is_cut_to_the_name_limit(self) -> None:
        text = "word " * 40

        name = draft_from_history(text).name

        assert len(name) <= MAX_NAME_LENGTH
        assert name == name.strip()
        assert name.startswith("word word")
