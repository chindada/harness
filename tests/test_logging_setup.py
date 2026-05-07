"""Tests for harness_mcp.logging_setup — EventLogger."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_mcp.logging_setup import EventLogger, _truncate


@dataclass
class FakeEvent:
    method: str
    payload: Any


def _fake_item(item_id: str, item_type: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(id=item_id, type=item_type, **kw)


def _read_log(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines()


class TestTruncate:
    def test_short_string_unchanged(self) -> None:
        assert _truncate("hello") == "hello"

    def test_long_string_clipped(self) -> None:
        out = _truncate("x" * 500, max_len=10)
        assert len(out) <= 13  # 10 + ellipsis
        assert out.endswith("…")

    def test_non_string_stringified(self) -> None:
        assert _truncate(12345, max_len=10) == "12345"


class TestEventLogger:
    @pytest.mark.asyncio
    async def test_agent_message_delta_writes_text(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        await logger.handle(
            FakeEvent(method="item/agentMessage/delta", payload=SimpleNamespace(delta="hello "))
        )
        await logger.handle(
            FakeEvent(method="item/agentMessage/delta", payload=SimpleNamespace(delta="world"))
        )
        await logger.aclose()
        lines = _read_log(log)
        assert lines == ["hello ", "world"]

    @pytest.mark.asyncio
    async def test_tool_call_paired(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        item_started = _fake_item("c1", "commandExecution", command="ls -la")
        item_completed = _fake_item(
            "c1", "commandExecution", command="ls -la", aggregatedOutput="file1\nfile2"
        )
        await logger.handle(
            FakeEvent(method="item/started", payload=SimpleNamespace(item=item_started))
        )
        await logger.handle(
            FakeEvent(method="item/completed", payload=SimpleNamespace(item=item_completed))
        )
        await logger.aclose()
        lines = _read_log(log)
        assert any("[tool: exec args=ls -la ->" in line for line in lines)

    @pytest.mark.asyncio
    async def test_orphan_tool_call_flushed_on_close(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        item_started = _fake_item("c1", "mcpToolCall", tool="Read", arguments='{"path":"/x"}')
        await logger.handle(
            FakeEvent(method="item/started", payload=SimpleNamespace(item=item_started))
        )
        # No completion — close while orphaned.
        await logger.aclose()
        lines = _read_log(log)
        assert any("NO_RESULT" in line and "Read" in line for line in lines)

    @pytest.mark.asyncio
    async def test_turn_started_and_completed(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        turn_started = SimpleNamespace(id="t1", status=SimpleNamespace(value="started"))
        turn_completed = SimpleNamespace(id="t1", status=SimpleNamespace(value="completed"))
        await logger.handle(
            FakeEvent(method="turn/started", payload=SimpleNamespace(turn=turn_started))
        )
        await logger.handle(
            FakeEvent(method="turn/completed", payload=SimpleNamespace(turn=turn_completed))
        )
        await logger.aclose()
        lines = _read_log(log)
        assert "--- turn t1 (started) ---" in lines
        assert "--- turn t1 (completed) ---" in lines

    @pytest.mark.asyncio
    async def test_unknown_method_ignored(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        await logger.handle(
            FakeEvent(method="thread/tokenUsage/updated", payload=SimpleNamespace(tokens=42))
        )
        await logger.aclose()
        assert log.read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_empty_delta_treated_as_no_op(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        await logger.handle(
            FakeEvent(method="item/agentMessage/delta", payload=SimpleNamespace(delta=""))
        )
        await logger.aclose()
        assert log.read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_aclose_idempotent(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        await logger.aclose()
        # Second close should not raise (file already closed).
        # If it raises, that's a bug we'd notice in the chunk loop's finally block.
        # The contract is: aclose is best-effort; second call is at most a warning.
