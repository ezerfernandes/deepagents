# CLI Development Guide

## Live CSS development with Textual devtools

Textual's devtools console enables CSS hot-reload and live `self.log()` output during development.

### Prerequisites

Sync the `test` dependency group (includes `textual-dev`):

```bash
cd libs/cli && uv sync --group test
```

Create the dev wrapper script (one-time):

```bash
cat > /tmp/dev_deepagents.py << 'PYEOF'
"""Dev wrapper to run Deep Agents CLI with textual devtools."""
import sys
sys.argv = ["deepagents"] + sys.argv[1:]

from deepagents_cli.main import cli_main
cli_main()
PYEOF
```

### Running

**Terminal 1** — devtools console:

```bash
cd libs/cli && uv run --group test textual console
```

**Terminal 2** — CLI with live reload:

```bash
cd libs/cli && uv run --group test textual run --dev /tmp/dev_deepagents.py
```

Edit any `.tcss` file and save — changes appear immediately. Any `self.log()` calls in widget code show in the console.

### Console options

- `textual console -v` — verbose mode, shows all events (key presses, mouse, etc.)
- `textual console -x EVENT` — exclude noisy event groups
- `textual console --port 7342` — custom port (pass matching `--port` to `textual run`)

### Why the wrapper script?

`textual run --dev` handles the devtools connection, but it needs to run inside the project's virtualenv to import `deepagents_cli`. The wrapper script bridges the gap — `uv run --group test textual run --dev` ensures both `textual-dev` (from the `test` group) and `deepagents_cli` are available in the same environment.

## Debugging

The CLI runs a `langgraph dev` subprocess for every interactive session. When the subprocess crashes during startup, the TUI shows a one-line failure banner; the actual exception lives in the subprocess's stdout/stderr, which is captured to a temp file.

### Environment variables

| Variable | Effect |
| --- | --- |
| `DEEPAGENTS_CLI_DEBUG=1` | Preserves the server subprocess log on shutdown and prints its path to stderr. Without this, the log is deleted when the process stops. Also enables the CLI-process file handler below. Accepts `1`/`true`/`yes`/`on` (case-insensitive) as enabled; `0`/`false`/`no`/`off`/empty/unset as disabled. |
| `DEEPAGENTS_CLI_DEBUG_FILE=<path>` | Overrides the default path (`/tmp/deepagents_debug.log`) for the CLI-process file handler, which attaches at `DEBUG` level to `textual_adapter` and `remote_client`. **Only takes effect when `DEEPAGENTS_CLI_DEBUG` is truthy.** Useful for diagnosing streaming/client-side issues; does **not** capture the server subprocess. |
| `DEEPAGENTS_CLI_OAUTH_CALLBACK_HOST=<host>` | Bind address for the local OAuth callback server used by `deepagents login {anthropic,openai-codex}`. Default `localhost` (resolved via `getaddrinfo` so dual-stack browsers reach the listener). Override to e.g. `0.0.0.0` when the browser runs on a different machine and the callback comes through a tunnel. The IdP-registered redirect URIs always remain `http://localhost:{53692,1455}/<path>`. |

`DEEPAGENTS_CLI_DEBUG` is what you want for startup crashes (graph init, MCP config, sandbox): the preserved subprocess log contains the real traceback. The optional `DEEPAGENTS_CLI_DEBUG_FILE` override is for post-startup client-side debugging.

### Finding the server subprocess log

On macOS, `tempfile` resolves to `$TMPDIR` (a path under `/var/folders/.../T/`). Each `ServerProcess` writes its combined stdout+stderr to a file matching `deepagents_server_log_*.txt`:

```bash
# Newest first
ls -lt ${TMPDIR:-/tmp}/deepagents_server_log_*.txt | head -5

# Tail the latest while reproducing the crash
tail -F "$(ls -t ${TMPDIR:-/tmp}/deepagents_server_log_*.txt | head -1)"
```

The interesting line is `Failed to initialize server graph: <exc>` followed by a traceback — everything above that is uvicorn/lifespan unwinding.

### Triage flow for a startup crash

1. **Rerun with `DEEPAGENTS_CLI_DEBUG=1`.** The log is preserved and a "Server log preserved at: ..." line is printed to stderr. Textual's fullscreen mode can hide that line, but the file itself is still on disk.
2. **Locate the log** via the `ls` command above. Open it in your editor.
3. **Search for `Failed to initialize server graph`.** The stack trace beneath names the concrete failure point (MCP config validation, sandbox init, model resolution, subagent load, etc.).
4. **Pre-flight validators run in the CLI process** for the common failure modes (e.g., `--mcp-config` is validated in `start_server_and_get_agent` before the subprocess spawns). When the banner shows `MCPConfigError: <path>: <reason>`, the subprocess never started — fix the file and retry.

### Common startup failure patterns

- **`MCPConfigError: Invalid MCP config at <path>: ...`** — malformed `--mcp-config`. The pre-flight wraps the underlying `ValueError`/`TypeError` with the offending path. See `_preflight_validate_mcp_config` in `server_manager.py`.
- **`Server 'X' missing required 'command' field`** (from a discovered project `.mcp.json`, not `--mcp-config`) — an stdio server config without `command`. For remote servers, just use `{"url": "..."}`; transport is inferred as `http` when no `type`/`transport` is present.
- **Uncaught exception inside a bare `sys.exit(1)`** — usually means the surrounding `make_graph()` raised. Look one traceback up in the subprocess log for the real cause.

## Subscription OAuth

The `deepagents_cli/oauth/` package implements login + refresh flows
for Anthropic Pro/Max, GitHub Copilot, and ChatGPT Plus/Pro Codex.
Layout:

| Module | Purpose |
| --- | --- |
| `oauth/types.py` | `OAuthCredentials` dataclass, `OAuthProvider` Protocol, error hierarchy (`OAuthError`, `OAuthRefreshError`, `OAuthStateMismatchError`, `OAuthCancelledError`) |
| `oauth/pkce.py` | RFC 7636 S256 verifier + challenge generator |
| `oauth/callback_server.py` | Async stdlib HTTP server catching the OAuth redirect; `try_start_callback_server` returns `None` instead of raising on port-in-use |
| `oauth/storage.py` | Per-provider JSON files at `~/.deepagents/.state/oauth-tokens/<provider>.json`, atomic `tmp+rename`, `fcntl.flock` sidecar lock; `lock_provider` (sync) and `alock_provider` (async) context managers |
| `oauth/registry.py` | Built-in provider registry; `register_provider` / `unregister_provider` for tests and custom integrations |
| `oauth/_api.py` | Public API: `login`, `refresh_credentials`, `get_access_token`, `get_stored_credentials`, `delete_credentials`. Refresh paths re-read on-disk credentials inside the lock so concurrent processes don't clobber rotated refresh tokens. |
| `oauth/_kwargs.py` | Translates stored credentials into LangChain ChatModel kwargs (`api_key`, `betas`, `default_headers`, `base_url`) for `_get_provider_kwargs` |
| `oauth/providers/{anthropic,github_copilot,openai_codex}.py` | Per-provider login + refresh implementations |
| `oauth_commands.py` | CLI subcommands `deepagents login [provider]`, `deepagents logout [provider]`, `deepagents auth list` |
| `_oauth_middleware.py` | Per-request middlewares: `AnthropicOAuthIdentityMiddleware` (system prompt prefix + token rotation), `GitHubCopilotHeadersMiddleware` (dynamic per-call headers), `OpenAICodexOAuthMiddleware` (account-id rotation) |

### Running tests

OAuth unit tests live in `tests/unit_tests/oauth/`. Tests that bind
real TCP listeners (the callback server tests) use
`pytest.mark.enable_socket` to opt out of the project-wide
`pytest-socket` block. The httpx-mocking tests use `respx`. Run the
suite under the same flags the Makefile uses:

```bash
cd libs/cli
uv run --group test pytest -n auto --benchmark-disable --disable-socket --allow-unix-socket tests/unit_tests/oauth/
```

Regression tests for past bug rounds live in
`tests/unit_tests/oauth/test_oauth_bug_fixes.py` (rounds 1+2) and
`tests/unit_tests/oauth/test_oauth_bugs3_fixes.py` (round 3 + GHE
URL fix). Each `TestBugN` class pins exactly one bug from the
matching `bugs*.md` entry — keep them focused.

### Rotating client identifiers

Anthropic and GitHub Copilot expose obfuscated `client_id` strings
(base64-encoded constants in `oauth/providers/{anthropic,github_copilot}.py`).
The `claudeCodeVersion` constant
(`CLAUDE_CODE_USER_AGENT_VERSION`) and the Copilot `User-Agent` /
`Editor-Version` headers should be bumped in lockstep with pi-mono
to keep the IdP-side fingerprints recognised. The OpenAI Codex
`originator` is `deepagents` (intentionally distinct from
pi-mono's `pi`).
