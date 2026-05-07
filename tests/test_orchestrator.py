"""Tests for harness_mcp.orchestrator — per-job state machine + cancel registry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from harness_mcp import orchestrator as _orch_module
from harness_mcp.config import JobOptions
from harness_mcp.orchestrator import (
    cancel_job,
    register_scope,
    run_job,
    unregister_scope,
)
from harness_mcp.planning import PlanPhaseFailed
from harness_mcp.prereqs import PrereqsResult
from harness_mcp.state import close_db, db_write, init_db, open_reader
from harness_mcp.types import UnknownJobError


@pytest.fixture
async def db(tmp_harness_home: Path) -> AsyncIterator[Path]:
    close_db()  # ensure clean state from prior tests
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
        # DB row reflects the cancel — including the spec'd last_message wording.
        async with open_reader() as r:
            row = r.execute(
                "SELECT status, last_message, finished_at, updated_at FROM jobs WHERE id='J_PEND'"
            ).fetchone()
        assert row[0] == "cancelled"
        assert row[1] == "cancelled by user before orchestrator started"
        assert row[2] is not None  # finished_at set
        assert row[3] is not None  # updated_at set

    @pytest.mark.asyncio
    async def test_cancel_is_idempotent(self, db: Path) -> None:
        """Spec §3.2: calling cancel_build twice on the same job is safe;
        the second call returns was_already_terminal=True."""
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_IDEM", "pending", "init", "/x", "{}", 1, 1),
        )
        first = await cancel_job("J_IDEM")
        assert first["ok"] is True
        assert first["was_already_terminal"] is False

        # Second cancel: row is now in `cancelled` (terminal) — must short-circuit.
        second = await cancel_job("J_IDEM")
        assert second == {"ok": True, "was_already_terminal": True}

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

        async with open_reader() as r:
            row = r.execute(
                "SELECT status, last_message, finished_at, updated_at FROM jobs WHERE id='J_RUN'"
            ).fetchone()
        assert row[0] == "cancelled"
        assert row[1] == "cancelled by user"
        assert row[2] is not None  # finished_at set
        assert row[3] is not None  # updated_at set
        await unregister_scope("J_RUN")


def _empty_prereqs() -> PrereqsResult:
    """Minimal PrereqsResult — orchestrator passes it through to run_sprint,
    which our patched run_plan_phase short-circuits before it's read."""
    return PrereqsResult(
        captured_mcp={"context7": {}},
        setting_sources=["user"],
        codex_overrides=("sandbox=workspace-write", "approval_policy=never"),
    )


class TestRunJobPlanPhaseFailed:
    """Spec §5.1:320 / §5.2:327 / §5.2:343 — when run_plan_phase raises
    PlanPhaseFailed, the orchestrator must persist the carried phase and
    error_text verbatim, not the generic `orchestrator_error: …` envelope.
    """

    @pytest.mark.asyncio
    async def test_phase_review_propagates(
        self, db: Path, monkeypatch: pytest.MonkeyPatch, tmp_harness_home: Path
    ) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_PHASE", "pending", "init", "/x", "{}", 1, 1),
        )
        # Job dir must exist; orchestrator opens an orchestrator.log there.
        (tmp_harness_home / "jobs" / "J_PHASE").mkdir()

        async def fake_plan_phase(**_kw: Any) -> None:
            raise PlanPhaseFailed(
                "max_plan_review_rounds_exceeded: missing schema; no tests",
                phase="plan-review",
            )

        monkeypatch.setattr(_orch_module, "run_plan_phase", fake_plan_phase)

        await run_job(
            job_id="J_PHASE",
            options=JobOptions(),
            prereqs_result=_empty_prereqs(),
            planner_options_factory=lambda **_kw: object(),
            reviewer_options_factory=lambda **_kw: object(),
            evaluator_options_factory=lambda **_kw: object(),
            summarizer_options_factory=lambda **_kw: object(),
            codex_bin="codex",
        )

        async with open_reader() as r:
            row = r.execute(
                "SELECT status, current_phase, error_text FROM jobs WHERE id='J_PHASE'"
            ).fetchone()
        assert row[0] == "failed"
        assert row[1] == "plan-review"
        # Verbatim error_text — not wrapped in `orchestrator_error: …`.
        assert row[2] == "max_plan_review_rounds_exceeded: missing schema; no tests"

    @pytest.mark.asyncio
    async def test_phase_planning_propagates(
        self, db: Path, monkeypatch: pytest.MonkeyPatch, tmp_harness_home: Path
    ) -> None:
        """Spec §5.1:320 — initial plan-v1 unstructured after retry →
        phase=planning, error_text=planner_emitted_unstructured_plan_after_retry."""
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_PLAN", "pending", "init", "/x", "{}", 1, 1),
        )
        (tmp_harness_home / "jobs" / "J_PLAN").mkdir()

        async def fake_plan_phase(**_kw: Any) -> None:
            raise PlanPhaseFailed(
                "planner_emitted_unstructured_plan_after_retry",
                phase="planning",
            )

        monkeypatch.setattr(_orch_module, "run_plan_phase", fake_plan_phase)

        await run_job(
            job_id="J_PLAN",
            options=JobOptions(),
            prereqs_result=_empty_prereqs(),
            planner_options_factory=lambda **_kw: object(),
            reviewer_options_factory=lambda **_kw: object(),
            evaluator_options_factory=lambda **_kw: object(),
            summarizer_options_factory=lambda **_kw: object(),
            codex_bin="codex",
        )

        async with open_reader() as r:
            row = r.execute(
                "SELECT status, current_phase, error_text FROM jobs WHERE id='J_PLAN'"
            ).fetchone()
        assert row[0] == "failed"
        assert row[1] == "planning"
        assert row[2] == "planner_emitted_unstructured_plan_after_retry"
