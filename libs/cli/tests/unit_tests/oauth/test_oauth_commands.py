"""Tests for `deepagents_cli.oauth_commands`."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from deepagents_cli.oauth import (
    OAuthCallbacks,
    OAuthCredentials,
    register_provider,
)
from deepagents_cli.oauth.storage import save_credentials
from deepagents_cli.oauth_commands import (
    _humanize_seconds,
    run_auth_list,
    run_login,
    run_logout,
)

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("fake_state_dir")


class _StubProvider:
    """Minimal `OAuthProvider` that always succeeds without doing real I/O."""

    id = "stub"
    name = "Stub Provider"
    uses_callback_server = False

    async def login(self, callbacks: OAuthCallbacks) -> OAuthCredentials:
        callbacks.on_auth.__call__  # ensure attribute exists
        return OAuthCredentials(
            access="stub-access",
            refresh="stub-refresh",
            expires=time.time() + 3600,
        )

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return credentials

    def get_api_key(self, credentials: OAuthCredentials) -> str:
        return credentials.access


class TestRunLogin:
    """`run_login` happy/error paths."""

    async def test_unknown_provider_exits_one(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as info:
            await run_login("does-not-exist")
        assert info.value.code == 1
        assert "Unknown provider" in capsys.readouterr().out

    async def test_known_provider_runs_login(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        register_provider(_StubProvider())
        rc = await run_login("stub")
        assert rc == 0
        out = capsys.readouterr().out
        assert "Logged in to Stub Provider" in out


class TestRunLogout:
    """`run_logout` covers single-provider and bulk paths."""

    async def test_logout_specific(self, capsys: pytest.CaptureFixture[str]) -> None:
        save_credentials(
            "anthropic",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )
        rc = await run_logout("anthropic")
        assert rc == 0
        assert "Logged out of anthropic" in capsys.readouterr().out

    async def test_logout_missing_returns_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = await run_logout("anthropic")
        assert rc == 0
        assert "No stored credentials" in capsys.readouterr().out

    async def test_logout_all_drains_storage(
        self, capsys: pytest.CaptureFixture[str], fake_state_dir: Path
    ) -> None:
        save_credentials(
            "anthropic",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )
        save_credentials(
            "github-copilot",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )
        rc = await run_logout(None)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Logged out of anthropic" in out
        assert "Logged out of github-copilot" in out
        # Token JSONs are gone; sidecar `.lock` files survive on disk
        # for reuse by the next login. They're inert when no process
        # holds the flock.
        tokens_dir = fake_state_dir / "oauth-tokens"
        json_files = sorted(p.name for p in tokens_dir.iterdir() if p.suffix == ".json")
        assert json_files == []


class TestRunAuthList:
    """`run_auth_list` reports each registered provider's status."""

    def test_lists_built_ins_with_not_logged_in(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = run_auth_list()
        assert rc == 0
        out = capsys.readouterr().out
        assert "anthropic" in out
        assert "github-copilot" in out
        assert "openai-codex" in out
        assert "not logged in" in out

    def test_logged_in_provider_shows_expiry(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        save_credentials(
            "anthropic",
            OAuthCredentials(access="a", refresh="r", expires=time.time() + 7 * 86400),
        )
        rc = run_auth_list()
        assert rc == 0
        out = capsys.readouterr().out
        assert "expires in" in out


class TestHumanize:
    """`_humanize_seconds` rounding behaviour."""

    def test_zero_returns_zero(self) -> None:
        assert _humanize_seconds(0) == "0s"

    def test_negative_returns_zero(self) -> None:
        assert _humanize_seconds(-100) == "0s"

    def test_minutes_only(self) -> None:
        assert _humanize_seconds(125) == "2m"

    def test_hours_and_minutes(self) -> None:
        assert _humanize_seconds(3 * 3600 + 15 * 60) == "3h 15m"

    def test_days_skip_minutes(self) -> None:
        assert _humanize_seconds(2 * 86400 + 4 * 3600 + 30 * 60) == "2d 4h"

    def test_sub_minute(self) -> None:
        assert _humanize_seconds(45) == "<1m"
