# Harness MCP — Part 5: Orchestration & Server (Integration)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tie everything together. Build the sprint-loop driver (`sprints.py`), the per-job orchestrator coroutine (`orchestrator.py`), the FastMCP server with the four tool surfaces (`server.py`), the `harness-mcp` CLI (`__main__.py`), the README, and the smoke test (`tests/smoke.py`). After this part, `uv run python -m harness_mcp serve` boots a working MCP server.

**Architecture:** `sprints.py` calls into Part 3's modules (Generator, Evaluator) and Part 4's helpers in a deterministic sequence. `orchestrator.py` owns the per-job state machine: insert row → CAS update → planning → sprint loop → summarize. It also owns the cancel-scope registry from spec §3.2. `server.py` exposes four MCP tools (`start_build`, `poll_build`, `get_build_result`, `cancel_build`) and converts custom exceptions to `CallToolResult.is_error` shapes. `__main__.py` is a thin Click-or-argparse wrapper for `serve` and `doctor` subcommands.

**Tech Stack:** `mcp` (FastMCP), `anyio`, `claude_agent_sdk`, stdlib (`argparse`, `signal`, `json`).

**Spec source:** `docs/superpowers/specs/2026-05-07-harness-mcp-design.md` — sections §2.1 (process model), §3 (tool surface), §3.1 (error mapping), §3.2 (cancel registry), §4.3 (state machine), §5–§6 (plan + sprint phases), §6.5 (sprint timeout), §6.6 (job completion), §10.1 (lifespan), §10.7 (graceful shutdown), §11 (project structure), §12 (testing), §13 (notable decisions), §14 (README outline) are load-bearing.

**Depends on:** Parts 1–4 (everything).

---

## Branch & Commit Policy (READ FIRST)

- **Stay on the `main` branch for the entire plan.** Do not create or switch branches.
- **Do NOT run `git commit`, `git add`, `git push`, or any git mutation against this harness repo.** Verify by running tests / inspecting files only.
- The smoke test (Task 8) does invoke real LLM calls and runs `git init` against per-job `app/` directories under `~/.harness/jobs/<job_id>/app/`. Those are runtime artifacts in the user's home directory; they are not commits to the harness repo. The smoke test is documented as manual-only (`uv run python tests/smoke.py`), excluded from default `pytest` collection.
- If a step's check fails, fix the problem and re-run the check — never paper over with a commit.

---

## File Structure (this part owns)

| File | Purpose |
|---|---|
| `src/harness_mcp/sprints.py` | `run_sprint` (Stages 1–3 + retry), `negotiate_contract` (round-by-round), `run_evaluation` (spawn launcher + parse result), `commit_and_tag_check` (annotated-tag-collision narrowing) |
| `src/harness_mcp/orchestrator.py` | `start_orchestrator` (cancel registry + CAS-protected first DB write), `run_job` (top-level flow), `_cancel_scopes` registry, cancel handler |
| `src/harness_mcp/server.py` | FastMCP app, lifespan, four tool defs (`start_build`, `poll_build`, `get_build_result`, `cancel_build`), error mapper |
| `src/harness_mcp/__main__.py` | CLI: `harness-mcp serve [--transport stdio|streamable-http] [--host] [--port]`; `harness-mcp doctor` |
| `README.md` | Full README per spec §14 |
| `tests/test_orchestrator.py` | Cancel registry behavior, CAS race, run_job flow with all SDKs mocked |
| `tests/test_sprints.py` | Contract negotiation convergence, retry on FAIL, evaluator launcher mock |
| `tests/test_server.py` | Tool registration, error-mapper code mapping, idempotent cancel |
| `tests/smoke.py` | End-to-end run against `examples/todo-app-design.md` (manual) |

---

## Pre-flight (one-time, before Task 1)

- [ ] **Step 0a: Confirm Parts 1–4 are present.**

```bash
for f in src/harness_mcp/types.py src/harness_mcp/state.py \
         src/harness_mcp/contracts.py src/harness_mcp/generator.py \
         src/harness_mcp/evaluator.py src/harness_mcp/evaluator_runner.py \
         src/harness_mcp/planning.py src/harness_mcp/summarizer.py \
         src/harness_mcp/prereqs.py src/harness_mcp/process_group.py; do
    test -f "$f" || { echo "MISSING: $f"; exit 1; }
done
echo OK
```

Expected: `OK`. Otherwise STOP and finish prior parts.

- [ ] **Step 0b: Run prior tests.**

```bash
uv run pytest -q
```

Expected: green.

---

## Task 1: `sprints.py` — contract negotiation

**Files:**
- Create: `tests/test_sprints.py`
- Create: `src/harness_mcp/sprints.py`

Per spec §6.1: round-based negotiation. Each round spawns Generator (Codex) + Evaluator (Claude); orchestrator parses bodies via `parse_round_body_from_*` from `contracts.py`, appends with `## Round N — <Role>` header, checks for mutual `APPROVED`. Empty-body guard skips writing but still counts. Both empty in same round → abort.

- [ ] **Step 1: Write the failing test for contract negotiation.**

Create `tests/test_sprints.py`:

```python
"""Tests for harness_mcp.sprints — contract negotiation, evaluation, retry loop."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator, Any

import pytest

from harness_mcp.config import JobOptions
from harness_mcp.sprints import negotiate_contract


def _agent_msg(text: str) -> object:
    return SimpleNamespace(
        method="item/agentMessage/delta", payload=SimpleNamespace(delta=text)
    )


def _turn_completed() -> object:
    return SimpleNamespace(
        method="turn/completed", payload=SimpleNamespace(turn=SimpleNamespace(id="t1"))
    )


def _claude_text_msg(text: str) -> object:
    block = SimpleNamespace(text=text)
    block.__class__.__name__ = "TextBlock"
    msg = SimpleNamespace(content=[block])
    msg.__class__.__name__ = "AssistantMessage"
    return msg


@pytest.fixture
def fake_codex_factory(monkeypatch: pytest.MonkeyPatch):
    """Returns a closure that builds a fake AsyncCodex which yields scripted bodies."""

    def make(scripted_bodies: list[str]):
        idx = [0]

        @asynccontextmanager
        async def fake_codex(*_a: Any, **_kw: Any) -> AsyncIterator[Any]:
            class _Thread:
                async def turn(self, _x: Any) -> Any:
                    body = scripted_bodies[idx[0]]
                    idx[0] += 1
                    events = [_agent_msg(body), _turn_completed()]

                    class _T:
                        async def stream(self) -> AsyncIterator[Any]:
                            for e in events:
                                yield e
                    return _T()

            class _Wrap:
                async def thread_start(self) -> _Thread:
                    return _Thread()

            yield _Wrap()

        return fake_codex

    return make


@pytest.fixture
def fake_query_factory(monkeypatch: pytest.MonkeyPatch):
    """Patch claude_agent_sdk.query with a script."""
    from harness_mcp import sprints

    def make(scripted_bodies: list[str]):
        idx = [0]

        async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
            body = scripted_bodies[idx[0]]
            idx[0] += 1
            yield _claude_text_msg(body)

        monkeypatch.setattr(sprints, "query", fake_query)

    return make


class TestNegotiateContract:
    @pytest.mark.asyncio
    async def test_immediate_approve_round_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex_factory, fake_query_factory
    ) -> None:
        from harness_mcp import sprints
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("# Sprint 1: Title\n", encoding="utf-8")

        # Generator emits criteria; Evaluator emits APPROVED.
        monkeypatch.setattr(sprints, "AsyncCodex", fake_codex_factory(
            ["1. server starts\n2. tests pass"]
        ))
        fake_query_factory(["criteria look good\nAPPROVED"])

        sealed = await negotiate_contract(
            job_dir=tmp_path,
            sprint_dir=sprint_dir,
            sprint_seq=1,
            sprint_title="Title",
            design_text="DESIGN",
            plan_section_text="PLAN_SECTION",
            options=JobOptions(),
            generator_md="GENERATOR_PROMPT",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
        )
        assert sealed is True
        contract_text = (sprint_dir / "contract.md").read_text(encoding="utf-8")
        assert "## Round 1 — Generator" in contract_text
        assert "## Round 1 — Evaluator" in contract_text
        assert "APPROVED" in contract_text

    @pytest.mark.asyncio
    async def test_two_rounds_to_converge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex_factory, fake_query_factory
    ) -> None:
        from harness_mcp import sprints
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("# Sprint 1: Title\n", encoding="utf-8")

        # Round 1: Generator proposes, Evaluator critiques (no APPROVED).
        # Round 2: Generator revises with APPROVED, Evaluator APPROVED.
        monkeypatch.setattr(sprints, "AsyncCodex", fake_codex_factory([
            "criterion 1: x\ncriterion 2: y",
            "criterion 1: x\ncriterion 2: y\ncriterion 3: z\nAPPROVED",
        ]))
        fake_query_factory([
            "missing criterion 3",
            "looks good\nAPPROVED",
        ])

        sealed = await negotiate_contract(
            job_dir=tmp_path,
            sprint_dir=sprint_dir,
            sprint_seq=1,
            sprint_title="Title",
            design_text="DESIGN",
            plan_section_text="PLAN_SECTION",
            options=JobOptions(),
            generator_md="GENERATOR_PROMPT",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
        )
        assert sealed is True
        text = (sprint_dir / "contract.md").read_text(encoding="utf-8")
        assert text.count("## Round") == 4

    @pytest.mark.asyncio
    async def test_failure_after_max_rounds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex_factory, fake_query_factory
    ) -> None:
        from harness_mcp import sprints
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")

        # Always-disagreeing parties.
        monkeypatch.setattr(sprints, "AsyncCodex", fake_codex_factory([
            "round 1 criteria",
            "round 2 criteria",
            "round 3 criteria",
        ]))
        fake_query_factory([
            "round 1 critique",
            "round 2 critique",
            "round 3 critique",
        ])

        sealed = await negotiate_contract(
            job_dir=tmp_path,
            sprint_dir=sprint_dir,
            sprint_seq=1,
            sprint_title="x",
            design_text="DESIGN",
            plan_section_text="PLAN_SECTION",
            options=JobOptions(max_contract_negotiation_rounds=3),
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
        )
        assert sealed is False

    @pytest.mark.asyncio
    async def test_both_empty_round_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex_factory, fake_query_factory
    ) -> None:
        from harness_mcp import sprints
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")

        monkeypatch.setattr(sprints, "AsyncCodex", fake_codex_factory(["", ""]))
        fake_query_factory(["", ""])

        with pytest.raises(Exception):  # ContractNegotiationFailedError or similar
            await negotiate_contract(
                job_dir=tmp_path,
                sprint_dir=sprint_dir,
                sprint_seq=1,
                sprint_title="x",
                design_text="D",
                plan_section_text="P",
                options=JobOptions(max_contract_negotiation_rounds=2),
                generator_md="G",
                evaluator_options_factory=lambda **_kw: object(),
                codex_bin="/usr/bin/codex",
                codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            )
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_sprints.py::TestNegotiateContract -v
```

Expected: ImportError on `harness_mcp.sprints`.

- [ ] **Step 3: Implement contract negotiation in `sprints.py`.**

Create `src/harness_mcp/sprints.py`:

```python
"""Sprint loop: stages 1 (contract) → 2 (impl) → 3 (eval) → 4 (retry).

Stage 1 here. Stages 2–4 added in Tasks 2–3.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from harness_mcp.config import JobOptions, jobs_root
from harness_mcp.contracts import (
    append_round_atomic,
    is_approved_body,
    parse_round_body_from_claude_msgs,
    parse_round_body_from_codex_events,
)
from harness_mcp.evaluator import parse_eval_md
from harness_mcp.generator import chunk_loop
from harness_mcp.process_group import PIPE, ProcessGroupScope
from harness_mcp.prompts_loader import _resolved_prompt_text
from harness_mcp.types import (
    EvaluationResult,
    EvaluatorEmittedUnparseableEvalMdError,
    HarnessToolError,
    ImplementationResult,
)

# Lazy SDK imports so unit tests can monkeypatch.
try:
    from codex_app_server import AppServerConfig, AsyncCodex, TextInput  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    AppServerConfig = AsyncCodex = TextInput = None  # type: ignore[assignment]
try:
    from claude_agent_sdk import query  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    query = None  # type: ignore[assignment]


class ContractNegotiationFailedError(HarnessToolError):
    """Both Generator and Evaluator emitted empty bodies in the same round."""


# ---------- Stage 1: contract negotiation ----------


def _round_aware_addendum(role: str, n: int, contract_text: str) -> str:
    return (
        f"## Round instruction\n"
        f"The contract above contains {n} completed rounds. Emit ROUND {n + 1} only.\n"
        f"Either propose a revision that addresses the latest opposite-side feedback, OR emit "
        f"`APPROVED` (the literal token, on its own line at the end of your response) if you accept "
        f"the latest counter-proposal verbatim. Do NOT re-propose criteria you have already proposed "
        f"unchanged in earlier rounds."
    )


def _contract_user_prompt(
    *,
    role: str,  # "Generator" | "Evaluator"
    sprint_title: str,
    design_text: str,
    plan_section_text: str,
    contract_text: str,
    rounds_completed: int,
    generator_md: str,
) -> str:
    return f"""{generator_md}

## Mode: contract-negotiation

## Sprint title
{sprint_title}

## Design (verbatim)
{design_text}

## Plan section (verbatim)
{plan_section_text}

## Contract so far (verbatim)
{contract_text}

{_round_aware_addendum(role, rounds_completed, contract_text)}
"""


async def _drive_codex_round(
    *,
    cwd: Path,
    user_prompt: str,
    codex_bin: str,
    codex_overrides: tuple[str, ...],
    max_turns: int,
) -> str:
    """Drive one Codex turn, return the concatenated agent-message-delta body."""
    cfg = AppServerConfig(
        codex_bin=codex_bin,
        cwd=str(cwd),
        config_overrides=codex_overrides,
        client_name="harness-mcp",
        client_title="Harness Generator",
        client_version="0.1.0",
    )
    events: list[Any] = []
    async with AsyncCodex(config=cfg) as codex:
        thread = await codex.thread_start()
        turn = await thread.turn(TextInput(user_prompt) if TextInput else user_prompt)
        item_started_count = 0
        async for event in turn.stream():
            events.append(event)
            method = getattr(event, "method", "")
            if method == "item/started":
                item_started_count += 1
            if method == "turn/completed":
                break
            if item_started_count >= max_turns:
                break
    return parse_round_body_from_codex_events(events)


async def _drive_claude_round(
    *,
    user_prompt: str,
    options: Any,
    max_turns: int,
) -> str:
    """Drive one Claude query() to completion (or to max_turns), return concatenated TextBlocks."""
    msgs: list[Any] = []
    assistant_count = 0
    async for msg in query(prompt=user_prompt, options=options):
        msgs.append(msg)
        if type(msg).__name__ == "AssistantMessage":
            assistant_count += 1
            if assistant_count >= max_turns:
                break
    return parse_round_body_from_claude_msgs(msgs)


async def negotiate_contract(
    *,
    job_dir: Path,
    sprint_dir: Path,
    sprint_seq: int,
    sprint_title: str,
    design_text: str,
    plan_section_text: str,
    options: JobOptions,
    generator_md: str,
    evaluator_options_factory: Callable[..., Any],
    codex_bin: str,
    codex_overrides: tuple[str, ...],
) -> bool:
    """Round-based contract negotiation per spec §6.1.

    Returns True iff both sides emit APPROVED in the same round.
    Raises ContractNegotiationFailedError if both emit empty bodies in
    the same round (no progress).
    """
    contract_path = sprint_dir / "contract.md"
    rounds_completed = 0

    while rounds_completed < options.max_contract_negotiation_rounds:
        contract_text = contract_path.read_text(encoding="utf-8")
        n = rounds_completed

        # Generator round.
        gen_prompt = _contract_user_prompt(
            role="Generator",
            sprint_title=sprint_title,
            design_text=design_text,
            plan_section_text=plan_section_text,
            contract_text=contract_text,
            rounds_completed=n,
            generator_md=generator_md,
        )
        gen_body = (
            await _drive_codex_round(
                cwd=job_dir / "app",
                user_prompt=gen_prompt,
                codex_bin=codex_bin,
                codex_overrides=codex_overrides,
                max_turns=options.max_negotiation_turns,
            )
        ).strip()

        # Evaluator round.
        eval_options = evaluator_options_factory(job_dir=job_dir, sprint_seq=sprint_seq)
        contract_text_for_eval = contract_text + (
            f"\n## Round {n + 1} — Generator\n{gen_body}\n" if gen_body else ""
        )
        eval_prompt = _contract_user_prompt(
            role="Evaluator",
            sprint_title=sprint_title,
            design_text=design_text,
            plan_section_text=plan_section_text,
            contract_text=contract_text_for_eval,
            rounds_completed=n,
            generator_md=generator_md,
        )
        eval_body = (
            await _drive_claude_round(
                user_prompt=eval_prompt,
                options=eval_options,
                max_turns=options.max_negotiation_turns,
            )
        ).strip()

        # Empty-body guard.
        if not gen_body and not eval_body:
            raise ContractNegotiationFailedError(
                f"sprint {sprint_seq}: contract_negotiation_no_progress (both bodies empty)"
            )

        if gen_body:
            append_round_atomic(
                contract_path, f"\n## Round {n + 1} — Generator\n", gen_body + "\n"
            )
        if eval_body:
            append_round_atomic(
                contract_path, f"\n## Round {n + 1} — Evaluator\n", eval_body + "\n"
            )

        rounds_completed += 1

        if is_approved_body(gen_body) and is_approved_body(eval_body):
            return True

    return False
```

- [ ] **Step 4: Run the contract-negotiation tests.**

```bash
uv run pytest tests/test_sprints.py::TestNegotiateContract -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/sprints.py tests/test_sprints.py
uv run ruff format --check src/harness_mcp/sprints.py tests/test_sprints.py
```

Expected: zero findings.

---

## Task 2: `sprints.py` — Stage 3 (evaluation via launcher) + Stage 4 (retry)

**Files:**
- Modify: `tests/test_sprints.py`
- Modify: `src/harness_mcp/sprints.py`

`run_evaluation` spawns `python -m harness_mcp.evaluator_runner` under `ProcessGroupScope`, writes the JSON payload to its stdin, drains stdout/stderr, parses `eval.md`. `run_sprint` is the §6.5 timeout-bounded driver of all four stages.

- [ ] **Step 1: Append failing tests.**

Append to `tests/test_sprints.py`:

```python
import sys
import textwrap

from harness_mcp.sprints import run_evaluation, run_sprint


class TestRunEvaluation:
    @pytest.mark.asyncio
    async def test_invokes_launcher_via_subprocess(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end style — write a tiny eval.md from a fake launcher script."""
        # Stub launcher: reads stdin JSON, writes eval.md per the payload.
        stub = tmp_path / "stub_runner.py"
        stub.write_text(
            textwrap.dedent(
                """
                import json, sys, pathlib
                payload = json.loads(sys.stdin.read())
                ep = pathlib.Path(payload["paths"]["eval"])
                ep.parent.mkdir(parents=True, exist_ok=True)
                ep.write_text(
                    "# Sprint 1 Evaluation\\n"
                    "## Static audit\\n\\n### Criterion 1: x\\n"
                    "**Result:** PASS\\n**Evidence:** e\\n**Notes:** n\\n"
                    "## Dynamic verification\\n\\n### Routing decision\\nran tests\\n\\n"
                    "### Criterion 1: y\\n**Result:** PASS\\n**Evidence:** e\\n**Notes:** n\\n"
                )
                """
            ).strip()
        )

        # Patch sprints._launcher_command to return our stub.
        from harness_mcp import sprints
        monkeypatch.setattr(
            sprints, "_launcher_command",
            lambda: [sys.executable, str(stub)],
        )

        job_id = "JOBID"
        from harness_mcp.config import jobs_root
        job_dir = jobs_root() / job_id  # depends on tmp_harness_home fixture; add it
        # Use tmp_path as job_dir directly to avoid HARNESS_HOME complexity.
        job_dir = tmp_path / "jobs" / job_id
        sprint_dir = job_dir / "sprint-1"
        sprint_dir.mkdir(parents=True)
        (sprint_dir / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")

        result = await run_evaluation(
            job_id=job_id,
            sprint_seq=1,
            sprint_dir=sprint_dir,
            job_dir=job_dir,
            captured_mcp={"context7": {"command": "ctx7"}},
            setting_sources=["user"],
            options=JobOptions(max_evaluation_seconds=30),
            prior_tag=None,
        )
        assert isinstance(result, EvaluationResult)
        assert result.passed is True
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_sprints.py::TestRunEvaluation -v
```

Expected: ImportError on `run_evaluation`.

- [ ] **Step 3: Append the implementation.**

Append to `src/harness_mcp/sprints.py`:

```python
# ---------- Stage 3: evaluation (launcher subprocess) ----------


def _launcher_command() -> list[str]:
    """Return the argv for the Evaluator launcher.

    Indirected via a function so tests can monkeypatch it to a stub script.
    """
    return [sys.executable, "-m", "harness_mcp.evaluator_runner"]


async def run_evaluation(
    *,
    job_id: str,
    sprint_seq: int,
    sprint_dir: Path,
    job_dir: Path,
    captured_mcp: dict[str, dict[str, Any]],
    setting_sources: list[str],
    options: JobOptions,
    prior_tag: str | None,
) -> EvaluationResult:
    """Spawn the launcher subprocess under ProcessGroupScope; parse eval.md.

    On non-zero exit OR parse failure → returns EvaluationResult with
    passed=False and unparseable=True. The caller (sprint loop) treats
    that as a retry.
    """
    eval_path = sprint_dir / "eval.md"
    contract_path = sprint_dir / "contract.md"
    log_path = sprint_dir / "log.txt"
    app_dir = job_dir / "app"

    payload = {
        "job_id": job_id,
        "sprint_seq": sprint_seq,
        "paths": {
            "design": str(job_dir / "design.md"),
            "plan": str(job_dir / "plan.md"),
            "contract": str(contract_path),
            "eval": str(eval_path),
            "app": str(app_dir),
            "log": str(log_path),
        },
        "captured_mcp_stanzas": captured_mcp,
        "setting_sources": setting_sources,
        "max_evaluation_seconds": options.max_evaluation_seconds,
        "prior_tag": prior_tag,
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    async with ProcessGroupScope(f"eval-{job_id}-{sprint_seq}") as pg:
        proc = await pg.spawn(
            _launcher_command(), stdin=PIPE, stdout=PIPE, stderr=PIPE,
        )
        await pg.communicate(proc, payload_bytes)

        async def _drain(stream: Any) -> None:
            if stream is None:
                return
            try:
                async for chunk in stream:
                    _ = chunk  # discard
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                return
            except Exception:
                return

        async with anyio.create_task_group() as tg:
            tg.start_soon(_drain, proc.stdout)
            tg.start_soon(_drain, proc.stderr)
            with anyio.fail_after(options.max_evaluation_seconds + 60):
                rc = await proc.wait()

    try:
        result = parse_eval_md(eval_path, sprint_seq=sprint_seq)
    except EvaluatorEmittedUnparseableEvalMdError:
        return EvaluationResult(
            sprint_seq=sprint_seq,
            static_criteria=[],
            dynamic_criteria=[],
            routing_decision="",
            passed=False,
            unparseable=True,
        )
    if rc != 0 and result.passed:
        # Launcher errored even though eval.md parsed clean — distrust.
        return EvaluationResult(
            sprint_seq=sprint_seq,
            static_criteria=result.static_criteria,
            dynamic_criteria=result.dynamic_criteria,
            routing_decision=result.routing_decision,
            passed=False,
            unparseable=True,
        )
    return result
```

- [ ] **Step 4: Run the evaluation tests.**

```bash
uv run pytest tests/test_sprints.py::TestRunEvaluation -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/sprints.py
uv run ruff format --check src/harness_mcp/sprints.py
```

Expected: zero findings.

---

## Task 3: `sprints.py` — `run_sprint` (Stages 1–4 with timeout)

**Files:**
- Modify: `tests/test_sprints.py`
- Modify: `src/harness_mcp/sprints.py`

Per spec §6.5: `anyio.fail_after(max_sprint_duration_minutes * 60)` wraps Stages 1–3 of each attempt. Stage 4 (retry) is the outer `range(max_sprint_retries + 1)` loop.

- [ ] **Step 1: Append failing tests for the full sprint runner.**

Append to `tests/test_sprints.py`:

```python
class TestRunSprint:
    @pytest.mark.asyncio
    async def test_first_attempt_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harness_mcp import sprints

        # Stub all the pieces: contract negotiation seals on round 1, chunk_loop returns ok=True,
        # evaluation returns passed=True.
        async def fake_negotiate(**_kw: Any) -> bool:
            sprint_dir = _kw["sprint_dir"]
            (sprint_dir / "contract.md").write_text(
                "# Sprint 1\n## Round 1 — Generator\nAPPROVED\n## Round 1 — Evaluator\nAPPROVED\n",
                encoding="utf-8",
            )
            return True

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            return ImplementationResult(ok=True, commit_sha="abc", summary="done")

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            return EvaluationResult(
                sprint_seq=1, static_criteria=[], dynamic_criteria=[],
                routing_decision="", passed=True,
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        result = await sprints.run_sprint(
            job_id="JOBID",
            sprint_seq=1,
            sprint_title="Title",
            job_dir=job_dir,
            options=JobOptions(),
            captured_mcp={"context7": {"command": "ctx7"}},
            setting_sources=["user"],
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            prior_tag=None,
        )
        assert result.passed is True
        assert result.attempts == 1

    @pytest.mark.asyncio
    async def test_retries_on_eval_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harness_mcp import sprints

        async def fake_negotiate(**_kw: Any) -> bool:
            (_kw["sprint_dir"] / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")
            return True

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            return ImplementationResult(ok=True, commit_sha="abc", summary="done")

        eval_attempts = [0]

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            eval_attempts[0] += 1
            return EvaluationResult(
                sprint_seq=1, static_criteria=[], dynamic_criteria=[],
                routing_decision="", passed=(eval_attempts[0] == 2),
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        result = await sprints.run_sprint(
            job_id="JOBID",
            sprint_seq=1,
            sprint_title="Title",
            job_dir=job_dir,
            options=JobOptions(max_sprint_retries=2),
            captured_mcp={"context7": {"command": "ctx7"}},
            setting_sources=["user"],
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            prior_tag=None,
        )
        assert result.passed is True
        assert result.attempts == 2
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_sprints.py::TestRunSprint -v
```

Expected: ImportError on `run_sprint`.

- [ ] **Step 3: Append `run_sprint`.**

Append to `src/harness_mcp/sprints.py`:

```python
# ---------- Stages 2 + 4: implementation + retry; complete sprint runner ----------


@dataclass(frozen=True)
class SprintResult:
    sprint_seq: int
    passed: bool
    attempts: int
    error: str | None = None


def _slice_plan_section(plan_text: str, sprint_seq: int) -> str:
    """Extract the body under `## Sprint <N>:` until the next `## Sprint`."""
    starts = list(re.finditer(r"^##\s+Sprint\s+(\d+):\s*(.+?)\s*$", plan_text, re.MULTILINE))
    for i, m in enumerate(starts):
        if int(m.group(1)) == sprint_seq:
            start = m.start()
            end = starts[i + 1].start() if i + 1 < len(starts) else len(plan_text)
            return plan_text[start:end]
    return ""


async def run_sprint(
    *,
    job_id: str,
    sprint_seq: int,
    sprint_title: str,
    job_dir: Path,
    options: JobOptions,
    captured_mcp: dict[str, dict[str, Any]],
    setting_sources: list[str],
    generator_md: str,
    evaluator_options_factory: Callable[..., Any],
    codex_bin: str,
    codex_overrides: tuple[str, ...],
    prior_tag: str | None,
) -> SprintResult:
    """Run one sprint end-to-end: contract → impl → eval → retry."""
    sprint_dir = job_dir / f"sprint-{sprint_seq}"
    sprint_dir.mkdir(parents=True, exist_ok=True)
    contract_path = sprint_dir / "contract.md"
    if not contract_path.is_file():
        contract_path.write_text(f"# Sprint {sprint_seq}: {sprint_title}\n", encoding="utf-8")

    design_text = (job_dir / "design.md").read_text(encoding="utf-8") if (job_dir / "design.md").is_file() else ""
    plan_text = (job_dir / "plan.md").read_text(encoding="utf-8") if (job_dir / "plan.md").is_file() else ""
    plan_section_text = _slice_plan_section(plan_text, sprint_seq)
    log_path = sprint_dir / "log.txt"

    attempts = 0
    eval_md_for_retry: Path | None = None

    while attempts <= options.max_sprint_retries:
        attempts += 1
        try:
            with anyio.fail_after(options.max_sprint_duration_minutes * 60):
                if attempts == 1:
                    sealed = await negotiate_contract(
                        job_dir=job_dir,
                        sprint_dir=sprint_dir,
                        sprint_seq=sprint_seq,
                        sprint_title=sprint_title,
                        design_text=design_text,
                        plan_section_text=plan_section_text,
                        options=options,
                        generator_md=generator_md,
                        evaluator_options_factory=evaluator_options_factory,
                        codex_bin=codex_bin,
                        codex_overrides=codex_overrides,
                    )
                    if not sealed:
                        return SprintResult(
                            sprint_seq=sprint_seq, passed=False, attempts=attempts,
                            error="contract_not_sealed",
                        )

                impl_result = await chunk_loop(
                    app_dir=job_dir / "app",
                    sprint_dir=sprint_dir,
                    contract_path=contract_path,
                    design_path=(job_dir / "design.md"),
                    plan_section_path=None,  # we inline plan_section_text already in contract
                    log_path=log_path,
                    options=options,
                    generator_md_text=generator_md,
                    sprint_seq=sprint_seq,
                    job_id=job_id,
                    eval_md_for_retry=eval_md_for_retry,
                    codex_bin=codex_bin,
                    codex_config_overrides=codex_overrides,
                )
                if not impl_result.ok:
                    return SprintResult(
                        sprint_seq=sprint_seq, passed=False, attempts=attempts,
                        error=impl_result.error or "implementation_failed",
                    )

                eval_result = await run_evaluation(
                    job_id=job_id,
                    sprint_seq=sprint_seq,
                    sprint_dir=sprint_dir,
                    job_dir=job_dir,
                    captured_mcp=captured_mcp,
                    setting_sources=setting_sources,
                    options=options,
                    prior_tag=prior_tag,
                )
                if eval_result.passed:
                    return SprintResult(sprint_seq=sprint_seq, passed=True, attempts=attempts)
                # Failed eval → retry path uses this eval.md as input.
                eval_md_for_retry = sprint_dir / "eval.md"
        except TimeoutError:
            return SprintResult(
                sprint_seq=sprint_seq, passed=False, attempts=attempts,
                error="sprint_timeout",
            )

    return SprintResult(
        sprint_seq=sprint_seq, passed=False, attempts=attempts,
        error="max_sprint_retries_exceeded",
    )
```

- [ ] **Step 4: Run the sprint-runner tests.**

```bash
uv run pytest tests/test_sprints.py::TestRunSprint -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/sprints.py
uv run ruff format --check src/harness_mcp/sprints.py
```

Expected: zero findings.

---

## Task 4: `orchestrator.py` — cancel registry + per-job coroutine

**Files:**
- Create: `tests/test_orchestrator.py`
- Create: `src/harness_mcp/orchestrator.py`

Per spec §3.2 + §4.3 + §6.6: the per-job coroutine. Owns `_cancel_scopes: dict[str, CancelScope]`. CAS-protected `pending → running` first DB write. Drives planning → sprints → summarizer. On cancel, scope unwind + final DB write under shielded `move_on_after(15)`.

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_orchestrator.py`:

```python
"""Tests for harness_mcp.orchestrator — per-job state machine + cancel registry."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import pytest

from harness_mcp.config import JobOptions, jobs_root
from harness_mcp.orchestrator import (
    cancel_job,
    register_scope,
    unregister_scope,
)
from harness_mcp.state import close_db, db_write, init_db


@pytest.fixture
async def db(tmp_harness_home: Path) -> Path:
    init_db()
    yield tmp_harness_home / "state.db"
    close_db()


class TestCancelRegistry:
    @pytest.mark.asyncio
    async def test_register_and_unregister(self) -> None:
        scope = anyio.CancelScope()
        await register_scope("J1", scope)
        # Re-register same key should be a no-op (or replace).
        await register_scope("J1", scope)
        await unregister_scope("J1")
        # Unregistering a missing key is fine.
        await unregister_scope("J1")


class TestCancelJob:
    @pytest.mark.asyncio
    async def test_unknown_job_raises(self, db: Path) -> None:
        from harness_mcp.types import UnknownJobError
        with pytest.raises(UnknownJobError):
            await cancel_job("NO_SUCH_JOB")

    @pytest.mark.asyncio
    async def test_terminal_job_returns_idempotent_marker(self, db: Path) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_TERM", "completed", "done", "/x", "{}", 1, 1),
        )
        result = await cancel_job("J_TERM")
        assert result == {"ok": True, "was_already_terminal": True}

    @pytest.mark.asyncio
    async def test_pending_job_marked_cancelled(self, db: Path) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_PEND", "pending", "init", "/x", "{}", 1, 1),
        )
        result = await cancel_job("J_PEND")
        assert result["ok"] is True
        assert result["was_already_terminal"] is False
        # DB row reflects the cancel.
        from harness_mcp.state import open_reader
        async with open_reader() as r:
            row = r.execute("SELECT status FROM jobs WHERE id='J_PEND'").fetchone()
        assert row[0] == "cancelled"

    @pytest.mark.asyncio
    async def test_running_job_scope_cancelled(self, db: Path) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_RUN", "running", "planning", "/x", "{}", 1, 1),
        )
        scope = anyio.CancelScope()
        await register_scope("J_RUN", scope)
        result = await cancel_job("J_RUN")
        assert result == {"ok": True, "was_already_terminal": False}
        assert scope.cancel_called

        from harness_mcp.state import open_reader
        async with open_reader() as r:
            row = r.execute("SELECT status, last_message FROM jobs WHERE id='J_RUN'").fetchone()
        assert row[0] == "cancelled"
        await unregister_scope("J_RUN")
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_orchestrator.py -v
```

Expected: ImportError on `harness_mcp.orchestrator`.

- [ ] **Step 3: Implement the orchestrator (cancel registry + cancel_job).**

Create `src/harness_mcp/orchestrator.py`:

```python
"""Per-job orchestrator coroutine + cancel-scope registry.

The MCP `start_build` tool inserts a `pending` row, then schedules
`run_job(job_id)` on the server's task group. `run_job`'s first action
is a CAS-protected UPDATE to flip `pending → running` (spec §3.2);
if that UPDATE matches zero rows, the cancel handler beat us — exit cleanly.

The cancel-scope registry (`_cancel_scopes`) is module-global, guarded
by `_scopes_lock`. `cancel_build` looks up the running job's scope and
calls `scope.cancel()` after writing the row.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio

from harness_mcp.config import JobOptions, jobs_root, now_ms
from harness_mcp.planning import run_plan_phase
from harness_mcp.prereqs import PrereqsResult
from harness_mcp.prompts_loader import _resolved_prompt_text
from harness_mcp.sprints import run_sprint
from harness_mcp.state import (
    db_write,
    db_write_returning_rowcount,
    new_job_id,
    open_reader,
    TERMINAL_JOB_STATUSES,
)
from harness_mcp.summarizer import run_summarizer
from harness_mcp.types import UnknownJobError


_cancel_scopes: dict[str, anyio.CancelScope] = {}
_scopes_lock = anyio.Lock()


async def register_scope(job_id: str, scope: anyio.CancelScope) -> None:
    async with _scopes_lock:
        _cancel_scopes[job_id] = scope


async def unregister_scope(job_id: str) -> None:
    async with _scopes_lock:
        _cancel_scopes.pop(job_id, None)


async def cancel_job(job_id: str) -> dict[str, Any]:
    """Implement §3.2 cancel_build semantics. Idempotent."""
    async with open_reader() as r:
        row = r.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise UnknownJobError(job_id)
    status = row[0]
    if status in TERMINAL_JOB_STATUSES:
        return {"ok": True, "was_already_terminal": True}
    if status == "pending":
        await db_write(
            "UPDATE jobs SET status='cancelled', last_message=?, finished_at=?, updated_at=? "
            "WHERE id=? AND status='pending'",
            ("cancelled by user before orchestrator started", now_ms(), now_ms(), job_id),
        )
        return {"ok": True, "was_already_terminal": False}
    # Running.
    await db_write(
        "UPDATE jobs SET status='cancelled', last_message=?, finished_at=?, updated_at=? WHERE id=?",
        ("cancelled by user", now_ms(), now_ms(), job_id),
    )
    async with _scopes_lock:
        scope = _cancel_scopes.get(job_id)
    if scope is not None:
        scope.cancel()
    return {"ok": True, "was_already_terminal": False}


# ---------- run_job ----------


async def start_orchestrator_inserts_row(
    *,
    design_doc_path: Path,
    options: JobOptions,
) -> str:
    """Insert the `pending` row and copy the design doc. Return the job_id.

    Called synchronously from `start_build` so the tool's return reflects
    durable state. The orchestrator coroutine is then spawned separately
    via the server's task group.
    """
    job_id = new_job_id()
    job_dir = jobs_root() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "plan-history").mkdir(exist_ok=True)
    (job_dir / "app").mkdir(exist_ok=True)

    # Seed app/.gitignore + git init.
    gitignore = job_dir / "app" / ".gitignore"
    gitignore.write_text(".codex/\nnode_modules/\n*.pyc\n.venv/\n.env\n")
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(job_dir / "app"), check=True)
    subprocess.run(["git", "config", "user.email", "harness@local"], cwd=str(job_dir / "app"), check=True)
    subprocess.run(["git", "config", "user.name", "harness"], cwd=str(job_dir / "app"), check=True)

    # Verbatim design copy.
    (job_dir / "design.md").write_text(design_doc_path.read_text(encoding="utf-8"), encoding="utf-8")

    await db_write(
        "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
        "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            "pending",
            "init",
            str(design_doc_path),
            json.dumps({k: getattr(options, k) for k in options.__dataclass_fields__}),
            now_ms(),
            now_ms(),
        ),
    )
    return job_id


async def run_job(
    *,
    job_id: str,
    options: JobOptions,
    prereqs_result: PrereqsResult,
    planner_options_factory: Callable[..., Any],
    reviewer_options_factory: Callable[..., Any],
    evaluator_options_factory: Callable[..., Any],
    summarizer_options_factory: Callable[..., Any],
    codex_bin: str,
) -> None:
    """Top-level per-job coroutine. Drives planning → sprints → summary.

    On cancel: the outer scope's __aexit__ propagates cancellation; the
    final state-update happens inside a shielded `move_on_after(15)`.
    """
    job_dir = jobs_root() / job_id
    log_path = job_dir / "orchestrator.log"

    with anyio.CancelScope() as scope:
        await register_scope(job_id, scope)
        try:
            # CAS pending → running.
            rc = await db_write_returning_rowcount(
                "UPDATE jobs SET status='running', current_phase='planning', updated_at=? "
                "WHERE id=? AND status='pending'",
                (now_ms(), job_id),
            )
            if rc == 0:
                return  # cancel beat us

            # Plan phase.
            generator_md = _resolved_prompt_text("generator.md")
            sprints, _rounds = await run_plan_phase(
                job_dir=job_dir,
                options=options,
                planner_options_factory=planner_options_factory,
                reviewer_options_factory=reviewer_options_factory,
                log_path=log_path,
            )

            # Insert sprint rows.
            for seq, title in sprints:
                await db_write(
                    "INSERT INTO sprints (job_id, seq, title, status) VALUES (?, ?, ?, ?)",
                    (job_id, seq, title, "pending"),
                )

            # Sprint loop.
            prior_tag: str | None = None
            for seq, title in sprints:
                await db_write(
                    "UPDATE jobs SET current_phase=?, updated_at=? WHERE id=?",
                    (f"sprint-{seq}/contract", now_ms(), job_id),
                )
                await db_write(
                    "UPDATE sprints SET status='running', started_at=? WHERE job_id=? AND seq=?",
                    (now_ms(), job_id, seq),
                )

                result = await run_sprint(
                    job_id=job_id,
                    sprint_seq=seq,
                    sprint_title=title,
                    job_dir=job_dir,
                    options=options,
                    captured_mcp=prereqs_result.captured_mcp,
                    setting_sources=prereqs_result.setting_sources,
                    generator_md=generator_md,
                    evaluator_options_factory=evaluator_options_factory,
                    codex_bin=codex_bin,
                    codex_overrides=prereqs_result.codex_overrides,
                    prior_tag=prior_tag,
                )

                final_status = "passed" if result.passed else "failed"
                await db_write(
                    "UPDATE sprints SET status=?, retry_count=?, finished_at=? "
                    "WHERE job_id=? AND seq=?",
                    (final_status, result.attempts - 1, now_ms(), job_id, seq),
                )

                if not result.passed:
                    await db_write(
                        "UPDATE jobs SET status='failed', current_phase=?, error_text=?, "
                        "finished_at=?, updated_at=? WHERE id=?",
                        (
                            f"sprint-{seq}/retry",
                            result.error or "sprint_failed",
                            now_ms(), now_ms(), job_id,
                        ),
                    )
                    return

                prior_tag = f"harness/{job_id}/sprint-{seq}"

            # Summarizer.
            await db_write(
                "UPDATE jobs SET current_phase='summarizing', updated_at=? WHERE id=?",
                (now_ms(), job_id),
            )
            summarizer_options = summarizer_options_factory(job_dir=job_dir)
            summary = await run_summarizer(job_dir=job_dir, options=summarizer_options)

            await db_write(
                "UPDATE jobs SET status='completed', current_phase='done', last_message=?, "
                "finished_at=?, updated_at=? WHERE id=?",
                (summary[:500], now_ms(), now_ms(), job_id),
            )
        except anyio.get_cancelled_exc_class():
            # Cancellation: the cancel_job handler already wrote the terminal row.
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(15):
                    await db_write(
                        "UPDATE jobs SET updated_at=? WHERE id=? AND status NOT IN "
                        "('completed','failed','cancelled','interrupted')",
                        (now_ms(), job_id),
                    )
            raise
        except Exception as e:  # noqa: BLE001 — top-level error reporter
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(15):
                    await db_write(
                        "UPDATE jobs SET status='failed', error_text=?, finished_at=?, "
                        "updated_at=? WHERE id=? AND status NOT IN "
                        "('completed','failed','cancelled','interrupted')",
                        (f"orchestrator_error: {e!r}", now_ms(), now_ms(), job_id),
                    )
        finally:
            await unregister_scope(job_id)
```

- [ ] **Step 4: Run the orchestrator tests.**

```bash
uv run pytest tests/test_orchestrator.py -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/orchestrator.py tests/test_orchestrator.py
uv run ruff format --check src/harness_mcp/orchestrator.py tests/test_orchestrator.py
```

Expected: zero findings.

---

## Task 5: `server.py` — FastMCP tools + error mapper

**Files:**
- Create: `tests/test_server.py`
- Create: `src/harness_mcp/server.py`

Per spec §3 + §3.1: four tools, error mapping via custom exceptions to `CallToolResult.is_error` with `structured_content.code`. Lifespan calls `prereqs.run_prereqs`.

- [ ] **Step 1: Write failing tests.**

Create `tests/test_server.py`:

```python
"""Tests for harness_mcp.server — tool registration and error mapping."""

from __future__ import annotations

import pytest

from harness_mcp.server import _to_call_tool_error
from harness_mcp.types import (
    DesignDocNotFoundError,
    InvalidOptionsError,
    JobNotFinishedError,
    UnknownJobError,
)


class TestErrorMapper:
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

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_server.py -v
```

Expected: ImportError on `harness_mcp.server`.

- [ ] **Step 3: Implement `server.py`.**

Create `src/harness_mcp/server.py`:

```python
"""FastMCP server: lifespan + four tools + error mapper.

This module defines the MCP-tool entry points and the lifespan context
manager that runs `prereqs.run_prereqs` at startup. Spawned-agent
options factories live here too — they encapsulate the captured MCP
state so the lower-level modules don't need it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP

from harness_mcp.config import JobOptions, jobs_root
from harness_mcp.orchestrator import (
    cancel_job,
    register_scope,
    run_job,
    start_orchestrator_inserts_row,
    unregister_scope,
)
from harness_mcp.prereqs import (
    PrereqsResult,
    DoctorReport,
    run_prereqs,
)
from harness_mcp.prompts_loader import _resolved_prompt_text
from harness_mcp.state import (
    db_write,
    open_reader,
    TERMINAL_JOB_STATUSES,
)
from harness_mcp.types import (
    DesignDocNotFoundError,
    HarnessToolError,
    InvalidOptionsError,
    JobNotFinishedError,
    UnknownJobError,
)


_ERROR_CODES: dict[type[HarnessToolError], str] = {
    UnknownJobError: "UNKNOWN_JOB",
    JobNotFinishedError: "JOB_NOT_FINISHED",
    DesignDocNotFoundError: "DESIGN_DOC_NOT_FOUND",
    InvalidOptionsError: "INVALID_OPTIONS",
}


def _to_call_tool_error(exc: Exception) -> mcp_types.CallToolResult:
    """Convert a HarnessToolError into a CallToolResult with structured code."""
    code = "INTERNAL_ERROR"
    for cls, c in _ERROR_CODES.items():
        if isinstance(exc, cls):
            code = c
            break
    return mcp_types.CallToolResult(
        isError=True,
        content=[mcp_types.TextContent(type="text", text=f"{code}: {exc}")],
        structuredContent={"code": code, "message": str(exc)},
    )


# ---------- options factories ----------


def _make_planner_options_factory(prereqs_result: PrereqsResult, *, job_dir: Path):
    def _factory(**_kw: Any) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("planner.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=prereqs_result.setting_sources,
            mcp_servers={"context7": prereqs_result.captured_mcp["context7"]},
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
    return _factory


def _make_reviewer_options_factory(prereqs_result: PrereqsResult, *, job_dir: Path):
    def _factory(**_kw: Any) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("reviewer.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=prereqs_result.setting_sources,
            mcp_servers={"context7": prereqs_result.captured_mcp["context7"]},
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
    return _factory


def _make_evaluator_options_factory(prereqs_result: PrereqsResult, *, job_dir: Path):
    def _factory(**_kw: Any) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions
        mcp = {"context7": prereqs_result.captured_mcp["context7"]}
        if "playwright" in prereqs_result.captured_mcp:
            mcp["playwright"] = prereqs_result.captured_mcp["playwright"]
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("evaluator.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=prereqs_result.setting_sources,
            mcp_servers=mcp,
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",  # contract negotiation only — Bash not needed
        )
    return _factory


def _make_summarizer_options_factory(prereqs_result: PrereqsResult):
    def _factory(*, job_dir: Path) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("summarizer.md"),
            cwd=str(job_dir),
            setting_sources=prereqs_result.setting_sources,
            mcp_servers={"context7": prereqs_result.captured_mcp["context7"]},
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )
    return _factory


# ---------- lifespan + tools ----------


@dataclass
class ServerState:
    """Shared mutable across all tool calls — initialized in lifespan."""

    prereqs_result: PrereqsResult
    codex_bin: str
    task_group: anyio.abc.TaskGroup


_state: ServerState | None = None


def _client_factory(**kw: Any) -> Any:
    """Default client factory passed to prereqs probes."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    options = ClaudeAgentOptions(**kw) if kw else ClaudeAgentOptions()
    return ClaudeSDKClient(options=options)


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
    """Run startup prereqs; refuse the server on any failure."""
    global _state
    import os
    import shutil
    codex_bin = os.environ.get("HARNESS_CODEX_BIN") or shutil.which("codex") or "codex"

    report = DoctorReport()
    prereqs_result = await run_prereqs(
        client_factory=_client_factory, project_root=None, report=report,
    )
    async with anyio.create_task_group() as tg:
        _state = ServerState(prereqs_result=prereqs_result, codex_bin=codex_bin, task_group=tg)
        try:
            yield
        finally:
            _state = None


server = FastMCP("harness-mcp", lifespan=lifespan)


@server.tool()
async def start_build(design_doc_path: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start a new build job. Returns {job_id}."""
    p = Path(design_doc_path)
    if not p.is_file() or p.stat().st_size == 0:
        raise DesignDocNotFoundError(design_doc_path)
    job_options = JobOptions.from_dict(options)

    job_id = await start_orchestrator_inserts_row(design_doc_path=p, options=job_options)

    assert _state is not None
    job_dir = jobs_root() / job_id
    # anyio.TaskGroup.start_soon takes only positional args; bind keywords with partial.
    _state.task_group.start_soon(
        partial(
            run_job,
            job_id=job_id,
            options=job_options,
            prereqs_result=_state.prereqs_result,
            planner_options_factory=_make_planner_options_factory(_state.prereqs_result, job_dir=job_dir),
            reviewer_options_factory=_make_reviewer_options_factory(_state.prereqs_result, job_dir=job_dir),
            evaluator_options_factory=_make_evaluator_options_factory(_state.prereqs_result, job_dir=job_dir),
            summarizer_options_factory=_make_summarizer_options_factory(_state.prereqs_result),
            codex_bin=_state.codex_bin,
        )
    )
    return {"job_id": job_id}


@server.tool()
async def poll_build(job_id: str) -> dict[str, Any]:
    async with open_reader() as r:
        row = r.execute(
            "SELECT status, current_phase, last_message, plan_review_rounds, "
            "started_at, updated_at FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise UnknownJobError(job_id)
        sprints_completed = r.execute(
            "SELECT COUNT(*) FROM sprints WHERE job_id=? AND status='passed'", (job_id,),
        ).fetchone()[0]
    return {
        "status": row[0],
        "current_phase": row[1],
        "last_message": row[2] or "",
        "plan_review_rounds": row[3],
        "sprints_completed": sprints_completed,
        "started_at": row[4],
        "updated_at": row[5],
    }


@server.tool()
async def get_build_result(job_id: str) -> dict[str, Any]:
    async with open_reader() as r:
        row = r.execute(
            "SELECT status, current_phase, last_message, plan_review_rounds, "
            "started_at, finished_at FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise UnknownJobError(job_id)
        if row[0] not in TERMINAL_JOB_STATUSES:
            raise JobNotFinishedError(job_id)
        sprints = r.execute(
            "SELECT seq, title, status, retry_count FROM sprints WHERE job_id=? ORDER BY seq",
            (job_id,),
        ).fetchall()

    started_at, finished_at = row[4], row[5]
    duration = ((finished_at or 0) - started_at) / 1000.0

    job_dir = jobs_root() / job_id
    summary_path = job_dir / "summary.md"
    summary_text = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else (row[2] or "")

    return {
        "app_path": str(job_dir / "app"),
        "summary": summary_text,
        "final_status": row[0],
        "sprints": [
            {"seq": s[0], "title": s[1], "status": s[2], "retry_count": s[3]} for s in sprints
        ],
        "plan_review_rounds": row[3],
        "duration_seconds": duration,
    }


@server.tool()
async def cancel_build(job_id: str) -> dict[str, Any]:
    return await cancel_job(job_id)
```

- [ ] **Step 4: Run the server tests.**

```bash
uv run pytest tests/test_server.py -v
```

Expected: passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/server.py tests/test_server.py
uv run ruff format --check src/harness_mcp/server.py tests/test_server.py
```

Expected: zero findings.

---

## Task 6: `__main__.py` — CLI entry

**Files:**
- Create: `src/harness_mcp/__main__.py`

Two subcommands: `serve` (with `--transport stdio | streamable-http`, `--host`, `--port`) and `doctor`. Spec §10.1 says doctor runs the same prereq sequence and prints a human report.

- [ ] **Step 1: Write `__main__.py`.**

Create `src/harness_mcp/__main__.py`:

```python
"""harness-mcp CLI: `serve` and `doctor` subcommands."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import NoReturn

import anyio


def _run_serve(args: argparse.Namespace) -> int:
    """Boot the FastMCP server with the chosen transport.

    FastMCP exposes async transport methods as `run_stdio_async()` and
    `run_streamable_http_async()` (plus `run_sse_async()` if needed) — the
    bare `run_stdio` / `run_streamable_http` names are NOT awaitable. Verify
    the exact method names against the pinned `mcp` version before lock-in;
    if the SDK rename happens, update both call sites here.
    """
    from harness_mcp.server import server

    transport = args.transport
    if transport == "stdio":
        anyio.run(server.run_stdio_async)
    elif transport == "streamable-http":
        anyio.run(
            lambda: server.run_streamable_http_async(host=args.host, port=args.port)
        )
    else:
        print(f"unknown transport: {transport}", file=sys.stderr)
        return 1
    return 0


def _run_doctor(_args: argparse.Namespace) -> int:
    """Run lifespan prereqs, print a human report."""
    from harness_mcp.prereqs import (
        DoctorReport,
        PrereqFailedError,
        format_doctor_report,
        run_prereqs,
    )
    from harness_mcp.server import _client_factory

    report = DoctorReport()

    async def _run() -> None:
        try:
            await run_prereqs(client_factory=_client_factory, project_root=None, report=report)
        except PrereqFailedError as e:
            report.add("FAILED", "FAIL", str(e))
            raise

    try:
        anyio.run(_run)
    except PrereqFailedError:
        print(format_doctor_report(report))
        return 1
    print(format_doctor_report(report))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="harness-mcp")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the MCP server")
    serve.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=_run_serve)

    doctor = sub.add_parser("doctor", help="Run lifespan prereq checks and exit")
    doctor.set_defaults(func=_run_doctor)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Validate syntax + imports.**

```bash
uv run python -c "import harness_mcp.__main__"
```

Expected: no error. The CLI itself is hard to unit-test because it boots the SDK; smoke covers it (Task 8).

- [ ] **Step 3: Lint.**

```bash
uv run ruff check src/harness_mcp/__main__.py
uv run ruff format --check src/harness_mcp/__main__.py
```

Expected: zero findings.

---

## Task 7: `README.md`

**Files:**
- Modify: `README.md` (it currently exists with one line)

Per spec §14: full README with Setup, Required env vars, Required MCP servers, Required skills, Example mcp.json, Quickstart, Notable decisions (verbatim from §13), Troubleshooting, Limitations.

- [ ] **Step 1: Write the README.**

Overwrite `README.md`:

```markdown
# harness-mcp

An MCP server that orchestrates multi-hour, multi-agent application builds from a feature design document. Hand it `design.md`; it spawns Planner/Reviewer/Generator/Evaluator agents in a loop until the app passes hard pass/fail criteria for every feature.

## Setup

1. Install the Codex CLI (https://github.com/openai/codex) and confirm `codex --version` works.
2. Configure `~/.codex/config.toml` — at minimum set your model. Example:

   ```toml
   model = "claude-sonnet-4-6"
   model_provider = "anthropic"
   ```

3. Install harness-mcp with uv:

   ```bash
   uv pip install harness-mcp     # or: uv pip install -e . from a checkout
   ```

4. Verify everything:

   ```bash
   harness-mcp doctor
   ```

   Expected: a list of `OK` lines for paths, env, codex, codex-shape, skill, mcp, strict-mcp-config, restart_sweep.

## Required environment variables

| Var | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
| `HARNESS_CODEX_BIN` | no  | Override `which codex`. Useful when codex isn't on PATH. |

(Codex auth lives in `~/.codex/auth.json`; no `OPENAI_API_KEY` needed.)

## Required MCP servers

- **context7** (HARD): used by Planner and Generator for library documentation.
- **playwright** (SOFT): used by Evaluator for dynamic verification of UI-bearing sprints. Optional — the server warns at startup if missing and only hard-fails when a sprint actually needs it.

The harness reads these from your existing Claude Code settings (`~/.claude.json`); no separate config file needed.

## Required skills

- **superpowers:writing-plans** (HARD): used by Planner. Install via the superpowers plugin at user scope.

## Example mcp.json

stdio (default — child process of your MCP client):

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

streamable-http (daemon — jobs survive client disconnects):

```bash
ANTHROPIC_API_KEY=... harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

```json
{
  "mcpServers": {
    "harness-mcp": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

## Quickstart

1. Write a design document (markdown).
2. From your client, call `start_build(design_doc_path="<absolute path>")`.
3. Poll with `poll_build(job_id)` until status is terminal.
4. `get_build_result(job_id)` returns the final summary, the path to the built app, and per-sprint pass/fail.

## Notable decisions

1. **File-mediated handoffs over message-passing.** Auditability + survives agent restarts + lets us run unit tests on the parsers.
2. **Reset over compaction for context anxiety.** Per the article: compaction preserves the anxious "I've been working a long time" feel. Only full resets clear it.
3. **Static audit before dynamic verification.** Catching "code looks plausible but skipped requirement X" before booting the app is cheaper and more reliable than discovering it via behavioral testing.
4. **Evaluator-managed app lifecycle + orchestrator process-group cleanup.** Flexibility per project shape; deterministic cleanup against leaked dev servers and orphan Playwright browsers.
5. **One ClaudeSDKClient across static→dynamic.** The static audit's reasoning is high-value context for the dynamic pass.
6. **Contract-round file ownership by orchestrator.** Agents emit messages; orchestrator owns structure. Lets us run cheap structural validations.
7. **Force `sandbox=workspace-write` + `approval_policy=never` for Codex; honor user `model` choice.** Autonomous-run requirements override user preferences only where required for unattended operation.
8. **Explicit MCP allowlist for spawned agents.** Closes the agent-recursion door even though the user has harness-mcp in their settings.
9. **ULID job IDs.** Sortable directory listings, no extra index needed.
10. **LLM-generated summary.** Costs one extra Claude call; produces a far more digestible job-end readout than mechanical concatenation.
11. **`plan-document-reviewer-prompt.md` over `code-review:code-review`.** The latter is built for GitHub PRs (uses `gh`, posts comments back). The former is the purpose-built plan-doc reviewer template that ships in `superpowers:writing-plans`.
12. **Two transports (stdio default, streamable-http for daemon use).** Stdio for ad-hoc; HTTP daemon for multi-hour jobs that should survive client disconnects.
13. **Untagged reviewer issues default to `[implementation]`.** Conservative under uncertainty — better to do an extra revision round than to silently drop a real issue.
14. **No tool restrictions on spawned Claude agents (preset claude_code tool set).** System prompts are the guardrail. Tradeoff acknowledged: a determined agent could escape via Bash; sandbox is best-effort, not OS-level.
15. **Concurrent UI-bearing jobs may contend on Playwright MCP.** Documented operational caveat: if running multiple jobs in parallel that both reach dynamic-verification UI sprints simultaneously, expect Playwright resource conflicts.

## Troubleshooting

- **"context7 not connected"** — check Claude Code's MCP config; `harness-mcp doctor` shows the resolution path.
- **Codex hangs** — ensure `~/.codex/config.toml` doesn't have `approval_policy=on-request`; the harness forces `never` regardless, so most hangs trace to the binary not exiting on completion.
- **Playwright tests fail with "browser not found"** — reinstall Playwright browsers via the playwright MCP plugin's install command.

## Limitations

- Sandbox is best-effort (cwd + system prompt + permission_mode), not OS-level. A determined agent can escape via Bash. For stronger isolation, run harness-mcp inside a container.
- Concurrent UI-bearing jobs contend on the single Playwright MCP. Run UI-heavy jobs sequentially.
- Workers die with the server: closing your client (under stdio transport) ends in-flight jobs as `interrupted`. Use streamable-http daemon mode for multi-hour jobs.
```

- [ ] **Step 2: Verify the README is non-empty and includes each spec-required section.**

```bash
for section in 'Setup' 'Required environment' 'Required MCP servers' 'Required skills' 'Quickstart' 'Notable decisions' 'Troubleshooting' 'Limitations'; do
    grep -q "$section" README.md || { echo "MISSING SECTION: $section"; exit 1; }
done
echo "README sections OK"
```

Expected: `README sections OK`.

---

## Task 8: `tests/smoke.py`

**Files:**
- Create: `tests/smoke.py`

Per spec §12.2: real end-to-end run against `examples/todo-app-design.md`. Manual; excluded from default `pytest` collection by name (`smoke.py` doesn't match `test_*.py` glob).

- [ ] **Step 1: Write the smoke test.**

Create `tests/smoke.py`:

```python
"""Manual smoke test: end-to-end harness run against examples/todo-app-design.md.

Run via: `uv run python tests/smoke.py`. Excluded from `pytest` collection
because the filename doesn't match `test_*.py`.

Asserts (in order):
  1. `harness-mcp doctor` exits 0 with `OK` lines for every prereq step.
  2. `start_build` returns a 26-char ULID job_id.
  3. `poll_build` advances through phases (planning → plan-review → sprint-1/* → ... → done).
  4. `get_build_result` raises `JOB_NOT_FINISHED` while running.
  5. After completion: `final_status == "completed"`, app_path exists, summary >= 30 chars.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import anyio

REPO = Path(__file__).resolve().parent.parent
DESIGN = REPO / "examples" / "todo-app-design.md"


def assert_doctor_ok() -> None:
    proc = subprocess.run(
        ["uv", "run", "harness-mcp", "doctor"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"doctor failed (rc={proc.returncode})")
    if "OK" not in proc.stdout:
        raise SystemExit(f"doctor stdout has no OK line:\n{proc.stdout}")
    print("[smoke] doctor OK")


async def run_build() -> None:
    """Drive start_build → poll_build → get_build_result via the in-process server module."""
    # Prereqs already validated; import server lazily so doctor's failures are surfaced first.
    from harness_mcp.server import (
        cancel_build,
        get_build_result,
        poll_build,
        start_build,
    )
    from harness_mcp.types import JobNotFinishedError

    job = await start_build(design_doc_path=str(DESIGN))
    job_id = job["job_id"]
    assert re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", job_id), job_id
    print(f"[smoke] started job {job_id}")

    seen_phases: set[str] = set()
    deadline = time.time() + 60 * 60  # 1h cap
    while time.time() < deadline:
        status = await poll_build(job_id)
        seen_phases.add(status["current_phase"])
        if status["status"] in ("completed", "failed", "cancelled", "interrupted"):
            break
        # Confirm get_build_result rejects non-terminal jobs.
        try:
            await get_build_result(job_id)
        except JobNotFinishedError:
            pass
        await anyio.sleep(15)

    final = await get_build_result(job_id)
    assert final["final_status"] == "completed", final["final_status"]
    assert Path(final["app_path"]).is_dir()
    assert len(final["summary"]) >= 30, final["summary"]
    expected_phases = {"planning", "plan-review", "summarizing", "done"}
    assert expected_phases.issubset(seen_phases), seen_phases
    print(f"[smoke] completed: {final['summary']}")


def main() -> int:
    assert_doctor_ok()
    anyio.run(run_build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Confirm the smoke test imports cleanly without running.**

```bash
uv run python -c "import importlib.util, sys; spec = importlib.util.spec_from_file_location('smoke', 'tests/smoke.py'); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print('OK')"
```

Expected: `OK`. Do NOT run the smoke test in this plan — it requires real SDK setup.

- [ ] **Step 3: Verify pytest does NOT collect smoke.py by default.**

```bash
uv run pytest --collect-only 2>&1 | grep -c "smoke" || true
```

Expected: `0`. (Pytest collects only `test_*.py` files; `smoke.py` is excluded by name.)

---

## Task 9: Final integration sweep

- [ ] **Step 1: Full pytest.**

```bash
uv run pytest tests/ -v
```

Expected: every test from Parts 1–5 passes (smoke.py is correctly skipped).

- [ ] **Step 2: Full ruff lint + format.**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: zero findings.

- [ ] **Step 3: Confirm import graph still clean.**

```bash
uv run python -c "
import importlib, sys
for mod in list(sys.modules):
    if mod == 'harness_mcp.state': sys.modules.pop(mod)
importlib.import_module('harness_mcp.evaluator_runner')
assert 'harness_mcp.state' not in sys.modules, 'state.py leaked into launcher'
print('OK launcher isolation')
"
```

Expected: `OK launcher isolation`.

- [ ] **Step 4: Confirm `harness-mcp` console script resolves.**

```bash
uv run harness-mcp --help
```

Expected: argparse usage line listing `serve` and `doctor` subcommands.

- [ ] **Step 5: Confirm `main` branch, no commits.**

```bash
git status --short && git rev-parse --abbrev-ref HEAD
```

Expected: untracked / modified files only, branch `main`. **Do not commit.**

- [ ] **Step 6: Print full file inventory for hand-off.**

```bash
find src tests examples README.md pyproject.toml -type f \( -name '*.py' -o -name '*.md' -o -name '*.json' -o -name '*.toml' \) 2>/dev/null | sort
```

Expected: a long list covering every file added in Parts 1–5.

---

## Done criteria

- All 9 tasks complete.
- `uv run pytest tests/ -v` passes (Parts 1–5).
- `uv run ruff check .` and `uv run ruff format --check .` exit 0.
- `harness_mcp.evaluator_runner` does not transitively import `harness_mcp.state`.
- `uv run harness-mcp --help` works.
- `uv run harness-mcp doctor` runs against the user's environment (manual confirmation; may FAIL if context7 isn't installed — document any FAILS the user should fix before running smoke).
- `tests/smoke.py` is importable but not collected by pytest.
- README contains every spec §14 section.
- Repo on `main`, NO commits.

The harness-mcp server is now feature-complete. Final integration validation:
1. User runs `harness-mcp doctor` and resolves any FAIL outputs (typically: install context7 MCP).
2. User runs `tests/smoke.py` to confirm an end-to-end build of the TODO example.
3. User integrates with their MCP client via one of the `examples/mcp.json.*` configs.
