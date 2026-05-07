"""Capture user MCP server stanzas at startup.

Strategy:
  1. Probe the live `ClaudeSDKClient.get_mcp_status()` response. If each
     wanted entry has an inline `config` dict and is `connected`, use it.
  2. Otherwise, parse `~/.claude.json` (user-scope) → `<project>/.mcp.json`
     (project-scope) for the missing names. First hit wins.

Captured stanzas may contain API keys, OAuth tokens, paths to credential
files. We pass them verbatim to spawned agents (required for them to
call the MCP server) but redact them in logs via `redact_for_log()`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def capture_from_mcp_status(
    status: dict[str, Any], *, want: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """Return the subset of `want` whose entries have inline config + are connected.

    `status` is the public-shape `get_mcp_status()` response — a dict with
    `mcpServers: list[{name, status, config?}]`. Any entry missing `config`
    or with non-connected `status` is dropped here; the caller falls back
    to file-based parsing for whatever is missing.
    """
    out: dict[str, dict[str, Any]] = {}
    for entry in status.get("mcpServers", []) or []:
        name = entry.get("name")
        if name not in want:
            continue
        if entry.get("status") != "connected":
            continue
        config = entry.get("config")
        if config:
            out[name] = dict(config)
    return out


def parse_user_config_files(  # noqa: PLR0912, PLR0915 — sequence of distinct lookup sources per spec §10.1 step 5
    want: tuple[str, ...], *, project_root: Path | None = None
) -> dict[str, dict[str, Any]]:
    """Walk known config files for the named servers; return first hits.

    Lookup order (per spec §10.1 step 5):
      1. ~/.claude.json — top-level `mcpServers.<name>`.
      2. ~/.claude.json — `projects.<project_root>.mcpServers.<name>` (if project_root given).
      3. <project_root>/.mcp.json — `mcpServers.<name>` (if project_root given).
      4. ~/.claude/plugins/cache/**/.mcp.json — plugin-shipped MCP configs.
      5. ~/.claude/plugins/cache/**/.claude-plugin/plugin.json — inline `mcpServers.<name>`.
    """
    found: dict[str, dict[str, Any]] = {}
    home = Path(os.environ.get("HOME", str(Path.home())))

    def _ingest(stanza_root: dict[str, Any]) -> None:
        for name in want:
            if name in found:
                continue
            entry = stanza_root.get(name)
            if isinstance(entry, dict):
                found[name] = entry

    user_claude = home / ".claude.json"
    if user_claude.is_file():
        try:
            data = json.loads(user_claude.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        # 1. Top-level mcpServers (user-scope default).
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            _ingest(servers)
        # 2. projects.<project_root>.mcpServers — Claude Code embeds project-scoped
        #    configs here when invoked inside a project tree.
        if project_root is not None:
            projects = data.get("projects") or {}
            if isinstance(projects, dict):
                proj_section = projects.get(str(project_root)) or {}
                if isinstance(proj_section, dict):
                    proj_servers = proj_section.get("mcpServers")
                    if isinstance(proj_servers, dict):
                        _ingest(proj_servers)

    if project_root is not None:
        project_mcp = project_root / ".mcp.json"
        if project_mcp.is_file():
            try:
                data = json.loads(project_mcp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                _ingest(servers)

    plugin_cache = home / ".claude" / "plugins" / "cache"
    if plugin_cache.is_dir():
        # 4. Plugin-shipped .mcp.json files.
        for mcp_json in plugin_cache.rglob(".mcp.json"):
            try:
                data = json.loads(mcp_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                _ingest(servers)
        # 5. Inline mcpServers inside each plugin's plugin.json (rarer, but supported
        #    by Claude Code per current docs).
        for plugin_json in plugin_cache.rglob(".claude-plugin/plugin.json"):
            try:
                data = json.loads(plugin_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                _ingest(servers)

    return found


async def capture_mcp_servers(
    client: Any,  # noqa: ANN401 — duck-typed SDK client (real type: ClaudeSDKClient)
    *,
    want: tuple[str, ...],
    project_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Probe the live SDK MCP status, fall back to user config files for missing names.

    Used by `prereqs.run_prereqs` (Part 4) at lifespan startup. Strategy
    (per spec §10.1 step 5):
      1. `await client.get_mcp_status()` — read inline `config` for every wanted
         entry that is `connected`.
      2. For wanted names where the SDK gave no `config` but reported `connected`,
         consult `parse_user_config_files` to find the stanza on disk.
      3. Return whatever was captured. Missing names are simply absent — the
         caller (probe_mcp_servers in prereqs.py) decides which are hard
         requirements vs. soft.
    """
    async with client as c:
        status = await c.get_mcp_status()

    captured = capture_from_mcp_status(status, want=want)

    # For names that came back `connected` but had no inline config, look in files.
    missing_with_inline: list[str] = []
    for entry in status.get("mcpServers") or []:
        name = entry.get("name")
        if name in want and entry.get("status") == "connected" and name not in captured:
            missing_with_inline.append(name)

    if missing_with_inline:
        captured.update(
            parse_user_config_files(tuple(missing_with_inline), project_root=project_root)
        )
    return captured


def redact_for_log(captured: dict[str, dict[str, Any]]) -> str:
    """Return a stringified summary safe for log lines.

    Includes server names but never env vars, args, or URLs (which can
    embed API keys via query strings). Format: `name=<status>` lines.
    """
    out_lines = []
    for name, stanza in captured.items():
        kind = "stdio" if "command" in stanza else "http" if "url" in stanza else "unknown"
        out_lines.append(f"  {name}=<{kind} config redacted>")
    return (
        "captured MCP servers:\n" + "\n".join(out_lines)
        if out_lines
        else "captured MCP servers: (none)"
    )
