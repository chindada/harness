"""Tests for harness_mcp.evaluator_runner — launcher entry point.

Spec §8.4 declares two invariants that we enforce here:
  1. No transitive import of harness_mcp.state (would race the orchestrator's writer).
  2. Path validation: every path under `payload["paths"]` must live inside
     `~/.harness/jobs/<job_id>/`. Paths outside that prefix are refused.
"""

from __future__ import annotations

import importlib
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
