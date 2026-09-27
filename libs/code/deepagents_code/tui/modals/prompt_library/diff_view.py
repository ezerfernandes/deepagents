"""Styled rendering of a word diff between two prompt versions.

`render_word_diff` turns a `WordDiff` into one `Content` that reads as the new
version with the old text shown inline. Inserted text has a green background,
and deleted text has a red background and strikethrough. The styles use theme
variables, as the file-edit diff in `tui/widgets/diff.py` does, so every theme
colors them the same way. Prompt text is user content, so it never goes
through markup parsing.

A changed line break has no width, so it shows as the newline glyph in the
style of its change. Real line breaks follow the lines of the new version:

- An unchanged line break is a real line break.
- An inserted line break is the glyph and then a real line break.
- A deleted line break is only the glyph, so lines that the new version joins
  stay on one line. It gets a real line break after the glyph when that break
  cannot split a line of the new version: the new version has no text on that
  line before it, or no text at all after it. So deleted lines that stood
  alone, such as a dropped paragraph, keep their own lines. A deletion that
  starts inside a line and runs over several lines stays on that line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.content import Content

from deepagents_code import theme
from deepagents_code.config import get_glyphs

if TYPE_CHECKING:
    from deepagents_code.word_diff import WordDiff

INSERT_STYLE = "on $success 30%"
"""Style of text that only the new version has."""

DELETE_STYLE = "strike $text-error on $error 30%"
"""Style of text that only the old version has."""

_CONTROL_CODES = dict.fromkeys((0x07, 0x08, 0x0B, 0x0C))
"""Bell, backspace, vertical tab, and form feed, which `Content` strips.

They are removed before the spans are measured. If `Content` stripped them
afterward, every later span would land on the wrong text.
"""

type _Part = str | tuple[str, str]
"""A plain string or a `(text, style)` pair for `Content.assemble`."""


def render_word_diff(diff: WordDiff) -> Content:
    """Render a word diff as the new text with the old text shown inline.

    Args:
        diff: The change from the old version to the new one.

    Returns:
        The merged text with the changes styled. It is plain text when the
        versions match.
    """
    glyph = get_glyphs().newline
    parts: list[_Part] = []
    new_text_on_line = False
    last = len(diff.segments) - 1
    for index, segment in enumerate(diff.segments):
        text = _clean(segment.text)
        if not text:
            continue
        if segment.kind == "delete":
            at_end = index == last
            split = at_end or not new_text_on_line
            parts += _deleted(text, glyph, split=split, at_end=at_end)
        else:
            is_equal = segment.kind == "equal"
            parts += [text] if is_equal else _lines(text, glyph, INSERT_STYLE)
            new_text_on_line = not text.endswith("\n")
    return Content.assemble(*parts, strip_control_codes=False)


def format_word_diff_stats(diff: WordDiff) -> Content:
    """Summarize a word diff as counts such as `+4 words -2 words`.

    The counts are colored as `format_diff_stats` in `tui/widgets/diff.py`
    colors line counts. A count of zero is left out.

    Args:
        diff: The diff to summarize.

    Returns:
        The styled counts, `no changes` when the versions match, or
        `no word changes` when only whitespace or punctuation changed.
    """
    if not diff.changed:
        return Content.styled("no changes", "dim")
    if not diff.words_added and not diff.words_removed:
        return Content.styled("no word changes", "dim")
    colors = theme.get_theme_colors()
    parts: list[_Part] = []
    if diff.words_added:
        parts += [(f"+{diff.words_added}", colors.success), _unit(diff.words_added)]
    if diff.words_removed:
        if parts:
            parts.append(" ")
        parts += [(f"-{diff.words_removed}", colors.error), _unit(diff.words_removed)]
    return Content.assemble(*parts)


def _clean(text: str) -> str:
    """Make each line break one line feed and drop the codes `Content` strips.

    A lone carriage return is a line break, as it is in the prompt body editor.

    Returns:
        The text as it is shown.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").translate(_CONTROL_CODES)


def _lines(text: str, glyph: str, style: str) -> list[_Part]:
    """Style changed text, ending each of its lines with the glyph.

    Returns:
        Parts with a real line break after each glyph.
    """
    *lines, tail = text.split("\n")
    parts: list[_Part] = []
    for line in lines:
        parts += [(line + glyph, style), "\n"]
    if tail:
        parts.append((tail, style))
    return parts


def _deleted(text: str, glyph: str, *, split: bool, at_end: bool) -> list[_Part]:
    """Style deleted text, marking each of its line breaks with the glyph.

    Args:
        text: The deleted text.
        glyph: The newline glyph.
        split: Whether each glyph gets a real line break after it.
        at_end: Whether the text ends the diff. A real break after its final
            line break would only add an empty line.

    Returns:
        The styled parts.
    """
    if not split:
        return [(text.replace("\n", glyph), DELETE_STYLE)]
    parts = _lines(text, glyph, DELETE_STYLE)
    if at_end and text.endswith("\n"):
        parts.pop()
    return parts


def _unit(count: int) -> str:
    return " word" if count == 1 else " words"
