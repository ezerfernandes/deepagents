"""Tests for the public OAuth API surface (`deepagents_cli.oauth`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from deepagents_cli.oauth import (
    OAuthCallbacks,
    OAuthCredentials,
    delete_credentials,
    get_access_token,
    get_provider,
    get_stored_credentials,
    list_logged_in_providers,
    list_providers,
    refresh_credentials,
    register_provider,
    unregister_provider,
)


class _FakeProvider:
    """Minimal `OAuthProvider` for testing the registry plumbing."""

    id = "fake"
    name = "Fake provider"
    uses_callback_server = False

    def __init__(self) -> None:
        self.refresh_calls = 0

    async def login(self, callbacks: OAuthCallbacks) -> OAuthCredentials:  # noqa: ARG002
        return OAuthCredentials(
            access="fake-access", refresh="fake-refresh", expires=0.0
        )

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        self.refresh_calls += 1
        return OAuthCredentials(
            access=f"refreshed-{self.refresh_calls}",
            refresh=credentials.refresh,
            expires=10**12,  # far future
        )

    def get_api_key(self, credentials: OAuthCredentials) -> str:
        return credentials.access


class TestRegistry:
    """Provider-registry operations."""

    def test_built_in_providers_present(self) -> None:
        """The three built-ins always appear in `list_providers`."""
        ids = {p.id for p in list_providers()}
        assert {"anthropic", "github-copilot", "openai-codex"} <= ids

    def test_register_replaces_built_in(self) -> None:
        class _Replacement:
            id = "anthropic"
            name = "Mocked Anthropic"
            uses_callback_server = False

            async def login(self, callbacks: OAuthCallbacks) -> OAuthCredentials:  # noqa: ARG002
                return OAuthCredentials(access="x", refresh="y", expires=0.0)

            async def refresh_token(
                self, credentials: OAuthCredentials
            ) -> OAuthCredentials:
                return credentials

            def get_api_key(self, credentials: OAuthCredentials) -> str:
                return credentials.access

        replacement = _Replacement()
        register_provider(replacement)
        assert get_provider("anthropic") is replacement
        unregister_provider("anthropic")
        # Built-in is restored after unregister.
        builtin = get_provider("anthropic")
        assert builtin is not None
        assert builtin.name == "Anthropic (Claude Pro/Max)"

    def test_unknown_provider_returns_none(self) -> None:
        assert get_provider("does-not-exist") is None


@pytest.mark.usefixtures("fake_state_dir")
class TestStoredCredentials:
    """`get_stored_credentials` and `delete_credentials` helpers."""

    def test_get_stored_returns_none_when_missing(self) -> None:
        assert get_stored_credentials("anthropic") is None

    def test_delete_returns_false_when_missing(self) -> None:
        assert delete_credentials("anthropic") is False


@pytest.mark.usefixtures("fake_state_dir")
class TestExplicitRefreshHook:
    """`refresh_credentials` is the documented explicit-refresh entry point."""

    async def test_refresh_persists_new_credentials(self) -> None:
        provider = _FakeProvider()
        register_provider(provider)
        old = OAuthCredentials(access="old", refresh="r", expires=0.0)
        refreshed = await refresh_credentials("fake", old)
        assert refreshed.access == "refreshed-1"
        # Persisted to disk
        stored = get_stored_credentials("fake")
        assert stored is not None
        assert stored.access == "refreshed-1"
        assert "fake" in list_logged_in_providers()

    async def test_refresh_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown OAuth provider"):
            await refresh_credentials(
                "nope",
                OAuthCredentials(access="a", refresh="r", expires=0.0),
            )


@pytest.mark.usefixtures("fake_state_dir")
class TestGetAccessToken:
    """`get_access_token` honours the auto-refresh-on-expired contract."""

    async def test_returns_none_when_no_credentials(self) -> None:
        assert await get_access_token("anthropic") is None

    async def test_returns_stored_token_when_valid(self) -> None:
        provider = _FakeProvider()
        register_provider(provider)
        from deepagents_cli.oauth.storage import save_credentials

        save_credentials(
            "fake",
            OAuthCredentials(access="still-good", refresh="r", expires=10**12),
        )
        assert await get_access_token("fake") == "still-good"
        assert provider.refresh_calls == 0

    async def test_refreshes_when_expired(self) -> None:
        provider = _FakeProvider()
        register_provider(provider)
        from deepagents_cli.oauth.storage import save_credentials

        save_credentials(
            "fake",
            OAuthCredentials(access="expired", refresh="r", expires=0.0),
        )
        token = await get_access_token("fake")
        assert token == "refreshed-1"
        assert provider.refresh_calls == 1

    async def test_refresh_skipped_when_caller_opts_out(self) -> None:
        provider = _FakeProvider()
        register_provider(provider)
        from deepagents_cli.oauth.storage import save_credentials

        save_credentials(
            "fake",
            OAuthCredentials(access="expired", refresh="r", expires=0.0),
        )
        token = await get_access_token("fake", refresh_if_expired=False)
        assert token == "expired"
        assert provider.refresh_calls == 0
