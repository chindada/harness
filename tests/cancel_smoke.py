"""Manual smoke test for `cancel_build` end-to-end behavior.

Run via: `uv run python tests/cancel_smoke.py`. Excluded from `pytest`
collection because the filename doesn't match `test_*.py`.

Per spec §12.2:1385, `cancel_build` is excluded from `tests/smoke.py`
("start/poll/get" wording from the brief). This file covers the
spec-§3.2 cancel semantics that smoke does not:

  1. start_build returns a job_id.
  2. cancel_build returns ok=True with was_already_terminal=False while
     the job is running.
  3. After cancel, poll_build reports status='cancelled' and the
     last_message includes "cancelled by user".
  4. cancel_build is idempotent: calling it again returns
     was_already_terminal=True.
  5. get_build_result returns final_status='cancelled'.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import anyio

REPO = Path(__file__).resolve().parent.parent
DESIGN = REPO / "examples" / "todo-app-design.md"


async def run_cancel_smoke() -> None:
    """Drive start_build → cancel_build → poll/get via the in-process server module."""
    from harness_mcp.server import (  # noqa: PLC0415
        cancel_build,
        get_build_result,
        poll_build,
        start_build,
    )

    job = await start_build(design_doc_path=str(DESIGN))
    job_id = job["job_id"]
    assert re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", job_id), job_id
    print(f"[cancel_smoke] started job {job_id}")

    # Let the orchestrator coroutine flip pending→running before cancelling.
    deadline = time.time() + 30
    while time.time() < deadline:
        status = await poll_build(job_id)
        if status["status"] == "running":
            break
        await anyio.sleep(0.5)

    cancel_result = await cancel_build(job_id)
    assert cancel_result["ok"] is True, cancel_result
    assert cancel_result["was_already_terminal"] is False, cancel_result
    print("[cancel_smoke] cancel_build returned ok=True")

    # Allow orchestrator a moment to observe its own cancel scope and unwind.
    await anyio.sleep(2)

    poll = await poll_build(job_id)
    assert poll["status"] == "cancelled", poll
    assert "cancelled by user" in poll["last_message"], poll
    print("[cancel_smoke] poll_build reports cancelled")

    # Idempotency.
    second = await cancel_build(job_id)
    assert second["ok"] is True and second["was_already_terminal"] is True, second
    print("[cancel_smoke] cancel_build is idempotent")

    final = await get_build_result(job_id)
    assert final["final_status"] == "cancelled", final
    print("[cancel_smoke] get_build_result reports cancelled — pass")


def main() -> int:
    anyio.run(run_cancel_smoke)
    return 0


if __name__ == "__main__":
    sys.exit(main())
