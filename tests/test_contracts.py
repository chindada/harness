"""Tests for harness_mcp.contracts — body extraction + APPROVED detection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harness_mcp.contracts import (
    append_round_atomic,
    is_approved_body,
    parse_round_body_from_claude_msgs,
    parse_round_body_from_codex_events,
)


@dataclass
class FakeEvent:
    method: str
    payload: Any


class TestIsApprovedBody:
    def test_bare_approved(self) -> None:
        assert is_approved_body("APPROVED") is True

    def test_approved_with_trailing_newline(self) -> None:
        assert is_approved_body("APPROVED\n") is True

    def test_approved_at_end_of_paragraph(self) -> None:
        body = "we accept the criteria as proposed.\n\nAPPROVED"
        assert is_approved_body(body) is True

    def test_approved_inline_NOT_accepted(self) -> None:
        # Spec §6.1: "APPROVED ... on its own line at the end of your response"
        assert is_approved_body("we say APPROVED here") is False

    def test_lowercase_not_approved(self) -> None:
        assert is_approved_body("approved") is False

    def test_empty_body_not_approved(self) -> None:
        assert is_approved_body("") is False
        assert is_approved_body("   \n\n") is False


class TestParseRoundBodyFromCodexEvents:
    def test_concatenates_agent_message_deltas(self) -> None:
        events = [
            FakeEvent("item/agentMessage/delta", SimpleNamespace(delta="hello ")),
            FakeEvent("item/agentMessage/delta", SimpleNamespace(delta="world")),
            FakeEvent("turn/completed", SimpleNamespace(turn=SimpleNamespace(id="t1"))),
        ]
        body = parse_round_body_from_codex_events(events)
        assert body == "hello world"

    def test_excludes_tool_calls(self) -> None:
        cmd_item = SimpleNamespace(item=SimpleNamespace(id="x", type="commandExecution"))
        events = [
            FakeEvent("item/agentMessage/delta", SimpleNamespace(delta="prelude ")),
            FakeEvent("item/started", cmd_item),
            FakeEvent("item/completed", cmd_item),
            FakeEvent("item/agentMessage/delta", SimpleNamespace(delta="postlude")),
            FakeEvent("turn/completed", SimpleNamespace(turn=SimpleNamespace(id="t1"))),
        ]
        body = parse_round_body_from_codex_events(events)
        assert body == "prelude postlude"

    def test_empty_when_no_text_deltas(self) -> None:
        events = [
            FakeEvent("turn/completed", SimpleNamespace(turn=SimpleNamespace(id="t1"))),
        ]
        body = parse_round_body_from_codex_events(events)
        assert body == ""


# The parser keys off `type(block).__name__ == "TextBlock"` / "ToolUseBlock", so the
# stand-in classes MUST be named exactly that. Class names need no underscore prefix
# because we never import the real SDK classes here — there's no collision.
class TextBlock:
    """Stand-in for claude_agent_sdk.TextBlock — `type(...).__name__ == "TextBlock"`."""

    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    """Stand-in for claude_agent_sdk.ToolUseBlock."""

    def __init__(self, name: str, inp: dict[str, Any] | None = None) -> None:
        self.name = name
        self.input = inp or {}


class TestParseRoundBodyFromClaudeMsgs:
    def test_extracts_text_blocks_from_final_assistant_message(self) -> None:
        # `parse_round_body_from_claude_msgs` keys off `type(block).__name__ == "TextBlock"`
        # — using a real class literally named TextBlock satisfies that check.
        # (SimpleNamespace's __class__.__name__ is read-only, so subclassing/mutation
        # isn't a viable shortcut.)
        msgs = [SimpleNamespace(content=[TextBlock("hello world")])]
        body = parse_round_body_from_claude_msgs(msgs)
        assert body == "hello world"

    def test_concatenates_multiple_text_blocks(self) -> None:
        msgs = [SimpleNamespace(content=[TextBlock("part 1 "), TextBlock("part 2")])]
        body = parse_round_body_from_claude_msgs(msgs)
        assert body == "part 1 part 2"

    def test_skips_tool_use_blocks(self) -> None:
        msgs = [
            SimpleNamespace(
                content=[
                    TextBlock("prelude"),
                    ToolUseBlock("Read", {"file_path": "x"}),
                ]
            )
        ]
        body = parse_round_body_from_claude_msgs(msgs)
        assert body == "prelude"


class TestAppendRoundAtomic:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.md"
        append_round_atomic(path, "## Round 1 — Generator\n", "criteria proposal\n")
        text = path.read_text(encoding="utf-8")
        assert "## Round 1 — Generator" in text
        assert "criteria proposal" in text

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.md"
        path.write_text("# Sprint 1\n\n## Round 1 — Generator\nold\n", encoding="utf-8")
        append_round_atomic(path, "## Round 1 — Evaluator\n", "evaluator response\n")
        text = path.read_text(encoding="utf-8")
        assert "Round 1 — Generator" in text
        assert "Round 1 — Evaluator" in text

    def test_temp_file_cleaned_up(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.md"
        append_round_atomic(path, "## Round 1\n", "body\n")
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []
