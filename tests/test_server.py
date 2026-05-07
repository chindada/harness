"""Tests for harness_mcp.server — tool registration and error mapping."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp import types as mcp_types

from harness_mcp.server import (
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
