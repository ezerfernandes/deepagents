"""Shared helpers for OAuth provider implementations.

These functions live outside individual provider modules so the
authorization-code parsing and HTTP-error formatting are identical
across providers — exactly the way pi-mono shares logic between
`anthropic.ts` and `openai-codex.ts`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

REFRESH_SAFETY_MARGIN_SECONDS = 5 * 60
"""Subtract from token lifetimes so we never spend the last 5 min on a long
streaming request that would die mid-flight when the token expires."""

DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
"""Per-request timeout for OAuth HTTP calls. pi-mono uses 30s
(`AbortSignal.timeout(30_000)`); we mirror that."""


@dataclass(frozen=True, slots=True)
class ParsedAuthorizationInput:
    """Result of `parse_authorization_input`.

    Both fields may be `None` when the user pasted something we can't
    parse (e.g. an empty string).
    """

    code: str | None
    state: str | None


def parse_authorization_input(value: str) -> ParsedAuthorizationInput:
    """Parse a callback URL, `code#state` string, or bare code.

    Ports `parseAuthorizationInput` from `pi-mono/packages/ai/src/utils/
    oauth/{anthropic,openai-codex}.ts`. Accepts:

    - A full URL with `?code=&state=...`.
    - `code#state`.
    - `code=...&state=...` (raw query string).
    - A bare code with no state.

    Empty input returns `(None, None)`.
    """
    stripped = value.strip()
    if not stripped:
        return ParsedAuthorizationInput(code=None, state=None)

    # Full URL
    if "://" in stripped:
        parsed = urlsplit(stripped)
        params = parse_qs(parsed.query)
        return ParsedAuthorizationInput(
            code=(params.get("code") or [None])[0],
            state=(params.get("state") or [None])[0],
        )

    # `code#state` shorthand from the Anthropic flow
    if "#" in stripped:
        code, _, state = stripped.partition("#")
        return ParsedAuthorizationInput(code=code or None, state=state or None)

    # Raw query string. Strip a leading `?` if the user pasted the
    # query portion of a redirect URL — `parse_qs` would otherwise key
    # the first param under `?code` and we'd silently miss the code.
    if "code=" in stripped:
        query = stripped.lstrip("?")
        params = parse_qs(query)
        return ParsedAuthorizationInput(
            code=(params.get("code") or [None])[0],
            state=(params.get("state") or [None])[0],
        )

    # Bare code
    return ParsedAuthorizationInput(code=stripped, state=None)


def expires_from_lifetime(lifetime_seconds: float) -> float:
    """Return an absolute POSIX expiry, including the 5-minute safety margin."""
    return time.time() + float(lifetime_seconds) - REFRESH_SAFETY_MARGIN_SECONDS


async def post_json(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """POST a JSON body and return the decoded JSON response.

    Raises:
        OAuthHTTPError: On non-2xx response or invalid JSON. Includes
            URL and body snippets in the error message so failures land
            in user-visible logs with enough detail to diagnose.
    """
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(
                url, headers=request_headers, content=json.dumps(body)
            )
        except httpx.HTTPError as exc:
            msg = f"HTTP request failed. url={url}; details={exc!s}"
            raise OAuthHTTPError(msg) from exc

    return _decode(response, url=url)


async def post_form(
    url: str,
    body: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """POST an `application/x-www-form-urlencoded` body and return JSON.

    GitHub and OpenAI Codex's token endpoints both expect form bodies;
    Anthropic's takes JSON. The two helpers cover both shapes.
    """
    request_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(url, headers=request_headers, data=body)
        except httpx.HTTPError as exc:
            msg = f"HTTP request failed. url={url}; details={exc!s}"
            raise OAuthHTTPError(msg) from exc

    return _decode(response, url=url)


async def get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """GET a URL and return the decoded JSON response."""
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(url, headers=request_headers)
        except httpx.HTTPError as exc:
            msg = f"HTTP request failed. url={url}; details={exc!s}"
            raise OAuthHTTPError(msg) from exc

    return _decode(response, url=url)


def _decode(response: httpx.Response, *, url: str) -> dict[str, Any]:
    body = response.text
    if response.status_code >= 400:
        snippet = body[:500] if body else "<empty>"
        msg = (
            f"HTTP request failed. status={response.status_code}; "
            f"url={url}; body={snippet}"
        )
        raise OAuthHTTPError(msg)
    if not body:
        return {}
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        snippet = body[:500]
        msg = f"Response was not valid JSON. url={url}; body={snippet}; details={exc!s}"
        raise OAuthHTTPError(msg) from exc
    if not isinstance(decoded, dict):
        snippet = body[:500]
        msg = f"Response JSON was not an object. url={url}; body={snippet}"
        raise OAuthHTTPError(msg)
    return decoded


class OAuthHTTPError(RuntimeError):
    """Raised when an OAuth HTTP request fails or returns malformed data."""
