"""Subprocess + process-group lifecycle scope for the Evaluator launcher.

Why a launcher subprocess at all:
  The Claude Agent SDK does NOT expose a `popen_factory` / `preexec_fn` /
  `start_new_session` knob on its internal subprocess transport. To kill
  the SDK's child plus any grandchildren (Bash dev servers, pytest workers,
  Playwright Chromium), we wrap the SDK call in our own process so we
  control the start_new_session flag.

Why a process-group scope:
  An async context manager so cleanup runs on every exit path — clean
  return, exception, or anyio cancellation. SIGTERM → wait grace_seconds
  → SIGKILL stragglers. Cleanup is shielded with anyio.CancelScope(shield=True)
  so a re-cancel during cleanup can't leak children.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from anyio.abc import Process

# Re-export so callers don't need to import subprocess.PIPE separately.
PIPE: int = subprocess.PIPE


@dataclass
class ProcessGroupHandle:
    """Returned to the body of `async with ProcessGroupScope(...) as pg:`.

    Holds the tracked pgid (set by the first `spawn`) and exposes
    `spawn` + `communicate` helpers.
    """

    label: str
    grace_seconds: float
    tracked_pgid: int | None = None

    async def spawn(
        self,
        cmd: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> Process:
        """Spawn a subprocess with `start_new_session=True`.

        The subprocess becomes its own session leader; its pgid equals
        its pid. We track that pgid for cleanup. Only the FIRST call's
        pid is tracked — secondary spawns within the same scope share
        cleanup with the parent (rare; most callers spawn one launcher).
        """
        proc = await anyio.open_process(
            cmd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        if self.tracked_pgid is None:
            self.tracked_pgid = proc.pid
        return proc

    async def communicate(self, proc: Process, payload: bytes) -> None:
        """Write payload to the child's stdin, close it.

        Mirrors `subprocess.Popen.communicate` semantics for the input side.
        Output draining is the caller's responsibility (see `evaluator.py`'s
        stdout/stderr drainer pattern in spec §8.4).
        """
        if proc.stdin is None:
            raise RuntimeError("process not spawned with stdin=PIPE")
        await proc.stdin.send(payload)
        await proc.stdin.aclose()


@asynccontextmanager
async def ProcessGroupScope(
    label: str, grace_seconds: float = 5.0
) -> AsyncIterator[ProcessGroupHandle]:
    """Bracket the lifetime of a launcher subprocess + its descendants.

    On context exit:
      1. Send SIGTERM to the tracked pgid.
      2. Wait up to `grace_seconds` for the group to clean up.
      3. SIGKILL any stragglers.

    All cleanup is shielded so an outer cancel during cleanup can't
    leak grandchildren. ProcessLookupError (group already gone) is
    swallowed — the goal is "nothing alive after this", not "we killed
    something".
    """
    handle = ProcessGroupHandle(label=label, grace_seconds=grace_seconds)
    try:
        yield handle
    finally:
        with anyio.CancelScope(shield=True):
            await _kill_pgroup(handle.tracked_pgid, handle.grace_seconds)


async def _kill_pgroup(pgid: int | None, grace_seconds: float) -> None:
    if pgid is None:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        # Most likely the OS already reaped and recycled the pid. Best-effort.
        return

    # Wait for children to exit. Poll via os.killpg(pgid, 0) which raises
    # ProcessLookupError when the group is gone.
    deadline = anyio.current_time() + grace_seconds
    while anyio.current_time() < deadline:
        await anyio.sleep(0.05)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return

    # Stragglers — kill hard.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
