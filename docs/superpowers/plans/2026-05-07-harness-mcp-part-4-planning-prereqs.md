# Harness MCP — Part 4: Planning, Summarizer & Prereqs

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build three focused Claude-SDK call sites: `planning.py` (plan v1 + review loop with `[implementation]`/`[design]` tag filtering, sprint extraction), `summarizer.py` (final 2–3-sentence summary), and `prereqs.py` (lifespan startup checks — paths, env, Codex binary + sandbox probe, skill probe, MCP capture + assertion, restart sweep).

**Architecture:** Each module surfaces async functions that the orchestrator calls. Planning is the most complex — it has plan parsing, the round-by-round review state machine, the tag-filtering logic that drops `[design]` issues, and sprint extraction (`## Sprint N: <Title>`). Prereqs is the load-bearing startup contract: any failure refuses the server. All three modules use `claude_agent_sdk.query()` (one-shot) — `ClaudeSDKClient` is reserved for the Evaluator (Part 3).

**Tech Stack:** `claude_agent_sdk`, `anyio`, stdlib (`re`, `subprocess`, `tempfile`, `shutil`).

**Spec source:** `docs/superpowers/specs/2026-05-07-harness-mcp-design.md` — sections §5 (plan phase), §5.1, §5.2, §5.3 (sprint extraction), §6.6 (summarizer), §10.1 (lifespan), §10.4–§10.5 (MCP allowlist + Codex overrides) are load-bearing.

**Depends on:** Parts 1 + 2 + 3. Specifically uses `harness_mcp.types`, `config`, `state`, `prompts_loader`, `contracts.append_round_atomic`, `mcp_capture`.

---

## Branch & Commit Policy (READ FIRST)

- **Stay on the `main` branch for the entire plan.** Do not create or switch branches.
- **Do NOT run `git commit`, `git add`, `git push`, or any git mutation.** Verify by running tests / inspecting files only.
- The Codex SDK probe in `prereqs.py` does run `git init` against a *temp directory* (so that `AsyncCodex.thread_start()` has a valid repo to operate in). That's runtime behavior in the user's home directory at server startup; it does not commit to this harness repo.
- If a step's check fails, fix the problem and re-run the check — never paper over with a commit.

---

## File Structure (this part owns)

| File | Purpose |
|---|---|
| `src/harness_mcp/planning.py` | `run_planner` (one-shot Claude call producing `plan-vN.md`), `run_reviewer` (writes `review-vN.md`), `parse_review` (extracts status + tagged issues), `verify_skill_invoked` (post-hoc check on `claude_agent_sdk` tool-use blocks), `extract_sprints` (parse `## Sprint N: <Title>` markers), `run_plan_phase` (the full §5 loop) |
| `src/harness_mcp/summarizer.py` | `run_summarizer` (one-shot Claude call producing `summary.md`); reads `design.md`, `plan.md`, every `sprint-N/eval.md` |
| `src/harness_mcp/prereqs.py` | `run_prereqs` (the §10.1 lifespan sequence): path resolution, env check, Codex binary + shape probe (with the §10.1.2b override-form matrix), skill probe, MCP probe + capture + strict-mcp-config assertion, restart sweep. Also `format_doctor_report` for the `harness-mcp doctor` CLI. |
| `tests/test_planning.py` | Plan parsing, sprint marker regex, review-tag filter (untagged → implementation, all-design dropped), `verify_skill_invoked` |
| `tests/test_summarizer.py` | Reads from a fixture job dir, asserts user prompt assembly. Mocks `claude_agent_sdk.query`. |
| `tests/test_prereqs.py` | Each step's pass + fail path with mocks (env vars, fake `which`, fake SDK clients) |

---

## Pre-flight (one-time, before Task 1)

- [ ] **Step 0a: Confirm Parts 1 + 2 + 3 artifacts exist.**

```bash
test -f src/harness_mcp/state.py && \
test -f src/harness_mcp/contracts.py && \
test -f src/harness_mcp/generator.py && \
test -f src/harness_mcp/evaluator.py && \
test -f src/harness_mcp/evaluator_runner.py && \
test -f src/harness_mcp/mcp_capture.py && echo OK
```

Expected: `OK`.

- [ ] **Step 0b: Run the existing test suite.**

```bash
uv run pytest -q
```

Expected: green.

---

## Task 1: `planning.py` — sprint extraction + review parser

**Files:**
- Create: `tests/test_planning.py`
- Create: `src/harness_mcp/planning.py`

Layer 1 of planning: pure parsing primitives. `extract_sprints` reads `^## Sprint (\d+): (.+)$` from a plan file. `parse_review` reads the latest `**Status:**` line and walks bullets under `**Issues (if any):**`, applying the tag-filter rules from §5.2.

- [ ] **Step 1: Write the failing tests for parsing primitives.**

Create `tests/test_planning.py`:

```python
"""Tests for harness_mcp.planning — parsers, review-loop, plan-phase driver."""

from __future__ import annotations

from collections.abc import AsyncIterator  # noqa: F401  — used by patched_query (Task 2)
from contextlib import contextmanager  # noqa: F401  — used by patched_query (Task 2)
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import patch  # noqa: F401  — used by patched_query (Task 2)

import pytest

from harness_mcp.config import JobOptions  # noqa: F401  — used by TestRunPlanPhase (Task 2)
from harness_mcp.planning import (
    extract_sprints,
    parse_review,
    run_plan_phase,  # noqa: F401  — used by TestRunPlanPhase (Task 2)
    verify_skill_invoked,
)


# Stand-in classes — names MUST match exactly because production code checks
# `type(obj).__name__ == "AssistantMessage" / "ToolUseBlock" / "TextBlock"` (avoids
# importing the SDK at module level so the launcher can stay free of state.py).
# No underscore prefix and no SDK import here means no name collision.
class TextBlock:
    """Stand-in for claude_agent_sdk.TextBlock."""
    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    """Stand-in for claude_agent_sdk.ToolUseBlock."""
    def __init__(self, name: str, inp: dict[str, Any] | None = None) -> None:
        self.name = name
        self.input = inp or {}


class AssistantMessage:
    """Stand-in for claude_agent_sdk.AssistantMessage."""
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class ResultMessage:
    """Stand-in for claude_agent_sdk.ResultMessage."""
    def __init__(self, cost: float = 0.01) -> None:
        self.total_cost_usd = cost


SAMPLE_PLAN = dedent(
    """
    # TODO App Implementation Plan

    ## Sprint 1: REST API skeleton
    Build GET/POST endpoints.

    ## Sprint 2: Web UI
    Add the form-and-list page.

    ## Sprint 3: Persistence
    Wire up SQLite.
    """
).strip()


SAMPLE_REVIEW_APPROVED = dedent(
    """
    ## Plan Review

    **Status:** Approved

    **Recommendations (advisory, do not block approval):**
    - Consider integrating httpx as a future enhancement.
    """
).strip()


SAMPLE_REVIEW_ISSUES_FOUND = dedent(
    """
    ## Plan Review

    **Status:** Issues Found

    **Issues (if any):**
    - [implementation] Sprint 1, Step 3: missing actual SQL schema.
    - [design] Acceptance criteria for sprint 2 are subjective.
    - [implementation] Sprint 3 has no test plan.

    **Recommendations (advisory, do not block approval):**
    - Add an example data fixture.
    """
).strip()


SAMPLE_REVIEW_ALL_DESIGN = dedent(
    """
    ## Plan Review

    **Status:** Issues Found

    **Issues (if any):**
    - [design] Goal isn't measurable.
    - [design] Persistence requirements ambiguous.
    """
).strip()


SAMPLE_REVIEW_UNTAGGED = dedent(
    """
    ## Plan Review

    **Status:** Issues Found

    **Issues (if any):**
    - missing test plan for sprint 1.
    - [implementation] no error handling described.
    """
).strip()


SAMPLE_REVIEW_QUOTED_STATUS = dedent(
    """
    ## Plan Review

    **Status:** Issues Found

    **Issues (if any):**
    - [implementation] The reviewer suggested adding `**Status:** Approved` to your plan, which the orchestrator misparses.

    **Status:** Approved
    """
).strip()


class TestExtractSprints:
    def test_three_sprints(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text(SAMPLE_PLAN, encoding="utf-8")
        sprints = extract_sprints(plan)
        assert sprints == [
            (1, "REST API skeleton"),
            (2, "Web UI"),
            (3, "Persistence"),
        ]

    def test_zero_sprints(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("# No sprints here\n\nfree-form.\n", encoding="utf-8")
        assert extract_sprints(plan) == []

    def test_handles_extra_whitespace(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text(
            "## Sprint 1:    Trim me   \n\nbody\n",
            encoding="utf-8",
        )
        assert extract_sprints(plan) == [(1, "Trim me")]


class TestParseReview:
    def test_approved(self, tmp_path: Path) -> None:
        path = tmp_path / "review-v1.md"
        path.write_text(SAMPLE_REVIEW_APPROVED, encoding="utf-8")
        result = parse_review(path)
        assert result.status == "Approved"
        assert result.implementation_issues == []

    def test_issues_found_filters_design(self, tmp_path: Path) -> None:
        path = tmp_path / "review-v1.md"
        path.write_text(SAMPLE_REVIEW_ISSUES_FOUND, encoding="utf-8")
        result = parse_review(path)
        assert result.status == "Issues Found"
        # [design] entries dropped; [implementation] retained, in order.
        assert len(result.implementation_issues) == 2
        assert "missing actual SQL schema" in result.implementation_issues[0]
        assert "no test plan" in result.implementation_issues[1]

    def test_all_design_returns_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "review-v1.md"
        path.write_text(SAMPLE_REVIEW_ALL_DESIGN, encoding="utf-8")
        result = parse_review(path)
        assert result.status == "Issues Found"
        assert result.implementation_issues == []  # → loop exits as if approved

    def test_untagged_defaults_to_implementation(self, tmp_path: Path) -> None:
        path = tmp_path / "review-v1.md"
        path.write_text(SAMPLE_REVIEW_UNTAGGED, encoding="utf-8")
        result = parse_review(path)
        assert len(result.implementation_issues) == 2

    def test_uses_last_status_line(self, tmp_path: Path) -> None:
        # Spec §5.2: parser uses the LAST `**Status:**` match (guards quoted examples).
        path = tmp_path / "review-v1.md"
        path.write_text(SAMPLE_REVIEW_QUOTED_STATUS, encoding="utf-8")
        result = parse_review(path)
        assert result.status == "Approved"

    def test_caps_at_top_30(self, tmp_path: Path) -> None:
        bullets = "\n".join(f"- [implementation] issue {i}" for i in range(50))
        body = (
            "## Plan Review\n\n**Status:** Issues Found\n\n"
            "**Issues (if any):**\n" + bullets + "\n"
        )
        path = tmp_path / "review-v1.md"
        path.write_text(body, encoding="utf-8")
        result = parse_review(path)
        assert len(result.implementation_issues) == 30


class TestVerifySkillInvoked:
    # Uses the `ToolUseBlock` class defined later in this file (TestRunPlanPhase
    # section) — `verify_skill_invoked` checks `type(block).__name__ == "ToolUseBlock"`,
    # so we instantiate a class literally named ToolUseBlock instead of mutating
    # SimpleNamespace.__class__.__name__ (which is read-only and raises TypeError).
    def test_finds_writing_plans_invocation(self) -> None:
        block = ToolUseBlock("Skill", {"skill": "superpowers:writing-plans"})
        assert verify_skill_invoked([block], skill="writing-plans") is True

    def test_finds_via_name_field(self) -> None:
        block = ToolUseBlock("Skill", {"name": "superpowers:writing-plans"})
        assert verify_skill_invoked([block], skill="writing-plans") is True

    def test_returns_false_when_skill_not_invoked(self) -> None:
        block = ToolUseBlock("Skill", {"skill": "code-review:code-review"})
        assert verify_skill_invoked([block], skill="writing-plans") is False

    def test_ignores_non_skill_blocks(self) -> None:
        block = ToolUseBlock("Read", {"file_path": "x"})
        assert verify_skill_invoked([block], skill="writing-plans") is False
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_planning.py -v
```

Expected: ImportError on `harness_mcp.planning`.

- [ ] **Step 3: Implement parsing primitives in `planning.py`.**

Create `src/harness_mcp/planning.py`:

```python
"""Plan + review loop driver and parsers.

Layer 1 (this file's first half): pure parsers.
  * extract_sprints — parse `## Sprint N: <Title>` markers from plan.md.
  * parse_review — pull `**Status:**` (last match) and `**Issues:**` bullets
    from review-vN.md; drop `[design]` tags; default untagged to
    `[implementation]`; cap at 30.
  * verify_skill_invoked — walk a list of ToolUseBlocks for the
    `superpowers:writing-plans` skill invocation.

Layer 2 (this file's second half, added in Task 2):
  * run_planner / run_reviewer — claude_agent_sdk.query() drivers.
  * run_plan_phase — the full §5 plan + review loop.
"""

from __future__ import annotations

import re
from collections.abc import Callable  # noqa: F401  — used by Layer 2 type aliases (Task 2)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_mcp.config import JobOptions  # noqa: F401  — used by run_plan_phase (Task 2)
from harness_mcp.types import HarnessToolError  # noqa: F401  — subclassed by PlanPhaseFailed in Layer 2 (Task 2)

# The SDK is imported lazily so unit tests can swap `query` via monkeypatch
# without making it a hard import (and to keep the module fast to import).
try:
    from claude_agent_sdk import query  # type: ignore[import-untyped]  # noqa: F401  — used by Layer 2's _drive_query (Task 2)
except ImportError:  # pragma: no cover
    query = None  # type: ignore[assignment]

# Regex anchors per spec §5.2 / §5.3.
_SPRINT_RE = re.compile(r"^##\s+Sprint\s+(\d+):\s*(.+?)\s*$", re.MULTILINE)
_STATUS_LINE_RE = re.compile(r"^\*\*Status:\*\*\s*(.+?)\s*$", re.MULTILINE)
_ISSUES_HEADER_RE = re.compile(r"^\*\*Issues \(if any\):\*\*\s*$", re.MULTILINE)
_NEXT_HEADER_RE = re.compile(r"^\*\*[^*]+:\*\*\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[\s]*-\s+(.+?)\s*$", re.MULTILINE)
_IMPL_TAG_RE = re.compile(r"^\[implementation\]\s*", re.IGNORECASE)
_DESIGN_TAG_RE = re.compile(r"^\[design\]\s*", re.IGNORECASE)

_MAX_FORWARDED_ISSUES = 30


@dataclass(frozen=True)
class ReviewResult:
    """Parsed plan-history/review-vN.md."""

    status: str  # raw word from the last `**Status:**` line, e.g. "Approved" or "Issues Found"
    implementation_issues: list[str]  # tag-filtered, capped at 30


def extract_sprints(plan_path: Path) -> list[tuple[int, str]]:
    """Return [(seq, title), ...] in document order."""
    if not plan_path.is_file():
        return []
    text = plan_path.read_text(encoding="utf-8")
    return [(int(m.group(1)), m.group(2).strip()) for m in _SPRINT_RE.finditer(text)]


def parse_review(review_path: Path) -> ReviewResult:
    """Parse review-vN.md per spec §5.2."""
    text = review_path.read_text(encoding="utf-8") if review_path.is_file() else ""

    # Status: take the LAST match. Guards a literal `**Status:**` inside a quoted example.
    status_matches = list(_STATUS_LINE_RE.finditer(text))
    status_word = status_matches[-1].group(1).strip() if status_matches else ""

    if status_word.lower() == "approved":
        return ReviewResult(status="Approved", implementation_issues=[])

    issues: list[str] = []
    issues_header = _ISSUES_HEADER_RE.search(text)
    if issues_header:
        # Bound the issues block by the next bold-header line (e.g., **Recommendations**).
        end = len(text)
        for n in _NEXT_HEADER_RE.finditer(text, pos=issues_header.end()):
            if n.start() > issues_header.end():
                end = n.start()
                break

        block = text[issues_header.end() : end]
        for m in _BULLET_RE.finditer(block):
            bullet = m.group(1).strip()
            if _DESIGN_TAG_RE.match(bullet):
                continue  # drop [design] issues
            stripped = _IMPL_TAG_RE.sub("", bullet)  # strip [implementation] prefix
            issues.append(stripped.strip())
            if len(issues) >= _MAX_FORWARDED_ISSUES:
                break

    return ReviewResult(status=status_word or "Issues Found", implementation_issues=issues)


def verify_skill_invoked(tool_uses: list[Any], *, skill: str) -> bool:
    """Walk Claude SDK ToolUseBlocks looking for the named skill invocation.

    Robust to the input-key variation between SDK versions (`skill` vs.
    `name`). Match is case-insensitive substring on the resolved arg.
    """
    for block in tool_uses:
        if type(block).__name__ != "ToolUseBlock":
            continue
        if getattr(block, "name", "") != "Skill":
            continue
        inp = getattr(block, "input", None) or {}
        skill_arg = inp.get("skill") or inp.get("name") or ""
        if skill.lower() in str(skill_arg).lower():
            return True
    return False
```

- [ ] **Step 4: Run the parsing tests.**

```bash
uv run pytest tests/test_planning.py -v -k "TestExtractSprints or TestParseReview or TestVerifySkillInvoked"
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/planning.py tests/test_planning.py
uv run ruff format --check src/harness_mcp/planning.py tests/test_planning.py
```

Expected: zero findings.

---

## Task 2: `planning.py` — `run_planner`, `run_reviewer`, plan phase driver

**Files:**
- Modify: `tests/test_planning.py`
- Modify: `src/harness_mcp/planning.py`

Layer 2: the actual `claude_agent_sdk.query()` drivers and the §5 plan/review loop. Tests fully mock `query` so we exercise the loop logic deterministically.

- [ ] **Step 1: Append failing tests for the loop driver.**

All imports needed by these tests (`contextmanager`, `patch`, `Any`, `run_plan_phase`, `JobOptions`) were hoisted to the top of `tests/test_planning.py` in Task 1 Step 1, so this step appends only test fixtures + classes — no new imports.

Append to `tests/test_planning.py`:

```python
@contextmanager
def patched_query(scripts: list[list[Any]]):
    """Patch `claude_agent_sdk.query` to yield scripted message lists in turn.

    Each item in `scripts` is a `(side_effect_callable, msgs_list)` tuple. On
    each query() call we run the side effect (e.g., writing the plan or
    review file the real Planner/Reviewer would have written) then yield
    the canned messages.
    """
    from harness_mcp import planning as plan_mod

    call_count = [0]

    async def fake_query(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        i = call_count[0]
        call_count[0] += 1
        # The scripts list also encodes side-effect functions (write plan/review).
        # Each script entry is (side_effect_callable, msgs_list).
        side_effect, msgs = scripts[i]
        async def _gen():
            side_effect()
            for m in msgs:
                yield m
        # Return the async generator instance directly (query() in real life
        # is also an async iterator factory).
        async for x in _gen():
            yield x

    with patch.object(plan_mod, "query", fake_query):
        yield


# `TextBlock`, `ToolUseBlock`, `AssistantMessage`, `ResultMessage` are defined
# at the top of `tests/test_planning.py` (Task 1 Step 1) — reused here.


def _make_assistant_with_skill_call() -> AssistantMessage:
    return AssistantMessage(
        content=[
            TextBlock("ok"),
            ToolUseBlock("Skill", {"skill": "superpowers:writing-plans"}),
        ]
    )


def _make_result_msg() -> ResultMessage:
    return ResultMessage()


class TestRunPlanPhase:
    @pytest.mark.asyncio
    async def test_approved_first_round(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "design.md").write_text("DESIGN")
        (job_dir / "plan-history").mkdir()

        def write_plan_v1():
            (job_dir / "plan-history" / "plan-v1.md").write_text(SAMPLE_PLAN)

        def write_review_v1():
            (job_dir / "plan-history" / "review-v1.md").write_text(SAMPLE_REVIEW_APPROVED)

        scripts = [
            (write_plan_v1, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_v1, [_make_result_msg()]),
        ]

        with patched_query(scripts):
            from harness_mcp.planning import run_plan_phase
            sprints, rounds = await run_plan_phase(
                job_dir=job_dir,
                options=JobOptions(),
                planner_options_factory=lambda **_kw: object(),  # opaque; mock doesn't read
                reviewer_options_factory=lambda **_kw: object(),
            )
            assert rounds == 0    # 0 review-driven revisions
            assert (job_dir / "plan.md").is_file()
            assert len(sprints) == 3

    @pytest.mark.asyncio
    async def test_revises_then_approves(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "design.md").write_text("DESIGN")
        (job_dir / "plan-history").mkdir()

        def write_v1(): (job_dir / "plan-history" / "plan-v1.md").write_text(SAMPLE_PLAN)
        def write_review_with_issues(): (job_dir / "plan-history" / "review-v1.md").write_text(SAMPLE_REVIEW_ISSUES_FOUND)
        def write_v2(): (job_dir / "plan-history" / "plan-v2.md").write_text(SAMPLE_PLAN)
        def write_review_approved(): (job_dir / "plan-history" / "review-v2.md").write_text(SAMPLE_REVIEW_APPROVED)

        scripts = [
            (write_v1, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_with_issues, [_make_result_msg()]),
            (write_v2, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_approved, [_make_result_msg()]),
        ]

        with patched_query(scripts):
            sprints, rounds = await run_plan_phase(
                job_dir=job_dir,
                options=JobOptions(),
                planner_options_factory=lambda **_kw: object(),
                reviewer_options_factory=lambda **_kw: object(),
            )
            assert rounds == 1
            assert (job_dir / "plan.md").is_file()
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_planning.py::TestRunPlanPhase -v
```

Expected: ImportError on `run_plan_phase`.

- [ ] **Step 3: Append the loop driver to `planning.py`.**

All imports already live at the top of the file (Task 1 Step 3 already added `Callable`, `JobOptions`, `HarnessToolError`, and the lazy `query` import). This step appends only definitions, no new imports.

Append to `src/harness_mcp/planning.py`:

```python
# Layer 2: SDK drivers + the §5 loop


class PlanPhaseFailed(HarnessToolError):
    """Raised when the plan phase exhausts review rounds or hits a structural error."""


# `*_options_factory` is a callable that returns a ClaudeAgentOptions instance per spawn.
# The orchestrator (Plan 5) constructs these — this module is decoupled from the
# captured-MCP / setting-sources state owned by `prereqs.py`.
PlannerOptionsFactory = Callable[..., Any]
ReviewerOptionsFactory = Callable[..., Any]


async def _drive_query(
    user_prompt: str,
    options: Any,
    log_path: Path | None = None,
) -> list[Any]:
    """Drive a single one-shot query() call to completion, returning the message list."""
    msgs: list[Any] = []
    async for m in query(prompt=user_prompt, options=options):
        msgs.append(m)
        # Optional: stream TextBlock content to log_path. Keeping it simple here.
    return msgs


def _collect_tool_uses(msgs: list[Any]) -> list[Any]:
    out: list[Any] = []
    for msg in msgs:
        if type(msg).__name__ != "AssistantMessage":
            continue
        for block in getattr(msg, "content", []) or []:
            if type(block).__name__ == "ToolUseBlock":
                out.append(block)
    return out


def _plan_is_structurally_valid(path: Path) -> bool:
    """True if `path` exists and contains at least one `^## Sprint \\d+:` line."""
    if not path.is_file():
        return False
    return bool(extract_sprints(path))


async def run_planner(
    *,
    user_prompt: str,
    options: Any,
    expected_plan_path: Path,
    require_skill: bool,
    log_path: Path | None = None,
) -> None:
    """One round of Planner. Verifies skill invocation AND that the written
    plan exists and contains at least one `## Sprint N:` marker.

    Per spec §5.1 step 7 / §5.2 step 0: on structural failure (missing file
    OR no Sprint markers) we re-prompt the Planner once with the explicit
    failure description; second consecutive failure raises PlanPhaseFailed.
    """
    msgs = await _drive_query(user_prompt, options, log_path)

    if require_skill:
        tool_uses = _collect_tool_uses(msgs)
        if not verify_skill_invoked(tool_uses, skill="writing-plans"):
            # Re-prompt once, then accept regardless. Spec §5.1 step 6.
            retry = user_prompt + (
                "\n\nYou forgot to invoke superpowers:writing-plans via the Skill tool. "
                "Please do so now."
            )
            await _drive_query(retry, options, log_path)

    if not _plan_is_structurally_valid(expected_plan_path):
        # Spec §5.1 step 7 / §5.2 step 0 — single retry with explicit message
        # covering BOTH missing-file and no-marker cases.
        retry = (
            user_prompt
            + f"\n\nThe file at {expected_plan_path} either does not exist or contains no "
              "`## Sprint N:` markers; please write the plan there as one H2 per sprint."
        )
        await _drive_query(retry, options, log_path)

    if not _plan_is_structurally_valid(expected_plan_path):
        raise PlanPhaseFailed(
            f"planner_emitted_unstructured_plan_after_retry: {expected_plan_path}"
        )


async def run_reviewer(
    *,
    user_prompt: str,
    options: Any,
    expected_review_path: Path,
    log_path: Path | None = None,
) -> None:
    msgs = await _drive_query(user_prompt, options, log_path)
    _ = msgs  # drained for side effects
    if not expected_review_path.is_file():
        raise PlanPhaseFailed(f"reviewer did not write {expected_review_path}")


def _planner_user_prompt(job_dir: Path, plan_version: int, issues: list[str] | None) -> str:
    if issues:
        issues_section = "\n## Issues to address\n" + "\n".join(f"- {i}" for i in issues)
    else:
        issues_section = ""
    return f"""Read `design.md` and write a plan to `plan-history/plan-v{plan_version}.md` using `## Sprint N: <Title>` H2 markers.

Working dir: {job_dir}
{issues_section}
"""


def _reviewer_user_prompt(job_dir: Path, plan_version: int) -> str:
    return f"""Read `design.md` and `plan-history/plan-v{plan_version}.md`. Write your review to `plan-history/review-v{plan_version}.md` per the rubric in your system prompt.

Working dir: {job_dir}
"""


async def run_plan_phase(
    *,
    job_dir: Path,
    options: JobOptions,
    planner_options_factory: PlannerOptionsFactory,
    reviewer_options_factory: ReviewerOptionsFactory,
    log_path: Path | None = None,
) -> tuple[list[tuple[int, str]], int]:
    """Run the full §5 plan + review loop.

    Returns (sprints, rounds_taken). `rounds_taken` is the number of
    review-driven revisions, not counting the initial Planner call.
    Writes the final approved plan to `<job_dir>/plan.md`.
    """
    plan_version = 1
    rounds = 0
    last_issues: list[str] = []

    while True:
        plan_path = job_dir / "plan-history" / f"plan-v{plan_version}.md"
        review_path = job_dir / "plan-history" / f"review-v{plan_version}.md"

        # Planner.
        planner_options = planner_options_factory(job_dir=job_dir, plan_version=plan_version)
        await run_planner(
            user_prompt=_planner_user_prompt(job_dir, plan_version, last_issues if rounds > 0 else None),
            options=planner_options,
            expected_plan_path=plan_path,
            require_skill=(plan_version == 1),  # only enforce on v1
            log_path=log_path,
        )

        # Pre-review structural check. `run_planner` already retried once on
        # missing file / missing Sprint markers; if we still see zero sprints
        # here, that's the second consecutive structural failure → fail hard.
        sprints = extract_sprints(plan_path)
        if not sprints:
            raise PlanPhaseFailed(
                f"plan-v{plan_version} contains no `## Sprint N:` markers after retry"
            )
        if len(sprints) > options.max_sprints:
            # Skip reviewer; inject a synthetic issue and revise.
            last_issues = [
                f"Plan exceeds max_sprints={options.max_sprints}; consolidate into ≤{options.max_sprints} sprints."
            ]
            rounds += 1
            if rounds > options.max_plan_review_rounds:
                raise PlanPhaseFailed("max_plan_review_rounds exceeded (sprint cap)")
            plan_version += 1
            continue

        # Reviewer.
        reviewer_options = reviewer_options_factory(job_dir=job_dir, plan_version=plan_version)
        await run_reviewer(
            user_prompt=_reviewer_user_prompt(job_dir, plan_version),
            options=reviewer_options,
            expected_review_path=review_path,
            log_path=log_path,
        )

        review = parse_review(review_path)
        if review.status.lower() == "approved" or not review.implementation_issues:
            # Lock in plan.md.
            (job_dir / "plan.md").write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
            return sprints, rounds

        # Revise.
        last_issues = review.implementation_issues
        rounds += 1
        if rounds > options.max_plan_review_rounds:
            raise PlanPhaseFailed("max_plan_review_rounds exceeded")
        plan_version += 1
```

- [ ] **Step 4: Run the loop tests.**

```bash
uv run pytest tests/test_planning.py::TestRunPlanPhase -v
```

Expected: passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/planning.py
uv run ruff format --check src/harness_mcp/planning.py
```

Expected: zero findings.

---

## Task 3: `summarizer.py`

**Files:**
- Create: `tests/test_summarizer.py`
- Create: `src/harness_mcp/summarizer.py`

Per spec §6.6: one `claude_agent_sdk.query()` call. The user prompt instructs reading `design.md`, `plan.md`, every `sprint-N/eval.md`, and writing 2–3 sentences to `summary.md`. We test the prompt-assembly logic and the file-write expectation; the SDK call itself is mocked.

- [ ] **Step 1: Write failing tests.**

Create `tests/test_summarizer.py`:

```python
"""Tests for harness_mcp.summarizer."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from harness_mcp.summarizer import build_summarizer_prompt, run_summarizer


class TestBuildSummarizerPrompt:
    def test_lists_each_sprint_eval(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "sprint-1").mkdir()
        (job_dir / "sprint-1" / "eval.md").write_text("EVAL_1")
        (job_dir / "sprint-2").mkdir()
        (job_dir / "sprint-2" / "eval.md").write_text("EVAL_2")

        prompt = build_summarizer_prompt(job_dir)
        assert "design.md" in prompt
        assert "plan.md" in prompt
        assert "sprint-1/eval.md" in prompt
        assert "sprint-2/eval.md" in prompt
        assert "summary.md" in prompt
        assert "2–3 sentences" in prompt or "2-3 sentences" in prompt

    def test_empty_when_no_sprints(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        prompt = build_summarizer_prompt(job_dir)
        assert "summary.md" in prompt


class TestRunSummarizer:
    @pytest.mark.asyncio
    async def test_writes_summary_md(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()

        # Fake query that side-effects writing summary.md. `run_summarizer` only
        # drains the async iterator and re-reads the file — it doesn't inspect
        # message types, so we can yield a sentinel rather than a typed mock.
        from harness_mcp import summarizer as sm

        async def fake_query(prompt: str, options: Any):
            (job_dir / "summary.md").write_text(
                "Built a tiny TODO app. 3 of 3 sprints passed.\n", encoding="utf-8"
            )
            yield object()

        with patch.object(sm, "query", fake_query):
            text = await run_summarizer(job_dir=job_dir, options=object())
            assert "TODO app" in text
            assert (job_dir / "summary.md").is_file()
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_summarizer.py -v
```

Expected: ImportError on `harness_mcp.summarizer`.

- [ ] **Step 3: Implement `summarizer.py`.**

Create `src/harness_mcp/summarizer.py`:

```python
"""Summarizer — one-shot Claude call producing summary.md at job end.

The user prompt is assembled here; the system prompt is the packaged
prompts/summarizer.md content (orchestrator passes it via options).

We do NOT inline `eval.md` contents — the prompt names files and the
agent reads them. (Inlining grows the prompt linearly with sprint count;
file reads scale better and the harness is happy to wait an extra turn.)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import query  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    query = None  # type: ignore[assignment]


_SPRINT_DIR_RE = re.compile(r"^sprint-(\d+)$")


def _sprint_dirs(job_dir: Path) -> list[Path]:
    """List sprint-N/ subdirs in numeric order."""
    if not job_dir.is_dir():
        return []
    sprints: list[tuple[int, Path]] = []
    for child in job_dir.iterdir():
        if not child.is_dir():
            continue
        m = _SPRINT_DIR_RE.match(child.name)
        if m:
            sprints.append((int(m.group(1)), child))
    sprints.sort()
    return [p for _, p in sprints]


def build_summarizer_prompt(job_dir: Path) -> str:
    sprint_lines = "\n".join(
        f"- {sprint.name}/eval.md" for sprint in _sprint_dirs(job_dir)
    )
    if not sprint_lines:
        sprint_lines = "(no sprints — job ended before any sprint completed)"

    return f"""Read these files and write a 2–3 sentence summary to `summary.md` in your cwd.

## Files to read
- design.md
- plan.md
{sprint_lines}

## What to cover (in 2-3 sentences total)
- What was built (one phrase).
- Sprint pass/fail tally (e.g., "3 of 4 sprints passed; sprint 4 failed two dynamic criteria").
- What's incomplete (one phrase). If everything passed, say so.

Plain prose. No code blocks, no bullets, no editorializing. Output goes to a status line.
"""


async def run_summarizer(*, job_dir: Path, options: Any) -> str:
    """Drive one query(), return the contents of summary.md after the agent finishes."""
    prompt = build_summarizer_prompt(job_dir)
    async for _msg in query(prompt=prompt, options=options):
        pass  # drain
    summary_path = job_dir / "summary.md"
    if not summary_path.is_file():
        return ""
    return summary_path.read_text(encoding="utf-8")
```

- [ ] **Step 4: Run the tests.**

```bash
uv run pytest tests/test_summarizer.py -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/summarizer.py tests/test_summarizer.py
uv run ruff format --check src/harness_mcp/summarizer.py tests/test_summarizer.py
```

Expected: zero findings.

---

## Task 4: `prereqs.py` — paths, env, Codex binary, restart sweep

**Files:**
- Create: `tests/test_prereqs.py`
- Create: `src/harness_mcp/prereqs.py`

Layer 1 of prereqs: the simple, fully synchronous checks. Path resolution + dir creation, `ANTHROPIC_API_KEY` env var, `which codex` + `--version`, restart sweep (uses `state.sweep_running_to_interrupted`).

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_prereqs.py`:

```python
"""Tests for harness_mcp.prereqs — lifespan startup checks."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager  # noqa: F401  — used by TestProbeCodexSdkShape (Task 5)
from pathlib import Path
from typing import Any  # noqa: F401  — used by TestProbeCodexSdkShape and Task 6 helpers
from typing import AsyncIterator  # noqa: F401  — used by TestProbeCodexSdkShape (Task 5)

import pytest

from harness_mcp.prereqs import (
    DoctorReport,
    PrereqFailedError,
    assert_strict_mcp_config_works,
    check_codex_binary,
    check_env,
    check_paths_and_db,
    format_doctor_report,
    probe_codex_sdk_shape,
    probe_mcp_servers,
    probe_skill,
    sweep_at_startup,
)


class TestCheckPathsAndDb:
    @pytest.mark.asyncio
    async def test_creates_harness_home_and_jobs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "h"))
        result = await check_paths_and_db()
        assert (tmp_path / "h").is_dir()
        assert (tmp_path / "h" / "jobs").is_dir()
        assert (tmp_path / "h" / "state.db").is_file()
        assert result.startswith("OK")


class TestCheckEnv:
    def test_passes_when_anthropic_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
        msg = check_env()
        assert msg.startswith("OK")

    def test_fails_when_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(PrereqFailedError):
            check_env()

    def test_fails_when_key_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        with pytest.raises(PrereqFailedError):
            check_env()


class TestCheckCodexBinary:
    def test_uses_harness_codex_bin_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Create a fake codex binary that prints a version.
        fake_bin = tmp_path / "fake_codex.sh"
        fake_bin.write_text("#!/bin/sh\necho 'codex 0.42.0'\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("HARNESS_CODEX_BIN", str(fake_bin))

        msg = check_codex_binary()
        assert "0.42.0" in msg or msg.startswith("OK")

    def test_fails_when_binary_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_CODEX_BIN", "/nonexistent/codex")
        monkeypatch.delenv("PATH", raising=False)
        with pytest.raises(PrereqFailedError):
            check_codex_binary()

    def test_fails_when_version_returns_nonzero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        bad = tmp_path / "bad.sh"
        bad.write_text("#!/bin/sh\nexit 1\n")
        bad.chmod(0o755)
        monkeypatch.setenv("HARNESS_CODEX_BIN", str(bad))
        with pytest.raises(PrereqFailedError):
            check_codex_binary()


class TestSweepAtStartup:
    @pytest.mark.asyncio
    async def test_running_jobs_flipped_to_interrupted(self, tmp_harness_home: Path) -> None:
        from harness_mcp.state import init_db, db_write, close_db

        init_db()
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J1", "running", "planning", "/tmp/x", "{}", 1, 1),
        )
        await sweep_at_startup()
        conn = sqlite3.connect(str(tmp_harness_home / "state.db"))
        try:
            row = conn.execute("SELECT status FROM jobs WHERE id='J1'").fetchone()
            assert row[0] == "interrupted"
        finally:
            conn.close()
        close_db()


class TestDoctorReport:
    def test_formats_passes_and_fails(self) -> None:
        report = DoctorReport()
        report.add("paths", "OK", "~/.harness exists; state.db at ~/.harness/state.db")
        report.add("env", "FAIL", "ANTHROPIC_API_KEY missing")
        out = format_doctor_report(report)
        assert "OK   paths" in out
        assert "FAIL env" in out
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_prereqs.py -v
```

Expected: ImportError on `harness_mcp.prereqs`.

- [ ] **Step 3: Implement `prereqs.py` — Layer 1 (sync checks + report).**

All imports are placed at the top of the file *now*, anticipating later tasks (probe, skill, MCP). This avoids ruff `E402` (module-level imports not at top) when later tasks append more functions.

Create `src/harness_mcp/prereqs.py`:

```python
"""Lifespan startup checks (`harness-mcp serve` and `harness-mcp doctor`).

Each check returns a one-line status string on pass or raises
`PrereqFailedError(message)` on fail. The orchestrator (Plan 5) wires
them into the FastMCP lifespan; the `doctor` subcommand runs the same
checks but pretty-prints them and exits non-zero on first failure.

Async vs. sync split:
  * Synchronous: env var, Codex binary `--version`, doctor report.
  * Async (uses anyio): paths + DB init, restart sweep, Codex SDK shape
    probe, skill probe, MCP probe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile  # noqa: F401  — used by probe_codex_sdk_shape (Task 5)
from collections.abc import Callable  # noqa: F401  — used by probe_skill / probe_mcp_servers (Task 6)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any  # noqa: F401  — used in Task 6 type hints

from harness_mcp.config import harness_home, jobs_root, state_db_path
from harness_mcp.mcp_capture import capture_from_mcp_status, parse_user_config_files  # noqa: F401  — used by probe_mcp_servers (Task 6)
from harness_mcp.state import init_db, sweep_running_to_interrupted

# Codex SDK is indirected so unit tests can monkeypatch each piece independently.
# Used in Task 5's probe; importing here keeps all imports at the top to avoid E402.
try:
    from codex_app_server import AppServerConfig as _AppServerConfig  # type: ignore[import-untyped]  # noqa: F401  — used by probe_codex_sdk_shape (Task 5)
    from codex_app_server import AsyncCodex as _AsyncCodex  # type: ignore[import-untyped]  # noqa: F401
    from codex_app_server import TextInput as _TextInput  # type: ignore[import-untyped]  # noqa: F401
except ImportError:  # pragma: no cover  — only hit when SDK isn't installed
    _AppServerConfig = None  # type: ignore[assignment]
    _AsyncCodex = None  # type: ignore[assignment]
    _TextInput = None  # type: ignore[assignment]


class PrereqFailedError(RuntimeError):
    """Raised by any prereq check on failure."""


@dataclass
class DoctorReport:
    """Accumulator for `harness-mcp doctor` output."""

    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.rows.append((name, status, detail))


def format_doctor_report(report: DoctorReport) -> str:
    lines = []
    for name, status, detail in report.rows:
        marker = "OK  " if status == "OK" else "FAIL"
        lines.append(f"{marker} {name}: {detail}")
    return "\n".join(lines)


# ---------- Layer 1: synchronous checks ----------


async def check_paths_and_db() -> str:
    """Resolve ~/.harness, mkdir -p jobs/, init the state DB."""
    home = harness_home()
    home.mkdir(parents=True, exist_ok=True)
    jobs_root().mkdir(exist_ok=True)
    init_db()  # idempotent
    return f"OK paths: home={home}; state_db={state_db_path()}"


def check_env() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise PrereqFailedError("ANTHROPIC_API_KEY not set or empty")
    return "OK env: ANTHROPIC_API_KEY is set"


def check_codex_binary() -> str:
    """Resolve $HARNESS_CODEX_BIN or `which codex`, run --version."""
    bin_path = os.environ.get("HARNESS_CODEX_BIN") or shutil.which("codex")
    if not bin_path:
        raise PrereqFailedError(
            "Codex binary not found: set HARNESS_CODEX_BIN or add codex to PATH"
        )
    if not Path(bin_path).is_file() and shutil.which(bin_path) is None:
        raise PrereqFailedError(f"Codex binary {bin_path!r} does not exist")
    try:
        proc = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise PrereqFailedError("Codex binary --version timed out") from e
    if proc.returncode != 0:
        raise PrereqFailedError(
            f"Codex binary --version exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return f"OK codex: {proc.stdout.strip() or '(no version output)'}"


async def sweep_at_startup() -> str:
    """Mark any leftover `running` jobs as `interrupted`."""
    await sweep_running_to_interrupted()
    return "OK sweep: prior `running` jobs flipped to `interrupted`"
```

- [ ] **Step 4: Run the layer-1 tests.**

```bash
uv run pytest tests/test_prereqs.py -v
```

Expected: every test in `TestCheckPathsAndDb`, `TestCheckEnv`, `TestCheckCodexBinary`, `TestSweepAtStartup`, `TestDoctorReport` passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/prereqs.py tests/test_prereqs.py
uv run ruff format --check src/harness_mcp/prereqs.py tests/test_prereqs.py
```

Expected: zero findings.

---

## Task 5: `prereqs.py` — Codex SDK shape probe

**Files:**
- Modify: `tests/test_prereqs.py`
- Modify: `src/harness_mcp/prereqs.py`

Per spec §10.1 step 2b: probe `AppServerConfig` + `AsyncCodex.thread_start()` against a tmp git repo, trying the four override forms (TOML field name vs. alias key, hyphenated vs. camelCase value), keep the first form that successfully writes a file. The accepted form goes into module-level `_CODEX_CONFIG_OVERRIDES` for runtime use.

- [ ] **Step 1: Append failing tests.**

All imports needed by these tests (`asynccontextmanager`, `AsyncIterator`, `probe_codex_sdk_shape`) were hoisted into Task 4 Step 1's initial import block, so this step appends only test classes.

Append to `tests/test_prereqs.py`:

```python
class TestProbeCodexSdkShape:
    @pytest.mark.asyncio
    async def test_finds_first_working_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_CODEX_BIN", "/usr/bin/echo")  # value irrelevant for the mock
        # Mock AppServerConfig and AsyncCodex.
        attempts: list[tuple[str, ...]] = []

        @asynccontextmanager
        async def fake_codex(config: Any) -> AsyncIterator[Any]:
            attempts.append(config["overrides"])

            class _Thread:
                async def turn(self, _input: object) -> object:
                    # Side-effect: write probe.txt only if the override form is the "good" one.
                    if config["overrides"] == ("sandbox_mode=workspace-write", "approval_policy=never"):
                        (Path(config["cwd"]) / "probe.txt").write_text("ok")
                    class _T:
                        async def stream(self) -> AsyncIterator[Any]:
                            if False:
                                yield None
                    return _T()
            class _Wrap:
                async def thread_start(self): return _Thread()
            yield _Wrap()

        # Each entry is (config_overrides, fake_class). Good form is index 0.
        from harness_mcp import prereqs as p

        def fake_app_server_config(*, codex_bin: str, cwd: str, config_overrides: tuple[str, ...], **kw: Any) -> dict[str, Any]:
            return {"codex_bin": codex_bin, "cwd": cwd, "overrides": config_overrides, **kw}

        class FakeTextInput:
            def __init__(self, prompt: str): self.prompt = prompt

        monkeypatch.setattr(p, "_AppServerConfig", fake_app_server_config)
        monkeypatch.setattr(p, "_AsyncCodex", fake_codex)
        monkeypatch.setattr(p, "_TextInput", FakeTextInput)

        msg, accepted = await probe_codex_sdk_shape()
        # First form is the good one; only one attempt should be needed.
        assert accepted == ("sandbox_mode=workspace-write", "approval_policy=never")
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_falls_back_to_camelcase_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Configure the fake so only the alias-camelcase form succeeds.
        from harness_mcp import prereqs as p
        monkeypatch.setenv("HARNESS_CODEX_BIN", "/usr/bin/echo")

        def fake_app_server_config(*, codex_bin: str, cwd: str, config_overrides: tuple[str, ...], **kw: Any) -> dict[str, Any]:
            return {"cwd": cwd, "overrides": config_overrides}

        @asynccontextmanager
        async def fake_codex(config: Any) -> AsyncIterator[Any]:
            class _T:
                async def turn(self, _input: object) -> object:
                    if config["overrides"] == ("sandbox=workspaceWrite", "approval_policy=never"):
                        (Path(config["cwd"]) / "probe.txt").write_text("ok")
                    class _S:
                        async def stream(self) -> AsyncIterator[Any]:
                            if False:
                                yield None
                    return _S()
            class _W:
                async def thread_start(self): return _T()
            yield _W()

        class _TI:
            def __init__(self, x: str): pass

        monkeypatch.setattr(p, "_AppServerConfig", fake_app_server_config)
        monkeypatch.setattr(p, "_AsyncCodex", fake_codex)
        monkeypatch.setattr(p, "_TextInput", _TI)

        msg, accepted = await probe_codex_sdk_shape()
        assert accepted == ("sandbox=workspaceWrite", "approval_policy=never")

    @pytest.mark.asyncio
    async def test_fails_when_no_form_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness_mcp import prereqs as p
        monkeypatch.setenv("HARNESS_CODEX_BIN", "/usr/bin/echo")

        @asynccontextmanager
        async def fake_codex(config: Any) -> AsyncIterator[Any]:
            class _T:
                async def turn(self, _x: object) -> object:
                    # Never writes probe.txt — silent ignore.
                    class _S:
                        async def stream(self) -> AsyncIterator[Any]:
                            if False:
                                yield None
                    return _S()
            class _W:
                async def thread_start(self): return _T()
            yield _W()

        monkeypatch.setattr(p, "_AppServerConfig", lambda **kw: {"cwd": kw["cwd"], "overrides": kw["config_overrides"]})
        monkeypatch.setattr(p, "_AsyncCodex", fake_codex)
        class _TI:
            def __init__(self, x: str): pass
        monkeypatch.setattr(p, "_TextInput", _TI)

        with pytest.raises(PrereqFailedError):
            await probe_codex_sdk_shape()
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_prereqs.py::TestProbeCodexSdkShape -v
```

Expected: ImportError on `probe_codex_sdk_shape`.

- [ ] **Step 3: Append the probe to `prereqs.py`.**

All imports are already at the top of `prereqs.py` (Task 4 Step 3 includes `tempfile`, `shutil`, `subprocess`, plus the lazy `_AsyncCodex` / `_AppServerConfig` / `_TextInput` aliases). This step appends only the new constant + function, no new imports.

Append to `src/harness_mcp/prereqs.py`:

```python
# ---------- Codex SDK shape probe ----------


_OVERRIDE_FORMS: tuple[tuple[str, ...], ...] = (
    ("sandbox_mode=workspace-write", "approval_policy=never"),  # TOML field name, hyphenated value
    ("sandbox_mode=workspaceWrite", "approval_policy=never"),   # TOML field name, camelCase value
    ("sandbox=workspace-write", "approval_policy=never"),       # alias key, hyphenated value
    ("sandbox=workspaceWrite", "approval_policy=never"),        # alias key, camelCase value
)


async def probe_codex_sdk_shape() -> tuple[str, tuple[str, ...]]:
    """Verify the Codex SDK install + sandbox override semantics.

    Tries each override form in `_OVERRIDE_FORMS`. For each, opens
    `AsyncCodex(config=cfg)`, calls `thread.turn(TextInput("write probe.txt..."))`,
    drains the stream, then checks whether `probe.txt` actually got written.
    Returns the first form that works. If none does, raises PrereqFailedError.
    """
    bin_path = os.environ.get("HARNESS_CODEX_BIN") or shutil.which("codex")
    if not bin_path:
        raise PrereqFailedError("HARNESS_CODEX_BIN / `codex` on PATH is required for the probe")

    last_error: str = ""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # The Python SDK requires a real git repo (no `skip_git_repo_check` flag).
        try:
            subprocess.run(["git", "init", "-q"], cwd=str(tmp_dir), check=True)
            subprocess.run(["git", "config", "user.email", "probe@harness"], cwd=str(tmp_dir), check=True)
            subprocess.run(["git", "config", "user.name", "Probe"], cwd=str(tmp_dir), check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise PrereqFailedError(f"git init failed during probe: {e}") from e

        for form in _OVERRIDE_FORMS:
            (tmp_dir / "probe.txt").unlink(missing_ok=True)
            cfg = _AppServerConfig(
                codex_bin=bin_path,
                cwd=str(tmp_dir),
                config_overrides=form,
                client_name="harness-mcp",
                client_title="Harness Probe",
                client_version="0.1.0",
            )
            try:
                async with _AsyncCodex(config=cfg) as codex:
                    thread = await codex.thread_start()
                    turn = await thread.turn(
                        _TextInput("write a file called probe.txt containing the word ok and exit")
                    )
                    async for _event in turn.stream():
                        pass
            except Exception as e:  # noqa: BLE001 — collect for the final error message
                last_error = f"override {form!r} raised {e!r}"
                continue

            if (tmp_dir / "probe.txt").is_file():
                return (f"OK codex-shape: accepted overrides {form}", form)
            last_error = f"override {form!r} accepted but probe.txt not written"

    raise PrereqFailedError(
        f"Codex sandbox override accepted but no form actually permitted writes. Last attempt: {last_error}"
    )
```

- [ ] **Step 4: Run the probe tests.**

```bash
uv run pytest tests/test_prereqs.py::TestProbeCodexSdkShape -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/prereqs.py
uv run ruff format --check src/harness_mcp/prereqs.py
```

Expected: zero findings.

---

## Task 6: `prereqs.py` — skill probe + MCP probe + assertion

**Files:**
- Modify: `tests/test_prereqs.py`
- Modify: `src/harness_mcp/prereqs.py`

Per spec §10.1 steps 4 and 5:
- Skill probe: boot a transient `ClaudeSDKClient` with `setting_sources=["user"]`, call `get_server_info()`, look for `superpowers:writing-plans` in `commands`. Fall back to a prose probe if needed.
- MCP probe: same client, call `get_mcp_status()`, capture `context7` (hard) and `playwright` (soft). Fall back to file parsing per `mcp_capture.py`.
- MCP merge-semantics assertion: re-boot a client with `mcp_servers={"context7": <captured>}` + `extra_args={"strict-mcp-config": None}`, assert exactly one server in the response.

- [ ] **Step 1: Append failing tests.**

All imports (`assert_strict_mcp_config_works`, `probe_mcp_servers`, `probe_skill`) were hoisted into Task 4 Step 1's initial import block. This step appends only the `_FakeClient` helper and test classes.

Append to `tests/test_prereqs.py`:

```python
class _FakeClient:
    def __init__(self, *, server_info: dict[str, Any], mcp_status: dict[str, Any]) -> None:
        self._server_info = server_info
        self._mcp_status = mcp_status

    async def __aenter__(self) -> "_FakeClient": return self
    async def __aexit__(self, *exc: object) -> None: return None

    async def query(self, _prompt: str) -> None: return None

    async def receive_response(self):
        if False:
            yield None

    async def get_server_info(self) -> dict[str, Any]: return self._server_info
    async def get_mcp_status(self) -> dict[str, Any]: return self._mcp_status


class TestProbeSkill:
    @pytest.mark.asyncio
    async def test_finds_writing_plans_in_commands(self) -> None:
        client = _FakeClient(
            server_info={"commands": ["superpowers:writing-plans", "code-review:code-review"]},
            mcp_status={},
        )
        msg, sources = await probe_skill(client_factory=lambda **_kw: client)
        assert sources == ["user"]
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_fails_when_skill_absent(self) -> None:
        client = _FakeClient(server_info={"commands": []}, mcp_status={})
        with pytest.raises(PrereqFailedError):
            await probe_skill(client_factory=lambda **_kw: client)


class TestProbeMcpServers:
    @pytest.mark.asyncio
    async def test_captures_context7_via_inline_config(self) -> None:
        client = _FakeClient(
            server_info={},
            mcp_status={"mcpServers": [
                {"name": "context7", "status": "connected", "config": {"command": "ctx7"}},
            ]},
        )
        msg, captured = await probe_mcp_servers(client_factory=lambda **_kw: client, project_root=None)
        assert "context7" in captured
        assert captured["context7"] == {"command": "ctx7"}
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_fails_when_context7_disconnected_no_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        client = _FakeClient(
            server_info={},
            mcp_status={"mcpServers": [
                {"name": "context7", "status": "disconnected"},
            ]},
        )
        with pytest.raises(PrereqFailedError):
            await probe_mcp_servers(client_factory=lambda **_kw: client, project_root=tmp_path)

    @pytest.mark.asyncio
    async def test_playwright_soft_warns_when_missing(self) -> None:
        client = _FakeClient(
            server_info={},
            mcp_status={"mcpServers": [
                {"name": "context7", "status": "connected", "config": {"command": "ctx7"}},
            ]},
        )
        msg, captured = await probe_mcp_servers(client_factory=lambda **_kw: client, project_root=None)
        assert "playwright" not in captured
        # No exception raised — playwright is soft.


class TestAssertStrictMcpConfigWorks:
    @pytest.mark.asyncio
    async def test_passes_when_strict_mcp_config_returns_one_server(self) -> None:
        client = _FakeClient(
            server_info={},
            mcp_status={"mcpServers": [{"name": "context7", "status": "connected"}]},
        )
        msg = await assert_strict_mcp_config_works(
            client_factory=lambda **_kw: client,
            captured={"context7": {"command": "ctx7"}},
            setting_sources=["user"],
        )
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_fails_when_extra_servers_leak(self) -> None:
        client = _FakeClient(
            server_info={},
            mcp_status={"mcpServers": [
                {"name": "context7", "status": "connected"},
                {"name": "playwright", "status": "connected"},
            ]},
        )
        with pytest.raises(PrereqFailedError):
            await assert_strict_mcp_config_works(
                client_factory=lambda **_kw: client,
                captured={"context7": {"command": "ctx7"}},
                setting_sources=["user"],
            )
```

- [ ] **Step 2: Confirm failure.**

```bash
uv run pytest tests/test_prereqs.py::TestProbeSkill tests/test_prereqs.py::TestProbeMcpServers tests/test_prereqs.py::TestAssertStrictMcpConfigWorks -v
```

Expected: ImportError on the new probe symbols.

- [ ] **Step 3: Append to `prereqs.py`.**

All imports are already at the top of `prereqs.py` (Task 4 Step 3 includes `Callable` + `mcp_capture` helpers). This step appends only the new functions, no new imports.

Append to `src/harness_mcp/prereqs.py`:

```python
# ---------- Skill + MCP probes ----------


async def probe_skill(
    *,
    client_factory: Callable[..., Any],
    skill_name: str = "superpowers:writing-plans",
) -> tuple[str, list[str]]:
    """Verify `superpowers:writing-plans` is installed at user scope.

    Returns (status_message, resolved_setting_sources). resolved_setting_sources
    is recorded as `_resolved_setting_sources` for spawn calls (spec §10.1).
    """
    sources = ["user"]
    client = client_factory(setting_sources=sources)
    async with client as c:
        # Some SDK versions need a no-op query before get_server_info().
        await c.query("ready?")
        async for _ in c.receive_response():
            break  # drain one message; some clients hang otherwise
        info = await c.get_server_info()
    commands = info.get("commands") or []
    if any(skill_name in str(cmd) for cmd in commands):
        return f"OK skill: {skill_name} found at setting_sources={sources}", sources
    raise PrereqFailedError(
        f"skill {skill_name} not available at setting_sources={sources}; "
        "install superpowers plugin at user scope: https://github.com/anthropics/claude-superpowers"
    )


async def probe_mcp_servers(
    *,
    client_factory: Callable[..., Any],
    project_root: Path | None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Capture context7 (hard) + playwright (soft) MCP server stanzas.

    Strategy:
      1. Open ClaudeSDKClient → get_mcp_status() — read inline `config` if present.
      2. For names without inline config but `connected`, fall back to
         parsing user config files via mcp_capture.parse_user_config_files.
      3. context7 missing → PrereqFailedError. playwright missing → warning.
    """
    client = client_factory()
    async with client as c:
        await c.query("ready?")
        async for _ in c.receive_response():
            break
        status = await c.get_mcp_status()

    want = ("context7", "playwright")
    captured = capture_from_mcp_status(status, want=want)

    missing_with_inline = [
        e["name"]
        for e in status.get("mcpServers", [])
        if e.get("name") in want
        and e.get("status") == "connected"
        and e.get("name") not in captured
    ]
    if missing_with_inline:
        captured.update(parse_user_config_files(tuple(missing_with_inline), project_root=project_root))

    if "context7" not in captured:
        raise PrereqFailedError(
            "context7 MCP server not connected or not configured. "
            "Add a context7 stanza to ~/.claude.json mcpServers."
        )

    msg = f"OK mcp: captured {sorted(captured.keys())}"
    if "playwright" not in captured:
        msg += " (warning: playwright absent — UI sprints will fail if they reach dynamic verification)"
    return msg, captured


async def assert_strict_mcp_config_works(
    *,
    client_factory: Callable[..., Any],
    captured: dict[str, dict[str, Any]],
    setting_sources: list[str],
) -> str:
    """Verify `extra_args={"strict-mcp-config": None}` actually overrides settings inheritance.

    Boots a probe client with strict-mcp-config + only context7 captured,
    then calls get_mcp_status(). Expect exactly one server. If extra
    servers leak through (e.g., user has more servers in settings), the
    flag isn't enforcing — refuse startup.
    """
    client = client_factory(
        setting_sources=setting_sources,
        mcp_servers={"context7": captured["context7"]},
        extra_args={"strict-mcp-config": None},
    )
    async with client as c:
        await c.query("ready?")
        async for _ in c.receive_response():
            break
        status = await c.get_mcp_status()
    names = {e.get("name") for e in status.get("mcpServers", []) if e.get("name")}
    if names != {"context7"}:
        raise PrereqFailedError(
            "strict-mcp-config flag did not enforce override; SDK behavior unexpected. "
            f"Expected just {{'context7'}}, got {names}. Update the dep or report a bug."
        )
    return "OK strict-mcp-config: enforced"


# ---------- run_prereqs: the complete §10.1 sequence ----------


@dataclass(frozen=True)
class PrereqsResult:
    captured_mcp: dict[str, dict[str, Any]]
    setting_sources: list[str]
    codex_overrides: tuple[str, ...]


async def run_prereqs(
    *,
    client_factory: Callable[..., Any],
    project_root: Path | None,
    report: DoctorReport | None = None,
) -> PrereqsResult:
    """Run the complete startup sequence per spec §10.1.

    Each step's status is added to `report` if provided (used by `harness-mcp doctor`).
    On any failure, raises PrereqFailedError immediately.
    """
    if report is None:
        report = DoctorReport()

    msg = await check_paths_and_db()
    report.add("paths_and_db", "OK", msg)

    msg = check_env()
    report.add("env", "OK", msg)

    msg = check_codex_binary()
    report.add("codex_binary", "OK", msg)

    codex_msg, codex_overrides = await probe_codex_sdk_shape()
    report.add("codex_sdk_shape", "OK", codex_msg)

    skill_msg, sources = await probe_skill(client_factory=client_factory)
    report.add("skill", "OK", skill_msg)

    mcp_msg, captured = await probe_mcp_servers(
        client_factory=client_factory, project_root=project_root
    )
    report.add("mcp", "OK", mcp_msg)

    strict_msg = await assert_strict_mcp_config_works(
        client_factory=client_factory,
        captured=captured,
        setting_sources=sources,
    )
    report.add("strict_mcp_config", "OK", strict_msg)

    msg = await sweep_at_startup()
    report.add("restart_sweep", "OK", msg)

    return PrereqsResult(
        captured_mcp=captured,
        setting_sources=sources,
        codex_overrides=codex_overrides,
    )
```

- [ ] **Step 4: Run all the prereq tests.**

```bash
uv run pytest tests/test_prereqs.py -v
```

Expected: every test passes.

- [ ] **Step 5: Lint.**

```bash
uv run ruff check src/harness_mcp/prereqs.py tests/test_prereqs.py
uv run ruff format --check src/harness_mcp/prereqs.py tests/test_prereqs.py
```

Expected: zero findings.

---

## Task 7: Final sweep

- [ ] **Step 1: Full pytest.**

```bash
uv run pytest tests/ -v
```

Expected: every test in Parts 1+2+3+4 passes.

- [ ] **Step 2: Full ruff.**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: zero findings.

- [ ] **Step 3: Confirm `main` branch, no commits.**

```bash
git status --short && git rev-parse --abbrev-ref HEAD
```

Expected: untracked / modified files only, branch `main`.

---

## Done criteria

- All 7 tasks complete.
- `uv run pytest tests/ -v` passes (Parts 1–4).
- `uv run ruff check .` and `uv run ruff format --check .` exit 0.
- Repo on `main`, NO commits.

The next plan in the series (Part 5: Orchestration & Server) ties everything together: per-job orchestrator coroutine, sprint loop, FastMCP tool definitions, the `harness-mcp` CLI (`serve` + `doctor`), and the README + smoke test.
