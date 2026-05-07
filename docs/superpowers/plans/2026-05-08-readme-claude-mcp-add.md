# README Setup Section — `claude mcp add` Instructions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update `README.md` so a fresh user can go from clone → `uv pip install -e .` → `claude mcp add ...` → `harness-mcp doctor` by following one linear sequence.

**Architecture:** Single-file documentation change. Two `Edit` operations on `README.md`: (1) restructure the "Setup" section into 5 numbered steps with a "Manual config (reference)" subsection appended; (2) delete the now-redundant standalone "Example mcp.json" section. Followed by a verification task that confirms the documented commands match the installed CLI's actual syntax and that `harness-mcp` resolves on PATH.

**Tech Stack:** Markdown (README), `claude mcp` CLI (Claude Code v2.1.121+), `harness-mcp` Python package, `git`, `uv`.

**Spec:** `docs/superpowers/specs/2026-05-08-readme-claude-mcp-add-design.md`

---

## File Structure

Files modified:

- `README.md` — two `Edit` operations:
  - Replace lines 5–27 (current "Setup" section) with new 5-step Setup section ending with a "Manual config (reference)" subsection.
  - Delete lines 49–78 (current standalone "Example mcp.json" section).

No files created. No code changes. No dependency or runtime changes.

The `Read` tool MUST be called on `README.md` before each `Edit` (the Edit tool refuses to operate on a file not previously Read in the session). After both Edits, the section "Required environment variables" through "Required skills" remains unchanged in content but its line numbers shift; that's expected.

---

## Task 1: Restructure the "Setup" section

**Files:**
- Modify: `README.md:5-27`

**Why this comes first:** The new step 4 ("Register with Claude Code") and the "Manual config (reference)" subsection together absorb everything in the current "Example mcp.json" section. Doing this Edit first means after Task 1 lands, the README has BOTH the new content AND the old "Example mcp.json" — duplicated but valid markdown. Task 2 then deletes the duplicate. Doing it in this order keeps each commit internally coherent and reviewable in isolation.

- [ ] **Step 1: Read the current `README.md` to lock in baseline**

Run via the Read tool: `Read /Users/timhsu/dev_projects/harness/README.md` (the full file). Confirm that:
- Line 5 is `## Setup`
- Line 27 ends with `Expected: a list of `OK` lines for paths, env, codex, codex-shape, skill, mcp, strict-mcp-config, restart_sweep.`
- Line 28 is blank
- Line 29 is `## Required environment variables`

If line numbers have drifted, adjust the `Edit` `old_string` accordingly. The exact `old_string` for the next step is the entire span lines 5–27 inclusive.

- [ ] **Step 2: Apply the Edit replacing the Setup section**

Edit `README.md` with these exact strings:

`old_string` (lines 5–27 of the current README):

````
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
````

`new_string`:

````
## Setup

1. Install the Codex CLI (https://github.com/openai/codex), confirm `codex --version` works, then configure `~/.codex/config.toml` — at minimum set your model. Example:

   ```toml
   model = "claude-sonnet-4-6"
   model_provider = "anthropic"
   ```

2. Clone the repo:

   ```bash
   git clone git@github.com:chindada/harness.git
   # or HTTPS: git clone https://github.com/chindada/harness.git
   cd harness
   ```

3. Install harness-mcp with uv:

   ```bash
   uv pip install -e .
   ```

   First-time install pulls the Codex SDK from a git dependency, so it takes longer than a typical `uv pip install`.

4. Register harness-mcp with Claude Code. Pick one transport.

   **Stdio** (quickstart, lifetime tied to your client). Use for trying it out or short builds.

   ```bash
   claude mcp add --scope user --transport stdio harness-mcp -- harness-mcp serve --transport stdio
   ```

   **Streamable-http** (daemon — jobs survive client disconnects). Use for multi-hour builds.

   ```bash
   # 1. Start the daemon (needs ANTHROPIC_API_KEY in env):
   ANTHROPIC_API_KEY=... harness-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765

   # 2. In another terminal, register the URL with Claude Code:
   claude mcp add --scope user --transport http harness-mcp http://127.0.0.1:8765/mcp
   ```

   `--scope user` registers harness-mcp across all projects (the CLI default `local` is per-project).

5. Verify everything:

   ```bash
   harness-mcp doctor
   ```

   Expected: a list of `OK` lines for paths, env, codex, codex-shape, skill, mcp, strict-mcp-config, restart_sweep.

### Manual config (reference)

If you'd rather edit `~/.claude.json` directly, or commit `.mcp.json` to a project repo, here's the equivalent for stdio:

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

For streamable-http, point a `url` field at the daemon (see step 4 above for the daemon command).
````

Apply via:

```
Edit:
  file_path: /Users/timhsu/dev_projects/harness/README.md
  old_string: <the old_string block above>
  new_string: <the new_string block above>
  replace_all: false
```

- [ ] **Step 3: Re-Read the file and verify the edit landed cleanly**

Read `/Users/timhsu/dev_projects/harness/README.md` from line 1, limit 110 lines.

Verify:
- Line 5 is still `## Setup`.
- The `## Setup` section now contains 5 numbered items (1–5).
- A `### Manual config (reference)` heading appears after step 5.
- The next `## ` heading after the Manual config subsection is `## Required environment variables`.
- The standalone `## Example mcp.json` section is still present further down the file (line numbers will have shifted after the edit, so locate by content). It's redundant after this edit; Task 2 removes it.
- No stray triple-backtick fences are unbalanced. (Counting check: the new section adds 4 fenced blocks — toml, bash, bash, bash, bash, json — paired open/close, all matched. Eyeball for opening fences without closes.)

If anything is wrong, the Edit didn't apply as expected; re-Read the file, identify the actual content, and re-apply.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): restructure Setup with claude-mcp-add instructions

Adds a `git clone` step, replaces the broken `uv pip install harness-mcp`
PyPI line with the editable-install-from-source form, and replaces the
JSON-only registration story with `claude mcp add` commands for both
stdio and streamable-http transports. Adds a small "Manual config
(reference)" subsection that retains a single JSON snippet for users
who edit ~/.claude.json directly.

Spec: docs/superpowers/specs/2026-05-08-readme-claude-mcp-add-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

(If the user said "no commit" for the implementation, skip this step and stage the change with `git add README.md` only, leaving the commit to the user.)

---

## Task 2: Delete the redundant standalone "Example mcp.json" section

**Files:**
- Modify: `README.md` — delete the section currently titled `## Example mcp.json` (was lines 49–78 pre-Task-1; after Task 1 the line numbers will have shifted, so locate by content not by line number).

**Why this is a separate task:** Two reasons. (1) Each commit is reviewable in isolation: Task 1 adds the new content, Task 2 removes the old. A reviewer can see at a glance that no instructions were lost, only relocated. (2) If a reviewer later objects to removing the JSON section entirely, this is the single commit to revert without losing the new structure.

- [ ] **Step 1: Read `README.md` to locate the current location of `## Example mcp.json`**

Read the full `README.md`. Locate by searching for the heading `## Example mcp.json` (line numbers will have shifted from Task 1's edit, so don't trust pre-Task-1 line numbers).

Identify the exact range:
- Start: the line beginning `## Example mcp.json`
- End: the line immediately before the next `##` heading (which should be `## Quickstart`)

Confirm the section content matches what's expected (stdio JSON block, streamable-http daemon bash block, streamable-http JSON block, separated by short prose lines).

- [ ] **Step 2: Apply the Edit deleting the section**

Edit `README.md`:

`old_string` (entire current `## Example mcp.json` section, including the leading `## Example mcp.json` heading and the trailing blank line that separates it from `## Quickstart`):

````
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

````

`new_string`: empty string `""`.

Apply via:

```
Edit:
  file_path: /Users/timhsu/dev_projects/harness/README.md
  old_string: <the old_string block above, exactly>
  new_string: ""
  replace_all: false
```

If the `old_string` doesn't match exactly (whitespace, trailing newlines, an extra blank line), the Edit will fail. In that case, re-Read the affected range and adjust `old_string` to match byte-for-byte. The intent is: delete the entire `## Example mcp.json` section including its leading heading and the blank line separating it from `## Quickstart`.

- [ ] **Step 3: Re-Read and verify**

Read `/Users/timhsu/dev_projects/harness/README.md` from line 1, full file.

Verify:
- The string `## Example mcp.json` no longer appears anywhere in the file.
- The section immediately preceding `## Quickstart` is now `## Required skills` (not `## Example mcp.json`).
- The new "Manual config (reference)" subsection from Task 1 is still present inside `## Setup`.
- No orphan fenced code blocks; no stray blank line gaps wider than 2 newlines between sections.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): remove redundant Example mcp.json section

Its content is now absorbed by the new step 4 (claude mcp add) and
the Manual config (reference) subsection inside Setup.

Spec: docs/superpowers/specs/2026-05-08-readme-claude-mcp-add-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

(Same caveat as Task 1: skip the commit and leave staged if the user requested no commits during implementation.)

---

## Task 3: Verify the documented commands match reality

**Files:** None modified. This task only runs verification commands to confirm the README's instructions actually work.

**Why this matters:** The spec's verification gate is "a fresh user following the README top-to-bottom can reach a passing `harness-mcp doctor`." If our documented `claude mcp add` flag names or argument order are wrong, the docs ship a footgun.

**Side-effect awareness:** Step 3 below intentionally does NOT run `claude mcp add` to register harness-mcp on the operator's own machine — that would mutate `~/.claude.json` and could conflict with the user's existing harness-mcp entry. Verification stays at the dry-run / syntax-check level. If the operator wants a hands-on end-to-end test, they can run the documented stdio command and `harness-mcp doctor` manually after the plan completes.

- [ ] **Step 1: Verify `claude mcp add --help` matches the documented syntax**

Run:

```bash
claude mcp add --help 2>&1
```

Expected: the output includes `--scope`, `--transport`, and (for the stdio path) describes how positional args after `--` are passed to the subprocess. The transport values should include both `stdio` and `http`.

If the output uses `streamable-http` instead of `http`, OR drops `--scope`, the README's syntax is wrong for the installed CLI version. Flag this as a regression and stop — don't push docs that contradict the local CLI.

If `claude mcp add --help` reports an unrecognized flag, run `claude --version` and `claude mcp --help` to identify version skew.

- [ ] **Step 2: Verify `which harness-mcp` resolves (assumes Task 1's documented `uv pip install -e .` was performed at some prior point — typically already true in this checkout)**

Run:

```bash
which harness-mcp 2>&1
```

Expected: a path under the project's `.venv/bin/harness-mcp` (or the user's active venv). If the command isn't found, the README's step 3 (`uv pip install -e .`) would not have made the binary available — a docs bug. Flag and stop.

If the operator has not yet installed harness-mcp in this checkout, this step is informational rather than a hard failure: the README's step 3 will create the binary on a fresh user's machine.

- [ ] **Step 3: Validate the JSON snippet in "Manual config (reference)" parses as valid JSON**

Run:

```bash
python3 -c 'import json,sys; json.loads(sys.stdin.read())' <<'EOF'
{
  "mcpServers": {
    "harness-mcp": {
      "command": "harness-mcp",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
EOF
```

Expected: no output, exit code 0. Any `json.JSONDecodeError` means the snippet in the README is malformed — fix it inline in `README.md` and re-verify.

- [ ] **Step 4: Eyeball the rendered Setup section**

Open `README.md` in a markdown viewer or run:

```bash
git diff HEAD~2 -- README.md 2>&1 | head -200
```

Confirm:
- The Setup section reads top-to-bottom as 5 coherent numbered steps.
- Step 4 has both stdio and streamable-http variants, each with a one-line "use this when…" lead-in.
- The Manual config subsection is below step 5, contains exactly one JSON block, and points to step 4 for the http daemon command.
- No remaining `## Example mcp.json` heading anywhere in the file.

- [ ] **Step 5: Optionally run a hands-on smoke (deferred to operator)**

This is a manual checkpoint, not an automated one. If you want to validate end-to-end:

```bash
# (only if you're willing to mutate ~/.claude.json)
claude mcp add --scope user --transport stdio harness-mcp -- harness-mcp serve --transport stdio
harness-mcp doctor
# ...inspect output for OK lines, then optionally:
claude mcp remove harness-mcp
```

If `harness-mcp doctor` returns all `OK` lines, the documented stdio path is verified end-to-end. If `mcp` or `strict-mcp-config` lines fail, the README's step 4 stdio command isn't producing a config the harness can read — flag and investigate before merging.

Skip this step in environments where mutating the user's Claude Code config is not desired.

---

## Out of Scope (locked in by the spec)

These are explicitly NOT part of this plan and should be rejected if they appear during implementation:

- Adding install instructions for `context7` or `playwright` (spec §7).
- Publishing harness-mcp to PyPI (spec §7).
- Changes to "Required environment variables", "Required MCP servers", "Required skills", "Quickstart", "Notable decisions", "Troubleshooting", "CI smoke", "Limitations", "Notable decisions" — anything outside `## Setup` and `## Example mcp.json` (spec §5, §7).
- Creating `INSTALL.md` or any new doc file (spec §7).
- Any code, dependency, or runtime change.
