# `run_build` Blocking Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an MCP tool `run_build` that starts a build and blocks until terminal, streaming progress notifications via `Context.report_progress` on every phase transition. Returns the same payload as `get_build_result` plus `job_id`.

**Architecture:** A new in-memory `PhaseBroker` (per-job pub/sub) lives in the lifespan-managed `ServerState`. The orchestrator publishes phase events to the broker after each `current_phase` / terminal `status` DB write. `run_build` subscribes before scheduling the orchestrator, drains events into MCP progress notifications, and returns the `get_build_result` payload on terminal. `start_build` is unchanged in behaviour but shares a small `_kickoff_build` helper.

**Tech Stack:** Python 3.12, anyio (streams + cancel scopes), FastMCP (`Context.report_progress`), SQLite (existing).

**Spec:** `docs/superpowers/specs/2026-05-08-run-build-blocking-tool-design.md`

---

## File Structure

**Create:**
- `src/harness_mcp/phase_broker.py` — `PhaseBroker` class
- `tests/test_phase_broker.py` — broker unit tests
- `tests/test_run_build.py` — `run_build` integration tests

**Modify:**
- `src/harness_mcp/server.py`:
  - Add `phase_broker: PhaseBroker` field to `ServerState` (line 225-231)
  - Construct broker in `lifespan` (line 264-280)
  - Extract `_kickoff_build` helper from existing `start_build` body
  - Refactor `start_build` to call `_kickoff_build` (line 286-344)
  - Add new `run_build` tool
- `src/harness_mcp/orchestrator.py`:
  - Add `_write_phase` helper that writes to DB then publishes to broker
  - Add `phase_broker: PhaseBroker` kwarg to `run_job` (line 160)
  - Replace the 9 existing `current_phase` / terminal-status `db_write` call sites with `_write_phase`
  - Add `broker.close(job_id)` to the outer `finally` (line 331-332)
- `tests/test_orchestrator.py`:
  - Update existing tests to pass a stub broker (most pass `phase_broker=None` won't work; pass a real `PhaseBroker()`)

---

## Task 1: PhaseBroker — basic subscribe + publish

**Files:**
- Create: `tests/test_phase_broker.py`
- Create: `src/harness_mcp/phase_broker.py`

- [ ] **Step 1: Write the failing test for basic subscribe + publish**

Create `tests/test_phase_broker.py`:

```python
"""Tests for PhaseBroker — per-job in-memory pub/sub for phase transitions."""

from __future__ import annotations

import anyio
import pytest

from harness_mcp.phase_broker import PhaseBroker


@pytest.mark.asyncio
async def test_subscribe_then_publish_delivers_event():
    broker = PhaseBroker()
    sub = broker.subscribe("J1")
    await broker.publish("J1", {"current_phase": "planning", "status": "running"})
    async with sub:
        event = await sub.receive()
    assert event == {"current_phase": "planning", "status": "running"}
```

(Note: the project's `pyproject.toml` sets `asyncio_mode = "auto"` for pytest-asyncio, so `@pytest.mark.asyncio` works without any conftest setup.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_phase_broker.py::test_subscribe_then_publish_delivers_event -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'harness_mcp.phase_broker'`

- [ ] **Step 3: Write minimal `PhaseBroker` implementation**

Create `src/harness_mcp/phase_broker.py`:

```python
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

from harness_mcp.state import TERMINAL_JOB_STATUSES, open_reader

_BUFFER_SIZE = 32


class PhaseBroker:
    """Per-job pub/sub. One broker instance per harness-mcp lifespan."""

    def __init__(self) -> None:
        self._streams: dict[str, list[MemoryObjectSendStream[dict[str, Any]]]] = {}

    def subscribe(self, job_id: str) -> MemoryObjectReceiveStream[dict[str, Any]]:
        """Return a receive stream for this job_id's phase events.

        Caller is responsible for `async with` / closing the receive end.
        """
        send, recv = anyio.create_memory_object_stream[dict[str, Any]](_BUFFER_SIZE)
        self._streams.setdefault(job_id, []).append(send)
        return recv

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:
        """Fan out event to every subscriber for job_id.

        Drop-newest on backpressure for non-terminal events; awaited send
        for terminal events.
        """
        is_terminal = event.get("status") in TERMINAL_JOB_STATUSES
        for send in list(self._streams.get(job_id, ())):
            if is_terminal:
                await send.send(event)
            else:
                try:
                    send.send_nowait(event)
                except anyio.WouldBlock:
                    pass

    def close(self, job_id: str) -> None:
        """Close every subscriber stream for job_id. Idempotent."""
        for send in self._streams.pop(job_id, []):
            send.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_phase_broker.py::test_subscribe_then_publish_delivers_event -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/harness_mcp/phase_broker.py tests/test_phase_broker.py
git commit -m "feat(phase_broker): add PhaseBroker for per-job phase pub/sub"
```

---

## Task 2: PhaseBroker — fan-out + close

**Files:**
- Modify: `tests/test_phase_broker.py`

- [ ] **Step 1: Write failing test for fan-out**

Append to `tests/test_phase_broker.py`:

```python
@pytest.mark.asyncio
async def test_two_subscribers_both_receive():
    broker = PhaseBroker()
    sub_a = broker.subscribe("J1")
    sub_b = broker.subscribe("J1")
    await broker.publish("J1", {"current_phase": "planning", "status": "running"})
    async with sub_a, sub_b:
        evt_a = await sub_a.receive()
        evt_b = await sub_b.receive()
    assert evt_a == evt_b == {"current_phase": "planning", "status": "running"}
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_phase_broker.py::test_two_subscribers_both_receive -v`
Expected: PASS (the implementation already supports fan-out — this test confirms it)

- [ ] **Step 3: Write failing test for close**

Append to `tests/test_phase_broker.py`:

```python
@pytest.mark.asyncio
async def test_close_ends_subscriber_loop():
    broker = PhaseBroker()
    sub = broker.subscribe("J1")
    received: list[dict] = []

    async def consume():
        async with sub:
            async for event in sub:
                received.append(event)

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await anyio.sleep(0)  # let consumer start
        await broker.publish("J1", {"current_phase": "planning", "status": "running"})
        await anyio.sleep(0)  # let consumer drain
        broker.close("J1")

    assert received == [{"current_phase": "planning", "status": "running"}]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_phase_broker.py::test_close_ends_subscriber_loop -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_phase_broker.py
git commit -m "test(phase_broker): cover fan-out and close lifecycle"
```

---

## Task 3: PhaseBroker — backpressure (drop-newest, terminal-protected)

**Files:**
- Modify: `tests/test_phase_broker.py`

- [ ] **Step 1: Write failing test for non-terminal drop-newest**

Append to `tests/test_phase_broker.py`:

```python
@pytest.mark.asyncio
async def test_non_terminal_publish_drops_when_subscriber_full():
    broker = PhaseBroker()
    sub = broker.subscribe("J1")
    # Fill the buffer (32 slots) then publish one more — should silently drop.
    for i in range(32):
        await broker.publish("J1", {"current_phase": f"p{i}", "status": "running"})
    # 33rd publish must not raise and must not block.
    with anyio.fail_after(0.5):
        await broker.publish("J1", {"current_phase": "p32", "status": "running"})
    # Drain — we should see the first 32 (newest dropped == p32).
    drained: list[dict] = []
    async with sub:
        for _ in range(32):
            drained.append(await sub.receive())
    assert drained[-1]["current_phase"] == "p31"
    assert "p32" not in [e["current_phase"] for e in drained]
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_phase_broker.py::test_non_terminal_publish_drops_when_subscriber_full -v`
Expected: PASS (drop-newest already implemented)

- [ ] **Step 3: Write failing test for terminal blocking-send fallback**

Append to `tests/test_phase_broker.py`:

```python
@pytest.mark.asyncio
async def test_terminal_publish_blocks_until_delivered():
    broker = PhaseBroker()
    sub = broker.subscribe("J1")
    # Fill the buffer with non-terminal events.
    for i in range(32):
        await broker.publish("J1", {"current_phase": f"p{i}", "status": "running"})

    delivered_at = anyio.Event()

    async def publish_terminal():
        await broker.publish("J1", {"current_phase": "done", "status": "completed"})
        delivered_at.set()

    async def drain_one_then_let_terminal_in():
        await anyio.sleep(0.05)  # let publish_terminal start and block
        async with sub:
            # Drain the 32 buffered events — this frees a slot for terminal.
            for _ in range(32):
                await sub.receive()
            # Now receive the terminal.
            terminal = await sub.receive()
        assert terminal == {"current_phase": "done", "status": "completed"}

    async with anyio.create_task_group() as tg:
        tg.start_soon(publish_terminal)
        tg.start_soon(drain_one_then_let_terminal_in)

    assert delivered_at.is_set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_phase_broker.py::test_terminal_publish_blocks_until_delivered -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_phase_broker.py
git commit -m "test(phase_broker): cover drop-newest and terminal-blocking semantics"
```

---

## Task 4: PhaseBroker — subscribe-after-terminal returns closed stream

**Files:**
- Modify: `src/harness_mcp/phase_broker.py`
- Modify: `tests/test_phase_broker.py`
- Modify: `tests/conftest.py` (only if missing the `init_db` fixture; check first)

- [ ] **Step 1: Inspect tests/conftest.py to see if there's an `init_db` fixture**

Run: `cat tests/conftest.py`
If a fixture that initializes a temp `~/.harness/state.db` already exists, reuse its name. Otherwise add the following helper inline to the test below.

- [ ] **Step 2: Write failing test for subscribe-after-terminal**

Append to `tests/test_phase_broker.py`:

```python
@pytest.mark.asyncio
async def test_subscribe_after_terminal_returns_closed_stream(tmp_path, monkeypatch):
    # Initialize a fresh harness state DB in a temp dir.
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))
    from harness_mcp import state as state_mod

    state_mod.close_db()  # drop any cached writer from a prior test
    state_mod.init_db()
    try:
        # Insert a terminal job row.
        await state_mod.db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("J-DONE", "completed", "done", "/tmp/x.md", "{}", 0, 0, 0),
        )
        broker = PhaseBroker()
        sub = broker.subscribe("J-DONE")
        # Stream should already be closed — receive raises EndOfStream immediately.
        with pytest.raises(anyio.EndOfStream):
            async with sub:
                await sub.receive()
    finally:
        state_mod.close_db()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_phase_broker.py::test_subscribe_after_terminal_returns_closed_stream -v`
Expected: FAIL — current `subscribe` returns an open stream regardless of DB state.

- [ ] **Step 4: Implement DB-aware subscribe**

Modify `src/harness_mcp/phase_broker.py`. Replace `subscribe` with:

```python
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
```

Add the helper at the bottom of the same file:

```python
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
```

(Why a fresh sqlite connection here instead of `open_reader`: `subscribe` is called from a synchronous context inside `run_build`'s setup, before any async event loop is required — keeping it sync avoids needing to make every caller `await` it.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_phase_broker.py::test_subscribe_after_terminal_returns_closed_stream -v`
Expected: PASS

- [ ] **Step 6: Run all phase_broker tests to make sure nothing regressed**

Run: `uv run pytest tests/test_phase_broker.py -v`
Expected: 5 passing

- [ ] **Step 7: Commit**

```bash
git add src/harness_mcp/phase_broker.py tests/test_phase_broker.py
git commit -m "feat(phase_broker): subscribe returns closed stream for terminal jobs"
```

---

## Task 5: Wire PhaseBroker into ServerState

**Files:**
- Modify: `src/harness_mcp/server.py:222-280`

- [ ] **Step 1: Update ServerState to hold the broker**

In `src/harness_mcp/server.py`, modify the `ServerState` dataclass and the lifespan. Replace lines 225-231:

```python
@dataclass
class ServerState:
    """Shared mutable across all tool calls — initialized in lifespan."""

    prereqs_result: PrereqsResult
    codex_bin: str
    task_group: TaskGroup
    phase_broker: PhaseBroker
```

- [ ] **Step 2: Add the import**

Near the top of `server.py`, add:

```python
from harness_mcp.phase_broker import PhaseBroker
```

- [ ] **Step 3: Construct the broker in lifespan**

Replace the lifespan body (lines 264-280) so `_state` is constructed with a broker:

```python
@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
    """Run startup prereqs; refuse the server on any failure."""
    global _state  # noqa: PLW0603
    codex_bin = os.environ.get("HARNESS_CODEX_BIN") or shutil.which("codex") or "codex"

    report = DoctorReport()
    prereqs_result = await run_prereqs(
        client_factory=_client_factory,
        project_root=None,
        report=report,
    )
    async with create_task_group() as tg:
        _state = ServerState(
            prereqs_result=prereqs_result,
            codex_bin=codex_bin,
            task_group=tg,
            phase_broker=PhaseBroker(),
        )
        try:
            yield
        finally:
            _state = None
```

- [ ] **Step 4: Run existing server tests to confirm no regression**

Run: `uv run pytest tests/test_server.py -v`
Expected: All existing tests still pass (any test that constructs `ServerState` directly will need updating — fix in next step if it fails).

- [ ] **Step 5: Fix any test that constructs ServerState directly**

If `tests/test_server.py` constructs `ServerState(...)` literally, add `phase_broker=PhaseBroker()`. Search:

Run: `grep -n "ServerState(" tests/test_server.py`

For each match, add the `phase_broker` kwarg. Re-run `uv run pytest tests/test_server.py -v`.

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harness_mcp/server.py tests/test_server.py
git commit -m "feat(server): construct PhaseBroker in lifespan, attach to ServerState"
```

---

## Task 6: Orchestrator — `_write_phase` helper threaded through `run_job`

**Files:**
- Modify: `src/harness_mcp/orchestrator.py`
- Modify: `tests/test_orchestrator.py` (update existing test call sites to pass `phase_broker`)

- [ ] **Step 1: Inspect existing orchestrator test setup**

Run: `grep -n "run_job(\|phase_broker" tests/test_orchestrator.py | head -20`

Note every place `run_job` is called — each one will need a `phase_broker=PhaseBroker()` kwarg added in step 5.

- [ ] **Step 2: Write a failing test that asserts phase events are published**

This adapts the same pattern used by `TestRunJobPlanPhaseFailed` (`test_orchestrator.py:250-294`): insert a pending row, monkeypatch `run_plan_phase` to raise `PlanPhaseFailed`, call `run_job` with the broker, then drain the subscriber.

Append to `tests/test_orchestrator.py`:

```python
class TestRunJobPhaseBroker:
    """run_job publishes a phase event for every current_phase / status transition."""

    @pytest.mark.asyncio
    async def test_publishes_planning_and_terminal_failed(
        self, db: Path, monkeypatch: pytest.MonkeyPatch, tmp_harness_home: Path
    ) -> None:
        from harness_mcp.phase_broker import PhaseBroker

        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J_BROKER", "pending", "init", "/x", "{}", 1, 1),
        )
        (tmp_harness_home / "jobs" / "J_BROKER").mkdir()

        async def fake_plan_phase(**_kw: Any) -> None:
            raise PlanPhaseFailed("synthetic failure", phase="planning")

        monkeypatch.setattr(_orch_module, "run_plan_phase", fake_plan_phase)

        broker = PhaseBroker()
        sub = broker.subscribe("J_BROKER")

        await run_job(
            job_id="J_BROKER",
            options=JobOptions(),
            prereqs_result=_empty_prereqs(),
            planner_options_factory=lambda **_kw: object(),
            reviewer_options_factory=lambda **_kw: object(),
            evaluator_options_factory=lambda **_kw: object(),
            summarizer_options_factory=lambda **_kw: object(),
            codex_bin="codex",
            phase_broker=broker,
        )

        received: list[dict] = []
        async with sub:
            async for event in sub:
                received.append(event)

        phases = [e["current_phase"] for e in received]
        statuses = [e["status"] for e in received]
        # CAS pending→running emits 'planning'; PlanPhaseFailed emits final 'failed'.
        assert phases[0] == "planning"
        assert statuses[0] == "running"
        assert statuses[-1] == "failed"
        assert phases[-1] == "planning"  # PlanPhaseFailed carries phase='planning'
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py::test_run_job_publishes_phase_events_via_broker -v`
Expected: FAIL — `run_job` doesn't accept `phase_broker` yet.

- [ ] **Step 4: Add the `_write_phase` helper to `orchestrator.py`**

Add at module top (after imports, before `_cancel_scopes`):

```python
async def _write_phase(
    *,
    job_id: str,
    phase: str,
    broker: PhaseBroker,
    sprints_completed: int | None = None,
    status: str = "running",
) -> None:
    """Update jobs.current_phase + commit, then publish to the broker.

    Status defaults to 'running' for normal phase transitions. Pass terminal
    statuses ('completed' / 'failed' / 'cancelled') to record terminal state;
    the broker will await-send terminal events so they're never dropped.

    Note: callers that need additional column writes (last_message,
    error_text, finished_at, plan_review_rounds) keep doing those as their
    own db_write calls — _write_phase only handles the phase/status pair.
    """
    await db_write(
        "UPDATE jobs SET current_phase=?, status=?, updated_at=? WHERE id=?",
        (phase, status, now_ms(), job_id),
    )
    await broker.publish(
        job_id,
        {
            "current_phase": phase,
            "status": status,
            "sprints_completed": sprints_completed,
        },
    )
```

Also add the import at the top:

```python
from harness_mcp.phase_broker import PhaseBroker
```

- [ ] **Step 5: Add `phase_broker` kwarg to `run_job` and replace phase-write call sites**

Modify `run_job` signature (line 160):

```python
async def run_job(
    *,
    job_id: str,
    options: JobOptions,
    prereqs_result: PrereqsResult,
    planner_options_factory: Callable[..., Any],
    reviewer_options_factory: Callable[..., Any],
    evaluator_options_factory: Callable[..., Any],
    summarizer_options_factory: Callable[..., Any],
    codex_bin: str,
    phase_broker: PhaseBroker,
) -> None:
```

Then replace the 9 phase-write sites listed below. For each, the *existing* `db_write` is replaced by `_write_phase` (which does the equivalent UPDATE plus a publish). Sites that write *additional* columns (last_message, error_text, finished_at, plan_review_rounds) keep those extra db_write calls; only the phase/status portion uses `_write_phase`.

**Site 1 — pending → running (line 183-187):** This is a CAS write that uses `db_write_returning_rowcount` and must stay as-is for the rowcount semantics. After it succeeds (rc != 0), publish manually:

```python
rc = await db_write_returning_rowcount(
    "UPDATE jobs SET status='running', current_phase='planning', updated_at=? "
    "WHERE id=? AND status='pending'",
    (now_ms(), job_id),
)
if rc == 0:
    return  # cancel beat us
await phase_broker.publish(
    job_id,
    {"current_phase": "planning", "status": "running", "sprints_completed": None},
)
```

**Site 2 — `_set_plan_phase` inner function (line 194-200):** Replace body to call `_write_phase`:

```python
async def _set_plan_phase(phase: str) -> None:
    await _write_phase(
        job_id=job_id, phase=phase, broker=phase_broker, sprints_completed=None
    )
```

**Site 3 — plan_review_rounds write (line 213-216):** This writes a single column (no phase change) — leave as-is, no broker publish needed.

**Site 4 — sprint enter, contract phase (line 228-231):** Replace with:

```python
await _write_phase(
    job_id=job_id,
    phase=f"sprint-{seq}/contract",
    broker=phase_broker,
    sprints_completed=seq - 1,
)
```

**Site 5 — sprint row → 'running' (line 232-235):** This is a `sprints` table write, not a phase change — leave as-is.

**Site 6 — `_set_sprint_phase` inner function (line 237-242):** Replace body:

```python
async def _set_sprint_phase(phase: str) -> None:
    await _write_phase(
        job_id=job_id, phase=phase, broker=phase_broker, sprints_completed=seq - 1
    )
```

**Site 7 — sprint final state (line 264-269):** This is a `sprints` table write inside the shielded block — leave as-is.

**Site 8 — sprint failed → terminal (line 272-282):** Replace. Must keep error_text and finished_at writes; do them as one combined statement, then publish:

```python
await db_write(
    "UPDATE jobs SET status='failed', current_phase=?, error_text=?, "
    "finished_at=?, updated_at=? WHERE id=?",
    (
        f"sprint-{seq}/retry",
        result.error or "sprint_failed",
        now_ms(),
        now_ms(),
        job_id,
    ),
)
await phase_broker.publish(
    job_id,
    {
        "current_phase": f"sprint-{seq}/retry",
        "status": "failed",
        "sprints_completed": seq - 1,
    },
)
return
```

**Site 9 — summarizer phase (line 288-291):** Replace with:

```python
await _write_phase(
    job_id=job_id,
    phase="summarizing",
    broker=phase_broker,
    sprints_completed=len(sprints),
)
```

**Site 10 — completed terminal (line 295-301):** Keep the combined write (it includes last_message and finished_at); follow with a publish:

```python
await db_write(
    "UPDATE jobs SET status='completed', current_phase='done', last_message=?, "
    "finished_at=?, updated_at=? WHERE id=?",
    (summary, now_ms(), now_ms(), job_id),
)
await phase_broker.publish(
    job_id,
    {
        "current_phase": "done",
        "status": "completed",
        "sprints_completed": len(sprints),
    },
)
```

**Sites 11–13 — exception terminal writes (lines 311-322, 323-330, 305-309):** The cancellation, PlanPhaseFailed, and generic-exception paths all write a final terminal status under a shielded scope. Add a publish after each shielded write. Example for the `PlanPhaseFailed` path:

```python
except PlanPhaseFailed as e:
    with anyio.CancelScope(shield=True), anyio.move_on_after(15):
        await db_write(
            "UPDATE jobs SET status='failed', current_phase=?, error_text=?, "
            "finished_at=?, updated_at=? WHERE id=? AND status NOT IN "
            "('completed','failed','cancelled','interrupted')",
            (e.phase, e.error_text, now_ms(), now_ms(), job_id),
        )
        await phase_broker.publish(
            job_id,
            {"current_phase": e.phase, "status": "failed", "sprints_completed": None},
        )
```

For the cancellation path (line 302-310 in the original), the row's terminal `status` and `current_phase` were already written by `cancel_job` itself (`status='cancelled'`, no current_phase change). Publish a synthetic terminal event so subscribers see it:

```python
except anyio.get_cancelled_exc_class():
    with anyio.CancelScope(shield=True), anyio.move_on_after(15):
        await db_write(
            "UPDATE jobs SET updated_at=? WHERE id=? AND status NOT IN "
            "('completed','failed','cancelled','interrupted')",
            (now_ms(), job_id),
        )
        # Read the (already-written-by-cancel_job) status to publish it.
        async with open_reader() as r:
            row = r.execute(
                "SELECT current_phase, status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is not None:
            await phase_broker.publish(
                job_id,
                {
                    "current_phase": row[0],
                    "status": row[1],
                    "sprints_completed": None,
                },
            )
    raise
```

For the generic-exception path (line 323-330 in the original):

```python
except Exception as e:
    with anyio.CancelScope(shield=True), anyio.move_on_after(15):
        await db_write(
            "UPDATE jobs SET status='failed', error_text=?, finished_at=?, "
            "updated_at=? WHERE id=? AND status NOT IN "
            "('completed','failed','cancelled','interrupted')",
            (f"orchestrator_error: {e!r}", now_ms(), now_ms(), job_id),
        )
        # current_phase is whatever it was — read it back for the event.
        async with open_reader() as r:
            row = r.execute(
                "SELECT current_phase FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        await phase_broker.publish(
            job_id,
            {
                "current_phase": row[0] if row else "unknown",
                "status": "failed",
                "sprints_completed": None,
            },
        )
```

- [ ] **Step 6: Add `broker.close(job_id)` to outer `finally`**

Modify `run_job`'s outer `finally` (line 331-332):

```python
finally:
    await unregister_scope(job_id)
    phase_broker.close(job_id)
```

- [ ] **Step 7: Update existing `run_job` callers in tests**

Every existing test in `tests/test_orchestrator.py` that calls `run_job(...)` must add `phase_broker=PhaseBroker()`. Run:

Run: `grep -n "run_job(" tests/test_orchestrator.py`

For each match, add `phase_broker=PhaseBroker()` to the kwargs.

- [ ] **Step 8: Run the new test + the orchestrator suite**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: All tests including the new `test_run_job_publishes_phase_events_via_broker` PASS.

- [ ] **Step 9: Commit**

```bash
git add src/harness_mcp/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): publish phase events to PhaseBroker on every transition"
```

---

## Task 7: Update `start_build` caller — pass broker into `run_job`

**Files:**
- Modify: `src/harness_mcp/server.py:325-343`

- [ ] **Step 1: Pass `phase_broker` from `_state` into `run_job`**

In `start_build`, modify the `task_group.start_soon(...)` call to pass the broker:

```python
_state.task_group.start_soon(
    partial(
        run_job,
        job_id=job_id,
        options=job_options,
        prereqs_result=_state.prereqs_result,
        planner_options_factory=_make_planner_options_factory(
            _state.prereqs_result, job_dir=job_dir
        ),
        reviewer_options_factory=_make_reviewer_options_factory(
            _state.prereqs_result, job_dir=job_dir
        ),
        evaluator_options_factory=_make_evaluator_options_factory(
            _state.prereqs_result, job_dir=job_dir
        ),
        summarizer_options_factory=_make_summarizer_options_factory(_state.prereqs_result),
        codex_bin=_state.codex_bin,
        phase_broker=_state.phase_broker,
    )
)
```

- [ ] **Step 2: Run server tests**

Run: `uv run pytest tests/test_server.py -v`
Expected: All tests pass; `start_build` continues working unchanged from the caller's perspective.

- [ ] **Step 3: Commit**

```bash
git add src/harness_mcp/server.py
git commit -m "feat(server): wire PhaseBroker through start_build into run_job"
```

---

## Task 8: Extract `_kickoff_build` helper

**Files:**
- Modify: `src/harness_mcp/server.py`

- [ ] **Step 1: Extract `_kickoff_build` from `start_build`**

Add this helper above `start_build` (after the `_state` declaration, before `start_build`):

```python
async def _kickoff_build(
    design_doc_path: str, options: dict[str, Any] | None
) -> tuple[str, JobOptions]:
    """Validate inputs, insert pending jobs row, schedule run_job. Return (job_id, options).

    Shared between start_build (fire-and-forget) and run_build (blocking).
    """
    p = Path(design_doc_path)
    if not p.is_file() or p.stat().st_size == 0:  # noqa: ASYNC240
        raise DesignDocNotFoundError(design_doc_path)
    job_options = JobOptions.from_dict(options)

    job_id = await start_orchestrator_inserts_row(design_doc_path=p, options=job_options)

    assert _state is not None
    job_dir = jobs_root() / job_id
    _state.task_group.start_soon(
        partial(
            run_job,
            job_id=job_id,
            options=job_options,
            prereqs_result=_state.prereqs_result,
            planner_options_factory=_make_planner_options_factory(
                _state.prereqs_result, job_dir=job_dir
            ),
            reviewer_options_factory=_make_reviewer_options_factory(
                _state.prereqs_result, job_dir=job_dir
            ),
            evaluator_options_factory=_make_evaluator_options_factory(
                _state.prereqs_result, job_dir=job_dir
            ),
            summarizer_options_factory=_make_summarizer_options_factory(_state.prereqs_result),
            codex_bin=_state.codex_bin,
            phase_broker=_state.phase_broker,
        )
    )
    return job_id, job_options
```

- [ ] **Step 2: Replace the body of `start_build` to delegate**

```python
@server.tool()
@_map_harness_errors
async def start_build(
    design_doc_path: str, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Start a new build job. Returns immediately with the job_id.

    The orchestrator runs asynchronously; use poll_build / get_build_result
    to observe progress and outcome, or run_build for a blocking variant.
    """
    job_id, _ = await _kickoff_build(design_doc_path, options)
    return {"job_id": job_id}
```

- [ ] **Step 3: Run server tests**

Run: `uv run pytest tests/test_server.py -v`
Expected: PASS — start_build's externally observable behavior is unchanged.

- [ ] **Step 4: Commit**

```bash
git add src/harness_mcp/server.py
git commit -m "refactor(server): extract _kickoff_build helper from start_build"
```

---

## Task 9: `run_build` tool — happy path

**Files:**
- Create: `tests/test_run_build.py`
- Modify: `src/harness_mcp/server.py`

- [ ] **Step 1: Write the failing test for happy path**

Create `tests/test_run_build.py`:

```python
"""Integration tests for run_build — start + block + return get_build_result payload."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from harness_mcp.phase_broker import PhaseBroker


class FakeContext:
    """Captures Context.report_progress calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float | None, str | None]] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.calls.append((progress, total, message))


@pytest.mark.asyncio
async def test_run_build_returns_get_build_result_payload_on_completion(
    tmp_path, monkeypatch
):
    """Stub the orchestrator: emit planning → completed via the broker, assert
    run_build returns a get_build_result-shaped payload with job_id."""
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))

    from harness_mcp import server, state

    state.close_db()
    state.init_db()
    try:
        broker = PhaseBroker()

        async def stub_run_job(*, job_id: str, phase_broker: PhaseBroker, **kw: Any) -> None:
            # Simulate planning then complete.
            await state.db_write(
                "UPDATE jobs SET status='running', current_phase='planning', updated_at=? "
                "WHERE id=?",
                (0, job_id),
            )
            await phase_broker.publish(
                job_id,
                {"current_phase": "planning", "status": "running", "sprints_completed": 0},
            )
            await state.db_write(
                "UPDATE jobs SET status='completed', current_phase='done', "
                "last_message=?, finished_at=?, updated_at=? WHERE id=?",
                ("done", 100, 100, job_id),
            )
            await phase_broker.publish(
                job_id,
                {"current_phase": "done", "status": "completed", "sprints_completed": 0},
            )
            phase_broker.close(job_id)

        # Monkeypatch the run_job that _kickoff_build schedules.
        monkeypatch.setattr(server, "run_job", stub_run_job)

        # Provide a minimal _state so _kickoff_build works.
        async with anyio.create_task_group() as tg:
            from harness_mcp.prereqs import PrereqsResult

            server._state = server.ServerState(
                prereqs_result=PrereqsResult.__new__(PrereqsResult),  # not used by stub
                codex_bin="codex",
                task_group=tg,
                phase_broker=broker,
            )
            design = tmp_path / "design.md"
            design.write_text("design")

            ctx = FakeContext()
            result = await server.run_build(str(design), None, ctx=ctx)

            assert result["final_status"] == "completed"
            assert "job_id" in result
            assert result["job_id"].startswith("0")  # ULID prefix
            # At least one progress event for planning + initial + terminal.
            assert any("planning" in (m or "") for _, _, m in ctx.calls)
    finally:
        state.close_db()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_run_build.py::test_run_build_returns_get_build_result_payload_on_completion -v`
Expected: FAIL — `run_build` does not exist yet.

- [ ] **Step 3: Implement `run_build`**

In `src/harness_mcp/server.py`, add (after `start_build`, before `poll_build`):

```python
@server.tool()
@_map_harness_errors
async def run_build(
    design_doc_path: str,
    options: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Start a build and block until terminal. Streams progress notifications.

    Returns the same payload as get_build_result, plus job_id so the caller
    has a handle for poll_build / get_build_result if anything goes sideways.
    """
    assert _state is not None
    job_id, _ = await _kickoff_build(design_doc_path, options)
    subscriber = _state.phase_broker.subscribe(job_id)

    if ctx is not None:
        await ctx.report_progress(0, None, f"starting (job_id={job_id})")

    try:
        async with subscriber:
            async for event in subscriber:
                if ctx is not None:
                    await ctx.report_progress(
                        event.get("sprints_completed") or 0,
                        None,
                        f"{event['current_phase']} (status={event['status']})",
                    )
                if event["status"] in TERMINAL_JOB_STATUSES:
                    break
    except anyio.get_cancelled_exc_class():
        await cancel_job(job_id)
        raise

    result = await get_build_result(job_id)
    result["job_id"] = job_id
    return result
```

Add the `Context` import at the top of `server.py`:

```python
from mcp.server.fastmcp import Context, FastMCP
```

(Replace the existing `from mcp.server.fastmcp import FastMCP` line.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_run_build.py::test_run_build_returns_get_build_result_payload_on_completion -v`
Expected: PASS.

- [ ] **Step 5: Run the full server suite to confirm no regressions**

Run: `uv run pytest tests/test_server.py tests/test_run_build.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harness_mcp/server.py tests/test_run_build.py
git commit -m "feat(server): add run_build tool that blocks until terminal with progress streaming"
```

---

## Task 10: `run_build` — failure path

**Files:**
- Modify: `tests/test_run_build.py`

- [ ] **Step 1: Write failing test for failure path**

Append to `tests/test_run_build.py`:

```python
@pytest.mark.asyncio
async def test_run_build_returns_failed_payload_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))
    from harness_mcp import server, state

    state.close_db()
    state.init_db()
    try:
        broker = PhaseBroker()

        async def stub_run_job(*, job_id: str, phase_broker: PhaseBroker, **kw: Any) -> None:
            await state.db_write(
                "UPDATE jobs SET status='failed', current_phase='sprint-1/retry', "
                "error_text=?, finished_at=?, updated_at=? WHERE id=?",
                ("oops", 100, 100, job_id),
            )
            await phase_broker.publish(
                job_id,
                {
                    "current_phase": "sprint-1/retry",
                    "status": "failed",
                    "sprints_completed": 0,
                },
            )
            phase_broker.close(job_id)

        monkeypatch.setattr(server, "run_job", stub_run_job)

        async with anyio.create_task_group() as tg:
            from harness_mcp.prereqs import PrereqsResult

            server._state = server.ServerState(
                prereqs_result=PrereqsResult.__new__(PrereqsResult),
                codex_bin="codex",
                task_group=tg,
                phase_broker=broker,
            )
            design = tmp_path / "design.md"
            design.write_text("design")

            result = await server.run_build(str(design), None, ctx=FakeContext())
            assert result["final_status"] == "failed"
            assert "job_id" in result
    finally:
        state.close_db()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_run_build.py::test_run_build_returns_failed_payload_without_raising -v`
Expected: PASS — `run_build` returns the `get_build_result` payload regardless of terminal status.

- [ ] **Step 3: Commit**

```bash
git add tests/test_run_build.py
git commit -m "test(run_build): cover failed-terminal returns payload without raising"
```

---

## Task 11: `run_build` — cancellation path

**Files:**
- Modify: `tests/test_run_build.py`

- [ ] **Step 1: Write failing test for cancellation**

Append to `tests/test_run_build.py`:

```python
@pytest.mark.asyncio
async def test_run_build_cancellation_invokes_cancel_job(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))
    from harness_mcp import server, state

    state.close_db()
    state.init_db()
    try:
        broker = PhaseBroker()
        cancel_calls: list[str] = []

        async def stub_run_job(
            *, job_id: str, phase_broker: PhaseBroker, **kw: Any
        ) -> None:
            # Block forever — never publish terminal.
            await anyio.sleep_forever()

        async def stub_cancel_job(job_id: str) -> dict[str, Any]:
            cancel_calls.append(job_id)
            # Simulate cancel writing terminal row.
            await state.db_write(
                "UPDATE jobs SET status='cancelled', current_phase='cancelled', "
                "last_message=?, finished_at=?, updated_at=? WHERE id=?",
                ("cancelled", 100, 100, job_id),
            )
            return {"ok": True, "was_already_terminal": False}

        monkeypatch.setattr(server, "run_job", stub_run_job)
        monkeypatch.setattr(server, "cancel_job", stub_cancel_job)

        async with anyio.create_task_group() as tg:
            from harness_mcp.prereqs import PrereqsResult

            server._state = server.ServerState(
                prereqs_result=PrereqsResult.__new__(PrereqsResult),
                codex_bin="codex",
                task_group=tg,
                phase_broker=broker,
            )
            design = tmp_path / "design.md"
            design.write_text("design")

            cancelled = anyio.Event()

            async def run_and_capture():
                try:
                    await server.run_build(str(design), None, ctx=FakeContext())
                except BaseException:
                    cancelled.set()
                    raise

            with anyio.move_on_after(0.5) as scope:
                async with anyio.create_task_group() as inner:
                    inner.start_soon(run_and_capture)
                    await anyio.sleep(0.1)
                    inner.cancel_scope.cancel()
            assert scope.cancelled_caught
            assert len(cancel_calls) == 1
    finally:
        state.close_db()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_run_build.py::test_run_build_cancellation_invokes_cancel_job -v`
Expected: PASS — the `except` clause in `run_build` calls `cancel_job` before re-raising.

- [ ] **Step 3: Commit**

```bash
git add tests/test_run_build.py
git commit -m "test(run_build): cover client-cancel path invokes cancel_job"
```

---

## Task 12: Reinstall and smoke-test against the editable harness-mcp

**Files:** None (verification only)

- [ ] **Step 1: Confirm the editable install picks up the new code**

Run: `uv run python -c "from harness_mcp.server import run_build; print(run_build.__doc__[:80])"`
Expected: The first 80 characters of `run_build`'s docstring print successfully.

- [ ] **Step 2: Run the entire test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass (incl. the 3 new test files: `test_phase_broker.py`, `test_run_build.py`, plus updates to `test_orchestrator.py` and `test_server.py`).

- [ ] **Step 3: Restart the running harness-mcp MCP server (if any)**

Run: `pgrep -f "harness-mcp serve" && pkill -f "harness-mcp serve" || true`
Then in Claude Code: `/mcp` → reconnect harness-mcp.

- [ ] **Step 4: Smoke test `run_build` end-to-end**

From an MCP client, invoke `run_build("/Users/timhsu/dev_projects/harness/examples/todo-app-design.md")`. The call should:
- Return progress notifications during the build (visible in the client's progress UI).
- Block until terminal.
- Return `{job_id, app_path, summary, final_status, sprints, plan_review_rounds, duration_seconds}`.

Expected: a final result object with `final_status` set to `completed` or `failed`, plus `job_id`.

If the call is timed out by the MCP client before terminal, that's expected — confirm via `poll_build(job_id)` that the orchestrator continued. (This is a documented limitation of MCP client timeouts; the spec's non-goals call it out.)

- [ ] **Step 5: No commit** (this task is verification only)
