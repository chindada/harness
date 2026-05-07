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
