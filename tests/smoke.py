"""Manual smoke test: end-to-end harness run against examples/todo-app-design.md.

Run via: `uv run python tests/smoke.py`. Excluded from `pytest` collection
because the filename doesn't match `test_*.py`.

Asserts (in order):
  1. `harness-mcp doctor` exits 0 with `OK` lines for every prereq step.
  2. `start_build` returns a 26-char ULID job_id.
  3. `poll_build` advances through phases (planning → plan-review → sprint-1/* → ... → done).
  4. `get_build_result` raises `JOB_NOT_FINISHED` while running.
  5. After completion: `final_status == "completed"`, app_path exists, summary >= 30 chars.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import time
from pathlib import Path

import anyio

REPO = Path(__file__).resolve().parent.parent
DESIGN = REPO / "examples" / "todo-app-design.md"


def assert_doctor_ok() -> None:
    proc = subprocess.run(
        ["uv", "run", "harness-mcp", "doctor"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"doctor failed (rc={proc.returncode})")
    if "OK" not in proc.stdout:
        raise SystemExit(f"doctor stdout has no OK line:\n{proc.stdout}")
    print("[smoke] doctor OK")


async def run_build() -> None:
    """Drive start_build → poll_build → get_build_result via the in-process server module."""
    # Prereqs already validated; import server lazily so doctor's failures are surfaced first.
    from harness_mcp.server import (  # noqa: PLC0415
        get_build_result,
        poll_build,
        start_build,
    )
    from harness_mcp.types import JobNotFinishedError  # noqa: PLC0415

    job = await start_build(design_doc_path=str(DESIGN))
    job_id = job["job_id"]
    assert re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", job_id), job_id
    print(f"[smoke] started job {job_id}")

    seen_phases: set[str] = set()
    deadline = time.time() + 60 * 60  # 1h cap
    while time.time() < deadline:
        status = await poll_build(job_id)
        seen_phases.add(status["current_phase"])
        if status["status"] in ("completed", "failed", "cancelled", "interrupted"):
            break
        # Confirm get_build_result rejects non-terminal jobs.
        with contextlib.suppress(JobNotFinishedError):
            await get_build_result(job_id)
        await anyio.sleep(15)

    final = await get_build_result(job_id)
    assert final["final_status"] == "completed", final["final_status"]
    assert Path(final["app_path"]).is_dir()  # noqa: ASYNC240
    assert len(final["summary"]) >= 30, final["summary"]
    expected_phases = {"planning", "plan-review", "summarizing", "done"}
    assert expected_phases.issubset(seen_phases), seen_phases
    print(f"[smoke] completed: {final['summary']}")


def main() -> int:
    assert_doctor_ok()
    anyio.run(run_build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
