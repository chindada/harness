# Harness MCP Server — Design Spec

**Date:** 2026-05-07
**Status:** Approved (brainstorm complete; awaiting implementation plan)

## 0. References

Verified against current docs at design time via context7 MCP. Pin exact SDK versions in `pyproject.toml`; revisit this section when bumping.

- Anthropic, "Harness design for long-running application development" (2026-03). Source pattern.
- `claude_agent_sdk` (Python) — `query()`, `ClaudeSDKClient`, `ClaudeAgentOptions`. Library ID: `/anthropics/claude-agent-sdk-python`.
- `codex_app_server` (Python) — `AsyncCodex`, `AppServerConfig`, `TextInput`, `thread_start()`. Library ID: `/openai/codex`. Event model uses `event.method` (string) discriminator with payload accessed via `event.payload`.
- `mcp` Python SDK (FastMCP server side) — Library ID: `/modelcontextprotocol/python-sdk`. v1.12.4+.
- `superpowers:writing-plans` skill — bundled `plan-document-reviewer-prompt.md` template used for plan review.
- Playwright MCP — used by the Evaluator for UI-bearing dynamic verification.

Implementation plan **must** start by re-verifying every SDK reference here against the pinned versions; any drift fails the prereq check (§10.1.2b).

## 1. Overview

Build a Python MCP server (`harness-mcp`) that exposes a four-tool surface for orchestrating multi-hour, multi-agent application builds from a feature design document. The orchestration pattern is taken from Anthropic's "Harness design for long-running application development" (2026-03): a **Planner** writes an implementation plan, a **Reviewer** vets that plan, a **Generator** implements the plan one sprint (one feature) at a time, and an **Evaluator** verifies each sprint via static audit + dynamic verification with hard pass/fail criteria.

- Planner / Reviewer / Evaluator / Summarizer: **Claude Agent SDK** (`claude_agent_sdk`).
- Generator: **Codex Agent SDK** (`codex_app_server`) against a local `codex` binary.
- Skills: `superpowers:writing-plans` (Planner) and the `plan-document-reviewer-prompt.md` template (Reviewer).
- Visualization / browser interaction (Evaluator only, when relevant): Playwright MCP.
- Library docs: context7 MCP (Planner, Generator).

The design follows the article's load-bearing assertions:
- **Reset over compaction** for context anxiety. Compaction preserves the anxious feel; only full resets clear it. Internal to the Generator.
- **Separate evaluator** for grading. Self-evaluation is too lenient.
- **File-mediated handoffs** between agents (contract.md, eval.md, handoff-NNN.md) — auditable, restart-survivable.
- **Hard pass/fail criteria** per contract item. Any single fail = sprint fail.
- **Sprint contracts** negotiated *before* implementation prevent scope mismatch.

## 2. Architecture

```
Client (Claude Code, etc.)
   │  MCP (stdio | streamable-http)
   ▼
┌───────────────────────────────────────┐
│  harness-mcp server  (anyio asyncio)  │
│   • lifespan: startup prereq checks   │
│   • 4 tools (start/poll/get/cancel)   │
│   • per-job orchestrator coroutine    │
│   • state.db (SQLite, WAL)            │
└───────────────────────────────────────┘
   │ spawns (per phase, per sprint)
   ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ Planner (Claude) │  │ Reviewer (Claude)│  │ Summarizer       │
│   query()        │  │   query()        │  │   query()        │
└──────────────────┘  └──────────────────┘  └──────────────────┘
┌──────────────────────────────┐  ┌──────────────────────────────┐
│ Generator (Codex SDK)        │  │ Evaluator (Claude)           │
│   AsyncCodex+thread_start()  │  │   ClaudeSDKClient (multi-turn│
│   reset-and-handoff loop     │  │   for static + dynamic)      │
└──────────────────────────────┘  └──────────────────────────────┘
```

### 2.1 Process model

- One async task per job. We use `anyio` because the spec relies on `anyio.CancelScope`, `anyio.fail_after`, `anyio.move_on_after`, and `anyio.to_thread.run_sync` for cancellation and thread offload. Both `claude_agent_sdk` and `mcp` are runtime-agnostic (work with asyncio, trio, anyio), so anyio is compatible — not "required by" them, just the right choice for our cancellation patterns.
- Concurrency: **unbounded**. Each `start_build` returns immediately and spawns its own orchestrator coroutine.
- Workers tied to server lifetime. On server restart, any rows still marked `running` are flipped to `interrupted` and never auto-resumed.
- Transports selectable via CLI:
  - `harness-mcp serve --transport stdio` (default; standard MCP child-of-client integration)
  - `harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765` (daemon; jobs survive client disconnects)
- Diagnostic CLI: `harness-mcp doctor` runs the same lifespan prereq checks as the server but exits with a human-readable summary instead of starting the MCP loop.

### 2.2 Agent role summary

| Role | SDK | Lifecycle |
|---|---|---|
| Planner | Claude `query()` | One-shot per plan or revision |
| Reviewer | Claude `query()` | One-shot per review round |
| Generator | Codex `AsyncCodex` | Multiple `thread_start()` invocations per sprint (reset-and-handoff loop) |
| Evaluator | Claude `ClaudeSDKClient` | One client per sprint evaluation; two `query()` calls (static, dynamic) |
| Summarizer | Claude `query()` | One-shot at job end |

## 3. MCP Tool Surface

Four tools. The umbrella `build_application` name from the original brief is **not** exposed — the four explicit tools are the canonical surface.

| Tool | Args | Returns | Errors |
|---|---|---|---|
| `start_build` | `design_doc_path: str (abs)`, `options: dict` (optional, schema in §10.2) | `{job_id: str}` | `design_doc_not_found`, `invalid_options` |
| `poll_build` | `job_id: str` | `{status, current_phase, last_message, sprints_completed, plan_review_rounds, started_at, updated_at}` | `unknown_job` |
| `get_build_result` | `job_id: str` | `{app_path, summary, final_status, sprints: [{seq, title, status, retry_count}], plan_review_rounds, duration_seconds}` | `unknown_job`, `job_not_finished` |
| `cancel_build` | `job_id: str` | `{ok: bool, was_already_terminal: bool}` | `unknown_job` |

`sprints_completed` in `poll_build` = `COUNT(*) FROM sprints WHERE job_id=? AND status='passed'`. Counted from terminal "passed" only — failed/retrying sprints are not counted as completed. Recomputed at every poll.

### 3.1 Error handling convention

- **Structural errors** (argument shape, unknown job, job not finished): raise an exception inside the tool function. A thin error-mapping helper in `server.py` catches custom exceptions (`UnknownJobError`, `JobNotFinishedError`, `DesignDocNotFoundError`, `InvalidOptionsError`) and converts them to `mcp.types.CallToolResult(is_error=True, content=[TextContent(...)], structured_content={"code": "<UPPER_SNAKE>", "message": "..."})`. Field naming uses snake_case per `mcp` v2 server-side construction conventions (the Pydantic model may also accept camelCase aliases on read, but snake_case is the documented v2 write form). Callers branch on `structured_content.code` instead of regex-matching the error string. Custom exception classes subclass a private `HarnessToolError` base.
- **Idempotency-and-status fields**: encoded in the success JSON return. `was_already_terminal` on `cancel_build` is a status field, not an error.

Examples:
- `get_build_result` on a `running` job → raises `JobNotFinishedError`, mapper emits `structured_content.code = "JOB_NOT_FINISHED"`. Caller must `poll_build` first.
- `cancel_build` on an already-terminal job → returns `{ok: true, was_already_terminal: true}` (idempotent, no error).

### 3.2 `cancel_build` execution semantics

The orchestrator maintains an in-memory `_cancel_scopes: dict[str, anyio.CancelScope]` (a module-level dict in `orchestrator.py`, guarded by an `anyio.Lock`).

**Registry lifecycle.** `start_build` (a) inserts the row with `status='pending'`, (b) launches the orchestrator coroutine via the server's task group, and (c) returns `job_id` immediately. The coroutine's first action is to register its outer scope:

```python
async def run_job(job_id: str) -> None:
    with anyio.CancelScope() as scope:
        async with _scopes_lock: _cancel_scopes[job_id] = scope
        try:
            # Compare-and-swap: only flip pending -> running if still pending.
            # If cancel_build's pending-branch (§3.2 step 1, pending case) already
            # set status='cancelled', the rowcount is 0 and we exit cleanly.
            rowcount = await db_write_returning_rowcount(
                "UPDATE jobs SET status='running', current_phase='planning', "
                "updated_at=? WHERE id=? AND status='pending'",
                (now_ms(), job_id),
            )
            if rowcount == 0:
                return     # already terminalized by cancel_build
            ...
        finally:
            async with _scopes_lock: _cancel_scopes.pop(job_id, None)
```

The CAS makes the `pending → cancelled` race window safe regardless of interleaving — `cancel_build` writes `'cancelled'` unconditionally; the orchestrator's CAS then either fires (we won) or no-ops (cancel won). No `BEGIN IMMEDIATE` ceremony needed because the single-row UPDATE with a status-equality clause is atomic at the SQLite level.

`cancel_build(job_id)`:

1. Look up the row in `jobs`.
   - Not found → raise `UnknownJobError`.
   - Status terminal (`completed`/`failed`/`cancelled`/`interrupted`) → return `{ok: true, was_already_terminal: true}`.
   - Status `pending` (rare; see §4.3) → mark `cancelled` directly, no scope to cancel yet. The orchestrator coroutine's first action is the CAS UPDATE in §3.2's pseudocode below; if the row is already `cancelled`, the WHERE clause matches zero rows and the coroutine returns immediately. Return `{ok: true, was_already_terminal: false}`.
   - Status `running`:
     a. Set `jobs.status='cancelled'`, `last_message='cancelled by user'`, `finished_at=now`. Commit before signaling so the orchestrator sees the cancellation when it next reads.
     b. Look up the scope in the registry. Call `scope.cancel()`. anyio propagates the cancel through every nested task: the active `query()` / `AsyncCodex` / `ClaudeSDKClient` raises `CancelledError`; their `__aexit__` runs; SDK subprocesses receive interrupts.
     c. The cancel propagates into `stage_3_evaluation`'s `async with ProcessGroupScope(...)`; the scope's `__aexit__` SIGTERM/SIGKILLs the launcher pgroup (§8.4). The orchestrator's `try/finally` (§6.5) only bounds the unwind and writes the final sprint row.
     d. Return `{ok: true, was_already_terminal: false}` immediately. Cleanup completes asynchronously.
2. The orchestrator's `try/except CancelledError` block in `orchestrator.py` ensures the row stays in `cancelled` and the scope is unregistered. If `CancelledError` arrives while we're already mid-write, the WAL write is durable; the next state lookup is consistent.

`cancel_build` is idempotent: a second call on a now-`cancelled` job hits the terminal branch and returns `was_already_terminal=true`.

## 4. Storage & State Machine

### 4.1 Filesystem layout

All under `~/.harness/`, resolved to absolute at startup.

```
~/.harness/
  state.db
  jobs/<job_id>/                  # job_id = ULID (sortable, URL-safe, 26 chars)
    design.md                     # verbatim copy of the input
    plan.md                       # final, post-review
    plan-history/
      plan-v1.md ... plan-vN.md
      review-v1.md ... review-vN.md
    sprint-N/
      contract.md                 # round-by-round negotiation, ends with mutual APPROVED
      handoff-NNN.md              # Generator's reset handoffs (zero-padded for sortability)
      eval.md                     # ## Static audit + ## Dynamic verification
      log.txt                     # plain-text streamed agent output (per-line flushed)
    app/                          # git-init'd at job start, tagged at each sprint (see §6.4)
      .gitignore                  # INSIDE app/ at job start; ignores .codex/, node_modules/, *.pyc, .venv/, .env
    summary.md                    # final LLM-generated summary
```

`app/` is `git init`'d at job start because (a) Codex's Python `thread_start()` does **not** expose a `skip_git_repo_check` flag (the TS SDK has `skipGitRepoCheck`; the documented Python params are `approval_policy, base_instructions, config, cwd, developer_instructions, ephemeral, model, model_provider, personality, sandbox`) — so a real repo is required, and (b) sprint-tag diffs (§6.3) need git history. The seeded `.gitignore` lives inside `app/` (the git repo root) and keeps `git add .` (§7.4) from sucking in stray Codex artifacts.

### 4.2 SQLite schema (WAL mode)

```sql
CREATE TABLE jobs (
  id                  TEXT PRIMARY KEY,           -- ULID
  status              TEXT NOT NULL,              -- pending|running|completed|failed|cancelled|interrupted
  current_phase       TEXT NOT NULL,              -- enum (see §4.4)
  design_path         TEXT NOT NULL,              -- absolute, original
  options_json        TEXT NOT NULL,
  last_message        TEXT,                       -- short status string for poll
  error_text          TEXT,                       -- non-null when status=failed
  plan_review_rounds  INTEGER NOT NULL DEFAULT 0,
  started_at          INTEGER NOT NULL,           -- epoch ms
  updated_at          INTEGER NOT NULL,
  finished_at         INTEGER                     -- nullable
);

CREATE TABLE sprints (
  job_id          TEXT NOT NULL,
  seq             INTEGER NOT NULL,               -- 1-based
  title           TEXT NOT NULL,
  status          TEXT NOT NULL,                  -- pending|running|passed|failed|cancelled
  retry_count     INTEGER NOT NULL DEFAULT 0,
  started_at      INTEGER,
  finished_at     INTEGER,
  PRIMARY KEY (job_id, seq),
  FOREIGN KEY (job_id) REFERENCES jobs(id)
);
```

No events table. Per-sprint `log.txt` is the event log; SQLite is for state lookups only.

**Connection strategy** (concurrency-safe). The `sqlite3` C calls are blocking; calling them from async coroutines without offload would block the event loop and defeat unbounded job concurrency. We use:

- **One writer connection** opened with `sqlite3.connect(path, check_same_thread=False)` so it can be used across thread-pool workers. All writer access goes through a single `anyio.Lock`. Every call wraps in `await anyio.to_thread.run_sync(...)`.
- **Per-coroutine reader connections**: also opened with `check_same_thread=False`, but each reader is a fresh connection used by exactly one coroutine for the lifetime of one read; `to_thread.run_sync` may dispatch to different worker threads across calls, which is safe under the flag.
- Pragmas applied to every connection at open time: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`.

Helpers (single thread-trip per write so `execute` and `commit` always land on the same thread):
```python
def _exec_commit(stmt: str, params: tuple) -> int:
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
```

We do not use `aiosqlite` — the dep is unnecessary at our concurrency level (< 100 jobs) and obscures the threading model.

### 4.3 Job state machine

```
                ┌─────────┐ ── cancel_build ──▶ ┌───────────┐
                │ pending │                     │ cancelled │
                └────┬────┘                     └───────────┘
                     │  orchestrator coroutine starts (microseconds after row insert)
                     ▼
                ┌─────────┐ ─── server crash / restart ──▶ ┌──────────────┐
                │ running │                                │ interrupted  │
                └─┬───┬───┘                                └──────────────┘
   cancel_build   │   │   all sprints pass
       ──────────▶│   └─────────────▶ ┌───────────┐
   ┌──────────┐   │                   │ completed │
   │cancelled │◀──┘                   └───────────┘
   └──────────┘
                     ▲
                     │  retries exhausted, plan-review cap, or fatal error
              ┌──────┴───┐
              │  failed  │
              └──────────┘
```

Terminal: `completed`, `failed`, `cancelled`, `interrupted`.

`pending → cancelled` is the rare race window where a client invokes `cancel_build` between `start_build`'s row insert and the orchestrator coroutine's first DB write. The cancel handler marks the row terminal; the coroutine, if it ever runs, fails its CAS UPDATE (§3.2) because the WHERE-clause `status='pending'` no longer matches, and exits immediately.

### 4.4 `current_phase` enum (informational)

```
init
planning
plan-review
plan-revision           # set during a revision round; flips back to plan-review after
sprint-<N>/contract
sprint-<N>/implementing
sprint-<N>/eval-static
sprint-<N>/eval-dynamic
sprint-<N>/retry
summarizing
done
```

All separators are hyphens — matches the on-disk `sprint-N/` directory naming and avoids the underscore/hyphen mismatch that would otherwise force two parallel conventions. `current_phase` is updated atomically with `updated_at` at every transition for accurate `poll_build` reporting.

Implicit transitions (not separately listed because they're obvious from the section ordering):
- contract sealed (mutual APPROVED) → flip phase to `sprint-<N>/implementing` immediately before §6.2's `implement_contract()` call.
- handoff `status=done` from chunk loop → flip to `sprint-<N>/eval-static` immediately before §6.3.
- static section written → flip to `sprint-<N>/eval-dynamic` between the two `query()` calls.

## 5. Plan Phase

### 5.1 Step A — Plan v1

1. Validate `design_doc_path` exists and is non-empty. Else raise `design_doc_not_found`.
2. Generate ULID job_id, create `jobs/<id>/`, copy design doc to `jobs/<id>/design.md` verbatim.
3. Insert `jobs` row with `status='pending'`, `current_phase='init'`. (The orchestrator coroutine flips to `'running'` and `'planning'` on its first DB write — see §3.2 for the CAS-protected transition.)
4. Spawn Planner via `claude_agent_sdk.query()`:
   - `system_prompt = _resolved_prompt_text("planner.md")` (file contents read fresh per spawn; the file-dict form is silently unsupported by the SDK — see §9)
   - `cwd = jobs/<id>/`
   - `setting_sources = _resolved_setting_sources` (`["user"]` per §10.1 step 4 — the only combination we try; loads installed user-scope skills)
   - `mcp_servers = {"context7": _captured_mcp["context7"]}` (explicit; **NOT inherited** — see §10.4)
   - `extra_args={"strict-mcp-config": None}` (CLI flag — `ClaudeAgentOptions` does not surface this as a typed field, so we pass it via the `extra_args` escape hatch. Asserts the explicit `mcp_servers` dict is authoritative; closes the recursion-via-inheritance door)
   - `permission_mode = "acceptEdits"`
   - Tools: full `claude_code` default (no `tools=` argument; the system prompt is the guardrail).
   - `max_turns` deliberately omitted.
5. Planner's system prompt commands invocation of `superpowers:writing-plans` via the Skill tool, then writes `plan-history/plan-v1.md`.
6. **`query()` is an async iterator** — calling it returns a generator object; no work happens until iterated. The orchestrator drives it with:
   ```python
   tool_uses: list[ToolUseBlock] = []
   async for msg in query(prompt=user_prompt, options=planner_options):
       if isinstance(msg, AssistantMessage):
           for block in msg.content:
               if isinstance(block, ToolUseBlock): tool_uses.append(block)
               elif isinstance(block, TextBlock):  await pipe_text_to_log(block.text, log_path)
       elif isinstance(msg, ResultMessage):
           # log final cost/duration if needed; iteration ends after this message.
           pass
   ```
   Then **verify Skill tool was invoked** by walking `tool_uses` for a block matching the predicate (case-insensitive substring match against the skill arg, robust to dict-shape variations):
   ```python
   def is_writing_plans_invocation(block: ToolUseBlock) -> bool:
       if block.name != "Skill":
           return False
       skill_arg = (block.input or {}).get("skill") or (block.input or {}).get("name") or ""
       return "writing-plans" in str(skill_arg)
   ```
   The exact `block.input` shape is verified at implementation time against `claude_agent_sdk` source; if the field key differs, update the predicate (not the spec). If no matching block is found, log a warning and re-prompt the Planner once with "you forgot to invoke the skill, please do so now". Accept the second result regardless.
7. Validate `plan-v1.md` exists and contains at least one `^## Sprint \d+:` line (the structural check). If not, re-prompt the Planner once with the explicit failure description ("the file you wrote contains no `## Sprint N:` markers; please structure the plan as one H2 per sprint"). Second failure → job `failed` with phase `planning`.

### 5.2 Step B — Review loop

Loop until convergence or `max_plan_review_rounds` (default 5). On each iteration:

0. **Pre-review structural check** (defense-in-depth, applied to every revision plan-vN.md, not just v1).
   - Verify the file contains at least one `^## Sprint \d+:` line. If not, re-prompt the Planner once with the same explicit message used in §5.1 step 7. Second consecutive structural failure on the same revision → job `failed` with phase `plan-revision` and `error_text="planner_emitted_unstructured_plan_after_retry"`.
   - Verify the file contains **no more than `options.max_sprints`** `^## Sprint \d+:` lines. If exceeded, before invoking the Reviewer, **inject** an `[implementation]` issue into the upcoming revision prompt: `[implementation] Plan exceeds max_sprints={options.max_sprints}; consolidate into ≤{options.max_sprints} sprints.` This skips the Reviewer round (saving an LLM call) and goes straight to a Planner revision. Counts toward `max_plan_review_rounds`.
1. Spawn Reviewer via `query()`:
   - `system_prompt = _resolved_prompt_text("reviewer.md")` — embeds `plan-document-reviewer-prompt.md` verbatim **plus** issue-tagging extension.
   - Same `cwd`, `setting_sources=_resolved_setting_sources`, `mcp_servers={"context7": _captured_mcp["context7"]}`, `extra_args={"strict-mcp-config": None}`, `permission_mode="acceptEdits"` configuration as Planner.
2. Reviewer reads the latest `plan-history/plan-vN.md` against `design.md`, writes `plan-history/review-vN.md`. The orchestrator drives `query()` with the same `async for msg in query(...)` iteration pattern as §5.1 step 6 (drains the stream so file writes commit and any `ToolUseBlock`s are observable).
3. Orchestrator parses (robust to noisy reviewer output):
   - Find the **last** line matching `^\*\*Status:\*\*\s*(\w.*)$` in the file. (Last, not first — guards against the literal string `**Status:**` appearing inside a quoted example.) If status word is `Approved` → exit loop. Copy `plan-vN.md` → `plan.md`. Increment `plan_review_rounds`. Advance.
   - If `Issues Found`:
     - Walk bulleted issue list **under** the `**Issues (if any):**` header (parser anchors on this header to avoid grabbing bullets from elsewhere in the doc).
     - Cap forwarded issues at top-30 (most reviewers won't approach this; the cap is a safety net against a runaway list bloating the next Planner prompt past context limits).
     - Each issue must start with `[implementation]` or `[design]`. **Untagged issues are treated as `[implementation]`** (conservative; logs a warning).
     - Drop `[design]`-tagged issues.
     - If no `[implementation]` issues remain → also exit loop (everything was design noise).
     - Else → spawn fresh Planner session with: design.md + previous plan + the `[implementation]` issues + (advisory) `Recommendations` section. Ask for `plan-v(N+1).md`. Loop back to step 0.

Cap exhaustion → job `failed` with phase `plan-review`, `error_text` listing unresolved `[implementation]` issues.

### 5.3 Step C — Sprint extraction

Parse `plan.md` for `^## Sprint (\d+): (.+)$` in document order. Insert one `sprints` row per match (`status='pending'`, `seq` = capture group 1, `title` = capture group 2). If zero matches at this point, defense-in-depth → job `failed`.

## 6. Sprint Phase

For each sprint in order. State transitions committed to SQLite at every boundary.

### 6.1 Stage 1 — Contract negotiation

Round-based, file-mediated. Up to `max_contract_negotiation_rounds` (default 3).

`contract.md` canonical structure:

```markdown
# Sprint <N>: <Title>

## Round 1 — Generator
<criteria proposal OR APPROVED>

## Round 1 — Evaluator
<critique OR APPROVED>

## Round 2 — Generator
<revised proposal OR APPROVED>

## Round 2 — Evaluator
...
```

**File ownership: orchestrator-driven.** Agents emit message bodies; the orchestrator parses, appends `## Round N — <Role>` headers + body to `contract.md`, runs the APPROVED check on each body.

**Body extraction.** Codex streams events, not cohesive bodies. The orchestrator assembles the proposal as:
- Concatenate text from all `item/agentMessage/delta` events (`event.payload.delta`) within the turn.
- Tool-call events (`item/started`, `item/completed`) are excluded from the body but still logged.
- End-of-body = first `turn/completed` event. The orchestrator closes the `AsyncCodex` thread immediately after that event during contract negotiation, regardless of Generator's intent.
- For Claude (Evaluator), the body is the concatenated `TextBlock` content from the final `AssistantMessage` of the `query()` stream; `ToolUseBlock`s are excluded.

**Round-aware prompt.** The Generator/Evaluator prompt for round `N+1` includes the entire `contract.md` so far, plus an explicit instruction:
> "The contract above contains N completed rounds. Emit ROUND N+1 only. Either propose a revision that addresses the latest opposite-side feedback, OR emit `APPROVED` (the literal token, on its own line at the end of your response) if you accept the latest counter-proposal verbatim. Do NOT re-propose criteria you have already proposed unchanged in earlier rounds."

Without this instruction, the negotiation can oscillate.

**Halting conditions.** Both Codex and Claude can over-run a contract round if not bounded. Bound them:
- New option: `max_negotiation_turns` (default 3). For Codex: counted via `item/started` events (each one = one agentic step inside the single `thread.turn()` call) — same pattern as the chunk loop's `step_count` (§7), because each `thread.turn()` emits exactly one `turn/started` so a turn-based count would never reach >1. For Claude: counted via `AssistantMessage` count from the `query()` stream. Hitting cap → close the stream, treat the accumulated body so far as the round's output, don't crash.
- For Codex: `prompts/generator.md` instructs "When called for contract negotiation, emit your proposal as a single final message ending in either `APPROVED` or your numbered criteria list. Read any reference files **before** drafting; do not interleave reads with prose."

Per round:
1. Spawn Generator (`AsyncCodex.thread_start()`, fresh thread, **no model param** — Codex uses `~/.codex/config.toml`):
   - `AppServerConfig(codex_bin=<resolved>, cwd=<jobs/<id>/app>, config_overrides=_CODEX_CONFIG_OVERRIDES, client_name="harness-mcp", client_title="Harness Generator", client_version=__version__)` — `_CODEX_CONFIG_OVERRIDES` is the tuple captured during the §10.1.2b probe (e.g., `("sandbox_mode=workspace-write", "approval_policy=never")` if that form passed).
   - Prompt: leading user message = `prompts/generator.md` content + per-phase context (mode marker `## Mode: contract-negotiation`, design.md content **inlined verbatim**, the relevant `## Sprint N:` slice of plan.md inlined, contract.md so far inlined, round-aware instruction). Files are passed inline rather than by path because LLMs handle inline reliably; "read this file" is unreliable across SDK boundaries.
   - Wait for `turn/completed`, extract body, close thread. Append to contract.md with `## Round N — Generator` header. If body's last non-empty line is `APPROVED` (case-sensitive, on its own line), set `generator_ok = True` for this round.
2. Spawn Evaluator (Claude `query()`):
   - `system_prompt = _resolved_prompt_text("evaluator.md")`.
   - Same `cwd`, `setting_sources=_resolved_setting_sources`, `extra_args={"strict-mcp-config": None}` as Planner. `mcp_servers = {"context7": _captured_mcp["context7"]}` (no playwright at this stage). `permission_mode = "acceptEdits"` for contract negotiation (Bash not needed; bypassPermissions reserved for the static/dynamic phase per §6.3 / §8.1).
   - Per-phase user prompt scopes to contract negotiation, includes the same inlined inputs as Generator plus the round-aware instruction.
   - Extract body, append with `## Round N — Evaluator` header. APPROVED check → `evaluator_ok`.
3. **Both flags true in the same round → contract sealed.** Phase flips to `sprint-<N>/implementing`. Otherwise advance.

**Empty-body guard.** If the extracted body for any round is empty (whitespace-only after strip), do **not** append the round to `contract.md`. Log `"sprint N round M: <role> emitted empty body; treating as no-op (counts toward max_contract_negotiation_rounds)"`. Increment the negotiation-round counter without writing the file, proceed to next round. If both Generator and Evaluator emit empty bodies in the same round, abort negotiation immediately with sprint `failed` and `error_text="contract_negotiation_no_progress"` (counts as one retry; next attempt restarts negotiation).

**Atomic appends.** "Append" is shorthand: the orchestrator reads existing `contract.md`, concatenates the new round in memory, writes to `contract.md.tmp`, and `os.replace()` to `contract.md`. Same temp-and-rename pattern as §7.1 — true file appends aren't crash-atomic for multi-byte writes.

Non-convergence after the cap → sprint `failed` (counts as one of the sprint's retries).

### 6.2 Stage 2 — Implementation

Single async function call:

```python
async def implement_contract(
    job_id: str, sprint_seq: int,
    contract_path: Path, design_path: Path, plan_section_path: Path,
    app_dir: Path, log_path: Path,
    options: JobOptions,
    eval_md_for_retry: Path | None = None,
) -> ImplementationResult: ...
```

Internally: the chunk loop (§7). Returns `ImplementationResult(ok: bool, files_touched: list[str], commit_sha: str, summary: str)`. On success, orchestrator runs `git tag harness/<job_id>/sprint-<N>` (or `git tag -f harness/<job_id>/sprint-<N>` on retry, after the namespace-aware annotated-tag-collision check from §6.4).

### 6.3 Stage 3 — Evaluation

Single `ClaudeSDKClient` per sprint, two `query()` calls in series — **shared context across static and dynamic** so the audit's reasoning carries into verification. The `ClaudeSDKClient` block runs **inside the launcher subprocess** (§8.4), not in the orchestrator process; the orchestrator only sees the resulting `eval.md`. Pseudocode for the launcher's body:

```python
# Inside harness_mcp.evaluator_runner (the launcher entry point):
with anyio.fail_after(max_evaluation_seconds):                # sync context manager
    async with ClaudeSDKClient(options=evaluator_options) as client:
        # CRITICAL: ClaudeSDKClient.query() requires draining receive_response()
        # before the next query() — undrained streams stall subsequent queries.
        await client.query(static_audit_prompt(...))
        async for msg in client.receive_response():
            await pipe_claude_msg_to_log(msg, log_path)   # also captures TextBlocks
        await sync_eval_md(eval_path, expect_section="## Static audit")

        await client.query(dynamic_verification_prompt(...))
        async for msg in client.receive_response():
            await pipe_claude_msg_to_log(msg, log_path)
        await sync_eval_md(eval_path, expect_section="## Dynamic verification")
```

`pipe_claude_msg_to_log` is a small helper analogous to `EventLogger.handle` for Codex, but for `claude_agent_sdk` message types: `AssistantMessage` → text from each `TextBlock`; `ToolUseBlock` → `[tool: <name> args=<...>]`; `ResultMessage` → final `--- result (cost $X) ---` line.

The orchestrator's view is just `await proc.wait()` under the outer budget (§8.4); the launcher itself enforces `max_evaluation_seconds` internally.

`evaluator_options` differs from Planner/Reviewer in two ways:
1. `permission_mode` — Evaluator needs Bash to start the app under test, run `pytest`, etc. `permission_mode="acceptEdits"` does **not** auto-approve Bash. Set `permission_mode="bypassPermissions"` for Evaluator only (Planner/Reviewer/Summarizer stay on `"acceptEdits"`). The hard cwd boundary plus the cleanup process group is the actual safety net; bypassing prompts isn't a safety regression at this level.
2. `mcp_servers` — includes `playwright` if captured at startup; otherwise just `context7`.

`sync_eval_md(path, expect_section)` is a small helper:
```python
def _fsync_dir(d: Path) -> None:
    """fsync a directory entry so a recently-renamed-into file is durable.

    POSIX: opens the dir as O_RDONLY and fsyncs the fd. Windows: best-effort
    no-op (target is POSIX; harness-mcp on Windows is a recovery-edge concern,
    not a primary platform).
    """
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except (PermissionError, OSError):
        return                            # Windows or unsupported FS — best-effort
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

async def sync_eval_md(path: Path, expect_section: str) -> None:
    """Wait for SDK file writes to land, validate the section header is present."""
    await anyio.to_thread.run_sync(_fsync_dir, path.parent)
    await anyio.sleep(0.1)
    text = path.read_text(encoding="utf-8")
    if expect_section not in text:
        raise EvaluatorEmittedUnparseableEvalMdError(
            f"expected '{expect_section}' header missing after query"
        )
```

The Evaluator writes `eval.md` directly (single-author, no negotiation). The system prompt instructs the Evaluator to **rewrite the entire eval.md from scratch each query**, re-including any prior section content rather than appending — this forecloses partial-write races where a streamed Write tool call might land mid-flush. Each query produces a coherent, parseable eval.md.

Canonical format:

```markdown
# Sprint <N> Evaluation

## Static audit

### Criterion 1: <text from contract>
**Result:** PASS | FAIL
**Evidence:** <code refs, file:line>
**Notes:** <reasoning>

### Criterion 2: ...

## Dynamic verification

### Routing decision
<one paragraph: which tools chosen and why>

### Criterion 1: <text>
**Result:** PASS | FAIL
**Evidence:** <playwright steps / curl / pytest / DB query>
**Notes:** <reasoning>

### Criterion 2: ...
```

`parse_eval_md()`:
- Walks `### Criterion` blocks under both sections.
- Reads each `**Result:**` (case-sensitive). Any `FAIL` in either section = sprint fails.
- If no `### Criterion` blocks parse → sprint marked failed with `error_text="evaluator_emitted_unparseable_eval_md"` (counts as retry; fresh Evaluator client on next attempt).

**App-server lifecycle:** Evaluator-managed. The Evaluator's `cwd` is `jobs/<id>/` (so it can read contract.md, plan.md, etc.) but the **app code lives at `jobs/<id>/app/`**. The system prompt explicitly tells the Evaluator: "Your cwd is `<job_dir>`, not `app/`. To run code, `cd app && …` or pass `cwd=app/` when invoking Bash." Without this hint the Evaluator burns turns on `python -m`, `pytest`, `npm` invocations that fail because the project root isn't where it's running.

The Evaluator's system prompt instructs it to start the app via Bash, drive it, and try to kill it. The orchestrator runs the Evaluator phase under a `ProcessGroupScope` (§8.4) — a launcher subprocess (`harness_mcp.evaluator_runner`) wrapping the `ClaudeSDKClient` call, spawned with `start_new_session=True` so all Bash-spawned dev servers, pytest workers, and Playwright Chromium share the launcher's pgroup. On Evaluator exit (clean or timeout), the scope's `__aexit__` issues `killpg(pgid, SIGTERM)` against that pgroup, catching everything the Evaluator forgot to clean up.

**Playwright unreachable mid-evaluation** → Playwright MCP tool error propagates → Evaluator records FAIL in eval.md → sprint fails normally. No distinct error code — the failure surfaces inside the eval.md routing-decision section ("Routing chose Playwright; Playwright unreachable") and as an explicit `**Result:** FAIL` on every UI criterion.

### 6.4 Stage 4 — Retry on fail

Up to `max_sprint_retries` (default 2). Same `implement_contract()` call, same `contract.md` (read-only on retry — the contract is the agreement, not a moving target), but with `eval_md_for_retry=Path(eval.md)`. The Generator's retry prompt explicitly instructs: "address these specific failed criteria without expanding scope."

**Git tag namespacing.** All harness tags live under the `harness/<job_id>/` namespace: `git tag harness/<job_id>/sprint-<N>` (or `git tag -f` on retry). This avoids collision with any user-created tags in the same `app/` repo and makes `git tag --list 'harness/*'` enumerate every tag the harness produced.

**Collision check, narrowed.** Before tagging, check whether the name already exists as an *annotated* tag (`git for-each-ref --format='%(taggerdate)' refs/tags/<name>` returns non-empty). If annotated:
- If the tag name starts with `harness/<this_job_id>/`: it's our own tag from a prior partial run; log a warning ("overwriting prior harness annotated tag from same job; this is a retry") and proceed with `git tag -f`.
- Otherwise (annotated tag is user-curated or from a different harness job): refuse with `error_text="harness_tag_collision"` to preserve their history.

This narrower check unblocks legitimate retries while still protecting user tags.

The Evaluator's static-audit `git diff` references for sprint N+1 use `harness/<job_id>/sprint-<N>` (the prior sprint's namespaced tag) as the diff base. On the very first sprint, no prior tag exists; the Evaluator's prompt explicitly handles "no prior tag" as "diff against the empty tree".

Beyond cap → job `failed` with phase `sprint-<N>/retry` and `error_text` summarizing persistently-failing criteria. If the same criterion fails ≥ 2 retries with similar root-cause text in eval.md, surface `error_text="criterion_<n>_persistently_unrealizable_under_current_contract"` so the user can recognize a contract-design issue rather than an implementation issue. (Future: contract renegotiation between retries — out of scope for v1.)

### 6.5 Sprint timeout

`anyio.fail_after(options.max_sprint_duration_minutes * 60)` wraps Stages 1–3 of each sprint attempt. On expiry: the cancel scope raises `TimeoutError` and propagates `CancelledError` through every nested task; SDK clients raise inside their `query()` / `turn.stream()`; their `__aexit__` runs.

But anyio cancellation alone doesn't catch grandchildren (Bash-spawned dev servers, Playwright browsers). The cleanup is a paired `try/finally`:

```python
# Each stage takes context-manager-shaped resources so __aexit__ runs on cancel.
# Stage 3 (evaluation) is wrapped in a ProcessGroupScope (§8.4) — the scope's
# __aexit__ handles SIGTERM/SIGKILL of the launcher pgroup. Stage 2 (impl) is
# wrapped in AsyncCodex (§7) — its __aexit__ closes the thread.
# So "cleanup" is just letting those context managers exit, bounded by a grace.

try:
    with anyio.fail_after(options.max_sprint_duration_minutes * 60):
        await stage_1_contract_negotiation(...)
        await stage_2_implementation(...)         # AsyncCodex __aexit__ on cancel
        await stage_3_evaluation(...)             # ProcessGroupScope __aexit__ on cancel
finally:
    # The stage-level context managers handle their own cleanup; this final
    # finally just bounds the unwind so a stuck SDK __aexit__ can't hang the
    # orchestrator forever.
    with anyio.CancelScope(shield=True):
        with anyio.move_on_after(15):
            await update_sprint_row(...)          # write final sprint status
```

If `TimeoutError` propagates out (cancellation cause = sprint timeout), the orchestrator catches it at the sprint-loop level and treats the sprint as failed with `error_text="sprint_timeout"`, counting toward `max_sprint_retries`. If `CancelledError` propagates out (cancellation cause = `cancel_build` or server shutdown), the orchestrator's outer `except CancelledError` block in §3.2 / §10.7 marks the job and re-raises to terminate the coroutine.

On timeout: sprint marked failed with `error_text="sprint_timeout"`; counts as one of `max_sprint_retries`.

### 6.6 Job completion

After all sprints `passed`:
1. Phase → `summarizing`.
2. Spawn Summarizer via `claude_agent_sdk.query()` with the same options shape as Planner (§5.1 step 4): `system_prompt = _resolved_prompt_text("summarizer.md")`, `cwd = jobs/<id>/`, `setting_sources = _resolved_setting_sources`, `mcp_servers = {"context7": _captured_mcp["context7"]}`, `extra_args = {"strict-mcp-config": None}`, `permission_mode = "acceptEdits"`, no `tools=` argument (default Claude Code toolset), `max_turns` deliberately omitted. User prompt instructs reading design.md + plan.md + every sprint's eval.md, writing 2–3 sentences to `summary.md`. The orchestrator drives `query()` with the same `async for msg in query(...)` iteration pattern as §5.1 step 6.
3. Set `jobs.last_message` to the summary's content. Status → `completed`. Phase → `done`. `finished_at = now`.

## 7. Generator Internals — Reset-and-Handoff Loop

Inside `implement_contract`. Pseudocode:

```python
chunk_seq = 1
prev_handoff = None
while True:
    handoff_path = sprint_dir / f"handoff-{chunk_seq:03d}.md"

    cfg = AppServerConfig(
        codex_bin = resolve_codex_bin(),       # HARNESS_CODEX_BIN | PATH
        cwd = str(app_dir),
        config_overrides = _CODEX_CONFIG_OVERRIDES,    # captured during §10.1.2b probe
        # NO model param — Codex uses ~/.codex/config.toml.
        client_name = "harness-mcp",
        client_title = "Harness Generator",
        client_version = __version__,
    )

    event_logger = EventLogger(log_path)
    step_count = 0                             # per-chunk: counts item/started events (agentic steps); resets on each AsyncCodex enter

    try:
        async with AsyncCodex(config=cfg) as codex:
            thread = await codex.thread_start()

            prompt = build_chunk_prompt(
                generator_md = load_prompt("generator.md"),
                contract_path, design_path, plan_section_path,
                prev_handoff,                  # None on first chunk
                eval_md_for_retry,             # only on retries
                handoff_path,                  # told where to write its handoff
                chunk_seq,
            )
            turn = await thread.turn(TextInput(prompt))   # turn() is awaitable; one AsyncTurnHandle

            chunk_started = monotonic()
            async for event in turn.stream():
                await event_logger.handle(event)
                # Each thread.turn() yields exactly ONE turn/started — we cannot use
                # turn count as an internal-progress signal. Use item/started instead:
                # one item event per agentic step (tool call / model message), so this
                # is a proxy for "how much work has the agent done so far in this turn."
                if event.method == "item/started":
                    step_count += 1

                # Reset trigger (first to fire wins):
                if step_count >= options.codex_reset_steps:                          break
                if monotonic() - chunk_started >= options.codex_reset_minutes * 60:  break
    except Exception as e:
        raise GeneratorChunkError(chunk_seq, e) from e
    finally:
        # Always close the logger so the file handle is released, even on exceptional exit.
        await event_logger.aclose()

    try:
        handoff = parse_handoff(handoff_path)   # may raise HandoffParseError
    except HandoffParseError as e:
        if chunk_seq < options.max_codex_chunks_per_sprint:
            log.warning("chunk %d: handoff malformed (%s); continuing fresh", chunk_seq, e)
            prev_handoff = None    # next chunk gets fresh start; addendum in build_chunk_prompt
            chunk_seq += 1
            continue
        # Cap-boundary salvage: if the malformed handoff was probably 'done',
        # try to commit anyway so we don't waste the chunk's work.
        if handoff_path.is_file():
            tail = handoff_path.read_text(encoding="utf-8", errors="replace")[-2048:]
            if re.search(r"^## Status\s*\n+\s*done\s*$", tail, re.MULTILINE):
                log.warning("chunk %d: handoff malformed but Status=done detected; salvaging",
                            chunk_seq)
                synthetic = Handoff(
                    chunk_seq=chunk_seq, status="done",
                    summary=f"sprint {sprint_seq} (salvaged)",
                    work_done=[], decisions=[], files_touched=[],
                    open_questions=[], next_steps=[], declares_done=True,
                )
                try:
                    return await commit_and_summarize(app_dir, synthetic, sprint_seq, job_id)
                except CommitFailedError as ce:
                    return ImplementationResult(ok=False, error=f"commit_failed: {ce}")
        return ImplementationResult(ok=False, error="handoff_persistently_malformed")

    if handoff.declares_done:
        try:
            return await commit_and_summarize(app_dir, handoff, sprint_seq, job_id)
        except CommitFailedError as e:
            return ImplementationResult(ok=False, error=f"commit_failed: {e}")

    prev_handoff = handoff_path
    chunk_seq += 1
    if chunk_seq > options.max_codex_chunks_per_sprint:
        return ImplementationResult(ok=False, error="generator_chunk_cap_exhausted")
```

**On `await thread.turn(...)`**: per `codex_app_server` docs, `thread.turn()` returns an `Awaitable[AsyncTurnHandle]`. The earlier syntax `turn = thread.turn(...)` (no await) does not work in Python; the spec previously had this wrong.

**On event names**: `item/agentMessage/delta` for streamed assistant text (camelCase, with `item/` prefix), `item/started` and `item/completed` for tool-call markers, `turn/started` and `turn/completed` for turn boundaries, `thread/tokenUsage/updated` for token usage (not currently used; available if a future revision drives resets on tokens instead of turn count). Matched on `event.method`; payload accessed via `event.payload`.

**On `AsyncCodex` exit cleanup**: when we `break` from `turn.stream()` while still inside `async with AsyncCodex(...)`, the `__aexit__` cleans up the thread. Bound the cleanup with `with anyio.move_on_after(10):` if implementation testing reveals slow shutdowns; verify the SDK's stream-cancellation semantics first. (`move_on_after` and `fail_after` are sync context managers in anyio; first positional arg is `seconds`.)

### 7.0 `build_chunk_prompt` templates

Four shapes, selected per call site. Every shape inlines content (not file paths) for sections the agent must read; LLMs handle inlined content more reliably than "go read X".

**Shape 1 — Contract negotiation** (used by §6.1, not the chunk loop):
```
{generator_md}

## Mode: contract-negotiation

## Design (verbatim)
<design.md content>

## Plan section (verbatim)
<this sprint's `## Sprint N:` slice from plan.md>

## Contract so far (verbatim)
<contract.md content>

## Round instruction
The contract above contains {N} completed rounds. Emit ROUND {N+1}
only — propose a revision OR emit `APPROVED` on its own line at the
end of your response if you accept the latest counter-proposal verbatim.
Do NOT re-propose criteria you have already proposed unchanged.
```

**Shape 2 — First chunk** (chunk_seq == 1, no eval_md_for_retry):
```
{generator_md}

## Mode: implementation (first chunk)

## Design (verbatim)
<design.md content>

## Plan section (verbatim)
<this sprint's slice from plan.md>

## Contract (verbatim)
<contract.md content>

## Where to write your handoff
At the end of this chunk, write your handoff to: {handoff_path}
Use the format in §7.1.
```

**Shape 3 — Continued chunk** (chunk_seq > 1, no retry):
```
{generator_md}

## Mode: implementation (chunk {chunk_seq}, continuation)

## Contract (verbatim)
<contract.md content>

## Previous handoff (verbatim)
<prev_handoff content — Status, Work done, Decisions, Files touched, Open questions, Next steps>

## Where to write your handoff
At the end of this chunk, write your handoff to: {handoff_path}
Pick up from "Next steps" in the previous handoff.
```

**Shape 4 — Retry chunk** (eval_md_for_retry is set; chunk_seq always == 1 because retries reset chunk_seq):
```
{generator_md}

## Mode: implementation (retry — previous attempt failed evaluation)

## Contract (verbatim, READ-ONLY)
<contract.md content>

## Failed evaluation (verbatim)
<eval.md content>

## Instructions
The previous attempt failed the evaluation above. Address the specific
FAIL criteria without expanding scope. Do NOT propose new criteria.
The contract is fixed — work within it.

## Where to write your handoff
At the end of this chunk, write your handoff to: {handoff_path}
```

If `prev_handoff is None` on a non-first chunk (because previous handoff was malformed), Shape 3 substitutes `## Previous handoff (verbatim)` with: "Previous chunk produced no valid handoff. Proceed fresh based on contract.md and what's already in the working tree."

### 7.1 Handoff format (`handoff-NNN.md`)

```markdown
# Handoff <chunk_seq>

## Status
<"in-progress" | "done">

## Summary
<one-line summary, used as commit message subject>

## Work done this chunk
- <bullets>

## Decisions made
- <bullets, with rationale>

## Files touched
- path/to/file.py — <brief reason>

## Open questions / concerns
- <bullets, optional>

## Next steps (if in-progress)
- <ordered list — what the next Codex chunk should do first>
```

`Summary` is new (used by `commit_and_summarize`). `Status` must be exactly `in-progress` or `done` (case-sensitive); other values raise `HandoffParseError`. The Generator's prompt explicitly tells Codex this file is the only artifact between resets and shows this exact format.

**Parser rule for `files_touched`**: split each bullet on the **first** ` — ` (space, em-dash, space) delimiter. Left side = path (stripped); right side = reason (stripped). If the bullet contains no ` — `, treat the whole bullet as the path with `reason=""`. Empty `path` → drop the entry with a warning. Multiple ` — ` in the reason side are preserved (only the first is the delimiter). The parser does **not** validate that the path exists on disk — a Generator sometimes lists a file it intended to write but failed to; that's diagnostic content, not a correctness invariant.

**Atomic writes.** The Generator's prompt instructs it to write the handoff via the temp-and-rename pattern: write to `handoff-NNN.md.tmp`, then `os.replace()` to `handoff-NNN.md`. This makes the write POSIX-atomic on the same filesystem; a crash mid-write leaves either no file or the previous one. The orchestrator likewise uses temp-and-rename for any file it writes (contract.md round appends, eval.md). `log.txt` stays append-with-flush — best-effort; not a recovery source.

### 7.2 Reset triggers

- **Step count**: `codex_reset_steps` (default 60). Counted from `item/started` events. Each event represents one agentic step inside the single `thread.turn()` call (a tool call, a model message, a command execution). This is a proxy for "how much agentic work has been done"; we don't use `turn/started` because Codex emits exactly one `turn/started` per `thread.turn()` call, so a turn-based count would never reach > 1 in our chunk-loop pattern.
- **Wall-clock**: `codex_reset_minutes` (default 25). `monotonic()` from chunk start.
- Whichever fires first.

If a future refactor moves to a multi-`thread.turn()` chunk loop (driven by continuation prompts), the counter can be renamed to `codex_reset_turns` and the increment moved to `turn/started` — at which point the default value should also be revisited because the unit changes meaningfully.

### 7.3 Logging

Every Codex event → `sprint-N/log.txt`. Plain text, per-line flushed for live `tail -f`. Codex SDK abstracts the underlying subprocess into a typed event stream; "pipe stdout/stderr to log.txt" from the original brief is satisfied via this event stream rather than raw subprocess capture (the SDK does not expose subprocess stdio directly). Mapping:

- `item/agentMessage/delta` → prose chunk, streamed (use `event.payload.delta`).
- `item/started` → record tool-call start in the EventLogger buffer.
- `item/completed` → emit `[tool: <name> args=<truncated>] → <result-summary>` line, paired with the buffered start.
- `turn/started`, `turn/completed` → `--- turn N (<status>) ---` separators.
- `thread/tokenUsage/updated` → ignored for now (could drive token-aware resets in a future revision).
- All other event types → ignored.

The mapping is stateful (tool-start without tool-completion needs to be flushed at chunk boundary). See §7.5 for the `EventLogger` class.

### 7.4 Commit handling

On `handoff.declares_done`:
1. `cd app_dir`. Repo was `git init`'d at job start with the seeded `.gitignore` (§4.1).
2. `git add .` (NOT `-A` — `.gitignore` is the filter; pathspec is implicit). Then `git diff --cached --quiet`. If working tree had uncommitted changes, commit with subject line `Sprint <N>: <handoff.summary truncated to 80 chars>`. Body includes the handoff's "Work done" + "Decisions" sections verbatim.
3. If Codex already committed during its work, those commits stand; the wrap-up commit (if any) is just for whatever was uncommitted.
4. Tag: `git tag harness/<job_id>/sprint-<N>` (or `git tag -f harness/<job_id>/sprint-<N>` on retry, after the annotated-tag-collision check from §6.4).
5. Return `ImplementationResult(ok=True, commit_sha=<HEAD>, files_touched=[p for p, _reason in handoff.files_touched], summary=handoff.summary)`. The conversion is explicit because `Handoff.files_touched` is `list[tuple[str, str]]` (path + reason) per §11.0 and §7.1's parser, while `ImplementationResult.files_touched` is `list[str]` (paths only — the reasons live in the handoff file for forensics).

Failure paths in this stage (permissions, disk full, git binary missing) raise `CommitFailedError`; the chunk loop converts to `ImplementationResult(ok=False, error=...)` so the sprint retry loop sees a normal failure rather than a crash.

### 7.5 EventLogger

```python
class EventLogger:
    """Stateful per-chunk Codex event → log.txt formatter.

    Design:
        Codex events arrive interleaved (a tool call's start and result are
        separate events). To produce useful single-line log entries like
        `[tool: Read args=<...>] → <result>`, we buffer in-flight calls
        keyed by call id and emit the combined line on completion. Orphaned
        starts (no completion by chunk end) flush as `[tool: ... -> NO_RESULT]`.

    Implementation:
        Maintains `self._calls: dict[str, _ToolStart]`. `handle(event)`
        switches on `event.method`. `aclose()` is called by the chunk loop
        on exit (clean or exceptional); it drains orphans via `flush()` and
        closes the file handle.

    Example:
        >>> logger = EventLogger(log_path)
        >>> async for event in turn.stream(): await logger.handle(event)
        >>> await logger.aclose()
    """

    def __init__(self, log_path: Path) -> None:
        # Open ONCE per chunk; held open until aclose(). buffering=1 = line-buffered.
        self._fh = open(log_path, "a", encoding="utf-8", buffering=1)
        self._calls: dict[str, _ToolStart] = {}

    async def handle(self, event: CodexEvent) -> None:
        """Inline state machine: switch on event.method and either buffer or emit.

        Payload shapes per Codex docs:
        - item/agentMessage/delta: payload.delta (str)
        - item/started, item/completed: payload.item (ThreadItem) with type-specific
          fields. We handle commandExecution and mcpToolCall; everything else is logged
          generically by item.id.
        - turn/started, turn/completed: payload.turn (object with id, status, ...)
        Field names verified at implementation time against `codex_app_server` source.
        """
        method = event.method
        line: str | None = None

        if method == "item/agentMessage/delta":
            line = getattr(event.payload, "delta", "") or None
        elif method == "item/started":
            item = getattr(event.payload, "item", None)
            if item is not None:
                item_id = getattr(item, "id", None)
                name, args = _summarize_item(item)
                if item_id:
                    self._calls[item_id] = _ToolStart(name=name, args=args)
        elif method == "item/completed":
            item = getattr(event.payload, "item", None)
            item_id = getattr(item, "id", None) if item else None
            start = self._calls.pop(item_id, None) if item_id else None
            if start is not None:
                result = _truncate(_summarize_item_result(item))
                line = f"[tool: {start.name} args={start.args} -> {result}]"
        elif method == "turn/started":
            turn = getattr(event.payload, "turn", None)
            tid = getattr(turn, "id", "?") if turn else "?"
            line = f"--- turn {tid} (started) ---"
        elif method == "turn/completed":
            turn = getattr(event.payload, "turn", None)
            tid = getattr(turn, "id", "?") if turn else "?"
            status_obj = getattr(turn, "status", None) if turn else None
            status = getattr(status_obj, "value", str(status_obj)) if status_obj else "?"
            line = f"--- turn {tid} ({status}) ---"
        # All other event types are ignored.

        if line is not None:
            await anyio.to_thread.run_sync(self._fh.write, line + "\n")

    async def flush(self) -> None:
        """Drain orphaned tool-call starts (no completion seen yet)."""
        for start in list(self._calls.values()):
            await anyio.to_thread.run_sync(
                self._fh.write, f"[tool: {start.name} args={start.args} -> NO_RESULT]\n"
            )
        self._calls.clear()
        await anyio.to_thread.run_sync(self._fh.flush)

    async def aclose(self) -> None:
        await self.flush()
        await anyio.to_thread.run_sync(self._fh.close)
```

`_ToolStart` is a small private dataclass `@dataclass class _ToolStart: name: str; args: str`. `_truncate(x, max_len=200)` stringifies and clips with an ellipsis. `_summarize_item(item)` switches on `item.type`: `"mcpToolCall"` → `(item.tool, _truncate(item.arguments))`; `"commandExecution"` → `("exec", _truncate(item.command))`; default → `(item.type, "")`. `_summarize_item_result(item)` reads `item.result` / `item.error` / `item.aggregatedOutput` per type, falling back to `""`. Field names verified at implementation time against `codex_app_server` source.

Writes are line-buffered (`buffering=1`) so live `tail -f` works. `aclose()` is called by the chunk loop on exit (clean or exceptional) — without it, each chunk leaks its file handle. All blocking I/O is offloaded via `anyio.to_thread.run_sync` so the event loop stays responsive even under high event-rate streams.

## 8. Evaluator Internals

### 8.1 Configuration

```python
options = ClaudeAgentOptions(
    system_prompt = _resolved_prompt_text("evaluator.md"),
    cwd = str(jobs_dir / job_id),                  # so Evaluator can read app/, contract.md, etc.
    setting_sources = _resolved_setting_sources,   # captured at startup; e.g., ["user"]
    mcp_servers = {                                # explicit, NOT inherited
        "context7": _captured_mcp["context7"],
        # playwright key included only when present at startup:
        **({"playwright": _captured_mcp["playwright"]} if "playwright" in _captured_mcp else {}),
    },
    extra_args = {"strict-mcp-config": None},      # enforce explicit-mcp-only via CLI flag
    permission_mode = "bypassPermissions",         # Evaluator needs Bash; acceptEdits doesn't cover Bash
)
```

`_resolved_prompt_text(name)` reads the prompt's contents fresh from `<package>/prompts/<name>` (see §9). `_resolved_setting_sources` is the combination that resolved `superpowers:writing-plans` at startup (see §10.1 step 4) — currently `["user"]`. `_captured_mcp` is the dict of captured stanzas (see §10.1 step 5).

### 8.2 Static audit

User prompt (template; orchestrator substitutes the literal tag):
- "Read design.md, plan.md, contract.md."
- "Read `git diff harness/<job_id>/sprint-<N-1>..HEAD` (or, if no prior sprint tag exists — i.e., this is sprint 1 — read the entire `app/` tree)."
- "For each contract criterion, render the `### Criterion <n>:` block with **Result:** PASS or FAIL, **Evidence:**, and **Notes:**."
- "Write your work as the `## Static audit` section of eval.md. Rewrite the entire file from scratch (re-include any prior content); do not partial-append."
- Identifies: missing implementation, design drift, missing edge-case handling visible at code-read time.

### 8.3 Dynamic verification

User prompt:
- "First, write a `### Routing decision` paragraph at the top of `## Dynamic verification`. State which tools you will drive (Playwright MCP / Bash test runner / httpx / DB inspection / nothing) and why."
- "Then, render `### Criterion <n>:` blocks for each contract criterion using your chosen tools."
- "If you start app processes, do your best to kill them. The orchestrator wraps you in a process group and SIGTERMs the group when you finish."

### 8.4 Process-group cleanup — launcher subprocess strategy

The Evaluator spawns Bash, which may spawn dev servers, pytest workers, Playwright Chromium. We need to kill the entire descendant tree on Evaluator exit. The Claude Agent SDK does **not** expose any hook for the underlying subprocess spawn (verified against `claude_agent_sdk._internal.transport.subprocess_cli`: only `cli_path`, `env`, `cwd`, `user`, `extra_args`, `settings`, `add_dirs`, `stderr` are surfaced; no `popen_factory` / `preexec_fn` / `start_new_session`). So the only reliable path is to wrap the Evaluator phase in our own launcher subprocess.

**Launcher entry point**: `harness_mcp.evaluator_runner` (a real module shipped in §11). Invoked under a `ProcessGroupScope` so cleanup is automatic:

```python
async with ProcessGroupScope(f"eval-{job_id}-{sprint_seq}") as pg:
    proc = await pg.spawn(                              # pg enforces start_new_session=True
        [sys.executable, "-m", "harness_mcp.evaluator_runner"],
        stdin=PIPE, stdout=PIPE, stderr=PIPE,
    )
    await pg.communicate(proc, payload_json)            # writes stdin then closes it
    rc = await proc.wait()
# pg.__aexit__ has now SIGTERMed/SIGKILLed any stragglers in the launcher's pgroup.
```

`pg.spawn(...)` is responsible for `start_new_session=True` so the launcher's `pgid == proc.pid`; the scope captures and tracks that pgid for cleanup.

**Args protocol — payload schema**: orchestrator writes a JSON payload to the launcher's stdin and closes it. Schema:

```json
{
  "job_id": "<ULID>",
  "sprint_seq": <int>,
  "paths": {
    "design": "<abs>", "plan": "<abs>", "contract": "<abs>",
    "eval": "<abs>",   "app": "<abs>",  "log": "<abs>"
  },
  "captured_mcp_stanzas": {
    "context7":  { ...full stanza dict from _captured_mcp... },
    "playwright": { ... }
  },
  "setting_sources": ["user"],
  "max_evaluation_seconds": 1800
}
```

**Launcher reconstruction** (in `evaluator_runner.py`):
```python
import json, sys
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from harness_mcp.prompts_loader import _resolved_prompt_text

payload = json.loads(sys.stdin.read())
mcp_servers = {name: dict(stanza) for name, stanza in payload["captured_mcp_stanzas"].items()}
evaluator_options = ClaudeAgentOptions(
    system_prompt = _resolved_prompt_text("evaluator.md"),
    cwd = payload["paths"]["app"].rsplit("/app", 1)[0],   # the job dir
    setting_sources = payload["setting_sources"],
    mcp_servers = mcp_servers,
    extra_args = {"strict-mcp-config": None},
    permission_mode = "bypassPermissions",
)
# ...then drive the static + dynamic queries as in §6.3.
```

The launcher does NOT trust paths blindly — it asserts each path is inside `~/.harness/jobs/<job_id>/` before opening. `_resolved_prompt_text` is called inside the launcher (importlib.resources resolves the same package files in the launcher's process).

**Result protocol** (filesystem only):
- The launcher itself drives `ClaudeSDKClient` and writes `eval.md`. The orchestrator parses it via `parse_eval_md` exactly as if there were no launcher.
- The launcher exits 0 on clean completion, 1 on internal error (Claude SDK crash, prompt-load failure, etc.).
- Orchestrator: `await proc.wait()` under `anyio.fail_after(max_evaluation_seconds + 60)`. The +60s buffer covers fork/exec + SDK init beyond the inner Evaluator budget. If `returncode != 0`, treat as `EvaluatorEmittedUnparseableEvalMdError` (same retry path as in-process). On parse-failure regardless of exit code, same retry.
- **Launcher stdio drainers.** The launcher communicates results via filesystem (eval.md), not via pipes — but stdout and stderr still need to be drained, because any pipe (~64KB on Linux/macOS) that fills will deadlock the launcher. Stderr is drained for diagnostics (Claude SDK's verbose traces, deprecation warnings); the last 4KB is captured in a bounded `collections.deque(maxlen=4096)` for `error_text` on non-zero exit. Stdout is drained too — even though the launcher's own code doesn't print, transitive imports (`claude_agent_sdk`, `anyio`, `httpx`) or stray `DeprecationWarning`s can land there. Concrete pattern:
  ```python
  async with ProcessGroupScope(...) as pg:
      proc = await pg.spawn([...], stdin=PIPE, stdout=PIPE, stderr=PIPE)
      stderr_tail: deque[str] = deque(maxlen=4096)
      async with anyio.create_task_group() as tg:
          tg.start_soon(_pipe_stream_to_log, proc.stderr, log_path, stderr_tail)
          tg.start_soon(_pipe_stream_to_log, proc.stdout, log_path, None)   # drain only
          await pg.communicate(proc, payload_json)
          rc = await proc.wait()
      # tg exits; both drainers see EOF.
  ```
  `_pipe_stream_to_log` is a single helper that takes an optional tail-deque (None = drain only, no capture).
- **Launcher logging — Claude messages, not Codex events.** Inside the launcher, drive `ClaudeSDKClient` (§6.3) and call `pipe_claude_msg_to_log` (defined in `harness_mcp.evaluator` next to the Claude message helper described at §6.3) for each `AssistantMessage` / `ToolUseBlock` / `ResultMessage`. EventLogger is unused inside the launcher (it formats Codex events; the launcher never sees Codex). The launcher imports `harness_mcp.evaluator` (for the Claude logger and the static/dynamic prompt builders) and `harness_mcp.prompts_loader` (for `_resolved_prompt_text`). It does NOT import `harness_mcp.logging_setup` (Codex EventLogger) or `harness_mcp.state`.
- **Launcher SQLite isolation.** The launcher process is stateless w.r.t. `state.db` — only the orchestrator updates job/sprint state. `evaluator_runner.py` MUST NOT import `harness_mcp.state` (or anything that transitively does) at module level; doing so would open a second writer connection in the launcher process, racing with the orchestrator's writer. Validated by an import-graph unit test in §12.1.

**Cancellation forwarding**:
- `cancel_build` and sprint timeout cancel the orchestrator's anyio task. The cancel propagates into the `async with ProcessGroupScope(...)` wrapping the launcher; the scope's `__aexit__` issues shielded `SIGTERM` to the launcher's pgroup, waits up to `grace_seconds` (default 5), then `SIGKILL` for stragglers. The launcher and all its grandchildren die together.
- `proc.wait()` itself respects anyio cancellation; the scope is the cleanup primitive, not a manual `proc.terminate()` (which would only signal the launcher, not its descendants).

**`ProcessGroupScope` interface** (see §11 `process_group.py`):
```python
@asynccontextmanager
async def ProcessGroupScope(label: str, grace_seconds: float = 5.0) -> AsyncIterator[ProcessGroupHandle]:
    """Bracket the lifetime of a launcher subprocess + its descendants.

    Design:
        Tracks the launcher's pgid. On context exit (clean, exception, or
        anyio cancellation), shielded SIGTERM/SIGKILL of the whole group
        guarantees no leaked dev servers / Playwright browsers.

    Implementation:
        Uses os.killpg() with the tracked pgid. The launcher MUST be spawned
        with start_new_session=True (so pgid == proc.pid) — this is enforced
        in pg.spawn(). Cleanup is shielded with anyio.CancelScope(shield=True)
        so a re-cancel can't leak children.

    Example:
        >>> async with ProcessGroupScope(f"eval-{sprint_seq}") as pg:
        ...     proc = await pg.spawn([sys.executable, "-m", "harness_mcp.evaluator_runner"], ...)
        ...     await pg.communicate(proc, payload_json)
        ...     rc = await proc.wait()
    """
```

macOS subprocesses launched with `start_new_session=True` form a real session leader, so killpg propagates correctly to `posix_spawn`-launched grandchildren (Playwright Chromium uses posix_spawn).

**`ClaudeSDKClient` interrupt under cancel**: when sprint timeout or cancel_build fires, the orchestrator (running in the *parent*) sends SIGTERM to the launcher's pgroup. The launcher's `ClaudeSDKClient.__aexit__` runs in the launcher's own process — if you ever drive ClaudeSDKClient *in* the orchestrator process (e.g., for Planner/Reviewer/Summarizer where there's no launcher), wrap the cleanup interrupt in a shielded scope so the cancel doesn't re-fire mid-cleanup:

```python
# In Planner/Reviewer/Summarizer paths only (Evaluator goes through launcher):
try:
    ...
finally:
    with anyio.CancelScope(shield=True):
        with anyio.move_on_after(5):
            await client.interrupt()
```

## 9. Prompts (5 files inside the package)

All loaded at agent-spawn time so users can hot-edit between jobs. None are ever inlined in code.

**Location.** Inside the package at `src/harness_mcp/prompts/` (NOT a sibling of `src/`). This is the only way `pip install`/`uv pip install` reliably ships them. With the `hatchling` build backend (§11.1), files inside `src/harness_mcp/` are picked up automatically — no extra `pyproject.toml` block needed.

**Resolution.** `prompts_loader.py` exposes two helpers:
```python
from importlib.resources import files

_PROMPTS_ROOT = files("harness_mcp") / "prompts"

def _resolved_prompt(name: str) -> Path:
    """Return the absolute path of a prompt file (for tooling that wants a path)."""
    p = Path(str(_PROMPTS_ROOT / name))
    if not p.is_file():
        raise PromptNotFoundError(f"prompt {name} missing at {p}")
    return p

def _resolved_prompt_text(name: str) -> str:
    """Read the prompt's contents fresh at every call (supports hot-edits)."""
    return _resolved_prompt(name).read_text(encoding="utf-8")
```

**Why we read contents, not pass paths.** The `claude_agent_sdk`'s `ClaudeAgentOptions.system_prompt` accepts only (a) a plain string, or (b) `{"type": "preset", "preset": ..., "append": ...}`. The `{"type": "file", "path": ...}` form documented in some docs examples is **not implemented in the SDK's command builder** — it is silently ignored, leaving the spawned agent with no system prompt at all. So every spawn site must pass `system_prompt = _resolved_prompt_text("<name>")` (a string). Hot-edits work because we re-read the file on every spawn; we don't cache content. Bare relative paths would not work either way (they'd resolve against the spawned agent's cwd, which is wrong) — strings sidestep that entirely.

| File | Used by | Key contents |
|---|---|---|
| `planner.md` | Planner (`query()`) | "Invoke `superpowers:writing-plans` via the Skill tool. Each feature = one Sprint. Use `## Sprint N: <Title>` H2 markers. Stay inside cwd." |
| `reviewer.md` | Reviewer (`query()`) | Embeds `plan-document-reviewer-prompt.md` verbatim from the bundled `superpowers:writing-plans` skill (sourced at build time from the path resolved by `_resolved_prompt`-equivalent logic against the installed superpowers plugin; pin the source version in §0). Adds: "Every issue under `**Issues (if any):**` must start with `[implementation]` or `[design]`. If unsure, use `[implementation]`." |
| `evaluator.md` | Evaluator (contract review, static, dynamic) | "Be skeptical. LLM evaluators are lenient by default — you override that. Hard pass/fail per criterion. For dynamic: write a routing-decision paragraph first, then exercise chosen tools. Try to kill processes you start; orchestrator will SIGTERM the group if you don't." |
| `generator.md` | Generator (leading user message of every Codex chunk) | "All work in cwd `app/`. Write `handoff-NNN.md` with status=done OR status=in-progress + next-steps when you stop. On retry, read eval.md and address failed criteria without expanding scope." |
| `summarizer.md` | Summarizer (`query()`) | "Read design.md, plan.md, every sprint eval.md. Output 2–3 sentences: what was built, sprint pass/fail count, what's incomplete." |

## 10. Safety Controls & Prerequisites

### 10.1 Startup prereq sequence (lifespan, fail fast)

In order. Any failure → server refuses to start, logs the exact fix, exits non-zero.

1. **Path resolution.** Resolve `~/.harness` to absolute. `mkdir -p` jobs/. Open SQLite (WAL mode). For v1: `CREATE TABLE IF NOT EXISTS` for `jobs` and `sprints` per the §4.2 schema (idempotent; safe across server restarts). No migration framework yet — when v2 schema lands, add a `schema_version` table and an ordered migration list to `state.py`. The v1 → v2 migration step will be the first time we need it; keeping it absent now avoids over-engineering.
2. **Environment.** `ANTHROPIC_API_KEY` set and non-empty. (Note: Codex uses its own auth in `~/.codex/`; no `OPENAI_API_KEY` needed.)

   **2a. Codex binary.** Resolve `HARNESS_CODEX_BIN` if set, else `which codex`. Run `<bin> --version` with 5s timeout. Read `~/.codex/config.toml` (warn-only if missing — codex falls back to defaults; we still force `sandbox=workspace-write` and `approval_policy=never` per §10.5).

   **2b. Codex SDK shape probe.** Construct `AppServerConfig(codex_bin=..., cwd=<tmp git repo>, config_overrides=(...), client_name="harness-mcp", ...)`. Open `AsyncCodex(config=cfg)` as `async with`, call `await codex.thread_start()` (no model param), then attempt a small write inside the temp git repo via `await thread.turn(TextInput("write a file `probe.txt` containing the word `ok` and exit"))`. Verifies (a) tuple `config_overrides`, (b) `thread_start()` works without explicit model, (c) binary runs, (d) sandbox override actually permits writes (a correctly-named-but-ignored override is the dangerous case — accepted at the API but silently no-op'd, leaving Codex on its default `read-only` sandbox).

   The override-key form differs across Codex versions: the TOML field is `sandbox_mode`, the CLI flag is `--sandbox-mode`, and `--config sandbox=...` is sometimes accepted as alias. Probe matrix (try in order, keep the first form that successfully writes the file):
   1. `("sandbox_mode=workspace-write", "approval_policy=never")` — TOML field name, hyphenated value.
   2. `("sandbox_mode=workspaceWrite", "approval_policy=never")` — TOML field name, camelCase value.
   3. `("sandbox=workspace-write", "approval_policy=never")` — alias key, hyphenated value.
   4. `("sandbox=workspaceWrite", "approval_policy=never")` — alias key, camelCase value.

   Pin the working `config_overrides` tuple into a module-level constant `_CODEX_CONFIG_OVERRIDES` that §6.1 / §7 reference rather than hardcoding the strings. If all forms either fail outright or accept-but-silently-ignore (probe.txt not written), refuse startup with the last SDK exception or `"Codex sandbox override accepted but file write failed; sandbox may be silently ignored. Verify Codex version (>= ?)."`
3. **uv.** `which uv`. Recommended (warn-only); needed for the smoke test.
4. **`superpowers:writing-plans` skill probe.**
   - Skills and slash commands are unified — both surface in `get_server_info()["commands"]` per current Claude Code docs.
   - Boot transient `ClaudeSDKClient` with `setting_sources=["user"]`. Some SDK versions require an initial query to fully establish the connection — issue a no-op probe query first (`await client.query("ready?")` and drain `receive_response()` once), then call `await client.get_server_info()`. Check whether `superpowers:writing-plans` appears in the `commands` list.
   - We deliberately do **not** fall back to `setting_sources=["user", "project"]`. The probe runs in the daemon's cwd, but spawned agents run with `cwd=jobs/<id>/` — `project` source would resolve to the job dir's nearest `.claude/` ancestor, which typically won't exist, so a `project`-resolved skill at probe time wouldn't load at spawn time. To avoid this silent mismatch, harness-mcp requires `superpowers:writing-plans` to be installed at user scope. Daemon-mode users with project-only installs see a clear `"skill must be installed at user scope (not project-only) for harness-mcp to work"` error.
   - Record `_resolved_setting_sources = ["user"]` for all subsequent spawns.
   - If `commands` doesn't surface the skill (defensive: SDK behavior may evolve), fall back to a prose probe:
     ```
     prompt = ("List every skill name available to you, one per line, "
               "no commentary. Format each line as exactly: SKILL: <full-name>")
     ```
     Parse with `re.findall(r'^SKILL:\s*([\w:_-]+)\s*$', resp, re.MULTILINE)`. Strict prefix keeps parsing robust against extra prose. Probe runs with `permission_mode="acceptEdits"`, `mcp_servers={}`, `extra_args={"strict-mcp-config": None}`, `setting_sources=["user"]` (matching the production spawn config), no extra tools. Drain `query()` via `async for msg in query(...)` to actually run it. The prose probe is expected to be dead code under current SDK behavior; it exists as forward-compat insurance only.
   - Whichever combination resolves the skill is recorded as `_resolved_setting_sources` and used for every Planner/Reviewer/Evaluator/Summarizer spawn.
   - If neither path (the `commands` lookup nor the prose-probe fallback) resolves the skill at user scope, refuse startup with: `"Install superpowers plugin at user scope: <link>"`.
5. **MCP server probe & capture.**
   - Boot transient `ClaudeSDKClient` with `setting_sources=_resolved_setting_sources`. Send a no-op probe query first (drain via `async for msg in client.receive_response()`) to ensure the SDK's connection is fully established, then `await client.get_mcp_status()`.
   - Response shape (per public `claude_agent_sdk` examples): a dict with key `"mcpServers"`. Iterate as `for entry in mcp_status.get("mcpServers", []):`. Each `entry` has `name: str` and `status: str` for sure. Some SDK versions also expose a `config` field on each entry carrying the full resolved stanza (command/args/env for stdio; url for HTTP/SSE) — **this is what we want, but it's not guaranteed by the public docs**.
   - **Capture algorithm** (probe optimistically, fall back to file parse):
     1. If `entry.get("config")` is non-empty, use it directly. Done.
     2. Else parse the user's MCP config files in this order, taking first hit:
        - `~/.claude.json` — top-level `mcpServers.<name>` key (Claude Code's user-scope MCP config lives here per current docs); also check `projects.<cwd>.mcpServers.<name>` if running in a project tree.
        - `<project_root>/.mcp.json` — project-scope `mcpServers.<name>`.
        - `~/.claude/plugins/cache/**/.mcp.json` — plugin-shipped MCP configs (sibling of `plugin.json`).
        - `~/.claude/plugins/cache/**/.claude-plugin/plugin.json` — inline `mcpServers.<name>` (rarer).
     3. If neither yields a stanza but the server is `connected`: refuse startup with a clear "we can't capture config for <name> even though it's running; please add an explicit stanza to ~/.claude.json mcpServers".
   - The fallback path is the practical default — most existing setups have stanzas in `~/.claude.json` rather than relying on a possibly-newer SDK `config` field.
   - **context7** (hard): assert one entry with `name == "context7"` and `status == "connected"`. Capture into `_captured_mcp["context7"]` per the algorithm above.
   - **playwright** (soft): same; if absent or disconnected, log warning. Will hard-fail later only if a sprint's Evaluator routes to it (§6.3).
   - **Secrets handling.** Captured stanzas may contain API keys, OAuth tokens, paths to credential files. Treat as secrets in transit: pass verbatim to spawned agents (required for them to call the MCP server) but **never log them**. Log lines about MCP capture include only `name` and `status`, never the full stanza dict. `_captured_mcp` is excluded from any `repr()` / error-message inclusion (use `mcp_servers=<redacted>` placeholder).

   **5b. MCP merge-semantics assertion.** With `extra_args={"strict-mcp-config": None}` (mapping to the SDK's `--strict-mcp-config` CLI flag), explicit `mcp_servers` overrides any settings-file inheritance. We use this flag on every spawn to enforce the no-recursion rule (§10.3). At startup, assertion probe: boot a transient `ClaudeSDKClient` with `setting_sources=_resolved_setting_sources` AND `mcp_servers={"context7": _captured_mcp["context7"]}` AND `extra_args={"strict-mcp-config": None}`. Send a no-op probe query, drain it, then call `get_mcp_status()`. Assert exactly one server name returned. If extra servers leak through, refuse startup with `"strict-mcp-config flag did not enforce override; SDK behavior unexpected. Update the dep or report a bug."`
6. **Restart sweep.** `UPDATE jobs SET status='interrupted', last_message='server restarted before job could finish', finished_at=? WHERE status='running'` bound to `(now_ms(),)`. SQLite has no `NOW()` function — every timestamp the schema stores is an epoch-ms integer injected from Python via parameter binding (`now_ms()` returns `int(time.time() * 1000)`).

### 10.2 Per-job options (all configurable in `start_build`'s `options` arg)

| Knob | Default | Behavior |
|---|---|---|
| `max_sprints` | 10 | Reviewer revises if plan has more sprint markers than this. |
| `max_sprint_duration_minutes` | 45 | `anyio.fail_after()` around each sprint attempt. |
| `max_contract_negotiation_rounds` | 3 | Mutual `APPROVED` required to seal. |
| `max_sprint_retries` | 2 | Beyond → job `failed`. |
| `max_plan_review_rounds` | 5 | Beyond → job `failed`. |
| `codex_reset_steps` | 60 | Per-chunk agentic-step cap (Generator). Counted from `item/started` events; see §7.2. |
| `codex_reset_minutes` | 25 | Per-chunk wall-clock cap (Generator). |
| `max_codex_chunks_per_sprint` | 8 | Hard cap on chunk loop. Beyond → sprint failed. |
| `max_negotiation_turns` | 3 | Per-round Codex/Claude turn cap during contract negotiation (§6.1). Hitting cap = use partial body, don't crash. |
| `max_evaluation_seconds` | 1800 | Bounds the Evaluator phase (both static + dynamic queries together). The launcher subprocess (§8.4) enforces this internally via `anyio.fail_after(max_evaluation_seconds)` around the `ClaudeSDKClient` block. The orchestrator wraps `proc.wait()` in `anyio.fail_after(max_evaluation_seconds + 60)` (the +60s buffer covers fork/exec + SDK init). |

### 10.3 No agent recursion

Spawned agents never receive `harness-mcp` in their `mcp_servers` argument. The orchestrator hardcodes the allowlist (§10.4) and explicitly omits any reference to itself.

### 10.4 MCP server passthrough — explicit allowlist

Despite using `setting_sources=_resolved_setting_sources` for skill loading, **MCP servers are NOT inherited**. The orchestrator captures `context7` (and `playwright` when present) at startup and passes them as an explicit `mcp_servers={...}` to every spawned agent. Combined with `extra_args={"strict-mcp-config": None}`, this forecloses the recursion risk where a Claude agent inheriting harness-mcp from user settings could call `cancel_build` on its own job or start nested jobs.

Per role:
- Planner / Reviewer / Summarizer: `{"context7": <captured>}`.
- Evaluator: `{"context7": <captured>, "playwright": <captured if present>}`.
- Generator: Codex reads its MCP config from `~/.codex/config.toml`. We do not override; the spec asks us to honor the user's codex configuration for everything except sandbox/approval.

### 10.5 Codex configuration overrides

We pass `config_overrides=("sandbox=workspace-write", "approval_policy=never")` on every Codex thread:

- **`sandbox=workspace-write`** — guarantees Codex can edit files in `cwd` but not arbitrary paths, regardless of user's setting. Paired with `cwd=<jobs/<id>/app>` to scope writes.
- **`approval_policy=never`** — required for unattended autonomous operation. Without it, Codex can hang indefinitely on a prompt the user never sees.

We deliberately do **not** override `model` — the spec says honor the user's codex model choice, and they've expressed it in `~/.codex/config.toml`.

### 10.6 Sandbox enforcement (honest documentation)

The harness enforces working directory via:
- `cwd = <appropriate dir>` on every spawned agent.
- `permission_mode = "acceptEdits"` for Planner / Reviewer / Summarizer (auto-approves Edit/Write/NotebookEdit, does NOT cover Bash). `permission_mode = "bypassPermissions"` for Evaluator only — it needs Bash to start the app under test, run pytest, drive Playwright. The hard cwd boundary plus the launcher process group cleanup is the real safety net for Evaluator; bypassing prompts isn't a safety regression at this level.
- System prompt instructions ("do not write outside cwd").

A determined agent could escape via `Bash` calls. This is **best-effort**, not OS-level isolation. README documents this honestly. If stronger isolation is needed later, the natural path is per-job containers (out of scope for v1).

### 10.7 Graceful shutdown

On SIGTERM (or `KeyboardInterrupt` in dev):
1. Stop accepting new tool calls.
2. For each running orchestrator coroutine: cancel the task, which propagates to child agent processes via SDK interrupts.
3. For each `running` job in SQLite: mark `interrupted` with `last_message="server received SIGTERM"`, `finished_at=now`.
4. `WAL` checkpoint, close SQLite.
5. Exit. If shutdown exceeds 30s, force-exit (no data corruption thanks to per-line log flushing + WAL).

## 11. Project Structure

```
harness/
  pyproject.toml                  # uv-managed; ruff configured
  README.md                       # setup, env vars, mcp.json examples, "Notable decisions"
  uv.lock
  src/harness_mcp/
    __init__.py                   # __version__
    __main__.py                   # CLI: harness-mcp [serve|doctor]
    server.py                     # FastMCP app, lifespan, tool defs, error mapper
    evaluator_runner.py           # entry point for the Evaluator launcher subprocess (§8.4 — always used for Stage 3 evaluation)
    config.py                     # paths, JobOptions dataclass, defaults (single home)
    state.py                      # SQLite schema + helpers; Status/Phase enums; db_write helper
    orchestrator.py               # per-job coroutine, top-level flow, cancel registry
    planning.py                   # plan + review loop (§5)
    sprints.py                    # sprint loop driver (§6)
    contracts.py                  # body extraction, round parsing, APPROVED detection
    generator.py                  # AsyncCodex wrapper, chunk loop, handoff parsing (§7)
    evaluator.py                  # ClaudeSDKClient wrapper, eval.md parsing, sync_eval_md (§8)
    summarizer.py                 # final summary (§6.6)
    prereqs.py                    # lifespan startup checks (§10.1)
    prompts_loader.py             # importlib.resources resolution; absolute paths (§9)
    logging_setup.py              # EventLogger for Codex stream → log.txt (§7.5)
    process_group.py              # ProcessGroupScope ctx manager (§8.4); exports PIPE = subprocess.PIPE for spawn() callers
    mcp_capture.py                # captures user MCP server stanzas at startup (§10.1 step 5)
    types.py                      # ImplementationResult, EvaluationResult, Criterion, Handoff, errors
    prompts/                      # SHIPPED INSIDE THE PACKAGE (§9)
      planner.md
      reviewer.md
      evaluator.md
      generator.md
      summarizer.md
  examples/
    todo-app-design.md
    mcp.json.stdio
    mcp.json.streamable-http
  tests/
    conftest.py                   # tmp ~/.harness fixture, mock SDK fixtures
    test_state.py                 # SQLite schema, transitions
    test_planning.py              # plan validation, review parsing, tag filtering
    test_contracts.py             # APPROVED parser, round headers
    test_generator.py             # handoff parsing, chunk loop with mocked Codex
    test_evaluator.py             # eval.md parsing, routing-decision extraction
    test_orchestrator.py          # full state machine with all SDKs mocked
    smoke.py                      # real end-to-end against examples/todo-app-design.md
```

### 11.0 Type definitions (`types.py`)

```python
@dataclass(frozen=True)
class ImplementationResult:
    ok: bool
    files_touched: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    summary: str = ""
    error: str | None = None    # set when ok=False

@dataclass(frozen=True)
class Criterion:
    text: str           # from contract.md
    result: str         # "PASS" | "FAIL"
    evidence: str
    notes: str

@dataclass(frozen=True)
class EvaluationResult:
    sprint_seq: int
    static_criteria: list[Criterion]
    dynamic_criteria: list[Criterion]
    routing_decision: str       # one paragraph
    passed: bool                # derived: all criteria PASS in both lists
    unparseable: bool = False

@dataclass(frozen=True)
class Handoff:
    chunk_seq: int
    status: str                 # "in-progress" | "done"
    summary: str
    work_done: list[str]
    decisions: list[str]
    files_touched: list[tuple[str, str]]
    open_questions: list[str]
    next_steps: list[str]
    declares_done: bool         # status == "done"
```

`JobOptions` lives in `config.py` (next to defaults), not `types.py`. Errors live in `types.py` (not `server.py`) so non-server modules can raise them without import cycles.

### 11.1 `pyproject.toml` highlights

- `requires-python = ">=3.12"`
- Dependencies (versions pinned at implementation time; revisit on upgrade per §0):
  - `mcp` (FastMCP)
  - `claude-agent-sdk`
  - `codex-app-server`
  - `anyio`
  - `httpx`
  - `python-ulid`
  - `pytest`, `pytest-asyncio` (test extras)
  - Stdlib for everything else.
- Build backend: `hatchling`. Because `prompts/` lives inside `src/harness_mcp/`, Hatch's default file inclusion picks up `*.md` automatically:
  ```toml
  [tool.hatch.build.targets.wheel]
  packages = ["src/harness_mcp"]
  ```
  No `force-include` or `package-data` block needed.
- `[tool.ruff]`:
  - `line-length = 100`
  - `select = ["E","F","W","I","B","UP","SIM","ASYNC","PL","RUF","ANN"]` — `ANN401` enforces the brief's "no `Any` leakage in public function signatures" rule. (`per-file-ignores` opts tests out of `ANN401`.)
  - Formatter on. **Both `ruff check .` and `ruff format --check .` must report zero findings before any commit; this is a hard CI gate per the brief.**
- `[project.scripts] harness-mcp = "harness_mcp.__main__:main"`.
- CI smoke (in README): `uv run ruff check . && uv run ruff format --check . && uv run pytest -k 'not smoke'`. Identical line in pre-commit hook recommended.

### 11.2 Docstring requirement

Every public function, method, and class has a docstring with three sections in this order:

```
Design:
    <why this exists, what tradeoffs it encodes>

Implementation:
    <how it works>

Example:
    >>> ...
```

Trivial-helper exception: if a function is too small to need this, inline it instead. No untyped, undocumented public functions.

## 12. Testing

### 12.1 Unit tests (`uv run pytest`)

Mock both SDKs via fixture-replaced module-level imports. Coverage:
- State transitions (every entry point of the state machine).
- Prereq-check failure paths (each step's failure mode).
- Sprint-marker regex.
- Plan-review tag filter (including untagged-defaults-to-implementation, all-design-drops-loop).
- Contract APPROVED parser including malformed cases (whitespace, mixed case, partial APPROVED).
- eval.md parser including unparseable cases (no `### Criterion`, missing `**Result:**`, FAIL outside expected blocks).
- Chunk-loop exit conditions (turn count, wall clock, status=done, max_codex_chunks_per_sprint).
- Orchestrator flow under all four terminal statuses.
- Skill-invocation post-hoc verification: present, absent, malformed.
- Process-group cleanup: spawn fake child, ensure it's reaped on context exit.
- Cancel registry behavior: terminal-branch idempotency, pending-branch race (CAS lost), running-branch scope lookup, registry deregistration after completion (§3.2).
- Launcher import isolation: assert `evaluator_runner` does not transitively import `harness_mcp.state` (§8.4 launcher SQLite isolation note).

### 12.2 Smoke test (`uv run python tests/smoke.py`)

Real end-to-end run against `examples/todo-app-design.md` (deliberately trivial Flask TODO list with both a form/list view UI and a `/api/todos` REST endpoint, so the smoke run exercises Playwright routing for at least one sprint).

Asserts (in this order):
1. **Prereq checks pass via the same code path the server uses.** Smoke test invokes `harness-mcp doctor` as a subprocess; assert `returncode == 0` and `stdout` contains `OK` lines for each prereq step (1 through 5b). This confirms the lifespan startup contract works end-to-end.
2. `start_build(design_doc_path=<abs path to examples/todo-app-design.md>, options={})` returns a 26-char ULID `job_id`.
3. `poll_build(job_id)` advances through phases over time. Capture phase strings; assert that at least `planning`, `plan-review`, `sprint-1/contract`, `sprint-1/implementing`, `sprint-1/eval-static`, `sprint-1/eval-dynamic`, `summarizing`, `done` appear (or a subset if sprints are sparse).
4. While `poll_build` returns non-terminal status, `get_build_result(job_id)` raises an MCP tool error with `code == "JOB_NOT_FINISHED"`.
5. After completion, `get_build_result` returns `final_status="completed"`, `app_path` points to a directory containing a runnable Python TODO app, and `summary` is a non-empty string ≥ 30 chars.

`cancel_build` is **not** exercised by smoke (per the brief's "start/poll/get" wording). A separate manual test in `tests/cancel_smoke.py` covers it.

Manual run only: `uv run python tests/smoke.py`. Not in default `pytest` collection (excluded via `-k 'not smoke'`).

### 12.3 `examples/todo-app-design.md` skeleton

Sketched here so the implementer doesn't re-decide; expand inline when authoring the actual file.

```markdown
# TODO App — Design Document

## Goal

A single-process Python web app for managing a personal todo list.
Single-user, no auth, runs locally on http://127.0.0.1:5000.

## User stories

- I can see all my todos on the home page.
- I can add a new todo via a form.
- I can mark a todo as done.
- I can delete a todo.

## Functional requirements

1. **Web UI** at GET `/` — renders list of todos with checkbox + delete button per item, plus a "new todo" form at the top.
2. **REST API** at:
   - GET `/api/todos` — JSON array of `{id, text, done}`.
   - POST `/api/todos` — body `{text}`, returns the created todo.
   - PATCH `/api/todos/<id>` — body `{done: bool}`, returns updated.
   - DELETE `/api/todos/<id>` — returns `{ok: true}`.
3. **Persistence** in SQLite at `./todos.db`, schema `todos(id INTEGER PK, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)`.

## Non-functional requirements

- Python 3.12, Flask 3.x.
- Single file deployable: `python app.py` starts the server.
- All runtime AND test dependencies in `requirements.txt` (Flask, pytest, ...; smoke test installs via `uv pip install -r`). The Generator must include `pytest` because acceptance criteria below depend on running it.
- Tests in `tests/` covering each endpoint (pytest + Flask test client).

## Acceptance criteria (the harness will verify these)

- The home page renders without 500.
- POSTing a todo via form makes it appear on the list after redirect.
- The DELETE endpoint removes the row from SQLite.
- `pytest` from the project root passes with zero failures.
```

### 12.4 `examples/mcp.json` snippets

**stdio variant** (`examples/mcp.json.stdio`):
```json
{
  "mcpServers": {
    "harness-mcp": {
      "command": "harness-mcp",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

`ANTHROPIC_API_KEY` (required) and `HARNESS_CODEX_BIN` (optional) must be set in the parent client's environment — most MCP clients do not perform shell-style `${VAR}` substitution inside JSON values, so don't put them there. README's "Setup" section documents this. Clients that *do* support env (Claude Code, Continue) typically have a UI for it outside the JSON.

**streamable-http variant** (`examples/mcp.json.streamable-http`):
```json
{
  "mcpServers": {
    "harness-mcp": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

The HTTP daemon is started independently:
```bash
ANTHROPIC_API_KEY=... harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

## 13. Notable Decisions

Each is a one-paragraph entry in the README's "Notable decisions" section.

1. **File-mediated handoffs over message-passing.** Auditability + survives agent restarts + lets us run unit tests on the parsers.
2. **Reset over compaction for context anxiety.** Per article: compaction preserves the anxious "I've been working a long time" feel. Only full resets clear it.
3. **Static audit before dynamic verification.** Catching "code looks plausible but skipped requirement X" before booting the app is cheaper and more reliable than discovering it via behavioral testing.
4. **Evaluator-managed app lifecycle + orchestrator process-group cleanup.** Flexibility per project shape; deterministic cleanup against leaked dev servers and orphan Playwright browsers.
5. **One ClaudeSDKClient across static→dynamic.** The static audit's reasoning is high-value context for the dynamic pass.
6. **Contract-round file ownership by orchestrator.** Agents emit messages; orchestrator owns structure. Lets us run cheap structural validations.
7. **Force `sandbox=workspace-write` + `approval_policy=never` for Codex; honor user `model` choice.** Autonomous-run requirements override user preferences only where required for unattended operation. Documented prominently.
8. **Explicit MCP allowlist for spawned agents.** Closes the agent-recursion door even though the user has harness-mcp in their settings.
9. **ULID job IDs.** Sortable directory listings, no extra index needed.
10. **LLM-generated summary.** Costs one extra Claude call; produces a far more digestible job-end readout than mechanical concatenation.
11. **`plan-document-reviewer-prompt.md` over `code-review:code-review`.** The latter is built for GitHub PRs (uses `gh`, posts comments back). The former is the purpose-built plan-doc reviewer template that ships in `superpowers:writing-plans`.
12. **Two transports (stdio default, streamable-http for daemon use).** Stdio for ad-hoc; HTTP daemon for multi-hour jobs that should survive client disconnects.
13. **Untagged reviewer issues default to `[implementation]`.** Conservative under uncertainty — better to do an extra revision round than to silently drop a real issue.
14. **No tool restrictions on spawned Claude agents (preset claude_code tool set).** System prompts are the guardrail. Tradeoff acknowledged: a determined agent could escape via Bash; sandbox is best-effort, not OS-level.
15. **Concurrent UI-bearing jobs may contend on Playwright MCP.** Documented operational caveat: if running multiple jobs in parallel that both reach dynamic-verification UI sprints simultaneously, expect Playwright resource conflicts. Future work could serialize via a semaphore.

## 14. README outline

The README is a deliverable per the brief. Structure it as follows; "Notable decisions" is §13 above and gets pasted in verbatim.

```
# harness-mcp

<Two-sentence elevator pitch.>

## Setup

1. Install Codex CLI (https://...) and confirm `codex --version` works.
2. Configure ~/.codex/config.toml — at minimum, set your model. Example below.
3. Install harness-mcp with uv:
   uv pip install harness-mcp     # or: uv pip install -e . from a checkout
4. Verify everything: harness-mcp doctor

## Required environment variables

| Var | Required | Purpose |
|---|---|---|
| ANTHROPIC_API_KEY | yes | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
| HARNESS_CODEX_BIN | no  | Override `which codex`. Useful when codex isn't on PATH. |

(Codex auth lives in ~/.codex/auth.json; no OPENAI_API_KEY needed.)

## Required MCP servers

- context7 (HARD): used by Planner and Generator for library documentation.
- playwright (SOFT): used by Evaluator for dynamic verification of UI-bearing sprints. Optional — the server warns at startup if missing and only hard-fails when a sprint actually needs it.

The harness reads these from your existing Claude Code settings; no separate config file.

## Required skills

- superpowers:writing-plans (HARD): used by Planner. Install: `claude plugins install superpowers` (or whatever the current install command is).

## Example mcp.json

(stdio and streamable-http variants — see §12.4.)

## Quickstart

1. Write a design document (markdown).
2. From your client, call `start_build(design_doc_path="<abs>")`.
3. Poll with `poll_build(job_id)` until status is terminal.
4. `get_build_result(job_id)` returns the final summary and app path.

## Notable decisions

(Verbatim copy of §13.)

## Troubleshooting

- "context7 not connected" — check Claude Code's MCP config; `harness-mcp doctor` shows the resolution path.
- Codex hangs — ensure `~/.codex/config.toml` doesn't have `approval_policy=on-request`; the harness forces `never` regardless.
- Playwright tests fail with "browser not found" — reinstall Playwright browsers via the playwright MCP plugin's install command.

## Limitations

- Sandbox is best-effort (cwd + system prompt + permission_mode), not OS-level. A determined agent can escape via Bash. For stronger isolation, run harness-mcp inside a container.
- Concurrent UI-bearing jobs contend on the single Playwright MCP. Run UI-heavy jobs sequentially.
- Workers die with the server: closing your client (under stdio transport) ends in-flight jobs as `interrupted`. Use streamable-http daemon mode for multi-hour jobs.
```

The actual README expansion is the implementer's job; this outline pins the structure and ensures every brief-mandated section appears.
