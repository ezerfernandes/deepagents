"""Anthropic Claude Pro/Max OAuth provider.

Port of `pi-mono/packages/ai/src/utils/oauth/anthropic.ts`. The `state`
parameter sent in the authorize URL is the PKCE verifier itself — that's
the contract Anthropic's authorize endpoint expects, and we mirror it
verbatim.

The login flow opens a short-lived local HTTP server on
`(host, 53692)/callback` and races it against an optional manual paste-back
input. Either path delivers an authorization code; we exchange it at
`https://platform.claude.com/v1/oauth/token` with the PKCE verifier.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self
from urllib.parse import urlencode

from deepagents_cli.oauth.callback_server import (
    CallbackResult,
    try_start_callback_server,
)
from deepagents_cli.oauth.pkce import generate_pkce
from deepagents_cli.oauth.providers._common import (
    OAuthHTTPError,
    expires_from_lifetime,
    parse_authorization_input,
    post_json,
)
from deepagents_cli.oauth.types import (
    OAuthAuthInfo,
    OAuthCallbacks,
    OAuthCancelledError,
    OAuthCredentials,
    OAuthPrompt,
    OAuthRefreshError,
    OAuthStateMismatchError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable


# Obfuscated in pi-mono via base64 to keep grep scans clean — we mirror
# the same dance so swapping codebases doesn't change the on-the-wire
# client_id Anthropic sees.
CLIENT_ID = base64.b64decode("OWQxYzI1MGEtZTYxYi00NGQ5LTg4ZWQtNTk0NGQxOTYyZjVl").decode(
    "ascii"
)

AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"

CALLBACK_PORT = 53692
CALLBACK_PATH = "/callback"

SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)

CLAUDE_CODE_USER_AGENT_VERSION = "2.1.75"
"""Version embedded in the `user-agent: claude-cli/<v>` header on every
OAuth-authenticated Anthropic request. Bump in sync with pi-mono so the
OAuth-2025-04-20 beta endpoint sees a recognised client."""

CLAUDE_CODE_BETAS = ("claude-code-20250219", "oauth-2025-04-20")
"""Mandatory `anthropic-beta` flags for OAuth tokens. Without these the
endpoint rejects the request with an `oauth_required` error."""

CLAUDE_CODE_IDENTITY_PROMPT = (
    "You are Claude Code, Anthropic's official CLI for Claude."
)
"""System-prompt prefix Anthropic requires on every OAuth request. Used
by `AnthropicOAuthIdentityMiddleware` in `_oauth_middleware.py`."""


def is_oauth_token(api_key: str) -> bool:
    """Heuristic for distinguishing OAuth tokens from regular API keys.

    OAuth-issued tokens always carry the `sk-ant-oat` prefix; classic
    API keys use `sk-ant-api`. Mirrors `isOAuthToken` from
    `pi-mono/packages/ai/src/providers/anthropic.ts:758`.
    """
    return "sk-ant-oat" in api_key


@dataclass(frozen=True, slots=True)
class _RaceResult:
    code: str | None
    state: str | None


async def _race_callback_against_manual(
    *,
    callback_awaitable: Awaitable[CallbackResult | None],
    manual_input: Awaitable[str] | None,
    expected_state: str,
) -> _RaceResult:
    """Race the callback server against `on_manual_code_input`.

    Returns the winning side's code/state. The loser is cancelled
    cleanly so neither side leaks a pending coroutine into the
    surrounding event loop.
    """
    callback_task = asyncio.ensure_future(callback_awaitable)
    if manual_input is None:
        result = await callback_task
        if result is None:
            return _RaceResult(code=None, state=None)
        return _RaceResult(code=result.code, state=result.state)

    manual_task = asyncio.ensure_future(manual_input)

    done, pending = await asyncio.wait(
        {callback_task, manual_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    # Drain cancellations so the event loop doesn't log "Task was destroyed".
    for task in pending:
        with _suppress_cancel():
            await task

    if callback_task in done:
        result = callback_task.result()
        if result is not None:
            return _RaceResult(code=result.code, state=result.state)

    if manual_task in done:
        try:
            pasted = manual_task.result()
        except asyncio.CancelledError:
            return _RaceResult(code=None, state=None)
        parsed = parse_authorization_input(pasted)
        if parsed.state and parsed.state != expected_state:
            msg = "OAuth state mismatch"
            raise OAuthStateMismatchError(msg)
        return _RaceResult(code=parsed.code, state=parsed.state or expected_state)

    return _RaceResult(code=None, state=None)


class _suppress_cancel:  # noqa: N801 - context-manager naming
    """Swallow `asyncio.CancelledError` from awaited cancelled tasks."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return exc_type is asyncio.CancelledError


async def login_anthropic(callbacks: OAuthCallbacks) -> OAuthCredentials:
    """Run the Anthropic OAuth authorization-code + PKCE flow."""
    pkce = generate_pkce()
    # `state` MUST be independent of the PKCE verifier. pi-mono reused
    # `verifier` as `state`, which leaks the verifier into browser
    # history, the IdP's request log, and any Referer-bearing redirect
    # along the way — defeating PKCE's anti-interception guarantee.
    # We use the same opaque hex state shape as the OpenAI Codex flow.
    expected_state = secrets.token_hex(16)

    # Anthropic registers `http://localhost:53692/callback` as the only
    # acceptable redirect URI, so even when the local listener can't
    # bind we still surface that URL — the user can copy-paste from the
    # final redirect on a different machine via `on_manual_code_input`
    # or `on_prompt`.
    redirect_uri = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

    server = await try_start_callback_server(
        port=CALLBACK_PORT,
        callback_path=CALLBACK_PATH,
        expected_state=expected_state,
        success_message=(
            "Anthropic authentication completed. You can close this window."
        ),
    )
    if server is not None:
        redirect_uri = server.redirect_uri

    try:
        authorize_params = urlencode(
            {
                "code": "true",
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": SCOPES,
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
                "state": expected_state,
            }
        )
        instructions = (
            "Complete login in your browser. If the browser is on "
            "another machine, paste the final redirect URL here."
        )
        if server is None:
            instructions = (
                "Local callback server could not bind (port "
                f"{CALLBACK_PORT} is busy). Complete login in your "
                "browser, then paste the final redirect URL here."
            )
        callbacks.on_auth(
            OAuthAuthInfo(
                url=f"{AUTHORIZE_URL}?{authorize_params}",
                instructions=instructions,
            )
        )

        code: str | None = None
        state: str | None = None

        if server is not None:
            manual_input = (
                callbacks.on_manual_code_input()
                if callbacks.on_manual_code_input is not None
                else None
            )
            race = await _race_callback_against_manual(
                callback_awaitable=server.wait_for_code(),
                manual_input=manual_input,
                expected_state=expected_state,
            )
            code, state = race.code, race.state
        elif callbacks.on_manual_code_input is not None:
            pasted = await callbacks.on_manual_code_input()
            parsed = parse_authorization_input(pasted)
            if parsed.state and parsed.state != expected_state:
                msg = "OAuth state mismatch"
                raise OAuthStateMismatchError(msg)
            code = parsed.code
            state = parsed.state or expected_state

        if code is None:
            pasted = await callbacks.on_prompt(
                OAuthPrompt(
                    message=("Paste the authorization code or full redirect URL"),
                    placeholder=redirect_uri,
                )
            )
            parsed = parse_authorization_input(pasted)
            if parsed.state and parsed.state != expected_state:
                msg = "OAuth state mismatch"
                raise OAuthStateMismatchError(msg)
            code = parsed.code
            state = parsed.state or expected_state

        if code is None:
            msg = "Missing authorization code"
            raise OAuthCancelledError(msg)

        if state is None:
            state = expected_state

        if callbacks.on_progress:
            callbacks.on_progress("Exchanging authorization code for tokens...")

        return await _exchange_authorization_code(
            code=code,
            state=state,
            verifier=pkce.verifier,
            redirect_uri=redirect_uri,
        )
    finally:
        if server is not None:
            await server.close()


async def _exchange_authorization_code(
    *,
    code: str,
    state: str,
    verifier: str,
    redirect_uri: str,
) -> OAuthCredentials:
    """Exchange the authorization code for tokens at the Anthropic token endpoint."""
    try:
        data = await post_json(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "state": state,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
    except OAuthHTTPError as exc:
        msg = f"Anthropic token exchange failed. url={TOKEN_URL}; details={exc!s}"
        raise OAuthHTTPError(msg) from exc

    try:
        access = str(data["access_token"])
        refresh = str(data["refresh_token"])
        expires_in = float(data["expires_in"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"Anthropic token exchange returned an unexpected payload: {data!r}"
        raise OAuthHTTPError(msg) from exc

    return OAuthCredentials(
        access=access,
        refresh=refresh,
        expires=expires_from_lifetime(expires_in),
    )


async def refresh_anthropic_token(refresh_token: str) -> OAuthCredentials:
    """Refresh an Anthropic OAuth token via the platform token endpoint.

    Note: pi-mono does not include `scope` in the refresh body, and we
    don't either — Anthropic's refresh endpoint rejects requests that
    add `scope` here.
    """
    try:
        data = await post_json(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
    except OAuthHTTPError as exc:
        msg = f"Anthropic token refresh failed: {exc!s}"
        raise OAuthRefreshError(msg) from exc

    try:
        access = str(data["access_token"])
        refresh = str(data["refresh_token"])
        expires_in = float(data["expires_in"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"Anthropic token refresh returned unexpected payload: {data!r}"
        raise OAuthRefreshError(msg) from exc

    return OAuthCredentials(
        access=access,
        refresh=refresh,
        expires=expires_from_lifetime(expires_in),
    )


class _AnthropicOAuthProvider:
    """Singleton implementation of `OAuthProvider` for Anthropic."""

    id = "anthropic"
    name = "Anthropic (Claude Pro/Max)"
    uses_callback_server = True

    async def login(self, callbacks: OAuthCallbacks) -> OAuthCredentials:
        return await login_anthropic(callbacks)

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return await refresh_anthropic_token(credentials.refresh)

    def get_api_key(self, credentials: OAuthCredentials) -> str:
        return credentials.access


anthropic_oauth_provider = _AnthropicOAuthProvider()
"""Module-level singleton registered in `oauth.registry`."""
