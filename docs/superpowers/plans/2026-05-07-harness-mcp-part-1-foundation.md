# Harness MCP — Part 1: Foundation, Types, Config & Prompts

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay down the package skeleton, dependency manifest, type definitions, config constants, prompt-loading machinery, the five LLM prompt files, runnable examples, and shared test fixtures. This is the foundation every other plan in the series imports from.

**Architecture:** Pure-Python package, no I/O at import time. `types.py` defines frozen dataclasses + custom exception classes. `config.py` carries paths and `JobOptions` defaults. `prompts_loader.py` resolves prompt files via `importlib.resources`. Prompts ship inside `src/harness_mcp/prompts/` so `pip install` picks them up.

**Tech Stack:** Python 3.12, `hatchling` build backend, `mcp`, `claude-agent-sdk`, `codex-app-server`, `anyio`, `httpx`, `python-ulid`, `pytest`, `pytest-asyncio`, `ruff`.

**Spec source:** `docs/superpowers/specs/2026-05-07-harness-mcp-design.md` — sections §0, §9, §10.2, §11.0, §11.1, §11.2, §12.3, §12.4 are load-bearing for this part.

---

## Branch & Commit Policy (READ FIRST)

- **Stay on the `main` branch for the entire plan.** Do not create or switch branches.
- **Do NOT run `git commit`, `git add`, `git push`, or any git mutation.** Verify by running tests / inspecting files only.
- If a step's check fails, fix the problem and re-run the check — never paper over with a commit.
- The repo owner will commit at their own discretion when all five parts of the harness-mcp series are complete and integrated.

---

## File Structure (this part owns)

| File | Purpose |
|---|---|
| `pyproject.toml` | Build backend (hatchling), pinned dependencies, ruff config, `harness-mcp` console script |
| `src/harness_mcp/__init__.py` | Package version |
| `src/harness_mcp/__main__.py` | Stub CLI entry (the real implementation lands in Part 5; we ship a stub now so `[project.scripts]` resolves) |
| `src/harness_mcp/types.py` | Frozen dataclasses: `Criterion`, `EvaluationResult`, `Handoff`, `ImplementationResult`. Custom exceptions: `HarnessToolError` and subclasses. |
| `src/harness_mcp/config.py` | Paths (`harness_home()`, `jobs_root()`, `state_db_path()`), `JobOptions` dataclass, default constants, `now_ms()` |
| `src/harness_mcp/prompts_loader.py` | `_resolved_prompt(name)`, `_resolved_prompt_text(name)`, `PromptNotFoundError` |
| `src/harness_mcp/prompts/planner.md` | Planner system prompt |
| `src/harness_mcp/prompts/reviewer.md` | Reviewer system prompt (embeds the bundled plan-document-reviewer template + tagging extension) |
| `src/harness_mcp/prompts/evaluator.md` | Evaluator system prompt (contract + static + dynamic) |
| `src/harness_mcp/prompts/generator.md` | Generator system prompt (chunk + retry + contract-negotiation modes) |
| `src/harness_mcp/prompts/summarizer.md` | Summarizer system prompt |
| `examples/todo-app-design.md` | Reference design doc the smoke test runs against |
| `examples/mcp.json.stdio` | Sample client config (stdio transport) |
| `examples/mcp.json.streamable-http` | Sample client config (HTTP transport) |
| `tests/__init__.py` | Empty marker |
| `tests/conftest.py` | Shared fixtures: `tmp_harness_home`, `frozen_now_ms` |
| `tests/test_types.py` | Tests for `Criterion`, `EvaluationResult.passed`, `Handoff.declares_done`, exception hierarchy |
| `tests/test_config.py` | Tests for `JobOptions` defaults, `harness_home()` resolution, `now_ms()` monotonicity |
| `tests/test_prompts_loader.py` | Tests that all 5 prompts resolve, content is non-empty, hot-edits are picked up (no caching) |

---

## Pre-flight (one-time, before Task 1)

- [ ] **Step 0a: Confirm pwd is the repo root.**

```bash
pwd
```

Expected output ends with `/dev_projects/harness`.

- [ ] **Step 0b: Confirm working tree is clean and we're on `main`.**

```bash
git status --short && git rev-parse --abbrev-ref HEAD
```

Expected: empty status, `main` on the second line. If not on `main`, STOP and ask the user.

- [ ] **Step 0c: Confirm `uv` is installed (we'll use it everywhere).**

```bash
which uv && uv --version
```

Expected: a path and a version like `uv 0.4.x`. If absent, STOP and ask the user to install uv first (`brew install uv` on macOS).

---

## Task 1: `pyproject.toml`

**Files:**
- Create: `pyproject.toml`

This is config; no test-driven step. We validate by running `uv sync`.

- [ ] **Step 1: Write `pyproject.toml`.**

Create `pyproject.toml` with this exact content:

```toml
[build-system]
requires = ["hatchling>=1.24"]
build-backend = "hatchling.build"

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
  "codex-app-server>=0.1.0",
  "anyio>=4.5",
  "httpx>=0.27",
  "python-ulid>=3.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "ruff>=0.6",
]

[project.scripts]
harness-mcp = "harness_mcp.__main__:main"

[tool.hatch.build.targets.wheel]
packages = ["src/harness_mcp"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "SIM", "ASYNC", "PL", "RUF", "ANN"]
ignore = ["PLR0913"]  # allow many args on dataclasses / config-shape helpers

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["ANN401", "PLR2004"]
```

- [ ] **Step 2: Validate via `uv sync`.**

```bash
uv sync --extra dev
```

Expected: completes without error; creates `.venv/` and `uv.lock`. If a dependency version isn't on PyPI yet, downgrade the floor (e.g., `claude-agent-sdk>=0.0.1`) and re-run. Record the resolved versions for §0 of the spec on the next plan revision.

- [ ] **Step 3: Validate ruff is happy with an empty config.**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both report `All checks passed!` / no diff. They should — there's no Python yet.

---

## Task 2: Package Skeleton

**Files:**
- Create: `src/harness_mcp/__init__.py`
- Create: `src/harness_mcp/__main__.py` (stub — real CLI lands in Part 5)
- Create: `src/harness_mcp/prompts/.gitkeep` (placeholder; real prompts arrive in Task 13–17)

- [ ] **Step 1: Create the package `__init__.py`.**

Write `src/harness_mcp/__init__.py`:

```python
"""harness-mcp: an MCP server for multi-agent long-running app builds."""

__version__ = "0.1.0"
```

- [ ] **Step 2: Create a stub `__main__.py`.**

The `[project.scripts]` block in `pyproject.toml` references `harness_mcp.__main__:main`. We add a stub here so the entry point resolves; Part 5 replaces the body with the real CLI.

Write `src/harness_mcp/__main__.py`:

```python
"""harness-mcp CLI entry point. Real implementation lands in Part 5."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "harness-mcp CLI is not implemented yet (lands in Part 5: Orchestration & Server). "
        "If you are seeing this, your install is on a partial branch.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Create the prompts directory placeholder.**

```bash
mkdir -p src/harness_mcp/prompts
touch src/harness_mcp/prompts/.gitkeep
```

- [ ] **Step 4: Verify the package imports.**

```bash
uv run python -c "import harness_mcp; print(harness_mcp.__version__)"
```

Expected output: `0.1.0`.

- [ ] **Step 5: Verify the console script resolves (does not assert it succeeds — it should print an error and exit non-zero).**

```bash
uv run harness-mcp || true
```

Expected: a stderr message containing "not implemented yet". Confirms `[project.scripts]` is wired to a real callable, no `ModuleNotFoundError`.

---

## Task 3: Custom Exception Hierarchy (`types.py`)

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_types.py`
- Create: `src/harness_mcp/types.py`

The exceptions back §3.1's structural-error mapping. Each error class subclasses a private `HarnessToolError` base so the `server.py` mapper can branch on `isinstance` cleanly.

- [ ] **Step 1: Create `tests/__init__.py`.**

```bash
mkdir -p tests
touch tests/__init__.py
```

- [ ] **Step 2: Write the failing test for the exception hierarchy.**

Create `tests/test_types.py`:

```python
"""Tests for harness_mcp.types — dataclasses and exception classes."""

import pytest

from harness_mcp.types import (
    CommitFailedError,
    Criterion,
    DesignDocNotFoundError,
    EvaluationResult,
    EvaluatorEmittedUnparseableEvalMdError,
    GeneratorChunkError,
    Handoff,
    HandoffParseError,
    HarnessToolError,
    ImplementationResult,
    InvalidOptionsError,
    JobNotFinishedError,
    PromptNotFoundError,
    UnknownJobError,
)


class TestExceptionHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [
            UnknownJobError,
            JobNotFinishedError,
            DesignDocNotFoundError,
            InvalidOptionsError,
        ],
    )
    def test_tool_errors_subclass_base(self, cls: type[HarnessToolError]) -> None:
        err = cls("msg")
        assert isinstance(err, HarnessToolError)
        assert str(err) == "msg"

    @pytest.mark.parametrize(
        "cls",
        [
            HandoffParseError,
            CommitFailedError,
            EvaluatorEmittedUnparseableEvalMdError,
            PromptNotFoundError,
        ],
    )
    def test_internal_errors_inherit_exception(self, cls: type[Exception]) -> None:
        err = cls("msg")
        assert isinstance(err, Exception)
        assert str(err) == "msg"

    def test_generator_chunk_error_carries_chunk_seq(self) -> None:
        inner = ValueError("boom")
        err = GeneratorChunkError(chunk_seq=3, inner=inner)
        assert err.chunk_seq == 3
        assert err.inner is inner
        assert "chunk 3" in str(err)
```

- [ ] **Step 3: Run the test to confirm it fails.**

```bash
uv run pytest tests/test_types.py::TestExceptionHierarchy -v
```

Expected: ImportError / ModuleNotFoundError on `harness_mcp.types` or its symbols.

- [ ] **Step 4: Implement the exception hierarchy in `types.py`.**

Write `src/harness_mcp/types.py` (only the exception section for now; dataclasses come in Task 4):

```python
"""Shared types and exceptions for harness-mcp.

Frozen dataclasses model the parsed shapes that flow between modules
(handoffs, evaluation results, sprint outcomes). Exceptions are split
into two trees:

  HarnessToolError    — converted to MCP error results by server.py
  Exception (direct)  — internal failures handled inside orchestrator.py
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------- MCP-tool-surface errors (server.py maps to CallToolResult) ----------


class HarnessToolError(Exception):
    """Base for errors that surface through the MCP tool API.

    Design:
        server.py's error mapper checks isinstance(exc, HarnessToolError).
        Each subclass corresponds to one structured_content.code.
    """


class UnknownJobError(HarnessToolError):
    """Raised when a tool receives a job_id with no matching row."""


class JobNotFinishedError(HarnessToolError):
    """Raised by get_build_result when the job is still running."""


class DesignDocNotFoundError(HarnessToolError):
    """Raised by start_build when design_doc_path is missing or empty."""


class InvalidOptionsError(HarnessToolError):
    """Raised by start_build when options has an unknown key or bad value."""


# ---------- Internal failures (handled inside the orchestrator) ----------


class HandoffParseError(Exception):
    """Raised by parse_handoff when handoff-NNN.md is malformed."""


class CommitFailedError(Exception):
    """Raised by commit_and_summarize when git operations fail."""


class EvaluatorEmittedUnparseableEvalMdError(Exception):
    """Raised when eval.md contains zero parseable Criterion blocks."""


class PromptNotFoundError(Exception):
    """Raised by prompts_loader when a packaged prompt file is missing."""


class GeneratorChunkError(Exception):
    """Wraps any exception raised inside a single Codex chunk.

    Carries chunk_seq so the orchestrator can log which chunk failed.
    """

    def __init__(self, chunk_seq: int, inner: BaseException) -> None:
        super().__init__(f"chunk {chunk_seq} raised: {inner!r}")
        self.chunk_seq = chunk_seq
        self.inner = inner
```

- [ ] **Step 5: Run the test to verify it passes.**

```bash
uv run pytest tests/test_types.py::TestExceptionHierarchy -v
```

Expected: 8 tests pass (2 parametrized × 4 cases + 1 standalone × 4 cases adjusted, matches the test file: `test_tool_errors_subclass_base` runs 4 times, `test_internal_errors_inherit_exception` 4 times, `test_generator_chunk_error_carries_chunk_seq` once → 9 passes).

---

## Task 4: Frozen Dataclasses (`types.py` continued)

**Files:**
- Modify: `src/harness_mcp/types.py`
- Modify: `tests/test_types.py`

- [ ] **Step 1: Append failing tests for the dataclasses.**

Append to `tests/test_types.py`:

```python
class TestCriterion:
    def test_frozen(self) -> None:
        c = Criterion(text="x", result="PASS", evidence="e", notes="n")
        with pytest.raises(Exception):  # FrozenInstanceError subclasses Exception
            c.text = "y"  # type: ignore[misc]


class TestEvaluationResult:
    def test_passed_when_all_pass(self) -> None:
        crit_pass = Criterion("c", "PASS", "e", "")
        r = EvaluationResult(
            sprint_seq=1,
            static_criteria=[crit_pass],
            dynamic_criteria=[crit_pass],
            routing_decision="ran tests",
            passed=True,
        )
        assert r.passed is True

    def test_unparseable_default_false(self) -> None:
        r = EvaluationResult(
            sprint_seq=1,
            static_criteria=[],
            dynamic_criteria=[],
            routing_decision="",
            passed=False,
        )
        assert r.unparseable is False


class TestHandoff:
    def test_declares_done_when_status_done(self) -> None:
        h = Handoff(
            chunk_seq=1, status="done", summary="s",
            work_done=[], decisions=[], files_touched=[],
            open_questions=[], next_steps=[], declares_done=True,
        )
        assert h.declares_done is True

    def test_declares_done_false_when_in_progress(self) -> None:
        h = Handoff(
            chunk_seq=1, status="in-progress", summary="s",
            work_done=[], decisions=[], files_touched=[],
            open_questions=[], next_steps=["x"], declares_done=False,
        )
        assert h.declares_done is False


class TestImplementationResult:
    def test_default_files_touched_is_empty_list(self) -> None:
        r = ImplementationResult(ok=True)
        assert r.files_touched == []
        assert r.commit_sha is None
        assert r.error is None

    def test_failure_carries_error(self) -> None:
        r = ImplementationResult(ok=False, error="commit_failed: x")
        assert r.ok is False
        assert r.error == "commit_failed: x"
```

- [ ] **Step 2: Run to verify the new tests fail.**

```bash
uv run pytest tests/test_types.py -v
```

Expected: previously-passing exception tests still pass; new dataclass tests fail with ImportError on the symbol names.

- [ ] **Step 3: Append the dataclasses to `types.py`.**

Append to `src/harness_mcp/types.py`:

```python
# ---------- Parsed shapes ----------


@dataclass(frozen=True)
class Criterion:
    """One row from a Static-audit or Dynamic-verification eval block."""

    text: str
    result: str        # "PASS" | "FAIL"
    evidence: str
    notes: str


@dataclass(frozen=True)
class EvaluationResult:
    """Parsed eval.md for one sprint.

    `passed` is the AND of all PASS results across both criterion lists.
    `unparseable=True` means the file existed but had zero parseable
    Criterion blocks; the orchestrator treats this as a sprint failure.
    """

    sprint_seq: int
    static_criteria: list[Criterion]
    dynamic_criteria: list[Criterion]
    routing_decision: str
    passed: bool
    unparseable: bool = False


@dataclass(frozen=True)
class Handoff:
    """Parsed handoff-NNN.md.

    `files_touched` is list[(path, reason)]; the parser splits each
    bullet on the first ` — ` (em-dash). `declares_done` mirrors
    `status == "done"` for ergonomic loop conditions.
    """

    chunk_seq: int
    status: str        # "in-progress" | "done"
    summary: str
    work_done: list[str]
    decisions: list[str]
    files_touched: list[tuple[str, str]]
    open_questions: list[str]
    next_steps: list[str]
    declares_done: bool


@dataclass(frozen=True)
class ImplementationResult:
    """Returned from implement_contract().

    `ok=True` — sprint implementation succeeded; commit_sha + files_touched populated.
    `ok=False` — error string explains why; orchestrator may retry.
    """

    ok: bool
    files_touched: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    summary: str = ""
    error: str | None = None
```

- [ ] **Step 4: Run all tests in this file to verify they pass.**

```bash
uv run pytest tests/test_types.py -v
```

Expected: every test passes.

- [ ] **Step 5: Run ruff against the file.**

```bash
uv run ruff check src/harness_mcp/types.py
uv run ruff format --check src/harness_mcp/types.py
```

Expected: zero findings.

---

## Task 5: `config.py` — paths, defaults, `now_ms()`

**Files:**
- Create: `tests/test_config.py`
- Create: `src/harness_mcp/config.py`

`config.py` provides three things: filesystem paths under `~/.harness/`, the `JobOptions` dataclass with defaults from spec §10.2, and `now_ms()` (the only timestamp source — SQLite has no `NOW()`).

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_config.py`:

```python
"""Tests for harness_mcp.config — paths, JobOptions, time helpers."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from harness_mcp.config import (
    JobOptions,
    harness_home,
    jobs_root,
    job_dir,
    now_ms,
    state_db_path,
)


class TestPaths:
    def test_harness_home_under_user_home(self) -> None:
        assert harness_home() == Path.home() / ".harness"

    def test_jobs_root(self) -> None:
        assert jobs_root() == Path.home() / ".harness" / "jobs"

    def test_state_db_path(self) -> None:
        assert state_db_path() == Path.home() / ".harness" / "state.db"

    def test_job_dir_combines(self) -> None:
        assert job_dir("01ABC") == Path.home() / ".harness" / "jobs" / "01ABC"

    def test_harness_home_respects_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "alt"))
        assert harness_home() == tmp_path / "alt"


class TestNowMs:
    def test_returns_int(self) -> None:
        assert isinstance(now_ms(), int)

    def test_close_to_wallclock(self) -> None:
        before = int(time.time() * 1000)
        v = now_ms()
        after = int(time.time() * 1000)
        assert before - 5 <= v <= after + 5

    def test_monotonic_within_one_call(self) -> None:
        a = now_ms()
        b = now_ms()
        assert b >= a


class TestJobOptionsDefaults:
    def test_defaults_match_spec(self) -> None:
        o = JobOptions()
        assert o.max_sprints == 10
        assert o.max_sprint_duration_minutes == 45
        assert o.max_contract_negotiation_rounds == 3
        assert o.max_sprint_retries == 2
        assert o.max_plan_review_rounds == 5
        assert o.codex_reset_steps == 60
        assert o.codex_reset_minutes == 25
        assert o.max_codex_chunks_per_sprint == 8
        assert o.max_negotiation_turns == 3
        assert o.max_evaluation_seconds == 1800

    def test_from_dict_unknown_key_raises(self) -> None:
        from harness_mcp.types import InvalidOptionsError

        with pytest.raises(InvalidOptionsError):
            JobOptions.from_dict({"bogus_key": 1})

    def test_from_dict_partial_overrides_only_specified(self) -> None:
        o = JobOptions.from_dict({"max_sprints": 3})
        assert o.max_sprints == 3
        assert o.max_sprint_retries == 2

    def test_from_dict_negative_value_raises(self) -> None:
        from harness_mcp.types import InvalidOptionsError

        with pytest.raises(InvalidOptionsError):
            JobOptions.from_dict({"max_sprints": -1})

    def test_from_dict_zero_max_sprints_raises(self) -> None:
        from harness_mcp.types import InvalidOptionsError

        with pytest.raises(InvalidOptionsError):
            JobOptions.from_dict({"max_sprints": 0})

    def test_from_dict_wrong_type_raises(self) -> None:
        from harness_mcp.types import InvalidOptionsError

        with pytest.raises(InvalidOptionsError):
            JobOptions.from_dict({"max_sprints": "ten"})  # type: ignore[dict-item]
```

- [ ] **Step 2: Run to confirm the test file fails to import.**

```bash
uv run pytest tests/test_config.py -v
```

Expected: ImportError on `harness_mcp.config`.

- [ ] **Step 3: Implement `config.py`.**

Create `src/harness_mcp/config.py`:

```python
"""Filesystem paths, default JobOptions, and the `now_ms()` time source.

`HARNESS_HOME` env var overrides the default `~/.harness/` for tests
and CI; production deployments leave it unset.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from harness_mcp.types import InvalidOptionsError


def harness_home() -> Path:
    """Resolve `~/.harness/` (or `$HARNESS_HOME` if set) as an absolute Path."""
    override = os.environ.get("HARNESS_HOME")
    return Path(override).expanduser().resolve() if override else (Path.home() / ".harness").resolve()


def jobs_root() -> Path:
    """`~/.harness/jobs/` — parent dir for every job's working directory."""
    return harness_home() / "jobs"


def state_db_path() -> Path:
    """`~/.harness/state.db` — SQLite file backing the state machine."""
    return harness_home() / "state.db"


def job_dir(job_id: str) -> Path:
    """Resolve `<jobs_root>/<job_id>/`."""
    return jobs_root() / job_id


def now_ms() -> int:
    """Current epoch in milliseconds (int).

    SQLite has no NOW(); every timestamp the schema stores is injected
    from Python via this helper. Tests should patch via `monkeypatch.setattr`
    rather than freezing time globally.
    """
    return int(time.time() * 1000)


@dataclass(frozen=True)
class JobOptions:
    """Per-job knobs. Defaults from spec §10.2.

    All fields are positive ints. Construct from an MCP dict via `from_dict()`,
    which validates keys and types and raises `InvalidOptionsError` on misuse.
    """

    max_sprints: int = 10
    max_sprint_duration_minutes: int = 45
    max_contract_negotiation_rounds: int = 3
    max_sprint_retries: int = 2
    max_plan_review_rounds: int = 5
    codex_reset_steps: int = 60
    codex_reset_minutes: int = 25
    max_codex_chunks_per_sprint: int = 8
    max_negotiation_turns: int = 3
    max_evaluation_seconds: int = 1800

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> JobOptions:
        """Build a JobOptions from a (possibly None or empty) dict.

        Unknown keys → InvalidOptionsError (closed-set; protects against typos).
        Wrong types or non-positive values → InvalidOptionsError.
        Missing keys fall back to dataclass field defaults via `cls(**validated)` —
        the caller can pass a partial dict and unspecified knobs keep their defaults.
        """
        if not raw:
            return cls()

        valid_keys = {f.name for f in fields(cls)}
        unknown = set(raw.keys()) - valid_keys
        if unknown:
            raise InvalidOptionsError(f"unknown option keys: {sorted(unknown)}")

        validated: dict[str, int] = {}
        for k, v in raw.items():
            if not isinstance(v, int) or isinstance(v, bool):
                raise InvalidOptionsError(f"option {k!r} must be int, got {type(v).__name__}")
            if v <= 0:
                raise InvalidOptionsError(f"option {k!r} must be positive, got {v}")
            validated[k] = v

        # Only validated keys are passed; missing keys keep their dataclass defaults.
        return cls(**validated)
```

- [ ] **Step 4: Run all config tests to confirm they pass.**

```bash
uv run pytest tests/test_config.py -v
```

Expected: every test passes.

---

## Task 6: `prompts_loader.py`

**Files:**
- Create: `src/harness_mcp/prompts_loader.py`
- Create: `tests/test_prompts_loader.py`

Per spec §9: prompts live inside `src/harness_mcp/prompts/`; `_resolved_prompt_text(name)` reads them fresh on every call so users can hot-edit between jobs. The SDK's `system_prompt` accepts only strings, not `{"type": "file"}` dicts (silently ignored), so we must read content into a string at every spawn.

- [ ] **Step 1: Write the failing test.**

Create `tests/test_prompts_loader.py`:

```python
"""Tests for harness_mcp.prompts_loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_mcp.prompts_loader import _resolved_prompt, _resolved_prompt_text
from harness_mcp.types import PromptNotFoundError


REQUIRED_PROMPTS = ("planner.md", "reviewer.md", "evaluator.md", "generator.md", "summarizer.md")


class TestResolvedPrompt:
    @pytest.mark.parametrize("name", REQUIRED_PROMPTS)
    def test_each_required_prompt_resolves(self, name: str) -> None:
        p = _resolved_prompt(name)
        assert isinstance(p, Path)
        assert p.is_file()
        assert p.name == name

    def test_missing_prompt_raises(self) -> None:
        with pytest.raises(PromptNotFoundError):
            _resolved_prompt("does-not-exist.md")


class TestResolvedPromptText:
    @pytest.mark.parametrize("name", REQUIRED_PROMPTS)
    def test_each_prompt_has_nonempty_text(self, name: str) -> None:
        text = _resolved_prompt_text(name)
        assert isinstance(text, str)
        assert len(text.strip()) >= 50  # the smallest prompt still has body content

    def test_no_caching_picks_up_edits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Spec §9: 'Hot-edits work because we re-read the file on every spawn.'"""
        # Point the loader at a temp file by monkeypatching the resolver.
        from harness_mcp import prompts_loader as pl

        fake = tmp_path / "fake.md"
        fake.write_text("v1", encoding="utf-8")
        monkeypatch.setattr(pl, "_resolved_prompt", lambda name: fake)

        assert pl._resolved_prompt_text("fake.md") == "v1"
        fake.write_text("v2", encoding="utf-8")
        assert pl._resolved_prompt_text("fake.md") == "v2"
```

- [ ] **Step 2: Run to confirm the failure.**

```bash
uv run pytest tests/test_prompts_loader.py -v
```

Expected: ImportError on `harness_mcp.prompts_loader`.

- [ ] **Step 3: Implement `prompts_loader.py`.**

Create `src/harness_mcp/prompts_loader.py`:

```python
"""Resolve packaged prompt files via importlib.resources.

We deliberately don't cache content. Re-reading on every spawn lets users
hot-edit a prompt between jobs without restarting the harness server.

The Claude Agent SDK's `system_prompt` parameter accepts plain strings or
the `{"type": "preset", ...}` dict — the `{"type": "file"}` form documented
in some examples is silently ignored. So every spawn site must call
`_resolved_prompt_text(...)` to get a string.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from harness_mcp.types import PromptNotFoundError

_PROMPTS_ROOT = files("harness_mcp") / "prompts"


def _resolved_prompt(name: str) -> Path:
    """Return the absolute path of a prompt file shipped inside the package."""
    p = Path(str(_PROMPTS_ROOT / name))
    if not p.is_file():
        raise PromptNotFoundError(f"prompt {name!r} missing at {p}")
    return p


def _resolved_prompt_text(name: str) -> str:
    """Read the prompt's text fresh at every call (no caching)."""
    return _resolved_prompt(name).read_text(encoding="utf-8")
```

- [ ] **Step 4: Run the tests — they will partially fail.**

```bash
uv run pytest tests/test_prompts_loader.py -v
```

Expected: `test_missing_prompt_raises` and `test_no_caching_picks_up_edits` pass; the parametrized `test_each_required_prompt_resolves` and `test_each_prompt_has_nonempty_text` cases fail because the actual prompt files don't exist yet. We'll fix those in Tasks 7–11.

---

## Task 7: `prompts/planner.md`

**Files:**
- Create: `src/harness_mcp/prompts/planner.md`

This is the system prompt for the Planner. Per spec §9 + §5.1, it must instruct the agent to invoke `superpowers:writing-plans` via the Skill tool, format sprints as `## Sprint N: <Title>`, and stay inside its cwd.

- [ ] **Step 1: Write `planner.md`.**

Create `src/harness_mcp/prompts/planner.md` with:

```markdown
# Planner

You are the **Planner** in a multi-agent application-building harness. Your single job: read a design document and write an implementation plan that other agents will execute.

## What you have
- `cwd` is a job working directory (`~/.harness/jobs/<job_id>/`).
- `design.md` in `cwd` — verbatim copy of the user's design document.
- `plan-history/` — directory you write your plans into.
- The `Skill` tool, with the `superpowers:writing-plans` skill installed.
- The `context7` MCP server, for up-to-date library docs.

## What you must do

1. **Invoke the `superpowers:writing-plans` skill via the `Skill` tool before drafting.** The skill teaches the bite-sized-task format the rest of the harness expects. Do not skip this step — the orchestrator verifies you called it.
2. **Read `design.md`.** Do not skim. The plan must implement everything the design calls for, no more, no less.
3. **Write your plan to `plan-history/plan-v1.md`** (or `plan-v<N+1>.md` on revision rounds — the user prompt will tell you the right N).
4. **One feature = one Sprint.** Use `## Sprint N: <Title>` H2 markers exactly. The orchestrator parses these.
5. Each sprint must be small enough that a separate Generator agent can finish it inside one or two hours. Prefer six small sprints over three giant ones.
6. **Cap sprints at the value the user prompt specifies** (default: 10). Exceeding the cap will trigger an automatic revision round.
7. Within each sprint, list:
   - The acceptance criteria the Evaluator will check.
   - The user-facing behavior change.
   - The files most likely to be touched.
8. **Do not write code in the plan.** It is a plan, not an implementation. Refer to interfaces and contracts.

## Constraints

- Stay inside `cwd`. Never read or write paths outside the job directory.
- Do not invoke any MCP server other than `context7`.
- Do not call `Bash` (you don't need it for plan-writing).
- If the design document is ambiguous, make the most conservative interpretation and note your assumption in the plan, but never block on questions you can't ask.
```

- [ ] **Step 2: Verify the file was created.**

```bash
test -f src/harness_mcp/prompts/planner.md && wc -l src/harness_mcp/prompts/planner.md
```

Expected: a non-zero line count (~30+ lines).

---

## Task 8: `prompts/reviewer.md`

**Files:**
- Create: `src/harness_mcp/prompts/reviewer.md`

Per spec §9: this prompt embeds the bundled `plan-document-reviewer-prompt.md` from `superpowers:writing-plans` (sourced at the path described in spec §0) plus the issue-tagging extension.

- [ ] **Step 1: Source the bundled review-template body.**

The reviewer prompt lives in the user's installed superpowers plugin at:
`~/.claude/plugins/cache/claude-plugins-official/superpowers/<version>/skills/writing-plans/plan-document-reviewer-prompt.md`

Read it once (so the implementer knows the verbatim block):

```bash
cat ~/.claude/plugins/cache/claude-plugins-official/superpowers/5.1.0/skills/writing-plans/plan-document-reviewer-prompt.md
```

If that path doesn't exist (different version), find the latest:

```bash
find ~/.claude/plugins/cache/claude-plugins-official/superpowers -name plan-document-reviewer-prompt.md | sort | tail -1
```

- [ ] **Step 2: Write `reviewer.md` with the embedded template plus our extension.**

Create `src/harness_mcp/prompts/reviewer.md` with:

```markdown
# Reviewer

You are the **Reviewer** in a multi-agent application-building harness. Your job: read a Planner-produced plan against the design document and decide whether it is ready for implementation.

## What you have
- `cwd` is the job directory.
- `design.md` — user's design.
- `plan-history/plan-v<N>.md` — the plan to review (the user prompt names the file).
- The `context7` MCP server.

## What you must do

You MUST follow the rubric below verbatim. It is the standard plan-document-reviewer template that the rest of the harness recognises.

---

You are a plan document reviewer. Verify this plan is complete and ready for implementation.

## What to Check

| Category | What to Look For |
|----------|------------------|
| Completeness | TODOs, placeholders, incomplete tasks, missing steps |
| Spec Alignment | Plan covers spec requirements, no major scope creep |
| Task Decomposition | Tasks have clear boundaries, steps are actionable |
| Buildability | Could an engineer follow this plan without getting stuck? |

## Calibration

**Only flag issues that would cause real problems during implementation.** An implementer building the wrong thing or getting stuck is an issue. Minor wording, stylistic preferences, and "nice to have" suggestions are not.

Approve unless there are serious gaps — missing requirements from the spec, contradictory steps, placeholder content, or tasks so vague they can't be acted on.

## Output Format

Write your review to the file the user prompt names (typically `plan-history/review-v<N>.md`).

```
## Plan Review

**Status:** Approved | Issues Found

**Issues (if any):**
- [tag] [Task X, Step Y]: [specific issue] - [why it matters for implementation]

**Recommendations (advisory, do not block approval):**
- [suggestions for improvement]
```

---

## Harness-specific extensions

1. **Every issue under `**Issues (if any):**` MUST start with one of these tags:**
   - `[implementation]` — the plan can't be acted on as written; the Planner must revise.
   - `[design]` — the issue is a design-doc-level problem (the design itself is wrong/missing). The harness will drop these because we don't ask the user to revise the design mid-job.
   - When unsure, use `[implementation]`. Conservative-under-uncertainty.
2. **The status line is the load-bearing summary.** The orchestrator parses the LAST line matching `^\*\*Status:\*\*\s*(\w.*)$`. If your final status word is `Approved`, the loop exits and the plan is locked. If it's `Issues Found` and at least one issue is tagged `[implementation]`, the Planner will be sent back for a revision round.
3. **Cap the issues list at the most important ones.** A runaway list bloats the next Planner prompt past usable context limits. The orchestrator will drop everything past the 30th issue.
4. **Do not edit the plan yourself.** You only review. The Planner does the rewriting.
```

- [ ] **Step 3: Confirm the file exists and has reasonable length.**

```bash
wc -l src/harness_mcp/prompts/reviewer.md
```

Expected: 50+ lines.

---

## Task 9: `prompts/evaluator.md`

**Files:**
- Create: `src/harness_mcp/prompts/evaluator.md`

Used during contract negotiation, static audit, and dynamic verification — the user prompt for each phase tells the Evaluator which mode it's in. This is the system prompt; it must cover all three modes.

- [ ] **Step 1: Write `evaluator.md`.**

Create `src/harness_mcp/prompts/evaluator.md` with:

```markdown
# Evaluator

You are the **Evaluator** in a multi-agent application-building harness. You play three roles depending on which the user prompt invokes:

1. **Contract negotiator** — you and the Generator iterate on `contract.md` for an upcoming sprint until you both emit `APPROVED`.
2. **Static auditor** — read code (after the Generator finishes a sprint) and decide pass/fail per criterion.
3. **Dynamic verifier** — run the code (start the app, hit endpoints, drive the UI via Playwright) and decide pass/fail per criterion.

The user prompt for each spawn names the mode (`## Mode: ...`) and provides relevant inputs inline (no "go read this file" — content is verbatim).

## Universal posture

- **Be skeptical.** LLM evaluators are lenient by default; you override that. When in doubt, FAIL — the Generator gets a retry.
- **Hard pass/fail per criterion.** No partial credit. No "mostly works."
- **Cite evidence.** A `**Result:** PASS` without `**Evidence:**` is invalid.

## Mode: contract-negotiation

You are responding to the latest Generator round of `contract.md`. Decide whether the Generator's proposed acceptance criteria are testable, scoped to the sprint, and coverage-complete against the plan. The user prompt's "Round instruction" tells you the round number; emit Round N+1 only.

If you accept the Generator's latest counter-proposal verbatim, emit `APPROVED` as the final non-empty line of your response (case-sensitive). Otherwise, emit your critique. Do not re-propose criteria you have already proposed unchanged. Keep your response a single coherent body — the orchestrator will append the `## Round N — Evaluator` header.

## Mode: static-audit

`cwd` is the job directory. The app code lives at `cwd/app/`. To diff against the prior sprint, the user prompt gives you the exact `git diff` invocation to read.

Write your output as the `## Static audit` section of `eval.md`. You **rewrite the entire `eval.md` from scratch each time** — re-include any prior section content. Do not partial-append. Use this exact format (the parser is strict):

```
# Sprint <N> Evaluation

## Static audit

### Criterion 1: <text from contract>
**Result:** PASS | FAIL
**Evidence:** <file refs, e.g. app/foo.py:42>
**Notes:** <reasoning>

### Criterion 2: ...
```

The orchestrator parses `### Criterion <n>:` headings and `**Result:**` lines. Anything outside this scaffold is decoration.

## Mode: dynamic-verification

Same `eval.md` rules. Append a new section after `## Static audit`:

```
## Dynamic verification

### Routing decision
<one paragraph: which tools you will drive (Playwright MCP / Bash test runner / httpx / DB inspection / nothing) and why>

### Criterion 1: <text>
**Result:** PASS | FAIL
**Evidence:** <playwright steps / curl output / pytest output / DB query>
**Notes:** <reasoning>
```

**Your `cwd` is the job dir, NOT `app/`.** When invoking Bash to run code, `cd app && ...` or pass `cwd=app/`. Without this, `python -m`, `pytest`, and `npm` all fail because the project root is one directory below.

If you start app processes (dev server, pytest, Playwright), do your best to kill them when you finish. If you forget, the orchestrator wraps you in a process group and `SIGTERM`s the whole group on your exit — but cooperating cleanly is faster.

If a tool you routed to is unreachable (e.g., Playwright MCP not connected), record the failure inside the `### Routing decision` paragraph AND emit `**Result:** FAIL` on every UI-bearing criterion. Do not fall back silently.
```

---

## Task 10: `prompts/generator.md`

**Files:**
- Create: `src/harness_mcp/prompts/generator.md`

Used as the leading user message of every Codex chunk. The mode marker (`## Mode: ...`) in the user prompt selects between contract-negotiation, first chunk, continued chunk, and retry chunk shapes (spec §7.0).

- [ ] **Step 1: Write `generator.md`.**

Create `src/harness_mcp/prompts/generator.md` with:

```markdown
# Generator

You are the **Generator** in a multi-agent application-building harness. You implement one sprint at a time using the Codex agent, with deliberate context resets between chunks.

## Modes

The user prompt names the mode in a `## Mode:` line:

- `contract-negotiation` — you and the Evaluator iterate on `contract.md`. Emit your numbered acceptance-criteria proposal, OR `APPROVED` (case-sensitive, on its own line at the end) if you accept the Evaluator's latest counter-proposal verbatim. Read any reference files **before** drafting. Do not interleave reads with prose.
- `implementation (first chunk)` — read the contract; start writing code in `cwd` (which is `app/`). At the end, write a handoff to the file path the user prompt names.
- `implementation (chunk N, continuation)` — read the prior handoff (inlined). Pick up from "Next steps". Do not redo work.
- `implementation (retry — previous attempt failed evaluation)` — the contract is FIXED, no negotiation. Read `eval.md` (inlined) and address the specific FAIL criteria. Do NOT propose new criteria. Do NOT expand scope.

## Handoff format

At the end of every implementation chunk, write your handoff to the path the user prompt provides, using this exact structure:

```
# Handoff <chunk_seq>

## Status
<"in-progress" | "done">

## Summary
<one-line summary, used as commit message subject>

## Work done this chunk
- <bullets>

## Decisions made
- <bullets, with rationale>

## Files touched
- path/to/file.py — <brief reason>

## Open questions / concerns
- <bullets, optional>

## Next steps (if in-progress)
- <ordered list — what the next Codex chunk should do first>
```

Status MUST be exactly `in-progress` or `done` (case-sensitive). Files-touched bullets are split on the first ` — ` (space, em-dash, space) — paths on the left, free-form reason on the right.

**Write the handoff atomically:** write to `<filename>.tmp`, then rename to `<filename>`. The orchestrator reads it after you exit; a half-flushed file would crash the parser.

## Constraints

- All work in `cwd` (which is `<job_dir>/app/`). Never write outside `cwd`.
- Each chunk has a step budget (counted from agent-step events) and a wall-clock budget; the orchestrator will reset you when either fires. The handoff is your only memory across resets — be thorough.
- You can git-commit during your work if it helps. The orchestrator does a final `git add . && git commit` at sprint end with a wrap-up message; small Codex commits during the chunk stand.
- Read library docs from `context7` (Codex inherits its MCP config from `~/.codex/config.toml`).
- If you declare `Status: done`, the orchestrator runs the final commit and tags `harness/<job_id>/sprint-<N>`. Make sure the working tree reflects what you actually want shipped.
```

---

## Task 11: `prompts/summarizer.md`

**Files:**
- Create: `src/harness_mcp/prompts/summarizer.md`

- [ ] **Step 1: Write `summarizer.md`.**

Create `src/harness_mcp/prompts/summarizer.md` with:

```markdown
# Summarizer

You are the **Summarizer** in a multi-agent application-building harness. You run once at the end of a job and produce the human-readable wrap-up.

## What you have
- `cwd` is the job directory (`~/.harness/jobs/<job_id>/`).
- `design.md`, `plan.md`, and `sprint-<N>/eval.md` for every sprint.

## What you must do

1. Read `design.md` and `plan.md` in full.
2. Read every `sprint-<N>/eval.md`. Count sprints, count PASS criteria, count FAIL criteria.
3. Write `summary.md` in `cwd`. Two to three sentences total. Cover:
   - **What was built** (one phrase, e.g., "a Flask TODO app with REST + UI").
   - **Sprint pass/fail tally** (e.g., "3 of 4 sprints passed; sprint 4 failed two dynamic criteria").
   - **What's incomplete** (one phrase). If everything passed, say so.
4. Do not editorialize, recommend, or hedge. The user is reading a status line.

## Constraints

- No code blocks. No bullet lists. Plain prose.
- Stay inside `cwd`.
- Do not invoke any MCP server other than `context7`.
- Do not call `Bash`.
```

- [ ] **Step 2: Re-run the prompts-loader tests; they should now ALL pass.**

```bash
uv run pytest tests/test_prompts_loader.py -v
```

Expected: every test (10 cases) passes.

---

## Task 12: `examples/todo-app-design.md`

**Files:**
- Create: `examples/todo-app-design.md`

Per spec §12.3: this is the design doc the smoke test runs against. Deliberately trivial Flask TODO app with both UI and REST so dynamic verification exercises Playwright for at least one sprint.

- [ ] **Step 1: Write the example design doc.**

Create `examples/todo-app-design.md` with:

```markdown
# TODO App — Design Document

## Goal

A single-process Python web app for managing a personal todo list. Single-user, no auth, runs locally on http://127.0.0.1:5000.

## User stories

- I can see all my todos on the home page.
- I can add a new todo via a form.
- I can mark a todo as done.
- I can delete a todo.

## Functional requirements

1. **Web UI** at GET `/` — renders list of todos with checkbox + delete button per item, plus a "new todo" form at the top.
2. **REST API** at:
   - `GET /api/todos` — JSON array of `{id, text, done}`.
   - `POST /api/todos` — body `{text}`, returns the created todo.
   - `PATCH /api/todos/<id>` — body `{done: bool}`, returns updated todo.
   - `DELETE /api/todos/<id>` — returns `{ok: true}`.
3. **Persistence** in SQLite at `./todos.db`, schema `todos(id INTEGER PK, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)`.

## Non-functional requirements

- Python 3.12, Flask 3.x.
- Single-file deployable: `python app.py` starts the server.
- All runtime AND test dependencies in `requirements.txt` (Flask, pytest, ...; the smoke harness installs them via `uv pip install -r requirements.txt`). The Generator must include `pytest` because acceptance criteria below depend on running it.
- Tests in `tests/` covering each endpoint (pytest + Flask test client).

## Acceptance criteria (the harness will verify these)

- The home page renders without HTTP 500.
- POSTing a todo via the form makes it appear on the list after the redirect.
- The DELETE endpoint removes the row from SQLite.
- `pytest` from the project root passes with zero failures.
```

---

## Task 13: `examples/mcp.json.stdio` and `examples/mcp.json.streamable-http`

**Files:**
- Create: `examples/mcp.json.stdio`
- Create: `examples/mcp.json.streamable-http`

- [ ] **Step 1: Write `examples/mcp.json.stdio`.**

```json
{
  "mcpServers": {
    "harness-mcp": {
      "command": "harness-mcp",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

- [ ] **Step 2: Write `examples/mcp.json.streamable-http`.**

```json
{
  "mcpServers": {
    "harness-mcp": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

- [ ] **Step 3: Validate both are well-formed JSON.**

```bash
uv run python -c "import json; json.load(open('examples/mcp.json.stdio')); json.load(open('examples/mcp.json.streamable-http')); print('ok')"
```

Expected: `ok`.

---

## Task 14: `tests/conftest.py` — shared fixtures

**Files:**
- Create: `tests/conftest.py`

Provides `tmp_harness_home` (a per-test `~/.harness/` redirected to a temp directory via `HARNESS_HOME`) and `frozen_now_ms` (deterministic timestamp).

- [ ] **Step 1: Write `conftest.py`.**

```python
"""Shared pytest fixtures for harness-mcp tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_harness_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect `~/.harness/` to a per-test tmp directory.

    Sets `HARNESS_HOME` so harness_mcp.config.harness_home() resolves to the tmp dir.
    Creates the dir before yielding so callers can assume it exists.
    """
    home = tmp_path / "harness_home"
    home.mkdir()
    (home / "jobs").mkdir()
    monkeypatch.setenv("HARNESS_HOME", str(home))
    yield home


@pytest.fixture
def frozen_now_ms(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Replace harness_mcp.config.now_ms with a deterministic counter.

    Yields a list whose [0] is the current "time"; tests can advance it
    by mutating the list. Default starting value: 1_700_000_000_000.
    """
    counter = [1_700_000_000_000]

    def _fake_now() -> int:
        counter[0] += 1
        return counter[0]

    from harness_mcp import config as cfg

    monkeypatch.setattr(cfg, "now_ms", _fake_now)
    yield counter
```

- [ ] **Step 2: Sanity-check fixtures by running the existing test suite.**

```bash
uv run pytest tests/ -v
```

Expected: all currently-defined tests still pass; no fixture-related errors. Tests that don't use the new fixtures are unaffected.

---

## Task 15: Final ruff + pytest sweep

The package is feature-complete for Part 1. Verify the whole tree before handing off.

- [ ] **Step 1: Ruff lint.**

```bash
uv run ruff check .
```

Expected: `All checks passed!`. If anything fires, fix it (the most common offenders are unused imports in `types.py` and missing `from __future__ import annotations`).

- [ ] **Step 2: Ruff format check.**

```bash
uv run ruff format --check .
```

Expected: `<n> files already formatted`.

- [ ] **Step 3: Full pytest run.**

```bash
uv run pytest tests/ -v
```

Expected: every test passes. Capture and record the count for the next plan handoff.

- [ ] **Step 4: Confirm we are still on `main` and nothing is committed.**

```bash
git status --short && git rev-parse --abbrev-ref HEAD
```

Expected: many `??` lines for new files, no `M`/`A`/`D` (we haven't run `git add`). Branch: `main`. **Do not run `git add` or `git commit`.**

- [ ] **Step 5: Print the file inventory for handoff.**

```bash
find src/harness_mcp examples tests -type f \( -name '*.py' -o -name '*.md' -o -name '*.json' -o -name '*.toml' \) | sort
```

Expected: every file listed in the "File Structure" table at the top of this plan.

---

## Done criteria

- All tasks above complete.
- `uv run pytest tests/ -v` exits 0 with at least 25 passing tests.
- `uv run ruff check .` and `uv run ruff format --check .` exit 0.
- `uv run python -c "from harness_mcp.prompts_loader import _resolved_prompt_text; print(len(_resolved_prompt_text('planner.md')))"` prints a non-zero number.
- The repo is on `main` with NO commits, NO `git add`s by you. Just untracked / modified files in the working tree.

The next plan in the series (Part 2: Storage & Infrastructure) starts from this state and adds `state.py`, `process_group.py`, `logging_setup.py`, `mcp_capture.py`. It depends on `harness_mcp.types`, `harness_mcp.config`, and the prompts directory existing.
