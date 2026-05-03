"""Shared fixtures for OAuth unit tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect OAuth token storage into `tmp_path/.state`.

    Mirrors the `fake_home` fixture in `tests/unit_tests/test_mcp_auth.py`.
    `oauth.storage._tokens_dir` resolves `DEFAULT_STATE_DIR` lazily, so
    patching the module attribute is enough — no need to also patch
    `Path.home()`.
    """
    state = tmp_path / ".state"
    monkeypatch.setattr("deepagents_cli.model_config.DEFAULT_STATE_DIR", state)
    return state


@pytest.fixture(autouse=True)
def reset_oauth_registry() -> None:
    """Restore the built-in OAuth provider registry between tests.

    Ensures tests that call `register_provider` or `unregister_provider`
    don't leak state into unrelated tests.
    """
    from deepagents_cli.oauth import registry

    registry.reset_providers()
    yield
    registry.reset_providers()
