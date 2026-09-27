"""Modal that creates a saved prompt or saves a new version of one.

The prompt library opens it to create and edit prompts, including a prompt
taken from history. Every store call runs in a worker thread. A failed save
shows the store's message in the status line and keeps what the user typed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, override

from textual import work
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TextArea

from deepagents_code.config import get_glyphs
from deepagents_code.data_db import DataDBError
from deepagents_code.prompt_store import PromptStoreError, PromptSummary

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from deepagents_code.prompt_store import EditOutcome, PromptStore

_DISCARD_WARNING = "Unsaved changes. Press Esc again to discard."
_NO_CHANGES = "No changes to save"


@dataclass(frozen=True, slots=True, kw_only=True)
class NewPrompt:
    """Starting values for a prompt that the editor creates.

    Attributes:
        name: The starting name, such as one suggested from the body.
        body: The starting body, such as a prompt from history.
    """

    name: str = ""
    body: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class EditorResult:
    """What a save in the prompt editor did.

    Attributes:
        prompt: The prompt after the save.
        outcome: What changed in an edited prompt, or `None` when the editor
            created the prompt.
    """

    prompt: PromptSummary
    outcome: EditOutcome | None = None


class PromptEditorScreen(ModalScreen[EditorResult | None]):
    """Edit the name and body of a prompt, and save both with `ctrl+s`."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS_PATH = "prompt_editor.tcss"

    def __init__(self, store: PromptStore, target: NewPrompt | PromptSummary) -> None:
        """Open the editor on a new prompt or on a saved one.

        Args:
            store: Where the prompt is saved.
            target: The starting values of a new prompt, or the saved prompt
                to edit, starting from its latest version.
        """
        super().__init__()
        self._store = store
        self._target = target
        self._baseline = (target.name, target.body)
        self._warned: tuple[str, str] | None = None
        self._saving = False

    @override
    def compose(self) -> ComposeResult:
        """Compose the title, prompt name field, prompt body editor, and help.

        Yields:
            Widgets for the prompt editor.
        """
        target = self._target
        with Vertical():
            yield Static(self._title(), id="prompt-editor-title")
            yield Static("Name", classes="prompt-editor-label")
            yield Input(target.name, placeholder="Prompt name", id="prompt-name")
            yield Static("", id="prompt-editor-status")
            yield Static("Body", classes="prompt-editor-label")
            yield TextArea(target.body, id="prompt-body")
            bullet = get_glyphs().bullet
            keys = ("Ctrl+S save", "Ctrl+G external editor", "Esc cancel")
            help_text = f"  {bullet}  ".join(keys)
            yield Static(Content(help_text), id="prompt-editor-help")

    def on_mount(self) -> None:
        """Record the starting values and focus the field to change first."""
        # The widgets may normalize the text, such as mixed line endings, so
        # changes are measured against what they show rather than the target.
        self._baseline = self._values()
        editing = isinstance(self._target, PromptSummary)
        (self.body_editor if editing else self._name_field()).focus()

    def action_save(self) -> None:
        """Save in a worker. An edit without changes only says so."""
        if self._saving:
            return
        values = self._values()
        if isinstance(self._target, PromptSummary) and values == self._baseline:
            self._show_status(_NO_CHANGES)
            return
        self._saving = True
        self._save(*values)

    def action_cancel(self) -> None:
        """Close the editor, asking for a second `escape` before losing changes.

        The app calls this for `escape` while the editor is the active screen.
        """
        values = self._values()
        if values in {self._baseline, self._warned}:
            self.dismiss(None)
            return
        self._show_status(_DISCARD_WARNING)
        self._warned = values

    def action_move_up(self) -> None:
        """Move focus back, for the `shift+tab` that the app routes here."""
        self.focus_previous()

    @work(group="prompt-editor-save")
    async def _save(self, name: str, body: str) -> None:
        try:
            result = await asyncio.to_thread(self._write, name, body)
        except PromptStoreError as exc:
            self._show_status(str(exc), error=True)
        except DataDBError as exc:
            self._show_status(f"Could not save the prompt: {exc.reason}.", error=True)
        else:
            if result is None:
                self._show_status(_NO_CHANGES)
            else:
                self.dismiss(result)
        finally:
            self._saving = False

    def _write(self, name: str, body: str) -> EditorResult | None:
        """Send the values to the store. This runs in a worker thread.

        An edit sends only the fields that changed, so it keeps a rename or a
        new version saved from another window in the meantime.

        Returns:
            What the save did, or `None` when the store found nothing to change.
        """
        target = self._target
        if isinstance(target, NewPrompt):
            return EditorResult(prompt=self._store.create(name, body))
        start_name, start_body = self._baseline
        outcome = self._store.edit(
            target.prompt_id,
            name=None if name == start_name else name,
            body=None if body == start_body else body,
        )
        if not outcome.changed:
            return None
        return EditorResult(prompt=outcome.prompt, outcome=outcome)

    def _title(self) -> Content:
        target = self._target
        if isinstance(target, NewPrompt):
            return Content("New prompt")
        version = f", from v{target.latest_version}"
        return Content.assemble("Edit ", target.name, version)

    def _show_status(self, message: str, *, error: bool = False) -> None:
        self._warned = None
        status = self.query_one("#prompt-editor-status", Static)
        status.update(Content(message))
        status.set_class(error, "-error")

    def _values(self) -> tuple[str, str]:
        return self._name_field().value, self.body_editor.text

    def _name_field(self) -> Input:
        return self.query_one("#prompt-name", Input)

    @property
    def body_editor(self) -> TextArea:
        """The prompt body editor, which `ctrl+g` opens in the external editor."""
        return self.query_one("#prompt-body", TextArea)
