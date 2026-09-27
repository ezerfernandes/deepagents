"""Inline word diff between two versions of a prompt.

`word_diff` returns ordered segments of unchanged, inserted, and deleted text,
plus counts of the words added and removed. The versions view of the prompt
library renders them as one merged text. This module imports only the standard
library, so it is cheap to test and safe to run in a worker thread.

The comparison narrows down in three levels. It compares lines first, then the
sentences inside each block of changed lines, then the tokens inside each block
of changed sentences. A token is a run of word characters, one line break, a
run of other whitespace, or one other character. Unchanged lines and sentences
never reach the token level, so it only sees short runs of text.

The token level must be exact, so it runs `difflib.SequenceMatcher` with
`autojunk` off. That is quadratic on prose and worse on repetitive text. In
2026-09 on a developer laptop, 2,100 prose tokens took 0.4 s, and 800 tokens of
`x x x` with every third one edited took 2.3 s. So the token level first trims
the tokens that both sides start and end with, which keeps a small edit in a
long sentence exact and cheap. If more than `PRECISE_DIFF_LIMIT` tokens are
left on either side, that run shows as one deletion and one insertion.

Lines and sentences are mostly unique, so their levels keep the default
`autojunk`, which ignores very common units such as blank lines. Those levels
trim the common start and end too, and a run of more than `_UNIT_LIMIT`
changed units goes to the next level whole.

`_TIME_BUDGET` bounds the whole comparison. When it runs out, each block that
is still unmatched shows as one deletion and one insertion, so even a
pathological input returns quickly with a correct, if coarser, diff.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import takewhile
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DiffKind = Literal["equal", "insert", "delete"]
"""Whether a segment is in both versions, only the new one, or only the old one."""

PRECISE_DIFF_LIMIT = 400
"""Most changed tokens per side that are compared word by word.

The count is taken after the common start and end are trimmed. A longer run of
changed tokens shows as one deletion and one insertion. At this size one
comparison takes at most about 0.3 s, even on repetitive text.
"""

_UNIT_LIMIT = 10_000
"""Most changed lines or sentences per side that are aligned at their level.

A longer run goes to the next level whole. Aligning many repeated units, such
as a file of blank lines with scattered edits, costs about the number of units
times the number of edits, so this keeps that cost bounded.
"""

_TIME_BUDGET = 1.0
"""Seconds of matching before the rest of a diff falls back to replacements."""

_TOKEN_RE = re.compile(r"\w+|\r?\n|[^\S\r\n]+|.", re.DOTALL)
"""Splits text into word, line-break, whitespace, and single-character tokens.

It matches every character, so the tokens of a text join back into it exactly.
Each line break is its own token, so a new or removed line break does not pull
the spaces next to it into the change.
"""

_SENTENCE_RE = re.compile(
    r"[^.!?\n]*+(?:[.!?]++(?!\s|\Z)[^.!?\n]*+)*+(?:[.!?]++\s*+|\n|\Z)"
)
"""Matches text up to the end of a sentence, a line break, or the end.

A sentence ends at a run of `.`, `!`, or `?` that is followed by whitespace or
the end of the text, and it keeps that whitespace. A run followed by anything
else, as in `v1.2` or `...x`, stays inside the sentence.

Every match starts where the last one ended, so the pieces join back into the
text. The final empty match at the end of the text is dropped.

The quantifiers are possessive, so a match never backtracks and the time is
linear in the text. A lazy `.*?` rescans a long run of dots from each of its
positions: in 2026-09, 16,000 dots took 5.6 s that way.
"""

_WORD_RE = re.compile(r"\w")


@dataclass(frozen=True, slots=True, kw_only=True)
class DiffSegment:
    """A run of text that is unchanged, inserted, or deleted.

    Attributes:
        kind: `"equal"` for text in both versions, `"insert"` for text only in
            the new version, and `"delete"` for text only in the old version.
        text: The text of the run. It is never empty.
    """

    kind: DiffKind
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class WordDiff:
    """The change from one text to another, in reading order.

    Neighboring segments always differ in kind. Between two `equal` segments
    there is at most one `delete` segment and one `insert` segment, and the
    `delete` segment comes first.

    Attributes:
        segments: The runs in order. The `equal` and `delete` runs join into
            the old text. The `equal` and `insert` runs join into the new text.
        words_added: Number of word tokens in `insert` runs.
        words_removed: Number of word tokens in `delete` runs.
    """

    segments: tuple[DiffSegment, ...]
    words_added: int
    words_removed: int

    @property
    def changed(self) -> bool:
        """Whether the texts differ, even if only in whitespace or punctuation."""
        return any(segment.kind != "equal" for segment in self.segments)


def word_diff(old: str, new: str) -> WordDiff:
    """Compare two texts word by word.

    Args:
        old: The earlier text.
        new: The later text.

    Returns:
        The segments that turn `old` into `new`, with word counts.
    """
    builder = _Builder(deadline=time.monotonic() + _TIME_BUDGET)
    if old == new:
        builder.equal(old)
    else:
        _diff_level(builder, old, new, 0)
    return builder.result()


def _split_lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _split_sentences(text: str) -> list[str]:
    return [match.group() for match in _SENTENCE_RE.finditer(text) if match.group()]


_LEVELS: tuple[Callable[[str], list[str]], ...] = (_split_lines, _split_sentences)
"""Unit splitters from coarse to fine. Tokens are the last level."""


def _diff_level(builder: _Builder, old: str, new: str, level: int) -> None:
    """Align two texts by the units of one level and refine the changed blocks."""
    if level == len(_LEVELS):
        tokens = _TOKEN_RE.findall
        _align(
            builder,
            tokens(old),
            tokens(new),
            limit=PRECISE_DIFF_LIMIT,
            autojunk=False,
            refine=builder.change,
        )
        return

    def refine(old_units: Sequence[str], new_units: Sequence[str]) -> None:
        _diff_level(builder, "".join(old_units), "".join(new_units), level + 1)

    split = _LEVELS[level]
    _align(
        builder, split(old), split(new), limit=_UNIT_LIMIT, autojunk=True, refine=refine
    )


def _align(
    builder: _Builder,
    old: Sequence[str],
    new: Sequence[str],
    *,
    limit: int,
    autojunk: bool,
    refine: Callable[[Sequence[str], Sequence[str]], None],
) -> None:
    """Match two unit sequences and pass each changed block to `refine`.

    The common start and end are trimmed first. When more than `limit` units
    are left on either side, or the time budget is spent, `SequenceMatcher` is
    skipped and the whole middle goes to `refine`. That keeps the cost bounded
    on long or repetitive text.
    """
    start = _common_prefix(old, new)
    end = _common_suffix(old, new, start)
    builder.equal("".join(old[:start]))
    old_mid = old[start : len(old) - end]
    new_mid = new[start : len(new) - end]
    if max(len(old_mid), len(new_mid)) > limit or builder.out_of_time():
        refine(old_mid, new_mid)
    else:
        matcher = SequenceMatcher(None, old_mid, new_mid, autojunk=autojunk)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                builder.equal("".join(old_mid[i1:i2]))
            else:
                refine(old_mid[i1:i2], new_mid[j1:j2])
    builder.equal("".join(old[len(old) - end :]))


def _common_prefix(old: Sequence[str], new: Sequence[str]) -> int:
    """Count the units that both sequences start with.

    Returns:
        The length of the common prefix.
    """
    pairs = zip(old, new, strict=False)
    return sum(1 for _ in takewhile(lambda pair: pair[0] == pair[1], pairs))


def _common_suffix(old: Sequence[str], new: Sequence[str], start: int) -> int:
    """Count the units that both sequences end with, after the first `start`.

    Returns:
        The length of the common suffix. It never overlaps the prefix.
    """
    pairs = zip(reversed(old[start:]), reversed(new[start:]), strict=False)
    return sum(1 for _ in takewhile(lambda pair: pair[0] == pair[1], pairs))


def _count_words(tokens: Sequence[str]) -> int:
    """Count the word tokens.

    Returns:
        How many tokens are runs of word characters.
    """
    return sum(1 for token in tokens if _WORD_RE.match(token))


class _Builder:
    """Collect runs and merge neighbors into the fewest segments.

    All the changes between two equal runs become one `delete` segment and then
    one `insert` segment. Moving the deleted text in front of the inserted text
    inside that span keeps both texts intact. The old text reads only equal and
    deleted runs, and the new text reads only equal and inserted runs.
    """

    def __init__(self, *, deadline: float) -> None:
        self._deadline = deadline
        self._segments: list[DiffSegment] = []
        self._equal: list[str] = []
        self._deleted: list[str] = []
        self._inserted: list[str] = []
        self._words_added = 0
        self._words_removed = 0

    def out_of_time(self) -> bool:
        """Whether the time budget for exact matching is spent.

        Returns:
            `True` once `time.monotonic()` reaches the deadline.
        """
        return time.monotonic() >= self._deadline

    def equal(self, text: str) -> None:
        """Add unchanged text."""
        if not text:
            return
        self._flush_change()
        self._equal.append(text)

    def change(self, deleted: Sequence[str], inserted: Sequence[str]) -> None:
        """Add tokens that only the old text or only the new text has."""
        if not deleted and not inserted:
            return
        self._flush_equal()
        self._deleted.extend(deleted)
        self._inserted.extend(inserted)
        self._words_removed += _count_words(deleted)
        self._words_added += _count_words(inserted)

    def result(self) -> WordDiff:
        """Return the finished diff.

        Returns:
            The segments and word counts collected so far.
        """
        self._flush_equal()
        self._flush_change()
        return WordDiff(
            segments=tuple(self._segments),
            words_added=self._words_added,
            words_removed=self._words_removed,
        )

    def _flush_equal(self) -> None:
        if self._equal:
            self._segments.append(DiffSegment(kind="equal", text="".join(self._equal)))
            self._equal.clear()

    def _flush_change(self) -> None:
        if self._deleted:
            text = "".join(self._deleted)
            self._segments.append(DiffSegment(kind="delete", text=text))
            self._deleted.clear()
        if self._inserted:
            text = "".join(self._inserted)
            self._segments.append(DiffSegment(kind="insert", text=text))
            self._inserted.clear()
