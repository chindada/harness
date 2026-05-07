# Pyright Integration & Type-Error Cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `pyright` (basic mode) as a dev tool, swap the broken Codex SDK dependency that was hidden by `try/except ImportError` blocks, and drive pyright errors from 42 to 0 without regressing the 225-test suite.

**Architecture:** Six sequential tasks, each rolled back-able independently, that descend the pyright error count: dep swap (drops 4) → lazy-import refactor (drops 8) → cast at MCP boundary (drops 9) → anyio import narrowing (drops 14) → real runtime bug in `__main__.py` (drops 2 + bug) → test-side type cleanups (drops 5).

**Tech Stack:** Python 3.12+, `uv` package manager, pyright via PyPI wrapper (`pyright>=1.1.380`), `mcp` v1.27, `claude_agent_sdk` v0.1.76, `openai-codex-app-server-sdk` (installed from `git+https://github.com/openai/codex.git@main#subdirectory=sdk/python`).

---

## File Structure

**Modify (production code):**
- `pyproject.toml` — add `[tool.pyright]`, dev-dep, swap Codex package
- `src/harness_mcp/sprints.py` — drop lazy SDK imports
- `src/harness_mcp/generator.py` — drop lazy SDK imports + narrow anyio
- `src/harness_mcp/planning.py` — drop lazy SDK imports
- `src/harness_mcp/summarizer.py` — drop lazy SDK imports
- `src/harness_mcp/prereqs.py` — drop lazy SDK imports
- `src/harness_mcp/server.py` — `cast(Any, ...)` in 4 options factories + narrow anyio
- `src/harness_mcp/evaluator_runner.py` — `cast(Any, ...)` for mcp_servers + narrow anyio
- `src/harness_mcp/state.py` — narrow anyio imports
- `src/harness_mcp/evaluator.py` — narrow anyio imports
- `src/harness_mcp/logging_setup.py` — narrow anyio imports
- `src/harness_mcp/__main__.py` — fix host/port runtime bug

**Modify (tests):**
- `tests/test_state.py` — fix `initialized_home` fixture annotation
- `tests/test_server.py` — narrow `structuredContent` before subscript
- `tests/test_generator.py` — replace `codex_reset_minutes=0.01` with int + larger clock advance

**Create:** None.

---

## Notes on test approach

This plan is a refactor + dep swap, not a feature addition. There are no new behaviors to test-drive. The "tests" each task runs are:
1. **`uv run pytest tests/`** — must remain at 225 passed (no regressions).
2. **`uv run pyright`** — error count must drop by the documented amount and never regress.
3. **`uv run ruff check .`** — must remain clean.

Each task ends with a commit so any single task can be reverted.

---

## Task 1: Add pyright config + dev dep + swap Codex SDK

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Replace the broken Codex dep, add pyright dev-dep, add `[tool.pyright]`**

Open `pyproject.toml`. Find the `dependencies` array (in `[project]`) and replace `"codex-app-server-sdk>=0.3.2",` with the OpenAI git URL. Find `[project.optional-dependencies]`'s `dev` array and add `pyright>=1.1.380`. Append a new `[tool.pyright]` block at the end.

The full `pyproject.toml` after the edit (relevant portions):

```toml
[project]
name = "harness-mcp"
version = "0.1.0"
description = "MCP server orchestrating multi-hour, multi-agent application builds from a design document."
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "harness-mcp authors" }]
dependencies = [
  "mcp>=1.12.4",
  "claude-agent-sdk>=0.1.0",
  "openai-codex-app-server-sdk @ git+https://github.com/openai/codex.git@main#subdirectory=sdk/python",
  "anyio>=4.5",
  "httpx>=0.27",
  "python-ulid>=3.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "ruff>=0.6",
  "pyright>=1.1.380",
]
```

Append at the very end of the file:

```toml
[tool.pyright]
include = ["src", "tests"]
venvPath = "."
venv = ".venv"
pythonVersion = "3.12"
typeCheckingMode = "basic"
```

- [ ] **Step 2: Resync the venv with the new deps**

Run: `uv sync --extra dev`
Expected: a line about installing/updating `openai-codex-app-server-sdk` (built from the GitHub repo) and `pyright`. May take 30–90 seconds the first time because uv has to clone the openai/codex repo.

If it fails with a network error, retry once. If it still fails, the GitHub URL or commit SHA may have moved — check `https://github.com/openai/codex/tree/main/sdk/python` exists and contains a `pyproject.toml` declaring `openai-codex-app-server-sdk`.

- [ ] **Step 3: Verify the SDK swap didn't break runtime**

Run: `uv run python -c "from codex_app_server import AsyncCodex, AppServerConfig, TextInput; print('OK')"`
Expected: `OK`

If this fails, the install didn't produce the expected import names. Don't proceed to step 4 — diagnose why first.

- [ ] **Step 4: Run pytest to confirm no regressions**

Run: `uv run pytest tests/`
Expected: `225 passed`

If any test fails, the SDK shape has drifted from what the codebase calls. Do NOT continue — investigate the failure inline (likely an `AsyncCodex.thread_start()` signature change or similar) and either patch the call site or roll back the dep swap.

- [ ] **Step 5: Capture the pyright baseline**

Run: `uv run pyright`
Expected: `38 errors, 0 warnings, 0 informations`

(42 errors at design time minus 4 `Import "codex_app_server" could not be resolved` errors that disappear once the right module is installed.)

- [ ] **Step 6: Run ruff to confirm formatting**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
chore(deps): integrate pyright; swap codex dep to OpenAI SDK

The declared codex-app-server-sdk was a third-party SDK with a different
API; the codebase imports `from codex_app_server import AsyncCodex, ...`
which is OpenAI's official SDK at github.com/openai/codex/sdk/python.
The mismatch was hidden by try/except ImportError blocks (next task
removes them).

Adds pyright>=1.1.380 + [tool.pyright] basic-mode config. Baseline:
38 errors, will drive to 0 across the remaining tasks.

EOF
)"
```

---

## Task 2: Remove lazy SDK imports

After Task 1, both SDKs are importable; the `try/except ImportError` pattern in five modules is dead code that confuses pyright (`AsyncCodex: type[AsyncCodex] | None` → `reportOptionalCall` at 8 call sites).

**Files:**
- Modify: `src/harness_mcp/sprints.py`
- Modify: `src/harness_mcp/generator.py`
- Modify: `src/harness_mcp/planning.py`
- Modify: `src/harness_mcp/summarizer.py`
- Modify: `src/harness_mcp/prereqs.py`

- [ ] **Step 1: Replace the try/except in `sprints.py`**

In `src/harness_mcp/sprints.py`, find this block near the top (around lines 38–50):

```python
# Lazy SDK imports so unit tests can monkeypatch.
try:
    from codex_app_server import (  # type: ignore[import-untyped]
        AppServerConfig,
        AsyncCodex,
        TextInput,
    )
except ImportError:  # pragma: no cover
    AppServerConfig = AsyncCodex = TextInput = None  # type: ignore[assignment]
try:
    from claude_agent_sdk import query  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    query = None  # type: ignore[assignment]
```

Replace with:

```python
from codex_app_server import AppServerConfig, AsyncCodex, TextInput
from claude_agent_sdk import query
```

- [ ] **Step 2: Remove the `if AppServerConfig is not None else None` guard at the call site**

Still in `sprints.py`, find `_drive_codex_round` (around line 110) and simplify the `cfg = (...)` ternary. Before:

```python
    cfg = (
        AppServerConfig(
            codex_bin=codex_bin,
            cwd=str(cwd),
            config_overrides=codex_overrides,
            client_name="harness-mcp",
            client_title="Harness Generator",
            client_version=__version__,
        )
        if AppServerConfig is not None
        else None
    )
```

After:

```python
    cfg = AppServerConfig(
        codex_bin=codex_bin,
        cwd=str(cwd),
        config_overrides=codex_overrides,
        client_name="harness-mcp",
        client_title="Harness Generator",
        client_version=__version__,
    )
```

The downstream `async with AsyncCodex(config=cfg)` and `TextInput(user_prompt) if TextInput else user_prompt` ternary should also be simplified. Replace `TextInput(user_prompt) if TextInput else user_prompt` with `TextInput(user_prompt)`.

- [ ] **Step 3: Replace the try/except in `generator.py`**

In `src/harness_mcp/generator.py`, find the import block near line 24:

```python
# Imported lazily so unit tests can monkeypatch AsyncCodex without bringing the SDK in.
try:
    from codex_app_server import AppServerConfig, AsyncCodex, TextInput
except ImportError:  # pragma: no cover  — only hit if SDK isn't installed during isolated unit runs
    AsyncCodex = AppServerConfig = TextInput = None  # type: ignore[assignment]
```

Replace with:

```python
from codex_app_server import AppServerConfig, AsyncCodex, TextInput
```

- [ ] **Step 4: Simplify the `cfg = ... if AppServerConfig is not None ...` ternary in `generator.py`**

In `chunk_loop` (around line 405), find:

```python
        cfg = (
            AppServerConfig(
                codex_bin=codex_bin,
                cwd=str(app_dir),
                config_overrides=codex_config_overrides,
                client_name="harness-mcp",
                client_title="Harness Generator",
                client_version=__version__,
            )
            if AppServerConfig is not None
            else None
        )
```

Replace with:

```python
        cfg = AppServerConfig(
            codex_bin=codex_bin,
            cwd=str(app_dir),
            config_overrides=codex_config_overrides,
            client_name="harness-mcp",
            client_title="Harness Generator",
            client_version=__version__,
        )
```

Also find `TextInput(prompt) if TextInput else prompt` (around line 435) and replace with `TextInput(prompt)`.

- [ ] **Step 5: Replace the try/except in `planning.py`**

In `src/harness_mcp/planning.py`, find the block at lines 32–39:

```python
# The SDK is imported lazily so unit tests can swap `query` via monkeypatch
# without making it a hard import (and to keep the module fast to import).
try:
    from claude_agent_sdk import (
        query,  # type: ignore[import-untyped]
    )
except ImportError:  # pragma: no cover
    query = None  # type: ignore[assignment]
```

Replace with:

```python
from claude_agent_sdk import query
```

- [ ] **Step 6: Replace the try/except in `summarizer.py`**

In `src/harness_mcp/summarizer.py`, find lines 17–20:

```python
try:
    from claude_agent_sdk import query  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    query = None  # type: ignore[assignment]
```

Replace with:

```python
from claude_agent_sdk import query
```

- [ ] **Step 7: Replace the try/except in `prereqs.py`**

In `src/harness_mcp/prereqs.py`, find lines 35–50:

```python
# Codex SDK is indirected so unit tests can monkeypatch each piece independently.
# Used in Task 5's probe; importing here keeps all imports at the top to avoid E402.
try:
    from codex_app_server import (
        AppServerConfig as _AppServerConfig,  # type: ignore[import-untyped]
    )
    from codex_app_server import (
        AsyncCodex as _AsyncCodex,  # type: ignore[import-untyped]
    )
    from codex_app_server import (
        TextInput as _TextInput,  # type: ignore[import-untyped]
    )
except ImportError:  # pragma: no cover  - only hit when SDK isn't installed
    _AppServerConfig = None  # type: ignore[assignment]
    _AsyncCodex = None  # type: ignore[assignment]
    _TextInput = None  # type: ignore[assignment]
```

Replace with:

```python
from codex_app_server import AppServerConfig as _AppServerConfig
from codex_app_server import AsyncCodex as _AsyncCodex
from codex_app_server import TextInput as _TextInput
```

(The `_`-prefixed aliases are intentional — they signal "internal to this module" and the existing call sites use these names.)

- [ ] **Step 8: Run pytest — must still be 225 passed**

Run: `uv run pytest tests/`
Expected: `225 passed`

If any test fails, a test was relying on the `None` fallback in a way I missed. Most likely: a test imports `AppServerConfig` from one of these modules without the SDK installed. Check the failing test's traceback and either install the SDK in that scope or restore the lazy import for that single name.

- [ ] **Step 9: Run pyright — must drop to 30 errors**

Run: `uv run pyright`
Expected: `30 errors, 0 warnings, 0 informations`

(38 → 30 = 8 errors dropped, all `reportOptionalCall` from the previously-Optional names: 1 in generator.py, 1 in planning.py, 1 in summarizer.py, 3 in prereqs.py, 2 in sprints.py.)

If the count is wrong, scan the output: any remaining `reportOptionalCall` against `AsyncCodex` / `AppServerConfig` / `query` means a guard or ternary still wraps a call site. Find and remove it.

- [ ] **Step 10: Run ruff**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 11: Commit**

```bash
git add src/harness_mcp/sprints.py src/harness_mcp/generator.py src/harness_mcp/planning.py src/harness_mcp/summarizer.py src/harness_mcp/prereqs.py
git commit -m "$(cat <<'EOF'
refactor(sdk): drop lazy try/except for required deps

Both claude_agent_sdk and the OpenAI codex_app_server SDK are now
required deps that uv sync always installs. The try/except ImportError
fallback was dead defense and produced 8 reportOptionalCall errors
in pyright. Removing it gives every call site a real type and drops
the optional-call boilerplate.

Tests still pass — monkeypatch.setattr on a normal import works
identically to monkeypatch on a lazy import.

EOF
)"
```

---

## Task 3: Cast at MCP-options boundary

Pyright sees `setting_sources: list[SettingSource] | None` and `mcp_servers: dict[str, McpServerConfig] | str | Path` on `ClaudeAgentOptions`; the codebase passes `list[str]` and `dict[str, dict[str, Any]]` (compatible at runtime, but the type checker needs a hint).

**Files:**
- Modify: `src/harness_mcp/server.py`
- Modify: `src/harness_mcp/evaluator_runner.py`

- [ ] **Step 1: Add `cast` to imports in `server.py`**

In `src/harness_mcp/server.py`, find the existing `from typing import Any` (around line 19) and replace with:

```python
from typing import Any, cast
```

- [ ] **Step 2: Cast in `_make_planner_options_factory`**

In `src/harness_mcp/server.py`, find `_make_planner_options_factory` (around lines 97–113). The existing factory body returns:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("planner.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=prereqs_result.setting_sources,
            mcp_servers={"context7": prereqs_result.captured_mcp["context7"]},
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
```

Replace with:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("planner.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
```

- [ ] **Step 3: Cast in `_make_reviewer_options_factory`**

In `_make_reviewer_options_factory` (around lines 117–134) the existing return is:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("reviewer.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=prereqs_result.setting_sources,
            mcp_servers={"context7": prereqs_result.captured_mcp["context7"]},
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
```

Replace with:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("reviewer.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
```

- [ ] **Step 4: Cast in `_make_evaluator_options_factory`**

In `_make_evaluator_options_factory` (around lines 137–162). Round 5 already removed Playwright from this factory; the existing return is:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("evaluator.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=prereqs_result.setting_sources,
            mcp_servers={"context7": prereqs_result.captured_mcp["context7"]},
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
```

Replace with:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("evaluator.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
```

- [ ] **Step 5: Cast in `_make_summarizer_options_factory`**

In `_make_summarizer_options_factory` (around lines 165–177) the existing return is:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("summarizer.md"),
            cwd=str(job_dir),
            setting_sources=prereqs_result.setting_sources,
            mcp_servers={"context7": prereqs_result.captured_mcp["context7"]},
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
```

Replace with:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("summarizer.md"),
            cwd=str(job_dir),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
```

- [ ] **Step 6: Cast in `evaluator_runner._run`**

In `src/harness_mcp/evaluator_runner.py`, the existing imports are at lines 14–32. Add `cast` to the typing import. Find:

```python
from typing import Any
```

Replace with:

```python
from typing import Any, cast
```

Now find the `ClaudeAgentOptions(...)` construction in `_run` (around lines 87–94):

```python
    options = ClaudeAgentOptions(
        system_prompt=_resolved_prompt_text("evaluator.md"),
        cwd=str(job_dir),
        setting_sources=setting_sources,
        mcp_servers={name: dict(stanza) for name, stanza in captured_mcp.items()},
        extra_args={"strict-mcp-config": None},
        permission_mode="bypassPermissions",
    )
```

Replace with:

```python
    options = ClaudeAgentOptions(
        system_prompt=_resolved_prompt_text("evaluator.md"),
        cwd=str(job_dir),
        setting_sources=cast(Any, setting_sources),
        mcp_servers=cast(Any, {name: dict(stanza) for name, stanza in captured_mcp.items()}),
        extra_args={"strict-mcp-config": None},
        permission_mode="bypassPermissions",
    )
```

- [ ] **Step 7: Run pytest — must still be 225 passed**

Run: `uv run pytest tests/`
Expected: `225 passed`

`cast` is a runtime no-op, so behavior cannot have changed.

- [ ] **Step 8: Run pyright — must drop to 21 errors**

Run: `uv run pyright`
Expected: `21 errors, 0 warnings, 0 informations`

(30 → 21 = 9 errors dropped: 8 in `server.py` factories, 1 in `evaluator_runner.py:91`.)

- [ ] **Step 9: Run ruff**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add src/harness_mcp/server.py src/harness_mcp/evaluator_runner.py
git commit -m "$(cat <<'EOF'
chore(types): cast(Any, ...) at the ClaudeAgentOptions boundary

ClaudeAgentOptions wants list[SettingSource] (Literal) and
dict[str, McpServerConfig] (TypedDict); we pass list[str] and
dict[str, dict[str, Any]] from prereqs.py / mcp_capture.py. The runtime
shapes are compatible — the cast is a localized "trust me" so we don't
have to leak SDK type names into PrereqsResult.

EOF
)"
```

---

## Task 4: Narrow anyio imports

Pyright resolves `anyio.to_thread` to a `BrokenWorkerInterpreter` union member instead of the real `to_thread` submodule, losing the `run_sync` attribute. Same issue for `anyio.create_task_group`. Importing the names directly bypasses the union.

**Files:**
- Modify: `src/harness_mcp/state.py`
- Modify: `src/harness_mcp/evaluator.py`
- Modify: `src/harness_mcp/evaluator_runner.py`
- Modify: `src/harness_mcp/generator.py`
- Modify: `src/harness_mcp/logging_setup.py`
- Modify: `src/harness_mcp/server.py`

- [ ] **Step 1: Narrow `state.py`**

In `src/harness_mcp/state.py`, find the existing import line (around line 26):

```python
import anyio
```

Replace with:

```python
import anyio
from anyio import to_thread
```

(Keep `import anyio` because `anyio.Lock` is still used in the module.)

Then find every `anyio.to_thread.run_sync(...)` call in this file and rewrite as `to_thread.run_sync(...)`. Two sites (around lines 179 and 204).

Before:
```python
    async with _writer_lock:
        await anyio.to_thread.run_sync(_exec_commit, stmt, params)
```

After:
```python
    async with _writer_lock:
        await to_thread.run_sync(_exec_commit, stmt, params)
```

Apply identically to the second site.

- [ ] **Step 2: Narrow `evaluator.py`**

In `src/harness_mcp/evaluator.py`, find the import (line 23):

```python
import anyio
```

Replace with:

```python
import anyio
from anyio import to_thread
```

Then rewrite the four `anyio.to_thread.run_sync(...)` call sites (lines 148, 150, 153, 245) as `to_thread.run_sync(...)`. Each is a single-token replacement.

- [ ] **Step 3: Narrow `evaluator_runner.py`**

In `src/harness_mcp/evaluator_runner.py`, find the import (line 21):

```python
import anyio
```

Replace with:

```python
import anyio
from anyio import to_thread
```

Then rewrite the two `anyio.to_thread.run_sync(...)` call sites (lines 80, 82) as `to_thread.run_sync(...)`.

- [ ] **Step 4: Narrow `generator.py`**

In `src/harness_mcp/generator.py`, find the import (around line 21):

```python
import anyio
```

Replace with:

```python
import anyio
from anyio import to_thread
```

Then rewrite the single `anyio.to_thread.run_sync(...)` call site (around line 288 — inside `_commit_and_tag_sync` wrapper) as `to_thread.run_sync(...)`.

- [ ] **Step 5: Narrow `logging_setup.py`**

In `src/harness_mcp/logging_setup.py`, find the import (line 23):

```python
import anyio
```

Replace with:

```python
from anyio import to_thread
```

(`anyio` itself is not used in this file beyond `to_thread`, so we can drop the bare `import anyio`.)

Then rewrite the four `anyio.to_thread.run_sync(...)` call sites (lines 110, 115, 120, 127) as `to_thread.run_sync(...)`.

- [ ] **Step 6: Narrow `server.py`**

In `src/harness_mcp/server.py`, find the imports near line 21:

```python
import anyio
```

Replace with:

```python
import anyio
from anyio import create_task_group
```

Then rewrite the single call site at line 192 (in `lifespan`):

Before:
```python
    async with anyio.create_task_group() as tg:
```

After:
```python
    async with create_task_group() as tg:
```

(Keep `import anyio` because `anyio.abc.TaskGroup`, `anyio.CancelScope`, `anyio.Lock`, etc. are still referenced elsewhere in the file.)

- [ ] **Step 7: Run pytest — must still be 225 passed**

Run: `uv run pytest tests/`
Expected: `225 passed`

This is a pure rebinding refactor — runtime behavior is identical.

- [ ] **Step 8: Run pyright — must drop to 7 errors**

Run: `uv run pyright`
Expected: `7 errors, 0 warnings, 0 informations`

(21 → 7 = 14 errors dropped, matching the spec's count.)

- [ ] **Step 9: Run ruff**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add src/harness_mcp/state.py src/harness_mcp/evaluator.py src/harness_mcp/evaluator_runner.py src/harness_mcp/generator.py src/harness_mcp/logging_setup.py src/harness_mcp/server.py
git commit -m "$(cat <<'EOF'
chore(types): narrow anyio.to_thread / create_task_group imports

Pyright loses anyio.to_thread through a union with BrokenWorkerInterpreter
and reports reportAttributeAccessIssue at every run_sync site. Importing
the submodule directly (from anyio import to_thread) bypasses the bad
resolution. Same fix for create_task_group in server.py.

EOF
)"
```

---

## Task 5: Fix `__main__.py` host/port — real runtime bug

`FastMCP.run_streamable_http_async()` takes zero args. The codebase passes `host=` and `port=` kwargs that would crash the streamable-http transport at runtime. The correct path is to mutate `server.settings.host`/`port` before invoking the runner.

**Files:**
- Modify: `src/harness_mcp/__main__.py`

- [ ] **Step 1: Fix the streamable-http branch**

In `src/harness_mcp/__main__.py`, find `_run_serve` (around lines 11–30). The existing function:

```python
def _run_serve(args: argparse.Namespace) -> int:
    """Boot the FastMCP server with the chosen transport.

    FastMCP exposes async transport methods as `run_stdio_async()` and
    `run_streamable_http_async()` (plus `run_sse_async()` if needed) — the
    bare `run_stdio` / `run_streamable_http` names are NOT awaitable. Verify
    the exact method names against the pinned `mcp` version before lock-in;
    if the SDK rename happens, update both call sites here.
    """
    from harness_mcp.server import server  # noqa: PLC0415

    transport = args.transport
    if transport == "stdio":
        anyio.run(server.run_stdio_async)
    elif transport == "streamable-http":
        anyio.run(lambda: server.run_streamable_http_async(host=args.host, port=args.port))
    else:
        print(f"unknown transport: {transport}", file=sys.stderr)
        return 1
    return 0
```

Replace with:

```python
def _run_serve(args: argparse.Namespace) -> int:
    """Boot the FastMCP server with the chosen transport.

    FastMCP exposes async transport methods as `run_stdio_async()` and
    `run_streamable_http_async()`. Neither takes positional / keyword
    arguments; host/port are read from `server.settings`, which is set at
    `FastMCP(...)` construction time. We mutate it from the CLI args here
    so `--host` / `--port` flags actually take effect.
    """
    from harness_mcp.server import server  # noqa: PLC0415

    transport = args.transport
    if transport == "stdio":
        anyio.run(server.run_stdio_async)
    elif transport == "streamable-http":
        server.settings.host = args.host
        server.settings.port = args.port
        anyio.run(server.run_streamable_http_async)
    else:
        print(f"unknown transport: {transport}", file=sys.stderr)
        return 1
    return 0
```

- [ ] **Step 2: Run pytest — must still be 225 passed**

Run: `uv run pytest tests/`
Expected: `225 passed`

(The CLI is not exercised by the unit tests; the smoke test is the only thing that would notice. No regression expected.)

- [ ] **Step 3: Run pyright — must drop to 5 errors**

Run: `uv run pyright`
Expected: `5 errors, 0 warnings, 0 informations`

(7 → 5 = 2 errors dropped, the bogus `host`/`port` kwargs.)

- [ ] **Step 4: Run ruff**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/harness_mcp/__main__.py
git commit -m "$(cat <<'EOF'
fix(cli): route --host/--port through server.settings, not run_streamable_http_async

FastMCP.run_streamable_http_async() takes zero args. The previous code
passed host/port as kwargs that the SDK does not accept, so any non-
default --host or --port flag would crash the streamable-http
transport at runtime. The intended config path is server.settings.host /
server.settings.port, which the run method reads. Mutate those from the
parsed CLI args before invoking the runner.

EOF
)"
```

---

## Task 6: Fix test-side type issues

Five remaining errors, all in test files: a sync-fixture-as-Path annotation, two None-subscript reads, and one float-as-int.

**Files:**
- Modify: `tests/test_state.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_generator.py`

- [ ] **Step 1: Fix `test_state.py:initialized_home` fixture annotation**

In `tests/test_state.py`, find the fixture at lines 26–31:

```python
@pytest.fixture
def initialized_home(tmp_harness_home: Path) -> Path:
    """tmp_harness_home + state.db opened."""
    init_db()
    yield tmp_harness_home
    close_db()
```

Replace with:

```python
@pytest.fixture
def initialized_home(tmp_harness_home: Path) -> Iterator[Path]:
    """tmp_harness_home + state.db opened."""
    init_db()
    yield tmp_harness_home
    close_db()
```

Also add `Iterator` to the imports near the top of `tests/test_state.py`. Find:

```python
import sqlite3
from pathlib import Path
```

Add immediately above `from pathlib import Path`:

```python
from collections.abc import Iterator
```

(Note: the spec said `AsyncIterator[Path]`, but this fixture is **sync** — `def initialized_home`, not `async def`. Sync generator fixture → `Iterator[Path]`.)

- [ ] **Step 2: Add `structuredContent is not None` assertion in `test_server.py`**

In `tests/test_server.py`, find `test_known_errors_map_to_codes` (around lines 27–42):

```python
    @pytest.mark.parametrize(
        "exc,expected_code",
        [
            (UnknownJobError("J"), "UNKNOWN_JOB"),
            (JobNotFinishedError("J"), "JOB_NOT_FINISHED"),
            (DesignDocNotFoundError("/x"), "DESIGN_DOC_NOT_FOUND"),
            (InvalidOptionsError("k"), "INVALID_OPTIONS"),
        ],
    )
    def test_known_errors_map_to_codes(self, exc: Exception, expected_code: str) -> None:
        result = _to_call_tool_error(exc)
        assert result.isError is True
        # structured_content holds the code under "code".
        assert result.structuredContent["code"] == expected_code
        assert isinstance(result.structuredContent["message"], str)
```

Insert one line — `assert result.structuredContent is not None` — between `assert result.isError is True` and the comment. After:

```python
    def test_known_errors_map_to_codes(self, exc: Exception, expected_code: str) -> None:
        result = _to_call_tool_error(exc)
        assert result.isError is True
        assert result.structuredContent is not None
        # structured_content holds the code under "code".
        assert result.structuredContent["code"] == expected_code
        assert isinstance(result.structuredContent["message"], str)
```

- [ ] **Step 3: Replace float `codex_reset_minutes` in `test_generator.py`**

In `tests/test_generator.py`, find `test_chunk_loop_breaks_when_wall_clock_exceeds_reset_minutes` (around lines 816–897). Two changes:

**Change A — the fake clock cadence comment + advance step:**

Before (around lines 826–834):

```python
        # Fake monotonic clock the chunk_loop sees. Each call advances 0.5s.
        # codex_reset_minutes=0.01 -> 0.6 seconds. Two clock advances put
        # us past the threshold.
        clock_now = [1000.0]

        def fake_monotonic() -> float:
            clock_now[0] += 0.5
            return clock_now[0]
```

After:

```python
        # Fake monotonic clock the chunk_loop sees. Each call advances 31s.
        # codex_reset_minutes=1 -> 60-second threshold. The first call
        # captures `chunk_started`; the next __anext__ ticks +31s (delta=31s,
        # below threshold), the one after that ticks +31s more (delta=62s,
        # past threshold) and triggers the break.
        clock_now = [1000.0]

        def fake_monotonic() -> float:
            clock_now[0] += 31.0
            return clock_now[0]
```

**Change B — the JobOptions construction:**

Before (around line 877–879):

```python
        opts = JobOptions(
            codex_reset_steps=99999, codex_reset_minutes=0.01, max_codex_chunks_per_sprint=2
        )
```

After:

```python
        opts = JobOptions(
            codex_reset_steps=99999, codex_reset_minutes=1, max_codex_chunks_per_sprint=2
        )
```

**Change C — update the trailing comment that explains the cadence (around lines 872–876):**

Before:

```python
        # reset_minutes=0.01 → 0.6s threshold. Each fake_monotonic() call
        # advances by 0.5s. The first call is for `chunk_started = monotonic()`
        # outside the loop (at t=1000.5). Inside the per-event branch it ticks
        # to 1001.0 (delta=0.5; below threshold), then 1001.5 (delta=1.0; above).
        # So the second event triggers the break.
```

After:

```python
        # reset_minutes=1 → 60s threshold. Each fake_monotonic() call
        # advances by 31s. The first call captures chunk_started at t=1031.0;
        # the next event ticks to 1062.0 (delta=31s, below threshold), and the
        # one after that ticks to 1093.0 (delta=62s, above). So the second
        # event after chunk_started triggers the break.
```

Also update the assertion's trailing comment (around lines 893–895):

Before:

```python
        # Each chunk yields 2 events before the wall-clock branch triggers
        # the break (event 2 sees delta >= 1.0s > 0.6s threshold).
        assert per_chunk_yields == [2, 2], (
            f"expected 2 yields per chunk (wall-clock break at event 2), got {per_chunk_yields}"
        )
```

After:

```python
        # Each chunk yields 2 events before the wall-clock branch triggers
        # the break (event 2 sees delta >= 62s > 60s threshold).
        assert per_chunk_yields == [2, 2], (
            f"expected 2 yields per chunk (wall-clock break at event 2), got {per_chunk_yields}"
        )
```

- [ ] **Step 4: Run pytest — `test_chunk_loop_breaks_when_wall_clock_exceeds_reset_minutes` must still pass**

Run: `uv run pytest tests/test_generator.py::TestChunkLoop::test_chunk_loop_breaks_when_wall_clock_exceeds_reset_minutes -v`
Expected: `1 passed`

If the assertion `per_chunk_yields == [2, 2]` fails, the new clock cadence didn't trigger the break at the expected event. Re-check the math: `chunk_started` captures the clock immediately before `async for event in turn.stream():` (per the Round 4 fix that moved this line); each `__anext__` call invokes `fake_monotonic` once via `monotonic() - chunk_started`. With +31s per call, event 1 has delta=31s (below 60s threshold), event 2 has delta=62s (above), so the break fires after event 2 yields → `per_chunk_yields[i] == 2`.

- [ ] **Step 5: Run the full test suite — must still be 225 passed**

Run: `uv run pytest tests/`
Expected: `225 passed`

- [ ] **Step 6: Run pyright — MUST be 0 errors**

Run: `uv run pyright`
Expected: `0 errors, 0 warnings, 0 informations`

(5 → 0. Done.)

If any errors remain, list them and address each per the appropriate earlier task pattern.

- [ ] **Step 7: Run ruff**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add tests/test_state.py tests/test_server.py tests/test_generator.py
git commit -m "$(cat <<'EOF'
chore(tests): satisfy pyright on fixture annotation + None subscript + int field

- test_state.py:initialized_home is a sync yielding fixture; annotate
  Iterator[Path] not Path so pyright accepts the generator.
- test_server.py:test_known_errors_map_to_codes asserts
  structuredContent is not None before subscripting (it's typed
  dict | None on CallToolResult).
- test_generator.py wall-clock test used codex_reset_minutes=0.01
  (float) to make a sub-second threshold reachable from a fake clock;
  rewrote with reset_minutes=1 (int) and a clock that advances 31s per
  __anext__ so delta crosses the 60s threshold on event 2. Same
  behavior asserted, type-correct field value.

Pyright is now at 0 errors.

EOF
)"
```

---

## Self-Review

After completing all tasks, the following invariants should hold:

- [ ] `uv run pytest tests/` → `225 passed`
- [ ] `uv run pyright` → `0 errors, 0 warnings, 0 informations`
- [ ] `uv run ruff check .` → `All checks passed!`
- [ ] `pyproject.toml` has `[tool.pyright]` block, `pyright>=1.1.380` in dev deps, and `openai-codex-app-server-sdk @ git+...` in deps (no `codex-app-server-sdk`).
- [ ] No file in `src/harness_mcp/` contains `try:` followed by an SDK import wrapped in `except ImportError`.
- [ ] No file calls `anyio.to_thread.run_sync(...)` or `anyio.create_task_group()` (use the narrowed imports).
- [ ] `__main__.py` does not pass `host=` or `port=` to `run_streamable_http_async`.

If any check fails, re-examine the most recent task's commit; partial revert + redo is preferred over patching forward.

---

## Spec Coverage Check

| Spec section | Plan task |
|---|---|
| §2 Pyright config | Task 1 step 1, step 7 |
| §3 Codex dep swap | Task 1 step 1, steps 2–4 |
| §4 Lazy-import refactor | Task 2 |
| §5 MCP boundary cast | Task 3 |
| §6 anyio narrowing | Task 4 |
| §7 host/port runtime fix | Task 5 |
| §8.1 fixture annotation | Task 6 step 1 |
| §8.2 None subscript | Task 6 step 2 |
| §8.3 float-as-int | Task 6 step 3 |
| §9 ordering | Tasks 1–6 in order |
| §10 out of scope | (no tasks; documented as not done) |
| §11 notable decisions | (informational; no tasks) |
