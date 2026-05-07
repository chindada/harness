"""Tests for harness_mcp.server — tool registration and error mapping."""

from __future__ import annotations

import pytest

from harness_mcp.server import _to_call_tool_error
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
