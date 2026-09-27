"""Confirmation modal for prompt library actions, such as a delete."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from deepagents_code.prompt_store import PromptSummary


def delete_question(prompt: PromptSummary) -> Content:
    """Ask whether to delete a prompt, naming it and its number of versions.

    Returns:
        The question for `PromptConfirmScreen`.
    """
    # Versions are numbered from 1 and only a delete of the prompt removes
    # them, so the latest number is also the count.
    count = prompt.latest_version
    versions = "1 version" if count == 1 else f"{count} versions"
    tail = f" and its {versions}? This cannot be undone."
    return Content.assemble("Delete ", (prompt.name, "bold"), tail)


class PromptConfirmScreen(ModalScreen[bool]):
    """Ask a yes or no question. `enter` confirms and `escape` cancels.

    Modeled on `DeleteThreadConfirmScreen` in `tui/widgets/thread_selector.py`.
    Both keys are priority bindings, so they win over the screen underneath.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "confirm", "Confirm", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    PromptConfirmScreen {
        align: center middle;
    }

    PromptConfirmScreen > Vertical {
        width: 60;
        max-width: 90%;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    PromptConfirmScreen.-danger > Vertical {
        border: solid $error;
    }

    PromptConfirmScreen #prompt-confirm-title {
        text-style: bold;
        text-align: center;
    }

    PromptConfirmScreen #prompt-confirm-message {
        text-align: center;
        margin: 1 0;
    }

    PromptConfirmScreen #prompt-confirm-help {
        color: $text-muted;
        text-style: italic;
        text-align: center;
    }
    """

    def __init__(self, title: str, message: Content, *, danger: bool = False) -> None:
        """Create the modal.

        Args:
            title: A short name for the action, such as `Delete prompt`.
            message: The question. It is `Content`, so a prompt name in it is
                never parsed as markup.
            danger: Whether the action cannot be undone. The border turns red.
        """
        super().__init__(classes="-danger" if danger else None)
        self._title = title
        self._message = message

    @override
    def compose(self) -> ComposeResult:
        """Compose the title, the question, and the key help.

        Yields:
            Widgets for the confirmation.
        """
        with Vertical():
            yield Static(Content(self._title), id="prompt-confirm-title")
            yield Static(self._message, id="prompt-confirm-message")
            help_text = Content("Enter to confirm, Esc to cancel")
            yield Static(help_text, id="prompt-confirm-help")

    def action_confirm(self) -> None:
        """Answer yes."""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Answer no. The app calls this for `escape` too."""
        self.dismiss(False)
