"""Public OAuth API implementation.

`oauth/__init__.py` re-exports the names defined here. Keeping the
implementations in a sibling module satisfies `RUF067` (no logic in
`__init__.py`) without forcing every caller to learn a private import
path.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from deepagents_cli.oauth.registry import get_provider
from deepagents_cli.oauth.storage import (
    alock_provider,
    delete_credentials as _delete_credentials_file,
    load_credentials,
    save_credentials,
    save_credentials_locked,
)

if TYPE_CHECKING:
    from deepagents_cli.oauth.types import OAuthCallbacks, OAuthCredentials


async def login(
    provider_id: str,
    callbacks: OAuthCallbacks,
) -> OAuthCredentials:
    """Run *provider_id*'s login flow, persist credentials, and return them.

    Returns:
        The freshly-issued credentials (also saved to disk).

    Raises:
        ValueError: If *provider_id* is not registered.
    """
    provider = get_provider(provider_id)
    if provider is None:
        msg = f"Unknown OAuth provider: {provider_id}"
        raise ValueError(msg)
    credentials = await provider.login(callbacks)
    save_credentials(provider_id, credentials)
    return credentials


async def refresh_credentials(
    provider_id: str,
    credentials: OAuthCredentials,
) -> OAuthCredentials:
    """Refresh *credentials* for *provider_id* and persist the result.

    This is the **explicit refresh hook**: callers that hold a
    credentials value (e.g. a long-running model client) can force a
    refresh ahead of time without relying on the auto-refresh path in
    `get_access_token`.

    The whole load → refresh → save sequence runs under
    `alock_provider`. We re-read the on-disk credentials inside the
    lock so a concurrent refresh that already rotated the refresh
    token wins instead of being clobbered — Anthropic and OpenAI Codex
    both rotate `refresh_token` on every refresh, so a losing race
    invalidates the loser's stored token.

    Returns:
        The refreshed credentials (also saved to disk).

    Raises:
        ValueError: If *provider_id* is not registered.
        OAuthRefreshError: If the provider's refresh endpoint rejects
            the refresh token.
    """
    provider = get_provider(provider_id)
    if provider is None:
        msg = f"Unknown OAuth provider: {provider_id}"
        raise ValueError(msg)
    async with alock_provider(provider_id):
        # Use the freshest on-disk refresh token if another process
        # already rotated it while we waited for the lock; the caller's
        # `credentials` may have been minted from a now-invalidated
        # refresh token.
        latest = load_credentials(provider_id) or credentials
        refreshed = await provider.refresh_token(latest)
        save_credentials_locked(provider_id, refreshed)
    return refreshed


def get_stored_credentials(provider_id: str) -> OAuthCredentials | None:
    """Return persisted credentials for *provider_id*, or `None`."""
    return load_credentials(provider_id)


def delete_credentials(provider_id: str) -> bool:
    """Delete persisted credentials for *provider_id*.

    Returns:
        `True` if a file was removed, `False` if none existed.
    """
    return _delete_credentials_file(provider_id)


async def get_access_token(
    provider_id: str,
    *,
    refresh_if_expired: bool = True,
) -> str | None:
    """Return the current access token for *provider_id*.

    Implements double-checked locking: we read the credential outside
    the lock for the common (still-valid) case, and only acquire the
    cross-process lock when a refresh is actually needed. Inside the
    lock we re-read so we skip the network call if a peer process
    refreshed first.

    Args:
        provider_id: Provider key (e.g. `"anthropic"`).
        refresh_if_expired: When `True` (default), expired credentials
            are refreshed and the new token is returned. When `False`,
            an expired credential is returned as-is — the caller is
            responsible for handling the resulting 401.

    Returns:
        The access-token string, or `None` if there are no stored
        credentials.

    Raises:
        OAuthRefreshError: If a refresh was attempted and failed.
    """
    credentials = load_credentials(provider_id)
    if credentials is None:
        return None
    provider = get_provider(provider_id)
    if provider is None:
        # Stored credentials for an unknown provider id — return the raw
        # access token so callers can still try; we don't know the
        # refresh URL anyway.
        return credentials.access
    if not refresh_if_expired or time.time() < credentials.expires:
        return provider.get_api_key(credentials)

    async with alock_provider(provider_id):
        # Re-check after acquiring the lock: another process may have
        # refreshed during our wait. If so, use their token rather than
        # spending another rotated-refresh-token round-trip.
        latest = load_credentials(provider_id) or credentials
        if time.time() < latest.expires:
            return provider.get_api_key(latest)
        refreshed = await provider.refresh_token(latest)
        save_credentials_locked(provider_id, refreshed)
    return provider.get_api_key(refreshed)
