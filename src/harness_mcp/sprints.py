"""Sprint loop: stages 1 (contract) -> 2 (impl) -> 3 (eval) -> 4 (retry).

Stage 1 here. Stages 2-4 added in Tasks 2-3.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from harness_mcp.config import JobOptions
from harness_mcp.contracts import (
    append_round_atomic,
    is_approved_body,
    parse_round_body_from_claude_msgs,
    parse_round_body_from_codex_events,
)
from harness_mcp.evaluator import parse_eval_md
from harness_mcp.generator import chunk_loop
from harness_mcp.process_group import PIPE, ProcessGroupScope
from harness_mcp.types import (
    EvaluationResult,
    EvaluatorEmittedUnparseableEvalMdError,
    HarnessToolError,
)

logger = logging.getLogger(__name__)

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


class ContractNegotiationFailedError(HarnessToolError):
    """Both Generator and Evaluator emitted empty bodies in the same round."""


# ---------- Stage 1: contract negotiation ----------


def _round_aware_addendum(role: str, n: int, contract_text: str) -> str:
    return (
        "## Round instruction\n"
        f"The contract above contains {n} completed rounds. Emit ROUND {n + 1} only.\n"
        "Either propose a revision that addresses the latest opposite-side feedback, OR emit "
        "`APPROVED` (the literal token, on its own line at the end of your response) if you accept "
        "the latest counter-proposal verbatim. Do NOT re-propose criteria you have already proposed "  # noqa: E501
        "unchanged in earlier rounds."
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
    cfg = (
        AppServerConfig(
            codex_bin=codex_bin,
            cwd=str(cwd),
            config_overrides=codex_overrides,
            client_name="harness-mcp",
            client_title="Harness Generator",
            client_version="0.1.0",
        )
        if AppServerConfig is not None
        else None
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
    options: Any,  # noqa: ANN401 — SDK options dict; importing the SDK type breaks lazy imports
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

        # Spec §6.1: log one-sided empty bodies (still counts toward max rounds).
        if not gen_body:
            logger.warning(
                "sprint %d round %d: Generator emitted empty body; "
                "treating as no-op (counts toward max_contract_negotiation_rounds)",
                sprint_seq,
                n + 1,
            )
        if not eval_body:
            logger.warning(
                "sprint %d round %d: Evaluator emitted empty body; "
                "treating as no-op (counts toward max_contract_negotiation_rounds)",
                sprint_seq,
                n + 1,
            )

        if gen_body:
            append_round_atomic(contract_path, f"\n## Round {n + 1} — Generator\n", gen_body + "\n")
        if eval_body:
            append_round_atomic(
                contract_path, f"\n## Round {n + 1} — Evaluator\n", eval_body + "\n"
            )

        rounds_completed += 1

        if is_approved_body(gen_body) and is_approved_body(eval_body):
            return True

    return False


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

    On non-zero exit OR parse failure -> returns EvaluationResult with
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

    rc = 0
    async with ProcessGroupScope(f"eval-{job_id}-{sprint_seq}") as pg:
        proc = await pg.spawn(
            _launcher_command(),
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
        )
        await pg.communicate(proc, payload_bytes)

        async def _drain(stream: Any) -> None:  # noqa: ANN401 — opaque process stream
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
    """Run one sprint end-to-end: contract -> impl -> eval -> retry."""
    sprint_dir = job_dir / f"sprint-{sprint_seq}"
    sprint_dir.mkdir(parents=True, exist_ok=True)
    contract_path = sprint_dir / "contract.md"
    if not contract_path.is_file():
        contract_path.write_text(f"# Sprint {sprint_seq}: {sprint_title}\n", encoding="utf-8")

    design_text = (
        (job_dir / "design.md").read_text(encoding="utf-8")
        if (job_dir / "design.md").is_file()
        else ""
    )
    plan_text = (
        (job_dir / "plan.md").read_text(encoding="utf-8") if (job_dir / "plan.md").is_file() else ""
    )
    plan_section_text = _slice_plan_section(plan_text, sprint_seq)
    log_path = sprint_dir / "log.txt"

    # Spec §7.0 Shape 2: the first chunk's prompt requires the sprint's plan
    # section verbatim. Materialize it on disk so chunk_loop can pass the path
    # down to build_chunk_prompt (the SDK reads via Path, not text).
    plan_section_file = sprint_dir / "plan_section.md"
    plan_section_file.write_text(plan_section_text, encoding="utf-8")

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
                            sprint_seq=sprint_seq,
                            passed=False,
                            attempts=attempts,
                            error="contract_not_sealed",
                        )

                impl_result = await chunk_loop(
                    app_dir=job_dir / "app",
                    sprint_dir=sprint_dir,
                    contract_path=contract_path,
                    design_path=(job_dir / "design.md"),
                    plan_section_path=plan_section_file,
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
                        sprint_seq=sprint_seq,
                        passed=False,
                        attempts=attempts,
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
                # Failed eval -> retry path uses this eval.md as input.
                eval_md_for_retry = sprint_dir / "eval.md"
        except TimeoutError:
            return SprintResult(
                sprint_seq=sprint_seq,
                passed=False,
                attempts=attempts,
                error="sprint_timeout",
            )

    return SprintResult(
        sprint_seq=sprint_seq,
        passed=False,
        attempts=attempts,
        error="max_sprint_retries_exceeded",
    )
