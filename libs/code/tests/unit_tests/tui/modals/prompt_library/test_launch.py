"""Tests for opening the prompt library from the real app."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from textual.screen import ModalScreen
from textual.widgets import Input, TextArea

from deepagents_code import data_db
from deepagents_code.app import DeepAgentsApp
from deepagents_code.prompt_store import PromptStore
from deepagents_code.tui.modals.prompt_clipboard import PromptClipboardScreen
from deepagents_code.tui.modals.prompt_editor import PromptEditorScreen
from deepagents_code.tui.modals.prompt_library import PromptLibraryScreen
from deepagents_code.tui.modals.prompt_library.confirm import PromptConfirmScreen
from deepagents_code.tui.modals.prompt_library.launch import (
    _insert_result,
    handle_ctrl_d,
    library_block_reason,
)
from deepagents_code.tui.modals.prompt_library.prompt_list import (
    SavedPromptList,
    SavedPromptRow,
)
from deepagents_code.tui.modals.prompt_library.versions import (
    PromptVersionsScreen,
    VersionRow,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from textual.pilot import Pilot


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PromptStore:
    """A store on `tmp_path` that the app opens as its `data.db`."""
    path = tmp_path / "data.db"
    monkeypatch.setattr(data_db, "default_data_db_path", lambda profile=None: path)  # noqa: ARG005  # same signature as the real one
    return PromptStore(path)


async def _until(pilot: Pilot[None], condition: Callable[[], bool]) -> None:
    """Pause until `condition` holds. Store calls run in worker threads."""
    deadline = asyncio.get_running_loop().time() + 5
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await pilot.pause(0.01)


async def _open_library(app: DeepAgentsApp, pilot: Pilot[None]) -> PromptLibraryScreen:
    await app._handle_command("/library")
    await _until(pilot, lambda: isinstance(app.screen, PromptLibraryScreen))
    screen = app.screen
    assert isinstance(screen, PromptLibraryScreen)
    # The screen is on the stack before it composes, so query without raising.
    await _until(
        pilot, lambda: any(lst.prompts for lst in screen.query(SavedPromptList))
    )
    return screen


def _selected_name(screen: PromptLibraryScreen) -> str:
    selected = screen.query_one(SavedPromptList).selected
    assert selected is not None
    return selected.name


class TestLibraryCommand:
    """`/library` opens the library when the chat input is free."""

    async def test_library_command_opens_the_library(self, store: PromptStore) -> None:
        store.create("review", "Review the diff.")
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            screen = await _open_library(app, pilot)

            assert [row.prompt.name for row in screen.query(SavedPromptRow)] == [
                "review"
            ]

    @pytest.mark.usefixtures("store")
    async def test_blocked_command_says_why_and_opens_nothing(self) -> None:
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._pending_approval_widget = MagicMock()

            with patch.object(app, "_mount_message", new=AsyncMock()) as mount:
                await app._handle_command("/library")
            await pilot.pause()

            assert not isinstance(app.screen, PromptLibraryScreen)
            mount.assert_awaited_once()
            assert mount.await_args is not None
            assert "approval" in str(mount.await_args.args[0].render()).lower()

    @pytest.mark.usefixtures("store")
    async def test_each_blocking_surface_is_named(self) -> None:
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            for attribute, expected in (
                ("_pending_approval_widget", "approval"),
                ("_pending_ask_user_widget", "question"),
                ("_pending_goal_review_widget", "goal review"),
            ):
                setattr(app, attribute, MagicMock())
                reason = library_block_reason(app)
                assert reason is not None
                assert expected in reason.lower()
                setattr(app, attribute, None)

            assert library_block_reason(app) is None

    async def test_chosen_prompt_lands_at_the_chat_input_cursor(
        self, store: PromptStore
    ) -> None:
        store.create("review", "Review the diff.")
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            chat = app._chat_input
            assert chat is not None
            assert chat._text_area is not None
            chat._text_area.insert("Please: ")

            await _open_library(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: not isinstance(app.screen, PromptLibraryScreen))
            await pilot.pause()

            assert chat._text_area.text == "Please: Review the diff."
            assert chat._text_area.has_focus

    async def test_h_lists_the_chat_input_history(self, store: PromptStore) -> None:
        store.create("review", "body")
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            chat = app._chat_input
            assert chat is not None
            chat._history.add("Explain this error")

            await _open_library(app, pilot)
            await pilot.press("h")
            await _until(pilot, lambda: isinstance(app.screen, PromptClipboardScreen))

            clipboard = app.screen
            assert isinstance(clipboard, PromptClipboardScreen)
            assert clipboard._prompts == ("Explain this error",)


class TestReverseNavigation:
    """The app's priority `shift+tab` reaches each new screen's `action_move_up`."""

    async def test_editor_moves_focus_from_the_body_to_the_name(
        self, store: PromptStore
    ) -> None:
        store.create("review", "body")
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            mode = app._approval_mode
            await _open_library(app, pilot)

            await pilot.press("n")
            await _until(pilot, lambda: isinstance(app.screen, PromptEditorScreen))
            editor = app.screen
            await pilot.press("tab")
            assert isinstance(app.focused, TextArea)

            await pilot.press("shift+tab")

            assert app.focused is editor.query_one("#prompt-name", Input)
            assert app._approval_mode == mode

    async def test_library_moves_the_selection_up(self, store: PromptStore) -> None:
        store.create("older", "o")
        store.create("newer", "n")
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = await _open_library(app, pilot)

            await pilot.press("down")
            assert _selected_name(screen) == "older"
            await pilot.press("shift+tab")

            assert _selected_name(screen) == "newer"

    async def test_versions_view_moves_the_selection_up(
        self, store: PromptStore
    ) -> None:
        prompt = store.create("review", "v1")
        store.edit(prompt.prompt_id, body="v2")
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_library(app, pilot)

            await pilot.press("v")
            await _until(pilot, lambda: bool(app.screen.query(VersionRow)))
            versions = app.screen
            assert isinstance(versions, PromptVersionsScreen)
            await pilot.press("down", "shift+tab")

            [selected] = [
                r for r in versions.query(VersionRow) if r.has_class("-selected")
            ]
            assert selected.version.version == 2


class TestBlockReason:
    """The reasons that need no running app."""

    def test_missing_chat_input_blocks(self) -> None:
        app = MagicMock()
        app._chat_input = None

        assert library_block_reason(app) == (
            "The prompt library needs the composer, which is not ready yet."
        )

    def test_open_dialog_blocks(self) -> None:
        app = MagicMock()
        app.screen = ModalScreen()

        assert library_block_reason(app) == (
            "Close the open dialog before opening the prompt library."
        )


class TestInsertResult:
    """The result callback copes with a chat input that went away."""

    def test_missing_chat_input_warns_without_markup(self) -> None:
        app = MagicMock()
        app._chat_input = None
        app.call_after_refresh.side_effect = lambda callback: callback()

        _insert_result(app, "[bold]text[/bold]")

        app.notify.assert_called_once()
        assert app.notify.call_args.kwargs["markup"] is False

    def test_unavailable_chat_input_warns_and_refocuses(self) -> None:
        app = MagicMock()
        app.call_after_refresh.side_effect = lambda callback: callback()
        app._chat_input.insert_at_cursor.return_value = False

        _insert_result(app, "text")

        app.notify.assert_called_once()
        assert app.notify.call_args.kwargs["markup"] is False
        app._chat_input.focus_input.assert_called_once_with()

    def test_cancel_only_refocuses_the_chat_input(self) -> None:
        app = MagicMock()
        app.call_after_refresh.side_effect = lambda callback: callback()

        _insert_result(app, None)

        app._chat_input.insert_at_cursor.assert_not_called()
        app._chat_input.focus_input.assert_called_once_with()


async def _open_editor(app: DeepAgentsApp, pilot: Pilot[None]) -> PromptEditorScreen:
    """Open `/library`, then the prompt editor on the first prompt."""
    await _open_library(app, pilot)
    await pilot.press("e")
    await _until(
        pilot,
        lambda: (
            isinstance(app.screen, PromptEditorScreen)
            and isinstance(app.focused, TextArea)
        ),
    )
    editor = app.screen
    assert isinstance(editor, PromptEditorScreen)
    return editor


class TestCtrlD:
    """`ctrl+d` never quits dcode while a prompt library screen is active."""

    async def test_body_editor_deletes_one_character(self, store: PromptStore) -> None:
        store.create("review", "abc")
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = await _open_editor(app, pilot)
            await pilot.press("right")

            with patch.object(app, "exit") as exit_mock:
                await pilot.press("ctrl+d")
                await pilot.pause()

            assert editor.body_editor.text == "ac"
            exit_mock.assert_not_called()

    @pytest.mark.parametrize(
        "keys",
        [("e", "shift+tab"), ("e", "end"), (), ("v",), ("d",)],
        ids=["name-field", "end-of-body", "library-list", "versions", "confirm"],
    )
    async def test_ctrl_d_does_not_quit(
        self, store: PromptStore, keys: tuple[str, ...]
    ) -> None:
        store.create("review", "abc")
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_library(app, pilot)
            for key in keys:
                await pilot.press(key)
                await pilot.pause(0.05)
            await pilot.pause()
            screen = app.screen

            with patch.object(app, "exit") as exit_mock:
                await pilot.press("ctrl+d")
                await pilot.pause()

            exit_mock.assert_not_called()
            assert app.screen is screen
        assert store.list_prompts()[0].body == "abc"

    def test_upstream_screens_keep_their_ctrl_d(self) -> None:
        app = MagicMock()
        app.screen_stack = [PromptClipboardScreen(("history",))]

        assert handle_ctrl_d(app) is False

    def test_no_screen_goes_on_as_before(self) -> None:
        app = MagicMock()
        app.screen_stack = []

        assert handle_ctrl_d(app) is False


_FEATURE_MODULES = (
    "deepagents_code.tui.modals.prompt_editor",
    "deepagents_code.tui.modals.prompt_library",
)


async def test_chat_input_ctrl_d_imports_nothing_from_the_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It still deletes right, as upstream does, without loading the feature."""
    for name in [name for name in sys.modules if name.startswith(_FEATURE_MODULES)]:
        monkeypatch.delitem(sys.modules, name)
    app = DeepAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app._chat_input
        assert chat is not None
        assert chat._text_area is not None
        chat._text_area.focus()
        await pilot.press("h", "i", "ctrl+a")

        with patch.object(app, "exit") as exit_mock:
            await pilot.press("ctrl+d")
            await pilot.pause()

        assert chat.value == "i"
        exit_mock.assert_not_called()
    assert [name for name in sys.modules if name.startswith(_FEATURE_MODULES)] == []
