"""Tests for harness_mcp.types — dataclasses and exception classes."""

from dataclasses import FrozenInstanceError

import pytest

from harness_mcp.types import (
    CommitFailedError,
    Criterion,
    DesignDocNotFoundError,
    EvaluationResult,
    EvaluatorEmittedUnparseableEvalMdError,
    GeneratorChunkError,
    Handoff,
    HandoffParseError,
    HarnessToolError,
    ImplementationResult,
    InvalidOptionsError,
    JobNotFinishedError,
    PromptNotFoundError,
    UnknownJobError,
)


class TestExceptionHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [
            UnknownJobError,
            JobNotFinishedError,
            DesignDocNotFoundError,
            InvalidOptionsError,
        ],
    )
    def test_tool_errors_subclass_base(self, cls: type[HarnessToolError]) -> None:
        err = cls("msg")
        assert isinstance(err, HarnessToolError)
        assert str(err) == "msg"

    @pytest.mark.parametrize(
        "cls",
        [
            HandoffParseError,
            CommitFailedError,
            EvaluatorEmittedUnparseableEvalMdError,
            PromptNotFoundError,
        ],
    )
    def test_internal_errors_inherit_exception(self, cls: type[Exception]) -> None:
        err = cls("msg")
        assert isinstance(err, Exception)
        assert str(err) == "msg"

    def test_generator_chunk_error_carries_chunk_seq(self) -> None:
        inner = ValueError("boom")
        err = GeneratorChunkError(chunk_seq=3, inner=inner)
        assert err.chunk_seq == 3
        assert err.inner is inner
        assert "chunk 3" in str(err)


class TestCriterion:
    def test_frozen(self) -> None:
        c = Criterion(text="x", result="PASS", evidence="e", notes="n")
        with pytest.raises(FrozenInstanceError):
            c.text = "y"  # type: ignore[misc]


class TestEvaluationResult:
    def test_passed_when_all_pass(self) -> None:
        crit_pass = Criterion("c", "PASS", "e", "")
        r = EvaluationResult(
            sprint_seq=1,
            static_criteria=[crit_pass],
            dynamic_criteria=[crit_pass],
            routing_decision="ran tests",
            passed=True,
        )
        assert r.passed is True

    def test_unparseable_default_false(self) -> None:
        r = EvaluationResult(
            sprint_seq=1,
            static_criteria=[],
            dynamic_criteria=[],
            routing_decision="",
            passed=False,
        )
        assert r.unparseable is False


class TestHandoff:
    def test_declares_done_when_status_done(self) -> None:
        h = Handoff(
            chunk_seq=1,
            status="done",
            summary="s",
            work_done=[],
            decisions=[],
            files_touched=[],
            open_questions=[],
            next_steps=[],
            declares_done=True,
        )
        assert h.declares_done is True

    def test_declares_done_false_when_in_progress(self) -> None:
        h = Handoff(
            chunk_seq=1,
            status="in-progress",
            summary="s",
            work_done=[],
            decisions=[],
            files_touched=[],
            open_questions=[],
            next_steps=["x"],
            declares_done=False,
        )
        assert h.declares_done is False


class TestImplementationResult:
    def test_default_files_touched_is_empty_list(self) -> None:
        r = ImplementationResult(ok=True)
        assert r.files_touched == []
        assert r.commit_sha is None
        assert r.error is None

    def test_failure_carries_error(self) -> None:
        r = ImplementationResult(ok=False, error="commit_failed: x")
        assert r.ok is False
        assert r.error == "commit_failed: x"
