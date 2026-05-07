"""Tests for harness_mcp.summarizer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from harness_mcp import summarizer as _summarizer_module
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
        assert "2–3 sentences" in prompt or "2-3 sentences" in prompt  # noqa: RUF001

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
        # drains the async iterator and re-reads the file - it doesn't inspect
        # message types, so we can yield a sentinel rather than a typed mock.
        async def fake_query(prompt: str, options: Any) -> AsyncIterator[Any]:
            _ = (prompt, options)
            (job_dir / "summary.md").write_text(
                "Built a tiny TODO app. 3 of 3 sprints passed.\n", encoding="utf-8"
            )
            yield object()

        with patch.object(_summarizer_module, "query", fake_query):
            text = await run_summarizer(job_dir=job_dir, options=object())
            assert "TODO app" in text
            assert (job_dir / "summary.md").is_file()
