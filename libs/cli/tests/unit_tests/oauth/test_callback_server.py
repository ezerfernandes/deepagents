"""Tests for the local OAuth callback server.

These tests bind real TCP listeners and connect to them through
`httpx`, so they need to opt out of `pytest-socket`'s default
network-block. The whole module is gated behind the
`pytest.mark.enable_socket` marker rather than per-test so future
additions don't silently regress to the blocked path.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import closing

import httpx
import pytest

from deepagents_cli.oauth.callback_server import start_callback_server

pytestmark = pytest.mark.enable_socket


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_success_sets_future_and_returns_html() -> None:
    """A matching `state` resolves the future and returns 200/HTML."""
    port = _free_port()
    server = await start_callback_server(
        port=port,
        callback_path="/callback",
        expected_state="STATE",
        success_message="welcome",
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/callback?code=AAA&state=STATE"
            )
        assert response.status_code == 200
        assert "Authentication successful" in response.text
        result = await asyncio.wait_for(server.wait_for_code(), timeout=5)
        assert result is not None
        assert result.code == "AAA"
        assert result.state == "STATE"
    finally:
        await server.close()


async def test_state_mismatch_returns_400_and_does_not_resolve() -> None:
    """Wrong `state` ⇒ 400, future never resolves to a result."""
    port = _free_port()
    server = await start_callback_server(
        port=port,
        callback_path="/callback",
        expected_state="STATE",
        success_message="welcome",
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/callback?code=AAA&state=WRONG"
            )
        assert response.status_code == 400
        assert "State mismatch" in response.text
        # Future should still be pending; cancel it so the test exits.
        server.cancel_wait()
        result = await asyncio.wait_for(server.wait_for_code(), timeout=2)
        assert result is None
    finally:
        await server.close()


async def test_unknown_path_returns_404() -> None:
    """Routes other than the callback path get a 404."""
    port = _free_port()
    server = await start_callback_server(
        port=port,
        callback_path="/callback",
        expected_state=None,
        success_message="welcome",
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/other")
        assert response.status_code == 404
    finally:
        server.cancel_wait()
        await server.close()


async def test_provider_error_propagates_to_future() -> None:
    """An `error=...` query causes `wait_for_code` to raise."""
    port = _free_port()
    server = await start_callback_server(
        port=port,
        callback_path="/callback",
        expected_state="STATE",
        success_message="welcome",
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/callback"
                "?error=access_denied&error_description=user%20denied"
            )
        assert response.status_code == 400
        with pytest.raises(RuntimeError, match="Authorization denied"):
            await asyncio.wait_for(server.wait_for_code(), timeout=2)
    finally:
        await server.close()


async def test_cancel_wait_returns_none() -> None:
    """`cancel_wait` lets the manual-paste path win the race."""
    port = _free_port()
    server = await start_callback_server(
        port=port,
        callback_path="/callback",
        expected_state="STATE",
        success_message="welcome",
    )
    try:
        server.cancel_wait()
        result = await asyncio.wait_for(server.wait_for_code(), timeout=2)
        assert result is None
    finally:
        await server.close()


async def test_callback_host_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DEEPAGENTS_CLI_OAUTH_CALLBACK_HOST` controls the bind address."""
    monkeypatch.setenv("DEEPAGENTS_CLI_OAUTH_CALLBACK_HOST", "127.0.0.1")
    port = _free_port()
    server = await start_callback_server(
        port=port,
        callback_path="/callback",
        expected_state="STATE",
        success_message="welcome",
    )
    try:
        assert server.host == "127.0.0.1"
    finally:
        server.cancel_wait()
        await server.close()
