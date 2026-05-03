"""Tests for `deepagents_cli.oauth.storage`."""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING

import pytest

from deepagents_cli.oauth.storage import (
    delete_credentials,
    list_logged_in_providers,
    load_credentials,
    save_credentials,
)
from deepagents_cli.oauth.types import OAuthCredentials

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.usefixtures("fake_state_dir")
class TestRoundTrip:
    """Save → load → delete round-trips."""

    def test_save_and_load(self) -> None:
        """A saved credential round-trips through `load_credentials`."""
        creds = OAuthCredentials(
            access="aaa",
            refresh="rrr",
            expires=1234.5,
            extras={"accountId": "x", "enterpriseUrl": "y"},
        )
        save_credentials("anthropic", creds)
        loaded = load_credentials("anthropic")
        assert loaded == creds

    def test_load_missing_returns_none(self) -> None:
        """No file ⇒ `None`, no exception."""
        assert load_credentials("anthropic") is None

    def test_delete_returns_true_when_removed(self) -> None:
        """Delete returns the existence-before-delete bit."""
        save_credentials(
            "anthropic",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )
        assert delete_credentials("anthropic") is True
        assert delete_credentials("anthropic") is False


@pytest.mark.usefixtures("fake_state_dir")
class TestFilePermissions:
    """File-mode invariants we promise to security-conscious users."""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file modes don't apply on Windows",
    )
    def test_file_mode_is_0600(self, fake_state_dir: Path) -> None:
        """Token files are not world-readable."""
        save_credentials(
            "anthropic",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )
        path = fake_state_dir / "oauth-tokens" / "anthropic.json"
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file modes don't apply on Windows",
    )
    def test_parent_dir_is_0700(self, fake_state_dir: Path) -> None:
        """The tokens dir itself is not listable by other users."""
        save_credentials(
            "anthropic",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )
        parent = fake_state_dir / "oauth-tokens"
        mode = parent.stat().st_mode & 0o777
        assert mode == 0o700


@pytest.mark.usefixtures("fake_state_dir")
class TestSchema:
    """On-disk schema invariants."""

    def test_payload_has_version_and_type(self, fake_state_dir: Path) -> None:
        """We embed a schema-version and a `type` discriminator."""
        save_credentials(
            "anthropic",
            OAuthCredentials(access="a", refresh="r", expires=1.0, extras={"x": "y"}),
        )
        path = fake_state_dir / "oauth-tokens" / "anthropic.json"
        payload = json.loads(path.read_text())
        assert payload["version"] == 1
        assert payload["type"] == "oauth"
        assert payload["provider"] == "anthropic"
        assert payload["extras"] == {"x": "y"}

    def test_unsupported_version_is_rejected(self, fake_state_dir: Path) -> None:
        """Files with the wrong schema version raise — never silently load."""
        path = fake_state_dir / "oauth-tokens" / "anthropic.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": 999,
                    "type": "oauth",
                    "provider": "anthropic",
                    "access": "a",
                    "refresh": "r",
                    "expires": 0.0,
                }
            )
        )
        with pytest.raises(RuntimeError, match="unsupported version"):
            load_credentials("anthropic")

    def test_corrupt_json_is_rejected(self, fake_state_dir: Path) -> None:
        """Bad JSON tells the user how to recover."""
        path = fake_state_dir / "oauth-tokens" / "anthropic.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        with pytest.raises(RuntimeError, match="re-run `deepagents login"):
            load_credentials("anthropic")


@pytest.mark.usefixtures("fake_state_dir")
class TestListing:
    """`list_logged_in_providers` returns the right providers in stable order."""

    def test_empty_dir_returns_empty_list(self) -> None:
        assert list_logged_in_providers() == []

    def test_lists_only_well_named_files(self, fake_state_dir: Path) -> None:
        save_credentials(
            "anthropic",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )
        save_credentials(
            "github-copilot",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )
        # Drop in a stray file that should be ignored.
        (fake_state_dir / "oauth-tokens" / "stale.json.tmp").write_bytes(b"junk")
        assert list_logged_in_providers() == ["anthropic", "github-copilot"]


def test_unsafe_provider_id_is_rejected() -> None:
    """Provider IDs must be `[A-Za-z0-9_-]+` — no path traversal."""
    with pytest.raises(ValueError, match="must match"):
        save_credentials(
            "../etc/passwd",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )
