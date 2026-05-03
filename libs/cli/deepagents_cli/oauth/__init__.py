"""OAuth subscription auth for AI providers.

Public surface:

- `login(provider_id, callbacks)` — run the login flow and persist
  credentials.
- `refresh_credentials(provider_id, credentials)` — explicit refresh
  hook (caller passes the credentials it wants refreshed).
- `get_access_token(provider_id, *, refresh_if_expired=True)` —
  convenience accessor that auto-refreshes when expired.
- `get_stored_credentials(provider_id)` / `delete_credentials(...)` /
  `list_logged_in_providers()` — storage accessors.
- `list_providers()` / `get_provider(...)` / `register_provider(...)` —
  provider registry.
"""

from deepagents_cli.oauth._api import (
    delete_credentials,
    get_access_token,
    get_stored_credentials,
    login,
    refresh_credentials,
)
from deepagents_cli.oauth.registry import (
    get_provider,
    list_providers,
    register_provider,
    unregister_provider,
)
from deepagents_cli.oauth.storage import list_logged_in_providers
from deepagents_cli.oauth.types import (
    OAuthAuthInfo,
    OAuthCallbacks,
    OAuthCancelledError,
    OAuthCredentials,
    OAuthError,
    OAuthPrompt,
    OAuthProvider,
    OAuthRefreshError,
    OAuthStateMismatchError,
)

__all__ = [
    "OAuthAuthInfo",
    "OAuthCallbacks",
    "OAuthCancelledError",
    "OAuthCredentials",
    "OAuthError",
    "OAuthPrompt",
    "OAuthProvider",
    "OAuthRefreshError",
    "OAuthStateMismatchError",
    "delete_credentials",
    "get_access_token",
    "get_provider",
    "get_stored_credentials",
    "list_logged_in_providers",
    "list_providers",
    "login",
    "refresh_credentials",
    "register_provider",
    "unregister_provider",
]
