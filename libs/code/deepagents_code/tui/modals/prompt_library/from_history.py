"""Save a prompt from history into the library.

`h` in the library shows the history modal of `/prompts`, which this module
reuses unchanged. A chosen prompt then opens the prompt editor with that text
as the body and a suggested name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents_code.prompt_store import MAX_NAME_LENGTH
from deepagents_code.tui.modals.prompt_editor import NewPrompt
from deepagents_code.tui.widgets.prompt_search import prompt_title

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App


def draft_from_history(text: str) -> NewPrompt:
    """Return the editor's starting values for a prompt from history.

    The suggested name is the first line of the prompt, with each run of
    whitespace made one space, cut to the longest name the library allows.

    Returns:
        The name and body to start the editor with.
    """
    name = " ".join(prompt_title(text).split())[:MAX_NAME_LENGTH].rstrip()
    return NewPrompt(name=name, body=text)


def choose_from_history(
    app: App[object], prompts: tuple[str, ...], on_chosen: Callable[[str], None]
) -> None:
    """Show the history modal, and hand the chosen prompt to `on_chosen`.

    `on_chosen` runs after a refresh, so the history modal unwinds before the
    next screen opens. A cancel calls nothing.

    Args:
        app: The app that shows the modal.
        prompts: Submitted prompts, newest first.
        on_chosen: Receives the text of the chosen prompt.
    """
    from deepagents_code.tui.modals.prompt_clipboard import PromptClipboardScreen

    def after_history(text: str | None) -> None:
        if text is not None:
            app.call_after_refresh(on_chosen, text)

    app.push_screen(PromptClipboardScreen(prompts), after_history)
