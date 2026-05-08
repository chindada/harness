"""In-memory per-job pub/sub for orchestrator phase transitions.

Broker is single-process and in-memory by design. The SQLite jobs row
remains the source of truth for status; broker events are an opt-in
fast notification path for callers (run_build) that want sub-millisecond
phase updates without polling.

Backpressure: subscribers receive a bounded stream (size 32). Publish
calls send_nowait by default; on WouldBlock for a non-terminal event, the
event is silently dropped. For terminal events (status in
TERMINAL_JOB_STATUSES) publish falls back to awaited send so the terminal
event is guaranteed to be delivered.
"""

from __future__ import annotations

from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from harness_mcp.state import TERMINAL_JOB_STATUSES

_BUFFER_SIZE = 32


class PhaseBroker:
    """Per-job pub/sub. One broker instance per harness-mcp lifespan."""

    def __init__(self) -> None:
        self._streams: dict[str, list[MemoryObjectSendStream[dict[str, Any]]]] = {}

    def subscribe(self, job_id: str) -> MemoryObjectReceiveStream[dict[str, Any]]:
        """Return a receive stream for this job_id's phase events.

        If the job is already terminal in the DB, the returned stream is
        already-closed (`async for` exits immediately, `receive` raises
        EndOfStream). Caller is responsible for closing the receive end.
        """
        send, recv = anyio.create_memory_object_stream[dict[str, Any]](_BUFFER_SIZE)
        if _job_is_terminal_in_db(job_id):
            send.close()
            return recv
        self._streams.setdefault(job_id, []).append(send)
        return recv

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:
        """Fan out event to every subscriber for job_id.

        Drop-newest on backpressure for non-terminal events; awaited send
        for terminal events.
        """
        is_terminal = event.get("status") in TERMINAL_JOB_STATUSES
        for send in list(self._streams.get(job_id, ())):
            try:
                if is_terminal:
                    await send.send(event)
                else:
                    try:
                        send.send_nowait(event)
                    except anyio.WouldBlock:
                        pass
            except anyio.ClosedResourceError:
                # Subscriber's stream was closed concurrently (e.g., by close());
                # skip silently — the receive end will see EndOfStream cleanly.
                pass

    def close(self, job_id: str) -> None:
        """Close every subscriber stream for job_id. Idempotent."""
        for send in self._streams.pop(job_id, []):
            send.close()


def _job_is_terminal_in_db(job_id: str) -> bool:
    """Synchronous DB read — used only by subscribe(), which is sync."""
    import sqlite3

    from harness_mcp.config import state_db_path

    conn = sqlite3.connect(str(state_db_path()))
    try:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    return row[0] in TERMINAL_JOB_STATUSES
