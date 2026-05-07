"""Tests for harness_mcp.prompts_loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_mcp import prompts_loader as pl
from harness_mcp.prompts_loader import _resolved_prompt, _resolved_prompt_text
from harness_mcp.types import PromptNotFoundError

REQUIRED_PROMPTS = ("planner.md", "reviewer.md", "evaluator.md", "generator.md", "summarizer.md")


class TestResolvedPrompt:
    @pytest.mark.parametrize("name", REQUIRED_PROMPTS)
    def test_each_required_prompt_resolves(self, name: str) -> None:
        p = _resolved_prompt(name)
        assert isinstance(p, Path)
        assert p.is_file()
        assert p.name == name

    def test_missing_prompt_raises(self) -> None:
        with pytest.raises(PromptNotFoundError):
            _resolved_prompt("does-not-exist.md")


class TestResolvedPromptText:
    @pytest.mark.parametrize("name", REQUIRED_PROMPTS)
    def test_each_prompt_has_nonempty_text(self, name: str) -> None:
        text = _resolved_prompt_text(name)
        assert isinstance(text, str)
        assert len(text.strip()) >= 50  # the smallest prompt still has body content

    def test_no_caching_picks_up_edits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §9: 'Hot-edits work because we re-read the file on every spawn.'"""
        # Point the loader at a temp file by monkeypatching the resolver.
        fake = tmp_path / "fake.md"
        fake.write_text("v1", encoding="utf-8")
        monkeypatch.setattr(pl, "_resolved_prompt", lambda name: fake)

        assert pl._resolved_prompt_text("fake.md") == "v1"
        fake.write_text("v2", encoding="utf-8")
        assert pl._resolved_prompt_text("fake.md") == "v2"
