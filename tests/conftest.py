"""Shared pytest fixtures for harness-mcp tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_mcp import config as cfg


@pytest.fixture
def tmp_harness_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect `~/.harness/` to a per-test tmp directory.

    Sets `HARNESS_HOME` so harness_mcp.config.harness_home() resolves to the tmp dir.
    Creates the dir before yielding so callers can assume it exists.
    """
    home = tmp_path / "harness_home"
    home.mkdir()
    (home / "jobs").mkdir()
    monkeypatch.setenv("HARNESS_HOME", str(home))
    yield home


@pytest.fixture
def frozen_now_ms(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Replace harness_mcp.config.now_ms with a deterministic counter.

    Yields a list whose [0] is the current "time"; tests can advance it
    by mutating the list. Default starting value: 1_700_000_000_000.
    """
    counter = [1_700_000_000_000]

    def _fake_now() -> int:
        counter[0] += 1
        return counter[0]

    monkeypatch.setattr(cfg, "now_ms", _fake_now)
    yield counter
