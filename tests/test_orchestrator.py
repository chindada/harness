"""Tests for harness_mcp.orchestrator — per-job state machine + cancel registry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest

from harness_mcp.orchestrator import (
    cancel_job,
    register_scope,
    unregister_scope,
)
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
