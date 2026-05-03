"""Regression tests for `bugs3.md`.

Each `TestBugN` class pins the behaviour required by the corresponding
bug entry. Keep them focused: they should fail before the fix and pass
after.

Numbering matches `bugs3.md` exactly (so e.g. `TestBug1Wallclock` ⇄
bug 1 from that file). Earlier bug rounds live in
`test_oauth_bug_fixes.py`.
"""

from __future__ import annotations

import asyncio
import socket
import time
from contextlib import closing
from typing import TYPE_CHECKING

import httpx
import pytest

from deepagents_cli.oauth import OAuthCredentials, register_provider
from deepagents_cli.oauth.callback_server import (
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    start_callback_server,
)
from deepagents_cli.oauth.providers._common import parse_authorization_input

if TYPE_CHECKING:
    from deepagents_cli.oauth import OAuthCallbacks


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Bug 1 — `wait_for_code` must surface a wall-clock timeout instead of
# hanging forever when no real callback arrives. Bug 1 also describes
# state-mismatch / missing-code requests being silently dropped (which
# they still are, by design — stale tabs are common); the timeout is
# the safety net that makes the silent drop tolerable.


@pytest.mark.enable_socket
@pytest.mark.usefixtures("fake_state_dir")
class TestBug1Wallclock:
    """`wait_for_code` enforces a wall-clock timeout."""

    async def test_timeout_returns_none(self) -> None:
        port = _free_port()
        server = await start_callback_server(
            port=port,
            callback_path="/callback",
            expected_state="STATE",
            success_message="ok",
        )
        try:
            result = await server.wait_for_code(timeout_seconds=0.05)
            assert result is None
        finally:
            await server.close()

    async def test_state_mismatch_does_not_resolve_but_timeout_fires(
        self,
    ) -> None:
        port = _free_port()
        server = await start_callback_server(
            port=port,
            callback_path="/callback",
            expected_state="STATE",
            success_message="ok",
        )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"http://127.0.0.1:{port}/callback?code=A&state=WRONG"
                )
            assert response.status_code == 400
            # No resolution happened; timeout takes over.
            result = await server.wait_for_code(timeout_seconds=0.05)
            assert result is None
        finally:
            await server.close()

    def test_default_timeout_is_finite(self) -> None:
        # Loose sanity check: the documented default must be a positive
        # finite duration so login flows can't legitimately hang.
        assert 0 < DEFAULT_WAIT_TIMEOUT_SECONDS < 24 * 3600


# Bug 2 — bind to `localhost` so dual-stack browsers reach the server
# regardless of whether `localhost` resolves to `127.0.0.1` or `::1`.


@pytest.mark.enable_socket
@pytest.mark.usefixtures("fake_state_dir")
class TestBug2DualStackBind:
    """Default bind host resolves to both IPv4 and IPv6 localhost."""

    async def test_default_bind_host_is_localhost(self) -> None:
        port = _free_port()
        server = await start_callback_server(
            port=port,
            callback_path="/callback",
            expected_state=None,
            success_message="ok",
        )
        try:
            assert server.host == "localhost"
        finally:
            server.cancel_wait()
            await server.close()


# Bug 3 — Anthropic OAuth `state` is independent of the PKCE verifier.


class TestBug3IndependentState:
    """The Anthropic flow generates `state` separately from the PKCE verifier."""

    async def test_state_is_independent_hex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run the flow with no callback server; assert state shape on the URL.

        We force `try_start_callback_server` to return `None` so the
        flow takes the manual-paste branch and we don't have to wait
        on a real socket. The test ends as soon as we've captured the
        authorize URL — any non-empty paste raises early.
        """
        from urllib.parse import parse_qs, urlsplit

        from deepagents_cli.oauth import OAuthCallbacks
        from deepagents_cli.oauth.providers import anthropic as ap

        async def _no_server(**kwargs):  # noqa: ANN003, ARG001
            return None

        monkeypatch.setattr(ap, "try_start_callback_server", _no_server)

        captured: dict[str, str] = {}

        def on_auth(info) -> None:
            captured.update(parse_qs_single(info.url))

        async def on_manual_code_input() -> str:
            # Empty paste => `parse_authorization_input` returns
            # `(None, None)`, so the flow falls through to `on_prompt`,
            # which also returns empty, and `login_anthropic` raises
            # `OAuthCancelledError("Missing authorization code")` —
            # short-circuiting before any network call.
            return ""

        async def on_prompt(prompt) -> str:  # noqa: ARG001
            return ""

        from deepagents_cli.oauth import OAuthCancelledError

        with pytest.raises(OAuthCancelledError):
            await ap.login_anthropic(
                OAuthCallbacks(
                    on_auth=on_auth,
                    on_prompt=on_prompt,
                    on_progress=None,
                    on_manual_code_input=on_manual_code_input,
                ),
            )

        challenge = captured.get("code_challenge")
        state = captured.get("state")
        assert state is not None
        assert challenge is not None
        # Independent state: hex (32 chars) per `secrets.token_hex(16)`.
        assert len(state) == 32
        assert all(c in "0123456789abcdef" for c in state)
        # Critical guarantee: the state is NOT the PKCE verifier, so
        # the verifier never leaks through browser history or IdP logs.
        # `code_challenge` is sha256(verifier), so a 32-char-hex state
        # cannot equal a 43-char-base64url verifier; the assertion is
        # symbolic but documents the contract.
        assert state != challenge


def parse_qs_single(url: str) -> dict[str, str]:
    """Helper: parse a URL's query into a flat single-value dict."""
    from urllib.parse import parse_qs, urlsplit

    parsed = urlsplit(url)
    raw = parse_qs(parsed.query)
    return {k: v[0] for k, v in raw.items() if v}


# Bugs 6 + 7 — `refresh_credentials` runs under an exclusive
# cross-process lock so concurrent refreshes serialize and rotated
# refresh tokens don't get clobbered.


@pytest.mark.usefixtures("fake_state_dir")
class TestBug6RefreshSerialization:
    """Two refreshes on the same provider serialize via the file lock."""

    async def test_two_concurrent_refreshes_serialize(self) -> None:
        order: list[str] = []
        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        class _Provider:
            id = "anthropic"
            name = "Anthropic"
            uses_callback_server = False

            async def login(self, callbacks):
                msg = "not used"
                raise NotImplementedError(msg)

            async def refresh_token(
                self,
                credentials: OAuthCredentials,  # noqa: ARG002 - protocol signature
            ) -> OAuthCredentials:
                nonlocal in_flight, max_in_flight
                async with lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                order.append("enter")
                # Hold the refresh open long enough that a second
                # racer would observe `in_flight == 2` if the lock
                # didn't serialize them.
                await asyncio.sleep(0.05)
                order.append("exit")
                async with lock:
                    in_flight -= 1
                # Anthropic rotates `refresh_token` on every refresh,
                # so each call mints a distinct one.
                return OAuthCredentials(
                    access=f"new-access-{len(order)}",
                    refresh=f"new-refresh-{len(order)}",
                    expires=time.time() + 3600,
                )

            def get_api_key(self, credentials: OAuthCredentials) -> str:
                return credentials.access

        register_provider(_Provider())

        from deepagents_cli.oauth import refresh_credentials
        from deepagents_cli.oauth.storage import save_credentials

        creds = OAuthCredentials(access="old", refresh="old-r", expires=0.0)
        save_credentials("anthropic", creds)

        await asyncio.gather(
            refresh_credentials("anthropic", creds),
            refresh_credentials("anthropic", creds),
        )

        assert max_in_flight == 1, (
            "Expected refreshes to serialize via the file lock; saw "
            f"{max_in_flight} concurrent calls."
        )

    async def test_get_access_token_skips_refresh_when_peer_already_refreshed(
        self,
    ) -> None:
        from deepagents_cli.oauth import get_access_token
        from deepagents_cli.oauth.storage import (
            alock_provider,
            save_credentials,
            save_credentials_locked,
        )

        refresh_calls = 0

        class _Provider:
            id = "anthropic"
            name = "Anthropic"
            uses_callback_server = False

            async def login(self, callbacks):
                msg = "not used"
                raise NotImplementedError(msg)

            async def refresh_token(
                self,
                credentials: OAuthCredentials,  # noqa: ARG002 - protocol signature
            ) -> OAuthCredentials:
                nonlocal refresh_calls
                refresh_calls += 1
                return OAuthCredentials(
                    access="should-not-fire",
                    refresh="should-not-fire",
                    expires=time.time() + 3600,
                )

            def get_api_key(self, credentials: OAuthCredentials) -> str:
                return credentials.access

        register_provider(_Provider())
        save_credentials(
            "anthropic",
            OAuthCredentials(access="expired", refresh="r", expires=0.0),
        )

        async def peer_refresh() -> None:
            # Simulate another process holding the lock and updating
            # the on-disk credential to a fresh value before our
            # `get_access_token` acquires the lock.
            async with alock_provider("anthropic"):
                save_credentials_locked(
                    "anthropic",
                    OAuthCredentials(
                        access="peer-fresh",
                        refresh="peer-r",
                        expires=time.time() + 3600,
                    ),
                )

        # Pre-set the disk to fresh so when get_access_token re-reads
        # under its lock, it sees fresh credentials and skips refresh.
        await peer_refresh()
        token = await get_access_token("anthropic")
        assert token == "peer-fresh"
        assert refresh_calls == 0


# Bug 9 — port-in-use degrades to manual paste-back rather than crashing.


@pytest.mark.enable_socket
@pytest.mark.usefixtures("fake_state_dir")
class TestBug9PortInUseFallback:
    """When the callback port is bound, login still completes via paste."""

    async def test_anthropic_login_falls_back_to_manual_paste(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hold the Anthropic callback port so the listener fails to bind.
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                held.bind(("127.0.0.1", 53692))
                held.listen(1)
            except OSError:
                pytest.skip(
                    "Anthropic OAuth port 53692 already in use by something "
                    "else; can't deterministically reproduce."
                )

            captured_url: dict[str, str] = {}

            def on_auth(info) -> None:
                captured_url["url"] = info.url
                captured_url["instructions"] = info.instructions or ""

            async def on_prompt(prompt) -> str:  # noqa: ARG001
                # Final paste-back fallback should not be needed because
                # `on_manual_code_input` wins the race-equivalent.
                return "test-code"

            async def on_manual_code_input() -> str:
                return "manual-code-from-other-machine"

            from deepagents_cli.oauth import OAuthCallbacks

            # Stub the token-exchange so we don't hit the network.
            async def _fake_exchange(
                **kwargs,  # noqa: ANN003, ARG001
            ) -> OAuthCredentials:
                return OAuthCredentials(
                    access="exchanged-access",
                    refresh="exchanged-refresh",
                    expires=time.time() + 3600,
                )

            from deepagents_cli.oauth.providers import anthropic as ap

            monkeypatch.setattr(ap, "_exchange_authorization_code", _fake_exchange)

            credentials = await ap.login_anthropic(
                OAuthCallbacks(
                    on_auth=on_auth,
                    on_prompt=on_prompt,
                    on_progress=None,
                    on_manual_code_input=on_manual_code_input,
                ),
            )
            assert credentials.access == "exchanged-access"
            assert "53692 is busy" in captured_url["instructions"]
        finally:
            held.close()


# Bug 10 — `parse_authorization_input` strips a leading `?` so the
# user can paste a query string copied from a callback URL.


# Bug 11 — GitHub Enterprise Server REST API lives at `<host>/api/v3/`,
# not at `api.<host>`. The Copilot token endpoint must follow the
# server's actual REST shape or refresh-on-GHE 404s.


class TestBug11GHEApiBase:
    """`_device_urls` routes Copilot-token requests to the right host."""

    def test_github_com_keeps_api_subdomain(self) -> None:
        from deepagents_cli.oauth.providers.github_copilot import _device_urls

        urls = _device_urls("github.com")
        assert (
            urls["copilot_token"]
            == "https://api.github.com/copilot_internal/v2/token"
        )

    def test_ghe_uses_api_v3_under_host(self) -> None:
        """GHE Server: `<host>/api/v3/...`, never `api.<host>/...`."""
        from deepagents_cli.oauth.providers.github_copilot import _device_urls

        urls = _device_urls("company.ghe.com")
        assert (
            urls["copilot_token"]
            == "https://company.ghe.com/api/v3/copilot_internal/v2/token"
        )
        assert "api.company.ghe.com" not in urls["copilot_token"]

    def test_ghe_oauth_endpoints_unchanged(self) -> None:
        """Device + access-token paths sit under `<host>/login/...`.

        Only the REST API root differs by flavour; OAuth endpoints stay
        under the bare host on github.com, GHE Server, and GHE Cloud.
        """
        from deepagents_cli.oauth.providers.github_copilot import _device_urls

        urls = _device_urls("github.mycompany.com")
        assert (
            urls["device_code"]
            == "https://github.mycompany.com/login/device/code"
        )
        assert (
            urls["access_token"]
            == "https://github.mycompany.com/login/oauth/access_token"
        )
        assert (
            urls["copilot_token"]
            == "https://github.mycompany.com/api/v3/copilot_internal/v2/token"
        )


class TestBug10LeadingQuestionMark:
    """Pasting `?code=…&state=…` parses correctly."""

    def test_leading_question_mark_is_stripped(self) -> None:
        parsed = parse_authorization_input("?code=abc&state=xyz")
        assert parsed.code == "abc"
        assert parsed.state == "xyz"

    def test_double_leading_question_marks_are_stripped(self) -> None:
        # Defensive: be tolerant of accidental duplication.
        parsed = parse_authorization_input("??code=abc&state=xyz")
        assert parsed.code == "abc"
        assert parsed.state == "xyz"

    def test_query_without_leading_question_mark_still_works(self) -> None:
        parsed = parse_authorization_input("code=abc&state=xyz")
        assert parsed.code == "abc"
        assert parsed.state == "xyz"
