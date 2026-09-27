"""Versions view: every saved version of a prompt, and what changed in each.

`PromptVersionsScreen` lists the versions of one prompt, newest first. The
right pane shows the inline word diff from the version before the selected
one, or from a pinned compare base. The user can restore an old version,
which saves its body as a new version, or insert any version into the chat
input. Store calls and diffs run in worker threads.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, override

from textual import work
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

from deepagents_code.config import get_glyphs
from deepagents_code.data_db import DataDBError
from deepagents_code.prompt_store import PromptStoreError
from deepagents_code.sessions import format_timestamp
from deepagents_code.tui.modals.prompt_library.confirm import PromptConfirmScreen
from deepagents_code.tui.modals.prompt_library.diff_view import (
    format_word_diff_stats,
    render_word_diff,
)
from deepagents_code.word_diff import word_diff

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Click

    from deepagents_code.prompt_store import PromptStore, PromptSummary, PromptVersion
    from deepagents_code.word_diff import WordDiff


class VersionRow(Static):
    """One version in the list: its number and when it was saved."""

    DEFAULT_CSS = """
    VersionRow {
        height: 1;
        padding: 0 1;
    }
    """

    class Clicked(Message):
        """Sent when the row is clicked. `enter` still inserts the version."""

        def __init__(self, version: int) -> None:
            """Name the version of the clicked row."""
            super().__init__()
            self.version = version

    def __init__(self, version: PromptVersion) -> None:
        """Create the row for a version."""
        saved = format_timestamp(version.created_at)
        text = Content.assemble((f"v{version.version}", "bold"), f"  {saved}")
        super().__init__(text, classes="prompt-versions-row")
        self.version = version

    def on_click(self, event: Click) -> None:
        """Ask the screen to select this row."""
        event.stop()
        self.post_message(self.Clicked(self.version.version))


class PromptVersionsScreen(ModalScreen[str | None]):
    """Show the versions of a prompt, and dismiss with the body to insert."""

    BINDINGS: ClassVar[list[BindingType]] = [
        # Priority, so the list's own scroll bindings do not take the arrows.
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("escape", "cancel", "Back", show=False, priority=True),
        Binding("enter", "insert", "Insert", show=False),
        Binding("t", "toggle_text", "Text or diff", show=False),
        Binding("b", "pin_base", "Compare base", show=False),
        Binding("r", "restore", "Restore", show=False),
    ]

    CSS_PATH = "prompt_library.tcss"

    KEY_HINTS: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("Enter insert", "t text/diff", "b compare base", "r restore"),
        ("Esc back",),
    )
    """Footer hints, in two fixed lines like the library's."""

    def __init__(self, store: PromptStore, prompt: PromptSummary) -> None:
        """Create the view for one prompt.

        Args:
            store: Where the versions are read and restored.
            prompt: The prompt whose versions to show.
        """
        super().__init__()
        self._store = store
        self._prompt = prompt
        self._versions: list[PromptVersion] = []
        self._index = 0
        self._base: int | None = None
        self._full_text = False
        self._diffs: dict[tuple[int, int], WordDiff] = {}
        self._pending: set[tuple[int, int]] = set()

    @override
    def compose(self) -> ComposeResult:
        """Compose the version list, the diff pane, and the key help.

        Yields:
            Widgets for the versions view.
        """
        glyphs = get_glyphs()
        title = Content.assemble(self._prompt.name, ", versions")
        with Vertical():
            yield Static(title, id="prompt-versions-title")
            with Horizontal(id="prompt-versions-panes"):
                yield VerticalScroll(id="prompt-versions-list")
                with Vertical(id="prompt-versions-right"):
                    yield Static("", id="prompt-versions-header")
                    pane = VerticalScroll(
                        id="prompt-versions-body-pane", can_focus=False
                    )
                    with pane:
                        loading = Content(f"Loading versions{glyphs.ellipsis}")
                        yield Static(loading, id="prompt-versions-body")
            separator = f"  {glyphs.bullet}  "
            hints = "\n".join(separator.join(line) for line in self.KEY_HINTS)
            yield Static(Content(hints), id="prompt-versions-help")

    def on_mount(self) -> None:
        """Focus the list and start loading the versions."""
        self.query_one("#prompt-versions-list").focus()
        self._load()

    def on_version_row_clicked(self, event: VersionRow.Clicked) -> None:
        """Select the clicked row."""
        event.stop()
        numbers = [version.version for version in self._versions]
        if event.version in numbers:
            self._select(numbers.index(event.version))

    def action_move_up(self) -> None:
        """Select the newer version. The app routes `shift+tab` here too."""
        self._step(-1)

    def action_move_down(self) -> None:
        """Select the older version."""
        self._step(1)

    def action_cancel(self) -> None:
        """Clear a pinned compare base, or else go back to the library.

        The app calls this for `escape` while the view is the active screen.
        """
        if self._base is None:
            self.dismiss(None)
            return
        self._base = None
        self._show()

    def action_insert(self) -> None:
        """Dismiss with the body of the selected version."""
        if (version := self._target()) is not None:
            self.dismiss(version.body)

    def action_toggle_text(self) -> None:
        """Switch the right pane between the diff and the full text."""
        if self._target() is not None:
            self._full_text = not self._full_text
            self._show()

    def action_pin_base(self) -> None:
        """Pin the selected version as the compare base, or clear the pin."""
        if (version := self._target()) is not None:
            same = self._base == version.version
            self._base = None if same else version.version
            self._show()

    def action_restore(self) -> None:
        """Ask to save the selected version's body as a new version."""
        if (version := self._target()) is None:
            return
        latest = self._versions[0].version
        if version.version == latest:
            self.notify(f"v{latest} is already the latest version.")
            return

        def restore_if_confirmed(confirmed: bool | None) -> None:
            if confirmed:
                self._restore(version.version)

        question = Content.assemble(
            f"Save the text of v{version.version} as v{latest + 1} of ",
            (self._prompt.name, "bold"),
            "? Older versions do not change.",
        )
        confirm = PromptConfirmScreen("Restore version", question)
        self.app.push_screen(confirm, restore_if_confirmed)

    @work(exclusive=True, group="prompt-versions-load")
    async def _load(self, select: int | None = None) -> None:
        """Read the versions in a worker thread, then list them.

        Args:
            select: The version number to select. The newest by default.
        """
        rows = self.query_one("#prompt-versions-list", VerticalScroll)
        try:
            versions = await asyncio.to_thread(
                self._store.list_versions, self._prompt.prompt_id
            )
        except (DataDBError, PromptStoreError) as exc:
            self._versions = []
            await rows.remove_children()
            message = _reason(exc)
            if isinstance(exc, DataDBError):
                message = f"Could not load the versions: {message}"
            self._fill(Content(""), Content(message), error=True)
            return
        self._versions = versions
        await rows.remove_children()
        await rows.mount_all(VersionRow(version) for version in versions)
        numbers = [version.version for version in versions]
        self._select(numbers.index(select) if select in numbers else 0)

    @work(group="prompt-versions-restore")
    async def _restore(self, version: int) -> None:
        try:
            outcome = await asyncio.to_thread(
                self._store.restore, self._prompt.prompt_id, version
            )
        except (DataDBError, PromptStoreError) as exc:
            message = f"Could not restore v{version}: {_reason(exc)}"
            self.notify(message, severity="error", markup=False)
            return
        if outcome.added_version is None:
            self.notify("The latest version already has this text.")
            return
        self._load(select=outcome.added_version)

    @work(group="prompt-versions-diff")
    async def _compare(self, old: PromptVersion, new: PromptVersion) -> None:
        """Compute a diff in a worker thread, cache it, and refresh the pane."""
        key = (old.version, new.version)
        try:
            self._diffs[key] = await asyncio.to_thread(word_diff, old.body, new.body)
        finally:
            self._pending.discard(key)
        self._show()

    def _select(self, index: int) -> None:
        self._index = index
        chosen = self._selected()
        for row in self.query(VersionRow):
            row.set_class(row.version is chosen, "-selected")
            if row.version is chosen:
                row.scroll_visible(animate=False)
        self.query_one("#prompt-versions-body-pane").scroll_home(animate=False)
        self._show()

    def _show(self) -> None:
        """Fill the right pane for the selected version."""
        if (version := self._selected()) is None:
            return
        if (label := self._text_label(version)) is not None:
            self._fill(Content(label), Content(version.body))
            return
        old = self._compare_from()
        glyphs = get_glyphs()
        prefix = "" if self._base is None else "base "
        label = f"{prefix}v{old.version} {glyphs.arrow_right} v{version.version}"
        key = (old.version, version.version)
        if (diff := self._diffs.get(key)) is not None:
            stats = format_word_diff_stats(diff)
            self._fill(Content.assemble(label, "   ", stats), render_word_diff(diff))
            return
        comparing = Content.styled(f"Comparing versions{glyphs.ellipsis}", "dim")
        self._fill(Content(label), comparing)
        if key not in self._pending:
            self._pending.add(key)
            self._compare(old, version)

    def _text_label(self, version: PromptVersion) -> str | None:
        """Return the header when the pane shows the full text, or `None` for a diff.

        Returns:
            The header for the full text, or `None` when there is a diff to show.
        """
        number = version.version
        if self._full_text:
            return f"v{number}, full text"
        if self._base == number:
            return f"v{number}, compare base"
        if self._base is None and self._index == len(self._versions) - 1:
            return f"v{number}, first version"
        return None

    def _compare_from(self) -> PromptVersion:
        """Return the version that the diff to the selected version starts from.

        Returns:
            The pinned base, or else the version saved just before the selected one.
        """
        by_number = {saved.version: saved for saved in self._versions}
        if self._base is not None and self._base in by_number:
            return by_number[self._base]
        return self._versions[self._index + 1]

    def _fill(self, header: Content, body: Content, *, error: bool = False) -> None:
        self.query_one("#prompt-versions-header", Static).update(header)
        body_widget = self.query_one("#prompt-versions-body", Static)
        body_widget.update(body)
        body_widget.set_class(error, "-error")

    def _step(self, delta: int) -> None:
        index = self._index + delta
        if 0 <= index < len(self._versions):
            self._select(index)
        else:
            self.app.bell()

    def _target(self) -> PromptVersion | None:
        """Return the version an action applies to, ringing the bell if none.

        Returns:
            The selected version, or `None` when no version is listed.
        """
        version = self._selected()
        if version is None:
            self.app.bell()
        return version

    def _selected(self) -> PromptVersion | None:
        if self._index < len(self._versions):
            return self._versions[self._index]
        return None


def _reason(exc: DataDBError | PromptStoreError) -> str:
    return exc.reason if isinstance(exc, DataDBError) else str(exc)
