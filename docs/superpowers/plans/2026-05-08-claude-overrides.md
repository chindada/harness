# Claude Binary + Config Dir Overrides — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the harness's choice of `claude` binary and `CLAUDE_CONFIG_DIR` user-overridable via `HARNESS_CLAUDE_BIN` and `HARNESS_CLAUDE_CONFIG_DIR` env vars; add a `check_claude_binary` doctor prereq that mirrors `check_codex_binary`.

**Architecture:** TDD-flavored. Tests for `check_claude_binary` first (Task 1, fails), then implementation in `prereqs.py` plus wiring into `run_prereqs` (Task 2, tests pass). Server-side helpers and 5 `ClaudeAgentOptions` sites updated next (Task 3). `evaluator_runner.py` inlines equivalents (Task 4). README updates (Task 5). End-to-end verification (Task 6).

**Tech Stack:** Python 3.12, `pytest`, `ruff`, `pyright`, `uv`.

**Spec:** `docs/superpowers/specs/2026-05-08-claude-overrides-design.md`

---

## File Structure

Files modified (in order of work):

- `tests/test_prereqs.py` — Task 1: add `check_claude_binary` to imports, add `TestCheckClaudeBinary` class with 4 tests.
- `src/harness_mcp/prereqs.py` — Task 2: add `check_claude_binary` function, wire into `run_prereqs` between `check_codex_binary` and `probe_codex_sdk_shape`.
- `src/harness_mcp/server.py` — Task 3: update `_resolve_claude_cli` to read `HARNESS_CLAUDE_BIN`, add `_claude_env_overrides`, add `env=_claude_env_overrides()` to all 5 `ClaudeAgentOptions` construction sites.
- `src/harness_mcp/evaluator_runner.py` — Task 4: add `import os`, inline the `cli_path` and `env` overrides in the single `ClaudeAgentOptions` site.
- `README.md` — Task 5: extend env vars table with two new rows, update the doctor expected-output line, add a troubleshooting bullet.

No files created, no abstractions changed beyond the new helper functions.

**Stage-only convention (this session):** Each task's "Commit" step documents a `git add` and an optional `git commit`. Per session preference, run only `git add`; let the user commit. Either is fine.

---

## Task 1: Add failing tests for `check_claude_binary`

**Files:**
- Modify: `tests/test_prereqs.py` (top imports + new class after `TestCheckCodexBinary`)

**Why this comes first:** TDD red-green. The new tests reference a function that doesn't exist yet — running them confirms the tests actually exercise new behavior. Once Task 2 implements the function, tests pass.

- [ ] **Step 1: Read `tests/test_prereqs.py` to lock in baseline**

Read the full file. Confirm:
- The import block near the top imports from `harness_mcp.prereqs` and includes `check_codex_binary` but NOT `check_claude_binary`.
- A `TestCheckCodexBinary` class exists (~line 63) that uses `monkeypatch.setenv("HARNESS_CODEX_BIN", ...)` and `tmp_path` fixtures.
- A `TestCheckEnv` class exists with the new tuple-returning behavior from earlier in the session.

If line numbers have drifted, locate by content; the Edits anchor on exact text.

- [ ] **Step 2: Add `check_claude_binary` to the import block**

Apply Edit on `tests/test_prereqs.py`:

`old_string`:

```python
from harness_mcp.prereqs import (
    DoctorReport,
    PrereqFailedError,
    assert_strict_mcp_config_works,
    check_codex_binary,
    check_env,
    check_paths_and_db,
    format_doctor_report,
    probe_codex_sdk_shape,
    probe_mcp_servers,
    probe_skill,
    sweep_at_startup,
)
```

`new_string`:

```python
from harness_mcp.prereqs import (
    DoctorReport,
    PrereqFailedError,
    assert_strict_mcp_config_works,
    check_claude_binary,
    check_codex_binary,
    check_env,
    check_paths_and_db,
    format_doctor_report,
    probe_codex_sdk_shape,
    probe_mcp_servers,
    probe_skill,
    sweep_at_startup,
)
```

- [ ] **Step 3: Add `TestCheckClaudeBinary` class right after `TestCheckCodexBinary`**

Locate the end of `TestCheckCodexBinary` (look for the next `class TestSweepAtStartup` or similar — that's the boundary). Insert the new class immediately before that next class, with a blank line between.

The class body to insert:

```python
class TestCheckClaudeBinary:
    def test_uses_harness_claude_bin_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_bin = tmp_path / "fake_claude.sh"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("HARNESS_CLAUDE_BIN", str(fake_bin))

        msg = check_claude_binary()
        assert msg.startswith("OK")
        assert str(fake_bin) in msg

    def test_uses_path_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("HARNESS_CLAUDE_BIN", raising=False)
        fake_bin = tmp_path / "claude"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))

        msg = check_claude_binary()
        assert msg.startswith("OK")
        assert str(fake_bin) in msg

    def test_fails_when_env_points_at_nonexistent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HARNESS_CLAUDE_BIN", "/nonexistent/claude")
        monkeypatch.setenv("PATH", "")
        with pytest.raises(PrereqFailedError):
            check_claude_binary()

    def test_fails_when_neither_env_nor_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HARNESS_CLAUDE_BIN", raising=False)
        monkeypatch.setenv("PATH", "")
        with pytest.raises(PrereqFailedError):
            check_claude_binary()
```

Apply via Edit using the line of code immediately preceding the insertion point as the anchor (e.g., the closing `)` or the last assertion of the previous class's last test).

A safe Edit pattern: locate a unique anchor like the FINAL closing line of `TestCheckCodexBinary` (its last test's last assertion) and append the new class with `\n\n`. Or insert before the unique heading line of the next class (e.g., `class TestSweepAtStartup:`):

`old_string`: `class TestSweepAtStartup:`
`new_string`:
```
class TestCheckClaudeBinary:
    ...  (the full class above)


class TestSweepAtStartup:
```

(Pick whichever boundary line is unique in the file. Both options preserve test order.)

- [ ] **Step 4: Run the new tests; expect ImportError**

```bash
uv run pytest tests/test_prereqs.py::TestCheckClaudeBinary -v
```

Expected: `ImportError: cannot import name 'check_claude_binary' from 'harness_mcp.prereqs'`. Because the import block now references a non-existent name, pytest fails to collect ANY tests in this file (collection error). That's the expected failure mode for a TDD red step where the import fails — it confirms the function is missing. Other passing tests in the file are now also "failing" via collection error; this is temporary and resolved by Task 2.

If you see a different error (e.g., `NameError: name 'check_claude_binary'`), the import edit didn't land correctly. Re-apply.

- [ ] **Step 5: Stage (or commit)**

```bash
git add tests/test_prereqs.py
```

Optional commit:

```bash
git commit -m "$(cat <<'EOF'
test(prereqs): assert check_claude_binary contract

Tests fail by design (collection ImportError) — Task 2 implements
check_claude_binary so the new TestCheckClaudeBinary class passes.

Spec: docs/superpowers/specs/2026-05-08-claude-overrides-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

(Skip the commit and stage only per session preference.)

---

## Task 2: Implement `check_claude_binary` and wire into `run_prereqs`

**Files:**
- Modify: `src/harness_mcp/prereqs.py` (insert function after `check_codex_binary`'s closing line; wire call site into `run_prereqs`)

**Why this is one task:** Adding the function alone leaves it dead code. Adding the call site without the function breaks `run_prereqs`. Both edits land together so the doctor remains coherent at every commit boundary.

- [ ] **Step 1: Read `src/harness_mcp/prereqs.py` to lock in baseline**

Read the full file. Confirm:
- `check_codex_binary` ends around line 146 with the `return f"OK codex: ..."` line.
- The line after `check_codex_binary` is blank, then the next `def` (e.g., `async def sweep_at_startup` or another check).
- `run_prereqs` (around line 350+) has the sequence:
  ```python
  msg = check_codex_binary()
  report.add("codex_binary", "OK", msg)

  codex_msg, codex_overrides = await probe_codex_sdk_shape()
  ```

- [ ] **Step 2: Insert `check_claude_binary` function**

Apply Edit on `src/harness_mcp/prereqs.py`:

`old_string` (the closing of `check_codex_binary` plus the blank separator before the next function — using the exact two lines as anchor for uniqueness):

```python
    return f"OK codex: {proc.stdout.strip() or '(no version output)'}{config_note}"


```

`new_string`:

```python
    return f"OK codex: {proc.stdout.strip() or '(no version output)'}{config_note}"


def check_claude_binary() -> str:
    """Resolve $HARNESS_CLAUDE_BIN or `which claude`. Validate it's a real file.

    Multi-account users set HARNESS_CLAUDE_BIN to pin which `claude` install
    the harness uses, independent of PATH or shell aliases. Skips a `--version`
    invocation (unlike check_codex_binary) because `claude --version` can be slow
    and may surface auth prompts; existence + executability is the right level
    of check, matching what the SDK validates internally before spawning.
    """
    bin_path = os.environ.get("HARNESS_CLAUDE_BIN") or shutil.which("claude")
    if not bin_path:
        raise PrereqFailedError(
            "Claude Code CLI not found: set HARNESS_CLAUDE_BIN or add claude to PATH"
        )
    if not Path(bin_path).is_file() and shutil.which(bin_path) is None:
        raise PrereqFailedError(f"Claude Code CLI {bin_path!r} does not exist")
    return f"OK claude: {bin_path}"


```

(Two trailing blank lines preserve the original spacing pattern of the file. The `old_string` anchors on the unique closing line of `check_codex_binary`.)

- [ ] **Step 3: Wire `check_claude_binary` into `run_prereqs`**

Apply Edit on `src/harness_mcp/prereqs.py`:

`old_string`:

```python
    msg = check_codex_binary()
    report.add("codex_binary", "OK", msg)

    codex_msg, codex_overrides = await probe_codex_sdk_shape()
    report.add("codex_sdk_shape", "OK", codex_msg)
```

`new_string`:

```python
    msg = check_codex_binary()
    report.add("codex_binary", "OK", msg)

    msg = check_claude_binary()
    report.add("claude_binary", "OK", msg)

    codex_msg, codex_overrides = await probe_codex_sdk_shape()
    report.add("codex_sdk_shape", "OK", codex_msg)
```

- [ ] **Step 4: Run the prereqs tests**

```bash
uv run pytest tests/test_prereqs.py -v
```

Expected: all tests pass, including the four new `TestCheckClaudeBinary` tests. The collection ImportError from Task 1's red state is resolved.

- [ ] **Step 5: Run lint and type check**

```bash
uv run ruff check src/harness_mcp/prereqs.py tests/test_prereqs.py
uv run ruff format --check src/harness_mcp/prereqs.py tests/test_prereqs.py
uv run pyright
```

Expected: all pass with no new findings. Pre-existing pyright noise in other files (asynccontextmanager deprecation, etc.) is unrelated.

If `ruff format --check` flags `tests/test_prereqs.py`, run `uv run ruff format tests/test_prereqs.py` (matches the inline-fix pattern from the previous session).

- [ ] **Step 6: Stage (or commit)**

```bash
git add src/harness_mcp/prereqs.py tests/test_prereqs.py
```

Optional commit:

```bash
git commit -m "$(cat <<'EOF'
feat(prereqs): add check_claude_binary doctor prereq

Mirrors check_codex_binary: resolves HARNESS_CLAUDE_BIN or PATH, fails
fast if neither yields a real executable. Wired into run_prereqs
between check_codex_binary and probe_codex_sdk_shape so any
SDK-using probe downstream sees a coherent error if claude is missing.

Skips --version (unlike codex) — claude --version can be slow and
auth-prompt-prone; existence + is_file() matches SDK's own validation.

Spec: docs/superpowers/specs/2026-05-08-claude-overrides-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Update `server.py` — helpers + 5 `ClaudeAgentOptions` sites

**Files:**
- Modify: `src/harness_mcp/server.py` (update `_resolve_claude_cli`, add `_claude_env_overrides`, add `env=_claude_env_overrides()` to all 5 sites; `_client_factory` uses `kw.setdefault` for symmetry).

**Why a separate task:** Lots of small edits in one file but each is independent; isolating them as a single atomic commit makes review obvious ("this commit threads the new env-var override through the server").

- [ ] **Step 1: Read `src/harness_mcp/server.py` to lock in baseline**

Read the full file. Confirm:
- `_resolve_claude_cli` exists at ~line 95-110, currently returns `shutil.which("claude")`.
- Five `ClaudeAgentOptions(...)` constructions exist:
  - Inside `_make_planner_options_factory` (~line 106-115)
  - Inside `_make_reviewer_options_factory` (~line 126-135)
  - Inside `_make_evaluator_options_factory` (~line 154-163)
  - Inside `_make_summarizer_options_factory` (~line 172-181)
  - Inside `_client_factory` (~line 199-209), via `kw.setdefault("cli_path", _resolve_claude_cli())`
- `os` is already imported at the top.

- [ ] **Step 2: Update `_resolve_claude_cli` and add `_claude_env_overrides`**

Apply Edit on `src/harness_mcp/server.py`:

`old_string`:

```python
def _resolve_claude_cli() -> str | None:
    """Return the user's PATH `claude` so the SDK doesn't fall back to its bundled binary.

    The SDK ships a `_bundled/claude` and prefers it over PATH. That bundled install
    has no plugins, so probing for plugin-provided skills (e.g., superpowers:writing-plans)
    reports them missing. Setting cli_path on ClaudeAgentOptions bypasses the bundled
    choice. Returns None if `claude` isn't on PATH; the SDK then falls back to its own
    resolver (no regression for users who never had `claude` on PATH anyway).
    """
    return shutil.which("claude")
```

`new_string`:

```python
def _resolve_claude_cli() -> str | None:
    """Return the user's PATH `claude` so the SDK doesn't fall back to its bundled binary.

    HARNESS_CLAUDE_BIN wins; falls back to PATH. The SDK ships a `_bundled/claude`
    and prefers it over PATH; setting cli_path on ClaudeAgentOptions bypasses that
    so the spawned claude has the user's plugins. Multi-account users set
    HARNESS_CLAUDE_BIN to pin a specific install regardless of PATH.

    Returns None if neither env nor PATH yields anything; the SDK then falls back
    to its own resolver (no regression for users who never had `claude` on PATH).
    """
    return os.environ.get("HARNESS_CLAUDE_BIN") or shutil.which("claude")


def _claude_env_overrides() -> dict[str, str]:
    """Env overrides spliced into ClaudeAgentOptions.env for the spawned claude.

    Per SDK behavior (subprocess_cli.py:430-455), options.env always wins over
    inherited env. So HARNESS_CLAUDE_CONFIG_DIR pins the spawned claude's config
    dir even if the launching parent had a different CLAUDE_CONFIG_DIR.
    """
    overrides: dict[str, str] = {}
    if cdir := os.environ.get("HARNESS_CLAUDE_CONFIG_DIR"):
        overrides["CLAUDE_CONFIG_DIR"] = cdir
    return overrides
```

- [ ] **Step 3: Add `env=_claude_env_overrides()` to `_make_planner_options_factory`**

Apply Edit on `src/harness_mcp/server.py`:

`old_string`:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("planner.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
            cli_path=_resolve_claude_cli(),
        )
```

`new_string`:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("planner.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
            cli_path=_resolve_claude_cli(),
            env=_claude_env_overrides(),
        )
```

- [ ] **Step 4: Add `env=_claude_env_overrides()` to `_make_reviewer_options_factory`**

Apply Edit on `src/harness_mcp/server.py`:

`old_string`:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("reviewer.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
            cli_path=_resolve_claude_cli(),
        )
```

`new_string`:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("reviewer.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
            cli_path=_resolve_claude_cli(),
            env=_claude_env_overrides(),
        )
```

- [ ] **Step 5: Add `env=_claude_env_overrides()` to `_make_evaluator_options_factory`**

Apply Edit on `src/harness_mcp/server.py`:

`old_string`:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("evaluator.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
            cli_path=_resolve_claude_cli(),
        )
```

`new_string`:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("evaluator.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
            cli_path=_resolve_claude_cli(),
            env=_claude_env_overrides(),
        )
```

- [ ] **Step 6: Add `env=_claude_env_overrides()` to `_make_summarizer_options_factory`**

Apply Edit on `src/harness_mcp/server.py`:

`old_string`:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("summarizer.md"),
            cwd=str(job_dir),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
            cli_path=_resolve_claude_cli(),
        )
```

`new_string`:

```python
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("summarizer.md"),
            cwd=str(job_dir),
            setting_sources=cast(Any, prereqs_result.setting_sources),
            mcp_servers=cast(Any, {"context7": prereqs_result.captured_mcp["context7"]}),
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
            cli_path=_resolve_claude_cli(),
            env=_claude_env_overrides(),
        )
```

- [ ] **Step 7: Add `env` setdefault to `_client_factory`**

Apply Edit on `src/harness_mcp/server.py`:

`old_string`:

```python
def _client_factory(**kw: Any) -> Any:  # noqa: ANN401
    """Default client factory passed to prereqs probes."""
    from claude_agent_sdk import (  # type: ignore[import-untyped]  # noqa: PLC0415
        ClaudeAgentOptions,
        ClaudeSDKClient,
    )

    kw.setdefault("cli_path", _resolve_claude_cli())
    options = ClaudeAgentOptions(**kw)
    return ClaudeSDKClient(options=options)
```

`new_string`:

```python
def _client_factory(**kw: Any) -> Any:  # noqa: ANN401
    """Default client factory passed to prereqs probes."""
    from claude_agent_sdk import (  # type: ignore[import-untyped]  # noqa: PLC0415
        ClaudeAgentOptions,
        ClaudeSDKClient,
    )

    kw.setdefault("cli_path", _resolve_claude_cli())
    kw.setdefault("env", _claude_env_overrides())
    options = ClaudeAgentOptions(**kw)
    return ClaudeSDKClient(options=options)
```

- [ ] **Step 8: Lint + format + pyright + tests**

```bash
uv run ruff check src/harness_mcp/server.py
uv run ruff format --check src/harness_mcp/server.py
uv run pyright
uv run pytest -k 'not smoke'
```

Expected: all pass. The existing 228 tests still pass; no new tests for the helpers themselves (they're trivial wrappers, indirectly tested through `TestCheckClaudeBinary`).

- [ ] **Step 9: Stage (or commit)**

```bash
git add src/harness_mcp/server.py
```

Optional commit:

```bash
git commit -m "$(cat <<'EOF'
feat(server): thread HARNESS_CLAUDE_BIN/CONFIG_DIR through ClaudeAgentOptions

_resolve_claude_cli now reads HARNESS_CLAUDE_BIN as override on top of
PATH. New _claude_env_overrides splices HARNESS_CLAUDE_CONFIG_DIR into
ClaudeAgentOptions.env (which always wins over inherited env per SDK
behavior). All 5 ClaudeAgentOptions construction sites get the env
override; _client_factory uses kw.setdefault for symmetry with cli_path.

Spec: docs/superpowers/specs/2026-05-08-claude-overrides-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Update `evaluator_runner.py` with inline overrides

**Files:**
- Modify: `src/harness_mcp/evaluator_runner.py` (add `import os`, expand the existing `cli_path=...` line and add `env=...`).

- [ ] **Step 1: Read `src/harness_mcp/evaluator_runner.py` to lock in baseline**

Read the imports (~lines 14-23) and the `ClaudeAgentOptions(...)` site (~line 88-99). Confirm:
- `import os` is NOT in the imports.
- `import shutil` IS in the imports.
- The current `ClaudeAgentOptions` call has `cli_path=shutil.which("claude")` from the previous session's fix.

- [ ] **Step 2: Add `import os` to the imports**

Apply Edit on `src/harness_mcp/evaluator_runner.py`:

`old_string`:

```python
import json
import shutil
import sys
from pathlib import Path
from typing import Any, cast
```

`new_string`:

```python
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, cast
```

(`os` slots between `json` and `shutil` to keep stdlib imports alphabetical, matching the convention.)

- [ ] **Step 3: Update the `ClaudeAgentOptions` construction**

Apply Edit on `src/harness_mcp/evaluator_runner.py`:

`old_string`:

```python
    options = ClaudeAgentOptions(
        system_prompt=_resolved_prompt_text("evaluator.md"),
        cwd=str(job_dir),
        setting_sources=cast(Any, setting_sources),
        mcp_servers=cast(Any, {name: dict(stanza) for name, stanza in captured_mcp.items()}),
        extra_args={"strict-mcp-config": None},
        permission_mode="bypassPermissions",
        # Override the SDK's bundled-CLI default; mirrors server._resolve_claude_cli().
        cli_path=shutil.which("claude"),
    )
```

`new_string`:

```python
    options = ClaudeAgentOptions(
        system_prompt=_resolved_prompt_text("evaluator.md"),
        cwd=str(job_dir),
        setting_sources=cast(Any, setting_sources),
        mcp_servers=cast(Any, {name: dict(stanza) for name, stanza in captured_mcp.items()}),
        extra_args={"strict-mcp-config": None},
        permission_mode="bypassPermissions",
        # Mirror server._resolve_claude_cli() and _claude_env_overrides(); kept inline
        # because evaluator_runner's allowed-import list forbids importing from server.py.
        cli_path=os.environ.get("HARNESS_CLAUDE_BIN") or shutil.which("claude"),
        env=(
            {"CLAUDE_CONFIG_DIR": v}
            if (v := os.environ.get("HARNESS_CLAUDE_CONFIG_DIR"))
            else {}
        ),
    )
```

- [ ] **Step 4: Lint + format + pyright + tests**

```bash
uv run ruff check src/harness_mcp/evaluator_runner.py
uv run ruff format --check src/harness_mcp/evaluator_runner.py
uv run pyright
uv run pytest -k 'not smoke'
```

Expected: all pass. evaluator_runner has its own test file (`tests/test_evaluator_runner.py`) — confirm those still pass; they don't reference `cli_path` or `env` per the previous-session check.

- [ ] **Step 5: Stage (or commit)**

```bash
git add src/harness_mcp/evaluator_runner.py
```

Optional commit:

```bash
git commit -m "$(cat <<'EOF'
feat(evaluator_runner): honor HARNESS_CLAUDE_BIN/CONFIG_DIR overrides

Inlines the same env-var resolution as server._resolve_claude_cli() and
_claude_env_overrides(). Inline rather than imported because
evaluator_runner's allowed-import list forbids server.py.

Spec: docs/superpowers/specs/2026-05-08-claude-overrides-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Update README

**Files:**
- Modify: `README.md` (env vars table, step 5 expected-output line, troubleshooting bullet).

- [ ] **Step 1: Read `README.md` to confirm baseline**

Read the full file. Confirm:
- Env vars table at lines ~77-80 has two rows (`ANTHROPIC_API_KEY`, `HARNESS_CODEX_BIN`).
- Step 5 line at ~line 56 reads: `Expected: a list of `OK` lines for paths, env, codex, codex-shape, skill, mcp, strict-mcp-config, restart_sweep.`
- A `## Troubleshooting` section exists with three bullets.

- [ ] **Step 2: Replace the env vars table with the expanded version**

Apply Edit on `README.md`:

`old_string`:

```markdown
| Var                 | Required | Purpose                                                                 |
| ------------------- | -------- | ----------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY` | no       | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
| `HARNESS_CODEX_BIN` | no       | Override `which codex`. Useful when codex isn't on PATH.                |
```

`new_string`:

```markdown
| Var                         | Required | Purpose                                                                                                                                                |
| --------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ANTHROPIC_API_KEY`         | no       | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK).                                                                                |
| `HARNESS_CODEX_BIN`         | no       | Override `which codex`. Useful when codex isn't on PATH.                                                                                               |
| `HARNESS_CLAUDE_BIN`        | no       | Override `which claude` for the Claude Agent SDK. Useful for multi-account setups.                                                                     |
| `HARNESS_CLAUDE_CONFIG_DIR` | no       | Override `CLAUDE_CONFIG_DIR` passed to the spawned `claude`. Pins which Claude account/config the harness uses, regardless of the shell that launched it. |
```

(Column widths grow because `HARNESS_CLAUDE_CONFIG_DIR` is the longest Var name. Markdown table padding is cosmetic and doesn't affect rendering, but keeping it aligned reads better in source.)

- [ ] **Step 3: Update step 5 expected-output line**

Apply Edit on `README.md`:

`old_string`:

```markdown
   Expected: a list of `OK` lines for paths, env, codex, codex-shape, skill, mcp, strict-mcp-config, restart_sweep.
```

`new_string`:

```markdown
   Expected: a list of `OK` lines for paths, env, codex, claude, codex-shape, skill, mcp, strict-mcp-config, restart_sweep.
```

(`claude` slots between `codex` and `codex-shape` — the position matching the run_prereqs sequence in §3.3.)

- [ ] **Step 4: Add troubleshooting bullet**

Apply Edit on `README.md`:

`old_string`:

```markdown
- **Playwright tests fail with "browser not found"** — reinstall Playwright browsers via the playwright MCP plugin's install command.
```

`new_string`:

```markdown
- **Playwright tests fail with "browser not found"** — reinstall Playwright browsers via the playwright MCP plugin's install command.
- **Skill probe finds the wrong account's plugins, or `claude_binary` resolves to the wrong install** — set `HARNESS_CLAUDE_CONFIG_DIR` and/or `HARNESS_CLAUDE_BIN` to pin which Claude account the harness uses. Example: `claude mcp add --scope user --transport stdio harness-mcp -e HARNESS_CLAUDE_CONFIG_DIR=$HOME/.claude-acct2 -e HARNESS_CLAUDE_BIN=$HOME/.claude-acct2/bin/claude -- harness-mcp serve --transport stdio`.
```

- [ ] **Step 5: Re-Read and verify**

Read the full `README.md`. Verify:
- Env vars table now has 4 rows.
- Step 5 line lists `claude` between `codex` and `codex-shape`.
- Troubleshooting has 4 bullets, the new one referencing `HARNESS_CLAUDE_*`.

- [ ] **Step 6: Stage (or commit)**

```bash
git add README.md
```

Optional commit:

```bash
git commit -m "$(cat <<'EOF'
docs(readme): document HARNESS_CLAUDE_BIN/CONFIG_DIR + claude doctor line

Adds the two new env vars to the Required environment variables table,
extends the doctor expected-output line with claude, and adds a
troubleshooting bullet for multi-account users.

Spec: docs/superpowers/specs/2026-05-08-claude-overrides-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: End-to-end verification

**Files:** None modified.

- [ ] **Step 1: Lint, format, type check, full test suite**

```bash
uv run ruff check . && uv run ruff format --check src/harness_mcp/server.py src/harness_mcp/evaluator_runner.py src/harness_mcp/prereqs.py tests/test_prereqs.py && uv run pyright && uv run pytest -k 'not smoke'
```

Expected: all pass. Test count should be 228 + 4 (new TestCheckClaudeBinary) = 232.

- [ ] **Step 2: Direct unit verification of `_resolve_claude_cli` with override**

```bash
HARNESS_CLAUDE_BIN=/usr/bin/test uv run python -c "from harness_mcp.server import _resolve_claude_cli; print(_resolve_claude_cli())"
```

Expected: `/usr/bin/test` (override used).

```bash
unset HARNESS_CLAUDE_BIN; uv run python -c "import os; os.environ.pop('HARNESS_CLAUDE_BIN', None); from harness_mcp.server import _resolve_claude_cli; print(_resolve_claude_cli())"
```

Expected: the path `shutil.which('claude')` returns (your normal claude path).

- [ ] **Step 3: Direct unit verification of `_claude_env_overrides` with override**

```bash
HARNESS_CLAUDE_CONFIG_DIR=/tmp/fake-config uv run python -c "from harness_mcp.server import _claude_env_overrides; print(_claude_env_overrides())"
```

Expected: `{'CLAUDE_CONFIG_DIR': '/tmp/fake-config'}`.

```bash
unset HARNESS_CLAUDE_CONFIG_DIR; uv run python -c "import os; os.environ.pop('HARNESS_CLAUDE_CONFIG_DIR', None); from harness_mcp.server import _claude_env_overrides; print(_claude_env_overrides())"
```

Expected: `{}`.

- [ ] **Step 4: Optional — full `harness-mcp doctor` smoke**

If the operator's environment is fully configured (codex, claude on PATH, plugins installed):

```bash
harness-mcp doctor
```

Expected: a `claude_binary` line in the output between `codex_binary` and `codex_sdk_shape`, in the form `OK   claude_binary: OK claude: <resolved-path>`.

If `HARNESS_CLAUDE_BIN` is set:

```bash
HARNESS_CLAUDE_BIN=/path/to/other/claude harness-mcp doctor
```

Expected: `OK   claude_binary: OK claude: /path/to/other/claude`.

(Skip this step if doctor's other prereqs aren't fully wired up locally; Steps 1-3 cover the unit-level guarantees.)

---

## Out of Scope (locked in by the spec §7)

These are explicitly NOT part of this plan and should be rejected if they appear during implementation:

- Per-job overrides (different account per build). Out of scope.
- Auto-detection of multi-account setups (scanning `~/.claude-*` directories). Out of scope.
- Validation that `HARNESS_CLAUDE_CONFIG_DIR` points at a directory containing valid Claude Code config. SDK and downstream probes already fail clearly if not.
- `--version` invocation in `check_claude_binary`. Existence + executability is sufficient.
- Refactoring `evaluator_runner.py`'s inline resolution into a shared module. Pattern matches existing codex resolution; out of scope.
- Storing the resolved claude path in `PrereqsResult` or `ServerState`. Re-resolving inline matches the existing codex pattern.
