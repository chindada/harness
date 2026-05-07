"""Evaluator helpers: eval.md parsing, sync helper, prompt builders, log piping.

The launcher subprocess (evaluator_runner.py) drives the §8.2/§8.3
modes — static audit and dynamic verification — using these helpers
plus `ClaudeSDKClient`. (Contract-review mode is the Evaluator's role
during §6.1 sprint negotiation, but that path runs in-process via
`sprints.negotiate_contract` against `claude_agent_sdk.query()`, not
through this launcher; it does not consume `parse_eval_md` and never
writes `eval.md`.) The orchestrator imports `parse_eval_md` to digest
the launcher's eval.md output.

The static / dynamic prompt builders live here so the launcher and any
orchestrator-side debugging can call the same functions.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import anyio
from anyio import to_thread

from harness_mcp.types import (
    Criterion,
    EvaluationResult,
    EvaluatorEmittedUnparseableEvalMdError,
)

# ---------- parse_eval_md ----------


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_CRITERION_RE = re.compile(r"^###\s+Criterion\s+(\d+):\s*(.+?)\s*$", re.MULTILINE)
_RESULT_RE = re.compile(r"^\*\*Result:\*\*\s*(\w+)", re.MULTILINE)
_EVIDENCE_RE = re.compile(
    r"^\*\*Evidence:\*\*\s*(.+?)(?=\n\*\*|\n###|\n##|\Z)", re.MULTILINE | re.DOTALL
)
_NOTES_RE = re.compile(r"^\*\*Notes:\*\*\s*(.+?)(?=\n\*\*|\n###|\n##|\Z)", re.MULTILINE | re.DOTALL)


def _section_slice(text: str, heading: str) -> str:
    """Return the body under the FIRST occurrence of `## <heading>` until the next `## ` or EOF."""
    headings = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(headings):
        if m.group(1).strip().lower() == heading.lower():
            start = m.end()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            return text[start:end]
    return ""


def _parse_criterion_blocks(section_body: str) -> list[Criterion]:
    """Walk `### Criterion <n>:` blocks; build Criterion entries."""
    out: list[Criterion] = []
    matches = list(_CRITERION_RE.finditer(section_body))
    for i, m in enumerate(matches):
        block_text = section_body[
            m.start() : matches[i + 1].start() if i + 1 < len(matches) else len(section_body)
        ]
        result_m = _RESULT_RE.search(block_text)
        evidence_m = _EVIDENCE_RE.search(block_text)
        notes_m = _NOTES_RE.search(block_text)
        if not result_m:
            continue
        result = result_m.group(1).strip().upper()
        if result not in ("PASS", "FAIL"):
            continue
        out.append(
            Criterion(
                text=m.group(2).strip(),
                result=result,
                evidence=(evidence_m.group(1).strip() if evidence_m else ""),
                notes=(notes_m.group(1).strip() if notes_m else ""),
            )
        )
    return out


def _extract_routing_decision(dynamic_body: str) -> str:
    """Body under `### Routing decision` until the next `### `."""
    routing_re = re.compile(r"^###\s+Routing decision\s*$", re.MULTILINE)
    next_h3_re = re.compile(r"^###\s+", re.MULTILINE)
    m = routing_re.search(dynamic_body)
    if not m:
        return ""
    start = m.end()
    n = next_h3_re.search(dynamic_body, pos=start)
    end = n.start() if n else len(dynamic_body)
    return dynamic_body[start:end].strip()


def parse_eval_md(path: Path, *, sprint_seq: int) -> EvaluationResult:
    """Parse eval.md into an EvaluationResult.

    Raise EvaluatorEmittedUnparseableEvalMdError if the file is missing
    or contains zero parseable Criterion blocks across both sections.
    """
    if not path.is_file():
        raise EvaluatorEmittedUnparseableEvalMdError(f"eval.md missing: {path}")

    text = path.read_text(encoding="utf-8")
    static_section = _section_slice(text, "Static audit")
    dynamic_section = _section_slice(text, "Dynamic verification")

    static = _parse_criterion_blocks(static_section)
    dynamic = _parse_criterion_blocks(dynamic_section)

    if not static and not dynamic:
        raise EvaluatorEmittedUnparseableEvalMdError(
            f"eval.md contains zero parseable criterion blocks: {path}"
        )

    routing = _extract_routing_decision(dynamic_section)
    passed = bool(static or dynamic) and all(c.result == "PASS" for c in static + dynamic)

    return EvaluationResult(
        sprint_seq=sprint_seq,
        static_criteria=static,
        dynamic_criteria=dynamic,
        routing_decision=routing,
        passed=passed,
    )


# ---------- sync_eval_md ----------


def _fsync_dir(d: Path) -> None:
    """fsync a directory entry so a recently-renamed-into file is durable.

    POSIX-only; on Windows the `os.open(..., O_RDONLY)` path raises and
    we fall through silently. The harness's primary platform is POSIX.
    """
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except (PermissionError, OSError):
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def sync_eval_md(path: Path, *, expect_section: str) -> None:
    """Wait for SDK file writes to land; assert the expected section is present."""
    await to_thread.run_sync(_fsync_dir, path.parent)
    await anyio.sleep(0.1)
    is_file = await to_thread.run_sync(path.is_file)
    if not is_file:
        raise EvaluatorEmittedUnparseableEvalMdError(f"eval.md missing after query: {path}")
    text = await to_thread.run_sync(path.read_text, "utf-8")
    if expect_section not in text:
        raise EvaluatorEmittedUnparseableEvalMdError(
            f"expected section {expect_section!r} missing in {path}"
        )


# ---------- Prompt builders ----------


def static_audit_prompt(
    *, job_dir: Path, sprint_seq: int, prior_tag: str | None, criteria_text: str
) -> str:
    """User prompt for the static-audit query (spec §8.2)."""
    if prior_tag:
        diff_clause = f"Read `git diff {prior_tag}..HEAD` (run from `app/`)."
    else:
        diff_clause = (
            "There is no prior sprint tag (this is sprint 1). "
            "Diff against the empty tree by reading the entire `app/` working tree."
        )

    # Long lines in prompt content are intentional — markdown rendered to LLM context.
    return f"""## Mode: static-audit

You are auditing sprint {sprint_seq}.

Read `design.md`, `plan.md`, and `contract.md` in `{job_dir}`.

{diff_clause}

For each contract criterion below, render the `### Criterion <n>:` block with **Result:** PASS or FAIL, **Evidence:** (file:line refs), and **Notes:** (reasoning):

{criteria_text}

Write your work as the `## Static audit` section of `eval.md`. Rewrite the entire file from scratch (re-include any prior content); do not partial-append.
"""  # noqa: E501


def dynamic_verification_prompt(*, job_dir: Path, sprint_seq: int, criteria_text: str) -> str:
    """User prompt for the dynamic-verification query (spec §8.3)."""
    # Long lines in prompt content are intentional — markdown rendered to LLM context.
    return f"""## Mode: dynamic-verification

You are dynamically verifying sprint {sprint_seq}.

Working dir: {job_dir}. Code is at `app/` (one level below). When invoking Bash, `cd app && ...`.

First, write a `### Routing decision` paragraph at the top of `## Dynamic verification`. State which tools you will drive (Playwright MCP / Bash test runner / httpx / DB inspection / nothing) and why.

Then, render `### Criterion <n>:` blocks for each contract criterion below:

{criteria_text}

If you start app processes (dev server, pytest, browsers), do your best to kill them when you finish. The orchestrator wraps you in a process group and SIGTERMs the group on your exit, so cooperating saves seconds but is not required for correctness.

Rewrite the entire `eval.md` from scratch (re-include `## Static audit` content); do not partial-append.
"""  # noqa: E501


# ---------- pipe_claude_msg_to_log ----------


async def pipe_claude_msg_to_log(msg: Any, log_path: Path) -> None:  # noqa: ANN401
    """Append a friendly representation of one Claude SDK message to log_path.

    Mirrors the EventLogger contract for Codex: open with line buffering,
    write atomic lines per message. Tool-call markers and result bookkeeping
    are flattened.
    """
    role = type(msg).__name__
    lines: list[str] = []
    if role == "AssistantMessage":
        for block in getattr(msg, "content", []) or []:
            block_type = type(block).__name__
            if block_type == "TextBlock":
                lines.append(getattr(block, "text", "") or "")
            elif block_type == "ToolUseBlock":
                name = getattr(block, "name", "?")
                args = getattr(block, "input", {})
                lines.append(f"[tool: {name} args={args}]")
    elif role == "ResultMessage":
        cost = getattr(msg, "total_cost_usd", "?")
        lines.append(f"--- result (cost ${cost}) ---")
    if not lines:
        return
    payload = "\n".join(lines) + "\n"

    def _write() -> None:
        with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
            fh.write(payload)

    await to_thread.run_sync(_write)
