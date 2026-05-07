"""Resolve packaged prompt files via importlib.resources.

We deliberately don't cache content. Re-reading on every spawn lets users
hot-edit a prompt between jobs without restarting the harness server.

The Claude Agent SDK's `system_prompt` parameter accepts plain strings or
the `{"type": "preset", ...}` dict — the `{"type": "file"}` form documented
in some examples is silently ignored. So every spawn site must call
`_resolved_prompt_text(...)` to get a string.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from harness_mcp.types import PromptNotFoundError

_PROMPTS_ROOT = files("harness_mcp") / "prompts"


def _resolved_prompt(name: str) -> Path:
    """Return the absolute path of a prompt file shipped inside the package."""
    p = Path(str(_PROMPTS_ROOT / name))
    if not p.is_file():
        raise PromptNotFoundError(f"prompt {name!r} missing at {p}")
    return p


def _resolved_prompt_text(name: str) -> str:
    """Read the prompt's text fresh at every call (no caching)."""
    return _resolved_prompt(name).read_text(encoding="utf-8")
