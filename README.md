# harness-mcp

An MCP server that orchestrates multi-hour, multi-agent application builds from a feature design document. Hand it `design.md`; it spawns Planner/Reviewer/Generator/Evaluator agents in a loop until the app passes hard pass/fail criteria for every feature.

## Setup

1. Install the Codex CLI (https://github.com/openai/codex) and confirm `codex --version` works.
2. Configure `~/.codex/config.toml` — at minimum set your model. Example:

   ```toml
   model = "claude-sonnet-4-6"
   model_provider = "anthropic"
   ```

3. Install harness-mcp with uv:

   ```bash
   uv pip install harness-mcp     # or: uv pip install -e . from a checkout
   ```

4. Verify everything:

   ```bash
   harness-mcp doctor
   ```

   Expected: a list of `OK` lines for paths, env, codex, codex-shape, skill, mcp, strict-mcp-config, restart_sweep.

## Required environment variables

| Var | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Used by Planner / Reviewer / Evaluator / Summarizer (Claude Agent SDK). |
| `HARNESS_CODEX_BIN` | no  | Override `which codex`. Useful when codex isn't on PATH. |

(Codex auth lives in `~/.codex/auth.json`; no `OPENAI_API_KEY` needed.)

## Required MCP servers

- **context7** (HARD): used by Planner and Generator for library documentation.
- **playwright** (SOFT): used by Evaluator for dynamic verification of UI-bearing sprints. Optional — the server warns at startup if missing and only hard-fails when a sprint actually needs it.

The harness reads these from your existing Claude Code settings (`~/.claude.json`); no separate config file needed.

## Required skills

- **superpowers:writing-plans** (HARD): used by Planner. Install via the superpowers plugin at user scope.

## Example mcp.json

stdio (default — child process of your MCP client):

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

streamable-http (daemon — jobs survive client disconnects):

```bash
ANTHROPIC_API_KEY=... harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

```json
{
  "mcpServers": {
    "harness-mcp": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

## Quickstart

1. Write a design document (markdown).
2. From your client, call `start_build(design_doc_path="<absolute path>")`.
3. Poll with `poll_build(job_id)` until status is terminal.
4. `get_build_result(job_id)` returns the final summary, the path to the built app, and per-sprint pass/fail.

## Notable decisions

1. **File-mediated handoffs over message-passing.** Auditability + survives agent restarts + lets us run unit tests on the parsers.
2. **Reset over compaction for context anxiety.** Per the article: compaction preserves the anxious "I've been working a long time" feel. Only full resets clear it.
3. **Static audit before dynamic verification.** Catching "code looks plausible but skipped requirement X" before booting the app is cheaper and more reliable than discovering it via behavioral testing.
4. **Evaluator-managed app lifecycle + orchestrator process-group cleanup.** Flexibility per project shape; deterministic cleanup against leaked dev servers and orphan Playwright browsers.
5. **One ClaudeSDKClient across static→dynamic.** The static audit's reasoning is high-value context for the dynamic pass.
6. **Contract-round file ownership by orchestrator.** Agents emit messages; orchestrator owns structure. Lets us run cheap structural validations.
7. **Force `sandbox=workspace-write` + `approval_policy=never` for Codex; honor user `model` choice.** Autonomous-run requirements override user preferences only where required for unattended operation.
8. **Explicit MCP allowlist for spawned agents.** Closes the agent-recursion door even though the user has harness-mcp in their settings.
9. **ULID job IDs.** Sortable directory listings, no extra index needed.
10. **LLM-generated summary.** Costs one extra Claude call; produces a far more digestible job-end readout than mechanical concatenation.
11. **`plan-document-reviewer-prompt.md` over `code-review:code-review`.** The latter is built for GitHub PRs (uses `gh`, posts comments back). The former is the purpose-built plan-doc reviewer template that ships in `superpowers:writing-plans`.
12. **Two transports (stdio default, streamable-http for daemon use).** Stdio for ad-hoc; HTTP daemon for multi-hour jobs that should survive client disconnects.
13. **Untagged reviewer issues default to `[implementation]`.** Conservative under uncertainty — better to do an extra revision round than to silently drop a real issue.
14. **No tool restrictions on spawned Claude agents (preset claude_code tool set).** System prompts are the guardrail. Tradeoff acknowledged: a determined agent could escape via Bash; sandbox is best-effort, not OS-level.
15. **Concurrent UI-bearing jobs may contend on Playwright MCP.** Documented operational caveat: if running multiple jobs in parallel that both reach dynamic-verification UI sprints simultaneously, expect Playwright resource conflicts.

## Troubleshooting

- **"context7 not connected"** — check Claude Code's MCP config; `harness-mcp doctor` shows the resolution path.
- **Codex hangs** — ensure `~/.codex/config.toml` doesn't have `approval_policy=on-request`; the harness forces `never` regardless, so most hangs trace to the binary not exiting on completion.
- **Playwright tests fail with "browser not found"** — reinstall Playwright browsers via the playwright MCP plugin's install command.

## Limitations

- Sandbox is best-effort (cwd + system prompt + permission_mode), not OS-level. A determined agent can escape via Bash. For stronger isolation, run harness-mcp inside a container.
- Concurrent UI-bearing jobs contend on the single Playwright MCP. Run UI-heavy jobs sequentially.
- Workers die with the server: closing your client (under stdio transport) ends in-flight jobs as `interrupted`. Use streamable-http daemon mode for multi-hour jobs.
