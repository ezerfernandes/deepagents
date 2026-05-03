"""Provider-specific kwargs for OAuth-authenticated chat requests.

Translates a stored `OAuthCredentials` value into the keyword arguments
LangChain's chat-model classes accept (`api_key`, `default_headers`,
`base_url`, `betas`, …). Called from `config._get_provider_kwargs` when
a user has logged in via `deepagents login` but has no environment-set
API key for the provider.

The shape of the returned dict mirrors what `init_chat_model` /
`ChatAnthropic` / `ChatOpenAI` accept so callers can `**spread` it
without further translation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents_cli.oauth.providers.anthropic import (
    CLAUDE_CODE_BETAS,
    CLAUDE_CODE_USER_AGENT_VERSION,
)
from deepagents_cli.oauth.providers.github_copilot import (
    COPILOT_HEADERS,
    ENTERPRISE_KEY,
    get_github_copilot_base_url,
)
from deepagents_cli.oauth.providers.openai_codex import (
    ACCOUNT_ID_KEY,
    ORIGINATOR,
)

if TYPE_CHECKING:
    from deepagents_cli.oauth.types import OAuthCredentials

# Stable mapping from `model_config.PROVIDER_API_KEY_ENV` keys to
# `oauth.registry` provider IDs. The Python conventions use
# `snake_case` for the provider key (`github_copilot`) while the OAuth
# registry uses pi-mono's hyphenated form (`github-copilot`).
PROVIDER_TO_OAUTH_ID: dict[str, str] = {
    "anthropic": "anthropic",
    "github_copilot": "github-copilot",
    "openai_codex": "openai-codex",
}

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
"""Base URL for ChatGPT Plus/Pro Codex Responses API requests.

Distinct from `https://api.openai.com/v1/responses` — the Codex variant
requires the OAuth-extracted `chatgpt-account-id` header on every request.
"""


def oauth_kwargs_for(provider: str, credentials: OAuthCredentials) -> dict[str, Any]:
    """Return ChatModel kwargs that carry *credentials* on the wire.

    Args:
        provider: The `model_config.PROVIDER_API_KEY_ENV` key
            (`anthropic`, `github_copilot`, `openai_codex`).
        credentials: Persisted OAuth credentials (already refreshed by
            the caller).

    Returns:
        A dict of kwargs to merge into the model constructor call. Keys
        are LangChain-supported (e.g. `api_key`, `default_headers`,
        `base_url`, `betas`).

    Raises:
        ValueError: If *provider* is not a known OAuth provider.
    """
    if provider == "anthropic":
        return _anthropic_kwargs(credentials)
    if provider == "github_copilot":
        return _github_copilot_kwargs(credentials)
    if provider == "openai_codex":
        return _openai_codex_kwargs(credentials)
    msg = f"OAuth kwargs not implemented for provider: {provider}"
    raise ValueError(msg)


def _anthropic_kwargs(credentials: OAuthCredentials) -> dict[str, Any]:
    """Kwargs for `langchain_anthropic.ChatAnthropic` with OAuth.

    The system-prompt prefix (`"You are Claude Code, ..."`) is NOT
    injected here — it's added at request time by
    `AnthropicOAuthIdentityMiddleware` so the model identity stays in
    sync with whatever model the runtime actually selects.
    """
    token = credentials.access
    return {
        "api_key": token,
        "betas": list(CLAUDE_CODE_BETAS),
        "default_headers": {
            # Anthropic's OAuth endpoint accepts Bearer auth on top of
            # the SDK's automatic `x-api-key` (added from `api_key`).
            # Including both is harmless — the server picks whichever
            # validates the OAuth token.
            "Authorization": f"Bearer {token}",
            "user-agent": f"claude-cli/{CLAUDE_CODE_USER_AGENT_VERSION}",
            "x-app": "cli",
        },
    }


def _github_copilot_kwargs(credentials: OAuthCredentials) -> dict[str, Any]:
    """Kwargs for `langchain_anthropic.ChatAnthropic` proxied through Copilot.

    GitHub Copilot exposes an Anthropic-compatible endpoint at
    `proxy.<flavor>.githubcopilot.com`; we send Bearer auth + the same
    `User-Agent`/`Editor-Version` headers the official VS Code
    extension uses (mirrored from
    `pi-mono/packages/ai/src/utils/oauth/github-copilot.ts:16-21`).
    """
    enterprise = credentials.extras.get(ENTERPRISE_KEY)
    base_url = get_github_copilot_base_url(credentials.access, enterprise)
    return {
        # The Copilot proxy validates the Bearer header, not x-api-key,
        # but ChatAnthropic still requires a non-empty `api_key`. Reuse
        # the OAuth token so both header paths agree.
        "api_key": credentials.access,
        "base_url": base_url,
        "default_headers": {
            "Authorization": f"Bearer {credentials.access}",
            **COPILOT_HEADERS,
        },
    }


def _openai_codex_kwargs(credentials: OAuthCredentials) -> dict[str, Any]:
    """Kwargs for `langchain_openai.ChatOpenAI(use_responses_api=True)` with Codex.

    The Codex endpoint at `https://chatgpt.com/backend-api/codex/responses`
    requires `chatgpt-account-id` (extracted from the JWT at login time)
    and `originator` headers — pi-mono uses `originator=pi`; we send
    `originator=deepagents` to identify our CLI distinctly.
    """
    account_id = credentials.extras.get(ACCOUNT_ID_KEY)
    if not isinstance(account_id, str) or not account_id:
        msg = (
            "OpenAI Codex credentials are missing 'accountId'. Run "
            "`deepagents login openai-codex` to refresh."
        )
        raise ValueError(msg)
    return {
        "api_key": credentials.access,
        "base_url": CODEX_BASE_URL,
        "use_responses_api": True,
        "default_headers": {
            "chatgpt-account-id": account_id,
            "originator": ORIGINATOR,
            "OpenAI-Beta": "responses=experimental",
        },
    }


def get_oauth_id_for_provider(provider: str) -> str | None:
    """Return the OAuth registry id for a `PROVIDER_API_KEY_ENV` key, if any."""
    return PROVIDER_TO_OAUTH_ID.get(provider)
