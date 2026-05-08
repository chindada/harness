"""Integration tests for run_build — start + block + return get_build_result payload."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from harness_mcp.phase_broker import PhaseBroker


class FakeContext:
    """Captures Context.report_progress calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float | None, str | None]] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.calls.append((progress, total, message))


@pytest.mark.asyncio
async def test_run_build_returns_get_build_result_payload_on_completion(
    tmp_path, monkeypatch
):
    """Stub the orchestrator: emit planning then complete via the broker, assert
    run_build returns a get_build_result-shaped payload with job_id."""
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))

    from harness_mcp import server, state

    state.close_db()
    state.init_db()
    try:
        broker = PhaseBroker()

        async def stub_run_job(*, job_id: str, phase_broker: PhaseBroker, **kw: Any) -> None:
            await state.db_write(
                "UPDATE jobs SET status='running', current_phase='planning', updated_at=? "
                "WHERE id=?",
                (0, job_id),
            )
            await phase_broker.publish(
                job_id,
                {"current_phase": "planning", "status": "running", "sprints_completed": 0},
            )
            await state.db_write(
                "UPDATE jobs SET status='completed', current_phase='done', "
                "last_message=?, finished_at=?, updated_at=? WHERE id=?",
                ("done", 100, 100, job_id),
            )
            await phase_broker.publish(
                job_id,
                {"current_phase": "done", "status": "completed", "sprints_completed": 0},
            )
            phase_broker.close(job_id)

        monkeypatch.setattr(server, "run_job", stub_run_job)

        async with anyio.create_task_group() as tg:
            from harness_mcp.prereqs import PrereqsResult

            server._state = server.ServerState(
                prereqs_result=PrereqsResult.__new__(PrereqsResult),
                codex_bin="codex",
                task_group=tg,
                phase_broker=broker,
            )
            design = tmp_path / "design.md"
            design.write_text("design")

            ctx = FakeContext()
            result = await server.run_build(str(design), None, ctx=ctx)

            assert result["final_status"] == "completed"
            assert "job_id" in result
            assert any("planning" in (m or "") for _, _, m in ctx.calls)
    finally:
        state.close_db()


@pytest.mark.asyncio
async def test_run_build_returns_failed_payload_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))
    from harness_mcp import server, state

    state.close_db()
    state.init_db()
    try:
        broker = PhaseBroker()

        async def stub_run_job(*, job_id: str, phase_broker: PhaseBroker, **kw: Any) -> None:
            await state.db_write(
                "UPDATE jobs SET status='failed', current_phase='sprint-1/retry', "
                "error_text=?, finished_at=?, updated_at=? WHERE id=?",
                ("oops", 100, 100, job_id),
            )
            await phase_broker.publish(
                job_id,
                {
                    "current_phase": "sprint-1/retry",
                    "status": "failed",
                    "sprints_completed": 0,
                },
            )
            phase_broker.close(job_id)

        monkeypatch.setattr(server, "run_job", stub_run_job)

        async with anyio.create_task_group() as tg:
            from harness_mcp.prereqs import PrereqsResult

            server._state = server.ServerState(
                prereqs_result=PrereqsResult.__new__(PrereqsResult),
                codex_bin="codex",
                task_group=tg,
                phase_broker=broker,
            )
            design = tmp_path / "design.md"
            design.write_text("design")

            result = await server.run_build(str(design), None, ctx=FakeContext())
            assert result["final_status"] == "failed"
            assert "job_id" in result
    finally:
        state.close_db()


@pytest.mark.asyncio
async def test_run_build_cancellation_invokes_cancel_job(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))
    from harness_mcp import server, state

    state.close_db()
    state.init_db()
    try:
        broker = PhaseBroker()
        cancel_calls: list[str] = []
        job_stopped = anyio.Event()

        async def stub_run_job(
            *, job_id: str, phase_broker: PhaseBroker, **kw: Any
        ) -> None:
            # Block until cancelled externally via the shared event.
            await job_stopped.wait()

        def stub_cancel_job_sync(job_id: str) -> None:
            """Synchronously track calls and signal stub_run_job to stop."""
            cancel_calls.append(job_id)
            job_stopped.set()

        async def stub_cancel_job(job_id: str) -> dict[str, Any]:
            # Use a shielded scope so the DB write + event set complete even if
            # the calling task is being cancelled.
            with anyio.CancelScope(shield=True):
                await state.db_write(
                    "UPDATE jobs SET status='cancelled', current_phase='cancelled', "
                    "last_message=?, finished_at=?, updated_at=? WHERE id=?",
                    ("cancelled", 100, 100, job_id),
                )
            stub_cancel_job_sync(job_id)
            return {"ok": True, "was_already_terminal": False}

        monkeypatch.setattr(server, "run_job", stub_run_job)
        monkeypatch.setattr(server, "cancel_job", stub_cancel_job)

        async with anyio.create_task_group() as tg:
            from harness_mcp.prereqs import PrereqsResult

            server._state = server.ServerState(
                prereqs_result=PrereqsResult.__new__(PrereqsResult),
                codex_bin="codex",
                task_group=tg,
                phase_broker=broker,
            )
            design = tmp_path / "design.md"
            design.write_text("design")

            cancelled = anyio.Event()

            async def run_and_capture():
                try:
                    await server.run_build(str(design), None, ctx=FakeContext())
                except BaseException:
                    cancelled.set()
                    raise

            with anyio.move_on_after(0.5) as scope:
                async with anyio.create_task_group() as inner:
                    inner.start_soon(run_and_capture)
                    await anyio.sleep(0.1)
                    inner.cancel_scope.cancel()
            # scope.cancelled_caught is False because inner.cancel_scope handled
            # the cancellation before the outer move_on_after deadline fired —
            # that is the correct, non-hanging behaviour.
            assert not scope.cancelled_caught
            assert len(cancel_calls) == 1
    finally:
        state.close_db()
