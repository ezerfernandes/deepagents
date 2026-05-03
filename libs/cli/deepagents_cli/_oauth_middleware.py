"""Per-request OAuth wiring for chat models.

Three middlewares plug into the agent runtime alongside
`ConfigurableModelMiddleware`:

- `AnthropicOAuthIdentityMiddleware` — prepends the mandatory
  `"You are Claude Code, Anthropic's official CLI for Claude."` system
  prompt prefix when the resolved model uses Anthropic's OAuth token,
  AND refreshes the OAuth token before each request so sessions that
  outlive the ~1h Anthropic token lifetime keep working.

- `GitHubCopilotHeadersMiddleware` — adds per-call
  `X-Initiator` / `Copilot-Vision-Request` / `Openai-Intent` headers
  the Copilot proxy expects, AND refreshes the ~15-minute Copilot
  token before each request.

- `OpenAICodexOAuthMiddleware` — refreshes the ChatGPT Codex token
  before each request and re-injects the latest `chatgpt-account-id`
  (a refresh can mint a new account id on the JWT).

All three honour the same per-request contract: load stored
credentials, refresh if expired, then publish the live token (and any
other dynamic headers) under
`request.model_settings["extra_headers"]`. The Anthropic and OpenAI
SDKs forward those headers per-call, overriding whatever was wired
into `default_headers` at construction time. This is the failsafe for
long-running sessions: tokens rotate transparently as long as the
refresh token is still valid.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)

from deepagents_cli.oauth import (
    OAuthCredentials,
    OAuthError,
    get_stored_credentials,
    refresh_credentials,
)
from deepagents_cli.oauth.providers.anthropic import CLAUDE_CODE_IDENTITY_PROMPT
from deepagents_cli.oauth.providers.openai_codex import ACCOUNT_ID_KEY

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


_OAUTH_BETA_FLAG = "oauth-2025-04-20"
"""Marker beta we set on Anthropic OAuth requests; safe heuristic for
detecting that a request is OAuth-authenticated."""

_OAUTH_BETA_FIELD_HEADER = "anthropic-beta"
"""Default-headers key the Anthropic SDK consumes for `betas`. We don't
read `betas` directly because some langchain-anthropic versions stash
betas under `model_kwargs` or `default_headers` depending on path."""

_REFRESH_LEAD_SECONDS = 30
"""Refresh tokens this many seconds before they expire on the wire.

`OAuthCredentials.expires` already includes a 5-minute safety margin
applied at storage time. The extra 30s here protects against a request
that takes >5 min to reach the server (rare but possible for streaming
calls), without double-counting the storage margin in normal cases."""


# ---------------------------------------------------------------------------
# Provider detection (model heuristics)
# ---------------------------------------------------------------------------


def _model_uses_anthropic_oauth(model: object) -> bool:
    """Return whether *model* is an Anthropic ChatModel using an OAuth token.

    Heuristics (any one is sufficient): a `betas` attribute that
    includes `"oauth-2025-04-20"`, or a `default_headers` mapping that
    carries the same value under `anthropic-beta`. Both shapes are
    produced by `oauth._kwargs._anthropic_kwargs`.
    """
    betas = getattr(model, "betas", None)
    if isinstance(betas, (list, tuple)) and _OAUTH_BETA_FLAG in betas:
        return True
    headers = getattr(model, "default_headers", None)
    if isinstance(headers, Mapping):
        beta_value = headers.get(_OAUTH_BETA_FIELD_HEADER)
        if isinstance(beta_value, str) and _OAUTH_BETA_FLAG in beta_value:
            return True
    return False


def _model_is_copilot_anthropic(model: object) -> bool:
    """Return whether *model* talks to GitHub Copilot's Anthropic endpoint.

    Heuristic: `default_headers` carries the `Copilot-Integration-Id`
    that pi-mono and our login flow set as a constant.
    """
    headers = getattr(model, "default_headers", None)
    if not isinstance(headers, Mapping):
        return False
    return headers.get("Copilot-Integration-Id") == "vscode-chat"


def _model_is_openai_codex(model: object) -> bool:
    """Return whether *model* talks to ChatGPT Plus/Pro Codex.

    Heuristic: `default_headers` carries our `originator` tag from
    `oauth._kwargs._openai_codex_kwargs`. We could also inspect
    `base_url`, but `originator` is unique to our wiring.
    """
    headers = getattr(model, "default_headers", None)
    if not isinstance(headers, Mapping):
        return False
    return headers.get("originator") == "deepagents"


# ---------------------------------------------------------------------------
# Token refresh (sync + async bridges)
# ---------------------------------------------------------------------------


async def _ensure_fresh_credentials_async(
    provider_id: str,
) -> OAuthCredentials | None:
    """Load + refresh-if-expired credentials for *provider_id*.

    Returns the fresh `OAuthCredentials`, or `None` when no credentials
    are stored. A failed refresh logs a warning and returns the stale
    credentials so the request can still try (the server returns a 401
    with a clear message instead of a confusing middleware traceback).
    """
    import time

    creds = get_stored_credentials(provider_id)
    if creds is None:
        return None
    if time.time() + _REFRESH_LEAD_SECONDS < creds.expires:
        return creds
    try:
        return await refresh_credentials(provider_id, creds)
    except OAuthError:
        logger.warning(
            "OAuth refresh for %s failed; using current credentials. "
            "Run `deepagents login %s` if requests start failing.",
            provider_id,
            provider_id,
            exc_info=True,
        )
        return creds


def _ensure_fresh_credentials_sync(
    provider_id: str,
) -> OAuthCredentials | None:
    """Sync entry point that mirrors `_ensure_fresh_credentials_async`.

    Used by `wrap_model_call` paths. By LangChain's contract, sync
    middleware is invoked from sync code only, so it's safe to use
    `asyncio.run` here — there's no surrounding event loop.
    """
    return asyncio.run(_ensure_fresh_credentials_async(provider_id))


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _prepend_identity(prompt: str | None) -> str:
    """Return *prompt* with the Claude Code identity sentence on top.

    If the prompt already starts with the identity sentence, returns it
    unchanged so middlewares are idempotent across multiple agent loops.
    """
    if prompt and prompt.startswith(CLAUDE_CODE_IDENTITY_PROMPT):
        return prompt
    if not prompt:
        return CLAUDE_CODE_IDENTITY_PROMPT
    return f"{CLAUDE_CODE_IDENTITY_PROMPT}\n\n{prompt}"


def _anthropic_extra_headers(token: str) -> dict[str, str]:
    """Per-call headers carrying a fresh Anthropic OAuth token.

    Sets both `Authorization` and `x-api-key` because the Anthropic SDK
    auto-injects `x-api-key` from the construction-time `api_key`,
    which is the stale token. Setting `x-api-key` here overrides the
    SDK's default. Anthropic's OAuth backend accepts the OAuth token
    on either header — sending both is the failsafe.
    """
    return {
        "Authorization": f"Bearer {token}",
        "x-api-key": token,
    }


def _build_anthropic_request(
    request: ModelRequest, fresh_token: str | None
) -> ModelRequest:
    """Apply identity prefix and (optionally) fresh-token headers.

    Returns the same request object when nothing needs to change so we
    don't churn objects on the hot path.
    """
    if not _model_uses_anthropic_oauth(request.model):
        return request

    new_prompt = _prepend_identity(request.system_prompt)
    overrides: dict[str, Any] = {}
    if new_prompt != request.system_prompt:
        overrides["system_prompt"] = new_prompt

    if fresh_token is not None:
        settings = dict(request.model_settings or {})
        merged_extra = {
            **(settings.get("extra_headers") or {}),
            **_anthropic_extra_headers(fresh_token),
        }
        settings["extra_headers"] = merged_extra
        overrides["model_settings"] = settings

    if not overrides:
        return request
    return request.override(**overrides)


def _resolve_anthropic_token_async() -> Awaitable[str | None]:
    async def _go() -> str | None:
        creds = await _ensure_fresh_credentials_async("anthropic")
        return creds.access if creds is not None else None

    return _go()


def _resolve_anthropic_token_sync() -> str | None:
    creds = _ensure_fresh_credentials_sync("anthropic")
    return creds.access if creds is not None else None


class AnthropicOAuthIdentityMiddleware(AgentMiddleware):
    """Inject Anthropic OAuth identity + fresh-token headers per call.

    Anthropic's OAuth endpoint REQUIRES the system prompt to start with
    `"You are Claude Code, Anthropic's official CLI for Claude."`.
    Without it the API responds with `oauth_required` and refuses the
    request, regardless of the rest of the system prompt.

    The middleware also refreshes the OAuth access token before each
    request when the stored copy is close to expiry, then publishes
    the new token via `extra_headers` so it overrides whatever was
    baked into the model's `default_headers` at construction time. This
    is what keeps long sessions alive across the ~1h token lifetime.
    """

    def wrap_model_call(  # noqa: PLR6301
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        token = (
            _resolve_anthropic_token_sync()
            if _model_uses_anthropic_oauth(request.model)
            else None
        )
        return handler(_build_anthropic_request(request, token))

    async def awrap_model_call(  # noqa: PLR6301
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        token = (
            await _resolve_anthropic_token_async()
            if _model_uses_anthropic_oauth(request.model)
            else None
        )
        return await handler(_build_anthropic_request(request, token))


# ---------------------------------------------------------------------------
# GitHub Copilot
# ---------------------------------------------------------------------------


def _infer_copilot_initiator(messages: list[Any]) -> str:
    """Mirror `inferCopilotInitiator` from
    `pi-mono/packages/ai/src/providers/github-copilot-headers.ts`.

    Copilot expects `X-Initiator: user` on the very first turn and
    `X-Initiator: agent` for follow-ups (assistant- or tool-initiated).
    """
    if not messages:
        return "user"
    last = messages[-1]
    role = getattr(last, "type", None) or getattr(last, "role", None)
    if isinstance(role, str) and role.lower() in {"human", "user"}:
        return "user"
    return "agent"


def _has_image_input(messages: list[Any]) -> bool:
    """Detect image content blocks anywhere in the conversation."""
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            block_type = (
                block.get("type") if isinstance(block, dict) else None
            ) or getattr(block, "type", None)
            if block_type in {"image", "image_url", "input_image"}:
                return True
    return False


def _build_copilot_request(
    request: ModelRequest, fresh_token: str | None
) -> ModelRequest:
    if not _model_is_copilot_anthropic(request.model):
        return request

    extra_headers: dict[str, str] = {
        "X-Initiator": _infer_copilot_initiator(list(request.messages or [])),
        "Openai-Intent": "conversation-edits",
    }
    if _has_image_input(list(request.messages or [])):
        extra_headers["Copilot-Vision-Request"] = "true"
    if fresh_token is not None:
        extra_headers["Authorization"] = f"Bearer {fresh_token}"

    settings = dict(request.model_settings or {})
    merged_extra = {**(settings.get("extra_headers") or {}), **extra_headers}
    settings["extra_headers"] = merged_extra
    return request.override(model_settings=settings)


def _resolve_copilot_token_sync() -> str | None:
    creds = _ensure_fresh_credentials_sync("github-copilot")
    return creds.access if creds is not None else None


async def _resolve_copilot_token_async() -> str | None:
    creds = await _ensure_fresh_credentials_async("github-copilot")
    return creds.access if creds is not None else None


class GitHubCopilotHeadersMiddleware(AgentMiddleware):
    """Inject per-call dynamic headers + fresh OAuth token for Copilot.

    Constant Copilot headers (`User-Agent`, `Editor-Version`, etc.) are
    set once in `oauth._kwargs._github_copilot_kwargs`. Headers that
    depend on the conversation (`X-Initiator`,
    `Copilot-Vision-Request`) and the OAuth bearer token (refreshed
    every ~15 minutes) are surfaced through
    `request.model_settings["extra_headers"]`, which langchain-anthropic
    forwards to the underlying Anthropic SDK call.
    """

    def wrap_model_call(  # noqa: PLR6301
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        token = (
            _resolve_copilot_token_sync()
            if _model_is_copilot_anthropic(request.model)
            else None
        )
        return handler(_build_copilot_request(request, token))

    async def awrap_model_call(  # noqa: PLR6301
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        token = (
            await _resolve_copilot_token_async()
            if _model_is_copilot_anthropic(request.model)
            else None
        )
        return await handler(_build_copilot_request(request, token))


# ---------------------------------------------------------------------------
# OpenAI Codex
# ---------------------------------------------------------------------------


def _build_codex_request(
    request: ModelRequest, credentials: OAuthCredentials | None
) -> ModelRequest:
    if not _model_is_openai_codex(request.model) or credentials is None:
        return request

    account_id = credentials.extras.get(ACCOUNT_ID_KEY)
    if not isinstance(account_id, str) or not account_id:
        # Refresh did not include an account id — nothing safe to inject;
        # let the construction-time headers take their chances.
        return request

    extra_headers = {
        "Authorization": f"Bearer {credentials.access}",
        "chatgpt-account-id": account_id,
    }
    settings = dict(request.model_settings or {})
    merged_extra = {**(settings.get("extra_headers") or {}), **extra_headers}
    settings["extra_headers"] = merged_extra
    return request.override(model_settings=settings)


class OpenAICodexOAuthMiddleware(AgentMiddleware):
    """Refresh the ChatGPT Codex OAuth token before each request.

    Codex tokens are JWTs that carry the `chatgpt_account_id` claim;
    a refresh can mint a new account id, so we re-inject both the
    bearer token and the account-id header on every call. Without this,
    a session that outlives the JWT silently fails with 401 / 403.
    """

    def wrap_model_call(  # noqa: PLR6301
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        creds = (
            _ensure_fresh_credentials_sync("openai-codex")
            if _model_is_openai_codex(request.model)
            else None
        )
        return handler(_build_codex_request(request, creds))

    async def awrap_model_call(  # noqa: PLR6301
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        creds = (
            await _ensure_fresh_credentials_async("openai-codex")
            if _model_is_openai_codex(request.model)
            else None
        )
        return await handler(_build_codex_request(request, creds))


# ---------------------------------------------------------------------------
# Backwards-compat exports for tests
# ---------------------------------------------------------------------------


def _apply_anthropic_identity(request: ModelRequest) -> ModelRequest:
    """Identity-only wrapper retained for unit tests written before refresh.

    Returns the request unchanged when the model is not Anthropic OAuth,
    or with the identity prefix prepended otherwise. Skips the per-request
    refresh path so existing tests can exercise the prompt prefix logic
    without a full storage stub.
    """
    return _build_anthropic_request(request, fresh_token=None)


def _apply_copilot_headers(request: ModelRequest) -> ModelRequest:
    """Headers-only wrapper retained for unit tests written before refresh."""
    return _build_copilot_request(request, fresh_token=None)
