"""Shared types for OAuth subscription auth.

Ported from `pi-mono/packages/ai/src/utils/oauth/types.ts`.

`OAuthCredentials` is the tuple of secrets and metadata persisted to
`~/.deepagents/.state/oauth-tokens/<provider>.json`. `expires` is **POSIX
seconds** (float), unlike pi-mono's millisecond epoch — keep this in mind
when comparing snapshots between the two codebases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True, slots=True)
class OAuthCredentials:
    """Persisted OAuth credentials for a single provider.

    Attributes:
        access: Provider-issued access token. For Anthropic this is the
            `sk-ant-oat-...` token; for GitHub Copilot the
            `tid=...;exp=...;proxy-ep=...` token; for OpenAI Codex the
            JWT.
        refresh: Long-lived refresh token. For GitHub Copilot this is the
            user's GitHub access token (Copilot tokens themselves are
            short-lived and minted from the user token on every refresh).
        expires: Absolute expiry time in POSIX seconds. Includes a
            5-minute safety margin so callers never spend the last
            5 minutes of a token's life on a request that might fail
            mid-stream.
        extras: Provider-specific metadata that must round-trip with the
            credential — e.g. `{"enterpriseUrl": "company.ghe.com"}` for
            GitHub Copilot Enterprise, or `{"accountId": "..."}` for
            ChatGPT Codex.
    """

    access: str
    refresh: str
    expires: float
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OAuthAuthInfo:
    """Information surfaced to the user when a login flow starts.

    Attributes:
        url: The browser URL the user should open.
        instructions: Optional human-readable hint (e.g. a device-flow
            user code, or a paste-back instruction for headless logins).
    """

    url: str
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthPrompt:
    """A single prompt the OAuth flow needs from the user."""

    message: str
    placeholder: str | None = None
    allow_empty: bool = False


@dataclass(frozen=True, slots=True)
class OAuthCallbacks:
    """Bundle of callbacks injected into an OAuth login flow.

    Lets the same provider implementation drive both the headless CLI
    (`deepagents login ...`) and the in-TUI `/login` slash command.

    Attributes:
        on_auth: Called once with the authorization URL (and any
            user-visible instructions) at the start of the flow.
        on_prompt: Called when the flow needs a free-text input from the
            user, e.g. a pasted callback URL or a GitHub Enterprise
            domain. Must be `async`.
        on_progress: Optional progress messages (e.g. "Enabling
            models...").
        on_manual_code_input: Optional async callable that returns a
            user-pasted code/URL. When set, the flow races the local
            callback server against this future so a user can paste a
            redirect URL captured on a different machine.
    """

    on_auth: Callable[[OAuthAuthInfo], None]
    on_prompt: Callable[[OAuthPrompt], Awaitable[str]]
    on_progress: Callable[[str], None] | None = None
    on_manual_code_input: Callable[[], Awaitable[str]] | None = None


class OAuthError(RuntimeError):
    """Base class for OAuth-flow errors raised by this package."""


class OAuthCancelledError(OAuthError):
    """Raised when the user cancels a login flow."""


class OAuthStateMismatchError(OAuthError):
    """Raised when an authorization callback's `state` does not match.

    Indicates either a stale browser tab or a CSRF attempt against the
    local callback server.
    """


class OAuthRefreshError(OAuthError):
    """Raised when refreshing a token fails.

    Callers should treat this as a signal to re-run `deepagents login`
    rather than silently dropping credentials, since a network blip can
    look the same as a revoked refresh token at this layer.
    """


@runtime_checkable
class OAuthProvider(Protocol):
    """Protocol implemented by every built-in or user-registered provider.

    Mirrors `OAuthProviderInterface` in `pi-mono/packages/ai/src/utils/
    oauth/types.ts`. Providers are stateless — credentials are passed in
    explicitly to `refresh_token` and `get_api_key` so the same provider
    object can serve multiple sessions.
    """

    @property
    def id(self) -> str:
        """Stable identifier used as the storage key (e.g. `"anthropic"`)."""

    @property
    def name(self) -> str:
        """Human-readable label shown in provider pickers."""

    @property
    def uses_callback_server(self) -> bool:
        """Whether the login flow opens a local HTTP callback server.

        Used by the UI to decide whether to show a manual paste-back
        affordance alongside the browser open.
        """

    async def login(self, callbacks: OAuthCallbacks) -> OAuthCredentials:
        """Run the full login flow and return fresh credentials."""

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        """Mint a new short-lived access token from a refresh token."""

    def get_api_key(self, credentials: OAuthCredentials) -> str:
        """Return the access-token string used as a provider API key.

        Almost always `credentials.access` — the indirection exists so a
        future provider can synthesise the API key from the credentials
        record without leaking the access field directly.
        """
