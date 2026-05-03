"""CLI commands for `deepagents login`, `deepagents logout`, `deepagents auth`.

These wrap the async public API in `deepagents_cli.oauth` for headless
shell use. The TUI surfaces the same flows via a `/login` slash command
(`deepagents_cli/_login_dialog.py`).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import webbrowser
from typing import TYPE_CHECKING, Any

from deepagents_cli.oauth import (
    OAuthAuthInfo,
    OAuthCallbacks,
    OAuthError,
    OAuthPrompt,
    delete_credentials,
    get_provider,
    get_stored_credentials,
    list_logged_in_providers,
    list_providers,
    login,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _lazy_ui_help(fn_name: str) -> Callable[[], None]:
    """Lazy-import and call a help function on `deepagents_cli.ui`."""

    def _show() -> None:
        from deepagents_cli import ui

        getattr(ui, fn_name)()

    return _show


def setup_oauth_parsers(
    subparsers: Any,  # noqa: ANN401
    *,
    make_help_action: Callable[[Callable[[], None]], type[argparse.Action]],
) -> None:
    """Register `login`, `logout`, and `auth` top-level command groups.

    Args:
        subparsers: Top-level argparse subparsers object.
        make_help_action: Help-action factory from `main.parse_args`.
    """
    login_parser = subparsers.add_parser(
        "login",
        help="Sign in to a subscription OAuth provider",
        add_help=False,
    )
    login_parser.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider id (anthropic, github-copilot, openai-codex)",
    )
    login_parser.add_argument(
        "-h",
        "--help",
        action=make_help_action(_lazy_ui_help("show_login_help")),
    )

    logout_parser = subparsers.add_parser(
        "logout",
        help="Forget stored OAuth credentials",
        add_help=False,
    )
    logout_parser.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider id; omit to log out of every provider",
    )
    logout_parser.add_argument(
        "-h",
        "--help",
        action=make_help_action(_lazy_ui_help("show_logout_help")),
    )

    auth_parser = subparsers.add_parser(
        "auth",
        help="Inspect OAuth credentials",
        add_help=False,
    )
    auth_parser.add_argument(
        "-h",
        "--help",
        action=make_help_action(_lazy_ui_help("show_auth_help")),
    )
    auth_sub = auth_parser.add_subparsers(dest="auth_command")
    auth_list = auth_sub.add_parser(
        "list",
        aliases=["ls"],
        help="Show login status for each provider",
        add_help=False,
    )
    auth_list.add_argument(
        "-h",
        "--help",
        action=make_help_action(_lazy_ui_help("show_auth_list_help")),
    )


def _print(line: str = "") -> None:
    """Plain-stdout print used by stdio OAuth callbacks."""
    print(line)  # noqa: T201 - intentional user-facing output


def _stdio_callbacks() -> OAuthCallbacks:
    """Return `OAuthCallbacks` that drive the flow over stdin/stdout.

    Mirrors `pi-mono/packages/ai/src/cli.ts` — prints the URL, opens the
    user's default browser best-effort, and reads pasted codes/URLs from
    `input()` on a worker thread (so we don't block the asyncio loop).
    """

    def on_auth(info: OAuthAuthInfo) -> None:
        _print()
        _print("Open this URL in your browser:")
        _print(f"  {info.url}")
        if info.instructions:
            _print(info.instructions)
        _print()
        try:
            webbrowser.open(info.url, new=1, autoraise=True)
        except (webbrowser.Error, OSError):
            # Best-effort: if no browser is wired up we still printed
            # the URL above so the user can copy it manually.
            logger.debug("webbrowser.open failed", exc_info=True)

    async def on_prompt(prompt: OAuthPrompt) -> str:
        suffix = f" [{prompt.placeholder}]" if prompt.placeholder else ""
        message = f"{prompt.message}{suffix}: "
        try:
            return await asyncio.to_thread(input, message)
        except EOFError as exc:
            msg = "stdin closed before authorization input was received."
            raise OAuthError(msg) from exc

    def on_progress(message: str) -> None:
        _print(message)

    return OAuthCallbacks(
        on_auth=on_auth,
        on_prompt=on_prompt,
        on_progress=on_progress,
        on_manual_code_input=None,
    )


def _resolve_provider_arg(provider: str | None) -> str:
    """Return a known provider id; prompt the user when *provider* is `None`.

    Mirrors the interactive provider picker in `pi-mono/packages/ai/src/
    cli.ts:96-112`.

    Raises:
        SystemExit: If no provider is selected (e.g. user enters an
            invalid choice).
    """
    providers = list_providers()
    if provider:
        if any(p.id == provider for p in providers):
            return provider
        _print(f"Unknown provider: {provider}")
        _print("Available providers:")
        for p in providers:
            _print(f"  {p.id.ljust(20)} {p.name}")
        sys.exit(1)

    _print("Select a provider:")
    _print()
    for i, p in enumerate(providers, start=1):
        _print(f"  {i}. {p.name} ({p.id})")
    _print()
    raw = input(f"Enter number (1-{len(providers)}): ").strip()
    try:
        index = int(raw) - 1
    except ValueError:
        _print(f"Invalid selection: {raw!r}")
        sys.exit(1)
    if not 0 <= index < len(providers):
        _print(f"Invalid selection: {raw!r}")
        sys.exit(1)
    return providers[index].id


async def run_login(provider: str | None) -> int:
    """Handle `deepagents login [provider]`.

    Returns:
        Process exit code: 0 on success, 1 on user/login failure.
    """
    provider_id = _resolve_provider_arg(provider)
    spec = get_provider(provider_id)
    assert spec is not None  # _resolve_provider_arg only returns known ids
    _print(f"Logging in to {spec.name}...")
    try:
        credentials = await login(provider_id, _stdio_callbacks())
    except OAuthError as exc:
        _print(f"Login failed: {exc}")
        return 1
    except KeyboardInterrupt:
        _print("Login cancelled.")
        return 1

    _print(f"Logged in to {spec.name}.")
    expires_in = max(0, int(credentials.expires - time.time()))
    _print(f"Token expires in {_humanize_seconds(expires_in)}.")
    return 0


async def run_logout(provider: str | None) -> int:
    """Handle `deepagents logout [provider]`."""
    if provider is None:
        ids = list_logged_in_providers()
        if not ids:
            _print("No stored OAuth credentials.")
            return 0
        for pid in ids:
            delete_credentials(pid)
            _print(f"Logged out of {pid}.")
        return 0

    if delete_credentials(provider):
        _print(f"Logged out of {provider}.")
        return 0
    _print(f"No stored credentials for {provider}.")
    return 0


def run_auth_list() -> int:
    """Handle `deepagents auth list`.

    A single corrupt or version-mismatched token file should not turn
    the entire listing into a traceback — `get_stored_credentials`
    raises `RuntimeError` for those cases (the message tells the user
    how to recover), and we report that per-row instead of letting it
    bubble up.
    """
    providers = list_providers()
    rows: list[tuple[str, str, str]] = []
    for spec in providers:
        try:
            creds = get_stored_credentials(spec.id)
        except RuntimeError as exc:
            logger.debug("Corrupt token file for %s: %s", spec.id, exc)
            rows.append(
                (
                    spec.id,
                    spec.name,
                    f"corrupt (re-run `deepagents login {spec.id}`)",
                )
            )
            continue
        if creds is None:
            rows.append((spec.id, spec.name, "not logged in"))
            continue
        remaining = creds.expires - time.time()
        status = (
            f"expires in {_humanize_seconds(int(remaining))}"
            if remaining > 0
            else "expired"
        )
        rows.append((spec.id, spec.name, status))

    width_id = max(len(r[0]) for r in rows)
    width_name = max(len(r[1]) for r in rows)
    for pid, name, status in rows:
        _print(f"  {pid.ljust(width_id)}  {name.ljust(width_name)}  {status}")
    return 0


def _humanize_seconds(seconds: int) -> str:
    """Render *seconds* as a coarse human-readable duration.

    Uses days/hours/minutes only — sub-minute precision adds noise to
    `auth list` output without helping the user.
    """
    if seconds <= 0:
        return "0s"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if not parts:
        return "<1m"
    return " ".join(parts)
