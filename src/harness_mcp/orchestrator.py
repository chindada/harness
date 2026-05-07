"""Per-job orchestrator coroutine + cancel-scope registry.

The MCP `start_build` tool inserts a `pending` row, then schedules
`run_job(job_id)` on the server's task group. `run_job`'s first action
is a CAS-protected UPDATE to flip `pending -> running` (spec §3.2);
if that UPDATE matches zero rows, the cancel handler beat us — exit cleanly.

The cancel-scope registry (`_cancel_scopes`) is module-global, guarded
by `_scopes_lock`. `cancel_build` looks up the running job's scope and
calls `scope.cancel()` after writing the row.
"""

from __future__ import annotations

import json
import subprocess
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
    TERMINAL_JOB_STATUSES,
    db_write,
    db_write_returning_rowcount,
    new_job_id,
    open_reader,
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
        "UPDATE jobs SET status='cancelled', last_message=?, finished_at=?, updated_at=? "
        "WHERE id=?",
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

    # Seed app/.gitignore + git init. One-shot startup work; blocking calls are intentional.
    gitignore = job_dir / "app" / ".gitignore"
    gitignore.write_text(".codex/\nnode_modules/\n*.pyc\n.venv/\n.env\n")
    subprocess.run(["git", "init", "-q"], cwd=str(job_dir / "app"), check=True)  # noqa: ASYNC221
    subprocess.run(  # noqa: ASYNC221
        ["git", "config", "user.email", "harness@local"],
        cwd=str(job_dir / "app"),
        check=True,
    )
    subprocess.run(  # noqa: ASYNC221
        ["git", "config", "user.name", "harness"],
        cwd=str(job_dir / "app"),
        check=True,
    )

    # Verbatim design copy.
    (job_dir / "design.md").write_text(
        design_doc_path.read_text(encoding="utf-8"),  # noqa: ASYNC240
        encoding="utf-8",
    )

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
    """Top-level per-job coroutine. Drives planning -> sprints -> summary.

    On cancel: the outer scope's __aexit__ propagates cancellation; the
    final state-update happens inside a shielded `move_on_after(15)`.
    """
    job_dir = jobs_root() / job_id
    log_path = job_dir / "orchestrator.log"

    with anyio.CancelScope() as scope:
        await register_scope(job_id, scope)
        try:
            # CAS pending -> running.
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
                            now_ms(),
                            now_ms(),
                            job_id,
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
            with anyio.CancelScope(shield=True), anyio.move_on_after(15):
                await db_write(
                    "UPDATE jobs SET updated_at=? WHERE id=? AND status NOT IN "
                    "('completed','failed','cancelled','interrupted')",
                    (now_ms(), job_id),
                )
            raise
        except Exception as e:
            with anyio.CancelScope(shield=True), anyio.move_on_after(15):
                await db_write(
                    "UPDATE jobs SET status='failed', error_text=?, finished_at=?, "
                    "updated_at=? WHERE id=? AND status NOT IN "
                    "('completed','failed','cancelled','interrupted')",
                    (f"orchestrator_error: {e!r}", now_ms(), now_ms(), job_id),
                )
        finally:
            await unregister_scope(job_id)
