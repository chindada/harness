"""Tests for harness_mcp.orchestrator — per-job state machine + cancel registry."""

from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from harness_mcp import orchestrator as _orch_module
from harness_mcp import sprints as _sprints_module
from harness_mcp import state as _state_module
from harness_mcp.config import JobOptions
from harness_mcp.orchestrator import (
    cancel_job,
    register_scope,
    run_job,
    start_orchestrator_inserts_row,
    unregister_scope,
)
from harness_mcp.phase_broker import PhaseBroker
from harness_mcp.planning import PlanPhaseFailed
from harness_mcp.prereqs import PrereqsResult
from harness_mcp.server import poll_build
from harness_mcp.sprints import SprintResult
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


class TestStartOrchestratorInsertsRow:
    """Spec §4.1 / §5.1:287 — start_orchestrator_inserts_row must:
    (a) create the job dir layout (`plan-history/`, `app/`),
    (b) copy the input design doc to `jobs/<id>/design.md` byte-for-byte,
    (c) `git init` the `app/` dir,
    (d) insert a `pending` jobs row.
    """

    @pytest.mark.asyncio
    async def test_design_md_is_copied_byte_for_byte(
        self, db: Path, tmp_path: Path, tmp_harness_home: Path
    ) -> None:
        # Multi-line UTF-8 with characters that exercise encoding edges
        # (em-dash, smart quotes, CRLF, trailing newline).
        original_bytes = (
            "# Design — alpha\r\n"
            "Some prose with “smart quotes” and é (é).\n"
            "Trailing newline included.\n"
        ).encode()
        src = tmp_path / "design_input.md"
        src.write_bytes(original_bytes)

        job_id = await start_orchestrator_inserts_row(
            design_doc_path=src,
            options=JobOptions(),
        )

        # Verify the copy is byte-equal to the source.
        copied = (tmp_harness_home / "jobs" / job_id / "design.md").read_bytes()
        assert copied == original_bytes, (
            f"design.md not byte-equal: got {copied!r}, want {original_bytes!r}"
        )

        # Required directory layout (spec §4.1).
        assert (tmp_harness_home / "jobs" / job_id / "plan-history").is_dir()
        assert (tmp_harness_home / "jobs" / job_id / "app").is_dir()
        assert (tmp_harness_home / "jobs" / job_id / "app" / ".git").is_dir()
        # The pending row.
        async with open_reader() as r:
            row = r.execute(
                "SELECT status, current_phase, design_path FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "pending"
        assert row[1] == "init"
        assert row[2] == str(src)


class TestRunJobCASRace:
    """Spec §3.2 — when `cancel_build` writes `status='cancelled'` to a row
    that is still `pending`, the orchestrator's CAS UPDATE
    (`SET status='running' ... WHERE status='pending'`) must observe rc=0
    and exit cleanly. The job stays `cancelled`; no sprints are written.
    """

    @pytest.mark.asyncio
    async def test_cancel_beats_orchestrator_cas_exit_clean(
        self, db: Path, monkeypatch: pytest.MonkeyPatch, tmp_harness_home: Path
    ) -> None:
        # Insert pending row.
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_RACE", "pending", "init", "/x", "{}", 1, 1),
        )
        (tmp_harness_home / "jobs" / "J_RACE").mkdir()

        # cancel_build wins: row flips to 'cancelled' before run_job starts.
        await cancel_job("J_RACE")

        # run_plan_phase should never be reached on the cancel-wins path —
        # any call here would be a regression.
        plan_calls = [0]

        async def fake_plan_phase(**_kw: Any) -> tuple[list[tuple[int, str]], int]:
            plan_calls[0] += 1
            return ([], 0)

        monkeypatch.setattr(_orch_module, "run_plan_phase", fake_plan_phase)

        # run_job must exit cleanly without raising.
        await run_job(
            job_id="J_RACE",
            options=JobOptions(),
            prereqs_result=_empty_prereqs(),
            planner_options_factory=lambda **_kw: object(),
            reviewer_options_factory=lambda **_kw: object(),
            evaluator_options_factory=lambda **_kw: object(),
            summarizer_options_factory=lambda **_kw: object(),
            codex_bin="codex",
            phase_broker=PhaseBroker(),
        )

        # The CAS UPDATE matched zero rows; the orchestrator returned
        # before reaching the plan phase.
        assert plan_calls[0] == 0
        async with open_reader() as r:
            row = r.execute(
                "SELECT status, last_message FROM jobs WHERE id='J_RACE'"
            ).fetchone()
            sprint_count = r.execute(
                "SELECT COUNT(*) FROM sprints WHERE job_id='J_RACE'"
            ).fetchone()[0]
        # Status remains 'cancelled' (cancel_build's write was not overwritten).
        assert row[0] == "cancelled"
        assert row[1] == "cancelled by user before orchestrator started"
        # No sprints were inserted — orchestrator exited before that step.
        assert sprint_count == 0


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
            phase_broker=PhaseBroker(),
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
            phase_broker=PhaseBroker(),
        )

        async with open_reader() as r:
            row = r.execute(
                "SELECT status, current_phase, error_text FROM jobs WHERE id='J_PLAN'"
            ).fetchone()
        assert row[0] == "failed"
        assert row[1] == "planning"
        assert row[2] == "planner_emitted_unstructured_plan_after_retry"


class TestLastMessagePopulated:
    """Spec §6.6 — at job completion, the Summarizer's content must be
    written to `jobs.last_message` (verbatim, no truncation) so callers
    polling via `poll_build` see the summary.
    """

    @pytest.mark.asyncio
    async def test_summary_is_stored_in_last_message(
        self, db: Path, monkeypatch: pytest.MonkeyPatch, tmp_harness_home: Path
    ) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_SUM", "pending", "init", "/x", "{}", 1, 1),
        )
        (tmp_harness_home / "jobs" / "J_SUM").mkdir()

        async def fake_plan_phase(**_kw: Any) -> tuple[list[tuple[int, str]], int]:
            return ([(1, "Title")], 0)

        async def fake_run_sprint(**_kw: Any) -> SprintResult:
            return SprintResult(sprint_seq=1, passed=True, attempts=1)

        SUMMARY_TEXT = (
            "Built a TODO app with REST API. All 1 of 1 sprints passed. Nothing incomplete."
        )

        async def fake_run_summarizer(**_kw: Any) -> str:
            return SUMMARY_TEXT

        monkeypatch.setattr(_orch_module, "run_plan_phase", fake_plan_phase)
        monkeypatch.setattr(_sprints_module, "run_sprint", fake_run_sprint)
        monkeypatch.setattr(_orch_module, "run_sprint", fake_run_sprint)
        monkeypatch.setattr(_orch_module, "run_summarizer", fake_run_summarizer)

        await run_job(
            job_id="J_SUM",
            options=JobOptions(),
            prereqs_result=_empty_prereqs(),
            planner_options_factory=lambda **_kw: object(),
            reviewer_options_factory=lambda **_kw: object(),
            evaluator_options_factory=lambda **_kw: object(),
            summarizer_options_factory=lambda **_kw: object(),
            codex_bin="codex",
            phase_broker=PhaseBroker(),
        )

        # poll_build must surface the summary in last_message.
        polled = await poll_build("J_SUM")
        assert polled["status"] == "completed"
        assert polled["current_phase"] == "done"
        assert polled["last_message"] == SUMMARY_TEXT


class TestSprintRowWriteShielded:
    """Spec §6.5:558-565 — the per-sprint state write must be wrapped in
    `anyio.CancelScope(shield=True)` + `anyio.move_on_after(15)` so a
    propagating cancel/timeout can't leave the row in 'running'.
    """

    def test_orchestrator_source_has_shielded_grace_around_sprint_update(self) -> None:
        """Lock the structural pattern: any future refactor that drops the
        shield or grace should fail this test loudly."""
        src = inspect.getsource(_orch_module.run_job)
        # The shield + grace must immediately precede the sprints UPDATE.
        pattern = re.compile(
            r"with\s+anyio\.CancelScope\(shield=True\)\s*,\s*"
            r"anyio\.move_on_after\(15\)\s*:\s*"
            r"\n\s*await\s+db_write\(\s*"
            r'"UPDATE sprints SET status',
            re.DOTALL,
        )
        assert pattern.search(src), (
            "expected shielded `move_on_after(15)` around sprint-row UPDATE in run_job"
        )

    @pytest.mark.asyncio
    async def test_sprint_row_update_completes_under_outer_cancel_request(
        self, db: Path, monkeypatch: pytest.MonkeyPatch, tmp_harness_home: Path
    ) -> None:
        """Behavioral cousin: cancel the orchestrator's outer scope at the
        moment the sprint UPDATE is being written; the shield must let the
        UPDATE complete so the row reaches a terminal state instead of
        staying 'running'."""
        # Pending job + pending sprint row.
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_SHIELD", "pending", "init", "/x", "{}", 1, 1),
        )
        (tmp_harness_home / "jobs" / "J_SHIELD").mkdir()

        async def fake_plan_phase(**_kw: Any) -> tuple[list[tuple[int, str]], int]:
            return ([(1, "Title")], 0)

        async def fake_run_sprint(**_kw: Any) -> SprintResult:
            return SprintResult(sprint_seq=1, passed=True, attempts=1)

        async def fake_run_summarizer(**_kw: Any) -> str:
            return "summary"

        monkeypatch.setattr(_orch_module, "run_plan_phase", fake_plan_phase)
        monkeypatch.setattr(_sprints_module, "run_sprint", fake_run_sprint)
        monkeypatch.setattr(_orch_module, "run_sprint", fake_run_sprint)
        monkeypatch.setattr(_orch_module, "run_summarizer", fake_run_summarizer)

        # Wrap db_write so that the moment we see the sprint UPDATE, we
        # cancel the orchestrator's outer scope. The shield should still
        # allow the write to complete.
        real_db_write = _state_module.db_write
        queries_seen: list[str] = []
        sprint_write_completed = [False]

        write_errors: list[BaseException] = []

        # Match only the SHIELDED final-state write (it carries `retry_count`),
        # not the earlier un-shielded `status='running'` write.
        SHIELDED_SQL_HALLMARK = "UPDATE sprints SET status=?, retry_count=?"

        async def cancelling_db_write(sql: str, params: tuple[Any, ...]) -> None:
            queries_seen.append(sql)
            if SHIELDED_SQL_HALLMARK in sql:
                # Pretend an external actor cancels the orchestrator at the
                # exact moment of the protected write. Because the shield
                # ignores outer cancellation, db_write must still complete.
                async with _orch_module._scopes_lock:
                    s = _orch_module._cancel_scopes.get("J_SHIELD")
                if s is not None:
                    s.cancel()
                try:
                    await real_db_write(sql, params)
                except BaseException as e:
                    write_errors.append(e)
                    raise
                sprint_write_completed[0] = True
                return
            await real_db_write(sql, params)

        monkeypatch.setattr(_state_module, "db_write", cancelling_db_write)
        # The orchestrator imports db_write by name; rebind there too.
        monkeypatch.setattr(_orch_module, "db_write", cancelling_db_write)

        await run_job(
            job_id="J_SHIELD",
            options=JobOptions(),
            prereqs_result=_empty_prereqs(),
            planner_options_factory=lambda **_kw: object(),
            reviewer_options_factory=lambda **_kw: object(),
            evaluator_options_factory=lambda **_kw: object(),
            summarizer_options_factory=lambda **_kw: object(),
            codex_bin="codex",
            phase_broker=PhaseBroker(),
        )

        # First: verify the SHIELDED sprint UPDATE was actually attempted.
        sprint_updates = [s for s in queries_seen if SHIELDED_SQL_HALLMARK in s]
        assert len(sprint_updates) >= 1, (
            f"shielded sprint UPDATE never issued; queries seen: {queries_seen}"
        )
        # Second: the shield held the write open under in-flight cancel.
        assert sprint_write_completed[0] is True, (
            f"shielded sprint UPDATE must complete despite cancellation in flight; "
            f"write_errors={[(type(e).__name__, str(e)) for e in write_errors]}"
        )
        async with open_reader() as r:
            row = r.execute(
                "SELECT status, retry_count, finished_at FROM sprints "
                "WHERE job_id='J_SHIELD' AND seq=1"
            ).fetchone()
        assert row is not None
        assert row[0] == "passed"
        assert row[1] == 0
        assert row[2] is not None  # finished_at written


class TestRunJobPhaseBroker:
    """run_job publishes a phase event for every current_phase / status transition."""

    @pytest.mark.asyncio
    async def test_publishes_planning_and_terminal_failed(
        self, db: Path, monkeypatch: pytest.MonkeyPatch, tmp_harness_home: Path
    ) -> None:
        from harness_mcp.phase_broker import PhaseBroker

        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_BROKER", "pending", "init", "/x", "{}", 1, 1),
        )
        (tmp_harness_home / "jobs" / "J_BROKER").mkdir()

        async def fake_plan_phase(**_kw: Any) -> None:
            raise PlanPhaseFailed("synthetic failure", phase="planning")

        monkeypatch.setattr(_orch_module, "run_plan_phase", fake_plan_phase)

        broker = PhaseBroker()
        sub = broker.subscribe("J_BROKER")

        await run_job(
            job_id="J_BROKER",
            options=JobOptions(),
            prereqs_result=_empty_prereqs(),
            planner_options_factory=lambda **_kw: object(),
            reviewer_options_factory=lambda **_kw: object(),
            evaluator_options_factory=lambda **_kw: object(),
            summarizer_options_factory=lambda **_kw: object(),
            codex_bin="codex",
            phase_broker=broker,
        )

        received: list[dict] = []
        async with sub:
            async for event in sub:
                received.append(event)

        phases = [e["current_phase"] for e in received]
        statuses = [e["status"] for e in received]
        # CAS pending→running emits 'planning'; PlanPhaseFailed emits final 'failed'.
        assert phases[0] == "planning"
        assert statuses[0] == "running"
        assert statuses[-1] == "failed"
        assert phases[-1] == "planning"  # PlanPhaseFailed carries phase='planning'
