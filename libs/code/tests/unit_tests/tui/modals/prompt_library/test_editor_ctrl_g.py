"""Tests for editing the prompt body in the external editor with `ctrl+g`."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import TextArea

from deepagents_code import data_db
from deepagents_code.app import DeepAgentsApp
from deepagents_code.editor import ExternalEditorError
from deepagents_code.prompt_store import PromptStore
from deepagents_code.tui.modals.prompt_editor import PromptEditorScreen
from deepagents_code.tui.modals.prompt_library.launch import open_prompt_body_in_editor
from deepagents_code.tui.modals.prompt_library.prompt_list import SavedPromptList

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from textual.pilot import Pilot


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PromptStore:
    """A store on `tmp_path` with one prompt, which the app opens as `data.db`."""
    path = tmp_path / "data.db"
    monkeypatch.setattr(data_db, "default_data_db_path", lambda profile=None: path)  # noqa: ARG005  # same signature as the real one
    store = PromptStore(path)
    store.create("review", "original body")
    return store


async def _until(pilot: Pilot[None], condition: Callable[[], bool]) -> None:
    """Pause until `condition` holds. Store calls run in worker threads."""
    deadline = asyncio.get_running_loop().time() + 5
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await pilot.pause(0.01)


async def _edit_prompt(app: DeepAgentsApp, pilot: Pilot[None]) -> PromptEditorScreen:
    """Open `/library`, then the prompt editor on the saved prompt."""
    await app._handle_command("/library")
    await _until(
        pilot, lambda: any(lst.prompts for lst in app.screen.query(SavedPromptList))
    )
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


class TestCtrlG:
    """`ctrl+g` in the prompt body editor edits the body, not the chat input."""

    async def test_result_replaces_the_body_and_saves_as_a_version(
        self, store: PromptStore
    ) -> None:
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            chat = app._chat_input
            assert chat is not None
            assert chat._text_area is not None
            chat._text_area.insert("chat draft")
            editor = await _edit_prompt(app, pilot)

            with (
                patch.object(app, "suspend"),
                patch(
                    "deepagents_code.editor.open_in_editor", return_value="edited body"
                ) as open_in_editor,
            ):
                await pilot.press("ctrl+g")
                await pilot.pause()

            assert open_in_editor.call_args.args[0] == "original body"
            assert editor.body_editor.text == "edited body"
            assert editor.body_editor.has_focus
            assert chat._text_area.text == "chat draft"

            await pilot.press("ctrl+s")
            await _until(pilot, lambda: not isinstance(app.screen, PromptEditorScreen))

        [prompt] = store.list_prompts()
        assert (prompt.latest_version, prompt.body) == (2, "edited body")

    @pytest.mark.usefixtures("store")
    async def test_editor_failure_keeps_the_body(self) -> None:
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = await _edit_prompt(app, pilot)

            with (
                patch.object(app, "suspend"),
                patch(
                    "deepagents_code.editor.open_in_editor",
                    side_effect=ExternalEditorError("boom"),
                ),
                patch.object(app, "notify") as notify,
            ):
                await pilot.press("ctrl+g")
                await pilot.pause()

            notify.assert_called_once()
            assert "External editor failed" in notify.call_args.args[0]
            assert editor.body_editor.text == "original body"
            assert editor.body_editor.has_focus

    @pytest.mark.usefixtures("store")
    async def test_name_field_focus_still_edits_the_body(self) -> None:
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            chat = app._chat_input
            assert chat is not None
            assert chat._text_area is not None
            chat._text_area.insert("chat draft")
            editor = await _edit_prompt(app, pilot)
            await pilot.press("shift+tab")
            assert not editor.body_editor.has_focus

            with (
                patch.object(app, "suspend"),
                patch(
                    "deepagents_code.editor.open_in_editor", return_value="edited body"
                ) as open_in_editor,
            ):
                await pilot.press("ctrl+g")
                await pilot.pause()

            assert open_in_editor.call_args.args[0] == "original body"
            assert editor.body_editor.text == "edited body"
            assert editor.body_editor.has_focus
            assert chat._text_area.text == "chat draft"

    async def test_chat_input_keeps_ctrl_g_outside_the_prompt_editor(
        self, store: PromptStore
    ) -> None:
        app = DeepAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            chat = app._chat_input
            assert chat is not None
            assert chat._text_area is not None
            chat._text_area.insert("chat draft")
            chat.focus_input()
            await pilot.pause()

            with (
                patch.object(app, "suspend"),
                patch(
                    "deepagents_code.editor.open_in_editor", return_value="new draft"
                ) as open_in_editor,
            ):
                await pilot.press("ctrl+g")
                await pilot.pause()

            assert open_in_editor.call_args.args[0] == "chat draft"
            assert chat._text_area.text == "new draft"
        assert store.list_prompts()[0].body == "original body"


_FEATURE_MODULES = (
    "deepagents_code.tui.modals.prompt_editor",
    "deepagents_code.tui.modals.prompt_library",
)


async def test_ctrl_g_elsewhere_imports_nothing_from_the_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the prompt editor module loads, no prompt editor can be open."""
    for name in [name for name in sys.modules if name.startswith(_FEATURE_MODULES)]:
        monkeypatch.delitem(sys.modules, name)
    app = DeepAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app._chat_input
        assert chat is not None
        chat.focus_input()
        await pilot.pause()

        with (
            patch.object(app, "suspend"),
            patch("deepagents_code.editor.open_in_editor", return_value="new draft"),
        ):
            await pilot.press("ctrl+g")
            await pilot.pause()

        assert chat._text_area is not None
        assert chat._text_area.text == "new draft"
    assert [name for name in sys.modules if name.startswith(_FEATURE_MODULES)] == []


async def test_app_without_screens_goes_on_as_before() -> None:
    """`action_open_editor` can run before a screen is pushed."""
    app = MagicMock()
    app.screen_stack = []

    assert await open_prompt_body_in_editor(app) is False
    app._open_text_area_in_editor.assert_not_called()
