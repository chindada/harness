"""Tests for harness_mcp.config — paths, JobOptions, time helpers."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from harness_mcp.config import (
    JobOptions,
    harness_home,
    job_dir,
    jobs_root,
    now_ms,
    state_db_path,
)
from harness_mcp.types import InvalidOptionsError


class TestPaths:
    def test_harness_home_under_user_home(self) -> None:
        assert harness_home() == Path.home() / ".harness"

    def test_jobs_root(self) -> None:
        assert jobs_root() == Path.home() / ".harness" / "jobs"

    def test_state_db_path(self) -> None:
        assert state_db_path() == Path.home() / ".harness" / "state.db"

    def test_job_dir_combines(self) -> None:
        assert job_dir("01ABC") == Path.home() / ".harness" / "jobs" / "01ABC"

    def test_harness_home_respects_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "alt"))
        assert harness_home() == tmp_path / "alt"


class TestNowMs:
    def test_returns_int(self) -> None:
        assert isinstance(now_ms(), int)

    def test_close_to_wallclock(self) -> None:
        before = int(time.time() * 1000)
        v = now_ms()
        after = int(time.time() * 1000)
        assert before - 5 <= v <= after + 5

    def test_monotonic_within_one_call(self) -> None:
        a = now_ms()
        b = now_ms()
        assert b >= a


class TestJobOptionsDefaults:
    def test_defaults_match_spec(self) -> None:
        o = JobOptions()
        assert o.max_sprints == 10
        assert o.max_sprint_duration_minutes == 45
        assert o.max_contract_negotiation_rounds == 3
        assert o.max_sprint_retries == 2
        assert o.max_plan_review_rounds == 5
        assert o.codex_reset_steps == 60
        assert o.codex_reset_minutes == 25
        assert o.max_codex_chunks_per_sprint == 8
        assert o.max_negotiation_turns == 3
        assert o.max_evaluation_seconds == 1800

    def test_from_dict_unknown_key_raises(self) -> None:
        with pytest.raises(InvalidOptionsError):
            JobOptions.from_dict({"bogus_key": 1})

    def test_from_dict_partial_overrides_only_specified(self) -> None:
        o = JobOptions.from_dict({"max_sprints": 3})
        assert o.max_sprints == 3
        assert o.max_sprint_retries == 2

    def test_from_dict_negative_value_raises(self) -> None:
        with pytest.raises(InvalidOptionsError):
            JobOptions.from_dict({"max_sprints": -1})

    def test_from_dict_zero_max_sprints_raises(self) -> None:
        with pytest.raises(InvalidOptionsError):
            JobOptions.from_dict({"max_sprints": 0})

    def test_from_dict_wrong_type_raises(self) -> None:
        with pytest.raises(InvalidOptionsError):
            JobOptions.from_dict({"max_sprints": "ten"})  # type: ignore[dict-item]
