"""The list of saved prompts in the prompt library, with its rows and filter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widget import Widget

from deepagents_code.config import get_glyphs
from deepagents_code.sessions import format_relative_timestamp

if TYPE_CHECKING:
    from textual.events import Click

    from deepagents_code.prompt_store import PromptSummary


def matches(prompt: PromptSummary, query: str) -> bool:
    """Check whether the name or latest body contains `query`, ignoring case.

    Returns:
        Whether the prompt matches. Every prompt matches an empty query.
    """
    needle = query.casefold()
    return needle in prompt.name.casefold() or needle in prompt.body.casefold()


class SavedPromptRow(Widget):
    """One saved prompt: its name, with its latest version and age at the right.

    A long name is cut short with an ellipsis, so the version and age always
    show. The name is user text, so it is never parsed as markup.
    """

    DEFAULT_CSS = """
    SavedPromptRow {
        height: 1;
        padding: 0 1;
    }
    """

    class Clicked(Message):
        """Sent when the row is clicked. `enter` still inserts the prompt."""

        def __init__(self, prompt_id: int) -> None:
            """Name the prompt of the clicked row."""
            super().__init__()
            self.prompt_id = prompt_id

    def __init__(self, prompt: PromptSummary) -> None:
        """Create the row for a prompt."""
        super().__init__(classes="prompt-library-row")
        self.prompt = prompt
        age = format_relative_timestamp(prompt.updated_at)
        self._details = Content.styled(f"  v{prompt.latest_version}  {age}", "dim")

    def render(self) -> Content:
        """Fit the name into the width that the version and age leave.

        Returns:
            The row text, one line as wide as the row.
        """
        width = max(1, self.size.width - self._details.cell_length)
        name = Content(self.prompt.name)
        if name.cell_length > width:
            # The glyph set, not Textual's built-in `…`, so ASCII terminals get `...`.
            ellipsis = Content(get_glyphs().ellipsis)
            name = name.truncate(max(0, width - ellipsis.cell_length)) + ellipsis
        return name.pad_right(max(0, width - name.cell_length)) + self._details

    def on_click(self, event: Click) -> None:
        """Ask the list to select this row."""
        event.stop()
        self.post_message(self.Clicked(self.prompt.prompt_id))


class SavedPromptList(VerticalScroll):
    """Rows of saved prompts. The filter hides rows, and one shown row is selected.

    Attributes:
        prompts: Every prompt, most recently updated first.
        shown: The prompts that match the filter, in the same order.
        query_text: The filter that `shown` reflects.
    """

    class Highlighted(Message):
        """Sent when the selected prompt or the shown prompts change."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002  # Textual widget constructor uses `id` parameter
        """Create an empty list."""
        super().__init__(id=id)
        self.prompts: list[PromptSummary] = []
        self.shown: list[PromptSummary] = []
        self.query_text = ""
        self._index = 0

    @property
    def selected(self) -> PromptSummary | None:
        """The selected prompt, or `None` when no prompt is shown."""
        return self.shown[self._index] if self._index < len(self.shown) else None

    async def show(
        self, prompts: list[PromptSummary], *, query: str, select: int | None
    ) -> None:
        """Replace the rows and filter them.

        Args:
            prompts: Every prompt, most recently updated first.
            query: The filter text.
            select: The prompt to select. `None` keeps the selected prompt.
        """
        await self.remove_children()
        await self.mount_all(SavedPromptRow(prompt) for prompt in prompts)
        self.prompts = prompts
        self.apply_filter(query, select=select)

    def apply_filter(self, query: str, *, select: int | None = None) -> None:
        """Show only the rows that match `query`.

        Args:
            query: The filter text.
            select: The prompt to select. `None` keeps the selected prompt if
                it still shows. Otherwise the first shown prompt is selected.
        """
        if select is None and (current := self.selected) is not None:
            select = current.prompt_id
        self.query_text = query
        self.shown = [prompt for prompt in self.prompts if matches(prompt, query)]
        ids = [prompt.prompt_id for prompt in self.shown]
        for row in self.query(SavedPromptRow):
            row.display = row.prompt.prompt_id in ids
        self._select(ids.index(select) if select in ids else 0)

    def move(self, delta: int) -> bool:
        """Move the selection by `delta` shown rows.

        Returns:
            `False`, without moving, when that would leave the shown rows.
        """
        index = self._index + delta
        if not 0 <= index < len(self.shown):
            return False
        self._select(index)
        return True

    def neighbor(self) -> int | None:
        """Return the prompt to select once the selected one is deleted.

        Returns:
            The id of the next shown prompt, else the previous one, else `None`.
        """
        ids = [prompt.prompt_id for prompt in self.shown]
        after, before = ids[self._index + 1 :], ids[: self._index]
        return after[0] if after else before[-1] if before else None

    def on_saved_prompt_row_clicked(self, event: SavedPromptRow.Clicked) -> None:
        """Select the clicked row."""
        event.stop()
        ids = [prompt.prompt_id for prompt in self.shown]
        if event.prompt_id in ids:
            self._select(ids.index(event.prompt_id))

    def _select(self, index: int) -> None:
        self._index = index
        chosen = self.selected
        for row in self.query(SavedPromptRow):
            is_chosen = row.prompt is chosen
            row.set_class(is_chosen, "-selected")
            if is_chosen:
                row.scroll_visible(animate=False)
        self.post_message(self.Highlighted())
