"""Tests for harness_mcp.mcp_capture — config probe + file fallback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness_mcp.mcp_capture import (
    capture_from_mcp_status,
    capture_mcp_servers,
    parse_user_config_files,
    redact_for_log,
)


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient.get_mcp_status()."""

    def __init__(self, status_response: dict[str, Any]) -> None:
        self._status = status_response

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_mcp_status(self) -> dict[str, Any]:
        return self._status


class TestCaptureFromMcpStatus:
    def test_uses_inline_config_when_present(self) -> None:
        status = {
            "mcpServers": [
                {"name": "context7", "status": "connected", "config": {"command": "ctx7"}},
            ]
        }
        captured = capture_from_mcp_status(status, want=("context7",))
        assert captured == {"context7": {"command": "ctx7"}}

    def test_skips_disconnected(self) -> None:
        status = {
            "mcpServers": [
                {"name": "context7", "status": "disconnected", "config": {"command": "ctx7"}},
            ]
        }
        captured = capture_from_mcp_status(status, want=("context7",))
        assert captured == {}

    def test_skips_when_config_missing(self) -> None:
        status = {
            "mcpServers": [
                {"name": "context7", "status": "connected"},  # no `config` field
            ]
        }
        captured = capture_from_mcp_status(status, want=("context7",))
        assert captured == {}


class TestParseUserConfigFiles:
    def test_finds_in_user_claude_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path
        monkeypatch.setenv("HOME", str(home))
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "context7": {"command": "ctx7", "env": {"K": "V"}},
                    }
                }
            )
        )
        captured = parse_user_config_files(("context7",), project_root=tmp_path)
        assert captured["context7"] == {"command": "ctx7", "env": {"K": "V"}}

    def test_finds_in_project_mcp_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # User config absent; project config present.
        monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
        (tmp_path / "empty_home").mkdir()
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"playwright": {"url": "http://x"}}})
        )
        captured = parse_user_config_files(("playwright",), project_root=project)
        assert captured["playwright"] == {"url": "http://x"}

    def test_user_takes_precedence_over_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"context7": {"command": "from-home"}}})
        )
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"context7": {"command": "from-project"}}})
        )
        captured = parse_user_config_files(("context7",), project_root=project)
        assert captured["context7"] == {"command": "from-home"}

    def test_returns_empty_when_no_files_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "empty_home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        captured = parse_user_config_files(("context7",), project_root=tmp_path)
        assert captured == {}

    def test_finds_in_user_claude_json_projects_subsection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §10.1 step 5: also check ~/.claude.json's projects.<cwd>.mcpServers."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        project = tmp_path / "myproj"
        project.mkdir()
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "projects": {
                        str(project): {
                            "mcpServers": {"context7": {"command": "ctx7-projects-scoped"}}
                        }
                    }
                }
            )
        )
        captured = parse_user_config_files(("context7",), project_root=project)
        assert captured["context7"] == {"command": "ctx7-projects-scoped"}

    def test_top_level_mcp_servers_wins_over_projects_subsection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        project = tmp_path / "myproj"
        project.mkdir()
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {"context7": {"command": "from-top"}},
                    "projects": {
                        str(project): {"mcpServers": {"context7": {"command": "from-proj"}}}
                    },
                }
            )
        )
        captured = parse_user_config_files(("context7",), project_root=project)
        assert captured["context7"] == {"command": "from-top"}

    def test_finds_in_plugin_cache_plugin_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §10.1 step 5: ~/.claude/plugins/cache/**/.claude-plugin/plugin.json."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        plugin_dir = home / ".claude" / "plugins" / "cache" / "fake-plug" / ".claude-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"mcpServers": {"playwright": {"url": "http://plug"}}})
        )
        captured = parse_user_config_files(("playwright",), project_root=None)
        assert captured["playwright"] == {"url": "http://plug"}


class TestCaptureMcpServers:
    @pytest.mark.asyncio
    async def test_uses_client_status_when_inline_config(self) -> None:
        client = _FakeClient(
            status_response={
                "mcpServers": [
                    {"name": "context7", "status": "connected", "config": {"command": "ctx7"}}
                ]
            }
        )
        captured = await capture_mcp_servers(client, want=("context7",), project_root=None)
        assert captured == {"context7": {"command": "ctx7"}}

    @pytest.mark.asyncio
    async def test_falls_back_to_files_when_inline_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path
        monkeypatch.setenv("HOME", str(home))
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"context7": {"command": "from-file"}}})
        )
        client = _FakeClient(
            status_response={
                "mcpServers": [{"name": "context7", "status": "connected"}]  # no config
            }
        )
        captured = await capture_mcp_servers(client, want=("context7",), project_root=tmp_path)
        assert captured == {"context7": {"command": "from-file"}}

    @pytest.mark.asyncio
    async def test_skips_disconnected_servers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        client = _FakeClient(
            status_response={"mcpServers": [{"name": "playwright", "status": "disconnected"}]}
        )
        captured = await capture_mcp_servers(client, want=("playwright",), project_root=None)
        assert captured == {}


class TestRedactForLog:
    def test_redacts_capture_dict(self) -> None:
        captured = {"context7": {"command": "ctx7", "env": {"API_KEY": "secret"}}}
        out = redact_for_log(captured)
        # Output should preserve names + statuses but not env/keys.
        assert "context7" in out
        assert "secret" not in out
        assert "API_KEY" not in out
