# Harness MCP — Part 3: Agent SDK Wrappers (Generator, Contracts, Evaluator, Launcher)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the four modules that wrap the SDKs and parse the file-mediated handoffs between agents: `generator.py` (Codex SDK chunk loop, handoff parsing, commit), `contracts.py` (round body extraction + APPROVED detection), `evaluator.py` (Claude SDK static + dynamic queries, eval.md parser, sync helper), `evaluator_runner.py` (the `python -m` launcher entry point that drives `ClaudeSDKClient` inside its own process group).

**Architecture:** Each module is a self-contained adapter. `generator.py` consumes the Codex event stream via `EventLogger` (Part 2). `contracts.py` is pure parsing — no SDK calls. `evaluator.py` and `evaluator_runner.py` together drive `ClaudeSDKClient`; the runner is a sibling of the orchestrator process so its descendants can be reaped via `ProcessGroupScope` (Part 2). All file writes go through temp-and-rename for crash atomicity.

**Tech Stack:** `claude_agent_sdk`, `codex_app_server`, `anyio`, stdlib (`re`, `json`, `subprocess` indirectly via `anyio.open_process` from Part 2).

**Spec source:** `docs/superpowers/specs/2026-05-07-harness-mcp-design.md` — sections §6.1 (contract negotiation), §6.3 (evaluation), §6.4 (retry & tag namespacing), §7 (chunk loop), §7.0 (chunk prompt shapes), §7.1 (handoff format), §7.4 (commit), §8.1–§8.4 (Evaluator + launcher) are load-bearing.

**Depends on:** Parts 1 + 2 — `harness_mcp.types`, `config`, `prompts_loader`, `process_group`, `logging_setup`, `state` (only used by orchestrator, not by these modules; the launcher must NOT import state).

---

## Branch & Commit Policy (READ FIRST)

- **Stay on the `main` branch for the entire plan.** Do not create or switch branches.
- **Do NOT run `git commit`, `git add`, `git push`, or any git mutation.** Verify by running tests / inspecting files only.
- The implementation will, however, write code that *executes* `git` against the per-job `app/` repos under `~/.harness/jobs/<job_id>/app/`. Those `git` invocations are runtime behavior, not commits to this harness repo. Tests use `tmp_path` git repos to exercise commit logic in isolation.
- If a step's check fails, fix the problem and re-run the check — never paper over with a commit to the harness repo.

---

## File Structure (this part owns)

| File | Purpose |
|---|---|
| `src/harness_mcp/contracts.py` | `parse_round_body_from_codex_events`, `parse_round_body_from_claude_msgs`, `is_approved_body`, `append_round_atomic` |
| `src/harness_mcp/generator.py` | `parse_handoff` (handoff-NNN.md → Handoff dataclass), `build_chunk_prompt` (4 shape selector), `chunk_loop` (the §7 reset-and-handoff loop), `commit_and_summarize` (§7.4 git commit + tag) |
| `src/harness_mcp/evaluator.py` | `parse_eval_md` (eval.md → EvaluationResult), `sync_eval_md` (await SDK file write + assert section header), `static_audit_prompt`, `dynamic_verification_prompt`, `pipe_claude_msg_to_log` |
| `src/harness_mcp/evaluator_runner.py` | `python -m harness_mcp.evaluator_runner` entry point — reads JSON payload from stdin, drives `ClaudeSDKClient` (§8.4), writes eval.md, exits 0/1. Must NOT import `harness_mcp.state`. |
| `tests/test_contracts.py` | Body-extract from Codex/Claude streams, APPROVED parser (case + line-anchored), atomic-append round files |
| `tests/test_generator.py` | Handoff parser (status, files-touched delimiter, malformed cases), chunk-prompt shape selection, commit-and-summarize against tmp git repo |
| `tests/test_evaluator.py` | eval.md parser (PASS/FAIL counts, routing decision, unparseable), sync_eval_md (header check + tmp file flow) |
| `tests/test_evaluator_runner.py` | Launcher import isolation (no `state.py`), JSON payload schema validation, path-inside-job-dir assertion |

---

## Pre-flight (one-time, before Task 1)

- [ ] **Step 0a: Confirm Parts 1 + 2 artifacts exist.**

```bash
test -f src/harness_mcp/state.py && \
test -f src/harness_mcp/process_group.py && \
test -f src/harness_mcp/logging_setup.py && \
test -f src/harness_mcp/mcp_capture.py && \
test -f src/harness_mcp/types.py && echo OK
```

Expected: `OK`. If any file is missing, prior parts aren't done — STOP.

- [ ] **Step 0b: Verify the prior test suite still passes.**

```bash
uv run pytest -q
```

Expected: green.

---

## Task 1: `contracts.py` — round body extraction & APPROVED parser

**Files:**
- Create: `tests/test_contracts.py`
- Create: `src/harness_mcp/contracts.py`

Per spec §6.1: Codex streams events; Claude returns `AssistantMessage` blocks. Both must be reduced to a single coherent body string per round, and the body's last non-empty line must be checked for the literal token `APPROVED` (case-sensitive, on its own line).

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_contracts.py`:

```python
"""Tests for harness_mcp.contracts — body extraction + APPROVED detection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_mcp.contracts import (
    append_round_atomic,
    is_approved_body,
    parse_round_body_from_claude_msgs,
    parse_round_body_from_codex_events,
)


@dataclass
class FakeEvent:
    method: str
    payload: Any


class TestIsApprovedBody:
    def test_bare_approved(self) -> None:
        assert is_approved_body("APPROVED") is True

    def test_approved_with_trailing_newline(self) -> None:
        assert is_approved_body("APPROVED\n") is True

    def test_approved_at_end_of_paragraph(self) -> None:
        body = "we accept the criteria as proposed.\n\nAPPROVED"
        assert is_approved_body(body) is True

    def test_approved_inline_NOT_accepted(self) -> None:
        # Spec §6.1: "APPROVED ... on its own line at the end of your response"
        assert is_approved_body("we say APPROVED here") is False

    def test_lowercase_not_approved(self) -> None:
        assert is_approved_body("approved") is False

    def test_empty_body_not_approved(self) -> None:
        assert is_approved_body("") is False
        assert is_approved_body("   \n\n") is False


class TestParseRoundBodyFromCodexEvents:
    def test_concatenates_agent_message_deltas(self) -> None:
        events = [
            FakeEvent("item/agentMessage/delta", SimpleNamespace(delta="hello ")),
            FakeEvent("item/agentMessage/delta", SimpleNamespace(delta="world")),
            FakeEvent("turn/completed", SimpleNamespace(turn=SimpleNamespace(id="t1"))),
        ]
        body = parse_round_body_from_codex_events(events)
        assert body == "hello world"

    def test_excludes_tool_calls(self) -> None:
        events = [
            FakeEvent("item/agentMessage/delta", SimpleNamespace(delta="prelude ")),
            FakeEvent("item/started", SimpleNamespace(item=SimpleNamespace(id="x", type="commandExecution"))),
            FakeEvent("item/completed", SimpleNamespace(item=SimpleNamespace(id="x", type="commandExecution"))),
            FakeEvent("item/agentMessage/delta", SimpleNamespace(delta="postlude")),
            FakeEvent("turn/completed", SimpleNamespace(turn=SimpleNamespace(id="t1"))),
        ]
        body = parse_round_body_from_codex_events(events)
        assert body == "prelude postlude"

    def test_empty_when_no_text_deltas(self) -> None:
        events = [
            FakeEvent("turn/completed", SimpleNamespace(turn=SimpleNamespace(id="t1"))),
        ]
        body = parse_round_body_from_codex_events(events)
        assert body == ""


# The parser keys off `type(block).__name__ == "TextBlock"` / "ToolUseBlock", so the
# stand-in classes MUST be named exactly that. Class names need no underscore prefix
# because we never import the real SDK classes here — there's no collision.
class TextBlock:
    """Stand-in for claude_agent_sdk.TextBlock — `type(...).__name__ == "TextBlock"`."""
    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    """Stand-in for claude_agent_sdk.ToolUseBlock."""
    def __init__(self, name: str, inp: dict[str, Any] | None = None) -> None:
        self.name = name
        self.input = inp or {}


class TestParseRoundBodyFromClaudeMsgs:
    def test_extracts_text_blocks_from_final_assistant_message(self) -> None:
        # `parse_round_body_from_claude_msgs` keys off `type(block).__name__ == "TextBlock"`
        # — using a real class literally named TextBlock satisfies that check.
        # (SimpleNamespace's __class__.__name__ is read-only, so subclassing/mutation
        # isn't a viable shortcut.)
        msgs = [SimpleNamespace(content=[TextBlock("hello world")])]
        body = parse_round_body_from_claude_msgs(msgs)
        assert body == "hello world"

    def test_concatenates_multiple_text_blocks(self) -> None:
        msgs = [SimpleNamespace(content=[TextBlock("part 1 "), TextBlock("part 2")])]
        body = parse_round_body_from_claude_msgs(msgs)
        assert body == "part 1 part 2"

    def test_skips_tool_use_blocks(self) -> None:
        msgs = [SimpleNamespace(content=[
            TextBlock("prelude"),
            ToolUseBlock("Read", {"file_path": "x"}),
        ])]
        body = parse_round_body_from_claude_msgs(msgs)
        assert body == "prelude"


class TestAppendRoundAtomic:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.md"
        append_round_atomic(path, "## Round 1 — Generator\n", "criteria proposal\n")
        text = path.read_text(encoding="utf-8")
        assert "## Round 1 — Generator" in text
        assert "criteria proposal" in text

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.md"
        path.write_text("# Sprint 1\n\n## Round 1 — Generator\nold\n", encoding="utf-8")
        append_round_atomic(path, "## Round 1 — Evaluator\n", "evaluator response\n")
        text = path.read_text(encoding="utf-8")
        assert "Round 1 — Generator" in text
        assert "Round 1 — Evaluator" in text

    def test_temp_file_cleaned_up(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.md"
        append_round_atomic(path, "## Round 1\n", "body\n")
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []
```

- [ ] **Step 2: Confirm the failure.**

```bash
uv run pytest tests/test_contracts.py -v
```

Expected: ImportError on `harness_mcp.contracts`.

- [ ] **Step 3: Implement `contracts.py`.**

Create `src/harness_mcp/contracts.py`:

```python
"""Round-by-round contract negotiation parsing + atomic file appends.

The orchestrator owns `contract.md` structure: agents emit message bodies,
this module reduces them to strings, the orchestrator concatenates a
`## Round N — <Role>` header on top, and we write the result atomically
via temp-and-rename.

Body extraction differs by SDK:
  * Codex (`thread.turn().stream()` events): concatenate every
    `item/agentMessage/delta` payload.delta string within the turn,
    excluding tool-call markers.
  * Claude (`AssistantMessage` content list): concatenate every TextBlock's
    `.text`, excluding ToolUseBlock entries.

APPROVED check: the body's last non-empty line must equal `APPROVED`
exactly (case-sensitive). Spec §6.1.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def parse_round_body_from_codex_events(events: list[Any]) -> str:
    """Reduce a Codex event stream slice to a single body string.

    Concatenates `item/agentMessage/delta` payload.delta values; ignores
    tool-call markers, turn boundaries, token-usage updates, etc.
    """
    parts: list[str] = []
    for event in events:
        method = getattr(event, "method", "")
        if method != "item/agentMessage/delta":
            continue
        payload = getattr(event, "payload", None)
        delta = getattr(payload, "delta", "") if payload is not None else ""
        if delta:
            parts.append(delta)
    return "".join(parts)


def parse_round_body_from_claude_msgs(msgs: list[Any]) -> str:
    """Reduce a Claude message list to the concatenated TextBlock content.

    `msgs` is the list yielded by iterating `query()` (or `client.receive_response()`).
    We pull text from every message's `.content` whose block class name is
    `TextBlock`. ToolUseBlocks are excluded — they're side effects, not body.

    Class-name comparison instead of isinstance keeps the parser independent
    from importing the SDK at module level (important for unit tests + for
    module-level import-graph isolation in the launcher).
    """
    parts: list[str] = []
    for msg in msgs:
        content = getattr(msg, "content", None)
        if not content:
            continue
        for block in content:
            if type(block).__name__ == "TextBlock":
                text = getattr(block, "text", "")
                if text:
                    parts.append(text)
    return "".join(parts)


def is_approved_body(body: str) -> bool:
    """True iff body's last non-empty line is exactly `APPROVED` (case-sensitive)."""
    if not body:
        return False
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    return lines[-1].strip() == "APPROVED"


def append_round_atomic(path: Path, header: str, body: str) -> None:
    """Append `<header><body>\\n` to `path` via temp-and-rename.

    Reads existing file (or treats as empty if missing), concatenates
    in memory, writes to `<path>.tmp`, then `os.replace()` to `path`.
    Same pattern used for handoff-NNN.md (§7.1) and eval.md.
    """
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    new_content = existing + header + body
    if not new_content.endswith("\n"):
        new_content += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    os.replace(tmp, path)
```

- [ ] **Step 4: Run the tests.**

```bash
uv run pytest tests/test_contracts.py -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/contracts.py tests/test_contracts.py
uv run ruff format --check src/harness_mcp/contracts.py tests/test_contracts.py
```

Expected: zero findings.

---

## Task 2: `generator.py` — handoff parser

**Files:**
- Create: `tests/test_generator.py`
- Create: `src/harness_mcp/generator.py` (handoff parser only; chunk loop arrives in Task 4)

Per spec §7.1: handoff-NNN.md is the only artifact between Codex resets. Strict format with `## Status`, `## Summary`, `## Files touched` (split on first ` — `), etc. `parse_handoff` returns a `Handoff` dataclass; malformed → `HandoffParseError`.

- [ ] **Step 1: Write the failing tests for the parser.**

Create `tests/test_generator.py`:

```python
"""Tests for harness_mcp.generator — handoff parser, chunk prompt builder, commit helper."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from harness_mcp.generator import build_chunk_prompt, parse_handoff
from harness_mcp.types import Handoff, HandoffParseError


GOOD_HANDOFF = dedent(
    """
    # Handoff 2

    ## Status
    in-progress

    ## Summary
    Wired up the /api/todos GET endpoint.

    ## Work done this chunk
    - Added the route in app.py.
    - Wrote the ORM helper.

    ## Decisions made
    - Used Flask's MethodView — simpler than class-based dispatch.

    ## Files touched
    - app.py — added blueprint registration
    - models.py — new Todo dataclass

    ## Open questions / concerns
    - None.

    ## Next steps (if in-progress)
    - Add POST endpoint
    - Add PATCH/DELETE
    """
).strip()


class TestParseHandoff:
    def test_full_parse(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-002.md"
        path.write_text(GOOD_HANDOFF, encoding="utf-8")
        h = parse_handoff(path)
        assert isinstance(h, Handoff)
        assert h.chunk_seq == 2
        assert h.status == "in-progress"
        assert h.declares_done is False
        assert h.summary.startswith("Wired up")
        assert ("app.py", "added blueprint registration") in h.files_touched
        assert ("models.py", "new Todo dataclass") in h.files_touched
        assert "Add POST endpoint" in h.next_steps

    def test_done_status(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-001.md"
        path.write_text(
            GOOD_HANDOFF.replace("in-progress", "done"), encoding="utf-8"
        )
        h = parse_handoff(path)
        assert h.status == "done"
        assert h.declares_done is True

    def test_missing_status_section(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-001.md"
        path.write_text("# Handoff 1\n\n## Summary\nx\n", encoding="utf-8")
        with pytest.raises(HandoffParseError):
            parse_handoff(path)

    def test_invalid_status_value(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-001.md"
        path.write_text(
            "# Handoff 1\n\n## Status\nmaybe\n\n## Summary\nx\n", encoding="utf-8"
        )
        with pytest.raises(HandoffParseError):
            parse_handoff(path)

    def test_files_touched_no_em_dash_keeps_path_only(self, tmp_path: Path) -> None:
        body = (
            "# Handoff 1\n\n## Status\ndone\n\n## Summary\nx\n\n"
            "## Files touched\n- app.py\n- models.py — refactored\n"
        )
        path = tmp_path / "handoff-001.md"
        path.write_text(body, encoding="utf-8")
        h = parse_handoff(path)
        assert ("app.py", "") in h.files_touched
        assert ("models.py", "refactored") in h.files_touched

    def test_chunk_seq_inferred_from_filename(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-007.md"
        path.write_text(GOOD_HANDOFF, encoding="utf-8")
        h = parse_handoff(path)
        assert h.chunk_seq == 7

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(HandoffParseError):
            parse_handoff(tmp_path / "nonexistent.md")


class TestBuildChunkPrompt:
    def test_first_chunk_includes_design_and_plan(self, tmp_path: Path) -> None:
        design = tmp_path / "design.md"
        design.write_text("DESIGN_BODY")
        plan_section = tmp_path / "plan_section.md"
        plan_section.write_text("PLAN_SECTION_BODY")
        contract = tmp_path / "contract.md"
        contract.write_text("CONTRACT_BODY")
        handoff_path = tmp_path / "handoff-001.md"
        prompt = build_chunk_prompt(
            generator_md="GENERATOR_PROMPT",
            contract_path=contract,
            design_path=design,
            plan_section_path=plan_section,
            prev_handoff=None,
            eval_md_for_retry=None,
            handoff_path=handoff_path,
            chunk_seq=1,
        )
        assert "GENERATOR_PROMPT" in prompt
        assert "DESIGN_BODY" in prompt
        assert "PLAN_SECTION_BODY" in prompt
        assert "CONTRACT_BODY" in prompt
        assert str(handoff_path) in prompt
        assert "first chunk" in prompt.lower()

    def test_continued_chunk_includes_prev_handoff(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.md"
        contract.write_text("CONTRACT")
        prev = tmp_path / "handoff-001.md"
        prev.write_text("PREV_HANDOFF_BODY")
        handoff_path = tmp_path / "handoff-002.md"
        prompt = build_chunk_prompt(
            generator_md="G",
            contract_path=contract,
            design_path=None,
            plan_section_path=None,
            prev_handoff=prev,
            eval_md_for_retry=None,
            handoff_path=handoff_path,
            chunk_seq=2,
        )
        assert "PREV_HANDOFF_BODY" in prompt
        assert "continuation" in prompt.lower()

    def test_retry_chunk_includes_eval_md(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.md"
        contract.write_text("CONTRACT")
        eval_md = tmp_path / "eval.md"
        eval_md.write_text("EVAL_BODY_FAILED")
        handoff_path = tmp_path / "handoff-001.md"
        prompt = build_chunk_prompt(
            generator_md="G",
            contract_path=contract,
            design_path=None,
            plan_section_path=None,
            prev_handoff=None,
            eval_md_for_retry=eval_md,
            handoff_path=handoff_path,
            chunk_seq=1,
        )
        assert "EVAL_BODY_FAILED" in prompt
        assert "retry" in prompt.lower()
        assert "READ-ONLY" in prompt or "fixed" in prompt.lower()

    def test_continued_chunk_with_lost_prev_handoff(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.md"
        contract.write_text("CONTRACT")
        handoff_path = tmp_path / "handoff-003.md"
        prompt = build_chunk_prompt(
            generator_md="G",
            contract_path=contract,
            design_path=None,
            plan_section_path=None,
            prev_handoff=None,  # malformed previous handoff → reset
            eval_md_for_retry=None,
            handoff_path=handoff_path,
            chunk_seq=3,
        )
        assert "Proceed fresh" in prompt or "no valid handoff" in prompt.lower()
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_generator.py -v
```

Expected: ImportError on `harness_mcp.generator`.

- [ ] **Step 3: Implement `generator.py` (parser + prompt builder only).**

Create `src/harness_mcp/generator.py`:

```python
"""Codex Generator wrapper: chunk loop, handoff parsing, commit helper.

This module is split into three layers:
  1. parse_handoff    — handoff-NNN.md → Handoff dataclass (pure parser).
  2. build_chunk_prompt — selects one of four prompt shapes per call site.
  3. chunk_loop       — the §7 reset-and-handoff loop. Imports AsyncCodex.
  4. commit_and_summarize — git add . / commit / tag at sprint end.

Layers 1 + 2 are pure (no I/O beyond reading filesystem); layer 3 does
async + subprocess via the SDK; layer 4 shells out to git.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from harness_mcp.types import Handoff, HandoffParseError, ImplementationResult


# ---------- Layer 1: parse_handoff ----------


_STATUS_HEADER_RE = re.compile(r"^##\s+Status\s*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[\s]*[-*]\s+(.+?)\s*$", re.MULTILINE)
_FILENAME_SEQ_RE = re.compile(r"handoff-(\d+)\.md$")


def _split_sections(text: str) -> dict[str, str]:
    """Map each `## Heading` to the body that follows (until next heading)."""
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        heading = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[heading] = text[start:end].strip()
    return sections


def _parse_files_touched(body: str) -> list[tuple[str, str]]:
    """Each bullet → (path, reason). Split on first ` — ` (em-dash)."""
    out: list[tuple[str, str]] = []
    for m in _BULLET_RE.finditer(body):
        bullet = m.group(1).strip()
        if " — " in bullet:
            path, _, reason = bullet.partition(" — ")
            path = path.strip()
            reason = reason.strip()
        else:
            path, reason = bullet, ""
        if path:
            out.append((path, reason))
    return out


def _parse_bullets(body: str) -> list[str]:
    return [m.group(1).strip() for m in _BULLET_RE.finditer(body)]


def parse_handoff(path: Path) -> Handoff:
    """Parse handoff-NNN.md → Handoff. Raise HandoffParseError on any malformedness."""
    if not path.is_file():
        raise HandoffParseError(f"handoff file missing: {path}")

    text = path.read_text(encoding="utf-8")
    sections = _split_sections(text)

    if "status" not in sections:
        raise HandoffParseError(f"missing `## Status` section in {path.name}")

    status_body = sections["status"].strip()
    if status_body not in ("in-progress", "done"):
        raise HandoffParseError(
            f"invalid status {status_body!r} in {path.name}; expected 'in-progress' or 'done'"
        )

    summary = sections.get("summary", "").strip()
    work_done = _parse_bullets(sections.get("work done this chunk", ""))
    decisions = _parse_bullets(sections.get("decisions made", ""))
    files_touched = _parse_files_touched(sections.get("files touched", ""))
    open_questions = _parse_bullets(sections.get("open questions / concerns", ""))
    next_steps = _parse_bullets(sections.get("next steps (if in-progress)", ""))

    seq_match = _FILENAME_SEQ_RE.search(path.name)
    chunk_seq = int(seq_match.group(1)) if seq_match else 0

    return Handoff(
        chunk_seq=chunk_seq,
        status=status_body,
        summary=summary,
        work_done=work_done,
        decisions=decisions,
        files_touched=files_touched,
        open_questions=open_questions,
        next_steps=next_steps,
        declares_done=(status_body == "done"),
    )


# ---------- Layer 2: build_chunk_prompt ----------


def _read_or_empty(p: Path | None) -> str:
    if p is None or not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")


def build_chunk_prompt(
    *,
    generator_md: str,
    contract_path: Path,
    design_path: Path | None,
    plan_section_path: Path | None,
    prev_handoff: Path | None,
    eval_md_for_retry: Path | None,
    handoff_path: Path,
    chunk_seq: int,
) -> str:
    """Build one of four prompt shapes per spec §7.0.

    Selection rules:
      * eval_md_for_retry given          → Shape 4 (retry)
      * chunk_seq == 1, no retry         → Shape 2 (first chunk)
      * chunk_seq > 1, prev_handoff path → Shape 3 (continuation)
      * chunk_seq > 1, prev_handoff None → Shape 3 with "no valid handoff" addendum
    """
    contract = _read_or_empty(contract_path)
    design = _read_or_empty(design_path)
    plan_section = _read_or_empty(plan_section_path)

    if eval_md_for_retry is not None:
        eval_body = _read_or_empty(eval_md_for_retry)
        return _shape_retry(generator_md, contract, eval_body, handoff_path)

    if chunk_seq == 1:
        return _shape_first(generator_md, design, plan_section, contract, handoff_path)

    prev_body = _read_or_empty(prev_handoff)
    return _shape_continued(generator_md, contract, prev_body, handoff_path, chunk_seq)


def _shape_first(generator_md: str, design: str, plan_section: str, contract: str, handoff_path: Path) -> str:
    return f"""{generator_md}

## Mode: implementation (first chunk)

## Design (verbatim)
{design}

## Plan section (verbatim)
{plan_section}

## Contract (verbatim)
{contract}

## Where to write your handoff
At the end of this chunk, write your handoff to: {handoff_path}
Use the format documented in the system prompt (atomic write: <name>.tmp then rename).
"""


def _shape_continued(generator_md: str, contract: str, prev_handoff_body: str, handoff_path: Path, chunk_seq: int) -> str:
    if prev_handoff_body:
        prev_section = f"## Previous handoff (verbatim)\n{prev_handoff_body}"
    else:
        prev_section = (
            "## Previous handoff (verbatim)\n"
            "Previous chunk produced no valid handoff. Proceed fresh based on contract.md and "
            "what's already in the working tree."
        )
    return f"""{generator_md}

## Mode: implementation (chunk {chunk_seq}, continuation)

## Contract (verbatim)
{contract}

{prev_section}

## Where to write your handoff
At the end of this chunk, write your handoff to: {handoff_path}
Pick up from "Next steps" in the previous handoff if available.
"""


def _shape_retry(generator_md: str, contract: str, eval_body: str, handoff_path: Path) -> str:
    return f"""{generator_md}

## Mode: implementation (retry — previous attempt failed evaluation)

## Contract (verbatim, READ-ONLY)
{contract}

## Failed evaluation (verbatim)
{eval_body}

## Instructions
The previous attempt failed the evaluation above. Address the specific FAIL criteria
without expanding scope. Do NOT propose new criteria. The contract is fixed — work within it.

## Where to write your handoff
At the end of this chunk, write your handoff to: {handoff_path}
"""
```

- [ ] **Step 4: Run the tests.**

```bash
uv run pytest tests/test_generator.py -v
```

Expected: parser + prompt-builder tests pass. Chunk loop + commit helper come in later tasks.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/generator.py tests/test_generator.py
uv run ruff format --check src/harness_mcp/generator.py tests/test_generator.py
```

Expected: zero findings.

---

## Task 3: `generator.py` — `commit_and_summarize`

**Files:**
- Modify: `tests/test_generator.py`
- Modify: `src/harness_mcp/generator.py`

Per spec §7.4: at handoff `Status: done`, run `git add .` (NOT `-A` — `.gitignore` is the filter), commit with the handoff's summary, tag `harness/<job_id>/sprint-<N>`. The annotated-tag-collision check from §6.4 lives in the orchestrator (Plan 5); this helper just commits + tags.

- [ ] **Step 1: Append the failing test for `commit_and_summarize`.**

Append to `tests/test_generator.py`:

```python
import os
import shutil
import subprocess

from harness_mcp.generator import commit_and_summarize
from harness_mcp.types import CommitFailedError


def _init_app_repo(app_dir: Path) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / ".gitignore").write_text("*.pyc\n.venv/\n")
    subprocess.run(["git", "init", "-q"], cwd=str(app_dir), check=True)
    # Configure local user so commits work in CI.
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(app_dir), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(app_dir), check=True)


@pytest.fixture
def app_repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    app = tmp_path / "app"
    _init_app_repo(app)
    return app


class TestCommitAndSummarize:
    @pytest.mark.asyncio
    async def test_commits_changes_and_tags(self, app_repo: Path) -> None:
        # Add a file in the worktree.
        (app_repo / "x.py").write_text("print('hi')\n")
        h = Handoff(
            chunk_seq=1, status="done", summary="add x.py",
            work_done=["wrote x.py"], decisions=[], files_touched=[("x.py", "scaffold")],
            open_questions=[], next_steps=[], declares_done=True,
        )
        result = await commit_and_summarize(app_repo, h, sprint_seq=1, job_id="JOBID")
        assert result.ok is True
        assert result.commit_sha is not None
        assert result.files_touched == ["x.py"]

        # Tag should exist.
        tags = subprocess.run(
            ["git", "tag", "--list", "harness/JOBID/sprint-1"],
            cwd=str(app_repo), capture_output=True, text=True, check=True,
        )
        assert "harness/JOBID/sprint-1" in tags.stdout

    @pytest.mark.asyncio
    async def test_no_changes_to_commit_still_tags(self, app_repo: Path) -> None:
        # Initial empty commit so HEAD exists.
        (app_repo / "init.py").write_text("")
        subprocess.run(["git", "add", "init.py"], cwd=str(app_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(app_repo), check=True)

        h = Handoff(
            chunk_seq=1, status="done", summary="no-op sprint",
            work_done=[], decisions=[], files_touched=[], open_questions=[],
            next_steps=[], declares_done=True,
        )
        result = await commit_and_summarize(app_repo, h, sprint_seq=2, job_id="JOBID")
        assert result.ok is True
        # Tag should still exist even with no new commit.
        tags = subprocess.run(
            ["git", "tag", "--list", "harness/JOBID/sprint-2"],
            cwd=str(app_repo), capture_output=True, text=True, check=True,
        )
        assert "harness/JOBID/sprint-2" in tags.stdout

    @pytest.mark.asyncio
    async def test_commit_failed_when_not_a_git_repo(self, tmp_path: Path) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not available")
        # tmp_path is plain dir, not a git repo.
        h = Handoff(
            chunk_seq=1, status="done", summary="x",
            work_done=[], decisions=[], files_touched=[],
            open_questions=[], next_steps=[], declares_done=True,
        )
        with pytest.raises(CommitFailedError):
            await commit_and_summarize(tmp_path, h, sprint_seq=1, job_id="J")
```

- [ ] **Step 2: Run to confirm fail.**

```bash
uv run pytest tests/test_generator.py::TestCommitAndSummarize -v
```

Expected: ImportError on `commit_and_summarize`.

- [ ] **Step 3: Append the implementation.**

Append to `src/harness_mcp/generator.py`:

```python
# ---------- Layer 4: commit_and_summarize ----------


import anyio  # noqa: E402  (logical layering — keep imports near use)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command synchronously, raise CommitFailedError on non-zero exit."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        from harness_mcp.types import CommitFailedError

        raise CommitFailedError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


async def commit_and_summarize(
    app_dir: Path, handoff: Handoff, *, sprint_seq: int, job_id: str
) -> ImplementationResult:
    """Commit any uncommitted changes in `app_dir`, tag `harness/<job_id>/sprint-<N>`.

    `git add .` honors the .gitignore (seeded at job start). If the working
    tree has uncommitted changes, commit with the handoff's summary as
    subject + work-done/decisions in the body. If it doesn't, skip the
    commit but still tag (so subsequent sprints can diff against it).

    Tag overwrite (`git tag -f`) is left to the caller — the orchestrator
    runs the namespace-aware annotated-tag-collision check from §6.4
    before invoking this helper.
    """
    # All git invocations are blocking C calls; offload so the event loop stays free.
    return await anyio.to_thread.run_sync(_commit_and_tag_sync, app_dir, handoff, sprint_seq, job_id)


def _commit_and_tag_sync(
    app_dir: Path, handoff: Handoff, sprint_seq: int, job_id: str
) -> ImplementationResult:
    if not (app_dir / ".git").is_dir():
        from harness_mcp.types import CommitFailedError

        raise CommitFailedError(f"{app_dir} is not a git repository")

    _git(["add", "."], cwd=app_dir)

    # Was anything actually staged?
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(app_dir),
        capture_output=True,
    )
    needs_commit = diff.returncode != 0

    commit_sha: str | None = None
    if needs_commit:
        subject = f"Sprint {sprint_seq}: {handoff.summary[:80]}"
        body_lines = ["", ""]
        if handoff.work_done:
            body_lines.append("Work done:")
            for w in handoff.work_done:
                body_lines.append(f"- {w}")
            body_lines.append("")
        if handoff.decisions:
            body_lines.append("Decisions:")
            for d in handoff.decisions:
                body_lines.append(f"- {d}")
        full_msg = subject + "\n" + "\n".join(body_lines)
        _git(["commit", "-q", "-m", full_msg], cwd=app_dir)
        commit_sha = _git(["rev-parse", "HEAD"], cwd=app_dir).stdout.strip()
    else:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(app_dir), capture_output=True, text=True,
        )
        commit_sha = head.stdout.strip() if head.returncode == 0 else None

    tag_name = f"harness/{job_id}/sprint-{sprint_seq}"
    _git(["tag", "-f", tag_name], cwd=app_dir)

    return ImplementationResult(
        ok=True,
        files_touched=[p for p, _ in handoff.files_touched],
        commit_sha=commit_sha,
        summary=handoff.summary,
    )
```

- [ ] **Step 4: Run the new tests.**

```bash
uv run pytest tests/test_generator.py::TestCommitAndSummarize -v
```

Expected: every test passes (skipped if `git` isn't on PATH).

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/generator.py
uv run ruff format --check src/harness_mcp/generator.py
```

Expected: zero findings. (Suppress the `import anyio  # noqa: E402` warning by moving the import to the top of the file if your ruff config rejects mid-file imports — both placements are valid.)

---

## Task 4: `generator.py` — `chunk_loop`

**Files:**
- Modify: `tests/test_generator.py`
- Modify: `src/harness_mcp/generator.py`

The chunk loop drives `AsyncCodex.thread_start()` and applies the reset triggers from spec §7.2 (`item/started` count, wall-clock minutes). It returns `ImplementationResult` directly when the handoff declares `done`. Tests fully mock `AsyncCodex` — we test the loop logic, not the SDK.

- [ ] **Step 1: Append the failing tests.**

Append to `tests/test_generator.py`:

```python
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from harness_mcp.generator import chunk_loop
from harness_mcp.config import JobOptions


@dataclass
class FakeTurn:
    events: list

    def stream(self) -> AsyncIterator:
        async def _gen() -> AsyncIterator:
            for e in self.events:
                yield e
        return _gen()


class FakeCodex:
    """Mocks AsyncCodex for the chunk_loop tests."""

    def __init__(self, scripted_handoffs: list[str], scripted_events_per_chunk: list[list]) -> None:
        self.scripted_handoffs = scripted_handoffs
        self.scripted_events_per_chunk = scripted_events_per_chunk
        self.chunk_idx = 0
        self.calls: list[str] = []  # captured prompts

    async def __aenter__(self) -> "FakeCodex":
        return self

    async def __aexit__(self, *exc: object) -> None:  # noqa: D401
        return None

    async def thread_start(self) -> "FakeCodex":
        return self

    async def turn(self, _input: object) -> FakeTurn:
        # Materialize the handoff file, then return scripted events.
        i = self.chunk_idx
        events = self.scripted_events_per_chunk[i]
        return FakeTurn(events=list(events))


def _agent_message(text: str) -> object:
    return SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta=text))


def _item_started() -> object:
    return SimpleNamespace(
        method="item/started",
        payload=SimpleNamespace(item=SimpleNamespace(id=f"i-{id(object())}", type="commandExecution", command="ls")),
    )


def _turn_completed() -> object:
    return SimpleNamespace(
        method="turn/completed",
        payload=SimpleNamespace(turn=SimpleNamespace(id="t1", status=SimpleNamespace(value="completed"))),
    )


class TestChunkLoop:
    @pytest.mark.asyncio
    async def test_done_handoff_triggers_commit(
        self, app_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        contract = sprint_dir / "contract.md"
        contract.write_text("CONTRACT")
        design = sprint_dir / "design.md"
        design.write_text("DESIGN")
        plan_section = sprint_dir / "plan_section.md"
        plan_section.write_text("PLAN_SECTION")
        log_path = sprint_dir / "log.txt"

        # Patch AsyncCodex so we can drive chunk_loop without the real SDK.
        # The fake's `_turn` simulates Codex doing real work: it writes a real file
        # into `app_repo` (so `git add .` later actually stages something) AND
        # writes the handoff into `sprint_dir` for the chunk loop to parse.
        from harness_mcp import generator as gen_mod

        @asynccontextmanager
        async def fake_async_codex(*_a: object, **_kw: object) -> AsyncIterator[object]:
            class _Thread:
                async def turn(self, _input: object) -> FakeTurn:
                    # Codex would have written code in cwd=app_repo.
                    (app_repo / "x.py").write_text("print('hi')\n", encoding="utf-8")
                    # Codex would also have written its handoff in the sprint dir.
                    (sprint_dir / "handoff-001.md").write_text(
                        GOOD_HANDOFF.replace("in-progress", "done"), encoding="utf-8"
                    )
                    return FakeTurn(events=[_agent_message("hello"), _turn_completed()])

            class _Wrap:
                async def thread_start(self) -> _Thread:
                    return _Thread()

            yield _Wrap()

        monkeypatch.setattr(gen_mod, "AsyncCodex", fake_async_codex)

        opts = JobOptions(codex_reset_steps=10, codex_reset_minutes=1, max_codex_chunks_per_sprint=4)
        result = await chunk_loop(
            app_dir=app_repo,
            sprint_dir=sprint_dir,
            contract_path=contract,
            design_path=design,
            plan_section_path=plan_section,
            log_path=log_path,
            options=opts,
            generator_md_text="GENERATOR_PROMPT",
            sprint_seq=1,
            job_id="JOBID",
            eval_md_for_retry=None,
        )
        assert result.ok is True
        assert result.commit_sha is not None

    @pytest.mark.asyncio
    async def test_max_chunks_exhausted_returns_failure(self, app_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("CONTRACT")

        from harness_mcp import generator as gen_mod

        chunk_idx = [0]

        @asynccontextmanager
        async def fake_async_codex(*_a: object, **_kw: object) -> AsyncIterator[object]:
            class _T:
                async def turn(self, _input: object) -> FakeTurn:
                    # Always write an in-progress handoff. Loop never converges.
                    handoff_path = sprint_dir / f"handoff-{chunk_idx[0] + 1:03d}.md"
                    handoff_path.write_text(GOOD_HANDOFF, encoding="utf-8")
                    chunk_idx[0] += 1
                    return FakeTurn(events=[_agent_message("..."), _turn_completed()])

            class _Wrap:
                async def thread_start(self) -> _T:
                    return _T()

            yield _Wrap()

        monkeypatch.setattr(gen_mod, "AsyncCodex", fake_async_codex)

        opts = JobOptions(codex_reset_steps=2, codex_reset_minutes=1, max_codex_chunks_per_sprint=2)
        result = await chunk_loop(
            app_dir=app_repo,
            sprint_dir=sprint_dir,
            contract_path=sprint_dir / "contract.md",
            design_path=None,
            plan_section_path=None,
            log_path=sprint_dir / "log.txt",
            options=opts,
            generator_md_text="G",
            sprint_seq=1,
            job_id="J",
            eval_md_for_retry=None,
        )
        assert result.ok is False
        assert "chunk_cap" in (result.error or "") or "exhausted" in (result.error or "")
```

- [ ] **Step 2: Confirm the failure.**

```bash
uv run pytest tests/test_generator.py::TestChunkLoop -v
```

Expected: ImportError on `chunk_loop`.

- [ ] **Step 3: Append `chunk_loop` to `generator.py`.**

Append to `src/harness_mcp/generator.py`:

```python
# ---------- Layer 3: chunk_loop ----------


from time import monotonic  # noqa: E402

# Imported lazily so unit tests can monkeypatch AsyncCodex without bringing the SDK in.
try:
    from codex_app_server import AsyncCodex, AppServerConfig, TextInput
except ImportError:  # pragma: no cover  — only hit if SDK isn't installed during isolated unit runs
    AsyncCodex = AppServerConfig = TextInput = None  # type: ignore[assignment]

from harness_mcp.config import JobOptions
from harness_mcp.logging_setup import EventLogger


async def chunk_loop(
    *,
    app_dir: Path,
    sprint_dir: Path,
    contract_path: Path,
    design_path: Path | None,
    plan_section_path: Path | None,
    log_path: Path,
    options: JobOptions,
    generator_md_text: str,
    sprint_seq: int,
    job_id: str,
    eval_md_for_retry: Path | None,
    codex_bin: str | None = None,
    codex_config_overrides: tuple[str, ...] = ("sandbox=workspace-write", "approval_policy=never"),
) -> ImplementationResult:
    """Drive AsyncCodex through bounded chunks until handoff says `done`.

    See spec §7 for the full reset-and-handoff state machine. This function:
      * Iterates chunk_seq from 1.
      * Per chunk: open AsyncCodex, start a thread, send the chunk prompt,
        stream events into EventLogger, count `item/started` events for the
        step-budget reset, observe wall-clock for the time-budget reset.
      * After each chunk, parse handoff-NNN.md. If declares_done → commit + tag.
      * Cap at options.max_codex_chunks_per_sprint.
    """
    chunk_seq = 1
    prev_handoff: Path | None = None

    while True:
        handoff_path = sprint_dir / f"handoff-{chunk_seq:03d}.md"

        cfg = (
            AppServerConfig(
                codex_bin=codex_bin,
                cwd=str(app_dir),
                config_overrides=codex_config_overrides,
                client_name="harness-mcp",
                client_title="Harness Generator",
                client_version="0.1.0",
            )
            if AppServerConfig is not None
            else None
        )

        event_logger = EventLogger(log_path)
        step_count = 0
        chunk_started = monotonic()

        try:
            async with AsyncCodex(config=cfg) as codex:
                thread = await codex.thread_start()
                prompt = build_chunk_prompt(
                    generator_md=generator_md_text,
                    contract_path=contract_path,
                    design_path=design_path,
                    plan_section_path=plan_section_path,
                    prev_handoff=prev_handoff,
                    eval_md_for_retry=eval_md_for_retry,
                    handoff_path=handoff_path,
                    chunk_seq=chunk_seq,
                )
                turn = await thread.turn(TextInput(prompt) if TextInput else prompt)

                async for event in turn.stream():
                    await event_logger.handle(event)
                    if getattr(event, "method", "") == "item/started":
                        step_count += 1
                    if step_count >= options.codex_reset_steps:
                        break
                    if monotonic() - chunk_started >= options.codex_reset_minutes * 60:
                        break
        except Exception as e:
            await event_logger.aclose()
            from harness_mcp.types import GeneratorChunkError
            raise GeneratorChunkError(chunk_seq, e) from e
        finally:
            await event_logger.aclose()

        # Parse handoff. Malformed = warn, fresh-start, count toward cap.
        try:
            handoff = parse_handoff(handoff_path)
        except HandoffParseError:
            if chunk_seq < options.max_codex_chunks_per_sprint:
                prev_handoff = None
                chunk_seq += 1
                continue
            return ImplementationResult(ok=False, error="handoff_persistently_malformed")

        if handoff.declares_done:
            try:
                return await commit_and_summarize(
                    app_dir, handoff, sprint_seq=sprint_seq, job_id=job_id
                )
            except Exception as e:  # CommitFailedError or worse
                return ImplementationResult(ok=False, error=f"commit_failed: {e}")

        prev_handoff = handoff_path
        chunk_seq += 1
        if chunk_seq > options.max_codex_chunks_per_sprint:
            return ImplementationResult(ok=False, error="generator_chunk_cap_exhausted")
```

- [ ] **Step 4: Run the chunk-loop tests.**

```bash
uv run pytest tests/test_generator.py::TestChunkLoop -v
```

Expected: every test passes. The mock-Codex pattern is intentionally lightweight; complex SDK behavior is integration-tested in Plan 5.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/generator.py
uv run ruff format --check src/harness_mcp/generator.py
```

Expected: zero findings.

---

## Task 5: `evaluator.py` — `parse_eval_md` + `sync_eval_md` + prompt builders

**Files:**
- Create: `tests/test_evaluator.py`
- Create: `src/harness_mcp/evaluator.py`

Per spec §6.3: `parse_eval_md` walks `### Criterion <n>:` blocks under `## Static audit` and `## Dynamic verification`, reading `**Result:**`, `**Evidence:**`, `**Notes:**`. `sync_eval_md` waits for the SDK's file write to land (fsync directory + tiny sleep) and asserts the expected section header is present.

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_evaluator.py`:

```python
"""Tests for harness_mcp.evaluator — eval.md parser, sync helper, prompt builders."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from harness_mcp.evaluator import (
    dynamic_verification_prompt,
    parse_eval_md,
    static_audit_prompt,
    sync_eval_md,
)
from harness_mcp.types import EvaluatorEmittedUnparseableEvalMdError


GOOD_EVAL = dedent(
    """
    # Sprint 1 Evaluation

    ## Static audit

    ### Criterion 1: home page renders
    **Result:** PASS
    **Evidence:** app.py:42 — Flask route returns 200
    **Notes:** clean.

    ### Criterion 2: pytest passes
    **Result:** FAIL
    **Evidence:** tests/test_app.py:18 missing
    **Notes:** test file not created.

    ## Dynamic verification

    ### Routing decision
    Used Bash to run pytest. Did not need Playwright (no UI in this sprint).

    ### Criterion 1: pytest exit 0
    **Result:** FAIL
    **Evidence:** `pytest` exited with code 1
    **Notes:** see Static-audit Criterion 2.
    """
).strip()


class TestParseEvalMd:
    def test_full_parse(self, tmp_path: Path) -> None:
        path = tmp_path / "eval.md"
        path.write_text(GOOD_EVAL, encoding="utf-8")
        r = parse_eval_md(path, sprint_seq=1)
        assert r.sprint_seq == 1
        assert len(r.static_criteria) == 2
        assert r.static_criteria[0].result == "PASS"
        assert r.static_criteria[1].result == "FAIL"
        assert len(r.dynamic_criteria) == 1
        assert r.dynamic_criteria[0].result == "FAIL"
        assert "Used Bash" in r.routing_decision
        assert r.passed is False
        assert r.unparseable is False

    def test_all_pass(self, tmp_path: Path) -> None:
        body = dedent(
            """
            # Sprint 1 Evaluation
            ## Static audit
            ### Criterion 1: x
            **Result:** PASS
            **Evidence:** e
            **Notes:** n
            ## Dynamic verification
            ### Routing decision
            ran tests
            ### Criterion 1: y
            **Result:** PASS
            **Evidence:** ok
            **Notes:** ok
            """
        ).strip()
        path = tmp_path / "eval.md"
        path.write_text(body, encoding="utf-8")
        r = parse_eval_md(path, sprint_seq=1)
        assert r.passed is True
        assert r.unparseable is False

    def test_unparseable_when_no_criteria(self, tmp_path: Path) -> None:
        path = tmp_path / "eval.md"
        path.write_text("# Sprint 1 Evaluation\n\nfree-form text only\n", encoding="utf-8")
        with pytest.raises(EvaluatorEmittedUnparseableEvalMdError):
            parse_eval_md(path, sprint_seq=1)

    def test_missing_file_unparseable(self, tmp_path: Path) -> None:
        with pytest.raises(EvaluatorEmittedUnparseableEvalMdError):
            parse_eval_md(tmp_path / "missing.md", sprint_seq=1)

    def test_routing_decision_blank_when_no_dynamic(self, tmp_path: Path) -> None:
        body = dedent(
            """
            # Sprint 1 Evaluation
            ## Static audit
            ### Criterion 1: x
            **Result:** PASS
            **Evidence:** e
            **Notes:** n
            """
        ).strip()
        path = tmp_path / "eval.md"
        path.write_text(body, encoding="utf-8")
        r = parse_eval_md(path, sprint_seq=1)
        # No dynamic section at all → empty list, blank routing.
        assert r.dynamic_criteria == []
        assert r.routing_decision == ""


class TestSyncEvalMd:
    @pytest.mark.asyncio
    async def test_passes_when_section_present(self, tmp_path: Path) -> None:
        path = tmp_path / "eval.md"
        path.write_text("# Sprint 1\n\n## Static audit\n\nbody\n", encoding="utf-8")
        await sync_eval_md(path, expect_section="## Static audit")  # no exception = pass

    @pytest.mark.asyncio
    async def test_raises_when_section_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "eval.md"
        path.write_text("# Sprint 1\n\n", encoding="utf-8")
        with pytest.raises(EvaluatorEmittedUnparseableEvalMdError):
            await sync_eval_md(path, expect_section="## Static audit")


class TestPromptBuilders:
    def test_static_audit_prompt_includes_diff_command(self) -> None:
        p = static_audit_prompt(
            job_dir=Path("/tmp/job"),
            sprint_seq=2,
            prior_tag="harness/J/sprint-1",
            criteria_text="Criterion 1: x\nCriterion 2: y",
        )
        assert "harness/J/sprint-1..HEAD" in p
        assert "Criterion 1: x" in p

    def test_static_audit_prompt_first_sprint_uses_empty_tree(self) -> None:
        p = static_audit_prompt(
            job_dir=Path("/tmp/job"),
            sprint_seq=1,
            prior_tag=None,
            criteria_text="Criterion 1: x",
        )
        assert "empty tree" in p.lower() or "no prior tag" in p.lower()

    def test_dynamic_verification_prompt_demands_routing_decision(self) -> None:
        p = dynamic_verification_prompt(
            job_dir=Path("/tmp/job"),
            sprint_seq=1,
            criteria_text="Criterion 1: app responds 200",
        )
        assert "Routing decision" in p
        assert "Criterion 1" in p
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_evaluator.py -v
```

Expected: ImportError on `harness_mcp.evaluator`.

- [ ] **Step 3: Implement `evaluator.py`.**

Create `src/harness_mcp/evaluator.py`:

```python
"""Evaluator helpers: eval.md parsing, sync helper, prompt builders, log piping.

The Evaluator phase has three modes (contract review, static audit,
dynamic verification). The launcher subprocess (evaluator_runner.py)
imports these helpers and drives `ClaudeSDKClient`. The orchestrator
imports `parse_eval_md` to digest the result.

The static / dynamic prompt builders live here so the launcher and any
orchestrator-side debugging can call the same functions.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import anyio

from harness_mcp.types import (
    Criterion,
    EvaluationResult,
    EvaluatorEmittedUnparseableEvalMdError,
)


# ---------- parse_eval_md ----------


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_CRITERION_RE = re.compile(r"^###\s+Criterion\s+(\d+):\s*(.+?)\s*$", re.MULTILINE)
_RESULT_RE = re.compile(r"^\*\*Result:\*\*\s*(\w+)", re.MULTILINE)
_EVIDENCE_RE = re.compile(r"^\*\*Evidence:\*\*\s*(.+?)(?=\n\*\*|\n###|\n##|\Z)", re.MULTILINE | re.DOTALL)
_NOTES_RE = re.compile(r"^\*\*Notes:\*\*\s*(.+?)(?=\n\*\*|\n###|\n##|\Z)", re.MULTILINE | re.DOTALL)


def _section_slice(text: str, heading: str) -> str:
    """Return the body under the FIRST occurrence of `## <heading>` until the next `## ` or EOF."""
    headings = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(headings):
        if m.group(1).strip().lower() == heading.lower():
            start = m.end()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            return text[start:end]
    return ""


def _parse_criterion_blocks(section_body: str) -> list[Criterion]:
    """Walk `### Criterion <n>:` blocks; build Criterion entries."""
    out: list[Criterion] = []
    matches = list(_CRITERION_RE.finditer(section_body))
    for i, m in enumerate(matches):
        block_text = section_body[m.start() : matches[i + 1].start() if i + 1 < len(matches) else len(section_body)]
        result_m = _RESULT_RE.search(block_text)
        evidence_m = _EVIDENCE_RE.search(block_text)
        notes_m = _NOTES_RE.search(block_text)
        if not result_m:
            continue
        result = result_m.group(1).strip().upper()
        if result not in ("PASS", "FAIL"):
            continue
        out.append(
            Criterion(
                text=m.group(2).strip(),
                result=result,
                evidence=(evidence_m.group(1).strip() if evidence_m else ""),
                notes=(notes_m.group(1).strip() if notes_m else ""),
            )
        )
    return out


def _extract_routing_decision(dynamic_body: str) -> str:
    """Body under `### Routing decision` until the next `### `."""
    routing_re = re.compile(r"^###\s+Routing decision\s*$", re.MULTILINE)
    next_h3_re = re.compile(r"^###\s+", re.MULTILINE)
    m = routing_re.search(dynamic_body)
    if not m:
        return ""
    start = m.end()
    n = next_h3_re.search(dynamic_body, pos=start)
    end = n.start() if n else len(dynamic_body)
    return dynamic_body[start:end].strip()


def parse_eval_md(path: Path, *, sprint_seq: int) -> EvaluationResult:
    """Parse eval.md into an EvaluationResult.

    Raise EvaluatorEmittedUnparseableEvalMdError if the file is missing
    or contains zero parseable Criterion blocks across both sections.
    """
    if not path.is_file():
        raise EvaluatorEmittedUnparseableEvalMdError(f"eval.md missing: {path}")

    text = path.read_text(encoding="utf-8")
    static_section = _section_slice(text, "Static audit")
    dynamic_section = _section_slice(text, "Dynamic verification")

    static = _parse_criterion_blocks(static_section)
    dynamic = _parse_criterion_blocks(dynamic_section)

    if not static and not dynamic:
        raise EvaluatorEmittedUnparseableEvalMdError(
            f"eval.md contains zero parseable criterion blocks: {path}"
        )

    routing = _extract_routing_decision(dynamic_section)
    passed = bool(static or dynamic) and all(c.result == "PASS" for c in static + dynamic)

    return EvaluationResult(
        sprint_seq=sprint_seq,
        static_criteria=static,
        dynamic_criteria=dynamic,
        routing_decision=routing,
        passed=passed,
    )


# ---------- sync_eval_md ----------


def _fsync_dir(d: Path) -> None:
    """fsync a directory entry so a recently-renamed-into file is durable.

    POSIX-only; on Windows the `os.open(..., O_RDONLY)` path raises and
    we fall through silently. The harness's primary platform is POSIX.
    """
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except (PermissionError, OSError):
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def sync_eval_md(path: Path, *, expect_section: str) -> None:
    """Wait for SDK file writes to land; assert the expected section is present."""
    await anyio.to_thread.run_sync(_fsync_dir, path.parent)
    await anyio.sleep(0.1)
    if not path.is_file():
        raise EvaluatorEmittedUnparseableEvalMdError(
            f"eval.md missing after query: {path}"
        )
    text = path.read_text(encoding="utf-8")
    if expect_section not in text:
        raise EvaluatorEmittedUnparseableEvalMdError(
            f"expected section {expect_section!r} missing in {path}"
        )


# ---------- Prompt builders ----------


def static_audit_prompt(
    *, job_dir: Path, sprint_seq: int, prior_tag: str | None, criteria_text: str
) -> str:
    """User prompt for the static-audit query (spec §8.2)."""
    if prior_tag:
        diff_clause = f"Read `git diff {prior_tag}..HEAD` (run from `app/`)."
    else:
        diff_clause = (
            "There is no prior sprint tag (this is sprint 1). "
            "Diff against the empty tree by reading the entire `app/` working tree."
        )

    return f"""## Mode: static-audit

You are auditing sprint {sprint_seq}.

Read `design.md`, `plan.md`, and `contract.md` in `{job_dir}`.

{diff_clause}

For each contract criterion below, render the `### Criterion <n>:` block with **Result:** PASS or FAIL, **Evidence:** (file:line refs), and **Notes:** (reasoning):

{criteria_text}

Write your work as the `## Static audit` section of `eval.md`. Rewrite the entire file from scratch (re-include any prior content); do not partial-append.
"""


def dynamic_verification_prompt(
    *, job_dir: Path, sprint_seq: int, criteria_text: str
) -> str:
    """User prompt for the dynamic-verification query (spec §8.3)."""
    return f"""## Mode: dynamic-verification

You are dynamically verifying sprint {sprint_seq}.

Working dir: {job_dir}. Code is at `app/` (one level below). When invoking Bash, `cd app && ...`.

First, write a `### Routing decision` paragraph at the top of `## Dynamic verification`. State which tools you will drive (Playwright MCP / Bash test runner / httpx / DB inspection / nothing) and why.

Then, render `### Criterion <n>:` blocks for each contract criterion below:

{criteria_text}

If you start app processes (dev server, pytest, browsers), do your best to kill them when you finish. The orchestrator wraps you in a process group and SIGTERMs the group on your exit, so cooperating saves seconds but is not required for correctness.

Rewrite the entire `eval.md` from scratch (re-include `## Static audit` content); do not partial-append.
"""


# ---------- pipe_claude_msg_to_log ----------


async def pipe_claude_msg_to_log(msg: Any, log_path: Path) -> None:
    """Append a friendly representation of one Claude SDK message to log_path.

    Mirrors the EventLogger contract for Codex: open with line buffering,
    write atomic lines per message. Tool-call markers and result bookkeeping
    are flattened.
    """
    role = type(msg).__name__
    lines: list[str] = []
    if role == "AssistantMessage":
        for block in getattr(msg, "content", []) or []:
            block_type = type(block).__name__
            if block_type == "TextBlock":
                lines.append(getattr(block, "text", "") or "")
            elif block_type == "ToolUseBlock":
                name = getattr(block, "name", "?")
                args = getattr(block, "input", {})
                lines.append(f"[tool: {name} args={args}]")
    elif role == "ResultMessage":
        cost = getattr(msg, "total_cost_usd", "?")
        lines.append(f"--- result (cost ${cost}) ---")
    if not lines:
        return
    payload = "\n".join(lines) + "\n"

    def _write() -> None:
        with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
            fh.write(payload)

    await anyio.to_thread.run_sync(_write)
```

- [ ] **Step 4: Run the tests.**

```bash
uv run pytest tests/test_evaluator.py -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/evaluator.py tests/test_evaluator.py
uv run ruff format --check src/harness_mcp/evaluator.py tests/test_evaluator.py
```

Expected: zero findings.

---

## Task 6: `evaluator_runner.py` — launcher entry point

**Files:**
- Create: `tests/test_evaluator_runner.py`
- Create: `src/harness_mcp/evaluator_runner.py`

Per spec §8.4: this is the `python -m harness_mcp.evaluator_runner` module that the orchestrator spawns under `ProcessGroupScope`. It reads a JSON payload from stdin, drives `ClaudeSDKClient`, writes `eval.md`, and exits 0/1. **Critical: must NOT import `harness_mcp.state`** — that would open a second writer connection and race the orchestrator. We assert this at import time via a unit test.

- [ ] **Step 1: Write the failing test.**

Create `tests/test_evaluator_runner.py`:

```python
"""Tests for harness_mcp.evaluator_runner — launcher entry point.

Spec §8.4 declares two invariants that we enforce here:
  1. No transitive import of harness_mcp.state (would race the orchestrator's writer).
  2. Path validation: every path under `payload["paths"]` must live inside
     `~/.harness/jobs/<job_id>/`. Paths outside that prefix are refused.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from harness_mcp.evaluator_runner import _validate_payload_paths


class TestImportIsolation:
    def test_state_not_transitively_imported(self) -> None:
        # Wipe any prior imports of state.
        for mod in list(sys.modules):
            if mod == "harness_mcp.state":
                sys.modules.pop(mod)

        # Re-import the runner; state must NOT be in sys.modules afterwards.
        for mod in list(sys.modules):
            if mod.startswith("harness_mcp.evaluator_runner"):
                sys.modules.pop(mod)
        importlib.import_module("harness_mcp.evaluator_runner")
        assert "harness_mcp.state" not in sys.modules, (
            "harness_mcp.state imported transitively from evaluator_runner; "
            "this races the orchestrator's writer connection."
        )


class TestValidatePayloadPaths:
    def test_accepts_paths_under_job_dir(self, tmp_harness_home: Path) -> None:
        job_dir = tmp_harness_home / "jobs" / "JOBID"
        job_dir.mkdir(parents=True)
        (job_dir / "app").mkdir()
        payload = {
            "job_id": "JOBID",
            "paths": {
                "design": str(job_dir / "design.md"),
                "plan": str(job_dir / "plan.md"),
                "contract": str(job_dir / "sprint-1" / "contract.md"),
                "eval": str(job_dir / "sprint-1" / "eval.md"),
                "app": str(job_dir / "app"),
                "log": str(job_dir / "sprint-1" / "log.txt"),
            },
        }
        _validate_payload_paths(payload)  # no exception

    def test_rejects_path_outside_job_dir(self, tmp_harness_home: Path) -> None:
        payload = {
            "job_id": "JOBID",
            "paths": {
                "design": "/tmp/elsewhere/design.md",
                "plan": "/tmp/elsewhere/plan.md",
                "contract": "/tmp/elsewhere/contract.md",
                "eval": "/tmp/elsewhere/eval.md",
                "app": "/tmp/elsewhere/app",
                "log": "/tmp/elsewhere/log.txt",
            },
        }
        with pytest.raises(ValueError):
            _validate_payload_paths(payload)

    def test_rejects_missing_required_path_key(self, tmp_harness_home: Path) -> None:
        payload = {"job_id": "JOBID", "paths": {"design": str(tmp_harness_home)}}
        with pytest.raises(ValueError):
            _validate_payload_paths(payload)
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_evaluator_runner.py -v
```

Expected: ImportError on `harness_mcp.evaluator_runner`.

- [ ] **Step 3: Implement `evaluator_runner.py`.**

Create `src/harness_mcp/evaluator_runner.py`:

```python
"""Evaluator launcher subprocess: drives ClaudeSDKClient inside its own pgroup.

Invoked via `python -m harness_mcp.evaluator_runner`. Reads a JSON payload
from stdin (schema in spec §8.4), runs static + dynamic queries, writes
eval.md, exits 0 on success or 1 on internal error.

Module-level imports:
  * ALLOWED: harness_mcp.types, harness_mcp.config, harness_mcp.evaluator,
    harness_mcp.prompts_loader, claude_agent_sdk, anyio, stdlib.
  * FORBIDDEN: harness_mcp.state (would race orchestrator's writer
    connection). Enforced by tests/test_evaluator_runner.py::TestImportIsolation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from harness_mcp.config import jobs_root  # safe — pure path helper, no DB
from harness_mcp.evaluator import (
    dynamic_verification_prompt,
    parse_eval_md,
    pipe_claude_msg_to_log,
    static_audit_prompt,
    sync_eval_md,
)
from harness_mcp.prompts_loader import _resolved_prompt_text


REQUIRED_PATH_KEYS = ("design", "plan", "contract", "eval", "app", "log")


def _validate_payload_paths(payload: dict[str, Any]) -> None:
    """Refuse payloads whose paths leave the job directory.

    Each path under `payload["paths"]` must resolve to a location inside
    `<jobs_root>/<payload["job_id"]>/`. This is the launcher's only
    sandbox primitive — without it, a malformed payload could direct the
    Evaluator to write outside ~/.harness.
    """
    job_id = payload.get("job_id")
    paths = payload.get("paths") or {}
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("payload missing string job_id")
    for key in REQUIRED_PATH_KEYS:
        if key not in paths:
            raise ValueError(f"payload.paths missing key {key!r}")

    job_root = (jobs_root() / job_id).resolve()
    for key, raw in paths.items():
        if not isinstance(raw, str):
            raise ValueError(f"payload.paths[{key!r}] must be a string")
        # The path may not exist yet (eval.md gets written by us), so we resolve
        # parents instead. Using is_relative_to() works for not-yet-existing files
        # because it operates on lexical path components.
        try:
            target = Path(raw).resolve(strict=False)
        except (OSError, RuntimeError) as e:
            raise ValueError(f"payload.paths[{key!r}] could not be resolved: {e}") from e
        if not (target == job_root or job_root in target.parents):
            raise ValueError(
                f"payload.paths[{key!r}]={raw} is outside job dir {job_root}"
            )


async def _run(payload: dict[str, Any]) -> int:
    """Drive ClaudeSDKClient through static + dynamic queries."""
    import anyio
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    paths = payload["paths"]
    job_dir = Path(paths["eval"]).parent.parent  # eval lives at <job>/sprint-N/eval.md
    sprint_seq = payload["sprint_seq"]
    log_path = Path(paths["log"])
    eval_path = Path(paths["eval"])
    contract_path = Path(paths["contract"])
    setting_sources = payload.get("setting_sources", ["user"])
    captured_mcp = payload.get("captured_mcp_stanzas", {})
    max_eval_seconds = int(payload.get("max_evaluation_seconds", 1800))

    contract_text = contract_path.read_text(encoding="utf-8") if contract_path.is_file() else ""
    prior_tag = payload.get("prior_tag")  # str | None

    options = ClaudeAgentOptions(
        system_prompt=_resolved_prompt_text("evaluator.md"),
        cwd=str(job_dir),
        setting_sources=setting_sources,
        mcp_servers={name: dict(stanza) for name, stanza in captured_mcp.items()},
        extra_args={"strict-mcp-config": None},
        permission_mode="bypassPermissions",
    )

    with anyio.fail_after(max_eval_seconds):
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                static_audit_prompt(
                    job_dir=job_dir,
                    sprint_seq=sprint_seq,
                    prior_tag=prior_tag,
                    criteria_text=contract_text,
                )
            )
            async for msg in client.receive_response():
                await pipe_claude_msg_to_log(msg, log_path)
            await sync_eval_md(eval_path, expect_section="## Static audit")

            await client.query(
                dynamic_verification_prompt(
                    job_dir=job_dir,
                    sprint_seq=sprint_seq,
                    criteria_text=contract_text,
                )
            )
            async for msg in client.receive_response():
                await pipe_claude_msg_to_log(msg, log_path)
            await sync_eval_md(eval_path, expect_section="## Dynamic verification")

    # Sanity: parse_eval_md confirms structural validity before returning.
    parse_eval_md(eval_path, sprint_seq=sprint_seq)
    return 0


def main() -> int:
    """Entry point for `python -m harness_mcp.evaluator_runner`."""
    import anyio

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"evaluator_runner: bad JSON on stdin: {e}", file=sys.stderr)
        return 1

    try:
        _validate_payload_paths(payload)
    except ValueError as e:
        print(f"evaluator_runner: invalid payload: {e}", file=sys.stderr)
        return 1

    try:
        return anyio.run(_run, payload)
    except Exception as e:  # noqa: BLE001 — top-level error reporter
        print(f"evaluator_runner: failed: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests.**

```bash
uv run pytest tests/test_evaluator_runner.py -v
```

Expected: every test passes — including the import-isolation invariant.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/evaluator_runner.py tests/test_evaluator_runner.py
uv run ruff format --check src/harness_mcp/evaluator_runner.py tests/test_evaluator_runner.py
```

Expected: zero findings.

---

## Task 7: Final sweep + import-graph audit

- [ ] **Step 1: Full pytest run.**

```bash
uv run pytest tests/ -v
```

Expected: every test from Parts 1, 2, and 3 passes.

- [ ] **Step 2: Full ruff lint + format.**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: `All checks passed!` and the format check reports no diffs.

- [ ] **Step 3: Confirm launcher import isolation a second time.**

Important enough to double-check at the end of this plan:

```bash
uv run python -c "
import importlib, sys
for mod in list(sys.modules):
    if mod == 'harness_mcp.state': sys.modules.pop(mod)
importlib.import_module('harness_mcp.evaluator_runner')
assert 'harness_mcp.state' not in sys.modules, 'state.py leaked into launcher'
print('OK: launcher does not import state')
"
```

Expected: `OK: launcher does not import state`.

- [ ] **Step 4: Confirm we are still on `main`.**

```bash
git status --short && git rev-parse --abbrev-ref HEAD
```

Expected: untracked / modified files only, branch `main`. **Do not commit.**

---

## Done criteria

- All 7 tasks complete.
- `uv run pytest tests/ -v` passes (Parts 1 + 2 + 3).
- `uv run ruff check .` and `uv run ruff format --check .` exit 0.
- `harness_mcp.evaluator_runner` does not transitively import `harness_mcp.state`.
- Repo on `main`, NO commits.

The next plan in the series (Part 4: Planning, Summarizer, Prereqs) builds the planning loop, summarization pass, and lifespan startup checks. It depends on `harness_mcp.contracts` (for the APPROVED parser), `harness_mcp.prompts_loader`, and `harness_mcp.mcp_capture`.
