"""Per-chunk Codex event-stream → log.txt formatter.

Codex events arrive interleaved (a tool call's start and result are
separate events). To produce useful single-line entries like
`[tool: Read args=<...>] -> <result>`, we buffer in-flight calls keyed
by item id and emit the combined line on `item/completed`.

Orphaned starts (no completion by chunk end) flush on `aclose()` as
`[tool: ... -> NO_RESULT]` so the chunk's behavior is fully recorded
even on cancellation.

Writes go through `anyio.to_thread.run_sync` so the event loop stays
responsive under high event-rate streams. The file is opened with
`buffering=1` (line-buffered) so live `tail -f` works.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio


@dataclass
class _ToolStart:
    name: str
    args: str


def _truncate(value: Any, max_len: int = 200) -> str:  # noqa: ANN401 — duck-typed event payload
    """Stringify and clip with an ellipsis. Robust to any input."""
    s = str(value)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def _summarize_item(item: Any) -> tuple[str, str]:  # noqa: ANN401 — duck-typed Codex item
    """Return (display_name, args_summary) for an in-flight item."""
    item_type = getattr(item, "type", None)
    if item_type == "mcpToolCall":
        return (
            getattr(item, "tool", "<unknown-tool>"),
            _truncate(getattr(item, "arguments", "")),
        )
    if item_type == "commandExecution":
        return ("exec", _truncate(getattr(item, "command", "")))
    return (str(item_type), "")


def _summarize_item_result(item: Any) -> str:  # noqa: ANN401 — duck-typed Codex item
    """Best-effort result summary for a completed item."""
    item_type = getattr(item, "type", None)
    if item_type == "commandExecution":
        return _truncate(getattr(item, "aggregatedOutput", "") or getattr(item, "error", ""))
    if item_type == "mcpToolCall":
        return _truncate(getattr(item, "result", "") or getattr(item, "error", ""))
    return ""


class EventLogger:
    """Stateful per-chunk Codex event → log.txt formatter."""

    def __init__(self, log_path: Path) -> None:
        # Open ONCE per chunk; held until aclose(). buffering=1 = line-buffered.
        self._fh = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        self._calls: dict[str, _ToolStart] = {}
        self._closed = False

    async def handle(self, event: Any) -> None:  # noqa: ANN401 — duck-typed Codex event
        """Map one event to at most one log line."""
        method = getattr(event, "method", "")
        payload = getattr(event, "payload", None)
        line: str | None = None

        if method == "item/agentMessage/delta":
            delta = getattr(payload, "delta", "") if payload else ""
            line = delta or None
        elif method == "item/started":
            item = getattr(payload, "item", None) if payload else None
            if item is not None:
                item_id = getattr(item, "id", None)
                name, args = _summarize_item(item)
                if item_id:
                    self._calls[item_id] = _ToolStart(name=name, args=args)
        elif method == "item/completed":
            item = getattr(payload, "item", None) if payload else None
            item_id = getattr(item, "id", None) if item is not None else None
            start = self._calls.pop(item_id, None) if item_id else None
            if start is not None:
                # Spec §7.3:888 — wrap _summarize_item_result in _truncate so the
                # tool-result side of the log line is always bounded, regardless
                # of whether _summarize_item_result internally truncates.
                result = _truncate(_summarize_item_result(item))
                line = f"[tool: {start.name} args={start.args} -> {result}]"
        elif method in ("turn/started", "turn/completed"):
            turn = getattr(payload, "turn", None) if payload else None
            tid = getattr(turn, "id", "?") if turn is not None else "?"
            if method == "turn/started":
                line = f"--- turn {tid} (started) ---"
            else:
                status_obj = getattr(turn, "status", None) if turn is not None else None
                status = getattr(status_obj, "value", str(status_obj)) if status_obj else "?"
                line = f"--- turn {tid} ({status}) ---"
        # Other event types ignored.

        if line is not None:
            await anyio.to_thread.run_sync(self._fh.write, line + "\n")

    async def flush(self) -> None:
        """Drain orphan tool-call starts and flush the file handle."""
        for start in list(self._calls.values()):
            await anyio.to_thread.run_sync(
                self._fh.write,
                f"[tool: {start.name} args={start.args} -> NO_RESULT]\n",
            )
        self._calls.clear()
        await anyio.to_thread.run_sync(self._fh.flush)

    async def aclose(self) -> None:
        """Idempotent. Drain orphans, close the file handle."""
        if self._closed:
            return
        await self.flush()
        await anyio.to_thread.run_sync(self._fh.close)
        self._closed = True
