"""End-to-end integration tests for OAuth -> create_model wiring.

Locks the contracts that connect:
- `oauth._kwargs.oauth_kwargs_for` produces the right shape for each
  provider.
- `config._get_provider_kwargs` picks up stored credentials when no
  env var is set.
- `model_config.has_provider_credentials` recognises OAuth-only logins.
- `_oauth_middleware.AnthropicOAuthIdentityMiddleware` prepends the
  Claude Code identity exactly when the model uses OAuth.
- `_oauth_middleware.GitHubCopilotHeadersMiddleware` injects per-call
  Copilot headers exactly when the model talks to the Copilot proxy.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from deepagents_cli._oauth_middleware import (
    _apply_anthropic_identity,
    _apply_copilot_headers,
    _model_is_copilot_anthropic,
    _model_uses_anthropic_oauth,
    _prepend_identity,
)
from deepagents_cli.oauth import OAuthCredentials
from deepagents_cli.oauth._kwargs import (
    CODEX_BASE_URL,
    oauth_kwargs_for,
)
from deepagents_cli.oauth.providers.anthropic import (
    CLAUDE_CODE_BETAS,
    CLAUDE_CODE_IDENTITY_PROMPT,
    CLAUDE_CODE_USER_AGENT_VERSION,
)
from deepagents_cli.oauth.providers.github_copilot import (
    COPILOT_HEADERS,
    ENTERPRISE_KEY,
)
from deepagents_cli.oauth.providers.openai_codex import (
    ACCOUNT_ID_KEY,
    ORIGINATOR,
)
from deepagents_cli.oauth.storage import save_credentials

pytestmark = pytest.mark.usefixtures("fake_state_dir")


class TestOAuthKwargs:
    """Per-provider kwarg shapes for chat-model construction."""

    def test_anthropic(self) -> None:
        creds = OAuthCredentials(access="sk-ant-oat-T", refresh="r", expires=0.0)
        kwargs = oauth_kwargs_for("anthropic", creds)
        assert kwargs["api_key"] == "sk-ant-oat-T"
        assert kwargs["betas"] == list(CLAUDE_CODE_BETAS)
        assert kwargs["default_headers"]["Authorization"] == "Bearer sk-ant-oat-T"
        assert kwargs["default_headers"]["user-agent"] == (
            f"claude-cli/{CLAUDE_CODE_USER_AGENT_VERSION}"
        )
        assert kwargs["default_headers"]["x-app"] == "cli"

    def test_github_copilot(self) -> None:
        creds = OAuthCredentials(
            access=("tid=x;exp=99;proxy-ep=proxy.individual.githubcopilot.com"),
            refresh="ghu_user",
            expires=0.0,
        )
        kwargs = oauth_kwargs_for("github_copilot", creds)
        assert kwargs["base_url"] == "https://api.individual.githubcopilot.com"
        assert kwargs["default_headers"]["Authorization"].startswith("Bearer ")
        for key, value in COPILOT_HEADERS.items():
            assert kwargs["default_headers"][key] == value

    def test_github_copilot_enterprise(self) -> None:
        creds = OAuthCredentials(
            access="tid=x",
            refresh="ghu_user",
            expires=0.0,
            extras={ENTERPRISE_KEY: "company.ghe.com"},
        )
        kwargs = oauth_kwargs_for("github_copilot", creds)
        assert kwargs["base_url"] == "https://copilot-api.company.ghe.com"

    def test_openai_codex(self) -> None:
        creds = OAuthCredentials(
            access="jwt-token",
            refresh="ref",
            expires=0.0,
            extras={ACCOUNT_ID_KEY: "acct-XYZ"},
        )
        kwargs = oauth_kwargs_for("openai_codex", creds)
        assert kwargs["base_url"] == CODEX_BASE_URL
        assert kwargs["use_responses_api"] is True
        assert kwargs["default_headers"]["chatgpt-account-id"] == "acct-XYZ"
        assert kwargs["default_headers"]["originator"] == ORIGINATOR

    def test_openai_codex_missing_account_raises(self) -> None:
        creds = OAuthCredentials(access="jwt", refresh="r", expires=0.0)
        with pytest.raises(ValueError, match="accountId"):
            oauth_kwargs_for("openai_codex", creds)

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="OAuth kwargs"):
            oauth_kwargs_for(
                "unknown-provider",
                OAuthCredentials(access="a", refresh="r", expires=0.0),
            )


class TestProviderKwargsHook:
    """`config._get_provider_kwargs` integrates with OAuth storage."""

    def test_anthropic_oauth_pulls_kwargs_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DEEPAGENTS_CLI_ANTHROPIC_API_KEY", raising=False)

        save_credentials(
            "anthropic",
            OAuthCredentials(
                access="sk-ant-oat-LIVE",
                refresh="r",
                expires=time.time() + 3600,
            ),
        )

        from deepagents_cli.config import _get_provider_kwargs

        kwargs = _get_provider_kwargs("anthropic", model_name="claude-sonnet-4-5")
        assert kwargs["api_key"] == "sk-ant-oat-LIVE"
        assert "oauth-2025-04-20" in kwargs["betas"]
        headers = kwargs["default_headers"]
        assert headers["Authorization"].startswith("Bearer ")

    def test_env_var_wins_over_oauth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-FROM-ENV")

        save_credentials(
            "anthropic",
            OAuthCredentials(access="sk-ant-oat-IGNORED", refresh="r", expires=0.0),
        )

        from deepagents_cli.config import _get_provider_kwargs

        kwargs = _get_provider_kwargs("anthropic", model_name="claude-sonnet-4-5")
        # Env-var path returns the api_key without the OAuth headers.
        assert kwargs["api_key"] == "sk-ant-api-FROM-ENV"
        assert "betas" not in kwargs
        assert "default_headers" not in kwargs

    def test_user_default_headers_layer_over_oauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User-supplied `default_headers` in `config.toml` shouldn't drop OAuth headers.

        Wires a stub `ModelConfig.get_kwargs` so we can assert the merge
        keeps both sets without a real config file on disk.
        """
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        save_credentials(
            "anthropic",
            OAuthCredentials(
                access="sk-ant-oat-LIVE",
                refresh="r",
                expires=time.time() + 3600,
            ),
        )

        from deepagents_cli import config as cfg
        from deepagents_cli.model_config import ModelConfig

        original_get_kwargs = ModelConfig.get_kwargs

        def _patched(self, provider, *, model_name=None):
            base = original_get_kwargs(self, provider, model_name=model_name)
            if provider == "anthropic":
                base["default_headers"] = {"x-team-tag": "blue"}
            return base

        monkeypatch.setattr(ModelConfig, "get_kwargs", _patched)

        kwargs = cfg._get_provider_kwargs("anthropic", model_name="claude-sonnet-4-5")
        headers = kwargs["default_headers"]
        # User header still present.
        assert headers["x-team-tag"] == "blue"
        # OAuth headers also preserved.
        assert headers["Authorization"].startswith("Bearer ")


class TestHasProviderCredentials:
    """`has_provider_credentials` treats OAuth logins as configured."""

    def test_oauth_only_provider_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        save_credentials(
            "anthropic",
            OAuthCredentials(access="x", refresh="r", expires=time.time() + 100),
        )

        from deepagents_cli.model_config import has_provider_credentials

        assert has_provider_credentials("anthropic") is True

    def test_no_creds_no_env_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from deepagents_cli.model_config import has_provider_credentials

        assert has_provider_credentials("anthropic") is False


class TestAnthropicIdentityMiddleware:
    """`AnthropicOAuthIdentityMiddleware` prepends only when needed."""

    def test_prepend_idempotent(self) -> None:
        once = _prepend_identity("Hello")
        twice = _prepend_identity(once)
        assert once == twice
        assert once.startswith(CLAUDE_CODE_IDENTITY_PROMPT)

    def test_detects_oauth_via_betas_attr(self) -> None:
        class FakeModel:
            betas = ["oauth-2025-04-20"]
            default_headers: dict[str, str] = {}

        assert _model_uses_anthropic_oauth(FakeModel()) is True

    def test_detects_oauth_via_default_headers(self) -> None:
        class FakeModel:
            betas = None
            default_headers = {
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20"
            }

        assert _model_uses_anthropic_oauth(FakeModel()) is True

    def test_no_oauth_no_change(self) -> None:
        class FakeModel:
            betas = None
            default_headers: dict[str, str] = {}

        class FakeRequest:
            def __init__(self) -> None:
                self.model = FakeModel()
                self.system_prompt = "user prompt"

            def override(self, **kwargs):  # noqa: ANN003, ARG002
                # Should not be called.
                msg = "override should not be called"
                raise AssertionError(msg)

        request = FakeRequest()
        assert _apply_anthropic_identity(request) is request


class TestCopilotHeadersMiddleware:
    """`GitHubCopilotHeadersMiddleware` adds dynamic headers per call."""

    def test_detects_copilot_via_integration_id(self) -> None:
        class FakeModel:
            default_headers = {"Copilot-Integration-Id": "vscode-chat"}

        assert _model_is_copilot_anthropic(FakeModel()) is True

    def test_initiator_user_for_first_user_message(self) -> None:
        from langchain_core.messages import HumanMessage

        class FakeModel:
            default_headers = {"Copilot-Integration-Id": "vscode-chat"}

        captured: dict[str, object] = {}

        class FakeRequest:
            def __init__(self) -> None:
                self.model = FakeModel()
                self.messages = [HumanMessage(content="hi")]
                self.model_settings: dict[str, object] = {}

            def override(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return self

        request = FakeRequest()
        _apply_copilot_headers(request)
        headers = captured["model_settings"]["extra_headers"]  # type: ignore[index]
        assert headers["X-Initiator"] == "user"
        assert headers["Openai-Intent"] == "conversation-edits"
        assert "Copilot-Vision-Request" not in headers

    def test_initiator_agent_for_followup(self) -> None:
        from langchain_core.messages import AIMessage

        class FakeModel:
            default_headers = {"Copilot-Integration-Id": "vscode-chat"}

        captured: dict[str, object] = {}

        class FakeRequest:
            def __init__(self) -> None:
                self.model = FakeModel()
                self.messages = [AIMessage(content="working on it")]
                self.model_settings: dict[str, object] = {}

            def override(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return self

        _apply_copilot_headers(FakeRequest())
        headers = captured["model_settings"]["extra_headers"]  # type: ignore[index]
        assert headers["X-Initiator"] == "agent"

    def test_copilot_vision_for_image_input(self) -> None:
        from langchain_core.messages import HumanMessage

        class FakeModel:
            default_headers = {"Copilot-Integration-Id": "vscode-chat"}

        captured: dict[str, object] = {}

        class FakeRequest:
            def __init__(self) -> None:
                self.model = FakeModel()
                self.messages = [
                    HumanMessage(
                        content=[
                            {"type": "text", "text": "what's this?"},
                            {"type": "image", "source_type": "base64", "data": "..."},
                        ]
                    )
                ]
                self.model_settings: dict[str, object] = {}

            def override(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return self

        _apply_copilot_headers(FakeRequest())
        headers = captured["model_settings"]["extra_headers"]  # type: ignore[index]
        assert headers["Copilot-Vision-Request"] == "true"

    def test_non_copilot_model_unchanged(self) -> None:
        class FakeModel:
            default_headers: dict[str, str] = {}

        class FakeRequest:
            def __init__(self) -> None:
                self.model = FakeModel()
                self.messages = []
                self.model_settings: dict[str, object] = {}

            def override(self, **kwargs):  # noqa: ANN003, ARG002
                msg = "override should not fire"
                raise AssertionError(msg)

        request = FakeRequest()
        assert _apply_copilot_headers(request) is request
