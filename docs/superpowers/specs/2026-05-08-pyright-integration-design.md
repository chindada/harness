# Pyright Integration & Type-Error Cleanup — Design Spec

**Date:** 2026-05-08
**Status:** Approved (brainstorm complete; awaiting implementation plan)

## 1. Goal

Add `pyright` as a dev tool for the harness-mcp project, fix the broken Codex SDK dependency that's been hidden by `try/except ImportError` blocks, and resolve the type errors pyright surfaces. No CI gate — developers run `uv run pyright` ad hoc.

Mode: **basic** (the default). Strict mode is rejected because the SDK-boundary code uses `Any` deliberately for duck-typed objects from `claude_agent_sdk` and `codex_app_server`; strict mode would fight that pattern with hundreds of `reportUnknownMemberType` findings.

The Codex dep fix is bundled into this spec because it's the upstream cause of half the type errors we're trying to address — splitting them apart would make both specs harder to read.

## 2. Pyright configuration

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "ruff>=0.6",
  "pyright>=1.1.380",
]

[tool.pyright]
include = ["src", "tests"]
venvPath = "."
venv = ".venv"
pythonVersion = "3.12"
typeCheckingMode = "basic"
```

The PyPI `pyright` package wraps the npm install so `uv sync` makes `uv run pyright` reproducible across machines without a global Node toolchain. `venvPath`/`venv` is what tells pyright where to find the project's installed deps; without it, pyright reports ~30 spurious `reportMissingImports` errors for `anyio`, `mcp`, `pytest`, etc. `pythonVersion=3.12` matches `requires-python` in `[project]`.

**Run via:** `uv run pyright`. Output expected at task completion: `0 errors, 0 warnings`.

## 3. Fix: replace the wrong Codex SDK dependency

### 3.1 The problem

`pyproject.toml` currently declares:

```toml
"codex-app-server-sdk>=0.3.2",
```

That installs the module `codex_app_server_sdk` — a third-party SDK by `emsi` whose API is `CodexClient.connect_stdio()` / `chat_once()` / etc. **It does not match the codebase**, which imports `from codex_app_server import AsyncCodex, AppServerConfig, TextInput`.

The codebase compiles only because every `from codex_app_server import …` is wrapped in a `try/except ImportError`. The `except` branch fires (because `codex_app_server` does not exist), the names are bound to `None`, and the test suite never notices because all 225 tests monkeypatch the `None`s with stubs. The production runtime would fail on first SDK call.

The spec at `2026-05-07-harness-mcp-design.md:12` explicitly names `codex_app_server` (the OpenAI official SDK) as the intended dep — confirmed via context7 against the OpenAI Codex docs. The intended SDK exports exactly the names the codebase imports.

### 3.2 The fix

OpenAI's `codex_app_server` SDK is **not on PyPI**. Its source pyproject.toml at `github.com/openai/codex/sdk/python/pyproject.toml` declares the project name as `openai-codex-app-server-sdk` (currently `0.116.0a1`, alpha). Install it from GitHub via PEP 508 VCS dependency:

```toml
# in pyproject.toml [project] dependencies, replacing codex-app-server-sdk:
"openai-codex-app-server-sdk @ git+https://github.com/openai/codex.git@main#subdirectory=sdk/python",
```

Verified: `pip install` from this URL succeeds, and `from codex_app_server import AsyncCodex, AppServerConfig, TextInput` resolves to real classes (`codex_app_server.api.AsyncCodex`, `codex_app_server.client.AppServerConfig`, `codex_app_server._inputs.TextInput`). The `AsyncCodex.__init__` signature is `(self, config: AppServerConfig | None = None)` — matches the codebase's `AsyncCodex(config=cfg)` calls. `AppServerConfig` accepts every kwarg the codebase passes (`codex_bin`, `cwd`, `config_overrides`, `client_name`, `client_title`, `client_version`).

### 3.3 Tradeoffs / risks

- **Alpha-version dependency.** The SDK is at `0.116.0a1`. The harness pins `@main`, which means re-running `uv lock` could pick up breaking changes in the SDK API. Mitigation: pin a specific commit SHA after the swap once we know it works (`@<sha>` instead of `@main`).
- **Network-dependent install.** First-time installs require GitHub access during `uv sync`. Acceptable for a development tool; matches how the OpenAI docs themselves recommend installing.
- **No PyPI alternative.** If/when OpenAI publishes the SDK to PyPI, the dep should switch to a regular version constraint.

### 3.4 Tests

Re-run the suite after the swap. Tests should still pass — they monkeypatch the imports either way, and the names resolve correctly now. If a test breaks, that's likely an SDK-shape drift to address inline.

## 4. Refactor: remove lazy SDK imports

### 4.1 Why this refactor

Five modules currently use this pattern (example from `sprints.py`):

```python
# Lazy SDK imports so unit tests can monkeypatch.
try:
    from codex_app_server import AppServerConfig, AsyncCodex, TextInput
except ImportError:  # pragma: no cover
    AppServerConfig = AsyncCodex = TextInput = None
try:
    from claude_agent_sdk import query
except ImportError:
    query = None
```

After §3 lands, both SDKs are reliably importable. The `try/except` is dead defense:

- `claude-agent-sdk` is on PyPI and installed by `uv sync`.
- `openai-codex-app-server-sdk` is installed by `uv sync` from the new git+ dep.
- The `monkeypatch.setattr(module, "AsyncCodex", fake)` pattern in tests works against unconditional imports identically — `monkeypatch` rebinds the attribute on the module object regardless of how it got there.
- Pyright sees `AsyncCodex: type[AsyncCodex] | None` and flags 8 call-site `reportOptionalCall` errors (1 each in `generator.py`, `planning.py`, `summarizer.py`; 3 in `prereqs.py`; 2 in `sprints.py`). Resolving them via `assert is not None` boilerplate would scatter dead-feeling code across the modules.

### 4.2 What changes

Files: `generator.py`, `sprints.py`, `planning.py`, `summarizer.py`, `prereqs.py`.

Replace each block with a normal top-level import:

```python
from codex_app_server import AppServerConfig, AsyncCodex, TextInput
from claude_agent_sdk import query
```

Drop the lazy comment and any `# pragma: no cover` / `# type: ignore[import-untyped]` / `# noqa: PLC0415` markers attached to the old block.

### 4.3 Tradeoff

After the refactor, importing any of those modules raises `ImportError` at module-load time if the SDK is missing, instead of silently `None`-ing out and erroring later when `AsyncCodex(config=cfg)` is called. This is fail-fast and louder, which is the correct posture — but it means a partial install can no longer load test modules that import these names. Acceptable: anyone running `uv sync` gets a complete environment.

### 4.4 Tests

No test changes required. Existing `monkeypatch.setattr(gen_mod, "AsyncCodex", fake_async_codex)` calls continue to work.

## 5. Fix: MCP SDK type-signature mismatches

### 5.1 Where

`server.py` (4 options factories, 8 errors total) and `evaluator_runner.py` (1 error):

- `_make_planner_options_factory`, `_make_reviewer_options_factory`, `_make_evaluator_options_factory`, `_make_summarizer_options_factory` — each constructs a `ClaudeAgentOptions(...)`. The `setting_sources` and `mcp_servers` arguments are flagged because:
  - `setting_sources` expects `list[SettingSource] | None` (a Literal-typed list); the code passes `list[str]` from `prereqs_result.setting_sources`.
  - `mcp_servers` expects `dict[str, McpServerConfig] | str | Path` (a TypedDict map); the code passes `dict[str, dict[str, Any]]` built from `mcp_capture.py`'s output.
- `evaluator_runner._run` — same `mcp_servers` type mismatch when the launcher subprocess builds its own `ClaudeAgentOptions`.

### 5.2 Fix

Cast at the boundary inside each factory and inside `evaluator_runner._run`:

```python
from typing import cast, Any

# inside _factory:
return ClaudeAgentOptions(
    system_prompt=_resolved_prompt_text("planner.md"),
    cwd=str(_kw.get("job_dir", job_dir)),
    setting_sources=cast(Any, prereqs_result.setting_sources),
    mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
    extra_args={"strict-mcp-config": None},
    permission_mode="acceptEdits",
)
```

We use `cast(Any, ...)` rather than `cast(dict[str, McpServerConfig], ...)` because the SDK's `McpServerConfig` type is a private re-export that's awkward to import cleanly, and the runtime shape IS compatible (it's literally what `claude.json` produces). The cast is a localized "trust me" at the boundary — no behavioral change.

Alternative considered: tighten `PrereqsResult.captured_mcp` and `PrereqsResult.setting_sources` to use SDK types directly. **Rejected** because it pulls SDK type names into a module (`prereqs.py`) we want to keep neutral about which agent backend consumes the values.

## 6. Fix: anyio attribute access

### 6.1 Where

14 errors across 6 files, all of the same shape:

- `state.py:179, 204` — `await anyio.to_thread.run_sync(...)`
- `evaluator.py:148, 150, 153, 245` — `await anyio.to_thread.run_sync(...)`
- `evaluator_runner.py:80, 82` — `await anyio.to_thread.run_sync(...)`
- `generator.py:288` — `await anyio.to_thread.run_sync(...)`
- `logging_setup.py:110, 115, 120, 127` — `await anyio.to_thread.run_sync(...)`
- `server.py:192` — `async with anyio.create_task_group() as tg:`

Pyright flags these as `reportAttributeAccessIssue`: `"Cannot access attribute 'run_sync' for class 'type[BrokenWorkerInterpreter]'"`. This is a known friction with anyio's union-typed module exports — pyright resolves `anyio.to_thread` to the wrong type and loses the `run_sync` attribute.

### 6.2 Fix

Narrow the imports at each module's top:

```python
# state.py top
from anyio import to_thread
...
await to_thread.run_sync(_exec_commit, stmt, params)
```

```python
# server.py top
from anyio import create_task_group
...
async with create_task_group() as tg:
```

`anyio.to_thread` is itself a real submodule (`anyio/to_thread.py`) and `from anyio import to_thread` imports the submodule directly, bypassing the union-typed re-export that confuses pyright. Same for `create_task_group`.

## 7. Fix: `__main__.py` host/port — real runtime bug

### 7.1 Where

`__main__.py:26` calls `server.run_streamable_http_async(host=args.host, port=args.port)` — but `FastMCP.run_streamable_http_async()` takes **zero parameters**. Verified against the installed mcp v1.27 SDK (`inspect.signature(FastMCP.run_streamable_http_async)` returns `(self) -> None`). This is a real bug, not a pyright artifact: anyone running `harness-mcp serve --transport streamable-http --host 1.2.3.4 --port 9000` would crash on the unexpected kwargs.

The correct API: `host`/`port` are constructor params on `FastMCP(...)` (defaults `127.0.0.1` / `8000`), and the run method reads them off `server.settings`.

### 7.2 Fix

Mutate the existing FastMCP instance's settings before invoking the async runner. The instance is created at `server.py` module-import time as `server = FastMCP("harness-mcp", lifespan=lifespan)`; we can reach in and update `server.settings.host` / `server.settings.port` from the CLI handler:

```python
# __main__.py:_run_serve, streamable-http branch
elif transport == "streamable-http":
    from harness_mcp.server import server  # already imported above
    server.settings.host = args.host
    server.settings.port = args.port
    anyio.run(server.run_streamable_http_async)
```

This is the clean version of the change: removes the bogus kwargs (resolves both pyright errors), routes through the real config path (resolves the runtime crash), and stays a tiny edit. No new abstractions.

Add a smoke test or doctor-time assertion if appropriate, but no new test required — the existing CLI is integration-tested via `tests/smoke.py`.

## 8. Fix: test-side type issues

### 8.1 `test_state.py:27,30` — fixture annotation

```python
@pytest.fixture
async def db(tmp_harness_home: Path) -> AsyncIterator[Path]:    # not Path
    close_db()
    init_db()
    yield tmp_harness_home / "state.db"
    close_db()
```

Currently annotated `-> Path`; the function uses `yield`, so the actual return type is `AsyncIterator[Path]`. Same one-character fix wherever the `db` fixture appears (it's already correct in `test_orchestrator.py`).

### 8.2 `test_server.py:41,42` — None subscript

```python
def test_known_errors_map_to_codes(self, exc, expected_code):
    result = _to_call_tool_error(exc)
    assert result.isError is True
    assert result.structuredContent is not None    # NEW
    assert result.structuredContent["code"] == expected_code
    assert isinstance(result.structuredContent["message"], str)
```

`CallToolResult.structuredContent` is typed `dict | None`. The assert narrows it for both subscript reads.

### 8.3 `test_generator.py:878` — float-as-int

The Round 1 wall-clock reset trigger test passes `codex_reset_minutes=0.01` to make the fractional threshold (0.6s) reachable from the fake monotonic clock. `JobOptions.codex_reset_minutes` is typed `int`.

**Fix:** keep `codex_reset_minutes=1` (the smallest valid int) and have the fake clock advance ~31 seconds per call. With two `__anext__` calls per chunk:
- After the call where `chunk_started` is captured (now at fake `t = 1031.0` say), and one event yields with the clock at `1062.0`, delta = 31s — below the 60s threshold (1 minute = 60 seconds).
- Second event: clock = `1093.0`, delta = 62s — past 60s threshold, branch fires.

Test still asserts 2 yields per chunk, same as before. Adjust the comment in the test to explain the new clock cadence.

## 9. Order of operations

The plan should land in this order so each step's tests pass independently. Error counts add up to 42 (the pyright baseline observed at design time):

1. **Add config + dev dep + swap Codex dep.** Update `pyproject.toml` with `[tool.pyright]`, the `pyright` dev dep, and the `openai-codex-app-server-sdk @ git+...` replacement (in place of `codex-app-server-sdk`). Run `uv sync`, then `uv run pytest tests/` to confirm nothing regresses (still 225 passed). Run `uv run pyright` to capture the baseline. **Drops 4 errors** (the `Import "codex_app_server" could not be resolved` cluster — 1 in `generator.py:25`, 3 in `prereqs.py:38,41,44`).
2. **Refactor lazy imports** (§4). **Drops 8 errors** (`reportOptionalCall` from the `try/except ImportError` Nones).
3. **Cast at MCP-options boundary** (§5). **Drops 9 errors** (8 in `server.py` factories, 1 in `evaluator_runner.py:91`).
4. **Narrow anyio imports** (§6). **Drops 14 errors**.
5. **Fix `__main__.py` host/port** (§7). **Drops 2 errors** AND a runtime bug.
6. **Test-side fixes** (§8). **Drops 5 errors** (1 float-as-int, 2 None subscript, 2 fixture annotation).
7. **Verify:** `uv run pytest tests/` (still 225 passed), `uv run ruff check .` (clean), `uv run pyright` (0 errors, 0 warnings).

Total: 4 + 8 + 9 + 14 + 2 + 5 = 42. Any single step can be rolled back independently if regressions surface.

## 10. Out of scope

- Strict mode adoption.
- CI gating (`pyright` is not added to README's CI smoke line).
- Replacing `Any` annotations on duck-typed SDK parameters (those carry `# noqa: ANN401` for a reason).
- Stub generation for `claude_agent_sdk` / `codex_app_server`. The SDKs ship without stubs and pyright's inference covers what we need at basic mode.
- Pre-commit hook recommendations.
- Pinning the Codex SDK to a specific commit SHA (left for a follow-up after we know `@main` is stable in our usage).

## 11. Notable decisions

1. **Basic mode over strict.** SDK-facing code uses `Any` deliberately; strict mode adds friction without catching real bugs.
2. **Refactor lazy imports rather than `assert`.** Six call sites would get six `assert is not None` lines; one upstream change deletes all of them. **Prerequisite:** §3 must land first or the refactor fails outright.
3. **`cast(Any, ...)` at the MCP boundary, not stricter typing.** Keeps `PrereqsResult` neutral about which SDK consumes its values.
4. **No CI gate.** Matches user preference for ad hoc enforcement; ruff and pytest remain the hard gates.
5. **VCS dep over PyPI.** OpenAI's `codex_app_server` SDK is not published. The `git+` install is the documented path per the OpenAI Codex docs themselves; this is not an unusual choice for an SDK-in-development.
6. **Dep fix bundled, not split.** The dep error is the upstream cause of the lazy-import pattern and several pyright findings; addressing them in one spec keeps the change set coherent.
