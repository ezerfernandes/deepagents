"""Tests for `deepagents_cli.oauth.pkce`."""

from __future__ import annotations

import base64
import hashlib

from deepagents_cli.oauth.pkce import generate_pkce


def test_verifier_is_43_chars_base64url_no_padding() -> None:
    """A 32-byte secret encodes to a 43-char base64url string with no padding.

    pi-mono uses the same length, so the OAuth servers see byte-identical
    PKCE shapes from either codebase.
    """
    pair = generate_pkce()
    assert len(pair.verifier) == 43
    assert "=" not in pair.verifier
    assert "+" not in pair.verifier
    assert "/" not in pair.verifier


def test_challenge_is_sha256_of_verifier_base64url() -> None:
    """The challenge is the base64url-encoded SHA-256 of the verifier ASCII."""
    pair = generate_pkce()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pair.verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert pair.challenge == expected
    assert "=" not in pair.challenge


def test_repeated_calls_return_unique_pairs() -> None:
    """Each call uses fresh randomness — never repeats."""
    pairs = {generate_pkce().verifier for _ in range(8)}
    assert len(pairs) == 8
