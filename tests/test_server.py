"""Tests for harness_mcp.server — tool registration and error mapping."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp import types as mcp_types

from harness_mcp.server import (
    _client_factory,
    _to_call_tool_error,
    cancel_build,
    get_build_result,
    poll_build,
    start_build,
)
from harness_mcp.state import close_db, db_write, init_db
from harness_mcp.types import (
    DesignDocNotFoundError,
    InvalidOptionsError,
    JobNotFinishedError,
    UnknownJobError,
)


class TestClientFactoryDefaults:
    """Regression: probe spawns must be MCP-isolated.

    Without strict-mcp-config + empty mcp_servers, the spawned claude loads the
    user's full MCP set — which includes harness-mcp itself (recursive lifespan)
    and plugin MCPs like playwright (npx + chromium). That cascades into a fork
    bomb at startup; the parent Claude Code reports `harness-mcp: failed`.
    """

    def test_defaults_to_strict_mcp_config_and_empty_mcp_servers(self) -> None:
        client = _client_factory()
        opts = client.options
        assert opts.mcp_servers == {}
        assert opts.extra_args == {"strict-mcp-config": None}

    def test_caller_can_override_mcp_servers_for_legitimate_probes(self) -> None:
        # assert_strict_mcp_config_works needs context7 to verify the flag works.
        client = _client_factory(mcp_servers={"context7": {"command": "ctx7"}})
        opts = client.options
        assert opts.mcp_servers == {"context7": {"command": "ctx7"}}
        # strict-mcp-config still applied so only the explicit servers load.
        assert opts.extra_args == {"strict-mcp-config": None}


class TestErrorMapper:
    @pytest.mark.parametrize(
        "exc,expected_code",
        [
            (UnknownJobError("J"), "UNKNOWN_JOB"),
            (JobNotFinishedError("J"), "JOB_NOT_FINISHED"),
            (DesignDocNotFoundError("/x"), "DESIGN_DOC_NOT_FOUND"),
            (InvalidOptionsError("k"), "INVALID_OPTIONS"),
        ],
    )
    def test_known_errors_map_to_codes(self, exc: Exception, expected_code: str) -> None:
        result = _to_call_tool_error(exc)
        assert result.isError is True
        assert result.structuredContent is not None
        # structured_content holds the code under "code".
        assert result.structuredContent["code"] == expected_code
        assert isinstance(result.structuredContent["message"], str)


@pytest.fixture
async def initialized_db(tmp_harness_home: Path) -> AsyncIterator[Path]:
    close_db()  # ensure clean state from prior tests
    init_db()
    yield tmp_harness_home / "state.db"
    close_db()


def _assert_error_result(
    result: object, expected_code: str, *, expected_message_prefix: str | None = None
) -> None:
    """Assert a tool's return value is the spec'd CallToolResult error shape."""
    assert isinstance(result, mcp_types.CallToolResult), (
        f"expected CallToolResult, got {type(result).__name__}"
    )
    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["code"] == expected_code
    assert isinstance(result.structuredContent["message"], str)
    if expected_message_prefix is not None:
        assert result.structuredContent["message"].startswith(expected_message_prefix)


class TestToolErrorIntegration:
    """End-to-end: tool functions return CallToolResult with code on HarnessToolError."""

    @pytest.mark.asyncio
    async def test_poll_build_unknown_job(self, initialized_db: Path) -> None:
        result = await poll_build("DOES_NOT_EXIST")
        _assert_error_result(result, "UNKNOWN_JOB", expected_message_prefix="DOES_NOT_EXIST")

    @pytest.mark.asyncio
    async def test_get_build_result_unknown_job(self, initialized_db: Path) -> None:
        result = await get_build_result("DOES_NOT_EXIST")
        _assert_error_result(result, "UNKNOWN_JOB")

    @pytest.mark.asyncio
    async def test_get_build_result_job_not_finished(self, initialized_db: Path) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("RUNNING_JOB", "running", "planning", "/x", "{}", 1, 1),
        )
        result = await get_build_result("RUNNING_JOB")
        _assert_error_result(result, "JOB_NOT_FINISHED")

    @pytest.mark.asyncio
    async def test_cancel_build_unknown_job(self, initialized_db: Path) -> None:
        result = await cancel_build("DOES_NOT_EXIST")
        _assert_error_result(result, "UNKNOWN_JOB")

    @pytest.mark.asyncio
    async def test_start_build_design_doc_not_found(self, initialized_db: Path) -> None:
        result = await start_build(design_doc_path="/no/such/file.md")
        _assert_error_result(result, "DESIGN_DOC_NOT_FOUND")

    @pytest.mark.asyncio
    async def test_start_build_invalid_options(self, initialized_db: Path, tmp_path: Path) -> None:
        design = tmp_path / "design.md"
        design.write_text("# design\n", encoding="utf-8")
        result = await start_build(design_doc_path=str(design), options={"bogus_key": 1})
        _assert_error_result(result, "INVALID_OPTIONS")


class TestToolHappyPath:
    """Spec §3 — every documented success-shape key must be present in the
    tool's return dict, and the values must come from the live state row.
    """

    @pytest.mark.asyncio
    async def test_poll_build_returns_full_documented_shape(
        self, initialized_db: Path
    ) -> None:
        """Spec §3 row + computed `sprints_completed` field. Insert a row
        with two passed sprints and one failed; verify poll_build returns
        every documented key with the right value."""
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "last_message, plan_review_rounds, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "POLL_J",
                "running",
                "sprint-3/eval-static",
                "/abs/design.md",
                "{}",
                "halfway through",
                2,
                1700000000000,
                1700000060000,
            ),
        )
        # Two passed + one failed sprint — sprints_completed must equal 2.
        for seq, status in [(1, "passed"), (2, "passed"), (3, "failed")]:
            await db_write(
                "INSERT INTO sprints (job_id, seq, title, status) VALUES (?, ?, ?, ?)",
                ("POLL_J", seq, f"S{seq}", status),
            )
        result = await poll_build("POLL_J")
        # Spec §3 documented keys, exact set.
        assert set(result.keys()) == {
            "status",
            "current_phase",
            "last_message",
            "plan_review_rounds",
            "sprints_completed",
            "started_at",
            "updated_at",
        }
        assert result["status"] == "running"
        assert result["current_phase"] == "sprint-3/eval-static"
        assert result["last_message"] == "halfway through"
        assert result["plan_review_rounds"] == 2
        assert result["sprints_completed"] == 2
        assert result["started_at"] == 1700000000000
        assert result["updated_at"] == 1700000060000

    @pytest.mark.asyncio
    async def test_get_build_result_returns_full_documented_shape(
        self, initialized_db: Path, tmp_harness_home: Path
    ) -> None:
        """Spec §3 — get_build_result on terminal job returns app_path,
        summary, final_status, sprints list, plan_review_rounds, duration."""
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "last_message, plan_review_rounds, started_at, updated_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "DONE_J",
                "completed",
                "done",
                "/abs/design.md",
                "{}",
                "fallback summary text",
                3,
                1700000000000,
                1700000123000,
                1700000123000,
            ),
        )
        await db_write(
            "INSERT INTO sprints (job_id, seq, title, status, retry_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("DONE_J", 1, "Frame the app", "passed", 1),
        )
        # Surface a real summary.md so the on-disk override is exercised.
        job_dir = tmp_harness_home / "jobs" / "DONE_J"
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "summary.md").write_text("real summary from disk", encoding="utf-8")

        result = await get_build_result("DONE_J")
        assert set(result.keys()) == {
            "app_path",
            "summary",
            "final_status",
            "sprints",
            "plan_review_rounds",
            "duration_seconds",
        }
        assert result["app_path"].endswith("/jobs/DONE_J/app")
        # summary.md on disk wins over last_message fallback.
        assert result["summary"] == "real summary from disk"
        assert result["final_status"] == "completed"
        assert result["sprints"] == [
            {"seq": 1, "title": "Frame the app", "status": "passed", "retry_count": 1}
        ]
        assert result["plan_review_rounds"] == 3
        assert result["duration_seconds"] == 123.0  # (finished - started) / 1000

    @pytest.mark.asyncio
    async def test_get_build_result_falls_back_to_last_message_when_summary_missing(
        self, initialized_db: Path
    ) -> None:
        """Spec §3 / server.py: if summary.md is absent, surface
        last_message in the `summary` field instead."""
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "last_message, plan_review_rounds, started_at, updated_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "FB_J",
                "failed",
                "sprint-2/retry",
                "/abs/design.md",
                "{}",
                "sprint_timeout",
                0,
                1,
                2,
                3,
            ),
        )
        result = await get_build_result("FB_J")
        # Fallback engaged because no summary.md was written.
        assert result["summary"] == "sprint_timeout"
        assert result["final_status"] == "failed"
