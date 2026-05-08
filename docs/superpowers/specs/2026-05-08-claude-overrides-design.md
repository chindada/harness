# Claude Binary + Config Dir Overrides — Design Spec

**Date:** 2026-05-08
**Status:** Approved (brainstorm complete; awaiting implementation plan)

## 1. Goal

Make the harness's choice of `claude` binary and `CLAUDE_CONFIG_DIR` user-overridable via environment variables, so users with multiple Claude Code accounts can pin the harness to a specific account regardless of which one launched it. Mirrors the existing `HARNESS_CODEX_BIN` pattern.

Today the harness:

- Resolves the binary via `shutil.which("claude")` only — no override knob.
- Inherits `CLAUDE_CONFIG_DIR` from parent env via SDK env-passthrough — works when the user can control the parent env, but provides no harness-level override that wins over a parent value.

After this change:

- `HARNESS_CLAUDE_BIN` overrides the binary path resolution.
- `HARNESS_CLAUDE_CONFIG_DIR` is set as `CLAUDE_CONFIG_DIR` on the SDK's spawned-claude env, where `options.env` always wins per SDK behavior (`subprocess_cli.py:430-455`). This lets the harness pin a specific account independent of the launching shell or Claude Code session.

## 2. Decisions (carried from brainstorm)

| Question | Choice | Rationale |
| --- | --- | --- |
| Which env vars? | **Both `HARNESS_CLAUDE_BIN` and `HARNESS_CLAUDE_CONFIG_DIR`.** | Multi-account users need to pin the harness's account independent of whatever spawned the harness. Symmetry with `HARNESS_CODEX_BIN`. |
| Add a doctor prereq for the binary? | **Yes — `check_claude_binary` mirroring `check_codex_binary`.** | Early FAIL when neither HARNESS_CLAUDE_BIN nor PATH yields a binary. Avoids a confusing downstream "skill not found" or `CLINotFoundError`. |
| Resolution helpers shared? | **Helpers in `server.py`; `evaluator_runner.py` inlines.** | Existing pattern: codex resolution is also re-resolved inline in different modules. Avoids cross-module coupling for evaluator_runner (whose import surface is constrained). |
| README troubleshooting note? | **Include.** | Directly addresses the motivating multi-account use case; surfaces the override knobs to users searching for the failure mode. |

## 3. Code changes

### 3.1 `src/harness_mcp/server.py` — two helpers + 5 ClaudeAgentOptions sites

Replace existing `_resolve_claude_cli()` (added in `2026-05-08-anthropic-api-key-optional` follow-up) with the env-aware version, and add `_claude_env_overrides()` next to it.

```python
def _resolve_claude_cli() -> str | None:
    """User-overridable path to `claude`. HARNESS_CLAUDE_BIN wins; falls back to PATH.

    Without an override, the SDK's bundled-CLI default is used iff PATH lookup
    also fails (per cli_path=None semantics). Setting HARNESS_CLAUDE_BIN pins
    a specific install for multi-account setups.
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

Add `env=_claude_env_overrides()` to all five `ClaudeAgentOptions(...)` constructions in `server.py`:

- `_make_planner_options_factory` (line ~106)
- `_make_reviewer_options_factory` (line ~126)
- `_make_evaluator_options_factory` (line ~154)
- `_make_summarizer_options_factory` (line ~172)
- `_client_factory` (line ~199, via `kw.setdefault("env", _claude_env_overrides())` for kwarg-passthrough symmetry with `cli_path`)

`cli_path=_resolve_claude_cli()` is already wired in all five sites from a previous fix; the helper update is transparent there.

### 3.2 `src/harness_mcp/evaluator_runner.py` — inline equivalents

Add `import os` at the top of the file (currently absent; `shutil` and `Path` are already imported).

Update the `ClaudeAgentOptions` construction site (currently line ~88, may shift after the import addition):

```python
options = ClaudeAgentOptions(
    system_prompt=_resolved_prompt_text("evaluator.md"),
    cwd=str(job_dir),
    setting_sources=cast(Any, setting_sources),
    mcp_servers=cast(Any, {name: dict(stanza) for name, stanza in captured_mcp.items()}),
    extra_args={"strict-mcp-config": None},
    permission_mode="bypassPermissions",
    # Mirror server._resolve_claude_cli() and _claude_env_overrides() — kept
    # inline because evaluator_runner's allowed-import list is constrained.
    cli_path=os.environ.get("HARNESS_CLAUDE_BIN") or shutil.which("claude"),
    env=(
        {"CLAUDE_CONFIG_DIR": v}
        if (v := os.environ.get("HARNESS_CLAUDE_CONFIG_DIR"))
        else {}
    ),
)
```

The inline duplication is acceptable: matches the existing codex pattern (`HARNESS_CODEX_BIN` is also resolved inline in multiple modules), and `evaluator_runner.py`'s import allowlist forbids importing from `server.py`.

### 3.3 `src/harness_mcp/prereqs.py` — new `check_claude_binary` prereq

Add a new check that mirrors `check_codex_binary`'s shape (file at `prereqs.py:100-145`), minus the `--version` invocation:

```python
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

Insert into `run_prereqs` between `check_codex_binary` and `probe_codex_sdk_shape`:

```python
msg = check_codex_binary()
report.add("codex_binary", "OK", msg)

msg = check_claude_binary()
report.add("claude_binary", "OK", msg)

codex_msg, codex_overrides = await probe_codex_sdk_shape()
report.add("codex_sdk_shape", "OK", codex_msg)
```

Placement rationale: claude is required by every probe from `probe_skill` onwards. Failing fast before those probes turns "skill not found / SDK error" surfaces into clear "claude binary not found" surfaces.

### 3.4 No other code changes

The `PrereqsResult` dataclass doesn't need a new field for the resolved claude path — neither the orchestrator nor downstream tools currently use the codex_bin field stored in `ServerState` for spawned-agent invocations (those re-resolve inline). Following the same pattern keeps the data flow consistent.

## 4. Tests

### 4.1 New `TestCheckClaudeBinary` class in `tests/test_prereqs.py`

Mirror of `TestCheckCodexBinary` (lines ~63-128):

```python
class TestCheckClaudeBinary:
    def test_uses_harness_claude_bin_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_bin = tmp_path / "fake_claude.sh"
        fake_bin.write_text("#!/bin/sh\necho 'claude 1.2.3'\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("HARNESS_CLAUDE_BIN", str(fake_bin))

        msg = check_claude_binary()
        assert str(fake_bin) in msg
        assert msg.startswith("OK")

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

    def test_fails_when_binary_missing(
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

Add `check_claude_binary` to the `from harness_mcp.prereqs import (...)` block at the top of the file.

### 4.2 No new probe_skill tests needed

The fake-client tests added in the previous session cover the matching logic. Env-var overrides change which `claude` is spawned but not the format the spawned `claude` returns from `get_server_info`. Existing TestProbeSkill coverage stays valid.

### 4.3 No tests for `_resolve_claude_cli` / `_claude_env_overrides`

Trivial wrappers around `os.environ.get` + `shutil.which`. Indirectly exercised by `TestCheckClaudeBinary` (for the resolver) and end-to-end via the full doctor flow.

## 5. README changes

### 5.1 Env vars table — two new rows

Replaces the table at `README.md:77-80`:

```markdown
| Var                         | Required | Purpose                                                                 |
| --------------------------- | -------- | ----------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`         | no       | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
| `HARNESS_CODEX_BIN`         | no       | Override `which codex`. Useful when codex isn't on PATH.                |
| `HARNESS_CLAUDE_BIN`        | no       | Override `which claude` for the Claude Agent SDK. Useful for multi-account setups. |
| `HARNESS_CLAUDE_CONFIG_DIR` | no       | Override `CLAUDE_CONFIG_DIR` passed to spawned `claude`. Pins which Claude account/config the harness uses, regardless of the shell that launched it. |
```

(Markdown table column widths grow to accommodate the longer Var names. Cosmetic only — column padding doesn't affect rendering.)

### 5.2 Step 5 expected-output line

Replaces the line at `README.md:56`:

```markdown
   Expected: a list of `OK` lines for paths, env, codex, claude, codex-shape, skill, mcp, strict-mcp-config, restart_sweep.
```

`claude` inserts after `codex` and before `codex-shape`, matching the new doctor sequence (§3.3). The list-words follow the existing convention: each word is the detail-prefix the corresponding check emits (e.g., `OK paths: ...` → `paths`, `OK codex: ...` → `codex`, and now `OK claude: ...` → `claude`), not the `report.add` name.

### 5.3 Troubleshooting bullet

Adds a bullet to the existing `## Troubleshooting` section:

```markdown
- **Skill probe finds the wrong account's plugins, or `claude_binary` resolves to the wrong install** — set `HARNESS_CLAUDE_CONFIG_DIR` and/or `HARNESS_CLAUDE_BIN` to pin which Claude account the harness uses. Example: `claude mcp add --scope user --transport stdio harness-mcp -e HARNESS_CLAUDE_CONFIG_DIR=$HOME/.claude-acct2 -e HARNESS_CLAUDE_BIN=$HOME/.claude-acct2/bin/claude -- harness-mcp serve --transport stdio`.
```

## 6. Verification

After implementation:

1. **Lint:** `uv run ruff check . && uv run ruff format --check src/harness_mcp/server.py src/harness_mcp/evaluator_runner.py src/harness_mcp/prereqs.py tests/test_prereqs.py`. Passes.
2. **Type check:** `uv run pyright`. Passes.
3. **Tests:** `uv run pytest -k 'not smoke'`. Passes, including 4 new `TestCheckClaudeBinary` tests.
4. **Manual smoke** — three scenarios:
   - **Default (no overrides):** `harness-mcp doctor` emits `OK   claude_binary: OK claude: <path-from-PATH>` with no behavior change for the existing single-account flow.
   - **Override binary:** `HARNESS_CLAUDE_BIN=/path/to/other/claude harness-mcp doctor` emits `OK   claude_binary: OK claude: /path/to/other/claude`.
   - **Override config dir:** `HARNESS_CLAUDE_CONFIG_DIR=$HOME/.claude-acct2 harness-mcp doctor` — the spawned claude now reads its plugins/auth from `~/.claude-acct2/`. The `skill` probe reflects whichever plugins are enabled in the alt account.

## 7. Out of scope

- Per-job overrides (e.g., starting a build with a different account than the harness was launched with). The harness uses one set of credentials per `harness-mcp serve` lifetime; per-job overrides are a separate feature.
- Auto-detection of multi-account setups (e.g., scanning for `~/.claude-*` directories). YAGNI — the env vars are explicit and discoverable.
- Validation that `HARNESS_CLAUDE_CONFIG_DIR` points at a directory that contains a valid Claude Code config. The SDK and downstream probes will fail clearly if it doesn't.
- `--version` invocation in `check_claude_binary`. Existence + executability is sufficient (matches SDK's own checks); avoids slow / auth-prompting `claude --version`.
- Refactoring `evaluator_runner.py`'s inline resolution into a shared module. Pattern matches existing codex resolution; out of scope.

## 8. Risks and tradeoffs

- **Inline duplication between `server.py` and `evaluator_runner.py`.** Two places need to read the same env vars. Mitigation: kept narrow (a single line each for `cli_path` and `env`), with a code comment in `evaluator_runner.py` pointing at `server.py` helpers. If a third site appears, escalate to a shared module.
- **`HARNESS_CLAUDE_CONFIG_DIR` overrides parent env unconditionally.** A user who sets `CLAUDE_CONFIG_DIR` for the parent process expecting it to propagate may be surprised when `HARNESS_CLAUDE_CONFIG_DIR` is also set and wins. Mitigation: documented behavior; the harness-prefixed var being explicit is the design intent.
- **`check_claude_binary` doesn't validate authentication state.** A binary that exists but isn't authenticated will pass the doctor and fail at first SDK call. Acceptable: matches `check_codex_binary`'s level of validation; the WARN-level `check_env` already directs users at `claude auth status` for that concern.
- **Doctor's expected-output line list in README is a maintenance hazard.** Adding `claude_binary` to that list keeps it accurate now but every future probe addition requires a parallel README update. Out of scope to address structurally, but worth noting.
