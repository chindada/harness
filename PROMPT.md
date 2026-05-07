# Build an MCP Server for a Long-Running Multi-Agent Coding Harness

## Goal

Build an MCP server, in **Python** managed with **uv**, that exposes a tool `build_application`. The tool takes the path to a **feature design document** and produces a working full-stack application via a multi-agent harness inspired by Anthropic's "Harness design for long-running application development" (March 2026).

The server orchestrates three agent roles:

- **Planner** — Claude Agent SDK. Reads the design document, then uses the `superpowers:writing-plans` skill to produce an implementation plan.
- **Generator** — **Codex Agent SDK** running against a local `codex` binary. Implements the application feature by feature according to the plan.
- **Evaluator** — Claude Agent SDK. First audits the gap between the implemented code and the design/plan/contract, then verifies behavior dynamically. Uses Playwright MCP only for UI-bearing features; uses direct API / CLI / test-suite verification for backend-only features.

After the plan is written, a **code-review loop** runs before any code generation begins. Implementation only starts once the plan is clean.

## Reference reading (do this first, before writing any code)

Fetch and read fully:

1. https://www.anthropic.com/engineering/harness-design-long-running-apps — the source pattern.
2. Claude Agent SDK overview: https://platform.claude.com/docs/en/agent-sdk/overview
3. **Codex Agent SDK** — confirm the current API surface from the latest official docs. Specifically verify the `AppServerConfig(codex_bin=...)` entry point. Do not reconstruct the API from training data; the SDK has been changing.
4. **Use the context7 MCP server throughout development** to look up current, accurate documentation for any library you touch (Codex SDK, Claude Agent SDK, MCP SDK, Playwright MCP, uv, ruff). Prefer context7 over web search when the topic is a library API. Treat context7 as the authoritative source for fitting best practice in third-party libraries.

## Hard prerequisites — fail fast at startup

The server must verify these on startup and exit with a clear, actionable error if any are missing. Do not paper over a missing prerequisite with a fallback.

- Skill `superpowers:writing-plans` is installed and resolvable.
- Skill `code-review:code-review` is installed and resolvable.
- A `codex` binary is on `PATH` or pointed at via `HARNESS_CODEX_BIN`.
- `ANTHROPIC_API_KEY` is set in the environment.
- `playwright` MCP is configured and reachable for the Evaluator. **Soft requirement**: log a warning at startup if missing, but do not abort. A pure-backend project never needs it. The hard failure happens later, at sprint evaluation time, only if a sprint's contract contains UI criteria and Playwright is unreachable.
- `context7` MCP is configured and reachable for Planner and Generator.

If a skill is missing: return an MCP error naming the missing skill and the install command (or instructions). Do **not** silently degrade.

## Storage layout

All artifacts live under `~/.harness`. Resolve `~` once at startup to an absolute path.

```
~/.harness/
  state.db                       # SQLite: jobs, sprints, status
  jobs/
    <job_id>/
      design.md                  # The input design document (copied verbatim)
      plan.md                    # Planner output (final, post-review)
      plan-history/              # Each review iteration archived for debugging
        plan-v1.md
        review-v1.md
        plan-v2.md
        ...
      sprint-N/
        contract.md              # Negotiated between Generator and Evaluator
        handoff-<seq>.md         # Generator's internal reset handoffs
        eval.md                  # Evaluator findings
        log.txt                  # Streamed agent output
      app/                       # The actual generated application
```

Neither Codex nor Claude may write outside the relevant `~/.harness/jobs/<job_id>/` directory.

## MCP tool surface

A full run is multi-hour. Synchronous MCP tool calls will time out. Expose:

- `start_build(design_doc_path, options) -> { job_id }` — `design_doc_path` is an absolute path to a local markdown file.
- `poll_build(job_id) -> { status, current_phase, last_message, sprints_completed }`
- `get_build_result(job_id) -> { app_path, summary, final_status }`
- `cancel_build(job_id) -> { ok }`

Persist job state in SQLite so polls survive server restarts.

## Workflow

### Phase 1 — Planning

1. Copy the design document to `jobs/<job_id>/design.md`.
2. Spawn a Claude Agent SDK session for the Planner role. The Planner **must** invoke the `superpowers:writing-plans` skill to produce the implementation plan. Output is `plan-history/plan-v1.md`.

### Phase 2 — Plan review loop

1. Spawn a Claude Agent SDK session for the Reviewer role. It **must** invoke the `code-review:code-review` skill against the latest plan. Output is `plan-history/review-vN.md`.
2. Parse the review output for issues with **score > 0**. Filter out any issue tagged or categorized as **design-related** — design is given by the user and not in scope to revise.
3. If filtered issues remain: spawn a Planner session to revise the plan, addressing each issue. Output `plan-v(N+1).md`. Loop back to step 1.
4. If no filtered issues remain: copy the latest plan to `plan.md` and proceed to Phase 3.
5. Cap iterations at `max_plan_review_rounds` (default 5). On exhaustion, abort the job with the unresolved issues in the error.

### Phase 3 — Sprint loop

For each feature/sprint in `plan.md`:

1. **Contract negotiation.** Generator (Codex) proposes scope and acceptance criteria → writes `contract.md` (draft). Evaluator (Claude) reviews and either approves or annotates pushback → writes back. Iterate up to 3 rounds. On non-convergence, abort the sprint with a clear error.

2. **Implementation.** Generator implements until the contract is met. Apply the reset-and-handoff loop internally if a single Codex run approaches its context limit (see "Context anxiety" below).

3. **Evaluation — two stages, in order.**

   **Stage 3a: Static audit (always).** The Evaluator reads `design.md`, `plan.md`, the sprint's `contract.md`, and the code produced this sprint (the diff against the previous sprint's tree). It identifies:
   - Contract criteria not implemented or only partially implemented.
   - Drift between the design intent and the code (e.g., feature renamed, scope expanded, scope silently dropped).
   - Missing edge-case handling visible at code-read time (no error path, hardcoded values where the design called for configurability, etc.).

   The audit findings go into `eval.md` under a `## Static audit` section. A criterion can already be marked failed at this stage if the audit shows it was not implemented at all — there is no point booting the app.

   **Stage 3b: Dynamic verification.**

   The Evaluator decides for itself whether the sprint has a UI surface that warrants Playwright. It makes this call by reading the contract, the design, and the actual code produced — looking at whether the criteria describe user-visible behavior, whether the implementation actually exposes a UI (HTML templates, frontend routes, served pages), and whether driving the running app would meaningfully verify anything beyond what direct testing already covers.

   The Evaluator records its routing decision at the top of the dynamic verification section in `eval.md` — one short paragraph stating which tools it chose and why — so the call is auditable and a future operator can argue with it.

   Then it executes:
   - If a UI surface is in scope → drive the running app via **Playwright MCP**. If Playwright is unreachable, fail the sprint with a clear error. Do not silently skip.
   - For backend behavior → exercise the implementation directly: run the project's test suite, hit HTTP endpoints with `httpx`, invoke the CLI, inspect database state, whichever fits. **Do not invoke Playwright** for these. Faking a UI test for a backend feature wastes time and produces noise.
   - When both apply, do both, scoped to the relevant criteria.

   Findings go into `eval.md` under a `## Dynamic verification` section, criterion by criterion. Each criterion gets a hard pass/fail. **Any single fail means the sprint fails.**

4. **Retry on fail.** Up to `max_sprint_retries` (default 2). Generator reads `eval.md` and revises. Beyond the cap, abort the job.

## Context anxiety — handled inside the Generator

Spawning a sub-agent does **not** resolve context anxiety; it shifts it to the child. Inside each Generator sprint:

- **Hard sprint scoping** — one feature per sprint, per the article.
- **Reset-and-handoff loop** — at sprint boundary or when a single Codex run approaches its window limit, write a structured handoff file (work done, decisions made, files touched, next steps), terminate the Codex run, spawn a fresh Codex run that reads the handoff file and continues.
- **No reliance on compaction** — the article shows compaction preserves the anxious "I've been working a long time" feel. Use full resets.

## Codex SDK integration

- Use the **local binary** entry point: `AppServerConfig(codex_bin=<resolved path>)`. Resolve from `HARNESS_CODEX_BIN` if set, else from `PATH`.
- **Do not pass a model parameter.** The `codex` binary loads its own model from its own config (e.g., `~/.codex/config.toml`). Honor that — the user has chosen their model intentionally.
- Pipe Codex stdout/stderr into the sprint's `log.txt` line by line.
- Encapsulate the reset-and-handoff loop inside the Generator adapter, so the rest of the orchestrator sees the Generator as a single "implement this contract" function.

## Evaluator quality

LLM evaluators are too lenient out of the box (the article spells this out). Therefore:

- The Evaluator system prompt lives at `prompts/evaluator.md`, version-controlled and tunable without code changes.
- The static audit comes first, every sprint, no exceptions. Catching "code looks plausible but skipped requirement X" before booting the app is cheaper and more reliable than discovering it via behavioral testing.
- Dynamic verification tooling is the Evaluator's call. It inspects the contract, design, and code, decides whether the sprint has a UI surface worth driving, and records its routing decision in `eval.md` so the choice is auditable. Playwright fires only when the Evaluator concludes a UI surface is genuinely in scope. Never on a sprint that the Evaluator has determined is backend-only — it produces nothing useful and adds noise.
- Be skeptical. Probe edge cases. Never approve work that fails the contract — even partially.
- Each contract criterion gets a hard pass/fail. Any single fail means the sprint fails.

## Safety controls

Configurable per job, with sane defaults:

- `max_sprints` (default 10)
- `max_sprint_duration_minutes` (default 45)
- `max_contract_negotiation_rounds` (default 3)
- `max_sprint_retries` (default 2)
- `max_plan_review_rounds` (default 5)
- **No agent recursion** — spawned agents must not call back into this MCP server.
- **Sandboxed working directory** — neither Codex nor Claude may write outside `~/.harness/jobs/<job_id>/`.

## Python tooling — non-negotiable

- **uv** for dependency and venv management. `pyproject.toml`, `uv sync`, `uv run`.
- **ruff** for both linting and formatting. Configure in `pyproject.toml`. CI smoke check: `uv run ruff check .` and `uv run ruff format --check .` must pass with zero findings.
- Python 3.12+ (use whatever current uv resolves; verify via context7).
- Type hints everywhere. Run `mypy` if you want; not required, but no `Any` leakage in public function signatures.
- Minimal dependencies. SDKs + stdlib + sqlite3 + the MCP framework. Resist adding a web framework, ORM, or task queue unless something concrete forces it.

## Docstring requirement — every method

Every method, function, and class **must** have a docstring with three sections in this exact order:

```python
def negotiate_contract(sprint_id: str, plan_section: str) -> Contract:
    """Negotiate the sprint contract between Generator and Evaluator.

    Design:
        Bridges the high-level plan and concrete testable behavior. The
        Generator proposes; the Evaluator pushes back; they iterate. Failure
        to converge is a hard signal that the sprint scope is wrong, so we
        abort rather than guess.

    Implementation:
        File-mediated handoff via `contract.md`. Up to
        `max_contract_negotiation_rounds` rounds. Each round appends to the
        same file with a header marker so the conversation is auditable.
        Returns a `Contract` object only after both agents emit `APPROVED`.

    Example:
        >>> contract = negotiate_contract("sprint-3", plan.section("level_editor"))
        >>> contract.criteria[0].text
        'Rectangle fill tool fills the dragged region with the selected tile'
    """
```

If a method is too trivial to need this, inline it instead. There should be no untyped, undocumented public functions in the codebase.

## Decision-making style for the implementing agent

When you face a design choice — library selection, file layout, async pattern, error handling — pick **the one that does the right thing for long-term maintainability**, not the one that is sweetest or easiest in the moment. If two options are close, write a one-paragraph tradeoff note in the README under "Notable decisions" and pick the more principled one. Justify with reference to the Anthropic article's "every harness component encodes an assumption" lesson where relevant.

## Deliverables

- MCP server source code (Python, uv-managed).
- `pyproject.toml` with ruff configured to pass on the codebase.
- `README.md` with setup, env vars (`ANTHROPIC_API_KEY`, `HARNESS_CODEX_BIN`), required MCP servers (playwright, context7), required skills, and an example `mcp.json` config for clients. Include a "Notable decisions" section.
- `prompts/planner.md`, `prompts/reviewer.md`, `prompts/evaluator.md` as standalone, tunable files.
- A smoke test (`tests/smoke.py` or `tests/smoke.sh`) that exercises the prerequisite checks and the start/poll/get tool surface against a trivial design doc.
- Example design doc at `examples/todo-app-design.md` to use with the smoke test.

## Start by

1. Reading the Anthropic article in full.
2. Verifying the **current** Codex Agent SDK API via context7 (especially `AppServerConfig(codex_bin=...)`).
3. Verifying the current MCP Python SDK shape via context7.
4. Confirming both required skills (`superpowers:writing-plans`, `code-review:code-review`) are resolvable in your environment. If not, stop and tell me how to install them — do not begin coding without them.
5. Sketching the job state machine before writing code.
6. Asking me clarifying questions if any constraint above is ambiguous for your environment.
