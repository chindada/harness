"""Summarizer - one-shot Claude call producing summary.md at job end.

The user prompt is assembled here; the system prompt is the packaged
prompts/summarizer.md content (orchestrator passes it via options).

We do NOT inline `eval.md` contents - the prompt names files and the
agent reads them. (Inlining grows the prompt linearly with sprint count;
file reads scale better and the harness is happy to wait an extra turn.)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import query

_SPRINT_DIR_RE = re.compile(r"^sprint-(\d+)$")


def _sprint_dirs(job_dir: Path) -> list[Path]:
    """List sprint-N/ subdirs in numeric order."""
    if not job_dir.is_dir():
        return []
    sprints: list[tuple[int, Path]] = []
    for child in job_dir.iterdir():
        if not child.is_dir():
            continue
        m = _SPRINT_DIR_RE.match(child.name)
        if m:
            sprints.append((int(m.group(1)), child))
    sprints.sort()
    return [p for _, p in sprints]


def build_summarizer_prompt(job_dir: Path) -> str:
    sprint_lines = "\n".join(f"- {sprint.name}/eval.md" for sprint in _sprint_dirs(job_dir))
    if not sprint_lines:
        sprint_lines = "(no sprints - job ended before any sprint completed)"

    return f"""Read these files and write a 2-3 sentence summary to `summary.md` in your cwd.

## Files to read
- design.md
- plan.md
{sprint_lines}

## What to cover (in 2-3 sentences total)
- What was built (one phrase).
- Sprint pass/fail tally (e.g., "3 of 4 sprints passed; sprint 4 failed two dynamic criteria").
- What's incomplete (one phrase). If everything passed, say so.

Plain prose. No code blocks, no bullets, no editorializing. Output goes to a status line.
"""


async def run_summarizer(
    *,
    job_dir: Path,
    options: Any,  # noqa: ANN401  - opaque SDK ClaudeAgentOptions
) -> str:
    """Drive one query(), return the contents of summary.md after the agent finishes."""
    prompt = build_summarizer_prompt(job_dir)
    async for _msg in query(prompt=prompt, options=options):
        pass  # drain
    summary_path = job_dir / "summary.md"
    if not summary_path.is_file():
        return ""
    return summary_path.read_text(encoding="utf-8")
