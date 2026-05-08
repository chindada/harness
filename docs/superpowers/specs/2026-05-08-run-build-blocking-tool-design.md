# `run_build`: Blocking Build Tool with Progress Streaming

## Goal

Add a new MCP tool, `run_build`, that starts a build and blocks until it reaches a terminal status (`completed` / `failed` / `cancelled`), returning the same payload as `get_build_result`. While blocked, it streams MCP progress notifications on every phase transition so callers see live status without polling.

The existing `start_build`, `poll_build`, `get_build_result`, and `cancel_build` tools remain unchanged.

## Motivation

For the common interactive use case — "start a build and wait for it" — the current four-tool surface forces the caller into a polling loop on `poll_build`. `run_build` collapses that into a single call with progress notifications, while keeping `poll_build` available for the legitimate reattach-to-an-in-flight-job use case.

## Non-goals

- Removing `poll_build`. It remains useful for reattaching to an in-flight job from a different MCP session, debugging, or external monitoring.
- Solving MCP client per-tool-call timeouts. If a client times out before the build finishes, the orchestrator keeps running; the caller can `poll_build` / `get_build_result` against `job_id` (which is included in progress messages and the final return payload).
- Changing the orchestrator's existing terminal-status semantics or sprint state machine.

## Architecture

```
                        +--------------------+
                        |   run_build tool   |
                        |  (server.py)       |
                        +----------+---------+
                                   | (1) subscribe(job_id) BEFORE start
                                   v
+------------------+        +------+------+        +-------------------+
|   PhaseBroker    |<-------+  state.py   |        |   orchestrator    |
| (in-memory pub/  |        |  (lifespan) |        |   (run_job)       |
|  sub, per-job)   |        +-------------+        +---------+---------+
+--------+---------+                                          |
         ^                                                    | (3) DB write
         | (4) publish AFTER commit                           |     + publish
         +----------------------------------------------------+
```

1. `run_build` subscribes to the broker for the new `job_id` *before* scheduling the orchestrator coroutine — closes the missed-first-event race.
2. `run_build` calls `task_group.start_soon(run_job, ...)` (same as `start_build` today).
3. The orchestrator advances through phases. At each transition it writes the new `current_phase` (and on terminal, `status`) to the SQLite jobs row, commits, then publishes a phase event to the broker.
4. `run_build` consumes events as MCP progress notifications. On a terminal event, it queries `get_build_result(job_id)` and returns that payload (with `job_id` added).

## Components

### 1. `harness_mcp/phase_broker.py` (new)

```python
class PhaseBroker:
    """Per-job pub/sub for orchestrator phase transitions. Single-process, in-memory."""

    def __init__(self) -> None:
        self._streams: dict[str, list[MemoryObjectSendStream[dict]]] = {}

    def subscribe(self, job_id: str) -> MemoryObjectReceiveStream[dict]:
        """Return a bounded receive stream (size 8). If the job is already
        terminal in the DB, the returned stream is already-closed."""

    async def publish(self, job_id: str, event: dict) -> None:
        """Fan out to every subscriber. Bounded streams drop oldest under backpressure."""

    def close(self, job_id: str) -> None:
        """Close every subscriber stream for this job. Called by the orchestrator's
        terminal try/finally; subsequent subscribes return already-closed streams."""
```

- Lives in the lifespan-managed `_state` (already exists in `server.py:lifespan`) so it shares lifetime with the task group.
- Bounded stream size: **32**. Phase events are infrequent (seconds-to-minutes apart in real builds), 32 is comfortable headroom for any plausible burst.
- Backpressure policy: **drop-newest, terminal-protected.** `publish` calls `send_nowait`; on `WouldBlock`, the event is dropped. Callers can always reconcile via `poll_build` against the DB, which is authoritative. Terminal events are exempt: when `event["status"]` is terminal, `publish` falls back to `await stream.send(event)` so the subscriber is guaranteed to see the terminal event (and `broker.close` immediately follows).
- Already-closed-on-subscribe-after-terminal: queried via a single `SELECT status FROM jobs WHERE id=?` against the existing reader pool. Returns `MemoryObjectReceiveStream` whose paired send was closed before return — `async for` exits immediately.

### 2. Orchestrator publish points

The orchestrator currently writes `current_phase` / terminal `status` at 9 sites in `orchestrator.py:run_job`. We collapse the `UPDATE jobs SET current_phase=...` + `commit()` pattern into:

```python
async def _write_phase(
    conn,
    *,
    job_id: str,
    phase: str,
    status: str = "running",
    broker: PhaseBroker,
    sprints_completed: int | None = None,
) -> None:
    conn.execute("UPDATE jobs SET current_phase=?, status=?, updated_at=? WHERE id=?",
                 (phase, status, now_ms(), job_id))
    conn.commit()
    await broker.publish(job_id, {
        "current_phase": phase,
        "status": status,
        "sprints_completed": sprints_completed,
    })
```

- **Publish strictly after commit.** Subscribers may immediately query the DB on receipt; the row must reflect the new phase first.
- The 9 existing call sites become 9 `await _write_phase(...)` calls. No behavioural change beyond the publish.
- The orchestrator's outer `try/finally` calls `broker.close(job_id)` on terminal exit (any of `completed` / `failed` / `cancelled`).

### 3. `run_build` tool (`server.py`)

```python
@server.tool()
@_map_harness_errors
async def run_build(
    design_doc_path: str,
    options: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Start a build and block until terminal. Emits MCP progress notifications
    on every phase transition. Returns the same payload as get_build_result, plus job_id."""
```

**Sequence:**

1. Validate `design_doc_path` and parse options (shared helper `_kickoff_build` extracted from current `start_build`).
2. Insert pending jobs row → `job_id`.
3. `subscriber = state.broker.subscribe(job_id)` — *before* scheduling.
4. `state.task_group.start_soon(run_job, ...)` (same call site as `start_build`).
5. `await ctx.report_progress(0, None, f"starting (job_id={job_id})")` — gives the client immediate confirmation including the job id.
6. ```python
   async with subscriber:
       async for event in subscriber:
           await ctx.report_progress(
               progress=event.get("sprints_completed") or 0,
               total=None,  # total sprints unknown until planning completes
               message=f"{event['current_phase']} (status={event['status']})",
           )
           if event["status"] in ("completed", "failed", "cancelled"):
               break
   ```
7. Fetch `get_build_result(job_id)` payload, splice in `job_id`, return.

**`start_build` / `run_build` deduplication.** Steps 1–4 are identical to `start_build`. Extract into `_kickoff_build(design_doc_path, options) -> tuple[str, JobOptions]` so drift between the two tools is a compile-time problem, not a silent behavioural divergence.

### 4. Cancellation

If the MCP client cancels the in-flight `run_build` call, FastMCP propagates `CancelledError`. We handle it in a `try` / `except (anyio.get_cancelled_exc_class()):`:

```python
try:
    # ... step 6 above
except anyio.get_cancelled_exc_class():
    await cancel_job(job_id)  # same path used by cancel_build tool
    raise
```

The orchestrator's existing cancel scope handles teardown. The `async with subscriber:` ensures the subscription is unregistered from the broker even on cancellation.

## Return shape

Identical to `get_build_result`, with `job_id` added so the caller has a handle:

```json
{
  "job_id": "01HC...",
  "app_path": "/Users/.../app",
  "summary": "Built X with...",
  "final_status": "completed",
  "sprints": [{"seq": 1, "title": "...", "status": "passed", "retry_count": 0}],
  "plan_review_rounds": 0,
  "duration_seconds": 1234.5
}
```

## Data flow

1. Client → `run_build(design_doc_path, options)`
2. Server validates, inserts `pending` row, returns `job_id` to internal caller.
3. Server subscribes to broker for `job_id`.
4. Server schedules `run_job` on the task group.
5. Server emits initial `report_progress(0, None, "starting (job_id=...)")`.
6. Orchestrator advances; each phase write commits to DB and publishes to broker.
7. Server consumes events; emits one `report_progress` per event.
8. Orchestrator reaches terminal; publishes terminal event; orchestrator's outer `finally` calls `broker.close(job_id)`.
9. Server's `async for` loop sees the terminal event (or stream-end) and breaks.
10. Server queries `get_build_result(job_id)`, splices in `job_id`, returns to client.

## Error handling

- **Orchestrator crashes mid-build:** the `finally` in `run_job` writes terminal `status='failed'` with `error_text`, then calls `broker.close(job_id)`. `run_build` sees the terminal event, returns `get_build_result` payload (which includes `final_status='failed'`). No exception raised.
- **Slow subscriber, full queue:** `publish` drops the new non-terminal event. Caller's progress notifications may skip an intermediate phase, but the DB row remains authoritative and the terminal event is delivered via the awaited fallback path.
- **`get_build_result` raises after a clean terminal:** that's a real bug — let it propagate. The error decorator converts it to a structured MCP error.
- **Subscribe-after-terminal:** `subscribe` returns a closed stream; `run_build`'s `async for` exits immediately; we go straight to step 10.
- **Bad inputs:** same errors as `start_build` (`DESIGN_DOC_NOT_FOUND`, `INVALID_OPTIONS`).

## Testing

### Unit: `tests/test_phase_broker.py`

- `subscribe` then `publish` — subscriber receives event.
- Two subscribers — both receive (fan-out).
- Slow consumer (subscriber doesn't drain) — non-terminal `publish` drops the new event silently; subsequent terminal `publish` blocks (awaits) and is delivered.
- `close(job_id)` — every subscriber's `async for` exits via stream-end.
- `subscribe(job_id)` for an already-terminal-in-DB job — returns a closed stream (no live publish needed).

### Unit: `tests/test_orchestrator_phase_publish.py`

- Stub broker; drive a synthetic phase sequence through `_write_phase`; assert `publish` called once per phase, *after* commit, with the right payload.
- Terminal path: assert `publish` then `close` are both called.

### Integration: `tests/test_run_build.py`

- Happy path: stub orchestrator drives `planning → sprint-1/implementing → completed`. Assert `run_build` returns `get_build_result` payload with `job_id`, and `ctx.report_progress` called 4× (initial + 3 transitions).
- Failure path: terminal `failed`. `run_build` returns payload with `final_status="failed"`, no exception.
- Cancellation: cancel the `run_build` task; assert `cancel_job(job_id)` invoked, subscription closed, `CancelledError` re-raised.
- Late publish after subscriber drained terminal — no leak, no exception (broker.close already idempotent).

### No new end-to-end test

The existing `start_build` e2e exercises the orchestrator. `run_build` is a thin orchestration layer over the same machinery.

## Migration

None. `run_build` is purely additive. Existing callers of `start_build` / `poll_build` / `get_build_result` / `cancel_build` see no change.

## Open questions

None at this time.
