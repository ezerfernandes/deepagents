"""Local HTTP server that catches the OAuth `GET /callback` redirect.

Built on `asyncio.start_server` rather than `http.server.HTTPServer` so
the login flow can race the callback against a manual paste-back
(`OAuthCallbacks.on_manual_code_input`) inside a single event loop.
We parse just enough HTTP to handle one request line, drain the rest of
the headers, and respond — no body, no keep-alive, no chunked transfer.

Mirrors the behaviour of the Node `http.createServer` callback in
`pi-mono/packages/ai/src/utils/oauth/{anthropic,openai-codex}.ts`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import parse_qs, urlsplit

from deepagents_cli._env_vars import OAUTH_CALLBACK_HOST
from deepagents_cli.oauth.pages import oauth_error_html, oauth_success_html

logger = logging.getLogger(__name__)

_REQUEST_LINE_LIMIT = 8192
"""Largest first-line we accept from the browser, in bytes."""

_HEADER_DRAIN_LIMIT = 16 * 1024
"""Largest header block we accept before dropping the connection."""

DEFAULT_WAIT_TIMEOUT_SECONDS = 5 * 60.0
"""Wall-clock cap on how long `wait_for_code()` will block.

Without it the login flow can hang indefinitely when the user closes
the browser tab, the IdP rate-limits, or a stale tab fires
`/callback?...` with a wrong `state` (which we silently drop).
"""


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """Successful callback payload extracted from the URL query string."""

    code: str
    state: str | None
    extras: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CallbackServer:
    """A running OAuth callback server.

    Returned by `start_callback_server`. Always close with `await
    server.close()` — even if the flow succeeded — because the listening
    socket must be released before the next login attempt on the same
    port can bind.
    """

    host: str
    port: int
    redirect_uri: str
    _server: asyncio.AbstractServer
    _future: asyncio.Future[CallbackResult | None]

    async def wait_for_code(
        self,
        *,
        timeout_seconds: float | None = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> CallbackResult | None:
        """Return the captured callback, or `None` when cancelled / timed out.

        A timeout fires either when the user abandons the browser tab or
        when our `_handle_request` silently drops a request that didn't
        meet the success criteria (state mismatch, missing code, stale
        preflight). Returning `None` lets the login flow fall through to
        the manual-paste prompt instead of hanging forever.
        """
        if timeout_seconds is None:
            try:
                return await self._future
            except asyncio.CancelledError:
                return None
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._future), timeout=timeout_seconds
            )
        except (TimeoutError, asyncio.CancelledError):
            return None

    def cancel_wait(self) -> None:
        """Resolve `wait_for_code()` with `None` without an error.

        Used when the manual-paste path wins the race against the
        callback server.
        """
        if not self._future.done():
            self._future.set_result(None)

    async def close(self) -> None:
        """Stop accepting connections and wait for in-flight ones to finish."""
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:
            logger.debug(
                "OAuth callback server on %s:%s closed with exception (safe to ignore)",
                self.host,
                self.port,
                exc_info=True,
            )


async def try_start_callback_server(
    *,
    port: int,
    callback_path: str,
    expected_state: str | None,
    success_message: str,
    host: str | None = None,
) -> CallbackServer | None:
    """Same as `start_callback_server`, but returns `None` if the port is busy.

    Used by login flows that can degrade to manual paste-back when the
    port is occupied (e.g. another `deepagents login` is mid-flight or
    a leftover process is still bound). The caller is responsible for
    surfacing a clear message to the user before falling back.
    """
    try:
        return await start_callback_server(
            port=port,
            callback_path=callback_path,
            expected_state=expected_state,
            success_message=success_message,
            host=host,
        )
    except OSError as exc:
        logger.warning(
            "Could not bind OAuth callback server on port %s (%s); "
            "falling back to manual paste-back.",
            port,
            exc,
        )
        return None


async def start_callback_server(
    *,
    port: int,
    callback_path: str,
    expected_state: str | None,
    success_message: str,
    host: str | None = None,
) -> CallbackServer:
    """Listen on `(host, port)` for one OAuth `GET <callback_path>` request.

    Args:
        port: TCP port to bind. Reusing pi-mono's per-provider ports
            (53692 for Anthropic, 1455 for OpenAI Codex) keeps the
            registered redirect URIs identical.
        callback_path: Path component the browser will hit
            (e.g. `/callback` or `/auth/callback`). Other paths get a
            404.
        expected_state: When non-`None`, the server returns 400 if
            `state` does not match. Pass `None` for flows that don't
            use state (none of ours, but kept for symmetry).
        success_message: Body for the success HTML page.
        host: Bind address. Defaults to
            `os.environ[DEEPAGENTS_CLI_OAUTH_CALLBACK_HOST]` or
            `localhost`. We bind to `localhost` (not `127.0.0.1`) so
            `asyncio.start_server` resolves through `getaddrinfo` and
            binds **both** IPv4 (`127.0.0.1`) and IPv6 (`::1`) — on
            dual-stack Linux/macOS, modern browsers prefer `::1` for
            `localhost`, and a v4-only bind would silently miss the
            redirect.

    Returns:
        A `CallbackServer` bound to a freshly-started TCP listener.

    Raises:
        OSError: If the port is already in use.
    """
    bind_host = host or os.environ.get(OAUTH_CALLBACK_HOST) or "localhost"
    redirect_uri = f"http://localhost:{port}{callback_path}"

    loop = asyncio.get_running_loop()
    future: asyncio.Future[CallbackResult | None] = loop.create_future()

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await _handle_request(
                reader=reader,
                writer=writer,
                callback_path=callback_path,
                expected_state=expected_state,
                success_message=success_message,
                future=future,
            )
        except Exception:
            logger.exception("OAuth callback handler crashed")
            with _suppress_close_errors():
                writer.close()

    server = await asyncio.start_server(handle, bind_host, port)
    return CallbackServer(
        host=bind_host,
        port=port,
        redirect_uri=redirect_uri,
        _server=server,
        _future=future,
    )


async def _handle_request(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    callback_path: str,
    expected_state: str | None,
    success_message: str,
    future: asyncio.Future[CallbackResult | None],
) -> None:
    """Parse a single GET request and either resolve `future` or return an error.

    Browser preflight quirk: Chrome and Edge sometimes open speculative
    connections to the callback URL before the user finishes the OAuth
    consent screen. Those connections never send a request line — we
    drop them silently rather than logging warnings.
    """
    request_line = await _read_line(reader, _REQUEST_LINE_LIMIT)
    if not request_line:
        with _suppress_close_errors():
            writer.close()
        return

    method, _, rest = request_line.partition(" ")
    target, _, _ = rest.partition(" ")
    method = method.upper()

    if method != "GET":
        await _send_error(writer, status=405, message="Only GET is supported.")
        return

    parsed = urlsplit(target)
    if parsed.path != callback_path:
        await _send_error(writer, status=404, message="Callback route not found.")
        return

    # Drain (and discard) headers so curl/Chrome don't see a RST.
    drained = 0
    while drained < _HEADER_DRAIN_LIMIT:
        line = await _read_line(reader, _REQUEST_LINE_LIMIT)
        if line in {"", "\r"}:
            break
        drained += len(line)

    params = parse_qs(parsed.query, keep_blank_values=True)

    error = (params.get("error") or [None])[0]
    if error:
        description = (params.get("error_description") or [""])[0]
        detail = f"Error: {error}" + (f" — {description}" if description else "")
        await _send_html(
            writer,
            status=400,
            body=oauth_error_html(
                "Authentication did not complete.",
                detail,
            ),
        )
        if not future.done():
            future.set_exception(
                RuntimeError(f"Authorization denied by provider: {error}")
            )
        return

    code = (params.get("code") or [None])[0]
    state = (params.get("state") or [None])[0]

    if code is None:
        # Log but don't resolve `future` — could be a browser preflight
        # or a stale tab firing `/callback` without finishing auth.
        # The wall-clock timeout in `wait_for_code` keeps the flow
        # from hanging when no real callback ever arrives.
        logger.debug("OAuth callback hit %s with no `code` param", parsed.path)
        await _send_html(
            writer,
            status=400,
            body=oauth_error_html("Missing code parameter."),
        )
        return

    if expected_state is not None and state != expected_state:
        # Likely a stale browser tab from a previous login attempt — its
        # `state` won't match the current PKCE flow. Log it so a CSRF
        # probe is at least visible, but don't resolve `future`: the
        # legitimate callback for the in-progress login may still be
        # coming. Wall-clock timeout in `wait_for_code` is the safety
        # net.
        logger.debug(
            "OAuth callback state mismatch on %s (stale tab or CSRF probe; ignoring)",
            parsed.path,
        )
        await _send_html(
            writer,
            status=400,
            body=oauth_error_html("State mismatch."),
        )
        return

    extras = {
        k: v[0]
        for k, v in params.items()
        if k not in {"code", "state", "error", "error_description"} and v
    }

    await _send_html(
        writer,
        status=200,
        body=oauth_success_html(success_message),
    )
    if not future.done():
        future.set_result(CallbackResult(code=code, state=state, extras=extras))


async def _read_line(reader: asyncio.StreamReader, limit: int) -> str:
    """Read one CRLF-terminated line, returning the decoded text without CRLF."""
    try:
        raw = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        raw = exc.partial
    except asyncio.LimitOverrunError:
        # The browser sent something pathologically long — just drop it.
        return ""
    if len(raw) > limit:
        return ""
    return raw.rstrip(b"\r\n").decode("latin-1", errors="replace")


async def _send_html(
    writer: asyncio.StreamWriter,
    *,
    status: int,
    body: str,
) -> None:
    """Send an HTML response and close the connection."""
    encoded = body.encode("utf-8")
    response = (
        f"HTTP/1.1 {status} {_status_text(status)}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(encoded)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + encoded
    writer.write(response)
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        # The browser may have already moved on; nothing we can do.
        return
    finally:
        with _suppress_close_errors():
            writer.close()


async def _send_error(
    writer: asyncio.StreamWriter,
    *,
    status: int,
    message: str,
) -> None:
    """Send a plain-text error response."""
    encoded = message.encode("utf-8")
    response = (
        f"HTTP/1.1 {status} {_status_text(status)}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(encoded)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + encoded
    writer.write(response)
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        return
    finally:
        with _suppress_close_errors():
            writer.close()


def _status_text(status: int) -> str:
    return {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }.get(status, "OK")


class _suppress_close_errors:  # noqa: N801 - context-manager naming
    """Silence transport errors from `writer.close()` paths."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return exc_type is not None and issubclass(exc_type, (ConnectionError, OSError))
