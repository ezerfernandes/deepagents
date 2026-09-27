"""Tests for the inline word diff between prompt versions."""

from __future__ import annotations

import random
from itertools import pairwise

import pytest

from deepagents_code import word_diff as word_diff_module
from deepagents_code.word_diff import PRECISE_DIFF_LIMIT, WordDiff, word_diff

_PIECES = (
    "alpha",
    "beta",
    "gamma",
    "café",
    "日本語",
    "_x1",
    " ",
    "  ",
    "\t",
    "\n",
    "\r\n",
    "\r",
    ",",
    ".",
    "!",
    "é",
    "\u2028",
)
"""Fragments for random texts, including the characters that split lines."""


def _old(diff: WordDiff) -> str:
    return "".join(s.text for s in diff.segments if s.kind != "insert")


def _new(diff: WordDiff) -> str:
    return "".join(s.text for s in diff.segments if s.kind != "delete")


def _runs(diff: WordDiff) -> list[tuple[str, str]]:
    return [(segment.kind, segment.text) for segment in diff.segments]


def _assert_valid(diff: WordDiff, old: str, new: str) -> None:
    """Check the invariants and the segment shape that the renderer relies on."""
    assert _old(diff) == old
    assert _new(diff) == new
    assert all(segment.text for segment in diff.segments)
    for left, right in pairwise(diff.segments):
        assert left.kind != right.kind
        assert (left.kind, right.kind) != ("insert", "delete")


def _random_text(rng: random.Random) -> str:
    return "".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 30)))


def _mutate(rng: random.Random, text: str) -> str:
    """Replace a few random slices of `text`, sometimes with nothing."""
    for _ in range(rng.randint(0, 5)):
        start = rng.randint(0, len(text))
        stop = rng.randint(start, min(len(text), start + 8))
        text = text[:start] + rng.choice(_PIECES) * rng.randint(0, 2) + text[stop:]
    return text


class TestSegments:
    """The segments show exactly what changed."""

    def test_identical_text_is_one_equal_segment(self) -> None:
        diff = word_diff("Review the diff.", "Review the diff.")

        assert _runs(diff) == [("equal", "Review the diff.")]
        assert (diff.words_added, diff.words_removed) == (0, 0)
        assert not diff.changed

    def test_two_empty_texts_have_no_segments(self) -> None:
        diff = word_diff("", "")

        assert diff.segments == ()
        assert not diff.changed

    def test_text_from_nothing_is_one_insert(self) -> None:
        diff = word_diff("", "Review the diff.")

        assert _runs(diff) == [("insert", "Review the diff.")]
        assert diff.words_added == 3

    def test_text_to_nothing_is_one_delete(self) -> None:
        diff = word_diff("Review the diff.", "")

        assert _runs(diff) == [("delete", "Review the diff.")]
        assert diff.words_removed == 3

    def test_one_changed_word_touches_only_that_word(self) -> None:
        diff = word_diff("Review this branch.", "Review that branch.")

        assert _runs(diff) == [
            ("equal", "Review "),
            ("delete", "this"),
            ("insert", "that"),
            ("equal", " branch."),
        ]
        assert (diff.words_added, diff.words_removed) == (1, 1)
        assert diff.changed

    def test_replaced_words_show_the_deletion_first(self) -> None:
        diff = word_diff("list all bugs", "list the current bugs")

        assert _runs(diff) == [
            ("equal", "list "),
            ("delete", "all"),
            ("insert", "the current"),
            ("equal", " bugs"),
        ]

    def test_added_line_break_is_its_own_segment(self) -> None:
        diff = word_diff("one\ntwo", "one\n\ntwo")

        assert _runs(diff) == [("equal", "one\n"), ("insert", "\n"), ("equal", "two")]

    def test_removed_line_break_is_its_own_segment(self) -> None:
        diff = word_diff("one\n\ntwo", "one\ntwo")

        assert _runs(diff) == [("equal", "one\n"), ("delete", "\n"), ("equal", "two")]

    def test_line_break_that_replaces_a_space(self) -> None:
        diff = word_diff("one two", "one\ntwo")

        assert _runs(diff) == [
            ("equal", "one"),
            ("delete", " "),
            ("insert", "\n"),
            ("equal", "two"),
        ]

    def test_spaces_before_a_line_break_change_alone(self) -> None:
        """Line breaks are separate tokens, so trailing spaces diff on their own."""
        diff = word_diff("end.  \nNext", "end.\nNext")

        assert _runs(diff) == [("equal", "end."), ("delete", "  "), ("equal", "\nNext")]

    def test_changed_paragraph_leaves_the_others_equal(self) -> None:
        old = "Intro stays.\n\nReview this branch.\n\nOutro stays.\n"
        new = "Intro stays.\n\nReview that branch.\n\nOutro stays.\n"

        diff = word_diff(old, new)

        assert _runs(diff) == [
            ("equal", "Intro stays.\n\nReview "),
            ("delete", "this"),
            ("insert", "that"),
            ("equal", " branch.\n\nOutro stays.\n"),
        ]


class TestWordCounts:
    """Only runs of word characters count as words."""

    def test_whitespace_and_punctuation_are_not_words(self) -> None:
        diff = word_diff("a, b", "a;  b!")

        assert (diff.words_added, diff.words_removed) == (0, 0)
        assert diff.changed

    def test_non_ascii_words_count(self) -> None:
        diff = word_diff("", "café 日本語")

        assert diff.words_added == 2


class TestInvariants:
    """The segments always rebuild both texts."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("", "a"),
            ("a", ""),
            ("a b", "b a"),
            ("line1\nline2\n", "line1\nline2"),
            ("line1\r\nline2", "line1\nline2"),
            ("x\ry", "x\ny"),
            ("caf\u00e9", "cafe\u0301"),
            ("日本語です", "日本語でした"),
            ("  lead", "lead  "),
            ("tab\tsep", "tab sep"),
            ("a\u2028b", "a\nb"),
            ("A. B. C.", "C. B. A."),
            ("\n\n\n", "\n"),
            ("word", "word\n"),
            ("one long line that wraps", "one long\nline that\nwraps"),
        ],
    )
    def test_table_of_edits(self, old: str, new: str) -> None:
        _assert_valid(word_diff(old, new), old, new)

    def test_random_edits(self) -> None:
        rng = random.Random(20260926)
        for _ in range(500):
            old = _random_text(rng)
            new = _mutate(rng, old) if rng.random() < 0.8 else _random_text(rng)
            _assert_valid(word_diff(old, new), old, new)


class TestLargeInput:
    """Input above `PRECISE_DIFF_LIMIT` stays valid and word-level."""

    def test_small_edit_in_a_long_line_is_exact(self) -> None:
        """Trimming the common prefix and suffix leaves one word to compare."""
        old = " ".join(f"word{n}" for n in range(10 * PRECISE_DIFF_LIMIT))
        new = old.replace(" word1700 ", " changed ")

        diff = word_diff(old, new)

        _assert_valid(diff, old, new)
        assert [segment.kind for segment in diff.segments] == [
            "equal",
            "delete",
            "insert",
            "equal",
        ]
        assert (diff.words_added, diff.words_removed) == (1, 1)

    def test_edits_far_apart_in_a_long_line_stay_word_level(self) -> None:
        """Unchanged sentences between the edits never reach the token level."""
        body = " ".join(f"Sentence {n} stays." for n in range(PRECISE_DIFF_LIMIT))
        old = f"Start here. {body} End here."
        new = f"Begin here. {body} Finish here."

        diff = word_diff(old, new)

        _assert_valid(diff, old, new)
        assert (diff.words_added, diff.words_removed) == (2, 2)

    def test_long_run_of_changes_becomes_one_replacement(self) -> None:
        """A changed run longer than the limit is not compared word by word."""
        middle = " ".join(f"w{n}" for n in range(PRECISE_DIFF_LIMIT))
        old = f"a {middle} z"
        new = f"b {middle} y"

        diff = word_diff(old, new)

        assert _runs(diff) == [("delete", old), ("insert", new)]

    @pytest.mark.timeout(10)
    def test_repetitive_text_with_many_edits_finishes(self) -> None:
        """Without the limits, this input takes about 15 s."""
        old = "x " * 740
        new = "".join("y " if n % 3 == 0 else "x " for n in range(740))

        _assert_valid(word_diff(old, new), old, new)

    @pytest.mark.timeout(10)
    def test_long_run_of_dots_finishes(self) -> None:
        """A dot run that ends no sentence is scanned once, not once per dot.

        With a backtracking sentence pattern, this input takes about 2 minutes.
        """
        dots = "." * 50_000

        diff = word_diff(f"{dots}x", f"{dots}y")

        assert _runs(diff) == [("equal", dots), ("delete", "x"), ("insert", "y")]

    def test_one_changed_line_among_many_is_exact(self) -> None:
        lines = [f"line {n}\n" for n in range(3_000)]
        old = "".join(lines)
        lines[1234] = "line changed\n"
        new = "".join(lines)

        diff = word_diff(old, new)

        _assert_valid(diff, old, new)
        assert _runs(diff)[1:3] == [("delete", "1234"), ("insert", "changed")]


class TestTimeBudget:
    """A spent time budget gives a coarser diff that is still correct."""

    def test_spent_budget_replaces_each_changed_block_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(word_diff_module, "_TIME_BUDGET", 0.0)
        old = "Keep this. one two three. Keep that."
        new = "Keep this. uno two tres. Keep that."

        diff = word_diff(old, new)

        _assert_valid(diff, old, new)
        assert _runs(diff) == [
            ("equal", "Keep this. "),
            ("delete", "one two three"),
            ("insert", "uno two tres"),
            ("equal", ". Keep that."),
        ]
