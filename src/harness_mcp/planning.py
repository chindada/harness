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

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_mcp.config import JobOptions
from harness_mcp.types import (
    HarnessToolError,
)

logger = logging.getLogger(__name__)

# The SDK is imported lazily so unit tests can swap `query` via monkeypatch
# without making it a hard import (and to keep the module fast to import).
try:
    from claude_agent_sdk import (
        query,  # type: ignore[import-untyped]
    )
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
            had_impl_tag = bool(_IMPL_TAG_RE.match(bullet))
            stripped = _IMPL_TAG_RE.sub("", bullet)  # strip [implementation] prefix
            if not had_impl_tag:
                # Spec §5.2: untagged issues default to [implementation]; warn the
                # operator so a Reviewer that omits tags can be caught and corrected.
                logger.warning(
                    "review %s: untagged issue defaulted to [implementation]: %s",
                    review_path.name,
                    stripped[:120],
                )
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


# Layer 2: SDK drivers + the §5 loop


class PlanPhaseFailed(HarnessToolError):
    """Raised when the plan phase exhausts review rounds or hits a structural error.

    Carries the spec-defined `current_phase` (one of `planning`, `plan-review`,
    `plan-revision`) and an `error_text` string that the orchestrator writes
    verbatim to the jobs row so `poll_build` reports the phase that actually
    failed, not the one in effect when the exception was caught.
    """

    def __init__(self, error_text: str, *, phase: str = "planning") -> None:
        super().__init__(error_text)
        self.phase = phase
        self.error_text = error_text


# `*_options_factory` is a callable that returns a ClaudeAgentOptions instance per spawn.
# The orchestrator (Plan 5) constructs these — this module is decoupled from the
# captured-MCP / setting-sources state owned by `prereqs.py`.
PlannerOptionsFactory = Callable[..., Any]
ReviewerOptionsFactory = Callable[..., Any]


async def _drive_query(
    user_prompt: str,
    options: Any,  # noqa: ANN401  — opaque SDK ClaudeAgentOptions; tests pass `object()`
    log_path: Path | None = None,
) -> list[Any]:
    """Drive a single one-shot query() call to completion, returning the message list."""
    _ = log_path  # reserved for streaming TextBlock content; not yet implemented
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
    options: Any,  # noqa: ANN401  — opaque SDK ClaudeAgentOptions
    expected_plan_path: Path,
    require_skill: bool,
    plan_version: int,
    log_path: Path | None = None,
) -> None:
    """One round of Planner. Verifies skill invocation AND that the written
    plan exists and contains at least one `## Sprint N:` marker.

    Per spec §5.1 step 7 / §5.2 step 0: on structural failure (missing file
    OR no Sprint markers) we re-prompt the Planner once with the explicit
    failure description; second consecutive failure raises PlanPhaseFailed
    with `phase='planning'` (v1, §5.1:320) or `phase='plan-revision'` (v≥2,
    §5.2:327) so the orchestrator reports the spec'd terminal phase.
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
        phase = "planning" if plan_version == 1 else "plan-revision"
        raise PlanPhaseFailed(
            "planner_emitted_unstructured_plan_after_retry",
            phase=phase,
        )


async def run_reviewer(
    *,
    user_prompt: str,
    options: Any,  # noqa: ANN401  — opaque SDK ClaudeAgentOptions
    expected_review_path: Path,
    log_path: Path | None = None,
) -> None:
    msgs = await _drive_query(user_prompt, options, log_path)
    _ = msgs  # drained for side effects
    if not expected_review_path.is_file():  # noqa: ASYNC240  — sync stat is acceptable here
        raise PlanPhaseFailed(
            f"reviewer_did_not_write_file: {expected_review_path}",
            phase="plan-review",
        )


def _planner_user_prompt(job_dir: Path, plan_version: int, issues: list[str] | None) -> str:
    if issues:
        issues_section = "\n## Issues to address\n" + "\n".join(f"- {i}" for i in issues)
    else:
        issues_section = ""
    return (
        f"Read `design.md` and write a plan to `plan-history/plan-v{plan_version}.md` "
        f"using `## Sprint N: <Title>` H2 markers.\n\n"
        f"Working dir: {job_dir}\n"
        f"{issues_section}\n"
    )


def _reviewer_user_prompt(job_dir: Path, plan_version: int) -> str:
    return (
        f"Read `design.md` and `plan-history/plan-v{plan_version}.md`. "
        f"Write your review to `plan-history/review-v{plan_version}.md` "
        f"per the rubric in your system prompt.\n\n"
        f"Working dir: {job_dir}\n"
    )


async def run_plan_phase(
    *,
    job_dir: Path,
    options: JobOptions,
    planner_options_factory: PlannerOptionsFactory,
    reviewer_options_factory: ReviewerOptionsFactory,
    log_path: Path | None = None,
    phase_setter: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[list[tuple[int, str]], int]:
    """Run the full §5 plan + review loop.

    Returns (sprints, rounds_taken). `rounds_taken` is the number of
    review-driven revisions, not counting the initial Planner call.
    Writes the final approved plan to `<job_dir>/plan.md`.

    `phase_setter` (if provided) is awaited at every spec §4.4 plan-phase
    transition (`planning` → `plan-review` → `plan-revision` → …) so
    `poll_build` mirrors the live state. Defaults to a no-op for unit
    tests that don't care about phase observability.
    """
    plan_version = 1
    rounds = 0
    last_issues: list[str] = []

    async def _set_phase(phase: str) -> None:
        if phase_setter is not None:
            await phase_setter(phase)

    while True:
        plan_path = job_dir / "plan-history" / f"plan-v{plan_version}.md"
        review_path = job_dir / "plan-history" / f"review-v{plan_version}.md"

        # Phase update: revisions enter `plan-revision` per §4.4 ("set during
        # a revision round; flips back to plan-review after"). v1 stays in
        # the orchestrator-owned `planning` phase.
        if plan_version > 1:
            await _set_phase("plan-revision")

        # Planner.
        planner_options = planner_options_factory(job_dir=job_dir, plan_version=plan_version)
        forwarded_issues = last_issues if rounds > 0 else None
        await run_planner(
            user_prompt=_planner_user_prompt(job_dir, plan_version, forwarded_issues),
            options=planner_options,
            expected_plan_path=plan_path,
            require_skill=(plan_version == 1),  # only enforce on v1
            plan_version=plan_version,
            log_path=log_path,
        )

        # Pre-review structural check. `run_planner` already retried once on
        # missing file / missing Sprint markers; if we still see zero sprints
        # here, that's the second consecutive structural failure → fail hard.
        sprints = extract_sprints(plan_path)
        if not sprints:
            phase = "planning" if plan_version == 1 else "plan-revision"
            raise PlanPhaseFailed(
                "planner_emitted_unstructured_plan_after_retry",
                phase=phase,
            )
        if len(sprints) > options.max_sprints:
            # Skip reviewer; inject a synthetic issue and revise.
            last_issues = [
                f"Plan exceeds max_sprints={options.max_sprints}; "
                f"consolidate into <={options.max_sprints} sprints."
            ]
            rounds += 1
            if rounds > options.max_plan_review_rounds:
                # Spec §5.2:343 — cap exhaustion → phase=`plan-review` with
                # error_text listing the unresolved [implementation] issues.
                issues_text = "; ".join(last_issues)
                raise PlanPhaseFailed(
                    f"max_plan_review_rounds_exceeded: {issues_text}",
                    phase="plan-review",
                )
            plan_version += 1
            continue

        # Phase: review.
        await _set_phase("plan-review")

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
            plan_text = plan_path.read_text(encoding="utf-8")
            (job_dir / "plan.md").write_text(plan_text, encoding="utf-8")
            return sprints, rounds

        # Revise.
        last_issues = review.implementation_issues
        rounds += 1
        if rounds > options.max_plan_review_rounds:
            # Spec §5.2:343 — list unresolved [implementation] issues.
            issues_text = "; ".join(last_issues)
            raise PlanPhaseFailed(
                f"max_plan_review_rounds_exceeded: {issues_text}",
                phase="plan-review",
            )
        plan_version += 1
