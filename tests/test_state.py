"""Tests for harness_mcp.state — schema, helpers, restart sweep."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import anyio
import pytest

from harness_mcp.state import (
    JOB_STATUSES,
    PHASES,
    SPRINT_STATUSES,
    TERMINAL_JOB_STATUSES,
    close_db,
    db_write,
    db_write_returning_rowcount,
    init_db,
    new_job_id,
    open_reader,
    sweep_running_to_interrupted,
)


@pytest.fixture
def initialized_home(tmp_harness_home: Path) -> Path:
    """tmp_harness_home + state.db opened."""
    init_db()
    yield tmp_harness_home
    close_db()


class TestSchema:
    def test_init_db_creates_jobs_and_sprints(self, initialized_home: Path) -> None:
        conn = sqlite3.connect(str(initialized_home / "state.db"))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "jobs" in tables
            assert "sprints" in tables
        finally:
            conn.close()

    def test_wal_mode_active(self, initialized_home: Path) -> None:
        conn = sqlite3.connect(str(initialized_home / "state.db"))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()

    def test_init_db_idempotent(self, initialized_home: Path) -> None:
        # Calling init_db twice should not raise (CREATE TABLE IF NOT EXISTS).
        init_db()
        init_db()


class TestStatusConstants:
    def test_job_statuses_contain_all_states(self) -> None:
        assert JOB_STATUSES == {  # noqa: SIM300 — constant on the left reads naturally here
            "pending",
            "running",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }

    def test_terminal_subset(self) -> None:
        assert TERMINAL_JOB_STATUSES == {"completed", "failed", "cancelled", "interrupted"}  # noqa: SIM300
        assert TERMINAL_JOB_STATUSES <= JOB_STATUSES

    def test_sprint_statuses(self) -> None:
        assert SPRINT_STATUSES == {"pending", "running", "passed", "failed", "cancelled"}  # noqa: SIM300

    def test_phases_contains_minimum(self) -> None:
        # Spec §4.4 — informational enum. Spot-check a few.
        for required in (
            "init",
            "planning",
            "plan-review",
            "plan-revision",
            "summarizing",
            "done",
        ):
            assert required in PHASES


class TestUlid:
    def test_new_job_id_is_26_chars(self) -> None:
        jid = new_job_id()
        assert isinstance(jid, str)
        assert len(jid) == 26

    def test_new_job_id_unique(self) -> None:
        ids = {new_job_id() for _ in range(100)}
        assert len(ids) == 100

    def test_new_job_id_sortable(self) -> None:
        # ULIDs are time-sortable. Two consecutive ones should be ordered.
        a = new_job_id()
        b = new_job_id()
        assert a <= b


class TestDbWrite:
    @pytest.mark.asyncio
    async def test_db_write_inserts(self, initialized_home: Path) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J1", "pending", "init", "/tmp/x", "{}", 1, 1),
        )
        async with open_reader() as r:
            row = r.execute("SELECT id, status FROM jobs WHERE id=?", ("J1",)).fetchone()
        assert row == ("J1", "pending")

    @pytest.mark.asyncio
    async def test_db_write_returning_rowcount_zero_on_no_match(
        self, initialized_home: Path
    ) -> None:
        rc = await db_write_returning_rowcount(
            "UPDATE jobs SET status='running' WHERE id=? AND status='pending'",
            ("does-not-exist",),
        )
        assert rc == 0

    @pytest.mark.asyncio
    async def test_db_write_returning_rowcount_one_on_cas_win(self, initialized_home: Path) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J2", "pending", "init", "/tmp/x", "{}", 1, 1),
        )
        rc = await db_write_returning_rowcount(
            "UPDATE jobs SET status='running' WHERE id=? AND status='pending'",
            ("J2",),
        )
        assert rc == 1

    @pytest.mark.asyncio
    async def test_db_write_serialized_under_concurrency(self, initialized_home: Path) -> None:
        # Race many concurrent inserts; all should land.
        async def insert(idx: int) -> None:
            await db_write(
                "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
                "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"R{idx:03d}", "pending", "init", "/tmp/x", "{}", idx, idx),
            )

        async with anyio.create_task_group() as tg:
            for i in range(50):
                tg.start_soon(insert, i)

        async with open_reader() as r:
            count = r.execute("SELECT COUNT(*) FROM jobs WHERE id LIKE 'R%'").fetchone()[0]
        assert count == 50


class TestRestartSweep:
    @pytest.mark.asyncio
    async def test_sweep_flips_running_to_interrupted(self, initialized_home: Path) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("S1", "running", "planning", "/tmp/x", "{}", 1, 1),
        )
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("S2", "pending", "init", "/tmp/x", "{}", 1, 1),
        )
        await sweep_running_to_interrupted()
        async with open_reader() as r:
            r1 = r.execute(
                "SELECT status, last_message, finished_at FROM jobs WHERE id='S1'"
            ).fetchone()
            r2 = r.execute("SELECT status FROM jobs WHERE id='S2'").fetchone()
        assert r1[0] == "interrupted"
        assert r1[1] is not None
        assert r1[2] is not None
        assert r2[0] == "pending"  # untouched
