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
from typing import Any

import anyio
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
            setting_sources=prereqs_result.setting_sources,
            mcp_servers={"context7": prereqs_result.captured_mcp["context7"]},
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
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
            setting_sources=prereqs_result.setting_sources,
            mcp_servers={"context7": prereqs_result.captured_mcp["context7"]},
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
        )

    return _factory


def _make_evaluator_options_factory(
    prereqs_result: PrereqsResult, *, job_dir: Path
) -> Callable[..., Any]:
    def _factory(**_kw: Any) -> Any:  # noqa: ANN401
        from claude_agent_sdk import (  # type: ignore[import-untyped]  # noqa: PLC0415
            ClaudeAgentOptions,
        )

        mcp = {"context7": prereqs_result.captured_mcp["context7"]}
        if "playwright" in prereqs_result.captured_mcp:
            mcp["playwright"] = prereqs_result.captured_mcp["playwright"]
        return ClaudeAgentOptions(
            system_prompt=_resolved_prompt_text("evaluator.md"),
            cwd=str(_kw.get("job_dir", job_dir)),
            setting_sources=prereqs_result.setting_sources,
            mcp_servers=mcp,
            extra_args={"strict-mcp-config": None},
            permission_mode="acceptEdits",
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


def _client_factory(**kw: Any) -> Any:  # noqa: ANN401
    """Default client factory passed to prereqs probes."""
    from claude_agent_sdk import (  # type: ignore[import-untyped]  # noqa: PLC0415
        ClaudeAgentOptions,
        ClaudeSDKClient,
    )

    options = ClaudeAgentOptions(**kw) if kw else ClaudeAgentOptions()
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
    async with anyio.create_task_group() as tg:
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
    """Start a new build job. Returns {job_id}."""
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
    return await cancel_job(job_id)
