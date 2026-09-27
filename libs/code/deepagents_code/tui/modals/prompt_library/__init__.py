"""Prompt library: browse saved prompts, manage them, and insert one.

`PromptLibraryScreen` lists the prompts in `data.db` beside a preview of the
selected one. From the list the user creates, edits, deletes, copies, and
inserts prompts, opens the versions of one, and saves one from history. Every
store call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, override

from textual import work
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from deepagents_code.config import get_glyphs
from deepagents_code.data_db import DataDBError
from deepagents_code.prompt_store import PromptStoreError
from deepagents_code.tui.modals.prompt_editor import NewPrompt, PromptEditorScreen
from deepagents_code.tui.modals.prompt_library.confirm import (
    PromptConfirmScreen,
    delete_question,
)
from deepagents_code.tui.modals.prompt_library.from_history import (
    choose_from_history,
    draft_from_history,
)
from deepagents_code.tui.modals.prompt_library.prompt_list import (
    SavedPromptList,
    matches,
)
from deepagents_code.tui.modals.prompt_library.versions import PromptVersionsScreen

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult

    from deepagents_code.prompt_store import PromptStore, PromptSummary
    from deepagents_code.tui.modals.prompt_editor import EditorResult

__all__ = ["PromptLibraryScreen"]


class PromptLibraryScreen(ModalScreen[str | None]):  # noqa: RUF067  # libs/code/AGENTS.md puts a component's root class in its `__init__.py`
    """List saved prompts, and dismiss with the body of the one to insert."""

    BINDINGS: ClassVar[list[BindingType]] = [
        # Priority, so the list's own scroll bindings do not take the arrows.
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("escape", "cancel", "Close", show=False, priority=True),
        # Not priority, so the filter field keeps every key typed into it.
        Binding("enter", "insert", "Insert", show=False),
        Binding("slash", "focus_filter", "Filter", show=False),
        Binding("n", "new", "New", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("v", "versions", "Versions", show=False),
        Binding("h", "from_history", "From history", show=False),
        Binding("c", "copy", "Copy", show=False),
        Binding("d", "delete", "Delete", show=False),
    ]

    CSS_PATH = "prompt_library.tcss"

    KEY_HINTS: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("Enter insert", "/ filter", "v versions", "c copy"),
        ("n new", "e edit", "h from history", "d delete", "Esc close"),
    )
    """Footer hints, one line for using prompts and one for managing them.

    Two fixed lines keep a narrow modal from wrapping inside a hint.
    """

    def __init__(
        self,
        store: PromptStore,
        *,
        recent_prompts: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        """Create the screen for the prompts in a store.

        Args:
            store: Where the prompts are read and saved.
            recent_prompts: Returns the prompts from history, newest first,
                for `h`. Without it, `h` only says that there is no history.
        """
        super().__init__()
        self._store = store
        self._recent_prompts = recent_prompts
        self._error: str | None = None

    @override
    def compose(self) -> ComposeResult:
        """Compose the filter field, the list, the preview, and the key help.

        Yields:
            Widgets for the prompt library.
        """
        glyphs = get_glyphs()
        with Vertical():
            yield Static("Prompt library", id="prompt-library-title")
            yield Input(
                placeholder="Filter by name or text", id="prompt-library-filter"
            )
            with Horizontal(id="prompt-library-panes"):
                with Vertical(id="prompt-library-left"):
                    loading = f"Loading saved prompts{glyphs.ellipsis}"
                    yield Static(Content(loading), id="prompt-library-message")
                    yield SavedPromptList(id="prompt-library-list")
                with VerticalScroll(id="prompt-library-preview-pane", can_focus=False):
                    yield Static("", id="prompt-library-preview")
            separator = f"  {glyphs.bullet}  "
            hints = "\n".join(separator.join(line) for line in self.KEY_HINTS)
            yield Static(Content(hints), id="prompt-library-help")

    def on_mount(self) -> None:
        """Focus the list and start loading the prompts."""
        self._list().focus()
        self._load()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter the list as the user types."""
        event.stop()
        self._sync_filter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Move from the filter field back to the list."""
        event.stop()
        self._list().focus()

    def on_saved_prompt_list_highlighted(
        self, event: SavedPromptList.Highlighted
    ) -> None:
        """Show the selected prompt, or say why no prompt shows."""
        event.stop()
        prompt_list = self._list()
        selected = prompt_list.selected
        body = "" if selected is None else selected.body
        self.query_one("#prompt-library-preview", Static).update(Content(body))
        self.query_one("#prompt-library-preview-pane").scroll_home(animate=False)
        if self._error is not None:
            text = self._error
        elif not prompt_list.prompts:
            text = "No saved prompts yet. Press n to create one."
        else:
            text = "" if prompt_list.shown else "No matching prompts."
        message = self.query_one("#prompt-library-message", Static)
        message.update(Content(text))
        message.display = bool(text)
        message.set_class(self._error is not None, "-error")

    def action_move_up(self) -> None:
        """Select the previous prompt. The app routes `shift+tab` here too."""
        self._step(-1)

    def action_move_down(self) -> None:
        """Select the next prompt, or leave the filter field for the list."""
        if self._filter_field().has_focus:
            self._list().focus()
        else:
            self._step(1)

    def action_cancel(self) -> None:
        """Clear the filter field if it has focus, or else close the library.

        The app calls this for `escape` while the library is the active screen.
        """
        field = self._filter_field()
        if not field.has_focus:
            self.dismiss(None)
            return
        field.value = ""
        self._sync_filter()
        self._list().focus()

    def action_focus_filter(self) -> None:
        """Move focus to the filter field."""
        self._filter_field().focus()

    def action_insert(self) -> None:
        """Dismiss with the latest body of the selected prompt."""
        if (prompt := self._target()) is not None:
            self.dismiss(prompt.body)

    def action_new(self) -> None:
        """Open the prompt editor on a new prompt."""
        self._open_editor(NewPrompt())

    def action_edit(self) -> None:
        """Open the prompt editor on the selected prompt."""
        if (prompt := self._target()) is not None:
            self._open_editor(prompt)

    def action_versions(self) -> None:
        """Open the versions of the selected prompt."""
        if (prompt := self._target()) is None:
            return

        def after_versions(body: str | None) -> None:
            if body is not None:
                # Let the versions view unwind before the library closes too.
                self.call_after_refresh(self._close_with, body)
            else:
                # A restore in the versions view may have added a version.
                self._load(select=prompt.prompt_id)

        versions = PromptVersionsScreen(self._store, prompt)
        self.app.push_screen(versions, after_versions)

    def action_from_history(self) -> None:
        """Pick a prompt from history, then open the prompt editor with it."""
        prompts = self._recent_prompts() if self._recent_prompts else ()
        if not prompts:
            self.notify("No prompts in history yet.")
            return
        choose_from_history(self.app, prompts, self._save_from_history)

    def _save_from_history(self, text: str) -> None:
        self._open_editor(draft_from_history(text))

    def action_copy(self) -> None:
        """Copy the latest body of the selected prompt to the clipboard."""
        if (prompt := self._target()) is None:
            return
        from deepagents_code.clipboard import copy_text_with_feedback

        copy_text_with_feedback(
            self.app,
            prompt.body,
            failure_noun="prompt",
            success_message="Prompt copied to clipboard",
        )

    def action_delete(self) -> None:
        """Ask to delete the selected prompt, then delete it in a worker."""
        if (prompt := self._target()) is None:
            return
        neighbor = self._list().neighbor()

        def delete_if_confirmed(confirmed: bool | None) -> None:
            if confirmed:
                self._delete(prompt, neighbor)

        question = delete_question(prompt)
        confirm = PromptConfirmScreen("Delete prompt", question, danger=True)
        self.app.push_screen(confirm, delete_if_confirmed)

    def _close_with(self, body: str) -> None:
        """Dismiss with `body`, without handing back the awaitable.

        `call_after_refresh` awaits what its callback returns, and awaiting a
        dismiss from this screen's own message pump never finishes.
        """
        self.dismiss(body)

    def _open_editor(self, target: NewPrompt | PromptSummary) -> None:
        editor = PromptEditorScreen(self._store, target)
        self.app.push_screen(editor, self._on_editor_closed)

    def _on_editor_closed(self, result: EditorResult | None) -> None:
        if result is not None:
            self._load(select=result.prompt.prompt_id)

    @work(exclusive=True, group="prompt-library-load")
    async def _load(self, select: int | None = None) -> None:
        """Read the prompts in a worker thread, then show them.

        Args:
            select: The prompt to select, such as one just saved. The filter
                is cleared if it would hide this prompt.
        """
        try:
            prompts = await asyncio.to_thread(self._store.list_prompts)
        except (DataDBError, PromptStoreError) as exc:
            prompts = []
            self._error = "\n".join(
                (
                    "Could not load saved prompts.",
                    f"Reason: {self._reason(exc)}",
                    f"Database: {self._store.path}",
                )
            )
        else:
            self._error = None
        field = self._filter_field()
        if any(p.prompt_id == select and not matches(p, field.value) for p in prompts):
            field.value = ""
        await self._list().show(prompts, query=field.value, select=select)

    @work(group="prompt-library-delete")
    async def _delete(self, prompt: PromptSummary, neighbor: int | None) -> None:
        try:
            await asyncio.to_thread(self._store.delete, prompt.prompt_id)
        except (DataDBError, PromptStoreError) as exc:
            message = f"Could not delete the prompt: {self._reason(exc)}"
            self.notify(message, severity="error", markup=False)
        self._load(select=neighbor)

    def _sync_filter(self) -> None:
        """Apply a filter edit whose `Changed` message has not arrived yet."""
        query = self._filter_field().value
        if query != self._list().query_text:
            self._list().apply_filter(query)

    def _step(self, delta: int) -> None:
        self._sync_filter()
        if not self._list().move(delta):
            self.app.bell()

    def _target(self) -> PromptSummary | None:
        """Return the prompt an action applies to, ringing the bell if none.

        Returns:
            The selected prompt after any pending filter edit, or `None`.
        """
        self._sync_filter()
        prompt = self._list().selected
        if prompt is None:
            self.app.bell()
        return prompt

    def _list(self) -> SavedPromptList:
        return self.query_one("#prompt-library-list", SavedPromptList)

    def _filter_field(self) -> Input:
        return self.query_one("#prompt-library-filter", Input)

    @staticmethod
    def _reason(exc: DataDBError | PromptStoreError) -> str:
        return exc.reason if isinstance(exc, DataDBError) else str(exc)
