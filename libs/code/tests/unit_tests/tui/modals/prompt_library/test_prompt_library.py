"""Tests for the prompt library screen."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container
from textual.widgets import Input, Static

from deepagents_code import clipboard
from deepagents_code._env_vars import UI_CHARSET_MODE
from deepagents_code.config import reset_glyphs_cache
from deepagents_code.prompt_store import PromptStore, PromptSummary
from deepagents_code.tui.modals.prompt_editor import PromptEditorScreen
from deepagents_code.tui.modals.prompt_library import PromptLibraryScreen
from deepagents_code.tui.modals.prompt_library.confirm import PromptConfirmScreen
from deepagents_code.tui.modals.prompt_library.prompt_list import SavedPromptRow
from deepagents_code.tui.modals.prompt_library.versions import PromptVersionsScreen

if TYPE_CHECKING:
    from pathlib import Path

    from textual.app import App as AnyApp
    from textual.content import Content
    from textual.pilot import Pilot
    from textual.widget import Widget


class _LibraryHost(App[None]):
    """Minimal host that records the library result.

    Like `DeepAgentsApp`, it takes `shift+tab` with a priority binding and
    hands it to the active screen's `action_move_up`.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("shift+tab", "reverse_nav", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.results: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield Container()

    def open(self, store: PromptStore) -> PromptLibraryScreen:
        screen = PromptLibraryScreen(store)
        self.push_screen(screen, self.results.append)
        return screen

    def action_reverse_nav(self) -> None:
        move_up = getattr(self.screen, "action_move_up", None)
        if move_up is not None:
            move_up()


class _Clock:
    """Clock that starts three hours ago and moves one second per read."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC) - timedelta(hours=3)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class _SlowListStore(PromptStore):
    """Store whose `list_prompts` waits until the test lets it go on."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.proceed = threading.Event()

    def list_prompts(self) -> list[PromptSummary]:
        self.proceed.wait(timeout=5)
        return super().list_prompts()


@pytest.fixture
def store(tmp_path: Path) -> PromptStore:
    return PromptStore(tmp_path / "data.db", clock=_Clock())


async def _settle(pilot: Pilot[None]) -> None:
    """Wait until no worker runs, since one store call can start the next."""
    await pilot.pause()
    while pilot.app.workers:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def _open(
    app: _LibraryHost, pilot: Pilot[None], store: PromptStore
) -> PromptLibraryScreen:
    screen = app.open(store)
    await _settle(pilot)
    return screen


def _names(screen: PromptLibraryScreen) -> list[str]:
    return [row.prompt.name for row in screen.query(SavedPromptRow) if row.display]


def _selected(screen: PromptLibraryScreen) -> str:
    [row] = [row for row in screen.query(SavedPromptRow) if row.has_class("-selected")]
    return row.prompt.name


def _plain(screen: Widget, selector: str) -> str:
    return cast("Content", screen.query_one(selector, Static).render()).plain


def _message(screen: PromptLibraryScreen) -> str:
    message = screen.query_one("#prompt-library-message", Static)
    return _plain(screen, "#prompt-library-message") if message.display else ""


def _filter(screen: PromptLibraryScreen) -> Input:
    return screen.query_one("#prompt-library-filter", Input)


def _is_active(app: AnyApp[None], screen_type: type) -> bool:
    return isinstance(app.screen, screen_type)


class TestStates:
    """The screen explains what it is showing when there are no rows."""

    async def test_empty_library_says_how_to_start(self, store: PromptStore) -> None:
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            assert _message(screen) == "No saved prompts yet. Press n to create one."
            assert _names(screen) == []

            await pilot.press("enter", "e", "v", "c", "d", "down")
            await pilot.pause()

            assert app.screen is screen
            assert app.results == []

    async def test_loading_line_shows_until_the_list_arrives(
        self, tmp_path: Path
    ) -> None:
        store = _SlowListStore(tmp_path / "data.db")
        store.create("review", "body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = app.open(store)
            await pilot.pause()

            assert _message(screen).startswith("Loading saved prompts")

            store.proceed.set()
            await _settle(pilot)

            assert _message(screen) == ""
            assert _names(screen) == ["review"]

    async def test_database_error_shows_the_reason_and_path(
        self, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        store = PromptStore(blocker / "data.db")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            assert _message(screen).splitlines() == [
                "Could not load saved prompts.",
                "Reason: cannot create its directory",
                f"Database: {store.path}",
            ]

            await pilot.press("escape")
            await pilot.pause()

        assert app.results == [None]


class TestList:
    """Rows show each prompt, and the preview shows the selected body."""

    async def test_rows_show_name_version_and_age_newest_first(
        self, store: PromptStore
    ) -> None:
        first = store.create("first", "one")
        store.create("second", "two")
        store.edit(first.prompt_id, body="one, again")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            rows = [row.render().plain for row in screen.query(SavedPromptRow)]
            assert _names(screen) == ["first", "second"]

        assert rows[0].startswith("first ")
        assert rows[0].endswith("v2  2h ago")
        assert rows[1].startswith("second ")
        assert rows[1].endswith("v1  2h ago")

    async def test_markup_in_a_name_renders_literally(self, store: PromptStore) -> None:
        store.create("[bold]x[/bold] [/tmp]", "[red]body[/red]")
        app = _LibraryHost()
        async with app.run_test(size=(160, 40)) as pilot:
            screen = await _open(app, pilot, store)

            [row] = screen.query(SavedPromptRow)
            assert row.render().plain.startswith("[bold]x[/bold] [/tmp]")
            assert _plain(screen, "#prompt-library-preview") == "[red]body[/red]"

    @pytest.mark.parametrize(
        ("charset", "ellipsis"), [("unicode", "…"), ("ascii", "...")]
    )
    async def test_long_name_is_cut_so_the_version_and_age_show(
        self,
        store: PromptStore,
        monkeypatch: pytest.MonkeyPatch,
        charset: str,
        ellipsis: str,
    ) -> None:
        monkeypatch.setenv(UI_CHARSET_MODE, charset)
        reset_glyphs_cache()
        store.create("n" * 80, "body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            [row] = screen.query(SavedPromptRow)
            text = row.render().plain
            width = row.size.width
        reset_glyphs_cache()

        assert text.endswith(f"n{ellipsis}  v1  2h ago")
        assert text.startswith("nnn")
        assert len(text) == width

    async def test_click_selects_a_row_without_inserting(
        self, store: PromptStore
    ) -> None:
        store.create("older", "older body")
        store.create("newer", "newer body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.click(list(screen.query(SavedPromptRow))[1])
            await pilot.pause()

            assert _selected(screen) == "older"
            assert app.results == []

    async def test_moving_the_selection_updates_the_preview(
        self, store: PromptStore
    ) -> None:
        store.create("older", "older body")
        store.create("newer", "newer body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)
            assert _plain(screen, "#prompt-library-preview") == "newer body"

            await pilot.press("down", "down")

            assert _selected(screen) == "older"
            assert _plain(screen, "#prompt-library-preview") == "older body"

            await pilot.press("shift+tab")

            assert _selected(screen) == "newer"


class TestInsert:
    """`enter` hands the latest body to the app."""

    async def test_enter_dismisses_with_the_latest_body(
        self, store: PromptStore
    ) -> None:
        prompt = store.create("review", "v1")
        store.edit(prompt.prompt_id, body="v2")
        store.create("newer", "other")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            await _open(app, pilot, store)

            await pilot.press("down", "enter")
            await pilot.pause()

        assert app.results == ["v2"]

    async def test_escape_closes_without_a_result(self, store: PromptStore) -> None:
        store.create("review", "body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            await _open(app, pilot, store)

            await pilot.press("escape")
            await pilot.pause()

        assert app.results == [None]


class TestFilter:
    """The filter field narrows the list and keeps every typed letter."""

    async def test_typing_filters_by_name_and_body_without_regard_to_case(
        self, store: PromptStore
    ) -> None:
        store.create("release-notes", "Summarize the changes")
        store.create("code-review", "Review the diff")
        store.create("refactor", "Plan a REVIEW of old code")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("slash", *"review")

            assert _filter(screen).value == "review"
            assert _names(screen) == ["refactor", "code-review"]

    async def test_letters_typed_in_the_filter_start_no_action(
        self, store: PromptStore
    ) -> None:
        store.create("review", "body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("slash", *"nedcvh")
            await pilot.pause()

            assert app.screen is screen
            assert _filter(screen).value == "nedcvh"
            assert _message(screen) == "No matching prompts."

    async def test_escape_in_the_filter_clears_it_and_returns_to_the_list(
        self, store: PromptStore
    ) -> None:
        store.create("alpha", "a")
        store.create("beta", "b")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("slash", *"alp", "escape")
            await pilot.pause()

            assert app.results == []
            assert _filter(screen).value == ""
            assert _names(screen) == ["beta", "alpha"]
            assert not _filter(screen).has_focus

    @pytest.mark.parametrize("key", ["enter", "down"])
    async def test_key_in_the_filter_returns_to_the_list(
        self, store: PromptStore, key: str
    ) -> None:
        store.create("alpha", "a")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("slash", *"alp", key)
            await pilot.pause()
            assert not _filter(screen).has_focus

            await pilot.press("enter")
            await pilot.pause()

        assert app.results == ["a"]


class TestManage:
    """Create, edit, delete, and copy run from the list."""

    async def test_new_prompt_is_added_and_selected(self, store: PromptStore) -> None:
        store.create("existing", "body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("n")
            await pilot.pause()
            assert _is_active(app, PromptEditorScreen)
            await pilot.press(*"fresh", "tab", *"text", "ctrl+s")
            await _settle(pilot)

            assert app.screen is screen
            assert _names(screen) == ["fresh", "existing"]
            assert _selected(screen) == "fresh"

    async def test_saved_prompt_that_the_filter_hides_clears_the_filter(
        self, store: PromptStore
    ) -> None:
        store.create("alpha", "a")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("slash", *"alp", "enter", "n")
            await pilot.pause()
            await pilot.press(*"beta", "tab", "b", "ctrl+s")
            await _settle(pilot)

            assert _filter(screen).value == ""
            assert _names(screen) == ["beta", "alpha"]
            assert _selected(screen) == "beta"

    async def test_cancelled_editor_leaves_the_list_as_it_was(
        self, store: PromptStore
    ) -> None:
        store.create("older", "o")
        store.create("newer", "n")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("down", "e", "escape")
            await _settle(pilot)

            assert app.screen is screen
            assert _selected(screen) == "older"

    async def test_edit_saves_a_version_and_keeps_the_selection(
        self, store: PromptStore
    ) -> None:
        store.create("older", "old body")
        store.create("newer", "new body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("down", "e")
            await pilot.pause()
            await pilot.press("x", "ctrl+s")
            await _settle(pilot)

            assert _names(screen) == ["older", "newer"]
            assert _selected(screen) == "older"
            assert _plain(screen, "#prompt-library-preview") == "xold body"

    async def test_delete_after_confirm_selects_the_neighbor(
        self, store: PromptStore
    ) -> None:
        store.create("third", "3")
        store.create("second", "2")
        store.create("first", "1")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("down", "d")
            await pilot.pause()
            assert _is_active(app, PromptConfirmScreen)
            assert _plain(app.screen, "#prompt-confirm-message") == (
                "Delete second and its 1 version? This cannot be undone."
            )
            await pilot.press("enter")
            await _settle(pilot)

            assert _names(screen) == ["first", "third"]
            assert _selected(screen) == "third"
        assert [p.name for p in store.list_prompts()] == ["first", "third"]

    async def test_delete_of_a_prompt_gone_elsewhere_says_so_and_reloads(
        self, store: PromptStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.create("kept", "k")
        gone = store.create("gone", "g")
        app = _LibraryHost()
        notes: list[str] = []
        monkeypatch.setattr(app, "notify", lambda message, **_: notes.append(message))
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)
            store.delete(gone.prompt_id)

            await pilot.press("d", "enter")
            await _settle(pilot)

            assert _names(screen) == ["kept"]
        gone_message = (
            "This prompt no longer exists. Another window may have deleted it."
        )
        assert notes == [f"Could not delete the prompt: {gone_message}"]

    async def test_delete_cancelled_keeps_the_prompt(self, store: PromptStore) -> None:
        store.create("review", "body")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("escape")
            await _settle(pilot)

            assert app.screen is screen
            assert _names(screen) == ["review"]
        assert len(store.list_prompts()) == 1

    async def test_copy_puts_the_latest_body_on_the_clipboard(
        self, store: PromptStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        copied: list[str] = []

        def fake_copy(_app: object, text: str) -> tuple[bool, None]:
            copied.append(text)
            return True, None

        monkeypatch.setattr(clipboard, "copy_text_to_clipboard", fake_copy)
        store.create("review", "body to copy")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            await pilot.press("c")
            await pilot.pause()

            assert app.screen is screen
        assert copied == ["body to copy"]


class TestVersions:
    """`v` opens the versions of the selected prompt."""

    async def test_enter_in_the_versions_view_inserts_that_version(
        self, store: PromptStore
    ) -> None:
        prompt = store.create("review", "first text")
        store.edit(prompt.prompt_id, body="second text")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            await _open(app, pilot, store)

            await pilot.press("v")
            await _settle(pilot)
            assert _is_active(app, PromptVersionsScreen)
            await pilot.press("down", "enter")
            await _settle(pilot)

        assert app.results == ["first text"]

    async def test_restore_shows_in_the_list_after_going_back(
        self, store: PromptStore
    ) -> None:
        prompt = store.create("review", "first text")
        store.edit(prompt.prompt_id, body="second text")
        app = _LibraryHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store)

            for keys in (("v",), ("down", "r"), ("enter",), ("escape",)):
                await pilot.press(*keys)
                await _settle(pilot)

            assert app.screen is screen
            [row] = screen.query(SavedPromptRow)
            assert row.render().plain.endswith("v3  2h ago")
            assert _plain(screen, "#prompt-library-preview") == "first text"
        assert app.results == []
