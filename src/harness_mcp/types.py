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


class TagCollisionError(Exception):
    """Raised by commit_and_summarize when an annotated tag with the target
    name already exists outside this job's `harness/<job_id>/` namespace.

    Spec §6.4 narrows tag overwrite to same-job retries; user-curated or
    other-job tags must be preserved. The orchestrator surfaces this as
    `error_text="harness_tag_collision"` verbatim.
    """


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


# ---------- Parsed shapes ----------


@dataclass(frozen=True)
class Criterion:
    """One row from a Static-audit or Dynamic-verification eval block."""

    text: str
    result: str  # "PASS" | "FAIL"
    evidence: str
    notes: str


@dataclass(frozen=True)
class EvaluationResult:
    """Parsed eval.md for one sprint.

    `passed` is the AND of all PASS results across both criterion lists.
    `unparseable=True` means the file existed but had zero parseable
    Criterion blocks; the orchestrator treats this as a sprint failure.

    `launcher_stderr_tail` is the last 4KB of the launcher subprocess's
    stderr (per spec §8.4:1023-1035), populated only when the launcher
    exited non-zero so the operator sees diagnostic context in the
    terminal `error_text`.
    """

    sprint_seq: int
    static_criteria: list[Criterion]
    dynamic_criteria: list[Criterion]
    routing_decision: str
    passed: bool
    unparseable: bool = False
    launcher_stderr_tail: str = ""


@dataclass(frozen=True)
class Handoff:
    """Parsed handoff-NNN.md.

    `files_touched` is list[(path, reason)]; the parser splits each
    bullet on the first ` — ` (em-dash). `declares_done` mirrors
    `status == "done"` for ergonomic loop conditions.
    """

    chunk_seq: int
    status: str  # "in-progress" | "done"
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
