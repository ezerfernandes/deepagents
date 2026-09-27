"""Everything the app calls to open the prompt library.

`app.py` reaches this module through one marked branch in `_handle_command`,
so the fork adds only a few lines to that upstream file. The functions here
read private app attributes, such as `_chat_input`. Where one mirrors app code,
its docstring names the upstream function, so a merge of upstream changes can
compare the two. A rename upstream breaks a test here, not the merge.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from textual.screen import ModalScreen
from textual.widgets import Input, TextArea

if TYPE_CHECKING:
    from textual.screen import Screen

    from deepagents_code.app import DeepAgentsApp


def library_block_reason(app: DeepAgentsApp) -> str | None:
    """Return why the prompt library cannot open, or `None` if it can.

    Mirrors `DeepAgentsApp._prompt_clipboard_block_reason`: the library needs
    the chat input, and nothing else may own the keyboard.

    Returns:
        A reason to show the user, or `None`.
    """
    if app._chat_input is None:
        return "The prompt library needs the composer, which is not ready yet."
    if isinstance(app.screen, ModalScreen):
        return "Close the open dialog before opening the prompt library."
    if app._pending_approval_widget is not None:
        return "Answer the pending approval before opening the prompt library."
    if app._pending_ask_user_widget is not None:
        return "Answer the pending question before opening the prompt library."
    if app._pending_goal_review_widget is not None:
        return "Finish the goal review before opening the prompt library."
    return None


async def open_prompt_library(app: DeepAgentsApp) -> None:
    """Open the prompt library, or say why it cannot open.

    Like the `/prompts` branch of `DeepAgentsApp._handle_command`, a blocked
    command mounts its reason, so it never looks broken.
    """
    if (reason := library_block_reason(app)) is not None:
        from deepagents_code.tui.widgets.messages import AppMessage

        await app._mount_message(AppMessage(reason))
        return
    from deepagents_code.data_db import default_data_db_path
    from deepagents_code.prompt_store import PromptStore
    from deepagents_code.tui.modals.prompt_library import PromptLibraryScreen

    store = PromptStore(default_data_db_path())
    # The block check above guarantees a chat input. The library reads its
    # history through the bound method, so it never reaches into the app.
    chat_input = app._chat_input
    recent_prompts = None if chat_input is None else chat_input.recent_prompts
    screen = PromptLibraryScreen(store, recent_prompts=recent_prompts)
    app.push_screen(screen, partial(_insert_result, app))


async def open_prompt_body_in_editor(app: DeepAgentsApp) -> bool:
    """Edit the prompt body in `$VISUAL` or `$EDITOR` while the prompt editor is open.

    `ctrl+g` is an app priority binding, so `action_open_editor` asks here
    first. It asks only once the prompt editor module is loaded, because no
    prompt editor can be open before that, so `ctrl+g` elsewhere imports
    nothing from the prompt library. The body is edited from either field,
    and focus returns to the prompt body editor afterward. The call matches
    how the action opens the goal review and ask-user editors: an empty
    result is kept, and a failure is reported.

    Returns:
        `True` when the prompt editor took the key. `False` lets the app
        handle `ctrl+g` as before.
    """
    from deepagents_code.tui.modals.prompt_editor import PromptEditorScreen

    screen = _top_screen(app)
    if not isinstance(screen, PromptEditorScreen):
        return False
    body = screen.body_editor
    await app._open_text_area_in_editor(
        body,
        body.text,
        allow_empty=True,
        raise_editor_errors=True,
        restore_focus=body.focus,
    )
    return True


def handle_ctrl_d(app: DeepAgentsApp) -> bool:
    """Keep `ctrl+d` from quitting dcode while a prompt library screen is open.

    `ctrl+d` is an app priority binding. Upstream it deletes forward in the
    chat input and inline prompts (`DeepAgentsApp._ctrl_d_delete_target`)
    and quits everywhere else, so an unsaved prompt would be lost. On these
    screens it deletes the selection or the character right of the cursor in
    a focused field, and otherwise does nothing.

    Returns:
        `True` when a prompt library screen took the key. `False` lets
        `action_quit_app` go on as before, also on upstream screens such as
        the history modal that `h` opens.
    """
    from deepagents_code.tui.modals.prompt_editor import PromptEditorScreen
    from deepagents_code.tui.modals.prompt_library import PromptLibraryScreen
    from deepagents_code.tui.modals.prompt_library.confirm import PromptConfirmScreen
    from deepagents_code.tui.modals.prompt_library.versions import (
        PromptVersionsScreen,
    )

    screens = (
        PromptLibraryScreen,
        PromptVersionsScreen,
        PromptEditorScreen,
        PromptConfirmScreen,
    )
    if not isinstance(_top_screen(app), screens):
        return False
    focused = app.focused
    if isinstance(focused, (Input, TextArea)):
        focused.action_delete_right()
    return True


def _top_screen(app: DeepAgentsApp) -> Screen[object] | None:
    """Return the active screen, or `None` while no screen is pushed.

    `app.screen` raises on an empty stack, and the key actions can run then.

    Returns:
        The screen on top of the stack, if any.
    """
    stack = app.screen_stack
    return stack[-1] if stack else None


def _insert_result(app: DeepAgentsApp, body: str | None) -> None:
    """Insert the chosen prompt at the chat input cursor, then focus it.

    Mirrors `handle_result` in `DeepAgentsApp._open_prompt_clipboard_modal`.
    The work runs after a refresh, so the library unwinds first.
    """

    def apply_result() -> None:
        chat_input = app._chat_input
        if chat_input is None:
            if body is not None:
                app.notify(
                    "Could not insert the prompt: the composer is gone",
                    severity="warning",
                    markup=False,
                )
            return
        if body is not None and not chat_input.insert_at_cursor(body):
            app.notify(
                "Could not insert the prompt: the composer is unavailable",
                severity="warning",
                markup=False,
            )
        chat_input.focus_input()

    app.call_after_refresh(apply_result)
