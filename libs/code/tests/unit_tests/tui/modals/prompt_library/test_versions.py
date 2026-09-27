"""Tests for the versions view of a saved prompt."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container
from textual.widgets import Static

from deepagents_code._env_vars import UI_CHARSET_MODE
from deepagents_code.config import UNICODE_GLYPHS, reset_glyphs_cache
from deepagents_code.data_db import DataDBUnavailableError
from deepagents_code.prompt_store import PromptStore, PromptSummary
from deepagents_code.tui.modals.prompt_library import versions as versions_module
from deepagents_code.tui.modals.prompt_library.confirm import PromptConfirmScreen
from deepagents_code.tui.modals.prompt_library.diff_view import (
    DELETE_STYLE,
    INSERT_STYLE,
)
from deepagents_code.tui.modals.prompt_library.versions import (
    PromptVersionsScreen,
    VersionRow,
)
from deepagents_code.word_diff import word_diff

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from textual.content import Content
    from textual.pilot import Pilot
    from textual.style import Style
    from textual.widget import Widget

    from deepagents_code.prompt_store import PromptVersion
    from deepagents_code.word_diff import WordDiff

V1 = "Review this branch."
V2 = "Review this branch and list bugs."
V3 = "Review the current branch and list bugs by severity."
ARROW = UNICODE_GLYPHS.arrow_right


class _VersionsHost(App[None]):
    """Minimal host that records the versions view result.

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

    def open(self, store: PromptStore, prompt: PromptSummary) -> PromptVersionsScreen:
        screen = PromptVersionsScreen(store, prompt)
        self.push_screen(screen, self.results.append)
        return screen

    def action_reverse_nav(self) -> None:
        move_up = getattr(self.screen, "action_move_up", None)
        if move_up is not None:
            move_up()


class _FailingReloadStore(PromptStore):
    """Store whose `list_versions` fails after the first call."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.lists = 0

    def list_versions(self, prompt_id: int) -> list[PromptVersion]:
        self.lists += 1
        if self.lists > 1:
            raise DataDBUnavailableError(self.path, "busy")
        return super().list_versions(prompt_id)


@pytest.fixture(autouse=True)
def _unicode_glyphs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Render with the Unicode glyphs, whatever charset the terminal has."""
    monkeypatch.setenv(UI_CHARSET_MODE, "unicode")
    reset_glyphs_cache()
    yield
    reset_glyphs_cache()


@pytest.fixture
def store(tmp_path: Path) -> PromptStore:
    return PromptStore(tmp_path / "data.db")


@pytest.fixture
def prompt(store: PromptStore) -> PromptSummary:
    """A prompt at three versions."""
    created = store.create("code-review", V1)
    store.edit(created.prompt_id, body=V2)
    return store.edit(created.prompt_id, body=V3).prompt


async def _settle(pilot: Pilot[None]) -> None:
    """Wait until no worker runs, since one store call can start the next."""
    await pilot.pause()
    while pilot.app.workers:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def _open(
    app: _VersionsHost, pilot: Pilot[None], store: PromptStore, prompt: PromptSummary
) -> PromptVersionsScreen:
    screen = app.open(store, prompt)
    await _settle(pilot)
    return screen


async def _press(pilot: Pilot[None], *keys: str) -> None:
    await pilot.press(*keys)
    await _settle(pilot)


def _content(screen: Widget, selector: str) -> Content:
    return cast("Content", screen.query_one(selector, Static).render())


def _header(screen: PromptVersionsScreen) -> str:
    return _content(screen, "#prompt-versions-header").plain


def _body(screen: PromptVersionsScreen) -> Content:
    return _content(screen, "#prompt-versions-body")


def _runs(content: Content) -> list[tuple[str, str | Style]]:
    return [
        (content.plain[span.start : span.end], span.style) for span in content.spans
    ]


def _selected(screen: PromptVersionsScreen) -> int:
    [row] = [row for row in screen.query(VersionRow) if row.has_class("-selected")]
    return row.version.version


class TestDiff:
    """The right pane shows what changed in the selected version."""

    async def test_opens_on_the_newest_version_with_the_diff_from_the_last(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            assert _selected(screen) == 3
            assert [row.version.version for row in screen.query(VersionRow)] == [
                3,
                2,
                1,
            ]
            assert _header(screen) == f"v2 {ARROW} v3   +4 words -1 word"
            assert (
                _body(screen).plain
                == "Review thisthe current branch and list bugs by severity."
            )
            assert _runs(_body(screen)) == [
                ("this", DELETE_STYLE),
                ("the current", INSERT_STYLE),
                (" by severity", INSERT_STYLE),
            ]

    async def test_first_version_shows_its_full_text(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "down")

            assert _selected(screen) == 1
            assert _header(screen) == "v1, first version"
            assert _body(screen).plain == V1
            assert _body(screen).spans == []

    async def test_t_switches_between_the_diff_and_the_full_text(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "t")

            assert _header(screen) == "v3, full text"
            assert _body(screen).plain == V3

            await _press(pilot, "t")

            assert _header(screen).startswith(f"v2 {ARROW} v3")

    async def test_shift_tab_selects_the_newer_version(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "up", "down", "down", "shift+tab")

            assert _selected(screen) == 2
            assert _header(screen) == f"v1 {ARROW} v2   +3 words"

    async def test_click_selects_a_version_without_inserting(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await pilot.click(list(screen.query(VersionRow))[2])
            await _settle(pilot)

            assert _selected(screen) == 1
            assert app.results == []

    async def test_each_diff_is_computed_once(
        self, store: PromptStore, prompt: PromptSummary, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pair asked for again, even while it is computing, is not recomputed."""
        pairs: list[tuple[str, str]] = []
        release = threading.Event()

        def held_diff(old: str, new: str) -> WordDiff:
            pairs.append((old, new))
            release.wait(timeout=5)
            return word_diff(old, new)

        monkeypatch.setattr(versions_module, "word_diff", held_diff)
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = app.open(store, prompt)
            while not screen.query(VersionRow):
                await pilot.pause()

            await pilot.press("down", "up", "down", "up")
            release.set()
            await _settle(pilot)
            await _press(pilot, "down", "up")

            assert _header(screen).startswith(f"v2 {ARROW} v3")
        assert sorted(pairs) == sorted([(V2, V3), (V1, V2)])


class TestCompareBase:
    """`b` pins a version, so any two versions can be compared."""

    async def test_pinned_base_compares_it_with_the_selected_version(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "down", "b", "up", "up")

            assert _selected(screen) == 3
            assert _header(screen) == f"base v1 {ARROW} v3   +7 words -1 word"
            assert _runs(_body(screen))[-1] == (
                " and list bugs by severity",
                INSERT_STYLE,
            )

    async def test_selected_base_shows_its_full_text(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "b")

            assert _header(screen) == "v2, compare base"
            assert _body(screen).plain == V2

    async def test_b_on_the_base_clears_the_pin(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "down", "b", "b", "up", "up")

            assert _header(screen).startswith(f"v2 {ARROW} v3")

    async def test_escape_clears_the_pin_before_it_closes(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "down", "b", "up", "up", "escape")

            assert app.results == []
            assert _header(screen).startswith(f"v2 {ARROW} v3")

            await _press(pilot, "escape")

        assert app.results == [None]


class TestActions:
    """Restore and insert act on the selected version."""

    async def test_restore_adds_a_version_with_the_old_body(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "down", "r")
            assert isinstance(app.screen, PromptConfirmScreen)
            await _press(pilot, "enter")

            assert app.screen is screen
            assert _selected(screen) == 4
        versions = store.list_versions(prompt.prompt_id)
        assert [(v.version, v.body) for v in versions] == [
            (4, V1),
            (3, V3),
            (2, V2),
            (1, V1),
        ]

    async def test_cancelled_restore_changes_nothing(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "r", "escape")

            assert app.screen is screen
            assert _selected(screen) == 2
        assert store.get_prompt(prompt.prompt_id).latest_version == 3

    async def test_restore_of_the_latest_version_adds_nothing(
        self, store: PromptStore, prompt: PromptSummary, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _VersionsHost()
        notes: list[str] = []
        monkeypatch.setattr(app, "notify", lambda message, **_: notes.append(message))
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "r")

            assert app.screen is screen
        assert notes == ["v3 is already the latest version."]
        assert store.get_prompt(prompt.prompt_id).latest_version == 3

    async def test_enter_dismisses_with_the_selected_body(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        app = _VersionsHost()
        async with app.run_test() as pilot:
            await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "enter")

        assert app.results == [V2]

    async def test_restore_of_text_the_latest_has_adds_nothing(
        self, store: PromptStore, prompt: PromptSummary, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.restore(prompt.prompt_id, 1)
        app = _VersionsHost()
        notes: list[str] = []
        monkeypatch.setattr(app, "notify", lambda message, **_: notes.append(message))
        async with app.run_test() as pilot:
            await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "down", "down", "r", "enter")

        assert notes == ["The latest version already has this text."]
        assert store.get_prompt(prompt.prompt_id).latest_version == 4

    async def test_restore_of_a_prompt_gone_elsewhere_says_so(
        self, store: PromptStore, prompt: PromptSummary, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _VersionsHost()
        notes: list[str] = []
        monkeypatch.setattr(app, "notify", lambda message, **_: notes.append(message))
        async with app.run_test() as pilot:
            await _open(app, pilot, store, prompt)
            store.delete(prompt.prompt_id)

            await _press(pilot, "down", "r", "enter")

        gone = "This prompt no longer exists. Another window may have deleted it."
        assert notes == [f"Could not restore v2: {gone}"]

    async def test_database_error_shows_the_reason(self, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        store = PromptStore(blocker / "data.db")
        prompt = PromptSummary(
            prompt_id=1,
            name="gone",
            latest_version=1,
            body="body",
            created_at="2026-09-26T12:00:00+00:00",
            updated_at="2026-09-26T12:00:00+00:00",
        )
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            assert _body(screen).plain == (
                "Could not load the versions: cannot create its directory"
            )

    async def test_failed_reload_clears_the_old_rows(self, tmp_path: Path) -> None:
        store = _FailingReloadStore(tmp_path / "data.db")
        created = store.create("review", V1)
        prompt = store.edit(created.prompt_id, body=V2).prompt
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            await _press(pilot, "down", "r", "enter")

            assert list(screen.query(VersionRow)) == []
            assert _body(screen).plain == "Could not load the versions: busy"

    async def test_deleted_prompt_shows_the_store_message(
        self, store: PromptStore, prompt: PromptSummary
    ) -> None:
        store.delete(prompt.prompt_id)
        app = _VersionsHost()
        async with app.run_test() as pilot:
            screen = await _open(app, pilot, store, prompt)

            assert _body(screen).plain == (
                "This prompt no longer exists. Another window may have deleted it."
            )

            await _press(pilot, "enter", "t", "b", "r", "escape")

        assert app.results == [None]
