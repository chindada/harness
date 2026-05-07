"""Filesystem paths, default JobOptions, and the `now_ms()` time source.

`HARNESS_HOME` env var overrides the default `~/.harness/` for tests
and CI; production deployments leave it unset.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from harness_mcp.types import InvalidOptionsError


def harness_home() -> Path:
    """Resolve `~/.harness/` (or `$HARNESS_HOME` if set) as an absolute Path."""
    override = os.environ.get("HARNESS_HOME")
    return (
        Path(override).expanduser().resolve() if override else (Path.home() / ".harness").resolve()
    )


def jobs_root() -> Path:
    """`~/.harness/jobs/` — parent dir for every job's working directory."""
    return harness_home() / "jobs"


def state_db_path() -> Path:
    """`~/.harness/state.db` — SQLite file backing the state machine."""
    return harness_home() / "state.db"


def job_dir(job_id: str) -> Path:
    """Resolve `<jobs_root>/<job_id>/`."""
    return jobs_root() / job_id


def now_ms() -> int:
    """Current epoch in milliseconds (int).

    SQLite has no NOW(); every timestamp the schema stores is injected
    from Python via this helper. Tests should patch via `monkeypatch.setattr`
    rather than freezing time globally.
    """
    return int(time.time() * 1000)


@dataclass(frozen=True)
class JobOptions:
    """Per-job knobs. Defaults from spec §10.2.

    All fields are positive ints. Construct from an MCP dict via `from_dict()`,
    which validates keys and types and raises `InvalidOptionsError` on misuse.
    """

    max_sprints: int = 10
    max_sprint_duration_minutes: int = 45
    max_contract_negotiation_rounds: int = 3
    max_sprint_retries: int = 2
    max_plan_review_rounds: int = 5
    codex_reset_steps: int = 60
    codex_reset_minutes: int = 25
    max_codex_chunks_per_sprint: int = 8
    max_negotiation_turns: int = 3
    max_evaluation_seconds: int = 1800

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> JobOptions:
        """Build a JobOptions from a (possibly None or empty) dict.

        Unknown keys → InvalidOptionsError (closed-set; protects against typos).
        Wrong types or non-positive values → InvalidOptionsError.
        Missing keys fall back to dataclass field defaults via `cls(**validated)` —
        the caller can pass a partial dict and unspecified knobs keep their defaults.
        """
        if not raw:
            return cls()

        valid_keys = {f.name for f in fields(cls)}
        unknown = set(raw.keys()) - valid_keys
        if unknown:
            raise InvalidOptionsError(f"unknown option keys: {sorted(unknown)}")

        validated: dict[str, int] = {}
        for k, v in raw.items():
            if not isinstance(v, int) or isinstance(v, bool):
                raise InvalidOptionsError(f"option {k!r} must be int, got {type(v).__name__}")
            if v <= 0:
                raise InvalidOptionsError(f"option {k!r} must be positive, got {v}")
            validated[k] = v

        # Only validated keys are passed; missing keys keep their dataclass defaults.
        return cls(**validated)
