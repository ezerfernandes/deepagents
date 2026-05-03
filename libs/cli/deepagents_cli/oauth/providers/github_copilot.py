"""GitHub Copilot OAuth provider (RFC 8628 device flow).

Port of `pi-mono/packages/ai/src/utils/oauth/github-copilot.ts`. The
flow is:

1. POST `/login/device/code` to get a `device_code` + `user_code`.
2. Show the user the verification URL and ask them to enter the user
   code in the browser.
3. Poll `/login/oauth/access_token` until the user approves (or denies,
   or the code expires).
4. Exchange the resulting GitHub user token for a short-lived Copilot
   token at `https://api.<domain>/copilot_internal/v2/token`.
5. Store the user token as `refresh` and the Copilot token as `access` —
   refreshes mint a new Copilot token from the user token.

The Copilot endpoint expects a very specific set of `User-Agent`,
`Editor-Version`, and `Copilot-Integration-Id` headers. Sending different
values triggers either 403s or rate-limit responses, so we mirror
pi-mono's constants exactly. They are also re-exported for the chat
integration shim (`_oauth_middleware.py`) so request-time headers stay
in sync with login-time headers.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from deepagents_cli.oauth.providers._common import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    OAuthHTTPError,
)
from deepagents_cli.oauth.types import (
    OAuthAuthInfo,
    OAuthCallbacks,
    OAuthCancelledError,
    OAuthCredentials,
    OAuthError,
    OAuthPrompt,
    OAuthRefreshError,
)

logger = logging.getLogger(__name__)

CLIENT_ID = base64.b64decode("SXYxLmI1MDdhMDhjODdlY2ZlOTg=").decode("ascii")
"""GitHub OAuth app id used by the official Copilot Chat extension. We
re-use it the same way pi-mono does — see notes in
`pi-mono/packages/ai/src/utils/oauth/github-copilot.ts:13`."""

COPILOT_HEADERS: dict[str, str] = {
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}
"""Headers that must accompany every Copilot device/token request and
every chat request through the Copilot proxy. Bumped together with
pi-mono."""

INITIAL_POLL_INTERVAL_MULTIPLIER = 1.2
SLOW_DOWN_POLL_INTERVAL_MULTIPLIER = 1.4

ENTERPRISE_KEY = "enterpriseUrl"
"""Key under `OAuthCredentials.extras` that stores the enterprise domain
when the user logged in to GHE rather than github.com."""


_PROXY_EP_RE = re.compile(r"proxy-ep=([^;]+)")


def normalize_domain(value: str) -> str | None:
    """Return the hostname portion of a domain or URL, or `None` if invalid.

    Mirrors `normalizeDomain` in `pi-mono/packages/ai/src/utils/oauth/
    github-copilot.ts:46-55`. Accepts `company.ghe.com`,
    `https://company.ghe.com/...`, and similar.
    """
    stripped = value.strip()
    if not stripped:
        return None
    if "://" not in stripped:
        stripped = f"https://{stripped}"
    parsed = urlsplit(stripped)
    return parsed.hostname or None


def get_github_copilot_base_url(
    token: str | None = None,
    enterprise_domain: str | None = None,
) -> str:
    """Return the base URL for chat requests through the Copilot proxy.

    Token shape is `tid=...;exp=...;proxy-ep=proxy.X.githubcopilot.com;...`.
    The `proxy-ep` host gets rewritten to `api.X.githubcopilot.com` —
    this is the contract the Anthropic-shaped Copilot endpoint expects.
    """
    if token:
        match = _PROXY_EP_RE.search(token)
        if match:
            proxy_host = match.group(1)
            api_host = re.sub(r"^proxy\.", "api.", proxy_host)
            return f"https://{api_host}"
    if enterprise_domain:
        return f"https://copilot-api.{enterprise_domain}"
    return "https://api.individual.githubcopilot.com"


def _api_base_url(domain: str) -> str:
    """Return the GitHub REST API root for *domain*.

    `github.com` puts its REST API on a dedicated host
    (`api.github.com`), but GitHub Enterprise Server installs serve
    REST under `<host>/api/v3/` — using `api.<host>` resolves to a
    different machine (or DNS NXDOMAIN) and the Copilot token refresh
    silently 404s/timeouts.

    pi-mono inherits the `api.${domain}` shape and breaks on every
    GHE Server deployment; we override here.
    """
    if domain == "github.com":
        return "https://api.github.com"
    return f"https://{domain}/api/v3"


def _device_urls(domain: str) -> dict[str, str]:
    api_base = _api_base_url(domain)
    return {
        "device_code": f"https://{domain}/login/device/code",
        "access_token": f"https://{domain}/login/oauth/access_token",
        "copilot_token": f"{api_base}/copilot_internal/v2/token",
    }


async def _fetch_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: dict[str, str] | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Issue a request and return JSON.

    Distinct from `_common.post_form` because GitHub returns errors as
    200 OK with an `error` field rather than non-2xx, so we need access
    to the raw payload either way.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.request(method, url, headers=headers, data=data)
        except httpx.HTTPError as exc:
            msg = f"HTTP request failed. url={url}; details={exc!s}"
            raise OAuthHTTPError(msg) from exc

    if response.status_code >= 400:
        snippet = response.text[:500] if response.text else "<empty>"
        msg = (
            f"HTTP request failed. status={response.status_code}; "
            f"url={url}; body={snippet}"
        )
        raise OAuthHTTPError(msg)
    if not response.text:
        return {}
    try:
        decoded = response.json()
    except ValueError as exc:
        snippet = response.text[:500]
        msg = f"Response was not valid JSON. url={url}; body={snippet}"
        raise OAuthHTTPError(msg) from exc
    if not isinstance(decoded, dict):
        msg = f"Response JSON was not an object. url={url}; body={response.text!r}"
        raise OAuthHTTPError(msg)
    return decoded


async def _start_device_flow(domain: str) -> dict[str, Any]:
    """POST `/login/device/code` and return the parsed response."""
    urls = _device_urls(domain)
    payload = await _fetch_json(
        "POST",
        urls["device_code"],
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": COPILOT_HEADERS["User-Agent"],
        },
        data={"client_id": CLIENT_ID, "scope": "read:user"},
    )
    required = (
        "device_code",
        "user_code",
        "verification_uri",
        "interval",
        "expires_in",
    )
    missing = [k for k in required if k not in payload]
    if missing:
        msg = f"Device-code response missing fields {missing}: {payload!r}"
        raise OAuthHTTPError(msg)
    return payload


async def _abortable_sleep(seconds: float) -> None:
    """Sleep that surfaces cancellation as `OAuthCancelledError`."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError as exc:
        msg = "Login cancelled"
        raise OAuthCancelledError(msg) from exc


async def _poll_for_github_access_token(
    *,
    domain: str,
    device_code: str,
    interval_seconds: float,
    expires_in_seconds: float,
) -> str:
    """Poll the GitHub access-token endpoint until approval or failure."""
    urls = _device_urls(domain)
    deadline = time.time() + expires_in_seconds
    interval_ms = max(1.0, float(interval_seconds))
    multiplier = INITIAL_POLL_INTERVAL_MULTIPLIER
    slow_down_responses = 0

    while time.time() < deadline:
        wait_seconds = min(interval_ms * multiplier, max(0.0, deadline - time.time()))
        await _abortable_sleep(wait_seconds)

        try:
            payload = await _fetch_json(
                "POST",
                urls["access_token"],
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": COPILOT_HEADERS["User-Agent"],
                },
                data={
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        except OAuthHTTPError as exc:
            # Transient network errors shouldn't kill the flow — log and retry.
            logger.debug("Device-flow polling hit a transient error; retrying: %s", exc)
            continue

        access = payload.get("access_token")
        if isinstance(access, str) and access:
            return access

        error = payload.get("error")
        if isinstance(error, str):
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                slow_down_responses += 1
                provider_interval = payload.get("interval")
                if (
                    isinstance(provider_interval, (int, float))
                    and provider_interval > 0
                ):
                    interval_ms = float(provider_interval)
                else:
                    interval_ms = max(1.0, interval_ms + 5.0)
                multiplier = SLOW_DOWN_POLL_INTERVAL_MULTIPLIER
                continue
            description = payload.get("error_description")
            suffix = f": {description}" if description else ""
            msg = f"Device flow failed: {error}{suffix}"
            raise OAuthError(msg)

    if slow_down_responses > 0:
        msg = (
            "Device flow timed out after one or more slow_down responses. "
            "This is often caused by clock drift in WSL or VM environments. "
            "Sync or restart the VM clock and try again."
        )
        raise OAuthError(msg)
    msg = "Device flow timed out"
    raise OAuthError(msg)


async def _exchange_github_token_for_copilot(
    github_user_token: str,
    enterprise_domain: str | None,
) -> OAuthCredentials:
    """Mint a short-lived Copilot token from a long-lived GitHub user token."""
    domain = enterprise_domain or "github.com"
    urls = _device_urls(domain)

    payload = await _fetch_json(
        "GET",
        urls["copilot_token"],
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {github_user_token}",
            **COPILOT_HEADERS,
        },
    )

    token = payload.get("token")
    expires_at = payload.get("expires_at")
    if not isinstance(token, str) or not isinstance(expires_at, (int, float)):
        msg = f"Copilot token response missing fields: {payload!r}"
        raise OAuthHTTPError(msg)

    extras: dict[str, Any] = {}
    if enterprise_domain:
        extras[ENTERPRISE_KEY] = enterprise_domain

    # `expires_at` is already an absolute POSIX timestamp from GitHub —
    # subtract the 5-minute safety margin instead of using
    # `expires_from_lifetime` (which assumes a relative lifetime).
    return OAuthCredentials(
        access=token,
        refresh=github_user_token,
        expires=float(expires_at) - 5 * 60,
        extras=extras,
    )


# Static, hand-curated list of Copilot-served models that may need
# explicit policy acceptance before chat works. Mirrors pi-mono's filter
# `getModels("github-copilot")` — keeping it inline rather than wiring
# in a full models registry keeps the OAuth package self-contained. Add
# new model IDs here as they appear in pi-mono's `models.generated.ts`.
GITHUB_COPILOT_DEFAULT_MODEL_IDS = (
    "claude-3.5-sonnet",
    "claude-3.7-sonnet",
    "claude-3.7-sonnet-thought",
    "claude-sonnet-4",
    "claude-sonnet-4-5",
    "claude-opus-4",
    "claude-opus-4-1",
    "gemini-2.0-flash-001",
    "gemini-2.5-pro",
    "gpt-4.1",
    "gpt-4o",
    "gpt-5",
    "gpt-5-mini",
    "grok-code-fast-1",
    "o3",
    "o3-mini",
    "o4-mini",
)


async def _enable_one_model(
    token: str,
    model_id: str,
    enterprise_domain: str | None,
) -> bool:
    """POST `/models/<id>/policy` to opt the user into a Copilot model."""
    base_url = get_github_copilot_base_url(token, enterprise_domain)
    url = f"{base_url}/models/{model_id}/policy"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    **COPILOT_HEADERS,
                    "openai-intent": "chat-policy",
                    "x-interaction-type": "chat-policy",
                },
                content='{"state": "enabled"}',
            )
            return response.is_success
    except httpx.HTTPError:
        return False


async def enable_all_github_copilot_models(
    token: str,
    enterprise_domain: str | None = None,
    model_ids: tuple[str, ...] = GITHUB_COPILOT_DEFAULT_MODEL_IDS,
) -> None:
    """Enable every known Copilot model in parallel.

    Failures are silent — if a model is already enabled the API returns
    400, and we don't want one stale entry to prevent the rest of the
    list from succeeding. Mirrors pi-mono's "fire and forget" strategy.
    """
    await asyncio.gather(
        *(_enable_one_model(token, mid, enterprise_domain) for mid in model_ids),
        return_exceptions=True,
    )


async def login_github_copilot(callbacks: OAuthCallbacks) -> OAuthCredentials:
    """Run the GitHub Copilot device-code login flow."""
    enterprise_input = await callbacks.on_prompt(
        OAuthPrompt(
            message="GitHub Enterprise URL/domain (blank for github.com)",
            placeholder="company.ghe.com",
            allow_empty=True,
        )
    )

    trimmed = enterprise_input.strip()
    enterprise_domain: str | None = None
    if trimmed:
        enterprise_domain = normalize_domain(trimmed)
        if enterprise_domain is None:
            msg = "Invalid GitHub Enterprise URL/domain"
            raise OAuthError(msg)

    domain = enterprise_domain or "github.com"

    device = await _start_device_flow(domain)

    callbacks.on_auth(
        OAuthAuthInfo(
            url=str(device["verification_uri"]),
            instructions=f"Enter code: {device['user_code']}",
        )
    )

    github_user_token = await _poll_for_github_access_token(
        domain=domain,
        device_code=str(device["device_code"]),
        interval_seconds=float(device["interval"]),
        expires_in_seconds=float(device["expires_in"]),
    )

    if callbacks.on_progress:
        callbacks.on_progress("Exchanging GitHub token for Copilot token...")

    credentials = await _exchange_github_token_for_copilot(
        github_user_token, enterprise_domain
    )

    if callbacks.on_progress:
        callbacks.on_progress("Enabling Copilot models...")
    await enable_all_github_copilot_models(credentials.access, enterprise_domain)

    return credentials


async def refresh_github_copilot_token(
    credentials: OAuthCredentials,
) -> OAuthCredentials:
    """Mint a fresh Copilot token from the stored GitHub user token."""
    enterprise_domain = credentials.extras.get(ENTERPRISE_KEY)
    try:
        return await _exchange_github_token_for_copilot(
            credentials.refresh, enterprise_domain
        )
    except OAuthHTTPError as exc:
        msg = f"GitHub Copilot token refresh failed: {exc!s}"
        raise OAuthRefreshError(msg) from exc


class _GitHubCopilotOAuthProvider:
    """Singleton implementation of `OAuthProvider` for GitHub Copilot."""

    id = "github-copilot"
    name = "GitHub Copilot"
    uses_callback_server = False

    async def login(self, callbacks: OAuthCallbacks) -> OAuthCredentials:
        return await login_github_copilot(callbacks)

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return await refresh_github_copilot_token(credentials)

    def get_api_key(self, credentials: OAuthCredentials) -> str:
        return credentials.access


github_copilot_oauth_provider = _GitHubCopilotOAuthProvider()
"""Module-level singleton registered in `oauth.registry`."""
