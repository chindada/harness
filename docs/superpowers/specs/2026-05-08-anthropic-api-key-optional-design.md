# `ANTHROPIC_API_KEY` Optional — Design Spec

**Date:** 2026-05-08
**Status:** Approved (brainstorm complete; awaiting implementation plan)

## 1. Goal

Soften `harness-mcp doctor`'s `ANTHROPIC_API_KEY` check from a hard failure to a warning. The Claude Agent SDK already resolves auth from a documented priority chain — env vars (`ANTHROPIC_API_KEY` → `CLAUDE_CODE_OAUTH_TOKEN`) → file (`~/.claude/.credentials.json`) → macOS keychain. The current `check_env()` raises `PrereqFailedError` when the env var is missing, blocking users who authenticate via Claude Code CLI subscription. That's a false negative.

**Verified empirically (2026-05-08):** Running `claude_agent_sdk.query(...)` with both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` removed from `os.environ` returned a successful `AssistantMessage` from `claude-opus-4-7`, sourcing credentials from the macOS keychain. The SDK auth fallback works in practice; the harness's hard requirement is the only blocker.

## 2. Decisions (carried from brainstorm)

| Question | Choice | Rationale |
| --- | --- | --- |
| What should doctor do when key is missing? | **Soften to WARN.** Doctor passes with a warning line; never fails on missing key. | Removes the false negative without losing the early signal. |
| Scope of changes? | **Code + tests + README.** Skip PROMPT.md. | README is user-facing and would drift; PROMPT.md is the frozen original brief. |
| How to thread WARN status through the doctor framework? | **Approach 1: `check_env()` returns `tuple[status, msg]`.** | Smallest contract change. No string parsing, no enum refactor, only check that needs WARN today. |

## 3. Code changes (`src/harness_mcp/prereqs.py`)

### 3.1 `check_env()` — new signature and body

Replaces the current implementation at `src/harness_mcp/prereqs.py:93-97`.

```python
def check_env() -> tuple[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return "OK", "env: ANTHROPIC_API_KEY is set"
    return (
        "WARN",
        "env: ANTHROPIC_API_KEY not set; relying on Claude Code CLI auth "
        "(keychain or ~/.claude/.credentials.json). Verify with 'claude auth status'.",
    )
```

Never raises `PrereqFailedError`. Returns `(status, msg)` where `status` is `"OK"` or `"WARN"`.

### 3.2 `format_doctor_report()` — render `WARN` distinctly

Replaces the current implementation at `src/harness_mcp/prereqs.py:54-59`.

```python
def format_doctor_report(report: DoctorReport) -> str:
    lines = []
    for name, status, detail in report.rows:
        if status == "OK":
            marker = "OK  "
        elif status == "WARN":
            marker = "WARN"
        else:
            marker = "FAIL"
        lines.append(f"{marker} {name}: {detail}")
    return "\n".join(lines)
```

Three branches replace the previous two-branch `"OK  " if status == "OK" else "FAIL"` ternary. Marker width stays at 4 characters across all states, so the existing column alignment in `harness-mcp doctor` output is preserved.

### 3.3 `run_prereqs()` — unpack the tuple

Replaces the call site at `src/harness_mcp/prereqs.py:362-363`.

```python
status, msg = check_env()
report.add("env", status, msg)
```

`check_env` is called from one site (verified via grep across `src/`); no other callers need updating.

### 3.4 No other code changes

`PrereqFailedError` is still imported by other checks (`check_codex_binary`, etc.), so the import stays. The class itself remains in use.

## 4. Test changes (`tests/test_prereqs.py`)

### 4.1 `TestCheckEnv` — rewrite all three tests

Replaces the existing `TestCheckEnv` class at `tests/test_prereqs.py:43-57`.

```python
class TestCheckEnv:
    def test_returns_ok_when_anthropic_key_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
        status, msg = check_env()
        assert status == "OK"
        assert "ANTHROPIC_API_KEY" in msg

    def test_returns_warn_when_key_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        status, msg = check_env()
        assert status == "WARN"
        assert "Claude Code CLI auth" in msg

    def test_returns_warn_when_key_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        status, msg = check_env()
        assert status == "WARN"
```

The `pytest.raises(PrereqFailedError)` assertions for the missing/empty cases are removed — `check_env` no longer raises. `PrereqFailedError` import at the top of the file stays (still used by `TestCheckCodexBinary`).

### 4.2 `TestDoctorReport.test_formats_passes_and_fails` — extend to cover WARN

Replaces the existing test at `tests/test_prereqs.py:149-156`.

```python
class TestDoctorReport:
    def test_formats_passes_warns_and_fails(self) -> None:
        report = DoctorReport()
        report.add("paths", "OK", "~/.harness exists; state.db at ~/.harness/state.db")
        report.add("env", "WARN", "ANTHROPIC_API_KEY not set; relying on Claude Code CLI auth")
        report.add("codex", "FAIL", "binary not found")
        out = format_doctor_report(report)
        assert "OK   paths" in out
        assert "WARN env" in out
        assert "FAIL codex" in out
```

Test name updates from `test_formats_passes_and_fails` to `test_formats_passes_warns_and_fails` to match the new coverage. The fabricated `"FAIL env"` row from the original test no longer represents a real scenario (env never FAILs anymore), so the FAIL coverage moves to a `codex` row to keep the FAIL formatter exercised.

### 4.3 No other test files need changes

Confirmed via `grep -rni ANTHROPIC_API_KEY tests/`: matches only in `tests/test_prereqs.py`.

## 5. README changes (`README.md`)

### 5.1 Table row — `Required` column from `yes` to `no`

Replaces the row at `README.md:79`.

```markdown
| `ANTHROPIC_API_KEY` | no | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
```

### 5.2 Note below the table — explain the fallback

Replaces the existing parenthetical at `README.md:82`. The current line is `(Codex auth lives in ~/.codex/auth.json; no OPENAI_API_KEY needed.)`. The new version retains that text and appends two sentences describing the Claude auth fallback:

```markdown
(Codex auth lives in `~/.codex/auth.json`; no `OPENAI_API_KEY` needed. If `ANTHROPIC_API_KEY` is unset, the Claude Agent SDK falls back to Claude Code CLI auth — system keychain on macOS, or `~/.claude/.credentials.json` on Linux/Windows. Verify with `claude auth status`.)
```

### 5.3 Step 4 streamable-http daemon command — drop the env prefix

Replaces lines 41–42 in `README.md`.

Before:
```bash
# 1. Start the daemon (needs ANTHROPIC_API_KEY in env):
ANTHROPIC_API_KEY=... harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

After:
```bash
# 1. Start the daemon:
harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

Auth resolution for the daemon is the same as for stdio — whatever's available from env vars or Claude Code CLI auth. The table + new sentence cover it for users who want to think about it.

## 6. Verification

After implementation:

1. **Lint:** `uv run ruff check . && uv run ruff format --check .` — passes.
2. **Type check:** `uv run pyright` — passes. (Project recently integrated pyright in basic mode; see `2026-05-08-pyright-integration-design.md`.)
3. **Tests:** `uv run pytest -k 'not smoke'` — passes, including the rewritten `TestCheckEnv` and extended `TestDoctorReport` tests.
4. **Manual doctor smoke**, two cases:
   - **With** `ANTHROPIC_API_KEY` set: `harness-mcp doctor` emits `OK   env: env: ANTHROPIC_API_KEY is set` and continues. All other checks pass; exit 0.
   - **Without** `ANTHROPIC_API_KEY`: `harness-mcp doctor` emits `WARN env: env: ANTHROPIC_API_KEY not set; relying on Claude Code CLI auth (keychain or ~/.claude/.credentials.json). Verify with 'claude auth status'.` and continues. All other checks pass; exit 0.

The doctor's exit code remains 0 in both cases — WARN does not cause failure.

## 7. Out of scope

- `PROMPT.md` updates. Per §2 (decision = B), PROMPT.md is the original frozen brief, not user-facing docs.
- Detecting Claude Code CLI auth state inside the harness (probing `~/.claude/.credentials.json`, querying the keychain, running `claude auth status`). Per §2 (decision = B, not C), the SDK already does this; replicating the logic in our prereq check duplicates SDK behavior we'd then need to keep in sync.
- Removing `check_env` entirely. Per §2 (decision = B, not A), the WARN line preserves the early signal a fresh user gets from `harness-mcp doctor`.
- Refactoring other prereq checks to a `PrereqStatus` enum. Per §2 (Approach 1, not 3), one new state is not worth the touch-everywhere refactor; if a second check needs WARN later, that's the natural moment to escalate.
- Adding new auth pathways (Bedrock, Vertex, manual OAuth token threading). Separate work.

## 8. Risks and tradeoffs

- **A user with no auth at all sees doctor pass with WARN, then their first build fails on the first SDK call.** This is the cost of softening from FAIL to WARN. The WARN message names `claude auth status` as the diagnostic command — that's the mitigation. Acceptable: a false-positive WARN is much rarer than the false-negative FAIL we're fixing.
- **Keychain OAuth tokens can expire.** Out of scope; the standard `claude auth login` workflow handles re-auth. The WARN message points users to `claude auth status`, which surfaces expiry.
- **Tests use `monkeypatch.delenv` to simulate "no key", but the SDK might still pick up keychain auth at test time.** Not a problem: the unit tests only call `check_env()`, which only reads `os.environ`. They don't invoke the SDK.
- **`"WARN"` marker shares column width with `"OK  "` and `"FAIL"` (4 chars).** Verified — existing doctor output formatting stays aligned.
