"""Tests for the styled word diff shown between prompt versions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.app import App
from textual.theme import Theme
from textual.widgets import Static

from deepagents_code import theme
from deepagents_code._env_vars import UI_CHARSET_MODE
from deepagents_code.config import UNICODE_GLYPHS, reset_glyphs_cache
from deepagents_code.tui.modals.prompt_library.diff_view import (
    DELETE_STYLE,
    INSERT_STYLE,
    format_word_diff_stats,
    render_word_diff,
)
from deepagents_code.word_diff import DiffKind, DiffSegment, WordDiff, word_diff

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rich.color import ColorTriplet
    from rich.style import Style as RichStyle
    from textual.app import ComposeResult
    from textual.content import Content
    from textual.style import Style

NL = UNICODE_GLYPHS.newline
"""The newline glyph that marks a changed line break."""


@pytest.fixture(autouse=True)
def _unicode_glyphs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Render with the Unicode glyphs, whatever charset the terminal has.

    `get_glyphs()` caches the detected charset for the whole process, so the
    cache is reset before and after each test.
    """
    monkeypatch.setenv(UI_CHARSET_MODE, "unicode")
    reset_glyphs_cache()
    yield
    reset_glyphs_cache()


def _diff(
    *segments: tuple[DiffKind, str], added: int = 0, removed: int = 0
) -> WordDiff:
    return WordDiff(
        segments=tuple(DiffSegment(kind=kind, text=text) for kind, text in segments),
        words_added=added,
        words_removed=removed,
    )


def _runs(content: Content) -> list[tuple[str, str | Style]]:
    """Return the text and style of each span, in order."""
    return [
        (content.plain[span.start : span.end], span.style) for span in content.spans
    ]


def _lines(content: Content) -> list[str]:
    return content.plain.split("\n")


class TestStyles:
    """Each kind of segment is styled on exactly its own text."""

    def test_each_kind_is_styled_on_exactly_its_text(self) -> None:
        diff = _diff(
            ("equal", "Review "),
            ("delete", "this"),
            ("insert", "that"),
            ("equal", " branch."),
        )

        content = render_word_diff(diff)

        assert content.plain == "Review thisthat branch."
        assert [(span.start, span.end, span.style) for span in content.spans] == [
            (7, 11, DELETE_STYLE),
            (11, 15, INSERT_STYLE),
        ]

    def test_engine_output_renders_deletion_before_insertion(self) -> None:
        content = render_word_diff(word_diff("list all bugs", "list the current bugs"))

        assert content.plain == "list allthe current bugs"
        assert _runs(content) == [("all", DELETE_STYLE), ("the current", INSERT_STYLE)]

    def test_unchanged_diff_is_plain_text(self) -> None:
        text = "Same body.\nSecond line."

        content = render_word_diff(word_diff(text, text))

        assert content.plain == text
        assert content.spans == []

    def test_empty_diff_is_empty(self) -> None:
        assert render_word_diff(word_diff("", "")).plain == ""


class TestUserText:
    """Prompt text shows exactly as the user wrote it."""

    @pytest.mark.parametrize(
        "text", ["[bold]x[/bold]", "[/tmp]", "[red]$var[/red]", "\\[not markup]"]
    )
    def test_markup_renders_literally(self, text: str) -> None:
        content = render_word_diff(
            _diff(("equal", text), ("delete", text), ("insert", text))
        )

        assert content.plain == text * 3
        assert _runs(content) == [(text, DELETE_STYLE), (text, INSERT_STYLE)]

    @pytest.mark.parametrize("line_break", ["\r\n", "\r"], ids=["crlf", "cr"])
    def test_other_line_breaks_render_as_line_breaks(self, line_break: str) -> None:
        diff = _diff(
            ("equal", f"one{line_break}"), ("insert", line_break), ("equal", "two")
        )

        content = render_word_diff(diff)

        assert content.plain == f"one\n{NL}\ntwo"
        assert _runs(content) == [(NL, INSERT_STYLE)]

    def test_control_codes_do_not_shift_the_styles(self) -> None:
        """`Content` strips these codes, which would move every later span."""
        diff = _diff(("equal", "a\x07b\x08 "), ("delete", "old"), ("insert", "new"))

        content = render_word_diff(diff)

        assert content.plain == "ab oldnew"
        assert _runs(content) == [("old", DELETE_STYLE), ("new", INSERT_STYLE)]

    def test_stripped_codes_do_not_count_as_text_on_the_line(self) -> None:
        """So the deleted line still gets its own line."""
        diff = _diff(("equal", "\x07"), ("delete", "old\n"), ("equal", "new"))

        content = render_word_diff(diff)

        assert _lines(content) == [f"old{NL}", "new"]


class TestLineBreaks:
    """Changed line breaks show as the newline glyph in the style of the change."""

    def test_deleted_line_break_is_a_glyph_on_the_joined_line(self) -> None:
        content = render_word_diff(word_diff("one\ntwo", "one two"))

        assert content.plain == f"one{NL} two"
        assert _runs(content) == [(NL, DELETE_STYLE), (" ", INSERT_STYLE)]

    def test_inserted_line_break_is_a_glyph_and_a_real_break(self) -> None:
        content = render_word_diff(word_diff("one two", "one\ntwo"))

        assert content.plain == f"one {NL}\ntwo"
        assert _runs(content) == [(" ", DELETE_STYLE), (NL, INSERT_STYLE)]

    def test_inserted_lines_keep_their_lines(self) -> None:
        diff = _diff(("equal", "A\n"), ("insert", "new 1\nnew 2\n"), ("equal", "B"))

        content = render_word_diff(diff)

        assert _lines(content) == ["A", f"new 1{NL}", f"new 2{NL}", "B"]
        assert _runs(content) == [
            (f"new 1{NL}", INSERT_STYLE),
            (f"new 2{NL}", INSERT_STYLE),
        ]

    def test_deleted_blank_line_keeps_its_own_line(self) -> None:
        content = render_word_diff(word_diff("one\n\ntwo", "one\ntwo"))

        assert _lines(content) == ["one", NL, "two"]
        assert _runs(content) == [(NL, DELETE_STYLE)]

    def test_dropped_paragraph_keeps_its_lines(self) -> None:
        old = "Intro.\n\nDrop this.\nAnd this.\n\nOutro."

        content = render_word_diff(word_diff(old, "Intro.\n\nOutro."))

        assert _lines(content) == [
            "Intro.",
            "",
            f"Drop this.{NL}",
            f"And this.{NL}",
            NL,
            "Outro.",
        ]
        assert _runs(content) == [
            (f"Drop this.{NL}", DELETE_STYLE),
            (f"And this.{NL}", DELETE_STYLE),
            (NL, DELETE_STYLE),
        ]

    def test_deletion_inside_a_line_stays_on_that_line(self) -> None:
        """A real break would split the line of the new version around it."""
        diff = _diff(
            ("equal", "Intro: "),
            ("delete", "a\nb\nc"),
            ("insert", "x"),
            ("equal", "\nrest"),
        )

        content = render_word_diff(diff)

        assert _lines(content) == [f"Intro: a{NL}b{NL}cx", "rest"]
        assert _runs(content) == [(f"a{NL}b{NL}c", DELETE_STYLE), ("x", INSERT_STYLE)]

    def test_deletion_at_the_end_keeps_its_lines(self) -> None:
        """Nothing of the new version follows, so no line of it can be split."""
        content = render_word_diff(_diff(("equal", "A."), ("delete", "\n\nB.")))

        assert _lines(content) == [f"A.{NL}", NL, "B."]

    def test_line_break_that_ends_the_diff_adds_no_empty_line(self) -> None:
        content = render_word_diff(_diff(("equal", "A"), ("delete", "\nB\n")))

        assert _lines(content) == [f"A{NL}", f"B{NL}"]

    def test_ascii_terminals_get_backslash_n(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(UI_CHARSET_MODE, "ascii")
        reset_glyphs_cache()
        diff = _diff(
            ("equal", "one"), ("delete", "\n"), ("insert", "\n"), ("equal", "two")
        )

        content = render_word_diff(diff)

        assert content.plain == "one\\n\\n\ntwo"
        assert _runs(content) == [("\\n", DELETE_STYLE), ("\\n", INSERT_STYLE)]


class TestStats:
    """The header summary counts the changed words."""

    def test_counts_are_colored_like_the_file_diff(self) -> None:
        colors = theme.get_theme_colors()

        stats = format_word_diff_stats(
            _diff(("delete", "a"), ("insert", "b"), added=4, removed=2)
        )

        assert stats.plain == "+4 words -2 words"
        assert _runs(stats) == [("+4", colors.success), ("-2", colors.error)]

    @pytest.mark.parametrize(
        ("added", "removed", "expected"),
        [
            (1, 0, "+1 word"),
            (0, 1, "-1 word"),
            (3, 0, "+3 words"),
            (1, 5, "+1 word -5 words"),
        ],
    )
    def test_zero_counts_are_left_out(
        self, added: int, removed: int, expected: str
    ) -> None:
        diff = _diff(("delete", "a"), ("insert", "b"), added=added, removed=removed)

        assert format_word_diff_stats(diff).plain == expected

    def test_matching_versions_say_no_changes(self) -> None:
        assert format_word_diff_stats(word_diff("Same.", "Same.")).plain == "no changes"

    def test_whitespace_and_punctuation_changes_say_no_word_changes(self) -> None:
        stats = format_word_diff_stats(word_diff("a, b", "a;  b!"))

        assert stats.plain == "no word changes"


class _ThemedDiff(App[None]):
    """Show one rendered diff under a LangChain theme."""

    def __init__(self, content: Content, *, dark: bool) -> None:
        super().__init__()
        self._content = content
        name = "langchain" if dark else "langchain-light"
        colors = theme.DARK_COLORS if dark else theme.LIGHT_COLORS
        self.register_theme(
            Theme(
                name=name,
                primary=colors.primary,
                secondary=colors.secondary,
                accent=colors.accent,
                foreground=colors.foreground,
                background=colors.background,
                surface=colors.surface,
                panel=colors.panel,
                warning=colors.warning,
                error=colors.error,
                success=colors.success,
                dark=dark,
            )
        )
        self.theme = name

    def compose(self) -> ComposeResult:
        yield Static(self._content)


def _background(style: RichStyle | None) -> ColorTriplet:
    assert style is not None
    assert style.bgcolor is not None
    triplet = style.bgcolor.triplet
    assert triplet is not None
    return triplet


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
async def test_default_themes_color_the_changes(dark: bool) -> None:
    """Deletions are red and struck through, and additions are green."""
    diff = _diff(("equal", "keep "), ("delete", "gone"), ("insert", "added"))
    app = _ThemedDiff(render_word_diff(diff), dark=dark)

    async with app.run_test() as pilot:
        await pilot.pause()
        styles = {s.text: s.style for s in app.query_one(Static).render_line(0)}

    deleted, inserted = styles["gone"], styles["added"]
    red = _background(deleted)
    green = _background(inserted)
    assert red.red > max(red.green, red.blue)
    assert green.green > max(green.red, green.blue)
    assert deleted is not None
    assert deleted.strike
    assert inserted is not None
    assert not inserted.strike
