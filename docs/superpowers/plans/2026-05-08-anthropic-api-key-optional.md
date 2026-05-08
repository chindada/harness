# `ANTHROPIC_API_KEY` Optional — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Soften `harness-mcp doctor`'s `ANTHROPIC_API_KEY` check from FAIL to WARN, so users authenticated via Claude Code CLI subscription can run the harness without setting an API key.

**Architecture:** TDD-flavored: write tests first (Task 1), then update production code so they pass (Task 2), then update user-facing README (Task 3), then verify end-to-end (Task 4). All changes are in three files: `src/harness_mcp/prereqs.py`, `tests/test_prereqs.py`, `README.md`. No new files, no new abstractions, no signature changes outside `check_env`.

**Tech Stack:** Python 3.12, `pytest`, `ruff`, `pyright` (basic mode), `uv`.

**Spec:** `docs/superpowers/specs/2026-05-08-anthropic-api-key-optional-design.md`

---

## File Structure

Files modified (in order of work):

- `tests/test_prereqs.py` — Task 1: rewrite `TestCheckEnv` (3 tests) and `TestDoctorReport.test_formats_passes_and_fails` (1 test).
- `src/harness_mcp/prereqs.py` — Task 2: three small Edits — `check_env`, `format_doctor_report`, and the `check_env` call site inside `run_prereqs`.
- `README.md` — Task 3: three small Edits — the table row, the parenthetical below the table, and the streamable-http daemon command.

No files created, no files deleted, no abstractions changed beyond `check_env`'s return signature.

**TDD ordering note:** Task 1 writes tests that will FAIL against current production code. This is intentional — the failing test confirms the test exercises new behavior. Task 2 makes them pass. Don't reorder Task 1 and Task 2 without intent: rewriting the tests after changing the code defeats the verification step.

**Stage-only convention (this session):** The user has been running stage-only without commits in this session. Each task's "Commit" step is structured so the implementer can either run the documented `git commit` or stop after `git add` and let the user commit manually. Either is fine.

---

## Task 1: Rewrite the `TestCheckEnv` and `TestDoctorReport` tests (failing)

**Files:**
- Modify: `tests/test_prereqs.py:43-57` (the `TestCheckEnv` class)
- Modify: `tests/test_prereqs.py:149-156` (the `TestDoctorReport.test_formats_passes_and_fails` method)

**Why this comes first:** The new tests assert the new contract (`check_env` returns a tuple, doctor renders a `WARN` line). Running them against current production code MUST fail — `check_env` currently returns a `str` and `format_doctor_report` doesn't render `WARN`. Verifying that failure proves the tests actually exercise new behavior; if Task 1's tests passed accidentally against the old code, the spec coverage is broken.

- [ ] **Step 1: Read the current `tests/test_prereqs.py` to lock in baseline**

Read `/Users/timhsu/dev_projects/harness/tests/test_prereqs.py` from line 1, full file. Confirm:
- Line 43 is `class TestCheckEnv:`
- Line 57 is the closing line of `test_fails_when_key_empty`
- Line 149 is `class TestDoctorReport:`
- Line 156 is `        assert "FAIL env" in out`
- The file imports `check_env`, `DoctorReport`, `format_doctor_report`, `PrereqFailedError` near the top.

If line numbers have drifted, locate by content; the Edits are anchored on exact text, not line numbers.

- [ ] **Step 2: Replace the `TestCheckEnv` class**

Apply Edit on `tests/test_prereqs.py`:

`old_string`:

```python
class TestCheckEnv:
    def test_passes_when_anthropic_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
        msg = check_env()
        assert msg.startswith("OK")

    def test_fails_when_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(PrereqFailedError):
            check_env()

    def test_fails_when_key_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        with pytest.raises(PrereqFailedError):
            check_env()
```

`new_string`:

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

The `PrereqFailedError` import at the top of the file stays — it's still used by `TestCheckCodexBinary`.

- [ ] **Step 3: Replace the `TestDoctorReport.test_formats_passes_and_fails` method**

Apply Edit on `tests/test_prereqs.py`:

`old_string`:

```python
class TestDoctorReport:
    def test_formats_passes_and_fails(self) -> None:
        report = DoctorReport()
        report.add("paths", "OK", "~/.harness exists; state.db at ~/.harness/state.db")
        report.add("env", "FAIL", "ANTHROPIC_API_KEY missing")
        out = format_doctor_report(report)
        assert "OK   paths" in out
        assert "FAIL env" in out
```

`new_string`:

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

- [ ] **Step 4: Run the updated tests; expect failures**

```bash
uv run pytest tests/test_prereqs.py::TestCheckEnv tests/test_prereqs.py::TestDoctorReport -v
```

Expected: FAILURES.

The tuple-unpack tests (`status, msg = check_env()`) will fail because `check_env` currently returns a `str`, not a tuple. The unpack will throw `ValueError: too many values to unpack` or similar (a 2+ char string unpacks to ≥2 chars; `"OK env: ANTHROPIC_API_KEY is set"` definitely won't unpack to 2 elements).

The `TestDoctorReport.test_formats_passes_warns_and_fails` `WARN env` assertion will fail because `format_doctor_report` currently renders any non-`OK` status as `FAIL` (so the WARN row would render as `"FAIL env: ..."`).

If any of these tests PASSES at this stage, stop and investigate — either the test isn't exercising new behavior or the production code already has partial implementation.

- [ ] **Step 5: Stage (or commit) the test changes**

```bash
git add tests/test_prereqs.py
```

If committing: use the message below. If staging only (this session's pattern), stop after `git add`.

```bash
git commit -m "$(cat <<'EOF'
test(prereqs): assert check_env returns tuple and doctor renders WARN

Tests fail against current production code by design — the next commit
implements check_env's tuple return and format_doctor_report's WARN
branch.

Spec: docs/superpowers/specs/2026-05-08-anthropic-api-key-optional-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Update `prereqs.py` to make tests pass

**Files:**
- Modify: `src/harness_mcp/prereqs.py:54-59` (`format_doctor_report`)
- Modify: `src/harness_mcp/prereqs.py:93-97` (`check_env`)
- Modify: `src/harness_mcp/prereqs.py:362-363` (`run_prereqs` call site)

**Why this is one task:** Updating `check_env` alone breaks `run_prereqs` (which does `msg = check_env()` expecting a string). Updating `format_doctor_report` alone has no effect until a check returns "WARN". The three changes only make sense as a unit; doing them in one commit keeps the production code coherent at every commit boundary.

- [ ] **Step 1: Read `src/harness_mcp/prereqs.py` to lock in baseline**

Read `/Users/timhsu/dev_projects/harness/src/harness_mcp/prereqs.py` from line 50, limit 80 lines (covers `format_doctor_report` and `check_env`). Then read from line 355, limit 35 lines (covers the `run_prereqs` call site).

Confirm:
- Lines 54–59 contain the existing `format_doctor_report` function with the two-branch `marker = "OK  " if status == "OK" else "FAIL"` ternary.
- Lines 93–97 contain the existing `check_env` function with the `raise PrereqFailedError` branch.
- Lines 362–363 contain `msg = check_env()` followed by `report.add("env", "OK", msg)`.

- [ ] **Step 2: Replace `format_doctor_report`**

Apply Edit on `src/harness_mcp/prereqs.py`:

`old_string`:

```python
def format_doctor_report(report: DoctorReport) -> str:
    lines = []
    for name, status, detail in report.rows:
        marker = "OK  " if status == "OK" else "FAIL"
        lines.append(f"{marker} {name}: {detail}")
    return "\n".join(lines)
```

`new_string`:

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

All three markers are 4 characters wide, so column alignment in the rendered doctor output is preserved.

- [ ] **Step 3: Replace `check_env`**

Apply Edit on `src/harness_mcp/prereqs.py`:

`old_string`:

```python
def check_env() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise PrereqFailedError("ANTHROPIC_API_KEY not set or empty")
    return "OK env: ANTHROPIC_API_KEY is set"
```

`new_string`:

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

The function now returns `(status, msg)` and never raises. `PrereqFailedError` import in `prereqs.py` stays — it's still raised by other checks (`check_codex_binary`, `probe_codex_sdk_shape`, etc.).

- [ ] **Step 4: Replace the `run_prereqs` call site**

Apply Edit on `src/harness_mcp/prereqs.py`:

`old_string`:

```python
    msg = check_env()
    report.add("env", "OK", msg)
```

`new_string`:

```python
    status, msg = check_env()
    report.add("env", status, msg)
```

This is the only call site for `check_env` (verified via `grep -rn "check_env" src/`).

- [ ] **Step 5: Run the test suite**

```bash
uv run pytest tests/test_prereqs.py -v
```

Expected: all tests in `tests/test_prereqs.py` pass, including the three rewritten `TestCheckEnv` tests and the renamed `TestDoctorReport.test_formats_passes_warns_and_fails`.

If any of `TestCheckCodexBinary`, `TestProbeCodexSdkShape`, or other non-env tests fail, the failure is unrelated to this change — investigate separately.

- [ ] **Step 6: Run lint and type check**

```bash
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

Expected: both pass with no new findings. Pyright should accept the new `tuple[str, str]` return type without issue.

- [ ] **Step 7: Stage (or commit) the production changes**

```bash
git add src/harness_mcp/prereqs.py
```

If committing:

```bash
git commit -m "$(cat <<'EOF'
fix(prereqs): soften ANTHROPIC_API_KEY check to WARN

check_env returns (status, msg) tuple instead of raising; status is
"OK" when the env var is set, "WARN" when missing. format_doctor_report
learns a third marker for WARN. run_prereqs unpacks the tuple at the
single call site.

This unblocks users authenticated via Claude Code CLI subscription
(keychain on macOS, ~/.claude/.credentials.json elsewhere) — the
Claude Agent SDK already resolves auth from those sources, so the
hard ANTHROPIC_API_KEY requirement was a false negative.

Spec: docs/superpowers/specs/2026-05-08-anthropic-api-key-optional-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Update README

**Files:**
- Modify: `README.md` — three Edits, by content (line numbers vary depending on staged state).

**Why a separate task:** The README change is independent of code/tests in terms of correctness — markdown can be wrong without breaking pytest. Keeping it as its own commit lets a reviewer who's only interested in user-facing copy review just this commit.

- [ ] **Step 1: Read `README.md` to confirm current content**

Read `/Users/timhsu/dev_projects/harness/README.md` (full file). Confirm:
- Lines ~41–42 contain the streamable-http daemon snippet inside step 4, with the `ANTHROPIC_API_KEY=...` prefix and the `# 1. Start the daemon (needs ANTHROPIC_API_KEY in env):` comment.
- The `## Required environment variables` section contains a table with two rows: `ANTHROPIC_API_KEY | yes | ...` and `HARNESS_CODEX_BIN | no | ...`. The padding may be aligned (`| yes      |`) or compact (`| yes |`) depending on which version is currently checked out — both will work with the Edit if the `old_string` matches exactly.
- A line below the table reads `(Codex auth lives in `~/.codex/auth.json`; no `OPENAI_API_KEY` needed.)`.

If the table padding differs from what `old_string` below shows, copy the exact line from the file as the `old_string` (do not retype).

- [ ] **Step 2: Replace the streamable-http daemon command (step 4)**

Apply Edit on `README.md`:

`old_string`:

````
   # 1. Start the daemon (needs ANTHROPIC_API_KEY in env):
   ANTHROPIC_API_KEY=... harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
````

`new_string`:

````
   # 1. Start the daemon:
   harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
````

(Both blocks have a 3-space indent because they're inside a numbered list. Preserve that.)

- [ ] **Step 3: Replace the `ANTHROPIC_API_KEY` table row — `yes` to `no`**

Apply Edit on `README.md`. The current row uses padded alignment (per the file as it stands today after prior session work):

`old_string`:

```
| `ANTHROPIC_API_KEY` | yes      | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
```

`new_string`:

```
| `ANTHROPIC_API_KEY` | no       | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
```

(`no       ` has 7 trailing spaces to match the column width of `yes      `, which has 6.)

If the file is in compact (HEAD-style) form (`| yes |`), use this `old_string` and `new_string` instead:

`old_string`:

```
| `ANTHROPIC_API_KEY` | yes | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
```

`new_string`:

```
| `ANTHROPIC_API_KEY` | no | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
```

Pick whichever matches what Step 1's Read showed.

- [ ] **Step 4: Replace the parenthetical note below the table**

Apply Edit on `README.md`:

`old_string`:

```
(Codex auth lives in `~/.codex/auth.json`; no `OPENAI_API_KEY` needed.)
```

`new_string`:

```
(Codex auth lives in `~/.codex/auth.json`; no `OPENAI_API_KEY` needed. If `ANTHROPIC_API_KEY` is unset, the Claude Agent SDK falls back to Claude Code CLI auth — system keychain on macOS, or `~/.claude/.credentials.json` on Linux/Windows. Verify with `claude auth status`.)
```

- [ ] **Step 5: Re-Read and verify**

Read `/Users/timhsu/dev_projects/harness/README.md` (full file).

Verify:
- Step 4's streamable-http example no longer has the `ANTHROPIC_API_KEY=...` prefix or the `(needs ANTHROPIC_API_KEY in env)` comment.
- The `ANTHROPIC_API_KEY` table row now reads `no` (not `yes`).
- The parenthetical below the table now mentions Claude Code CLI auth and references `claude auth status`.
- No other lines changed.

- [ ] **Step 6: Stage (or commit) the README changes**

```bash
git add README.md
```

If committing:

```bash
git commit -m "$(cat <<'EOF'
docs(readme): mark ANTHROPIC_API_KEY optional

Updates the Required column to "no", adds an explanatory sentence about
Claude Code CLI auth fallback (keychain on macOS, ~/.claude/.credentials.json
elsewhere), and drops the env-var prefix from the streamable-http daemon
example since auth resolution is the same as for stdio.

Spec: docs/superpowers/specs/2026-05-08-anthropic-api-key-optional-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: End-to-end verification

**Files:** None modified. Verification only.

**Why this matters:** The test suite covers the unit-level contract for `check_env` and `format_doctor_report`. End-to-end verification confirms the full `harness-mcp doctor` flow renders the new WARN line correctly when the env var is missing, and the OK line when it's set — the user-observable behavior that motivated the change.

- [ ] **Step 1: Lint, format, type check, full test suite**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -k 'not smoke'
```

Expected: all pass. If any fail, stop and investigate; do not proceed to manual verification with broken checks.

- [ ] **Step 2: Direct unit verification of `check_env` (no SDK calls, no harness startup)**

```bash
ANTHROPIC_API_KEY=sk-ant-test uv run python -c "from harness_mcp.prereqs import check_env; print(check_env())"
```

Expected: `('OK', 'env: ANTHROPIC_API_KEY is set')`.

```bash
unset ANTHROPIC_API_KEY; uv run python -c "import os; os.environ.pop('ANTHROPIC_API_KEY', None); from harness_mcp.prereqs import check_env; print(check_env())"
```

Expected: `('WARN', 'env: ANTHROPIC_API_KEY not set; relying on Claude Code CLI auth (keychain or ~/.claude/.credentials.json). Verify with \\'claude auth status\\'.')`.

(The `os.environ.pop` is belt-and-suspenders — `unset` may not propagate to subprocesses depending on shell.)

- [ ] **Step 3: Direct unit verification of `format_doctor_report` rendering**

```bash
uv run python -c "
from harness_mcp.prereqs import DoctorReport, format_doctor_report
r = DoctorReport()
r.add('paths', 'OK', 'home=/tmp/h')
r.add('env', 'WARN', 'env: ANTHROPIC_API_KEY not set; relying on Claude Code CLI auth')
r.add('codex', 'FAIL', 'binary not found')
print(format_doctor_report(r))
"
```

Expected output:

```
OK   paths: home=/tmp/h
WARN env: env: ANTHROPIC_API_KEY not set; relying on Claude Code CLI auth
FAIL codex: binary not found
```

The three markers (`OK  `, `WARN`, `FAIL`) are all 4 characters wide; column alignment is preserved.

- [ ] **Step 4: Optional — full `harness-mcp doctor` smoke**

This step requires the harness's other prereqs (Codex CLI, MCP servers, skills) to be fully wired up on the operator's machine. If any of those are missing, the doctor will FAIL on a different check, masking the env-line behavior we want to verify.

If the operator's environment is fully configured, run:

```bash
ANTHROPIC_API_KEY=sk-ant-test uv run harness-mcp doctor
```

Expected: an `OK` line for env among the other OK lines.

```bash
unset ANTHROPIC_API_KEY; uv run harness-mcp doctor
```

Expected: a `WARN env: env: ANTHROPIC_API_KEY not set; relying on Claude Code CLI auth (keychain or ~/.claude/.credentials.json). Verify with 'claude auth status'.` line, with all other checks passing and the doctor exiting 0.

If the doctor exits non-zero on the env line, the WARN softening did not take effect — review the `run_prereqs` change in Task 2 Step 4.

Skip this step if other prereqs aren't configured locally; Step 2 + Step 3 cover the unit-level guarantees.

---

## Out of Scope (locked in by the spec)

These are explicitly NOT part of this plan and should be rejected if they appear during implementation:

- Updates to `PROMPT.md` (spec §7).
- Probing Claude Code CLI auth (`~/.claude/.credentials.json`, macOS keychain via `security`, `claude auth status` shelling) inside the harness (spec §7).
- Removing `check_env` entirely (spec §7).
- Refactoring other prereq checks (`check_codex_binary`, `probe_codex_sdk_shape`, etc.) to a `PrereqStatus` enum or to clean up their pre-existing redundant detail strings (spec §7).
- Adding new auth pathways like Bedrock, Vertex, or manual `CLAUDE_CODE_OAUTH_TOKEN` threading (spec §7).
- Changes to `Required MCP servers`, `Required skills`, `Quickstart`, or any README section other than `Required environment variables` and the streamable-http example in step 4.
