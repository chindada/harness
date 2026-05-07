"""Tests for harness_mcp.sprints — contract negotiation, evaluation, retry loop."""

from __future__ import annotations

import sys
import textwrap
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from harness_mcp import sprints
from harness_mcp.config import JobOptions
from harness_mcp.sprints import (
    ContractNegotiationFailedError,
    negotiate_contract,
    run_evaluation,
    run_sprint,
)
from harness_mcp.types import EvaluationResult, ImplementationResult


def _agent_msg(text: str) -> object:
    return SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta=text))


def _turn_completed() -> object:
    return SimpleNamespace(
        method="turn/completed", payload=SimpleNamespace(turn=SimpleNamespace(id="t1"))
    )


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class AssistantMessage:
    def __init__(self, content: list[TextBlock]) -> None:
        self.content = content


def _claude_text_msg(text: str) -> object:
    return AssistantMessage(content=[TextBlock(text)])


@pytest.fixture
def fake_codex_factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Returns a closure that builds a fake AsyncCodex which yields scripted bodies."""

    def make(scripted_bodies: list[str]) -> Any:
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
def fake_query_factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch claude_agent_sdk.query with a script."""

    def make(scripted_bodies: list[str]) -> None:
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_codex_factory: Any,
        fake_query_factory: Any,
    ) -> None:
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("# Sprint 1: Title\n", encoding="utf-8")

        # Both Generator and Evaluator emit APPROVED in round 1.
        monkeypatch.setattr(
            sprints,
            "AsyncCodex",
            fake_codex_factory(["1. server starts\n2. tests pass\nAPPROVED"]),
        )
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_codex_factory: Any,
        fake_query_factory: Any,
    ) -> None:
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("# Sprint 1: Title\n", encoding="utf-8")

        # Round 1: Generator proposes, Evaluator critiques (no APPROVED).
        # Round 2: Generator revises with APPROVED, Evaluator APPROVED.
        monkeypatch.setattr(
            sprints,
            "AsyncCodex",
            fake_codex_factory(
                [
                    "criterion 1: x\ncriterion 2: y",
                    "criterion 1: x\ncriterion 2: y\ncriterion 3: z\nAPPROVED",
                ]
            ),
        )
        fake_query_factory(
            [
                "missing criterion 3",
                "looks good\nAPPROVED",
            ]
        )

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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_codex_factory: Any,
        fake_query_factory: Any,
    ) -> None:
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")

        # Always-disagreeing parties.
        monkeypatch.setattr(
            sprints,
            "AsyncCodex",
            fake_codex_factory(
                [
                    "round 1 criteria",
                    "round 2 criteria",
                    "round 3 criteria",
                ]
            ),
        )
        fake_query_factory(
            [
                "round 1 critique",
                "round 2 critique",
                "round 3 critique",
            ]
        )

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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_codex_factory: Any,
        fake_query_factory: Any,
    ) -> None:
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")

        monkeypatch.setattr(sprints, "AsyncCodex", fake_codex_factory(["", ""]))
        fake_query_factory(["", ""])

        with pytest.raises(ContractNegotiationFailedError):
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


class TestRunEvaluation:
    @pytest.mark.asyncio
    async def test_invokes_launcher_via_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        monkeypatch.setattr(sprints, "_launcher_command", lambda: [sys.executable, str(stub)])

        job_id = "JOBID"
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

    @pytest.mark.asyncio
    async def test_phase_dynamic_marker_drives_phase_setter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §4.4:280 — launcher emits `PHASE:eval-dynamic` on stderr after
        the static section is written; orchestrator captures it via the stderr
        drain and flips current_phase to `sprint-<N>/eval-dynamic`."""
        # Stub launcher: writes a valid eval.md AND emits the phase marker on stderr.
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
                # Emit the phase marker mid-run (after static, before dynamic).
                print("PHASE:eval-dynamic", file=sys.stderr, flush=True)
                """
            ).strip()
        )
        monkeypatch.setattr(sprints, "_launcher_command", lambda: [sys.executable, str(stub)])

        job_id = "JOBID"
        job_dir = tmp_path / "jobs" / job_id
        sprint_dir = job_dir / "sprint-1"
        sprint_dir.mkdir(parents=True)
        (sprint_dir / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")

        captured: list[str] = []

        async def capture(p: str) -> None:
            captured.append(p)

        result = await run_evaluation(
            job_id=job_id,
            sprint_seq=1,
            sprint_dir=sprint_dir,
            job_dir=job_dir,
            captured_mcp={"context7": {"command": "ctx7"}},
            setting_sources=["user"],
            options=JobOptions(max_evaluation_seconds=30),
            prior_tag=None,
            phase_setter=capture,
        )
        assert result.passed is True
        assert "sprint-1/eval-dynamic" in captured


class TestRunSprint:
    @pytest.mark.asyncio
    async def test_first_attempt_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
                sprint_seq=1,
                static_criteria=[],
                dynamic_criteria=[],
                routing_decision="",
                passed=True,
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        result = await run_sprint(
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
        async def fake_negotiate(**_kw: Any) -> bool:
            (_kw["sprint_dir"] / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")
            return True

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            return ImplementationResult(ok=True, commit_sha="abc", summary="done")

        eval_attempts = [0]

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            eval_attempts[0] += 1
            return EvaluationResult(
                sprint_seq=1,
                static_criteria=[],
                dynamic_criteria=[],
                routing_decision="",
                passed=(eval_attempts[0] == 2),
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        result = await run_sprint(
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

    @pytest.mark.asyncio
    async def test_phase_setter_called_for_sprint_transitions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §4.4:277-280 — phase_setter receives implementing→eval-static
        transitions for a passing sprint. (eval-dynamic is launcher-emitted
        and exercised in TestRunEvaluation.)"""

        async def fake_negotiate(**_kw: Any) -> bool:
            (_kw["sprint_dir"] / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")
            return True

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            return ImplementationResult(ok=True, commit_sha="abc", summary="done")

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            return EvaluationResult(
                sprint_seq=1,
                static_criteria=[],
                dynamic_criteria=[],
                routing_decision="",
                passed=True,
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        phases: list[str] = []

        async def capture(p: str) -> None:
            phases.append(p)

        result = await run_sprint(
            job_id="JOBID",
            sprint_seq=1,
            sprint_title="Title",
            job_dir=job_dir,
            options=JobOptions(),
            captured_mcp={"context7": {}},
            setting_sources=["user"],
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            prior_tag=None,
            phase_setter=capture,
        )
        assert result.passed is True
        assert phases == ["sprint-1/implementing", "sprint-1/eval-static"]

    @pytest.mark.asyncio
    async def test_contract_not_sealed_retries_on_next_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §6.1:408 — non-convergence after the cap counts as one retry;
        the next sprint attempt restarts negotiation with a fresh contract.md."""
        attempts_at_negotiate = [0]

        async def fake_negotiate(**_kw: Any) -> bool:
            attempts_at_negotiate[0] += 1
            sprint_dir: Path = _kw["sprint_dir"]
            # First attempt: never seals. Second: seals.
            if attempts_at_negotiate[0] == 1:
                return False
            (sprint_dir / "contract.md").write_text(
                "# Sprint 1\n## Round 1 — Generator\nAPPROVED\n", encoding="utf-8"
            )
            return True

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            return ImplementationResult(ok=True, commit_sha="abc", summary="done")

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            return EvaluationResult(
                sprint_seq=1,
                static_criteria=[],
                dynamic_criteria=[],
                routing_decision="",
                passed=True,
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        result = await run_sprint(
            job_id="JOBID",
            sprint_seq=1,
            sprint_title="Title",
            job_dir=job_dir,
            options=JobOptions(max_sprint_retries=2),
            captured_mcp={"context7": {}},
            setting_sources=["user"],
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            prior_tag=None,
        )
        assert result.passed is True
        assert result.attempts == 2  # one retry consumed by failed negotiation
        assert attempts_at_negotiate[0] == 2  # negotiation re-ran on attempt 2

    @pytest.mark.asyncio
    async def test_contract_negotiation_no_progress_then_seals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §6.1:404 — both-empty-bodies aborts via
        ContractNegotiationFailedError; next attempt restarts negotiation."""
        attempts = [0]

        async def fake_negotiate(**_kw: Any) -> bool:
            attempts[0] += 1
            sprint_dir: Path = _kw["sprint_dir"]
            if attempts[0] == 1:
                raise ContractNegotiationFailedError("both empty")
            (sprint_dir / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")
            return True

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            return ImplementationResult(ok=True, commit_sha="abc", summary="done")

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            return EvaluationResult(
                sprint_seq=1,
                static_criteria=[],
                dynamic_criteria=[],
                routing_decision="",
                passed=True,
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        result = await run_sprint(
            job_id="JOBID",
            sprint_seq=1,
            sprint_title="Title",
            job_dir=job_dir,
            options=JobOptions(max_sprint_retries=2),
            captured_mcp={"context7": {}},
            setting_sources=["user"],
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            prior_tag=None,
        )
        assert result.passed is True
        assert attempts[0] == 2

    @pytest.mark.asyncio
    async def test_sprint_timeout_returns_sprint_timeout_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §6.5 — `anyio.fail_after(max_sprint_duration_minutes * 60)`
        wraps stages 1-3; on expiry, the sprint result must report
        error='sprint_timeout' (not the generic max_sprint_retries_exceeded)."""

        async def fake_negotiate(**_kw: Any) -> bool:
            (_kw["sprint_dir"] / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")
            return True

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            # Sleep longer than the sprint budget so anyio.fail_after fires.
            await anyio.sleep(10)
            return ImplementationResult(ok=True)

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            return EvaluationResult(
                sprint_seq=1,
                static_criteria=[],
                dynamic_criteria=[],
                routing_decision="",
                passed=True,
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        # Use a 1-second timeout (max_sprint_duration_minutes is integer-only,
        # so monkeypatch the conversion to use seconds-as-minutes).
        original_fail_after = anyio.fail_after

        def short_fail_after(_seconds: float) -> Any:
            return original_fail_after(0.5)

        monkeypatch.setattr(sprints.anyio, "fail_after", short_fail_after)

        result = await run_sprint(
            job_id="JOBID",
            sprint_seq=1,
            sprint_title="Title",
            job_dir=job_dir,
            options=JobOptions(max_sprint_retries=1),
            captured_mcp={"context7": {}},
            setting_sources=["user"],
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            prior_tag=None,
        )
        assert result.passed is False
        assert result.error == "sprint_timeout"

    @pytest.mark.asyncio
    async def test_unparseable_eval_exhaustion_surfaces_specific_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §6.3:516 — when retries exhaust on persistently unparseable
        eval.md, the terminal error_text must be
        `evaluator_emitted_unparseable_eval_md`, not the generic
        `max_sprint_retries_exceeded`."""

        async def fake_negotiate(**_kw: Any) -> bool:
            (_kw["sprint_dir"] / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")
            return True

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            return ImplementationResult(ok=True, commit_sha="abc", summary="done")

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            return EvaluationResult(
                sprint_seq=1,
                static_criteria=[],
                dynamic_criteria=[],
                routing_decision="",
                passed=False,
                unparseable=True,
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        result = await run_sprint(
            job_id="JOBID",
            sprint_seq=1,
            sprint_title="Title",
            job_dir=job_dir,
            options=JobOptions(max_sprint_retries=1),
            captured_mcp={"context7": {}},
            setting_sources=["user"],
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            prior_tag=None,
        )
        assert result.passed is False
        assert result.error == "evaluator_emitted_unparseable_eval_md"

    @pytest.mark.asyncio
    async def test_unparseable_eval_with_stderr_tail_on_nonzero_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §8.4:1023 — when launcher exits non-zero, the captured stderr
        tail must surface in the terminal SprintResult.error."""

        async def fake_negotiate(**_kw: Any) -> bool:
            (_kw["sprint_dir"] / "contract.md").write_text("# Sprint 1\n", encoding="utf-8")
            return True

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            return ImplementationResult(ok=True, commit_sha="abc", summary="done")

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            return EvaluationResult(
                sprint_seq=1,
                static_criteria=[],
                dynamic_criteria=[],
                routing_decision="",
                passed=False,
                unparseable=True,
                launcher_stderr_tail="Traceback: SDKError: bad MCP config\n",
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        result = await run_sprint(
            job_id="JOBID",
            sprint_seq=1,
            sprint_title="Title",
            job_dir=job_dir,
            options=JobOptions(max_sprint_retries=1),
            captured_mcp={"context7": {}},
            setting_sources=["user"],
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            prior_tag=None,
        )
        assert result.passed is False
        assert result.error is not None
        assert "evaluator_emitted_unparseable_eval_md" in result.error
        assert "SDKError: bad MCP config" in result.error

    @pytest.mark.asyncio
    async def test_contract_never_seals_exhausts_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §6.1:408 — repeated non-convergence eventually exhausts
        max_sprint_retries; sprint fails with the last negotiation error."""

        async def fake_negotiate(**_kw: Any) -> bool:
            return False  # never seals

        async def fake_chunk_loop(**_kw: Any) -> ImplementationResult:
            return ImplementationResult(ok=True)

        async def fake_run_eval(**_kw: Any) -> EvaluationResult:
            return EvaluationResult(
                sprint_seq=1,
                static_criteria=[],
                dynamic_criteria=[],
                routing_decision="",
                passed=True,
            )

        monkeypatch.setattr(sprints, "negotiate_contract", fake_negotiate)
        monkeypatch.setattr(sprints, "chunk_loop", fake_chunk_loop)
        monkeypatch.setattr(sprints, "run_evaluation", fake_run_eval)

        job_dir = tmp_path / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "design.md").write_text("D")
        (job_dir / "plan.md").write_text("## Sprint 1: Title\n")

        result = await run_sprint(
            job_id="JOBID",
            sprint_seq=1,
            sprint_title="Title",
            job_dir=job_dir,
            options=JobOptions(max_sprint_retries=1),
            captured_mcp={"context7": {}},
            setting_sources=["user"],
            generator_md="G",
            evaluator_options_factory=lambda **_kw: object(),
            codex_bin="/usr/bin/codex",
            codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
            prior_tag=None,
        )
        assert result.passed is False
        assert result.error == "contract_not_sealed"
