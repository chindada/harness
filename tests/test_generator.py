"""Tests for harness_mcp.generator — handoff parser, chunk prompt builder, commit helper."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from harness_mcp import generator as gen_mod
from harness_mcp.config import JobOptions
from harness_mcp.generator import (
    build_chunk_prompt,
    chunk_loop,
    commit_and_summarize,
    parse_handoff,
)
from harness_mcp.types import CommitFailedError, Handoff, HandoffParseError

GOOD_HANDOFF = dedent(
    """
    # Handoff 2

    ## Status
    in-progress

    ## Summary
    Wired up the /api/todos GET endpoint.

    ## Work done this chunk
    - Added the route in app.py.
    - Wrote the ORM helper.

    ## Decisions made
    - Used Flask's MethodView — simpler than class-based dispatch.

    ## Files touched
    - app.py — added blueprint registration
    - models.py — new Todo dataclass

    ## Open questions / concerns
    - None.

    ## Next steps (if in-progress)
    - Add POST endpoint
    - Add PATCH/DELETE
    """
).strip()


class TestParseHandoff:
    def test_full_parse(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-002.md"
        path.write_text(GOOD_HANDOFF, encoding="utf-8")
        h = parse_handoff(path)
        assert isinstance(h, Handoff)
        assert h.chunk_seq == 2
        assert h.status == "in-progress"
        assert h.declares_done is False
        assert h.summary.startswith("Wired up")
        assert ("app.py", "added blueprint registration") in h.files_touched
        assert ("models.py", "new Todo dataclass") in h.files_touched
        assert "Add POST endpoint" in h.next_steps

    def test_done_status(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-001.md"
        path.write_text(GOOD_HANDOFF.replace("in-progress", "done"), encoding="utf-8")
        h = parse_handoff(path)
        assert h.status == "done"
        assert h.declares_done is True

    def test_missing_status_section(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-001.md"
        path.write_text("# Handoff 1\n\n## Summary\nx\n", encoding="utf-8")
        with pytest.raises(HandoffParseError):
            parse_handoff(path)

    def test_invalid_status_value(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-001.md"
        path.write_text("# Handoff 1\n\n## Status\nmaybe\n\n## Summary\nx\n", encoding="utf-8")
        with pytest.raises(HandoffParseError):
            parse_handoff(path)

    def test_files_touched_no_em_dash_keeps_path_only(self, tmp_path: Path) -> None:
        body = (
            "# Handoff 1\n\n## Status\ndone\n\n## Summary\nx\n\n"
            "## Files touched\n- app.py\n- models.py — refactored\n"
        )
        path = tmp_path / "handoff-001.md"
        path.write_text(body, encoding="utf-8")
        h = parse_handoff(path)
        assert ("app.py", "") in h.files_touched
        assert ("models.py", "refactored") in h.files_touched

    def test_chunk_seq_inferred_from_filename(self, tmp_path: Path) -> None:
        path = tmp_path / "handoff-007.md"
        path.write_text(GOOD_HANDOFF, encoding="utf-8")
        h = parse_handoff(path)
        assert h.chunk_seq == 7

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(HandoffParseError):
            parse_handoff(tmp_path / "nonexistent.md")


class TestBuildChunkPrompt:
    def test_first_chunk_includes_design_and_plan(self, tmp_path: Path) -> None:
        design = tmp_path / "design.md"
        design.write_text("DESIGN_BODY")
        plan_section = tmp_path / "plan_section.md"
        plan_section.write_text("PLAN_SECTION_BODY")
        contract = tmp_path / "contract.md"
        contract.write_text("CONTRACT_BODY")
        handoff_path = tmp_path / "handoff-001.md"
        prompt = build_chunk_prompt(
            generator_md="GENERATOR_PROMPT",
            contract_path=contract,
            design_path=design,
            plan_section_path=plan_section,
            prev_handoff=None,
            eval_md_for_retry=None,
            handoff_path=handoff_path,
            chunk_seq=1,
        )
        assert "GENERATOR_PROMPT" in prompt
        assert "DESIGN_BODY" in prompt
        assert "PLAN_SECTION_BODY" in prompt
        assert "CONTRACT_BODY" in prompt
        assert str(handoff_path) in prompt
        assert "first chunk" in prompt.lower()

    def test_continued_chunk_includes_prev_handoff(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.md"
        contract.write_text("CONTRACT")
        prev = tmp_path / "handoff-001.md"
        prev.write_text("PREV_HANDOFF_BODY")
        handoff_path = tmp_path / "handoff-002.md"
        prompt = build_chunk_prompt(
            generator_md="G",
            contract_path=contract,
            design_path=None,
            plan_section_path=None,
            prev_handoff=prev,
            eval_md_for_retry=None,
            handoff_path=handoff_path,
            chunk_seq=2,
        )
        assert "PREV_HANDOFF_BODY" in prompt
        assert "continuation" in prompt.lower()

    def test_retry_chunk_includes_eval_md(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.md"
        contract.write_text("CONTRACT")
        eval_md = tmp_path / "eval.md"
        eval_md.write_text("EVAL_BODY_FAILED")
        handoff_path = tmp_path / "handoff-001.md"
        prompt = build_chunk_prompt(
            generator_md="G",
            contract_path=contract,
            design_path=None,
            plan_section_path=None,
            prev_handoff=None,
            eval_md_for_retry=eval_md,
            handoff_path=handoff_path,
            chunk_seq=1,
        )
        assert "EVAL_BODY_FAILED" in prompt
        assert "retry" in prompt.lower()
        assert "READ-ONLY" in prompt or "fixed" in prompt.lower()

    def test_continued_chunk_with_lost_prev_handoff(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.md"
        contract.write_text("CONTRACT")
        handoff_path = tmp_path / "handoff-003.md"
        prompt = build_chunk_prompt(
            generator_md="G",
            contract_path=contract,
            design_path=None,
            plan_section_path=None,
            prev_handoff=None,  # malformed previous handoff → reset
            eval_md_for_retry=None,
            handoff_path=handoff_path,
            chunk_seq=3,
        )
        assert "Proceed fresh" in prompt or "no valid handoff" in prompt.lower()


def _init_app_repo(app_dir: Path) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / ".gitignore").write_text("*.pyc\n.venv/\n")
    subprocess.run(["git", "init", "-q"], cwd=str(app_dir), check=True)
    # Configure local user so commits work in CI.
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=str(app_dir), check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(app_dir), check=True)


@pytest.fixture
def app_repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    app = tmp_path / "app"
    _init_app_repo(app)
    return app


class TestCommitAndSummarize:
    @pytest.mark.asyncio
    async def test_commits_changes_and_tags(self, app_repo: Path) -> None:
        # Add a file in the worktree.
        (app_repo / "x.py").write_text("print('hi')\n")
        h = Handoff(
            chunk_seq=1,
            status="done",
            summary="add x.py",
            work_done=["wrote x.py"],
            decisions=[],
            files_touched=[("x.py", "scaffold")],
            open_questions=[],
            next_steps=[],
            declares_done=True,
        )
        result = await commit_and_summarize(app_repo, h, sprint_seq=1, job_id="JOBID")
        assert result.ok is True
        assert result.commit_sha is not None
        assert result.files_touched == ["x.py"]

        # Tag should exist.
        tags = subprocess.run(  # noqa: ASYNC221 — test-only inspection
            ["git", "tag", "--list", "harness/JOBID/sprint-1"],
            cwd=str(app_repo),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "harness/JOBID/sprint-1" in tags.stdout

        # Spec §7.4: tag MUST be annotated (so collision narrowing works
        # against `git rev-parse <tag>^{tag}` later). `cat-file -t` returns
        # "tag" for annotated, "commit" for lightweight.
        tag_type = subprocess.run(  # noqa: ASYNC221 — test-only inspection
            ["git", "cat-file", "-t", "harness/JOBID/sprint-1"],
            cwd=str(app_repo),
            capture_output=True,
            text=True,
            check=True,
        )
        assert tag_type.stdout.strip() == "tag", (
            f"sprint tag must be annotated; got {tag_type.stdout!r}"
        )

    @pytest.mark.asyncio
    async def test_no_changes_to_commit_still_tags(self, app_repo: Path) -> None:
        # Initial empty commit so HEAD exists.
        (app_repo / "init.py").write_text("")
        subprocess.run(  # noqa: ASYNC221 — test-only setup
            ["git", "add", "init.py"], cwd=str(app_repo), check=True
        )
        subprocess.run(  # noqa: ASYNC221 — test-only setup
            ["git", "commit", "-q", "-m", "init"], cwd=str(app_repo), check=True
        )

        h = Handoff(
            chunk_seq=1,
            status="done",
            summary="no-op sprint",
            work_done=[],
            decisions=[],
            files_touched=[],
            open_questions=[],
            next_steps=[],
            declares_done=True,
        )
        result = await commit_and_summarize(app_repo, h, sprint_seq=2, job_id="JOBID")
        assert result.ok is True
        # Tag should still exist even with no new commit.
        tags = subprocess.run(  # noqa: ASYNC221 — test-only inspection
            ["git", "tag", "--list", "harness/JOBID/sprint-2"],
            cwd=str(app_repo),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "harness/JOBID/sprint-2" in tags.stdout

    @pytest.mark.asyncio
    async def test_commit_failed_when_not_a_git_repo(self, tmp_path: Path) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not available")
        # tmp_path is plain dir, not a git repo.
        h = Handoff(
            chunk_seq=1,
            status="done",
            summary="x",
            work_done=[],
            decisions=[],
            files_touched=[],
            open_questions=[],
            next_steps=[],
            declares_done=True,
        )
        with pytest.raises(CommitFailedError):
            await commit_and_summarize(tmp_path, h, sprint_seq=1, job_id="J")


@dataclass
class FakeTurn:
    events: list

    def stream(self) -> AsyncIterator:
        async def _gen() -> AsyncIterator:
            for e in self.events:
                yield e

        return _gen()


class FakeCodex:
    """Mocks AsyncCodex for the chunk_loop tests."""

    def __init__(self, scripted_handoffs: list[str], scripted_events_per_chunk: list[list]) -> None:
        self.scripted_handoffs = scripted_handoffs
        self.scripted_events_per_chunk = scripted_events_per_chunk
        self.chunk_idx = 0
        self.calls: list[str] = []  # captured prompts

    async def __aenter__(self) -> FakeCodex:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def thread_start(self) -> FakeCodex:
        return self

    async def turn(self, _input: object) -> FakeTurn:
        # Materialize the handoff file, then return scripted events.
        i = self.chunk_idx
        events = self.scripted_events_per_chunk[i]
        return FakeTurn(events=list(events))


def _agent_message(text: str) -> object:
    return SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta=text))


def _item_started() -> object:
    return SimpleNamespace(
        method="item/started",
        payload=SimpleNamespace(
            item=SimpleNamespace(id=f"i-{id(object())}", type="commandExecution", command="ls")
        ),
    )


def _turn_completed() -> object:
    return SimpleNamespace(
        method="turn/completed",
        payload=SimpleNamespace(
            turn=SimpleNamespace(id="t1", status=SimpleNamespace(value="completed"))
        ),
    )


class TestChunkLoop:
    @pytest.mark.asyncio
    async def test_done_handoff_triggers_commit(
        self, app_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        contract = sprint_dir / "contract.md"
        contract.write_text("CONTRACT")
        design = sprint_dir / "design.md"
        design.write_text("DESIGN")
        plan_section = sprint_dir / "plan_section.md"
        plan_section.write_text("PLAN_SECTION")
        log_path = sprint_dir / "log.txt"

        # Patch AsyncCodex so we can drive chunk_loop without the real SDK.
        # The fake's `_turn` simulates Codex doing real work: it writes a real file
        # into `app_repo` (so `git add .` later actually stages something) AND
        # writes the handoff into `sprint_dir` for the chunk loop to parse.

        @asynccontextmanager
        async def fake_async_codex(*_a: object, **_kw: object) -> AsyncIterator[object]:
            class _Thread:
                async def turn(self, _input: object) -> FakeTurn:
                    # Codex would have written code in cwd=app_repo.
                    (app_repo / "x.py").write_text("print('hi')\n", encoding="utf-8")
                    # Codex would also have written its handoff in the sprint dir.
                    (sprint_dir / "handoff-001.md").write_text(
                        GOOD_HANDOFF.replace("in-progress", "done"), encoding="utf-8"
                    )
                    return FakeTurn(events=[_agent_message("hello"), _turn_completed()])

            class _Wrap:
                async def thread_start(self) -> _Thread:
                    return _Thread()

            yield _Wrap()

        monkeypatch.setattr(gen_mod, "AsyncCodex", fake_async_codex)

        opts = JobOptions(
            codex_reset_steps=10, codex_reset_minutes=1, max_codex_chunks_per_sprint=4
        )
        result = await chunk_loop(
            app_dir=app_repo,
            sprint_dir=sprint_dir,
            contract_path=contract,
            design_path=design,
            plan_section_path=plan_section,
            log_path=log_path,
            options=opts,
            generator_md_text="GENERATOR_PROMPT",
            sprint_seq=1,
            job_id="JOBID",
            eval_md_for_retry=None,
        )
        assert result.ok is True
        assert result.commit_sha is not None

    @pytest.mark.asyncio
    async def test_max_chunks_exhausted_returns_failure(
        self, app_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("CONTRACT")

        chunk_idx = [0]

        @asynccontextmanager
        async def fake_async_codex(*_a: object, **_kw: object) -> AsyncIterator[object]:
            class _T:
                async def turn(self, _input: object) -> FakeTurn:
                    # Always write an in-progress handoff. Loop never converges.
                    handoff_path = sprint_dir / f"handoff-{chunk_idx[0] + 1:03d}.md"
                    handoff_path.write_text(GOOD_HANDOFF, encoding="utf-8")
                    chunk_idx[0] += 1
                    return FakeTurn(events=[_agent_message("..."), _turn_completed()])

            class _Wrap:
                async def thread_start(self) -> _T:
                    return _T()

            yield _Wrap()

        monkeypatch.setattr(gen_mod, "AsyncCodex", fake_async_codex)

        opts = JobOptions(codex_reset_steps=2, codex_reset_minutes=1, max_codex_chunks_per_sprint=2)
        result = await chunk_loop(
            app_dir=app_repo,
            sprint_dir=sprint_dir,
            contract_path=sprint_dir / "contract.md",
            design_path=None,
            plan_section_path=None,
            log_path=sprint_dir / "log.txt",
            options=opts,
            generator_md_text="G",
            sprint_seq=1,
            job_id="J",
            eval_md_for_retry=None,
        )
        assert result.ok is False
        assert "chunk_cap" in (result.error or "") or "exhausted" in (result.error or "")

    @pytest.mark.asyncio
    async def test_cap_boundary_salvage_when_status_done(
        self, app_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Spec §7: malformed handoff at the chunk cap should salvage if its
        tail still declares Status=done — synthesize a Handoff and commit."""
        sprint_dir = tmp_path / "sprint-1"
        sprint_dir.mkdir()
        (sprint_dir / "contract.md").write_text("CONTRACT")

        @asynccontextmanager
        async def fake_async_codex(*_a: object, **_kw: object) -> AsyncIterator[object]:
            class _T:
                async def turn(self, _input: object) -> FakeTurn:
                    # Codex wrote real work into the repo this chunk.
                    (app_repo / "salvage.py").write_text("ok\n", encoding="utf-8")
                    # Write a deliberately MALFORMED handoff: status block has
                    # trailing junk so parse_handoff rejects (status_body is
                    # multi-line, not exactly "done"), but the tail regex
                    # `^## Status\s*\n+\s*done\s*$` still matches the "done"
                    # line. That's the salvage condition per spec §7.
                    handoff_path = sprint_dir / "handoff-001.md"
                    handoff_path.write_text(
                        "## Status\n\ndone\n\nstray trailing junk that breaks parse\n",
                        encoding="utf-8",
                    )
                    return FakeTurn(events=[_agent_message("..."), _turn_completed()])

            class _Wrap:
                async def thread_start(self) -> _T:
                    return _T()

            yield _Wrap()

        monkeypatch.setattr(gen_mod, "AsyncCodex", fake_async_codex)

        # max_codex_chunks_per_sprint=1 forces the very first chunk to hit the
        # cap-boundary path on its malformed handoff.
        opts = JobOptions(codex_reset_steps=2, codex_reset_minutes=1, max_codex_chunks_per_sprint=1)
        result = await chunk_loop(
            app_dir=app_repo,
            sprint_dir=sprint_dir,
            contract_path=sprint_dir / "contract.md",
            design_path=None,
            plan_section_path=None,
            log_path=sprint_dir / "log.txt",
            options=opts,
            generator_md_text="G",
            sprint_seq=1,
            job_id="JOBID",
            eval_md_for_retry=None,
        )
        assert result.ok is True, f"salvage failed: {result.error}"
        assert "salvaged" in (result.summary or "")
        # Tag should still be created.
        tags = subprocess.run(  # noqa: ASYNC221 — test-only inspection
            ["git", "tag", "--list", "harness/JOBID/sprint-1"],
            cwd=str(app_repo),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "harness/JOBID/sprint-1" in tags.stdout
