"""Tests for harness_mcp.planning — parsers, review-loop, plan-phase driver."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import patch

import pytest

from harness_mcp import planning as _planning_module
from harness_mcp.config import JobOptions
from harness_mcp.planning import (
    PlanPhaseFailed,
    extract_sprints,
    parse_review,
    run_plan_phase,
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
    """  # noqa: E501  — fixture text intentionally mirrors a real review
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
            "## Plan Review\n\n**Status:** Issues Found\n\n**Issues (if any):**\n" + bullets + "\n"
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


@contextmanager
def patched_query(scripts: list[list[Any]]) -> Iterator[None]:
    """Patch `claude_agent_sdk.query` to yield scripted message lists in turn.

    Each item in `scripts` is a `(side_effect_callable, msgs_list)` tuple. On
    each query() call we run the side effect (e.g., writing the plan or
    review file the real Planner/Reviewer would have written) then yield
    the canned messages.
    """
    call_count = [0]

    async def fake_query(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        _ = (args, kwargs)
        i = call_count[0]
        call_count[0] += 1
        # The scripts list also encodes side-effect functions (write plan/review).
        # Each script entry is (side_effect_callable, msgs_list).
        side_effect, msgs = scripts[i]

        async def _gen() -> AsyncIterator[Any]:
            side_effect()
            for m in msgs:
                yield m

        # Return the async generator instance directly (query() in real life
        # is also an async iterator factory).
        async for x in _gen():
            yield x

    with patch.object(_planning_module, "query", fake_query):
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

        def write_plan_v1() -> None:
            (job_dir / "plan-history" / "plan-v1.md").write_text(SAMPLE_PLAN)

        def write_review_v1() -> None:
            (job_dir / "plan-history" / "review-v1.md").write_text(SAMPLE_REVIEW_APPROVED)

        scripts = [
            (write_plan_v1, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_v1, [_make_result_msg()]),
        ]

        with patched_query(scripts):
            sprints, rounds = await run_plan_phase(
                job_dir=job_dir,
                options=JobOptions(),
                planner_options_factory=lambda **_kw: object(),  # opaque; mock doesn't read
                reviewer_options_factory=lambda **_kw: object(),
            )
            assert rounds == 0  # 0 review-driven revisions
            assert (job_dir / "plan.md").is_file()
            assert len(sprints) == 3

    @pytest.mark.asyncio
    async def test_revises_then_approves(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "design.md").write_text("DESIGN")
        (job_dir / "plan-history").mkdir()

        def write_v1() -> None:
            (job_dir / "plan-history" / "plan-v1.md").write_text(SAMPLE_PLAN)

        def write_review_with_issues() -> None:
            (job_dir / "plan-history" / "review-v1.md").write_text(SAMPLE_REVIEW_ISSUES_FOUND)

        def write_v2() -> None:
            (job_dir / "plan-history" / "plan-v2.md").write_text(SAMPLE_PLAN)

        def write_review_approved() -> None:
            (job_dir / "plan-history" / "review-v2.md").write_text(SAMPLE_REVIEW_APPROVED)

        scripts = [
            (write_v1, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_with_issues, [_make_result_msg()]),
            (write_v2, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_approved, [_make_result_msg()]),
        ]

        with patched_query(scripts):
            _sprints, rounds = await run_plan_phase(
                job_dir=job_dir,
                options=JobOptions(),
                planner_options_factory=lambda **_kw: object(),
                reviewer_options_factory=lambda **_kw: object(),
            )
            assert rounds == 1
            assert (job_dir / "plan.md").is_file()

    @pytest.mark.asyncio
    async def test_phase_setter_called_at_each_transition(self, tmp_path: Path) -> None:
        """Spec §4.4: phase_setter receives every plan-phase transition so
        poll_build reflects the live state. Sequence on one revision:
        plan-review (after v1) → plan-revision (entering v2) → plan-review (v2)."""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "design.md").write_text("DESIGN")
        (job_dir / "plan-history").mkdir()

        phase_log: list[str] = []

        async def capture(phase: str) -> None:
            phase_log.append(phase)

        def write_v1() -> None:
            (job_dir / "plan-history" / "plan-v1.md").write_text(SAMPLE_PLAN)

        def write_review_issues() -> None:
            (job_dir / "plan-history" / "review-v1.md").write_text(SAMPLE_REVIEW_ISSUES_FOUND)

        def write_v2() -> None:
            (job_dir / "plan-history" / "plan-v2.md").write_text(SAMPLE_PLAN)

        def write_review_approved() -> None:
            (job_dir / "plan-history" / "review-v2.md").write_text(SAMPLE_REVIEW_APPROVED)

        scripts = [
            (write_v1, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_issues, [_make_result_msg()]),
            (write_v2, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_approved, [_make_result_msg()]),
        ]

        with patched_query(scripts):
            await run_plan_phase(
                job_dir=job_dir,
                options=JobOptions(),
                planner_options_factory=lambda **_kw: object(),
                reviewer_options_factory=lambda **_kw: object(),
                phase_setter=capture,
            )
        assert phase_log == ["plan-review", "plan-revision", "plan-review"]

    @pytest.mark.asyncio
    async def test_cap_exhaustion_phase_and_issues_in_error_text(
        self, tmp_path: Path
    ) -> None:
        """Spec §5.2:343 — at cap, raise PlanPhaseFailed with phase=plan-review
        and error_text listing the unresolved [implementation] issues (so the
        operator sees what blocked approval, not a bare 'cap exceeded')."""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "design.md").write_text("DESIGN")
        (job_dir / "plan-history").mkdir()

        def write_v1() -> None:
            (job_dir / "plan-history" / "plan-v1.md").write_text(SAMPLE_PLAN)

        def write_review_issues_v1() -> None:
            (job_dir / "plan-history" / "review-v1.md").write_text(SAMPLE_REVIEW_ISSUES_FOUND)

        def write_v2() -> None:
            (job_dir / "plan-history" / "plan-v2.md").write_text(SAMPLE_PLAN)

        def write_review_issues_v2() -> None:
            (job_dir / "plan-history" / "review-v2.md").write_text(SAMPLE_REVIEW_ISSUES_FOUND)

        scripts = [
            (write_v1, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_issues_v1, [_make_result_msg()]),
            (write_v2, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_issues_v2, [_make_result_msg()]),
        ]

        # cap=1 — first review's issues count as round 1, second review trips it.
        opts = JobOptions(max_plan_review_rounds=1)
        with patched_query(scripts), pytest.raises(PlanPhaseFailed) as exc_info:
            await run_plan_phase(
                job_dir=job_dir,
                options=opts,
                planner_options_factory=lambda **_kw: object(),
                reviewer_options_factory=lambda **_kw: object(),
            )
        assert exc_info.value.phase == "plan-review"
        assert "max_plan_review_rounds_exceeded" in exc_info.value.error_text
        assert "missing actual SQL schema" in exc_info.value.error_text
        assert "no test plan" in exc_info.value.error_text

    @pytest.mark.asyncio
    async def test_max_sprints_oversize_skips_reviewer_and_revises(
        self, tmp_path: Path
    ) -> None:
        """Spec §5.2:328 — when a plan has more sprints than `max_sprints`,
        skip the Reviewer round entirely and inject a synthetic
        `[implementation]` issue into the next Planner revision."""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "design.md").write_text("DESIGN")
        (job_dir / "plan-history").mkdir()

        # v1 has 3 sprints (exceeds max_sprints=2), v2 has 2 sprints (within cap).
        oversized = dedent(
            """
            # Plan
            ## Sprint 1: A
            ## Sprint 2: B
            ## Sprint 3: C
            """
        ).strip()
        within_cap = dedent(
            """
            # Plan
            ## Sprint 1: AB
            ## Sprint 2: C
            """
        ).strip()

        def write_v1() -> None:
            (job_dir / "plan-history" / "plan-v1.md").write_text(oversized)

        def write_v2() -> None:
            (job_dir / "plan-history" / "plan-v2.md").write_text(within_cap)

        def write_review_v2() -> None:
            (job_dir / "plan-history" / "review-v2.md").write_text(SAMPLE_REVIEW_APPROVED)

        scripts = [
            (write_v1, [_make_assistant_with_skill_call(), _make_result_msg()]),
            # NOTE: no review-v1 script — the loop must skip the Reviewer.
            (write_v2, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_review_v2, [_make_result_msg()]),
        ]

        opts = JobOptions(max_sprints=2)
        with patched_query(scripts):
            sprints, rounds = await run_plan_phase(
                job_dir=job_dir,
                options=opts,
                planner_options_factory=lambda **_kw: object(),
                reviewer_options_factory=lambda **_kw: object(),
            )
        assert len(sprints) == 2
        assert rounds == 1  # one revision driven by the synthetic issue
        # review-v1.md should NOT exist (Reviewer skipped on oversized plan).
        assert not (job_dir / "plan-history" / "review-v1.md").is_file()

    @pytest.mark.asyncio
    async def test_unstructured_v1_after_retry_phase_planning(self, tmp_path: Path) -> None:
        """Spec §5.1:320 — initial plan-v1 unstructured after retry → phase=planning."""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "design.md").write_text("DESIGN")
        (job_dir / "plan-history").mkdir()

        def write_unstructured() -> None:
            (job_dir / "plan-history" / "plan-v1.md").write_text(
                "# No sprints here\n\nfree-form.\n"
            )

        scripts = [
            (write_unstructured, [_make_assistant_with_skill_call(), _make_result_msg()]),
            (write_unstructured, [_make_result_msg()]),  # retry leaves it unstructured
        ]

        with patched_query(scripts), pytest.raises(PlanPhaseFailed) as exc_info:
            await run_plan_phase(
                job_dir=job_dir,
                options=JobOptions(),
                planner_options_factory=lambda **_kw: object(),
                reviewer_options_factory=lambda **_kw: object(),
            )
        assert exc_info.value.phase == "planning"
        assert exc_info.value.error_text == "planner_emitted_unstructured_plan_after_retry"
