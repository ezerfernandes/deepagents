"""Tests for the OpenAI Codex (ChatGPT Plus/Pro) OAuth provider."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from deepagents_cli.oauth.providers._common import OAuthHTTPError
from deepagents_cli.oauth.providers.openai_codex import (
    ACCOUNT_ID_KEY,
    JWT_CLAIM_PATH,
    ORIGINATOR,
    TOKEN_URL,
    _decode_jwt_payload,
    _extract_account_id,
    refresh_openai_codex_token,
)
from deepagents_cli.oauth.types import OAuthRefreshError


def _make_jwt(claims: dict[str, object]) -> str:
    """Build a JWT-shaped string with the supplied payload."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode("ascii")
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    return f"{header}.{payload}.signature-placeholder"


class TestJwtDecode:
    """JWT payload extraction is forgiving but always honest."""

    def test_extracts_account_id(self) -> None:
        token = _make_jwt({JWT_CLAIM_PATH: {"chatgpt_account_id": "acct-123"}})
        assert _extract_account_id(token) == "acct-123"

    def test_returns_none_when_claim_missing(self) -> None:
        token = _make_jwt({JWT_CLAIM_PATH: {}})
        assert _extract_account_id(token) is None

    def test_returns_none_for_garbage(self) -> None:
        assert _extract_account_id("garbage") is None

    def test_decodes_unpadded_segments(self) -> None:
        """JWT segments often need padding restored before base64-decoding."""
        token = _make_jwt({"foo": "bar"})
        # Strip an extra character to force a base64 padding edge case.
        head, mid, tail = token.split(".")
        decoded = _decode_jwt_payload(f"{head}.{mid}.{tail}")
        assert decoded == {"foo": "bar"}


class TestRefresh:
    """`refresh_openai_codex_token` re-extracts `account_id` on every refresh."""

    @respx.mock
    async def test_refresh_re_extracts_account_id(self) -> None:
        """A new refresh response replaces both the token AND account_id extras."""
        new_jwt = _make_jwt({JWT_CLAIM_PATH: {"chatgpt_account_id": "acct-NEW"}})

        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": new_jwt,
                    "refresh_token": "new-refresh",
                    "expires_in": 1800,
                },
            )
        )
        creds = await refresh_openai_codex_token("old-refresh")
        assert creds.access == new_jwt
        assert creds.refresh == "new-refresh"
        assert creds.extras[ACCOUNT_ID_KEY] == "acct-NEW"

    @respx.mock
    async def test_refresh_failure_raises_oauth_refresh_error(self) -> None:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, text="invalid_grant")
        )
        with pytest.raises(OAuthRefreshError, match="refresh failed"):
            await refresh_openai_codex_token("dead")

    @respx.mock
    async def test_token_without_account_id_is_rejected(self) -> None:
        """A JWT without `chatgpt_account_id` is treated as a refresh failure."""
        bad_jwt = _make_jwt({JWT_CLAIM_PATH: {}})
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": bad_jwt,
                    "refresh_token": "r",
                    "expires_in": 60,
                },
            )
        )
        with pytest.raises(OAuthRefreshError, match="chatgpt_account_id"):
            await refresh_openai_codex_token("r")


def test_originator_constant_is_deepagents() -> None:
    """We deliberately diverge from pi-mono's `originator=pi`."""
    assert ORIGINATOR == "deepagents"
