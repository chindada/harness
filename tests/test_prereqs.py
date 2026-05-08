"""Tests for harness_mcp.prereqs - lifespan startup checks."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from harness_mcp import prereqs as _prereqs_module
from harness_mcp.prereqs import (
    DoctorReport,
    PrereqFailedError,
    assert_strict_mcp_config_works,
    check_claude_binary,
    check_codex_binary,
    check_env,
    check_paths_and_db,
    format_doctor_report,
    probe_codex_sdk_shape,
    probe_mcp_servers,
    probe_skill,
    sweep_at_startup,
)
from harness_mcp.state import close_db, db_write, init_db


class TestCheckPathsAndDb:
    @pytest.mark.asyncio
    async def test_creates_harness_home_and_jobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "h"))
        result = await check_paths_and_db()
        assert (tmp_path / "h").is_dir()
        assert (tmp_path / "h" / "jobs").is_dir()
        assert (tmp_path / "h" / "state.db").is_file()
        assert result.startswith("OK")


class TestCheckEnv:
    def test_returns_ok_when_anthropic_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
        status, msg = check_env()
        assert status == "OK"
        assert "ANTHROPIC_API_KEY" in msg

    def test_returns_warn_when_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        status, msg = check_env()
        assert status == "WARN"
        assert "Claude Code CLI auth" in msg

    def test_returns_warn_when_key_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        status, msg = check_env()
        assert status == "WARN"
        assert "Claude Code CLI auth" in msg


class TestCheckCodexBinary:
    def test_uses_harness_codex_bin_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Create a fake codex binary that prints a version.
        fake_bin = tmp_path / "fake_codex.sh"
        fake_bin.write_text("#!/bin/sh\necho 'codex 0.42.0'\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("HARNESS_CODEX_BIN", str(fake_bin))

        msg = check_codex_binary()
        assert "0.42.0" in msg or msg.startswith("OK")

    def test_fails_when_binary_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_CODEX_BIN", "/nonexistent/codex")
        monkeypatch.delenv("PATH", raising=False)
        with pytest.raises(PrereqFailedError):
            check_codex_binary()

    def test_fails_when_version_returns_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.sh"
        bad.write_text("#!/bin/sh\nexit 1\n")
        bad.chmod(0o755)
        monkeypatch.setenv("HARNESS_CODEX_BIN", str(bad))
        with pytest.raises(PrereqFailedError):
            check_codex_binary()

    def test_warns_when_config_toml_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Spec §10.1.2a:1125 — `~/.codex/config.toml` is read warn-only;
        missing file appends a warning to the success message but doesn't
        fail the check."""
        # Point HOME at an empty tmp dir so ~/.codex/config.toml is absent.
        monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
        # Fake a working codex binary.
        fake_bin = tmp_path / "fake_codex.sh"
        fake_bin.write_text("#!/bin/sh\necho 'codex 0.1.0'\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("HARNESS_CODEX_BIN", str(fake_bin))

        msg = check_codex_binary()
        assert msg.startswith("OK codex:")
        assert "config.toml" in msg
        assert "warning" in msg.lower()

    def test_no_warning_when_config_toml_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When ~/.codex/config.toml exists, the success message has no warning."""
        home = tmp_path / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text('model = "gpt-4o"\n')
        monkeypatch.setenv("HOME", str(home))

        fake_bin = tmp_path / "fake_codex.sh"
        fake_bin.write_text("#!/bin/sh\necho 'codex 0.1.0'\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("HARNESS_CODEX_BIN", str(fake_bin))

        msg = check_codex_binary()
        assert msg.startswith("OK codex:")
        assert "warning" not in msg.lower()


class TestCheckClaudeBinary:
    def test_uses_harness_claude_bin_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_bin = tmp_path / "fake_claude.sh"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("HARNESS_CLAUDE_BIN", str(fake_bin))

        msg = check_claude_binary()
        assert msg.startswith("OK")
        assert str(fake_bin) in msg

    def test_uses_path_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("HARNESS_CLAUDE_BIN", raising=False)
        fake_bin = tmp_path / "claude"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))

        msg = check_claude_binary()
        assert msg.startswith("OK")
        assert str(fake_bin) in msg

    def test_fails_when_env_points_at_nonexistent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_CLAUDE_BIN", "/nonexistent/claude")
        monkeypatch.setenv("PATH", "")
        with pytest.raises(PrereqFailedError):
            check_claude_binary()

    def test_fails_when_neither_env_nor_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HARNESS_CLAUDE_BIN", raising=False)
        monkeypatch.setenv("PATH", "")
        with pytest.raises(PrereqFailedError):
            check_claude_binary()


class TestSweepAtStartup:
    @pytest.mark.asyncio
    async def test_running_jobs_flipped_to_interrupted(self, tmp_harness_home: Path) -> None:
        # Reset the module-global writer in case an earlier test left it open
        # against a different HARNESS_HOME.
        close_db()
        init_db()
        await db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("J1", "running", "planning", "/tmp/x", "{}", 1, 1),
        )
        await sweep_at_startup()
        conn = sqlite3.connect(str(tmp_harness_home / "state.db"))
        try:
            row = conn.execute("SELECT status FROM jobs WHERE id='J1'").fetchone()
            assert row[0] == "interrupted"
        finally:
            conn.close()
        close_db()


class TestDoctorReport:
    def test_formats_passes_warns_and_fails(self) -> None:
        report = DoctorReport()
        report.add("paths", "OK", "~/.harness exists; state.db at ~/.harness/state.db")
        report.add("env", "WARN", "ANTHROPIC_API_KEY not set; relying on Claude Code CLI auth")
        report.add("codex", "FAIL", "binary not found")
        out = format_doctor_report(report)
        assert "OK   paths" in out
        assert "WARN env" in out
        assert "FAIL codex" in out


class TestProbeCodexSdkShape:
    @pytest.mark.asyncio
    async def test_finds_first_working_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HARNESS_CODEX_BIN", "/usr/bin/echo")  # value irrelevant
        # Mock AppServerConfig and AsyncCodex.
        attempts: list[tuple[str, ...]] = []

        @asynccontextmanager
        async def fake_codex(config: Any) -> AsyncIterator[Any]:
            attempts.append(config["overrides"])

            class _Thread:
                async def turn(self, _input: object) -> object:
                    # Side-effect: write probe.txt only if the override form is the "good" one.
                    if config["overrides"] == (
                        "sandbox_mode=workspace-write",
                        "approval_policy=never",
                    ):
                        (Path(config["cwd"]) / "probe.txt").write_text("ok")

                    class _T:
                        async def stream(self) -> AsyncIterator[Any]:
                            if False:
                                yield None

                    return _T()

            class _Wrap:
                async def thread_start(self) -> _Thread:
                    return _Thread()

            yield _Wrap()

        # Each entry is (config_overrides, fake_class). Good form is index 0.
        p = _prereqs_module

        def fake_app_server_config(
            *,
            codex_bin: str,
            cwd: str,
            config_overrides: tuple[str, ...],
            **kw: Any,
        ) -> dict[str, Any]:
            return {"codex_bin": codex_bin, "cwd": cwd, "overrides": config_overrides, **kw}

        class FakeTextInput:
            def __init__(self, prompt: str) -> None:
                self.prompt = prompt

        monkeypatch.setattr(p, "_AppServerConfig", fake_app_server_config)
        monkeypatch.setattr(p, "_AsyncCodex", fake_codex)
        monkeypatch.setattr(p, "_TextInput", FakeTextInput)

        msg, accepted = await probe_codex_sdk_shape()
        # First form is the good one; only one attempt should be needed.
        assert accepted == ("sandbox_mode=workspace-write", "approval_policy=never")
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_falls_back_to_camelcase_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Configure the fake so only the alias-camelcase form succeeds.
        p = _prereqs_module

        monkeypatch.setenv("HARNESS_CODEX_BIN", "/usr/bin/echo")

        def fake_app_server_config(
            *,
            codex_bin: str,
            cwd: str,
            config_overrides: tuple[str, ...],
            **kw: Any,
        ) -> dict[str, Any]:
            return {"cwd": cwd, "overrides": config_overrides}

        @asynccontextmanager
        async def fake_codex(config: Any) -> AsyncIterator[Any]:
            class _T:
                async def turn(self, _input: object) -> object:
                    if config["overrides"] == (
                        "sandbox=workspaceWrite",
                        "approval_policy=never",
                    ):
                        (Path(config["cwd"]) / "probe.txt").write_text("ok")

                    class _S:
                        async def stream(self) -> AsyncIterator[Any]:
                            if False:
                                yield None

                    return _S()

            class _W:
                async def thread_start(self) -> _T:
                    return _T()

            yield _W()

        class _TI:
            def __init__(self, x: str) -> None:
                pass

        monkeypatch.setattr(p, "_AppServerConfig", fake_app_server_config)
        monkeypatch.setattr(p, "_AsyncCodex", fake_codex)
        monkeypatch.setattr(p, "_TextInput", _TI)

        msg, accepted = await probe_codex_sdk_shape()
        _ = msg
        assert accepted == ("sandbox=workspaceWrite", "approval_policy=never")

    @pytest.mark.asyncio
    async def test_fails_when_no_form_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _prereqs_module

        monkeypatch.setenv("HARNESS_CODEX_BIN", "/usr/bin/echo")

        @asynccontextmanager
        async def fake_codex(config: Any) -> AsyncIterator[Any]:
            _ = config

            class _T:
                async def turn(self, _x: object) -> object:
                    # Never writes probe.txt - silent ignore.
                    class _S:
                        async def stream(self) -> AsyncIterator[Any]:
                            if False:
                                yield None

                    return _S()

            class _W:
                async def thread_start(self) -> _T:
                    return _T()

            yield _W()

        monkeypatch.setattr(
            p,
            "_AppServerConfig",
            lambda **kw: {"cwd": kw["cwd"], "overrides": kw["config_overrides"]},
        )
        monkeypatch.setattr(p, "_AsyncCodex", fake_codex)

        class _TI:
            def __init__(self, x: str) -> None:
                pass

        monkeypatch.setattr(p, "_TextInput", _TI)

        with pytest.raises(PrereqFailedError):
            await probe_codex_sdk_shape()


class _FakeClient:
    def __init__(self, *, server_info: dict[str, Any], mcp_status: dict[str, Any]) -> None:
        self._server_info = server_info
        self._mcp_status = mcp_status

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[Any]:
        if False:
            yield None

    async def get_server_info(self) -> dict[str, Any]:
        return self._server_info

    async def get_mcp_status(self) -> dict[str, Any]:
        return self._mcp_status


class TestProbeSkill:
    @pytest.mark.asyncio
    async def test_finds_writing_plans_in_commands(self) -> None:
        client = _FakeClient(
            server_info={"commands": ["superpowers:writing-plans", "code-review:code-review"]},
            mcp_status={},
        )
        msg, sources = await probe_skill(client_factory=lambda **_kw: client)
        assert sources == ["user"]
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_finds_skill_via_bare_name_and_plugin_tag_in_description(self) -> None:
        # Realistic format the live SDK returns for superpowers commands:
        # bare skill name + plugin identified in description as "(plugin-name)".
        client = _FakeClient(
            server_info={
                "commands": [
                    {
                        "name": "writing-plans",
                        "description": (
                            "(superpowers) Use when you have a spec or requirements "
                            "for a multi-step task, before touching code"
                        ),
                    },
                    {"name": "writing-skills", "description": "(superpowers) ..."},
                ]
            },
            mcp_status={},
        )
        msg, sources = await probe_skill(client_factory=lambda **_kw: client)
        assert sources == ["user"]
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_finds_skill_via_prefixed_name_dict(self) -> None:
        # Some plugins do report prefixed names (e.g., code-review:code-review).
        client = _FakeClient(
            server_info={
                "commands": [
                    {"name": "code-review:code-review", "description": "(code-review) ..."},
                ]
            },
            mcp_status={},
        )
        msg, sources = await probe_skill(
            client_factory=lambda **_kw: client,
            skill_name="code-review:code-review",
        )
        assert sources == ["user"]
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_does_not_match_bare_name_with_wrong_plugin_tag(self) -> None:
        # A plugin named "other" exposes a "writing-plans" skill — must not
        # match a probe targeting "superpowers:writing-plans".
        client = _FakeClient(
            server_info={
                "commands": [
                    {"name": "writing-plans", "description": "(other) Different plugin's skill"},
                ]
            },
            mcp_status={},
        )
        with pytest.raises(PrereqFailedError):
            await probe_skill(client_factory=lambda **_kw: client)

    @pytest.mark.asyncio
    async def test_fails_when_skill_absent(self) -> None:
        client = _FakeClient(server_info={"commands": []}, mcp_status={})
        with pytest.raises(PrereqFailedError):
            await probe_skill(client_factory=lambda **_kw: client)


class TestProbeMcpServers:
    @pytest.mark.asyncio
    async def test_captures_context7_via_inline_config(self) -> None:
        client = _FakeClient(
            server_info={},
            mcp_status={
                "mcpServers": [
                    {"name": "context7", "status": "connected", "config": {"command": "ctx7"}},
                ]
            },
        )
        msg, captured = await probe_mcp_servers(
            client_factory=lambda **_kw: client, project_root=None
        )
        assert "context7" in captured
        assert captured["context7"] == {"command": "ctx7"}
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_fails_when_context7_disconnected_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        client = _FakeClient(
            server_info={},
            mcp_status={
                "mcpServers": [
                    {"name": "context7", "status": "disconnected"},
                ]
            },
        )
        with pytest.raises(PrereqFailedError):
            await probe_mcp_servers(client_factory=lambda **_kw: client, project_root=tmp_path)

    @pytest.mark.asyncio
    async def test_playwright_soft_warns_when_missing(self) -> None:
        client = _FakeClient(
            server_info={},
            mcp_status={
                "mcpServers": [
                    {"name": "context7", "status": "connected", "config": {"command": "ctx7"}},
                ]
            },
        )
        msg, captured = await probe_mcp_servers(
            client_factory=lambda **_kw: client, project_root=None
        )
        _ = msg
        assert "playwright" not in captured
        # No exception raised - playwright is soft.


class TestAssertStrictMcpConfigWorks:
    @pytest.mark.asyncio
    async def test_passes_when_strict_mcp_config_returns_one_server(self) -> None:
        client = _FakeClient(
            server_info={},
            mcp_status={"mcpServers": [{"name": "context7", "status": "connected"}]},
        )
        msg = await assert_strict_mcp_config_works(
            client_factory=lambda **_kw: client,
            captured={"context7": {"command": "ctx7"}},
            setting_sources=["user"],
        )
        assert msg.startswith("OK")

    @pytest.mark.asyncio
    async def test_fails_when_extra_servers_leak(self) -> None:
        client = _FakeClient(
            server_info={},
            mcp_status={
                "mcpServers": [
                    {"name": "context7", "status": "connected"},
                    {"name": "playwright", "status": "connected"},
                ]
            },
        )
        with pytest.raises(PrereqFailedError):
            await assert_strict_mcp_config_works(
                client_factory=lambda **_kw: client,
                captured={"context7": {"command": "ctx7"}},
                setting_sources=["user"],
            )
