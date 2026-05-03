"""Tests for the GitHub Copilot OAuth provider."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from deepagents_cli.oauth.providers.github_copilot import (
    ENTERPRISE_KEY,
    _device_urls,
    enable_all_github_copilot_models,
    get_github_copilot_base_url,
    normalize_domain,
    refresh_github_copilot_token,
)
from deepagents_cli.oauth.types import (
    OAuthCredentials,
    OAuthRefreshError,
)


class TestNormalizeDomain:
    """Domain coercion for github.com vs Enterprise."""

    def test_bare_domain(self) -> None:
        assert normalize_domain("company.ghe.com") == "company.ghe.com"

    def test_url(self) -> None:
        assert normalize_domain("https://company.ghe.com/foo") == "company.ghe.com"

    def test_empty_returns_none(self) -> None:
        assert normalize_domain("") is None

    def test_whitespace_returns_none(self) -> None:
        assert normalize_domain("   ") is None


class TestProxyEpParsing:
    """Token `proxy-ep` parsing controls the base URL."""

    def test_proxy_ep_is_rewritten_to_api(self) -> None:
        token = "tid=foo;exp=1;proxy-ep=proxy.individual.githubcopilot.com;x=y"
        assert (
            get_github_copilot_base_url(token)
            == "https://api.individual.githubcopilot.com"
        )

    def test_enterprise_fallback(self) -> None:
        assert (
            get_github_copilot_base_url(None, "company.ghe.com")
            == "https://copilot-api.company.ghe.com"
        )

    def test_default_when_no_token_no_enterprise(self) -> None:
        assert (
            get_github_copilot_base_url(None, None)
            == "https://api.individual.githubcopilot.com"
        )


class TestRefresh:
    """`refresh_github_copilot_token` mints a fresh Copilot token."""

    @respx.mock
    async def test_refresh_returns_new_short_lived_copilot_token(self) -> None:
        """The refresh hits `copilot_internal/v2/token` with the user token."""
        urls = _device_urls("github.com")
        future_expires_at = int(time.time()) + 3600
        captured: dict[str, str] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["authorization"]
            captured["ua"] = request.headers["user-agent"]
            return httpx.Response(
                200,
                json={
                    "token": "tid=x;proxy-ep=proxy.foo.githubcopilot.com",
                    "expires_at": future_expires_at,
                },
            )

        respx.get(urls["copilot_token"]).mock(side_effect=_handler)
        creds = await refresh_github_copilot_token(
            OAuthCredentials(
                access="old-copilot",
                refresh="ghu_USER_TOKEN",
                expires=0.0,
            )
        )
        assert creds.refresh == "ghu_USER_TOKEN"
        assert "proxy-ep=" in creds.access
        assert captured["auth"] == "Bearer ghu_USER_TOKEN"
        assert captured["ua"] == "GitHubCopilotChat/0.35.0"
        # 5-minute safety margin pulled the expiry below the wire value.
        assert creds.expires < future_expires_at

    @respx.mock
    async def test_refresh_uses_enterprise_url(self) -> None:
        """Stored `enterpriseUrl` extra routes refresh to the GHE host."""
        urls = _device_urls("company.ghe.com")
        respx.get(urls["copilot_token"]).mock(
            return_value=httpx.Response(
                200,
                json={
                    "token": "tid=x;proxy-ep=proxy.ghe.githubcopilot.com",
                    "expires_at": int(time.time()) + 60,
                },
            )
        )
        creds = await refresh_github_copilot_token(
            OAuthCredentials(
                access="x",
                refresh="ghu_X",
                expires=0.0,
                extras={ENTERPRISE_KEY: "company.ghe.com"},
            )
        )
        assert creds.extras.get(ENTERPRISE_KEY) == "company.ghe.com"

    @respx.mock
    async def test_refresh_failure_raises_oauth_refresh_error(self) -> None:
        urls = _device_urls("github.com")
        respx.get(urls["copilot_token"]).mock(
            return_value=httpx.Response(401, text="bad token")
        )
        with pytest.raises(OAuthRefreshError, match="refresh failed"):
            await refresh_github_copilot_token(
                OAuthCredentials(access="x", refresh="ghu_DEAD", expires=0.0)
            )


class TestEnableModels:
    """`enable_all_github_copilot_models` POSTs once per model id."""

    @respx.mock
    async def test_enable_all_swallows_per_model_failures(self) -> None:
        """A single 400 doesn't abort the rest of the list."""
        respx.post("https://api.individual.githubcopilot.com/models/gpt-5/policy").mock(
            return_value=httpx.Response(200)
        )
        respx.post("https://api.individual.githubcopilot.com/models/o3/policy").mock(
            return_value=httpx.Response(400, text="already enabled")
        )
        # The full set fires too, so accept everything else with 200.
        respx.route().mock(return_value=httpx.Response(200))
        await enable_all_github_copilot_models(
            "tid=x;proxy-ep=proxy.individual.githubcopilot.com",
            None,
            model_ids=("gpt-5", "o3"),
        )
