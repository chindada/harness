"""FastMCP server: lifespan + four tools + error mapper.

This module defines the MCP-tool entry points and the lifespan context
manager that runs `prereqs.run_prereqs` at startup. Spawned-agent
options factories live here too — they encapsulate the captured MCP
state so the lower-level modules don't need it.
"""

from __future__ import annotations

import functools
import os
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast

from anyio import create_task_group
from anyio.abc import TaskGroup
from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP

from harness_mcp.config import JobOptions, jobs_root
from harness_mcp.orchestrator import (
    cancel_job,
    run_job,
    start_orchestrator_inserts_row,
)
from harness_mcp.prereqs import (
    DoctorReport,
    PrereqsResult,
    run_prereqs,
)
from harness_mcp.prompts_loader import _resolved_prompt_text
from harness_mcp.state import (
    TERMINAL_JOB_STATUSES,
    open_reader,
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


def _map_harness_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: catch HarnessToolError raised inside an MCP tool body and
    return a CallToolResult with structured `code` set per spec §3.1.

    Both FastMCP's `convert_result` (utilities/func_metadata.py) and the
    lowlevel server's call_tool handler pass `CallToolResult` through unchanged,
    so returning one short-circuits FastMCP's default exception → string-message
    flow and preserves `structured_content.code`.
    """

    @functools.wraps(fn)
    async def wrapper(
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        try:
            return await fn(*args, **kwargs)
        except HarnessToolError as e:
            return _to_call_tool_error(e)

    return wrapper


# ---------- claude CLI resolver ----------


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


# ---------- options factories ----------


def _make_planner_options_factory(
    prereqs_result: PrereqsResult, *, job_dir: Path
) -> Callable[..., Any]:
    def _factory(**_kw: Any) -> Any:  # noqa: ANN401
        from claude_agent_sdk import (  # type: ignore[import-untyped]  # noqa: PLC0415
            ClaudeAgentOptions,
        )

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

    return _factory


def _make_reviewer_options_factory(
    prereqs_result: PrereqsResult, *, job_dir: Path
) -> Callable[..., Any]:
    def _factory(**_kw: Any) -> Any:  # noqa: ANN401
        from claude_agent_sdk import (  # type: ignore[import-untyped]  # noqa: PLC0415
            ClaudeAgentOptions,
        )

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

    return _factory


def _make_evaluator_options_factory(
    prereqs_result: PrereqsResult, *, job_dir: Path
) -> Callable[..., Any]:
    """Build the Evaluator options used for **contract negotiation** only.

    Spec §6.1:399 — at this stage the Evaluator runs `query()` with
    `mcp_servers = {"context7": ...}` (no playwright). Playwright is
    introduced later, inside the launcher subprocess (evaluator_runner.py),
    where the actual evaluation phase needs it for dynamic verification.
    """

    def _factory(**_kw: Any) -> Any:  # noqa: ANN401
        from claude_agent_sdk import (  # type: ignore[import-untyped]  # noqa: PLC0415
            ClaudeAgentOptions,
        )

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

    return _factory


def _make_summarizer_options_factory(prereqs_result: PrereqsResult) -> Callable[..., Any]:
    def _factory(*, job_dir: Path) -> Any:  # noqa: ANN401
        from claude_agent_sdk import (  # type: ignore[import-untyped]  # noqa: PLC0415
            ClaudeAgentOptions,
        )

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

    return _factory


# ---------- lifespan + tools ----------


@dataclass
class ServerState:
    """Shared mutable across all tool calls — initialized in lifespan."""

    prereqs_result: PrereqsResult
    codex_bin: str
    task_group: TaskGroup


_state: ServerState | None = None


def _client_factory(**kw: Any) -> Any:  # noqa: ANN401
    """Default client factory for prereq probes — isolated by default.

    Defaults `extra_args={"strict-mcp-config": None}` and `mcp_servers={}` so
    spawned claudes don't load the user's MCP servers. Critical because
    harness-mcp itself is typically registered at user scope; without isolation,
    each probe spawn re-launches harness-mcp (recursive lifespan) plus every
    other user MCP (e.g., the playwright plugin's `npx @playwright/mcp@latest`),
    cascading into a fork bomb at startup. Callers that legitimately need an
    MCP (e.g., assert_strict_mcp_config_works needs context7) override by
    passing `mcp_servers` explicitly.
    """
    from claude_agent_sdk import (  # type: ignore[import-untyped]  # noqa: PLC0415
        ClaudeAgentOptions,
        ClaudeSDKClient,
    )

    kw.setdefault("cli_path", _resolve_claude_cli())
    kw.setdefault("env", _claude_env_overrides())
    kw.setdefault("mcp_servers", {})
    extra_args = dict(kw.pop("extra_args", None) or {})
    extra_args.setdefault("strict-mcp-config", None)
    options = ClaudeAgentOptions(**kw, extra_args=extra_args)
    return ClaudeSDKClient(options=options)


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
    """Run startup prereqs; refuse the server on any failure."""
    global _state  # noqa: PLW0603
    codex_bin = os.environ.get("HARNESS_CODEX_BIN") or shutil.which("codex") or "codex"

    report = DoctorReport()
    prereqs_result = await run_prereqs(
        client_factory=_client_factory,
        project_root=None,
        report=report,
    )
    async with create_task_group() as tg:
        _state = ServerState(prereqs_result=prereqs_result, codex_bin=codex_bin, task_group=tg)
        try:
            yield
        finally:
            _state = None


server = FastMCP("harness-mcp", lifespan=lifespan)


@server.tool()
@_map_harness_errors
async def start_build(
    design_doc_path: str, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Start a new build job from a feature design document.

    Design:
        Per spec §3, this is the entry point for the four-tool surface.
        The call inserts a `pending` jobs row synchronously (so the
        returned job_id is durable), copies design.md verbatim into the
        job dir, then schedules the orchestrator coroutine on the
        server's task group. The CAS-protected `pending → running`
        transition happens on the orchestrator's first DB write
        (orchestrator.run_job:160-168) — this closes the race where
        cancel_build runs before the orchestrator coroutine starts.

    Implementation:
        Validates `design_doc_path` exists and is non-empty (else
        DesignDocNotFoundError → DESIGN_DOC_NOT_FOUND), parses `options`
        through JobOptions.from_dict (closed-set keys; rejects unknown
        keys → INVALID_OPTIONS), then delegates to
        start_orchestrator_inserts_row + task_group.start_soon. Returns
        immediately; the orchestrator runs concurrently.

    Example:
        >>> await start_build("/abs/path/to/design.md", {"max_sprints": 5})
        {"job_id": "01HC..."}
    """
    p = Path(design_doc_path)
    if not p.is_file() or p.stat().st_size == 0:  # noqa: ASYNC240
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
            planner_options_factory=_make_planner_options_factory(
                _state.prereqs_result, job_dir=job_dir
            ),
            reviewer_options_factory=_make_reviewer_options_factory(
                _state.prereqs_result, job_dir=job_dir
            ),
            evaluator_options_factory=_make_evaluator_options_factory(
                _state.prereqs_result, job_dir=job_dir
            ),
            summarizer_options_factory=_make_summarizer_options_factory(_state.prereqs_result),
            codex_bin=_state.codex_bin,
        )
    )
    return {"job_id": job_id}


@server.tool()
@_map_harness_errors
async def poll_build(job_id: str) -> dict[str, Any]:
    """Return the live status of a build job.

    Design:
        Read-only mirror of the jobs row plus a recomputed `sprints_completed`
        count (only `status='passed'` rows). Per spec §3, every key the schema
        documents is included so callers can render progress without joining
        rows themselves.

    Implementation:
        Single SQLite reader connection (WAL); two queries: jobs row +
        COUNT of passed sprints. Raises `UnknownJobError` if the row is
        absent — the error decorator maps it to `UNKNOWN_JOB`.

    Example:
        >>> await poll_build("01HC...")
        {"status": "running", "current_phase": "sprint-1/eval-static",
         "last_message": "", "plan_review_rounds": 1,
         "sprints_completed": 0, "started_at": ..., "updated_at": ...}
    """
    async with open_reader() as r:
        row = r.execute(
            "SELECT status, current_phase, last_message, plan_review_rounds, "
            "started_at, updated_at FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise UnknownJobError(job_id)
        sprints_completed = r.execute(
            "SELECT COUNT(*) FROM sprints WHERE job_id=? AND status='passed'",
            (job_id,),
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
@_map_harness_errors
async def get_build_result(job_id: str) -> dict[str, Any]:
    """Return the terminal artifacts of a finished build.

    Design:
        Per spec §3, only callable on terminal jobs (`completed`, `failed`,
        `cancelled`, `interrupted`). Returns the produced app path, the
        Summarizer's prose summary, per-sprint pass/fail/retry counts, and
        wall-clock duration. The summary is read from `summary.md` if it
        exists (Summarizer wrote it directly) and falls back to the
        last_message column otherwise.

    Implementation:
        Two queries: jobs row (status + timestamps + last_message) + sprints
        rows (seq, title, status, retry_count). Raises `UnknownJobError` for
        unknown ids and `JobNotFinishedError` if the job is still in a
        non-terminal state. Both surface as structured `code` per §3.1.

    Example:
        >>> await get_build_result("01HC...")
        {"app_path": ".../app", "summary": "Built X with...",
         "final_status": "completed",
         "sprints": [{"seq": 1, "title": "...", "status": "passed", "retry_count": 0}],
         "plan_review_rounds": 0, "duration_seconds": 1234.5}
    """
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
    summary_text = (
        summary_path.read_text(encoding="utf-8") if summary_path.is_file() else (row[2] or "")
    )

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
@_map_harness_errors
async def cancel_build(job_id: str) -> dict[str, Any]:
    """Cancel a running or pending build. Idempotent.

    Design:
        Per spec §3.2, terminal jobs return `was_already_terminal=True`
        without further action. `pending` jobs are CAS-flipped to
        `cancelled` (covers the rare race where the orchestrator coroutine
        hasn't started yet). `running` jobs have their cancel scope
        cancelled after the row write, propagating `CancelledError`
        through every nested SDK client.

    Implementation:
        Delegates to orchestrator.cancel_job, which encapsulates the
        `pending → cancelled` CAS, the `running → cancelled` row write,
        and the cancel-scope lookup. Raises `UnknownJobError` if the row
        is absent — the error decorator maps it to `UNKNOWN_JOB`.

    Example:
        >>> await cancel_build("01HC...")
        {"ok": True, "was_already_terminal": False}
    """
    return await cancel_job(job_id)
