"""Codex Generator wrapper: chunk loop, handoff parsing, commit helper.

This module is split into three layers:
  1. parse_handoff    — handoff-NNN.md → Handoff dataclass (pure parser).
  2. build_chunk_prompt — selects one of four prompt shapes per call site.
  3. chunk_loop       — the §7 reset-and-handoff loop. Imports AsyncCodex.
  4. commit_and_summarize — git add . / commit / tag at sprint end.

Layers 1 + 2 are pure (no I/O beyond reading filesystem); layer 3 does
async + subprocess via the SDK; layer 4 shells out to git.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from time import monotonic

import anyio

# Imported lazily so unit tests can monkeypatch AsyncCodex without bringing the SDK in.
try:
    from codex_app_server import AppServerConfig, AsyncCodex, TextInput
except ImportError:  # pragma: no cover  — only hit if SDK isn't installed during isolated unit runs
    AsyncCodex = AppServerConfig = TextInput = None  # type: ignore[assignment]

from harness_mcp.config import JobOptions
from harness_mcp.logging_setup import EventLogger
from harness_mcp.types import (
    CommitFailedError,
    GeneratorChunkError,
    Handoff,
    HandoffParseError,
    ImplementationResult,
    TagCollisionError,
)

logger = logging.getLogger(__name__)

# ---------- Layer 1: parse_handoff ----------


_STATUS_HEADER_RE = re.compile(r"^##\s+Status\s*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[\s]*[-*]\s+(.+?)\s*$", re.MULTILINE)
_FILENAME_SEQ_RE = re.compile(r"handoff-(\d+)\.md$")


def _split_sections(text: str) -> dict[str, str]:
    """Map each `## Heading` to the body that follows (until next heading)."""
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        heading = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[heading] = text[start:end].strip()
    return sections


def _parse_files_touched(body: str) -> list[tuple[str, str]]:
    """Each bullet → (path, reason). Split on first ` — ` (em-dash)."""
    out: list[tuple[str, str]] = []
    for m in _BULLET_RE.finditer(body):
        bullet = m.group(1).strip()
        if " — " in bullet:
            path, _, reason = bullet.partition(" — ")
            path = path.strip()
            reason = reason.strip()
        else:
            path, reason = bullet, ""
        if path:
            out.append((path, reason))
    return out


def _parse_bullets(body: str) -> list[str]:
    return [m.group(1).strip() for m in _BULLET_RE.finditer(body)]


def parse_handoff(path: Path) -> Handoff:
    """Parse handoff-NNN.md → Handoff. Raise HandoffParseError on any malformedness."""
    if not path.is_file():
        raise HandoffParseError(f"handoff file missing: {path}")

    text = path.read_text(encoding="utf-8")
    sections = _split_sections(text)

    if "status" not in sections:
        raise HandoffParseError(f"missing `## Status` section in {path.name}")

    status_body = sections["status"].strip()
    if status_body not in ("in-progress", "done"):
        raise HandoffParseError(
            f"invalid status {status_body!r} in {path.name}; expected 'in-progress' or 'done'"
        )

    summary = sections.get("summary", "").strip()
    work_done = _parse_bullets(sections.get("work done this chunk", ""))
    decisions = _parse_bullets(sections.get("decisions made", ""))
    files_touched = _parse_files_touched(sections.get("files touched", ""))
    open_questions = _parse_bullets(sections.get("open questions / concerns", ""))
    next_steps = _parse_bullets(sections.get("next steps (if in-progress)", ""))

    seq_match = _FILENAME_SEQ_RE.search(path.name)
    chunk_seq = int(seq_match.group(1)) if seq_match else 0

    return Handoff(
        chunk_seq=chunk_seq,
        status=status_body,
        summary=summary,
        work_done=work_done,
        decisions=decisions,
        files_touched=files_touched,
        open_questions=open_questions,
        next_steps=next_steps,
        declares_done=(status_body == "done"),
    )


# ---------- Layer 2: build_chunk_prompt ----------


def _read_or_empty(p: Path | None) -> str:
    if p is None or not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")


def build_chunk_prompt(
    *,
    generator_md: str,
    contract_path: Path,
    design_path: Path | None,
    plan_section_path: Path | None,
    prev_handoff: Path | None,
    eval_md_for_retry: Path | None,
    handoff_path: Path,
    chunk_seq: int,
) -> str:
    """Build one of three implementation-prompt shapes (Shapes 2-4) per spec §7.0.

    Shape 1 (contract-negotiation) is assembled in `sprints.negotiate_contract`;
    this function only handles the chunk-loop's implementation shapes.

    Selection rules:
      * eval_md_for_retry given          → Shape 4 (retry)
      * chunk_seq == 1, no retry         → Shape 2 (first chunk)
      * chunk_seq > 1, prev_handoff path → Shape 3 (continuation)
      * chunk_seq > 1, prev_handoff None → Shape 3 with "no valid handoff" addendum
    """
    contract = _read_or_empty(contract_path)
    design = _read_or_empty(design_path)
    plan_section = _read_or_empty(plan_section_path)

    if eval_md_for_retry is not None:
        eval_body = _read_or_empty(eval_md_for_retry)
        return _shape_retry(generator_md, contract, eval_body, handoff_path)

    if chunk_seq == 1:
        return _shape_first(generator_md, design, plan_section, contract, handoff_path)

    prev_body = _read_or_empty(prev_handoff)
    return _shape_continued(generator_md, contract, prev_body, handoff_path, chunk_seq)


def _shape_first(
    generator_md: str,
    design: str,
    plan_section: str,
    contract: str,
    handoff_path: Path,
) -> str:
    return f"""{generator_md}

## Mode: implementation (first chunk)

## Design (verbatim)
{design}

## Plan section (verbatim)
{plan_section}

## Contract (verbatim)
{contract}

## Where to write your handoff
At the end of this chunk, write your handoff to: {handoff_path}
Use the format documented in the system prompt (atomic write: <name>.tmp then rename).
"""


def _shape_continued(
    generator_md: str,
    contract: str,
    prev_handoff_body: str,
    handoff_path: Path,
    chunk_seq: int,
) -> str:
    if prev_handoff_body:
        prev_section = f"## Previous handoff (verbatim)\n{prev_handoff_body}"
    else:
        prev_section = (
            "## Previous handoff (verbatim)\n"
            "Previous chunk produced no valid handoff. Proceed fresh based on contract.md and "
            "what's already in the working tree."
        )
    return f"""{generator_md}

## Mode: implementation (chunk {chunk_seq}, continuation)

## Contract (verbatim)
{contract}

{prev_section}

## Where to write your handoff
At the end of this chunk, write your handoff to: {handoff_path}
Pick up from "Next steps" in the previous handoff if available.
"""


def _shape_retry(generator_md: str, contract: str, eval_body: str, handoff_path: Path) -> str:
    return f"""{generator_md}

## Mode: implementation (retry — previous attempt failed evaluation)

## Contract (verbatim, READ-ONLY)
{contract}

## Failed evaluation (verbatim)
{eval_body}

## Instructions
The previous attempt failed the evaluation above. Address the specific FAIL criteria
without expanding scope. Do NOT propose new criteria. The contract is fixed — work within it.

## Where to write your handoff
At the end of this chunk, write your handoff to: {handoff_path}
"""


# ---------- Layer 4: commit_and_summarize ----------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command synchronously, raise CommitFailedError on non-zero exit."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise CommitFailedError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


async def commit_and_summarize(
    app_dir: Path, handoff: Handoff, *, sprint_seq: int, job_id: str
) -> ImplementationResult:
    """Commit any uncommitted changes in `app_dir`, tag `harness/<job_id>/sprint-<N>`.

    `git add .` honors the .gitignore (seeded at job start). If the working
    tree has uncommitted changes, commit with the handoff's summary as
    subject + work-done/decisions in the body. If it doesn't, skip the
    commit but still tag (so subsequent sprints can diff against it).

    Tag overwrite (`git tag -f`): per spec §6.4 we run the namespace-aware
    annotated-tag-collision check inline — same-job tags get overwritten
    (with a warning); other-job or user-curated annotated tags raise
    TagCollisionError so the sprint surfaces `harness_tag_collision`.
    """
    # All git invocations are blocking C calls; offload so the event loop stays free.
    return await anyio.to_thread.run_sync(
        _commit_and_tag_sync, app_dir, handoff, sprint_seq, job_id
    )


def _commit_and_tag_sync(
    app_dir: Path, handoff: Handoff, sprint_seq: int, job_id: str
) -> ImplementationResult:
    if not (app_dir / ".git").is_dir():
        raise CommitFailedError(f"{app_dir} is not a git repository")

    _git(["add", "."], cwd=app_dir)

    # Was anything actually staged?
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(app_dir),
        capture_output=True,
        check=False,
    )
    needs_commit = diff.returncode != 0

    commit_sha: str | None = None
    if needs_commit:
        subject = f"Sprint {sprint_seq}: {handoff.summary[:80]}"
        body_lines = ["", ""]
        if handoff.work_done:
            body_lines.append("Work done:")
            for w in handoff.work_done:
                body_lines.append(f"- {w}")
            body_lines.append("")
        if handoff.decisions:
            body_lines.append("Decisions:")
            for d in handoff.decisions:
                body_lines.append(f"- {d}")
        full_msg = subject + "\n" + "\n".join(body_lines)
        _git(["commit", "-q", "-m", full_msg], cwd=app_dir)
        commit_sha = _git(["rev-parse", "HEAD"], cwd=app_dir).stdout.strip()
    else:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(app_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        commit_sha = head.stdout.strip() if head.returncode == 0 else None

    tag_name = f"harness/{job_id}/sprint-{sprint_seq}"
    # Spec §6.4 — narrowed annotated-tag collision check. `git for-each-ref`
    # with `%(taggerdate)` returns non-empty only for *annotated* tags
    # (lightweight tags have no taggerdate). Non-empty + foreign namespace
    # = refuse; non-empty + same job = retry (warn + overwrite).
    existing = subprocess.run(
        ["git", "for-each-ref", "--format=%(taggerdate)", f"refs/tags/{tag_name}"],
        cwd=str(app_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0 and existing.stdout.strip():
        own_namespace = f"harness/{job_id}/"
        if not tag_name.startswith(own_namespace):
            raise TagCollisionError("harness_tag_collision")
        logger.warning(
            "overwriting prior harness annotated tag from same job; this is a retry: %s",
            tag_name,
        )
    # Use -a -m to support environments where tag.gpgsign / tag annotation is forced.
    _git(["tag", "-f", "-a", "-m", f"Sprint {sprint_seq} for {job_id}", tag_name], cwd=app_dir)

    return ImplementationResult(
        ok=True,
        files_touched=[p for p, _ in handoff.files_touched],
        commit_sha=commit_sha,
        summary=handoff.summary,
    )


# ---------- Layer 3: chunk_loop ----------


async def chunk_loop(  # noqa: PLR0912, PLR0915, PLR0911 — branching follows the §7 chunk-loop state machine
    *,
    app_dir: Path,
    sprint_dir: Path,
    contract_path: Path,
    design_path: Path | None,
    plan_section_path: Path | None,
    log_path: Path,
    options: JobOptions,
    generator_md_text: str,
    sprint_seq: int,
    job_id: str,
    eval_md_for_retry: Path | None,
    codex_bin: str | None = None,
    codex_config_overrides: tuple[str, ...] = (
        "sandbox=workspace-write",
        "approval_policy=never",
    ),
) -> ImplementationResult:
    """Drive AsyncCodex through bounded chunks until handoff says `done`.

    See spec §7 for the full reset-and-handoff state machine. This function:
      * Iterates chunk_seq from 1.
      * Per chunk: open AsyncCodex, start a thread, send the chunk prompt,
        stream events into EventLogger, count `item/started` events for the
        step-budget reset, observe wall-clock for the time-budget reset.
      * After each chunk, parse handoff-NNN.md. If declares_done → commit + tag.
      * Cap at options.max_codex_chunks_per_sprint.
    """
    chunk_seq = 1
    prev_handoff: Path | None = None

    while True:
        handoff_path = sprint_dir / f"handoff-{chunk_seq:03d}.md"

        cfg = (
            AppServerConfig(
                codex_bin=codex_bin,
                cwd=str(app_dir),
                config_overrides=codex_config_overrides,
                client_name="harness-mcp",
                client_title="Harness Generator",
                client_version="0.1.0",
            )
            if AppServerConfig is not None
            else None
        )

        event_logger = EventLogger(log_path)
        step_count = 0
        chunk_started = monotonic()

        try:
            async with AsyncCodex(config=cfg) as codex:
                thread = await codex.thread_start()
                prompt = build_chunk_prompt(
                    generator_md=generator_md_text,
                    contract_path=contract_path,
                    design_path=design_path,
                    plan_section_path=plan_section_path,
                    prev_handoff=prev_handoff,
                    eval_md_for_retry=eval_md_for_retry,
                    handoff_path=handoff_path,
                    chunk_seq=chunk_seq,
                )
                turn = await thread.turn(TextInput(prompt) if TextInput else prompt)

                async for event in turn.stream():
                    await event_logger.handle(event)
                    if getattr(event, "method", "") == "item/started":
                        step_count += 1
                    if step_count >= options.codex_reset_steps:
                        break
                    if monotonic() - chunk_started >= options.codex_reset_minutes * 60:
                        break
        except Exception as e:
            await event_logger.aclose()
            raise GeneratorChunkError(chunk_seq, e) from e
        finally:
            await event_logger.aclose()

        # Parse handoff. Malformed = warn, fresh-start, count toward cap.
        try:
            handoff = parse_handoff(handoff_path)
        except HandoffParseError as parse_err:
            if chunk_seq < options.max_codex_chunks_per_sprint:
                logger.warning(
                    "chunk %d: handoff malformed (%s); continuing fresh",
                    chunk_seq,
                    parse_err,
                )
                prev_handoff = None
                chunk_seq += 1
                continue
            # Spec §7 cap-boundary salvage: if the malformed handoff's tail still
            # declares Status=done, synthesize a Handoff and commit so we don't
            # waste the chunk's work on a parse-only error.
            if handoff_path.is_file():
                tail = handoff_path.read_text(encoding="utf-8", errors="replace")[-2048:]
                if re.search(r"^## Status\s*\n+\s*done\s*$", tail, re.MULTILINE):
                    logger.warning(
                        "chunk %d: handoff malformed but Status=done detected; salvaging",
                        chunk_seq,
                    )
                    synthetic = Handoff(
                        chunk_seq=chunk_seq,
                        status="done",
                        summary=f"sprint {sprint_seq} (salvaged)",
                        work_done=[],
                        decisions=[],
                        files_touched=[],
                        open_questions=[],
                        next_steps=[],
                        declares_done=True,
                    )
                    try:
                        return await commit_and_summarize(
                            app_dir, synthetic, sprint_seq=sprint_seq, job_id=job_id
                        )
                    except TagCollisionError as e:
                        return ImplementationResult(ok=False, error=str(e))
                    except CommitFailedError as ce:
                        return ImplementationResult(ok=False, error=f"commit_failed: {ce}")
            return ImplementationResult(ok=False, error="handoff_persistently_malformed")

        if handoff.declares_done:
            try:
                return await commit_and_summarize(
                    app_dir, handoff, sprint_seq=sprint_seq, job_id=job_id
                )
            except TagCollisionError as e:
                # Spec §6.4: surface verbatim, no `commit_failed:` prefix.
                return ImplementationResult(ok=False, error=str(e))
            except Exception as e:  # CommitFailedError or worse
                return ImplementationResult(ok=False, error=f"commit_failed: {e}")

        prev_handoff = handoff_path
        chunk_seq += 1
        if chunk_seq > options.max_codex_chunks_per_sprint:
            return ImplementationResult(ok=False, error="generator_chunk_cap_exhausted")
