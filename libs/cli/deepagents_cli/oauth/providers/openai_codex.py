"""OpenAI Codex (ChatGPT Plus/Pro subscription) OAuth provider.

Port of `pi-mono/packages/ai/src/utils/oauth/openai-codex.ts`. Uses
PKCE + a local callback server on `(host, 1455)/auth/callback`. The
returned access token is a JWT — we decode (without signature
verification, since it just came over TLS from `auth.openai.com`) to
extract `chatgpt_account_id` and store it under `extras.account_id`.
The Codex Responses endpoint requires that account id on every
subsequent request.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self
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
    post_form,
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

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
SCOPE = "openid profile email offline_access"
JWT_CLAIM_PATH = "https://api.openai.com/auth"

ACCOUNT_ID_KEY = "accountId"
"""Key under `OAuthCredentials.extras` for the ChatGPT account id."""

ORIGINATOR = "deepagents"
"""Identifier sent as the `originator` query param at authorize time and
as the `originator` request header on every Codex chat request. We use
`deepagents` (not pi-mono's `pi`) because this is a different CLI."""


@dataclass(frozen=True, slots=True)
class _RaceResult:
    code: str | None


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without verifying the signature.

    OAuth tokens we just minted over TLS from `auth.openai.com` are
    trustworthy enough for the limited use we have (extracting the
    account id) — full signature verification would require the OpenAI
    JWKS endpoint and an additional dependency for no security gain.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        decoded_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        decoded = json.loads(decoded_bytes)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _extract_account_id(access_token: str) -> str | None:
    """Pull `chatgpt_account_id` out of the access-token JWT payload."""
    payload = _decode_jwt_payload(access_token)
    if payload is None:
        return None
    auth = payload.get(JWT_CLAIM_PATH)
    if not isinstance(auth, dict):
        return None
    account_id = auth.get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id:
        return account_id
    return None


async def _race_callback_against_manual(
    *,
    callback_awaitable: Awaitable[CallbackResult | None],
    manual_input: Awaitable[str] | None,
    expected_state: str,
) -> _RaceResult:
    """Race the local callback server against a manual paste-back."""
    callback_task = asyncio.ensure_future(callback_awaitable)
    if manual_input is None:
        result = await callback_task
        return _RaceResult(code=result.code if result else None)

    manual_task = asyncio.ensure_future(manual_input)
    done, pending = await asyncio.wait(
        {callback_task, manual_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in pending:
        with _suppress_cancel():
            await task

    if callback_task in done:
        result = callback_task.result()
        if result is not None:
            return _RaceResult(code=result.code)

    if manual_task in done:
        try:
            pasted = manual_task.result()
        except asyncio.CancelledError:
            return _RaceResult(code=None)
        parsed = parse_authorization_input(pasted)
        if parsed.state and parsed.state != expected_state:
            msg = "OAuth state mismatch"
            raise OAuthStateMismatchError(msg)
        return _RaceResult(code=parsed.code)

    return _RaceResult(code=None)


class _suppress_cancel:  # noqa: N801 - context-manager naming
    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return exc_type is asyncio.CancelledError


async def login_openai_codex(callbacks: OAuthCallbacks) -> OAuthCredentials:
    """Run the OpenAI Codex authorization-code + PKCE flow."""
    pkce = generate_pkce()
    state = secrets.token_hex(16)

    # OpenAI registers `http://localhost:1455/auth/callback` as the only
    # acceptable redirect URI. We surface it even when the local listener
    # can't bind so manual paste-back still works.
    redirect_uri = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

    server = await try_start_callback_server(
        port=CALLBACK_PORT,
        callback_path=CALLBACK_PATH,
        expected_state=state,
        success_message=("OpenAI authentication completed. You can close this window."),
    )
    if server is not None:
        redirect_uri = server.redirect_uri

    try:
        authorize_params = urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": redirect_uri,
                "scope": SCOPE,
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
                "state": state,
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "originator": ORIGINATOR,
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

        if server is not None:
            manual_input = (
                callbacks.on_manual_code_input()
                if callbacks.on_manual_code_input is not None
                else None
            )
            race = await _race_callback_against_manual(
                callback_awaitable=server.wait_for_code(),
                manual_input=manual_input,
                expected_state=state,
            )
            code = race.code
        elif callbacks.on_manual_code_input is not None:
            pasted = await callbacks.on_manual_code_input()
            parsed = parse_authorization_input(pasted)
            if parsed.state and parsed.state != state:
                msg = "OAuth state mismatch"
                raise OAuthStateMismatchError(msg)
            code = parsed.code

        if code is None:
            pasted = await callbacks.on_prompt(
                OAuthPrompt(
                    message="Paste the authorization code or full redirect URL",
                    placeholder=redirect_uri,
                )
            )
            parsed = parse_authorization_input(pasted)
            if parsed.state and parsed.state != state:
                msg = "OAuth state mismatch"
                raise OAuthStateMismatchError(msg)
            code = parsed.code

        if code is None:
            msg = "Missing authorization code"
            raise OAuthCancelledError(msg)

        if callbacks.on_progress:
            callbacks.on_progress("Exchanging authorization code for tokens...")

        return await _exchange_authorization_code(
            code=code,
            verifier=pkce.verifier,
            redirect_uri=redirect_uri,
        )
    finally:
        if server is not None:
            await server.close()


async def _exchange_authorization_code(
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> OAuthCredentials:
    """Trade the authorization code for tokens at the OpenAI token endpoint."""
    try:
        data = await post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )
    except OAuthHTTPError as exc:
        msg = f"OpenAI Codex token exchange failed: {exc!s}"
        raise OAuthHTTPError(msg) from exc

    return _build_credentials(data)


async def refresh_openai_codex_token(refresh_token: str) -> OAuthCredentials:
    """Refresh a stored OpenAI Codex token, re-extracting the account id."""
    try:
        data = await post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
        )
    except OAuthHTTPError as exc:
        msg = f"OpenAI Codex token refresh failed: {exc!s}"
        raise OAuthRefreshError(msg) from exc

    try:
        return _build_credentials(data)
    except OAuthHTTPError as exc:
        raise OAuthRefreshError(str(exc)) from exc


def _build_credentials(data: dict[str, Any]) -> OAuthCredentials:
    try:
        access = str(data["access_token"])
        refresh = str(data["refresh_token"])
        expires_in = float(data["expires_in"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"OpenAI Codex token response missing fields: {data!r}"
        raise OAuthHTTPError(msg) from exc

    account_id = _extract_account_id(access)
    if account_id is None:
        msg = "OpenAI Codex token did not include a chatgpt_account_id claim"
        raise OAuthHTTPError(msg)

    return OAuthCredentials(
        access=access,
        refresh=refresh,
        expires=expires_from_lifetime(expires_in),
        extras={ACCOUNT_ID_KEY: account_id},
    )


class _OpenAICodexOAuthProvider:
    """Singleton implementation of `OAuthProvider` for ChatGPT Plus/Pro Codex."""

    id = "openai-codex"
    name = "ChatGPT Plus/Pro (Codex Subscription)"
    uses_callback_server = True

    async def login(self, callbacks: OAuthCallbacks) -> OAuthCredentials:
        return await login_openai_codex(callbacks)

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return await refresh_openai_codex_token(credentials.refresh)

    def get_api_key(self, credentials: OAuthCredentials) -> str:
        return credentials.access


openai_codex_oauth_provider = _OpenAICodexOAuthProvider()
"""Module-level singleton registered in `oauth.registry`."""
