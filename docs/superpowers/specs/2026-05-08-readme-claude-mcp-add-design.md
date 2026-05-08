# README Setup Section — `claude mcp add` Instructions — Design Spec

**Date:** 2026-05-08
**Status:** Approved (brainstorm complete; awaiting implementation plan)

## 1. Goal

Update the project README so a fresh user can go from zero to a working harness-mcp installation by following one linear sequence: clone the repo, install with uv, register with Claude Code via `claude mcp add`, and verify with `harness-mcp doctor`.

Today the README has two gaps:

- It says `uv pip install harness-mcp` works, but the package has a git-dependency on the Codex SDK and is not on PyPI; the line silently fails for anyone who tries it.
- It documents Claude Code registration only as raw `mcp.json` snippets. The current standard is `claude mcp add ...` from the Claude Code CLI, which is what writes `~/.claude.json` — the file the README's own prose says the harness reads from.

This spec covers a documentation change only. No code, dependencies, or runtime behavior change.

## 2. Decisions (carried from brainstorm)

| Question | Choice | Rationale |
| --- | --- | --- |
| Which MCPs get `claude mcp add` instructions? | Just **harness-mcp**. context7 and playwright assumed already wired up. | Smaller scope; matches how most users get those two (plugins). |
| What happens to existing `mcp.json` JSON snippets? | **Replace, but keep one minimal snippet** in a "Manual config (reference)" subsection. | CLI is the right primary path; reference snippet costs little and helps users who commit `.mcp.json`. |
| Transport coverage in `claude mcp add`? | **Both stdio and streamable-http.** | Stdio is the natural quickstart; http is too central to the project's multi-hour value prop to omit. |
| Structural approach? | **Linear restructure** of "Setup" + "Example mcp.json" into a single 5-step Setup section. | Reads top-to-bottom; also drops the broken PyPI line as a side effect. |

## 3. Final shape of the README "Setup" section

The new section replaces the current "Setup" (lines ~5–27) and the "Example mcp.json" section (lines ~49–78) with one consolidated "Setup" containing five numbered steps, followed by a small "Manual config (reference)" subsection.

### 3.1 Step 1 — Install the Codex CLI and configure it

One step covering both substeps from today's README, kept verbatim under a single step heading:

> Install the Codex CLI (https://github.com/openai/codex), confirm `codex --version` works, then configure `~/.codex/config.toml` — at minimum set your model. Example:
>
> ```toml
> model = "claude-sonnet-4-6"
> model_provider = "anthropic"
> ```

This stays one step (not two) so the section remains 5 steps total as designed.

### 3.2 Step 2 — Clone the repo

New step.

> ```bash
> git clone git@github.com:chindada/harness.git
> # or HTTPS: git clone https://github.com/chindada/harness.git
> cd harness
> ```

One-line note immediately after: the editable install in step 3 pulls the Codex SDK from a git dependency, so first-time install takes longer than typical.

### 3.3 Step 3 — Install harness-mcp

> ```bash
> uv tool install --editable .
> ```

The current README's `uv pip install harness-mcp` line is dropped; see §4.

**Why `uv tool install` and not `uv pip install -e .`** (correction applied 2026-05-08 after dogfooding): `uv pip install -e .` installs the `harness-mcp` console script into the project's `.venv/bin/`, which is not on a typical user's PATH. The user's shell can't find `harness-mcp` (so step 5's `harness-mcp doctor` fails with `command not found`), and — more critically — when Claude Code spawns the registered stdio MCP server later, it inherits the user's shell PATH and also can't find the binary. `uv tool install` puts the executable in `~/.local/share/uv/tools/<pkg>/bin/` and symlinks it into `~/.local/bin/`, which is on PATH for typical macOS/Linux setups. `--editable` preserves live-source-changes during development. If `~/.local/bin/` isn't on PATH, `uv tool update-shell` adds it to common shell rc files.

### 3.4 Step 4 — Register with Claude Code

New step. Two variants, each with a one-sentence "use this when…" lead-in.

> **Stdio (quickstart, lifetime tied to your client).** Use for trying it out or short builds.
>
> ```bash
> claude mcp add --scope user --transport stdio harness-mcp -- harness-mcp serve --transport stdio
> ```
>
> **Streamable-http (daemon — jobs survive client disconnects).** Use for multi-hour builds.
>
> ```bash
> # 1. Start the daemon (needs ANTHROPIC_API_KEY in env):
> ANTHROPIC_API_KEY=... harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
>
> # 2. In another terminal, register the URL with Claude Code:
> claude mcp add --scope user --transport http harness-mcp http://127.0.0.1:8765/mcp
> ```

Decisions baked into these commands:

- **`--scope user`.** harness-mcp is a cross-project tool — you'd start a build from any working directory — so user scope is right. The CLI default is `local` (per-project), which would force re-registration in every checkout.
- **`ANTHROPIC_API_KEY` not threaded through `claude mcp add -e`.** The existing "Required environment variables" section already directs users to set it. Stdio inherits the parent shell env. The http daemon command shows inline-prefix `ANTHROPIC_API_KEY=...`, matching the README's existing pattern.
- **`--port 8765`.** Matches the current README; no reason to change.
- **No `-e` env-var threading in the `claude mcp add` examples.** Keeps the lines short. A user who needs to override env can add one or more `-e KEY=VAL` flags before the server name (all `claude mcp add` options must come before the server name; `--` only separates Claude options from the subprocess command for stdio).

### 3.5 Step 5 — Verify

Unchanged from today:

> ```bash
> harness-mcp doctor
> ```
>
> Expected: a list of `OK` lines for paths, env, codex, codex-shape, skill, mcp, strict-mcp-config, restart_sweep.

### 3.6 Manual config (reference) subsection

Title: **"Manual config (reference)"**. Placement: immediately after step 5, before "Required environment variables".

Body:

> If you'd rather edit `~/.claude.json` directly, or commit `.mcp.json` to a project repo, here's the equivalent for stdio:
>
> ```json
> {
>   "mcpServers": {
>     "harness-mcp": {
>       "command": "harness-mcp",
>       "args": ["serve", "--transport", "stdio"]
>     }
>   }
> }
> ```
>
> For streamable-http, point a `url` field at the daemon (see step 4 above for the daemon command).

Single JSON block (stdio variant only). No second block for http — the daemon command is already in step 4 and the JSON shape is trivial.

## 4. What gets removed

- **Line:** `uv pip install harness-mcp     # or: uv pip install -e . from a checkout` — replaced by the `git clone` step + `uv pip install -e .` line.
- **Section:** "Example mcp.json" (current ~lines 49–78), including both stdio and streamable-http JSON blocks. Their content is absorbed into step 4 (CLI form) and the Manual config subsection (one JSON form, stdio only).

## 5. What stays unchanged

- Title and intro paragraph.
- "Required environment variables" table.
- "Required MCP servers" prose (per §2 decision: not in scope).
- "Required skills" section.
- "Quickstart", "Notable decisions", "Troubleshooting", "CI smoke", and "Limitations" sections.

## 6. Verification

After implementation, the following sequence should run cleanly on a fresh checkout following only the README:

1. `git clone` per step 2.
2. `uv pip install -e .` per step 3.
3. `claude mcp add --scope user --transport stdio harness-mcp -- harness-mcp serve --transport stdio` per step 4 (stdio variant).
4. `harness-mcp doctor` per step 5 — expect all `OK` lines.

A reviewer reading the README top-to-bottom should be able to reach a passing `doctor` without bouncing to external docs or guessing CLI syntax. If the reviewer hits any "wait, where do I get X?" moment in the Setup section, the docs are incomplete.

For the http variant, the same sequence works with two terminals: one running the daemon (`harness-mcp serve --transport streamable-http ...`), the other running `claude mcp add --transport http ...` and then `harness-mcp doctor`. Both should reach all `OK` lines.

## 7. Out of scope

- Installation instructions for context7 or playwright. The README continues to assume the user has those wired up; this is explicit per §2.
- Any change to runtime behavior, dependencies, or supported transports.
- Publishing harness-mcp to PyPI or providing a non-source install path. Currently impossible due to the git-dep on Codex SDK, and not in scope for this docs change.
- Changes to "Required MCP servers", "Required skills", "Required environment variables", "Notable decisions", "Troubleshooting", "CI smoke", or "Limitations" sections.
- A separate `INSTALL.md` or any new top-level doc file.

## 8. Risks and tradeoffs

- **CLI syntax drift.** `claude mcp add` syntax has changed before (scope renaming in 0.2.49, project-scope addition in 0.2.50). Mitigation: link to `claude mcp add --help` in the README so users with newer CLIs can self-correct if syntax shifts. Verified syntax above against Claude Code v2.1.121+.
- **HTTPS clone URL for non-SSH users.** Spec includes both SSH and HTTPS clone forms to avoid the "I don't have SSH set up" footgun.
- **`--scope user` recommendation diverges from CLI default.** Documented in step 4. Implementer should not silently change to `--scope local` — would break the cross-project use case.
- **Loss of `.mcp.json` discoverability.** The full "Example mcp.json" section is removed. Users who relied on copy-pasting the JSON now have to either run `claude mcp add` or read the smaller reference subsection. Trade is intentional per §2.
