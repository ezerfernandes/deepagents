"""Tests for the Anthropic OAuth provider."""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from deepagents_cli.oauth.providers._common import REFRESH_SAFETY_MARGIN_SECONDS
from deepagents_cli.oauth.providers.anthropic import (
    TOKEN_URL,
    is_oauth_token,
    refresh_anthropic_token,
)
from deepagents_cli.oauth.types import OAuthRefreshError


class TestIsOAuthToken:
    """`isOAuthToken`-equivalent prefix detection."""

    def test_oauth_token_is_detected(self) -> None:
        assert is_oauth_token("sk-ant-oat01-abcdef") is True

    def test_api_key_is_not_an_oauth_token(self) -> None:
        assert is_oauth_token("sk-ant-api03-xyz") is False

    def test_random_string_is_not_an_oauth_token(self) -> None:
        assert is_oauth_token("garbage") is False


class TestRefresh:
    """Behaviour of `refresh_anthropic_token`."""

    @respx.mock
    async def test_refresh_returns_new_credentials_with_safety_margin(self) -> None:
        """Refresh applies the 5-minute safety margin to `expires`."""
        before = time.time()
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "scope": "user:inference",
                },
            )
        )
        creds = await refresh_anthropic_token("old-refresh")
        assert creds.access == "new-access"
        assert creds.refresh == "new-refresh"
        # 3600s lifetime minus the 300s safety margin, plus a generous
        # window for clock noise during the test run.
        expected_min = before + 3600 - REFRESH_SAFETY_MARGIN_SECONDS - 1
        expected_max = time.time() + 3600 - REFRESH_SAFETY_MARGIN_SECONDS + 1
        assert expected_min <= creds.expires <= expected_max

    @respx.mock
    async def test_refresh_omits_scope_field(self) -> None:
        """Anthropic rejects refresh requests that include `scope`."""
        captured: dict[str, object] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read()
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 1800,
                },
            )

        respx.post(TOKEN_URL).mock(side_effect=_handler)
        await refresh_anthropic_token("old-refresh")
        body = captured["body"]
        assert b"scope" not in body  # type: ignore[operator]

    @respx.mock
    async def test_refresh_failure_raises_oauth_refresh_error(self) -> None:
        """Non-2xx responses surface as `OAuthRefreshError`."""
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(401, text="invalid_grant")
        )
        with pytest.raises(OAuthRefreshError, match="refresh failed"):
            await refresh_anthropic_token("dead-refresh")

    @respx.mock
    async def test_missing_fields_raise_oauth_refresh_error(self) -> None:
        """Token responses without required fields don't silently succeed."""
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "only"})
        )
        with pytest.raises(OAuthRefreshError, match="unexpected payload"):
            await refresh_anthropic_token("r")
