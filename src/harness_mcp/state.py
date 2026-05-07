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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from anyio import to_thread
from ulid import ULID

from harness_mcp.config import harness_home, now_ms, state_db_path

# ----- Constants -----

JOB_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "completed", "failed", "cancelled", "interrupted"}
)
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
SPRINT_STATUSES: frozenset[str] = frozenset({"pending", "running", "passed", "failed", "cancelled"})

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
    """Open the module-global writer connection and apply the schema.

    Design:
        Per spec §4.2 the harness uses a single writer connection
        opened once at lifespan startup, with `check_same_thread=False`
        so `anyio.to_thread.run_sync` can dispatch from any worker
        thread. Reads use per-coroutine connections (cheap with WAL).
        Idempotent so it can be called from the doctor CLI as well as
        the server lifespan without duplicating effort.

    Implementation:
        Ensures `~/.harness/` and `~/.harness/jobs/` exist (mkdir is
        idempotent), opens the writer if not yet open, applies the
        documented PRAGMAs (WAL, synchronous=NORMAL, busy_timeout=5000,
        foreign_keys=ON), then runs the `CREATE TABLE IF NOT EXISTS`
        schema and commits.

    Example:
        >>> init_db()  # called by lifespan; safe to call repeatedly
    """
    global _writer_conn  # noqa: PLW0603 — module-global writer is by design (one writer)
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
    global _writer_conn  # noqa: PLW0603 — module-global writer is by design
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
    """Serialize one write through the module-global writer connection.

    Design:
        Per spec §4.2 every write goes through the single writer
        connection. We hold `_writer_lock` for the entire trip so
        SQLite's busy-timeout retry doesn't have to fight in-process
        contention; the lock is async so we don't block the event
        loop while waiting our turn. The write runs on a worker
        thread via `anyio.to_thread.run_sync` — the C-level
        `sqlite3.Connection.execute` blocks the calling thread.

    Implementation:
        `async with _writer_lock` then `to_thread.run_sync` of
        `_exec_commit` which `execute(stmt, params)` + `commit()` in
        one trip. No retry on top: SQLite's `busy_timeout=5000`
        handles concurrent readers cleanly under WAL.

    Example:
        >>> await db_write("UPDATE jobs SET updated_at=? WHERE id=?", (1, "J"))
    """
    async with _writer_lock:
        await to_thread.run_sync(_exec_commit, stmt, params)


async def db_write_returning_rowcount(stmt: str, params: tuple) -> int:
    """Like `db_write`, but returns the affected row count for CAS use.

    Design:
        Per spec §3.2 the orchestrator's pending→running transition is
        a compare-and-swap UPDATE: `... WHERE id=? AND status='pending'`.
        The caller uses the rowcount to detect the cancel-wins race
        (rc=0 means cancel_build flipped the row to 'cancelled' first).

    Implementation:
        Same lock + thread offload pattern as `db_write`; the only
        difference is `_exec_commit` returns `cursor.rowcount` and
        we propagate it.

    Example:
        >>> rc = await db_write_returning_rowcount(
        ...     "UPDATE jobs SET status='running' WHERE id=? AND status='pending'",
        ...     ("J",),
        ... )
        >>> 0  # cancel beat us
    """
    async with _writer_lock:
        return await to_thread.run_sync(_exec_commit, stmt, params)


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
    """Spec §10.1 step 6 — flip leftover `running` rows to `interrupted` at startup.

    Design:
        Workers die with the server (spec §2.1:66). Any row left at
        `status='running'` after a server restart is by definition
        orphaned — its orchestrator coroutine no longer exists. We
        terminalize it as `interrupted` (a distinct status from
        `cancelled` / `failed`) so callers can distinguish "the harness
        restarted under you" from "your job hit max_sprint_retries".

    Implementation:
        Single UPDATE under the writer lock. `finished_at` and
        `updated_at` are written in the same statement so the row
        is atomically terminal; `last_message` carries the exact
        operator-visible string `"server restarted before job could
        finish"` (spec line 191) — refactors must preserve it.

    Example:
        >>> await sweep_running_to_interrupted()  # called by run_prereqs at startup
    """
    msg = "server restarted before job could finish"
    await db_write(
        "UPDATE jobs SET status='interrupted', last_message=?, "
        "finished_at=?, updated_at=? WHERE status='running'",
        (msg, now_ms(), now_ms()),
    )
