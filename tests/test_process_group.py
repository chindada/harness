"""Tests for harness_mcp.process_group — ProcessGroupScope."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from harness_mcp.process_group import PIPE, ProcessGroupScope


@pytest.fixture
def child_script(tmp_path: Path) -> Path:
    """A Python script that prints its pgid + sleeps forever."""
    p = tmp_path / "child.py"
    p.write_text(
        textwrap.dedent(
            """
            import os, sys, time
            print(os.getpgid(0), flush=True)
            try:
                time.sleep(60)
            except KeyboardInterrupt:
                pass
            """
        ).strip()
    )
    return p


class TestProcessGroupScope:
    @pytest.mark.asyncio
    async def test_spawn_uses_new_session(self, child_script: Path) -> None:
        async with ProcessGroupScope("test-1") as pg:
            proc = await pg.spawn(
                [sys.executable, str(child_script)],
                stdout=PIPE,
                stderr=PIPE,
            )
            # Read the child's printed pgid (its process group leader = its own pid).
            assert proc.stdout is not None
            line = await proc.stdout.receive(64)
            child_pgid = int(line.decode().strip())
            # We spawned with start_new_session=True so child's pgid == its pid.
            assert child_pgid == proc.pid
            # And the launcher pid is in our scope's tracked pgid.
            assert pg.tracked_pgid == proc.pid
        # On scope exit, the child must be reaped.
        assert proc.returncode is not None

    @pytest.mark.asyncio
    async def test_cleanup_kills_long_runner(self, child_script: Path) -> None:
        async with ProcessGroupScope("test-2", grace_seconds=0.5) as pg:
            proc = await pg.spawn(
                [sys.executable, str(child_script)],
                stdout=PIPE,
                stderr=PIPE,
            )
            # Don't wait — just exit the scope.
        # After exit, the process must be dead.
        assert proc.returncode is not None
        # Either signal-induced (negative returncode) or shell-style
        # 128+sig (rare for python). Either way, it shouldn't be 0.
        assert proc.returncode != 0

    @pytest.mark.asyncio
    async def test_communicate_writes_stdin_and_closes(self, tmp_path: Path) -> None:
        script = tmp_path / "echo.py"
        script.write_text(
            textwrap.dedent(
                """
                import sys
                sys.stdout.write(sys.stdin.read())
                """
            ).strip()
        )
        async with ProcessGroupScope("test-3") as pg:
            proc = await pg.spawn(
                [sys.executable, str(script)],
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
            )
            await pg.communicate(proc, b"hello\n")
            rc = await proc.wait()
            assert rc == 0
            assert proc.stdout is not None
            content = await proc.stdout.receive(64)
            assert content == b"hello\n"

    @pytest.mark.asyncio
    async def test_already_dead_child_is_handled(self, tmp_path: Path) -> None:
        """If the child exits before scope exit, cleanup should not raise."""
        script = tmp_path / "exit_fast.py"
        script.write_text("import sys; sys.exit(0)")
        async with ProcessGroupScope("test-4") as pg:
            proc = await pg.spawn(
                [sys.executable, str(script)],
                stdout=PIPE,
                stderr=PIPE,
            )
            rc = await proc.wait()
            assert rc == 0
        # Scope exit cleanup runs; killpg on a reaped pgid raises ProcessLookupError;
        # the scope swallows it. No assertion needed beyond "no exception".
