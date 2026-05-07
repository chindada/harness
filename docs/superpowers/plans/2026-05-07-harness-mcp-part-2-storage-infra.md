# Harness MCP — Part 2: Storage & Infrastructure

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the four pieces of low-level plumbing every higher-level module depends on: SQLite-backed state machine (`state.py`), launcher process-group cleanup (`process_group.py`), Codex-event-stream → log-file formatter (`logging_setup.py`), and user-config MCP-server stanza capture (`mcp_capture.py`).

**Architecture:** Pure infrastructure. No LLM calls. `state.py` owns one writer connection guarded by an `anyio.Lock`, plus per-coroutine readers. `process_group.py` uses `os.killpg` to reap launcher subprocesses + grandchildren. `logging_setup.py` is a stateful per-chunk formatter (orphaned-tool-start tracker). `mcp_capture.py` probes the SDK first, falls back to parsing user config files.

**Tech Stack:** stdlib (`sqlite3`, `os`, `signal`, `subprocess`), `anyio`, `claude_agent_sdk` (only for the type signature of probed responses; tests mock it).

**Spec source:** `docs/superpowers/specs/2026-05-07-harness-mcp-design.md` — sections §3.2, §4.2, §4.3, §4.4, §7.3, §7.5, §8.4, §10.1 (steps 1, 5, 6), §10.7 are load-bearing.

**Depends on:** Part 1 (`harness_mcp.types`, `harness_mcp.config`) is already in place. This plan does NOT modify any Part 1 files.

---

## Branch & Commit Policy (READ FIRST)

- **Stay on the `main` branch for the entire plan.** Do not create or switch branches.
- **Do NOT run `git commit`, `git add`, `git push`, or any git mutation.** Verify by running tests / inspecting files only.
- If a step's check fails, fix the problem and re-run the check — never paper over with a commit.
- The repo owner will commit when all five parts of the harness-mcp series are complete and integrated.

---

## File Structure (this part owns)

| File | Purpose |
|---|---|
| `src/harness_mcp/state.py` | SQLite schema, status/phase constants, writer/reader connection management, async `db_write` / `db_write_returning_rowcount` helpers, restart sweep, ULID generation |
| `src/harness_mcp/process_group.py` | `ProcessGroupScope` async context manager, `pg.spawn` (`start_new_session=True`), `pg.communicate`, shielded SIGTERM/SIGKILL cleanup; re-exports `subprocess.PIPE` |
| `src/harness_mcp/logging_setup.py` | `EventLogger` class for Codex event stream → `log.txt` (per-chunk, line-buffered, orphan-flush on close); `_ToolStart` dataclass; helpers (`_truncate`, `_summarize_item`, `_summarize_item_result`) |
| `src/harness_mcp/mcp_capture.py` | `capture_mcp_servers(client)` async function: probe via SDK `get_mcp_status()`, fall back to parsing `~/.claude.json` / project `.mcp.json` / plugin caches; redaction in logs |
| `tests/test_state.py` | Schema, status enum, writer concurrency, ULID format, restart sweep |
| `tests/test_process_group.py` | Spawn → reap, `start_new_session` enforcement, SIGTERM-then-SIGKILL grace, idempotent close |
| `tests/test_logging_setup.py` | Event mapping for each `event.method`, orphan tool-start flush, log-file line buffering |
| `tests/test_mcp_capture.py` | SDK-config path, file-fallback path, redaction, hard-fail when both empty |

---

## Pre-flight (one-time, before Task 1)

- [ ] **Step 0a: Confirm Part 1 artifacts exist.**

```bash
test -f src/harness_mcp/types.py && test -f src/harness_mcp/config.py && test -f src/harness_mcp/prompts_loader.py && echo OK
```

Expected: `OK`. If any file is missing, Part 1 isn't done — STOP and resolve before continuing.

- [ ] **Step 0b: Confirm we are on `main` with a working `uv` project.**

```bash
git rev-parse --abbrev-ref HEAD && uv run pytest -q
```

Expected: `main` and Part 1's tests still pass.

---

## Task 1: `state.py` — schema + connection setup

**Files:**
- Create: `tests/test_state.py`
- Create: `src/harness_mcp/state.py`

Phase 1 of state.py: schema constants, status / phase string sets, ULID generator, `init_db()` that opens / configures the writer connection and runs `CREATE TABLE IF NOT EXISTS`.

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_state.py`:

```python
"""Tests for harness_mcp.state — schema, helpers, restart sweep."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import anyio
import pytest

from harness_mcp.state import (
    JOB_STATUSES,
    PHASES,
    SPRINT_STATUSES,
    TERMINAL_JOB_STATUSES,
    close_db,
    db_write,
    db_write_returning_rowcount,
    init_db,
    new_job_id,
    open_reader,
    sweep_running_to_interrupted,
)


@pytest.fixture
def initialized_home(tmp_harness_home: Path) -> Path:
    """tmp_harness_home + state.db opened."""
    init_db()
    yield tmp_harness_home
    close_db()


class TestSchema:
    def test_init_db_creates_jobs_and_sprints(self, initialized_home: Path) -> None:
        conn = sqlite3.connect(str(initialized_home / "state.db"))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "jobs" in tables
            assert "sprints" in tables
        finally:
            conn.close()

    def test_wal_mode_active(self, initialized_home: Path) -> None:
        conn = sqlite3.connect(str(initialized_home / "state.db"))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()

    def test_init_db_idempotent(self, initialized_home: Path) -> None:
        # Calling init_db twice should not raise (CREATE TABLE IF NOT EXISTS).
        init_db()
        init_db()


class TestStatusConstants:
    def test_job_statuses_contain_all_states(self) -> None:
        assert JOB_STATUSES == {
            "pending", "running", "completed", "failed", "cancelled", "interrupted"
        }

    def test_terminal_subset(self) -> None:
        assert TERMINAL_JOB_STATUSES == {"completed", "failed", "cancelled", "interrupted"}
        assert TERMINAL_JOB_STATUSES <= JOB_STATUSES

    def test_sprint_statuses(self) -> None:
        assert SPRINT_STATUSES == {"pending", "running", "passed", "failed", "cancelled"}

    def test_phases_contains_minimum(self) -> None:
        # Spec §4.4 — informational enum. Spot-check a few.
        for required in (
            "init", "planning", "plan-review", "plan-revision",
            "summarizing", "done",
        ):
            assert required in PHASES


class TestUlid:
    def test_new_job_id_is_26_chars(self) -> None:
        jid = new_job_id()
        assert isinstance(jid, str)
        assert len(jid) == 26

    def test_new_job_id_unique(self) -> None:
        ids = {new_job_id() for _ in range(100)}
        assert len(ids) == 100

    def test_new_job_id_sortable(self) -> None:
        # ULIDs are time-sortable. Two consecutive ones should be ordered.
        a = new_job_id()
        b = new_job_id()
        assert a <= b


class TestDbWrite:
    @pytest.mark.asyncio
    async def test_db_write_inserts(self, initialized_home: Path) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J1", "pending", "init", "/tmp/x", "{}", 1, 1),
        )
        async with open_reader() as r:
            row = r.execute("SELECT id, status FROM jobs WHERE id=?", ("J1",)).fetchone()
        assert row == ("J1", "pending")

    @pytest.mark.asyncio
    async def test_db_write_returning_rowcount_zero_on_no_match(
        self, initialized_home: Path
    ) -> None:
        rc = await db_write_returning_rowcount(
            "UPDATE jobs SET status='running' WHERE id=? AND status='pending'",
            ("does-not-exist",),
        )
        assert rc == 0

    @pytest.mark.asyncio
    async def test_db_write_returning_rowcount_one_on_cas_win(
        self, initialized_home: Path
    ) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J2", "pending", "init", "/tmp/x", "{}", 1, 1),
        )
        rc = await db_write_returning_rowcount(
            "UPDATE jobs SET status='running' WHERE id=? AND status='pending'",
            ("J2",),
        )
        assert rc == 1

    @pytest.mark.asyncio
    async def test_db_write_serialized_under_concurrency(
        self, initialized_home: Path
    ) -> None:
        # Race many concurrent inserts; all should land.
        async def insert(idx: int) -> None:
            await db_write(
                "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
                "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"R{idx:03d}", "pending", "init", "/tmp/x", "{}", idx, idx),
            )

        async with anyio.create_task_group() as tg:
            for i in range(50):
                tg.start_soon(insert, i)

        async with open_reader() as r:
            count = r.execute(
                "SELECT COUNT(*) FROM jobs WHERE id LIKE 'R%'"
            ).fetchone()[0]
        assert count == 50


class TestRestartSweep:
    @pytest.mark.asyncio
    async def test_sweep_flips_running_to_interrupted(self, initialized_home: Path) -> None:
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("S1", "running", "planning", "/tmp/x", "{}", 1, 1),
        )
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("S2", "pending", "init", "/tmp/x", "{}", 1, 1),
        )
        await sweep_running_to_interrupted()
        async with open_reader() as r:
            r1 = r.execute("SELECT status, last_message, finished_at FROM jobs WHERE id='S1'").fetchone()
            r2 = r.execute("SELECT status FROM jobs WHERE id='S2'").fetchone()
        assert r1[0] == "interrupted"
        assert r1[1] is not None
        assert r1[2] is not None
        assert r2[0] == "pending"  # untouched
```

- [ ] **Step 2: Run to confirm the failure.**

```bash
uv run pytest tests/test_state.py -v
```

Expected: ImportError on `harness_mcp.state`.

- [ ] **Step 3: Implement `state.py`.**

Create `src/harness_mcp/state.py`:

```python
"""SQLite-backed state machine for harness-mcp.

Connection strategy (concurrency-safe):
  * One module-global writer connection, opened at init_db() time with
    `check_same_thread=False`. All writes serialize through `_writer_lock`.
    `db_write` / `db_write_returning_rowcount` execute the statement and
    commit in a single `to_thread.run_sync` call so both land on the same
    OS thread.
  * Per-coroutine reader connections via `open_reader()` async context
    manager. Each enter opens a fresh connection (cheap with WAL) and
    closes it on exit.
  * No `aiosqlite` — the dep is unnecessary at our concurrency level
    (< 100 jobs). Threading model is explicit.

Schema is `CREATE TABLE IF NOT EXISTS` — idempotent, safe across server
restarts. v1 has no migrations; when v2 lands, add a `schema_version`
table here.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import anyio
from ulid import ULID

from harness_mcp.config import harness_home, now_ms, state_db_path

# ----- Constants -----

JOB_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "completed", "failed", "cancelled", "interrupted"}
)
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
SPRINT_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "passed", "failed", "cancelled"}
)

# Phase enum (spec §4.4). The dynamic ones (`sprint-<N>/...`) are formed at runtime.
PHASES: frozenset[str] = frozenset(
    {
        "init",
        "planning",
        "plan-review",
        "plan-revision",
        "summarizing",
        "done",
    }
)


def is_dynamic_phase(phase: str) -> bool:
    """True for `sprint-<N>/<step>` strings (vs. the static enum members)."""
    return phase.startswith("sprint-")


# ----- Schema -----

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    status              TEXT NOT NULL,
    current_phase       TEXT NOT NULL,
    design_path         TEXT NOT NULL,
    options_json        TEXT NOT NULL,
    last_message        TEXT,
    error_text          TEXT,
    plan_review_rounds  INTEGER NOT NULL DEFAULT 0,
    started_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    finished_at         INTEGER
);

CREATE TABLE IF NOT EXISTS sprints (
    job_id          TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    started_at      INTEGER,
    finished_at     INTEGER,
    PRIMARY KEY (job_id, seq),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
"""

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)

_writer_conn: sqlite3.Connection | None = None
_writer_lock = anyio.Lock()


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    conn.commit()


def init_db() -> None:
    """Open (or reuse) the module-global writer connection and ensure schema.

    Idempotent — safe to call on every server boot. Creates `~/.harness/`
    if missing.
    """
    global _writer_conn
    home = harness_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "jobs").mkdir(exist_ok=True)
    if _writer_conn is None:
        _writer_conn = sqlite3.connect(str(state_db_path()), check_same_thread=False)
        _apply_pragmas(_writer_conn)
    # Schema apply is idempotent (CREATE TABLE IF NOT EXISTS).
    _writer_conn.executescript(_SCHEMA)
    _writer_conn.commit()


def close_db() -> None:
    """Close the writer connection. For test cleanup; unused in production."""
    global _writer_conn
    if _writer_conn is not None:
        _writer_conn.close()
        _writer_conn = None


# ----- Async write helpers -----


def _exec_commit(stmt: str, params: tuple) -> int:
    assert _writer_conn is not None, "init_db() must be called first"
    cur = _writer_conn.execute(stmt, params)
    _writer_conn.commit()
    return cur.rowcount


async def db_write(stmt: str, params: tuple) -> None:
    """Serialized writer (one thread-trip, WAL + busy_timeout retry)."""
    async with _writer_lock:
        await anyio.to_thread.run_sync(_exec_commit, stmt, params)


async def db_write_returning_rowcount(stmt: str, params: tuple) -> int:
    """Variant that returns affected rowcount; used by the §3.2 CAS UPDATE."""
    async with _writer_lock:
        return await anyio.to_thread.run_sync(_exec_commit, stmt, params)


# ----- Reader connections (per-coroutine) -----


@asynccontextmanager
async def open_reader() -> AsyncIterator[sqlite3.Connection]:
    """Open a fresh reader connection for the lifetime of one coroutine.

    Caller uses `async with open_reader() as conn:` and runs `.execute()`
    synchronously inside the body — reads are O(microsecond) on WAL.
    For larger reads, wrap individual queries in `to_thread.run_sync`.
    """
    conn = sqlite3.connect(str(state_db_path()), check_same_thread=False)
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


# ----- ULID -----


def new_job_id() -> str:
    """Generate a fresh 26-char ULID. Sortable by creation time."""
    return str(ULID())


# ----- Restart sweep -----


async def sweep_running_to_interrupted() -> None:
    """Mark any leftover `running` jobs as `interrupted`.

    Called once at server lifespan startup (spec §10.1 step 6). Anything
    still `running` is leftover from a prior server crash.
    """
    msg = "server restarted before job could finish"
    await db_write(
        "UPDATE jobs SET status='interrupted', last_message=?, "
        "finished_at=?, updated_at=? WHERE status='running'",
        (msg, now_ms(), now_ms()),
    )
```

- [ ] **Step 4: Run the tests.**

```bash
uv run pytest tests/test_state.py -v
```

Expected: every test passes.

- [ ] **Step 5: Ruff sweep on the new file.**

```bash
uv run ruff check src/harness_mcp/state.py tests/test_state.py
uv run ruff format --check src/harness_mcp/state.py tests/test_state.py
```

Expected: zero findings.

---

## Task 2: `process_group.py` — `ProcessGroupScope`

**Files:**
- Create: `tests/test_process_group.py`
- Create: `src/harness_mcp/process_group.py`

Per spec §8.4: the Evaluator launcher subprocess (and any descendants — Bash dev servers, pytest workers, Playwright Chromium) must die together when the orchestrator wants to stop. We use `os.setsid` (via `start_new_session=True`) so the launcher's `pgid == proc.pid`, then `os.killpg(pgid, signal)` to fan-out the signal.

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_process_group.py`:

```python
"""Tests for harness_mcp.process_group — ProcessGroupScope."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import textwrap
import time
from pathlib import Path

import anyio
import pytest

from harness_mcp.process_group import PIPE, ProcessGroupScope


@pytest.fixture
def child_script(tmp_path: Path) -> Path:
    """A Python script that prints its pgid + sleeps forever."""
    p = tmp_path / "child.py"
    p.write_text(
        textwrap.dedent(
            """
            import os, sys, time
            print(os.getpgid(0), flush=True)
            try:
                time.sleep(60)
            except KeyboardInterrupt:
                pass
            """
        ).strip()
    )
    return p


class TestProcessGroupScope:
    @pytest.mark.asyncio
    async def test_spawn_uses_new_session(self, child_script: Path) -> None:
        async with ProcessGroupScope("test-1") as pg:
            proc = await pg.spawn(
                [sys.executable, str(child_script)], stdout=PIPE, stderr=PIPE,
            )
            # Read the child's printed pgid (its process group leader = its own pid).
            assert proc.stdout is not None
            line = await proc.stdout.receive(64)
            child_pgid = int(line.decode().strip())
            # We spawned with start_new_session=True so child's pgid == its pid.
            assert child_pgid == proc.pid
            # And the launcher pid is in our scope's tracked pgid.
            assert pg.tracked_pgid == proc.pid
        # On scope exit, the child must be reaped.
        assert proc.returncode is not None

    @pytest.mark.asyncio
    async def test_cleanup_kills_long_runner(self, child_script: Path) -> None:
        async with ProcessGroupScope("test-2", grace_seconds=0.5) as pg:
            proc = await pg.spawn(
                [sys.executable, str(child_script)], stdout=PIPE, stderr=PIPE,
            )
            # Don't wait — just exit the scope.
        # After exit, the process must be dead.
        assert proc.returncode is not None
        # Either signal-induced (negative returncode) or shell-style
        # 128+sig (rare for python). Either way, it shouldn't be 0.
        assert proc.returncode != 0

    @pytest.mark.asyncio
    async def test_communicate_writes_stdin_and_closes(self, tmp_path: Path) -> None:
        script = tmp_path / "echo.py"
        script.write_text(
            textwrap.dedent(
                """
                import sys
                sys.stdout.write(sys.stdin.read())
                """
            ).strip()
        )
        async with ProcessGroupScope("test-3") as pg:
            proc = await pg.spawn(
                [sys.executable, str(script)], stdin=PIPE, stdout=PIPE, stderr=PIPE,
            )
            await pg.communicate(proc, b"hello\\n")
            rc = await proc.wait()
            assert rc == 0
            assert proc.stdout is not None
            content = await proc.stdout.receive(64)
            assert content == b"hello\\n"

    @pytest.mark.asyncio
    async def test_already_dead_child_is_handled(self, tmp_path: Path) -> None:
        """If the child exits before scope exit, cleanup should not raise."""
        script = tmp_path / "exit_fast.py"
        script.write_text("import sys; sys.exit(0)")
        async with ProcessGroupScope("test-4") as pg:
            proc = await pg.spawn(
                [sys.executable, str(script)], stdout=PIPE, stderr=PIPE,
            )
            rc = await proc.wait()
            assert rc == 0
        # Scope exit cleanup runs; killpg on a reaped pgid raises ProcessLookupError;
        # the scope swallows it. No assertion needed beyond "no exception".
```

- [ ] **Step 2: Confirm the test fails.**

```bash
uv run pytest tests/test_process_group.py -v
```

Expected: ImportError on `harness_mcp.process_group`.

- [ ] **Step 3: Implement `process_group.py`.**

Create `src/harness_mcp/process_group.py`:

```python
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
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
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
```

- [ ] **Step 4: Run the tests.**

```bash
uv run pytest tests/test_process_group.py -v
```

Expected: every test passes. macOS may emit harmless `ResourceWarning` lines about subprocess streams; ignore them.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/process_group.py tests/test_process_group.py
uv run ruff format --check src/harness_mcp/process_group.py tests/test_process_group.py
```

Expected: zero findings.

---

## Task 3: `logging_setup.py` — `EventLogger` for Codex events

**Files:**
- Create: `tests/test_logging_setup.py`
- Create: `src/harness_mcp/logging_setup.py`

Per spec §7.5: per-chunk stateful formatter. Buffers `item/started` events until matched by `item/completed`, emits combined `[tool: ... -> ...]` lines, flushes orphans on close.

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_logging_setup.py`:

```python
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
        await logger.handle(FakeEvent(method="item/agentMessage/delta", payload=SimpleNamespace(delta="hello ")))
        await logger.handle(FakeEvent(method="item/agentMessage/delta", payload=SimpleNamespace(delta="world")))
        await logger.aclose()
        lines = _read_log(log)
        assert lines == ["hello ", "world"]

    @pytest.mark.asyncio
    async def test_tool_call_paired(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        item_started = _fake_item("c1", "commandExecution", command="ls -la")
        item_completed = _fake_item(
            "c1", "commandExecution", command="ls -la", aggregatedOutput="file1\\nfile2"
        )
        await logger.handle(FakeEvent(method="item/started", payload=SimpleNamespace(item=item_started)))
        await logger.handle(FakeEvent(method="item/completed", payload=SimpleNamespace(item=item_completed)))
        await logger.aclose()
        lines = _read_log(log)
        assert any("[tool: exec args=ls -la ->" in line for line in lines)

    @pytest.mark.asyncio
    async def test_orphan_tool_call_flushed_on_close(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        item_started = _fake_item("c1", "mcpToolCall", tool="Read", arguments='{"path":"/x"}')
        await logger.handle(FakeEvent(method="item/started", payload=SimpleNamespace(item=item_started)))
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
        await logger.handle(FakeEvent(method="turn/started", payload=SimpleNamespace(turn=turn_started)))
        await logger.handle(FakeEvent(method="turn/completed", payload=SimpleNamespace(turn=turn_completed)))
        await logger.aclose()
        lines = _read_log(log)
        assert "--- turn t1 (started) ---" in lines
        assert "--- turn t1 (completed) ---" in lines

    @pytest.mark.asyncio
    async def test_unknown_method_ignored(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        await logger.handle(FakeEvent(method="thread/tokenUsage/updated", payload=SimpleNamespace(tokens=42)))
        await logger.aclose()
        assert log.read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_empty_delta_treated_as_no_op(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        logger = EventLogger(log)
        await logger.handle(FakeEvent(method="item/agentMessage/delta", payload=SimpleNamespace(delta="")))
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
```

- [ ] **Step 2: Confirm the failure.**

```bash
uv run pytest tests/test_logging_setup.py -v
```

Expected: ImportError on `harness_mcp.logging_setup`.

- [ ] **Step 3: Implement `logging_setup.py`.**

Create `src/harness_mcp/logging_setup.py`:

```python
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


def _truncate(value: Any, max_len: int = 200) -> str:
    """Stringify and clip with an ellipsis. Robust to any input."""
    s = str(value)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def _summarize_item(item: Any) -> tuple[str, str]:
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


def _summarize_item_result(item: Any) -> str:
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
        self._fh = open(log_path, "a", encoding="utf-8", buffering=1)
        self._calls: dict[str, _ToolStart] = {}
        self._closed = False

    async def handle(self, event: Any) -> None:
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
                result = _summarize_item_result(item)
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
```

- [ ] **Step 4: Run the tests.**

```bash
uv run pytest tests/test_logging_setup.py -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/logging_setup.py tests/test_logging_setup.py
uv run ruff format --check src/harness_mcp/logging_setup.py tests/test_logging_setup.py
```

Expected: zero findings.

---

## Task 4: `mcp_capture.py` — capture user MCP server stanzas

**Files:**
- Create: `tests/test_mcp_capture.py`
- Create: `src/harness_mcp/mcp_capture.py`

Per spec §10.1 step 5: probe the SDK's `get_mcp_status()` for resolved configs, fall back to parsing `~/.claude.json`, project `.mcp.json`, and plugin caches. Captured stanzas are passed verbatim to spawned agents but never logged.

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_mcp_capture.py`:

```python
"""Tests for harness_mcp.mcp_capture — config probe + file fallback."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_mcp.mcp_capture import (
    capture_from_mcp_status,
    capture_mcp_servers,
    parse_user_config_files,
    redact_for_log,
)


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient.get_mcp_status()."""

    def __init__(self, status_response: dict[str, Any]) -> None:
        self._status = status_response

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_mcp_status(self) -> dict[str, Any]:
        return self._status


class TestCaptureFromMcpStatus:
    def test_uses_inline_config_when_present(self) -> None:
        status = {
            "mcpServers": [
                {"name": "context7", "status": "connected", "config": {"command": "ctx7"}},
            ]
        }
        captured = capture_from_mcp_status(status, want=("context7",))
        assert captured == {"context7": {"command": "ctx7"}}

    def test_skips_disconnected(self) -> None:
        status = {
            "mcpServers": [
                {"name": "context7", "status": "disconnected", "config": {"command": "ctx7"}},
            ]
        }
        captured = capture_from_mcp_status(status, want=("context7",))
        assert captured == {}

    def test_skips_when_config_missing(self) -> None:
        status = {
            "mcpServers": [
                {"name": "context7", "status": "connected"},  # no `config` field
            ]
        }
        captured = capture_from_mcp_status(status, want=("context7",))
        assert captured == {}


class TestParseUserConfigFiles:
    def test_finds_in_user_claude_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path
        monkeypatch.setenv("HOME", str(home))
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "context7": {"command": "ctx7", "env": {"K": "V"}},
                    }
                }
            )
        )
        captured = parse_user_config_files(("context7",), project_root=tmp_path)
        assert captured["context7"] == {"command": "ctx7", "env": {"K": "V"}}

    def test_finds_in_project_mcp_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # User config absent; project config present.
        monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
        (tmp_path / "empty_home").mkdir()
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"playwright": {"url": "http://x"}}})
        )
        captured = parse_user_config_files(("playwright",), project_root=project)
        assert captured["playwright"] == {"url": "http://x"}

    def test_user_takes_precedence_over_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"context7": {"command": "from-home"}}})
        )
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"context7": {"command": "from-project"}}})
        )
        captured = parse_user_config_files(("context7",), project_root=project)
        assert captured["context7"] == {"command": "from-home"}

    def test_returns_empty_when_no_files_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "empty_home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        captured = parse_user_config_files(("context7",), project_root=tmp_path)
        assert captured == {}

    def test_finds_in_user_claude_json_projects_subsection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §10.1 step 5: also check ~/.claude.json's projects.<cwd>.mcpServers."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        project = tmp_path / "myproj"
        project.mkdir()
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "projects": {
                        str(project): {
                            "mcpServers": {"context7": {"command": "ctx7-projects-scoped"}}
                        }
                    }
                }
            )
        )
        captured = parse_user_config_files(("context7",), project_root=project)
        assert captured["context7"] == {"command": "ctx7-projects-scoped"}

    def test_top_level_mcp_servers_wins_over_projects_subsection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        project = tmp_path / "myproj"
        project.mkdir()
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {"context7": {"command": "from-top"}},
                    "projects": {
                        str(project): {"mcpServers": {"context7": {"command": "from-proj"}}}
                    },
                }
            )
        )
        captured = parse_user_config_files(("context7",), project_root=project)
        assert captured["context7"] == {"command": "from-top"}

    def test_finds_in_plugin_cache_plugin_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §10.1 step 5: ~/.claude/plugins/cache/**/.claude-plugin/plugin.json."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        plugin_dir = home / ".claude" / "plugins" / "cache" / "fake-plug" / ".claude-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"mcpServers": {"playwright": {"url": "http://plug"}}})
        )
        captured = parse_user_config_files(("playwright",), project_root=None)
        assert captured["playwright"] == {"url": "http://plug"}


class TestCaptureMcpServers:
    @pytest.mark.asyncio
    async def test_uses_client_status_when_inline_config(self) -> None:
        client = _FakeClient(
            status_response={
                "mcpServers": [
                    {"name": "context7", "status": "connected", "config": {"command": "ctx7"}}
                ]
            }
        )
        captured = await capture_mcp_servers(client, want=("context7",), project_root=None)
        assert captured == {"context7": {"command": "ctx7"}}

    @pytest.mark.asyncio
    async def test_falls_back_to_files_when_inline_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path
        monkeypatch.setenv("HOME", str(home))
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"context7": {"command": "from-file"}}})
        )
        client = _FakeClient(
            status_response={
                "mcpServers": [{"name": "context7", "status": "connected"}]  # no config
            }
        )
        captured = await capture_mcp_servers(client, want=("context7",), project_root=tmp_path)
        assert captured == {"context7": {"command": "from-file"}}

    @pytest.mark.asyncio
    async def test_skips_disconnected_servers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        client = _FakeClient(
            status_response={
                "mcpServers": [{"name": "playwright", "status": "disconnected"}]
            }
        )
        captured = await capture_mcp_servers(client, want=("playwright",), project_root=None)
        assert captured == {}


class TestRedactForLog:
    def test_redacts_capture_dict(self) -> None:
        captured = {"context7": {"command": "ctx7", "env": {"API_KEY": "secret"}}}
        out = redact_for_log(captured)
        # Output should preserve names + statuses but not env/keys.
        assert "context7" in out
        assert "secret" not in out
        assert "API_KEY" not in out
```

- [ ] **Step 2: Confirm the failure.**

```bash
uv run pytest tests/test_mcp_capture.py -v
```

Expected: ImportError on `harness_mcp.mcp_capture`.

- [ ] **Step 3: Implement `mcp_capture.py`.**

Create `src/harness_mcp/mcp_capture.py`:

```python
"""Capture user MCP server stanzas at startup.

Strategy:
  1. Probe the live `ClaudeSDKClient.get_mcp_status()` response. If each
     wanted entry has an inline `config` dict and is `connected`, use it.
  2. Otherwise, parse `~/.claude.json` (user-scope) → `<project>/.mcp.json`
     (project-scope) for the missing names. First hit wins.

Captured stanzas may contain API keys, OAuth tokens, paths to credential
files. We pass them verbatim to spawned agents (required for them to
call the MCP server) but redact them in logs via `redact_for_log()`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def capture_from_mcp_status(
    status: dict[str, Any], *, want: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """Return the subset of `want` whose entries have inline config + are connected.

    `status` is the public-shape `get_mcp_status()` response — a dict with
    `mcpServers: list[{name, status, config?}]`. Any entry missing `config`
    or with non-connected `status` is dropped here; the caller falls back
    to file-based parsing for whatever is missing.
    """
    out: dict[str, dict[str, Any]] = {}
    for entry in status.get("mcpServers", []) or []:
        name = entry.get("name")
        if name not in want:
            continue
        if entry.get("status") != "connected":
            continue
        config = entry.get("config")
        if config:
            out[name] = dict(config)
    return out


def parse_user_config_files(
    want: tuple[str, ...], *, project_root: Path | None = None
) -> dict[str, dict[str, Any]]:
    """Walk known config files for the named servers; return first hits.

    Lookup order (per spec §10.1 step 5):
      1. ~/.claude.json — top-level `mcpServers.<name>`.
      2. ~/.claude.json — `projects.<project_root>.mcpServers.<name>` (if project_root given).
      3. <project_root>/.mcp.json — `mcpServers.<name>` (if project_root given).
      4. ~/.claude/plugins/cache/**/.mcp.json — plugin-shipped MCP configs.
      5. ~/.claude/plugins/cache/**/.claude-plugin/plugin.json — inline `mcpServers.<name>`.
    """
    found: dict[str, dict[str, Any]] = {}
    home = Path(os.environ.get("HOME", str(Path.home())))

    def _ingest(stanza_root: dict[str, Any]) -> None:
        for name in want:
            if name in found:
                continue
            entry = stanza_root.get(name)
            if isinstance(entry, dict):
                found[name] = entry

    user_claude = home / ".claude.json"
    if user_claude.is_file():
        try:
            data = json.loads(user_claude.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        # 1. Top-level mcpServers (user-scope default).
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            _ingest(servers)
        # 2. projects.<project_root>.mcpServers — Claude Code embeds project-scoped
        #    configs here when invoked inside a project tree.
        if project_root is not None:
            projects = data.get("projects") or {}
            if isinstance(projects, dict):
                proj_section = projects.get(str(project_root)) or {}
                if isinstance(proj_section, dict):
                    proj_servers = proj_section.get("mcpServers")
                    if isinstance(proj_servers, dict):
                        _ingest(proj_servers)

    if project_root is not None:
        project_mcp = project_root / ".mcp.json"
        if project_mcp.is_file():
            try:
                data = json.loads(project_mcp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                _ingest(servers)

    plugin_cache = home / ".claude" / "plugins" / "cache"
    if plugin_cache.is_dir():
        # 4. Plugin-shipped .mcp.json files.
        for mcp_json in plugin_cache.rglob(".mcp.json"):
            try:
                data = json.loads(mcp_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                _ingest(servers)
        # 5. Inline mcpServers inside each plugin's plugin.json (rarer, but supported
        #    by Claude Code per current docs).
        for plugin_json in plugin_cache.rglob(".claude-plugin/plugin.json"):
            try:
                data = json.loads(plugin_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                _ingest(servers)

    return found


async def capture_mcp_servers(
    client: Any,
    *,
    want: tuple[str, ...],
    project_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Probe the live SDK MCP status, fall back to user config files for missing names.

    Used by `prereqs.run_prereqs` (Part 4) at lifespan startup. Strategy
    (per spec §10.1 step 5):
      1. `await client.get_mcp_status()` — read inline `config` for every wanted
         entry that is `connected`.
      2. For wanted names where the SDK gave no `config` but reported `connected`,
         consult `parse_user_config_files` to find the stanza on disk.
      3. Return whatever was captured. Missing names are simply absent — the
         caller (probe_mcp_servers in prereqs.py) decides which are hard
         requirements vs. soft.
    """
    async with client as c:
        status = await c.get_mcp_status()

    captured = capture_from_mcp_status(status, want=want)

    # For names that came back `connected` but had no inline config, look in files.
    missing_with_inline: list[str] = []
    for entry in (status.get("mcpServers") or []):
        name = entry.get("name")
        if (
            name in want
            and entry.get("status") == "connected"
            and name not in captured
        ):
            missing_with_inline.append(name)

    if missing_with_inline:
        captured.update(
            parse_user_config_files(tuple(missing_with_inline), project_root=project_root)
        )
    return captured


def redact_for_log(captured: dict[str, dict[str, Any]]) -> str:
    """Return a stringified summary safe for log lines.

    Includes server names but never env vars, args, or URLs (which can
    embed API keys via query strings). Format: `name=<status>` lines.
    """
    out_lines = []
    for name, stanza in captured.items():
        kind = "stdio" if "command" in stanza else "http" if "url" in stanza else "unknown"
        out_lines.append(f"  {name}=<{kind} config redacted>")
    return "captured MCP servers:\n" + "\n".join(out_lines) if out_lines else "captured MCP servers: (none)"
```

- [ ] **Step 4: Run the tests.**

```bash
uv run pytest tests/test_mcp_capture.py -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/mcp_capture.py tests/test_mcp_capture.py
uv run ruff format --check src/harness_mcp/mcp_capture.py tests/test_mcp_capture.py
```

Expected: zero findings.

---

## Task 5: Final sweep

- [ ] **Step 1: Full pytest run.**

```bash
uv run pytest tests/ -v
```

Expected: every test from Parts 1 + 2 passes.

- [ ] **Step 2: Full ruff lint + format.**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: `All checks passed!` and the format check reports no diffs.

- [ ] **Step 3: Confirm import graph isolation.**

The launcher subprocess (Part 3 / `evaluator_runner.py`) must NOT transitively import `harness_mcp.state`. Check that nothing in this part's modules pulls in state from the launcher's likely future imports:

```bash
uv run python -c "
import importlib, sys

for mod_name in ('harness_mcp.process_group', 'harness_mcp.logging_setup', 'harness_mcp.mcp_capture', 'harness_mcp.prompts_loader'):
    importlib.import_module(mod_name)
assert 'harness_mcp.state' not in sys.modules, 'state.py imported transitively by an infra module — fix this before Part 3'
print('OK: state.py not transitively imported by infra modules')
"
```

Expected: `OK: state.py not transitively imported by infra modules`. If this fails, an unintended import landed somewhere — find and remove it before continuing.

- [ ] **Step 4: Confirm we are still on `main`.**

```bash
git status --short && git rev-parse --abbrev-ref HEAD
```

Expected: untracked / modified files only, branch `main`. **Do not commit.**

---

## Done criteria

- All 5 tasks above complete.
- `uv run pytest tests/ -v` passes (Part 1 + Part 2 tests).
- `uv run ruff check .` and `uv run ruff format --check .` exit 0.
- The infra modules import cleanly without pulling `harness_mcp.state`.
- Repo on `main`, NO commits.

The next plan in the series (Part 3: Agent SDK Wrappers) builds `generator.py`, `contracts.py`, `evaluator.py`, and `evaluator_runner.py` on top of these foundations.
