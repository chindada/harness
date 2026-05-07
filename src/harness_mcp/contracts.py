"""Round-by-round contract negotiation parsing + atomic file appends.

The orchestrator owns `contract.md` structure: agents emit message bodies,
this module reduces them to strings, the orchestrator concatenates a
`## Round N — <Role>` header on top, and we write the result atomically
via temp-and-rename.

Body extraction differs by SDK:
  * Codex (`thread.turn().stream()` events): concatenate every
    `item/agentMessage/delta` payload.delta string within the turn,
    excluding tool-call markers.
  * Claude (`AssistantMessage` content list): concatenate every TextBlock's
    `.text`, excluding ToolUseBlock entries.

APPROVED check: the body's last non-empty line must equal `APPROVED`
exactly (case-sensitive). Spec §6.1.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def parse_round_body_from_codex_events(events: list[Any]) -> str:
    """Reduce a Codex event stream slice to a single body string.

    Concatenates `item/agentMessage/delta` payload.delta values; ignores
    tool-call markers, turn boundaries, token-usage updates, etc.
    """
    parts: list[str] = []
    for event in events:
        method = getattr(event, "method", "")
        if method != "item/agentMessage/delta":
            continue
        payload = getattr(event, "payload", None)
        delta = getattr(payload, "delta", "") if payload is not None else ""
        if delta:
            parts.append(delta)
    return "".join(parts)


def parse_round_body_from_claude_msgs(msgs: list[Any]) -> str:
    """Reduce a Claude message list to the concatenated TextBlock content.

    `msgs` is the list yielded by iterating `query()` (or `client.receive_response()`).
    We pull text from every message's `.content` whose block class name is
    `TextBlock`. ToolUseBlocks are excluded — they're side effects, not body.

    Class-name comparison instead of isinstance keeps the parser independent
    from importing the SDK at module level (important for unit tests + for
    module-level import-graph isolation in the launcher).
    """
    parts: list[str] = []
    for msg in msgs:
        content = getattr(msg, "content", None)
        if not content:
            continue
        for block in content:
            if type(block).__name__ == "TextBlock":
                text = getattr(block, "text", "")
                if text:
                    parts.append(text)
    return "".join(parts)


def is_approved_body(body: str) -> bool:
    """True iff body's last non-empty line is exactly `APPROVED` (case-sensitive)."""
    if not body:
        return False
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    return lines[-1].strip() == "APPROVED"


def append_round_atomic(path: Path, header: str, body: str) -> None:
    """Append `<header><body>\\n` to `path` via temp-and-rename.

    Reads existing file (or treats as empty if missing), concatenates
    in memory, writes to `<path>.tmp`, then `os.replace()` to `path`.
    Same pattern used for handoff-NNN.md (§7.1) and eval.md.
    """
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    new_content = existing + header + body
    if not new_content.endswith("\n"):
        new_content += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    os.replace(tmp, path)
