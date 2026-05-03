"""Registry of OAuth providers.

Built-in providers (`anthropic`, `github-copilot`, `openai-codex`) are
loaded lazily on first access so importing the OAuth package is cheap
even if no OAuth flow ever runs. Custom providers can be registered at
runtime via `register_provider`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deepagents_cli.oauth.types import OAuthProvider


_lock = threading.Lock()
_providers: dict[str, OAuthProvider] | None = None


def _load_builtins() -> dict[str, OAuthProvider]:
    """Import and return the built-in provider registry.

    Done lazily because each provider module imports `httpx` and pulls
    in its provider-specific constants — keeping the import out of the
    package init keeps `deepagents_cli` startup fast.
    """
    from deepagents_cli.oauth.providers.anthropic import (
        anthropic_oauth_provider,
    )
    from deepagents_cli.oauth.providers.github_copilot import (
        github_copilot_oauth_provider,
    )
    from deepagents_cli.oauth.providers.openai_codex import (
        openai_codex_oauth_provider,
    )

    return {
        anthropic_oauth_provider.id: anthropic_oauth_provider,
        github_copilot_oauth_provider.id: github_copilot_oauth_provider,
        openai_codex_oauth_provider.id: openai_codex_oauth_provider,
    }


def _ensure_loaded() -> dict[str, OAuthProvider]:
    global _providers  # noqa: PLW0603 - intentional module cache
    with _lock:
        if _providers is None:
            _providers = _load_builtins()
        return _providers


def list_providers() -> list[OAuthProvider]:
    """Return all registered providers, in registration order."""
    return list(_ensure_loaded().values())


def get_provider(provider_id: str) -> OAuthProvider | None:
    """Return the provider registered under *provider_id*, or `None`."""
    return _ensure_loaded().get(provider_id)


def register_provider(provider: OAuthProvider) -> None:
    """Register a custom OAuth provider.

    If a provider with the same id is already registered (built-in or
    custom), the new value replaces it. Use `unregister_provider` to
    restore the built-in.
    """
    registry = _ensure_loaded()
    registry[provider.id] = provider


def unregister_provider(provider_id: str) -> None:
    """Remove a registered provider.

    Built-in providers are restored from a freshly-loaded copy; custom
    providers are removed entirely.
    """
    registry = _ensure_loaded()
    builtins = _load_builtins()
    if provider_id in builtins:
        registry[provider_id] = builtins[provider_id]
        return
    registry.pop(provider_id, None)


def reset_providers() -> None:
    """Reset the registry to the built-in set (test helper)."""
    global _providers  # noqa: PLW0603 - test reset
    with _lock:
        _providers = None
