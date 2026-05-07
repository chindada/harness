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
    """Open (or reuse) the module-global writer connection and ensure schema.

    Idempotent — safe to call on every server boot. Creates `~/.harness/`
    if missing.
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
