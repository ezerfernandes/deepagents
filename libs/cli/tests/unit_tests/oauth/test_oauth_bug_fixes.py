"""Regression tests for `bugs.md`.

Each `TestBugN` class pins the behaviour required by the corresponding
bug entry. Keep these tests focused: they should fail before the fix
and pass after — adding ambient OAuth coverage belongs in the other
test modules in this directory.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from deepagents_cli.oauth import OAuthCredentials, register_provider
from deepagents_cli.oauth.providers.openai_codex import ACCOUNT_ID_KEY
from deepagents_cli.oauth.storage import (
    _STORAGE_VERSION,
    delete_credentials,
    save_credentials,
)
from deepagents_cli.oauth_commands import run_auth_list

if TYPE_CHECKING:
    from deepagents_cli.oauth.types import OAuthCallbacks


pytestmark = pytest.mark.usefixtures("fake_state_dir")


# ---------------------------------------------------------------------------
# Bug 1 — `_merge_oauth_kwargs` must not crash inside a running event loop.
# ---------------------------------------------------------------------------


class TestBug1RunningLoopBridge:
    """`_merge_oauth_kwargs` defers refresh when a loop is already running."""

    async def test_returns_kwargs_without_crashing_in_running_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The original bug raised `RuntimeError` here; we now skip the refresh."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        save_credentials(
            "anthropic",
            OAuthCredentials(
                access="sk-ant-oat-EXPIRED",
                refresh="r",
                # already past — would normally trigger a refresh
                expires=time.time() - 60,
            ),
        )

        # `pytest-asyncio` runs this inside a loop; the bug fired exactly here.
        from deepagents_cli.config import _get_provider_kwargs

        kwargs = _get_provider_kwargs("anthropic", model_name="claude-sonnet-4-5")
        # We did not refresh — the access token is whatever was on disk.
        assert kwargs["api_key"] == "sk-ant-oat-EXPIRED"
        # OAuth headers still wired so the per-request middleware can refresh later.
        assert "oauth-2025-04-20" in kwargs["betas"]

    def test_refresh_runs_when_no_loop_is_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sync callers (CLI startup) still get the refreshed token in-line."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        refreshed_with: dict[str, str] = {}

        # Stub `refresh_credentials` so we observe whether it ran.
        from deepagents_cli.oauth import _api as oauth_api

        async def _fake_refresh(provider_id, credentials):  # noqa: ARG001
            refreshed_with["called"] = "yes"
            return OAuthCredentials(
                access="sk-ant-oat-FRESH",
                refresh=credentials.refresh,
                expires=time.time() + 3600,
            )

        monkeypatch.setattr(oauth_api, "refresh_credentials", _fake_refresh)
        # config.py does `from deepagents_cli.oauth import refresh_credentials`
        # at call time, so the package re-export also needs the stub.
        import deepagents_cli.oauth as oauth_pkg

        monkeypatch.setattr(oauth_pkg, "refresh_credentials", _fake_refresh)
        save_credentials(
            "anthropic",
            OAuthCredentials(
                access="sk-ant-oat-OLD",
                refresh="r",
                expires=time.time() - 60,
            ),
        )

        from deepagents_cli.config import _get_provider_kwargs

        kwargs = _get_provider_kwargs("anthropic", model_name="claude-sonnet-4-5")
        assert refreshed_with == {"called": "yes"}
        assert kwargs["api_key"] == "sk-ant-oat-FRESH"


# ---------------------------------------------------------------------------
# Bug 2 — middlewares refresh tokens before each request and inject them.
# ---------------------------------------------------------------------------


class _RecordingHandler:
    """Coroutine-shaped handler that records the request it received."""

    def __init__(self) -> None:
        self.received: list[object] = []

    async def __call__(self, request):
        self.received.append(request)
        return "ok"


class _FakeRequest:
    """Just enough of `ModelRequest` to drive the middleware methods."""

    def __init__(
        self,
        model: object,
        *,
        messages: list[object] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.model = model
        self.messages = messages or []
        self.system_prompt = system_prompt
        self.model_settings: dict[str, object] = {}

    def override(self, **kwargs):  # noqa: ANN003
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self


class TestBug2PerRequestRefresh:
    """Middlewares refresh tokens and publish them as `extra_headers`."""

    async def test_anthropic_refreshes_and_injects_authorization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from deepagents_cli._oauth_middleware import (
            AnthropicOAuthIdentityMiddleware,
        )

        save_credentials(
            "anthropic",
            OAuthCredentials(
                access="stale-anthropic",
                refresh="r",
                expires=time.time() - 1,
            ),
        )

        captured: dict[str, OAuthCredentials] = {}

        async def _fake_refresh(provider_id, credentials):
            assert provider_id == "anthropic"
            fresh = OAuthCredentials(
                access="fresh-anthropic",
                refresh=credentials.refresh,
                expires=time.time() + 3600,
            )
            captured["fresh"] = fresh
            return fresh

        # Patch where the consumers look it up: the middleware imports it
        # at load time from `deepagents_cli.oauth`; the config helper does
        # `from deepagents_cli.oauth import refresh_credentials` at call
        # time, so updating the package re-export catches both.
        import deepagents_cli._oauth_middleware as middleware
        import deepagents_cli.oauth as oauth_pkg

        monkeypatch.setattr(oauth_pkg, "refresh_credentials", _fake_refresh)
        monkeypatch.setattr(middleware, "refresh_credentials", _fake_refresh)

        class FakeAnthropicModel:
            betas = ["oauth-2025-04-20"]
            default_headers: dict[str, str] = {}

        request = _FakeRequest(
            FakeAnthropicModel(), system_prompt="Be helpful.", messages=[]
        )
        handler = _RecordingHandler()
        await AnthropicOAuthIdentityMiddleware().awrap_model_call(request, handler)

        forwarded = handler.received[0]
        # Identity prefix prepended.
        assert forwarded.system_prompt.startswith("You are Claude Code")
        # Fresh token injected via extra_headers, overriding stale default.
        headers = forwarded.model_settings["extra_headers"]
        assert headers["Authorization"] == "Bearer fresh-anthropic"
        assert headers["x-api-key"] == "fresh-anthropic"
        assert captured["fresh"].access == "fresh-anthropic"

    async def test_anthropic_skips_refresh_when_not_oauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-OAuth Anthropic models pass through untouched."""
        from deepagents_cli._oauth_middleware import (
            AnthropicOAuthIdentityMiddleware,
        )

        called = False

        async def _fake_refresh(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            nonlocal called
            called = True

        # Patch where the consumers look it up: the middleware imports it
        # at load time from `deepagents_cli.oauth`; the config helper does
        # `from deepagents_cli.oauth import refresh_credentials` at call
        # time, so updating the package re-export catches both.
        import deepagents_cli._oauth_middleware as middleware
        import deepagents_cli.oauth as oauth_pkg

        monkeypatch.setattr(oauth_pkg, "refresh_credentials", _fake_refresh)
        monkeypatch.setattr(middleware, "refresh_credentials", _fake_refresh)

        class FakePlainAnthropic:
            betas = None
            default_headers = {"x-api-key": "real-api-key"}

        request = _FakeRequest(FakePlainAnthropic(), system_prompt="Be helpful.")
        handler = _RecordingHandler()
        await AnthropicOAuthIdentityMiddleware().awrap_model_call(request, handler)
        assert called is False

    async def test_copilot_refreshes_and_merges_with_dynamic_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from langchain_core.messages import HumanMessage

        from deepagents_cli._oauth_middleware import (
            GitHubCopilotHeadersMiddleware,
        )

        save_credentials(
            "github-copilot",
            OAuthCredentials(
                access="stale-copilot",
                refresh="ghu_user",
                expires=time.time() - 1,
            ),
        )

        async def _fake_refresh(provider_id, credentials):
            assert provider_id == "github-copilot"
            return OAuthCredentials(
                access="fresh-copilot",
                refresh=credentials.refresh,
                expires=time.time() + 900,
            )

        # Patch where the consumers look it up: the middleware imports it
        # at load time from `deepagents_cli.oauth`; the config helper does
        # `from deepagents_cli.oauth import refresh_credentials` at call
        # time, so updating the package re-export catches both.
        import deepagents_cli._oauth_middleware as middleware
        import deepagents_cli.oauth as oauth_pkg

        monkeypatch.setattr(oauth_pkg, "refresh_credentials", _fake_refresh)
        monkeypatch.setattr(middleware, "refresh_credentials", _fake_refresh)

        class FakeCopilot:
            default_headers = {"Copilot-Integration-Id": "vscode-chat"}

        request = _FakeRequest(FakeCopilot(), messages=[HumanMessage(content="hi")])
        handler = _RecordingHandler()
        await GitHubCopilotHeadersMiddleware().awrap_model_call(request, handler)

        headers = handler.received[0].model_settings["extra_headers"]
        assert headers["Authorization"] == "Bearer fresh-copilot"
        assert headers["X-Initiator"] == "user"
        assert headers["Openai-Intent"] == "conversation-edits"

    async def test_codex_refreshes_and_injects_account_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from deepagents_cli._oauth_middleware import OpenAICodexOAuthMiddleware

        save_credentials(
            "openai-codex",
            OAuthCredentials(
                access="stale-codex",
                refresh="r",
                expires=time.time() - 1,
                extras={ACCOUNT_ID_KEY: "acct-OLD"},
            ),
        )

        async def _fake_refresh(provider_id, credentials):
            assert provider_id == "openai-codex"
            return OAuthCredentials(
                access="fresh-codex",
                refresh=credentials.refresh,
                expires=time.time() + 3600,
                extras={ACCOUNT_ID_KEY: "acct-NEW"},
            )

        # Patch where the consumers look it up: the middleware imports it
        # at load time from `deepagents_cli.oauth`; the config helper does
        # `from deepagents_cli.oauth import refresh_credentials` at call
        # time, so updating the package re-export catches both.
        import deepagents_cli._oauth_middleware as middleware
        import deepagents_cli.oauth as oauth_pkg

        monkeypatch.setattr(oauth_pkg, "refresh_credentials", _fake_refresh)
        monkeypatch.setattr(middleware, "refresh_credentials", _fake_refresh)

        class FakeCodex:
            default_headers = {"originator": "deepagents"}

        request = _FakeRequest(FakeCodex())
        handler = _RecordingHandler()
        await OpenAICodexOAuthMiddleware().awrap_model_call(request, handler)

        headers = handler.received[0].model_settings["extra_headers"]
        assert headers["Authorization"] == "Bearer fresh-codex"
        assert headers["chatgpt-account-id"] == "acct-NEW"

    async def test_refresh_failure_falls_back_to_stale_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed refresh logs a warning but lets the request still try.

        Better to surface a 401 from the upstream API than swallow the
        request silently inside the middleware.
        """
        from deepagents_cli import _oauth_middleware as middleware_mod
        from deepagents_cli._oauth_middleware import (
            AnthropicOAuthIdentityMiddleware,
        )
        from deepagents_cli.oauth import OAuthRefreshError

        save_credentials(
            "anthropic",
            OAuthCredentials(
                access="stale-token",
                refresh="r",
                expires=time.time() - 1,
            ),
        )

        async def _fake_refresh(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            msg = "network down"
            raise OAuthRefreshError(msg)

        # `_oauth_middleware` imports `refresh_credentials` at module
        # top, so we patch the bound name there rather than on
        # `_api` — patching `_api` doesn't reach the middleware.
        monkeypatch.setattr(middleware_mod, "refresh_credentials", _fake_refresh)

        class FakeAnthropic:
            betas = ["oauth-2025-04-20"]
            default_headers: dict[str, str] = {}

        request = _FakeRequest(FakeAnthropic(), system_prompt="x")
        handler = _RecordingHandler()
        await AnthropicOAuthIdentityMiddleware().awrap_model_call(request, handler)
        headers = handler.received[0].model_settings["extra_headers"]
        # Stale token is still injected — failing closed at the middleware
        # would just hide the real reason from the user.
        assert headers["Authorization"] == "Bearer stale-token"


# ---------------------------------------------------------------------------
# Bug 3 — `delete_credentials` does not race against concurrent unlinks.
# ---------------------------------------------------------------------------


class TestBug3DeleteRace:
    """Concurrent `delete_credentials` calls don't surface FileNotFoundError."""

    def test_returns_false_when_file_disappears_between_check_and_unlink(
        self,
        fake_state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_credentials(
            "anthropic",
            OAuthCredentials(access="a", refresh="r", expires=0.0),
        )

        path = fake_state_dir / "oauth-tokens" / "anthropic.json"
        original_unlink = Path.unlink

        def _racing_unlink(self, *, missing_ok=False):
            if self == path:
                # Simulate a parallel logout that removed the file first.
                original_unlink(self)
                raise FileNotFoundError(self)
            return original_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _racing_unlink)
        # Must not raise.
        result = delete_credentials("anthropic")
        assert result is False
        # The file actually got removed (by the racing unlink), so a
        # follow-up call also returns False without raising.
        monkeypatch.setattr(Path, "unlink", original_unlink)
        assert delete_credentials("anthropic") is False


# ---------------------------------------------------------------------------
# Bug 4 — `auth list` survives corrupt token files.
# ---------------------------------------------------------------------------


class TestBug4AuthListResilience:
    """A single corrupt token file does not turn the listing into a crash."""

    def test_corrupt_file_renders_recovery_hint(
        self,
        fake_state_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Plant a file with the wrong schema version.
        path = fake_state_dir / "oauth-tokens" / "anthropic.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": _STORAGE_VERSION + 99,
                    "type": "oauth",
                    "provider": "anthropic",
                    "access": "a",
                    "refresh": "r",
                    "expires": 0.0,
                }
            )
        )

        rc = run_auth_list()
        assert rc == 0
        out = capsys.readouterr().out
        # Recovery hint includes the exact CLI command to copy-paste.
        assert "corrupt" in out
        assert "deepagents login anthropic" in out
        # Other providers still listed normally.
        assert "github-copilot" in out
        assert "openai-codex" in out


# ---------------------------------------------------------------------------
# Bug: provider name hyphen/underscore mismatch breaks --model routing.
#
# `deepagents login github-copilot` teaches users the hyphenated form, but
# all internal dicts (PROVIDER_API_KEY_ENV, _BUILT_IN_OAUTH_CLASS_PATHS,
# PROVIDER_TO_OAUTH_ID) use underscore keys.  `create_model` must normalise
# before any lookup so `github-copilot:model` resolves identically to
# `github_copilot:model`.
# ---------------------------------------------------------------------------


class TestProviderNameNormalization:
    """Hyphenated provider names are normalised before credential/class lookups."""

    def test_hyphenated_github_copilot_picks_up_oauth_credentials(
        self,
        fake_state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """create_model('github-copilot:model') routes through OAuth, not init_chat_model."""
        from unittest.mock import MagicMock, patch

        from deepagents_cli.oauth.providers.openai_codex import ACCOUNT_ID_KEY
        from deepagents_cli.oauth.storage import save_credentials

        # Store valid-looking GitHub Copilot credentials (access token has
        # the proxy-ep shape the kwargs builder expects for base_url).
        save_credentials(
            "github-copilot",
            OAuthCredentials(
                access="tid=x;exp=9999999999;proxy-ep=proxy.individual.githubcopilot.com;tok=x",
                refresh="ghu_refreshtoken",
                expires=time.time() + 3600,
            ),
        )

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        captured: dict = {}

        def _fake_create_from_class(class_path, model_name, provider, kwargs):
            captured["class_path"] = class_path
            captured["provider"] = provider
            captured["api_key"] = kwargs.get("api_key")
            return MagicMock()

        with patch(
            "deepagents_cli.config._create_model_from_class",
            side_effect=_fake_create_from_class,
        ):
            from deepagents_cli.config import create_model

            create_model("github-copilot:claude-3-5-sonnet-20241022")

        # Normalization applied: underscore form used for all lookups.
        assert captured["provider"] == "github_copilot"
        # Class path resolved via _BUILT_IN_OAUTH_CLASS_PATHS — NOT init_chat_model.
        assert captured["class_path"] == "langchain_anthropic.chat_models:ChatAnthropic"
        # OAuth credentials picked up: api_key is the stored access token.
        assert captured["api_key"] is not None

    def test_hyphenated_openai_codex_picks_up_oauth_credentials(
        self,
        fake_state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """create_model('openai-codex:model') routes through OAuth."""
        from unittest.mock import MagicMock, patch

        from deepagents_cli.oauth.providers.openai_codex import ACCOUNT_ID_KEY
        from deepagents_cli.oauth.storage import save_credentials

        save_credentials(
            "openai-codex",
            OAuthCredentials(
                access="jwt_access_token",
                refresh="rt",
                expires=time.time() + 3600,
                extras={ACCOUNT_ID_KEY: "acct_123"},
            ),
        )

        monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)

        captured: dict = {}

        def _fake_create_from_class(class_path, model_name, provider, kwargs):
            captured["class_path"] = class_path
            captured["provider"] = provider
            captured["api_key"] = kwargs.get("api_key")
            return MagicMock()

        with patch(
            "deepagents_cli.config._create_model_from_class",
            side_effect=_fake_create_from_class,
        ):
            from deepagents_cli.config import create_model

            create_model("openai-codex:gpt-4o")

        assert captured["provider"] == "openai_codex"
        assert captured["class_path"] == "langchain_openai.chat_models:ChatOpenAI"
        assert captured["api_key"] == "jwt_access_token"
