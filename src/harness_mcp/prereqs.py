"""Lifespan startup checks (`harness-mcp serve` and `harness-mcp doctor`).

Each check returns a one-line status string on pass or raises
`PrereqFailedError(message)` on fail. The orchestrator (Plan 5) wires
them into the FastMCP lifespan; the `doctor` subcommand runs the same
checks but pretty-prints them and exits non-zero on first failure.

Async vs. sync split:
  * Synchronous: env var, Codex binary `--version`, doctor report.
  * Async (uses anyio): paths + DB init, restart sweep, Codex SDK shape
    probe, skill probe, MCP probe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import (
    Callable,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codex_app_server import AppServerConfig as _AppServerConfig
from codex_app_server import AsyncCodex as _AsyncCodex
from codex_app_server import TextInput as _TextInput

from harness_mcp import __version__
from harness_mcp.config import harness_home, jobs_root, state_db_path
from harness_mcp.mcp_capture import parse_user_config_files
from harness_mcp.state import init_db, sweep_running_to_interrupted


class PrereqFailedError(RuntimeError):
    """Raised by any prereq check on failure."""


@dataclass
class DoctorReport:
    """Accumulator for `harness-mcp doctor` output."""

    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.rows.append((name, status, detail))


def format_doctor_report(report: DoctorReport) -> str:
    lines = []
    for name, status, detail in report.rows:
        if status == "OK":
            marker = "OK  "
        elif status == "WARN":
            marker = "WARN"
        else:
            marker = "FAIL"
        lines.append(f"{marker} {name}: {detail}")
    return "\n".join(lines)


# ---------- Layer 1: synchronous checks ----------


async def check_paths_and_db() -> str:
    """First lifespan prereq: ensure the harness home tree and state DB exist.

    Design:
        Per spec §10.1 step 1, the server must guarantee `~/.harness/`
        and `~/.harness/jobs/` exist and the SQLite state DB is opened
        with WAL mode and the schema applied. This is the only prereq
        with side effects on the filesystem; everything downstream
        assumes it has run.

    Implementation:
        Resolves `harness_home()` (honoring `$HARNESS_HOME`), creates
        the home and `jobs/` directories (idempotent), and calls
        `init_db()` which is itself idempotent (`CREATE TABLE IF NOT
        EXISTS`). Returns a one-line OK summary that the doctor CLI
        prints to the user.

    Example:
        >>> await check_paths_and_db()
        'OK paths: home=/.../.harness; state_db=/.../.harness/state.db'
    """
    home = harness_home()
    home.mkdir(parents=True, exist_ok=True)
    jobs_root().mkdir(exist_ok=True)
    init_db()  # idempotent
    return f"OK paths: home={home}; state_db={state_db_path()}"


def check_env() -> tuple[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return "OK", "env: ANTHROPIC_API_KEY is set"
    return (
        "WARN",
        "env: ANTHROPIC_API_KEY not set; relying on Claude Code CLI auth "
        "(keychain or ~/.claude/.credentials.json). Verify with 'claude auth status'.",
    )


def check_codex_binary() -> str:
    """Resolve $HARNESS_CODEX_BIN or `which codex`, run --version.

    Also reads `~/.codex/config.toml` (warn-only if missing) per spec
    §10.1 step 2a — codex falls back to defaults, and we still force
    `sandbox=workspace-write` and `approval_policy=never` per §10.5.
    """
    bin_path = os.environ.get("HARNESS_CODEX_BIN") or shutil.which("codex")
    if not bin_path:
        raise PrereqFailedError(
            "Codex binary not found: set HARNESS_CODEX_BIN or add codex to PATH"
        )
    if not Path(bin_path).is_file() and shutil.which(bin_path) is None:
        raise PrereqFailedError(f"Codex binary {bin_path!r} does not exist")
    try:
        proc = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise PrereqFailedError("Codex binary --version timed out") from e
    if proc.returncode != 0:
        raise PrereqFailedError(
            f"Codex binary --version exited {proc.returncode}: {proc.stderr.strip()}"
        )

    config_note = ""
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.is_file():
        config_note = (
            f" (warning: {config_path} not found; codex will use defaults — "
            "sandbox/approval are forced regardless)"
        )

    return f"OK codex: {proc.stdout.strip() or '(no version output)'}{config_note}"


def check_claude_binary() -> str:
    """Resolve $HARNESS_CLAUDE_BIN or `which claude`. Validate it's a real file.

    Multi-account users set HARNESS_CLAUDE_BIN to pin which `claude` install
    the harness uses, independent of PATH or shell aliases. Skips a `--version`
    invocation (unlike check_codex_binary) because `claude --version` can be slow
    and may surface auth prompts; existence + executability is the right level
    of check, matching what the SDK validates internally before spawning.
    """
    bin_path = os.environ.get("HARNESS_CLAUDE_BIN") or shutil.which("claude")
    if not bin_path:
        raise PrereqFailedError(
            "Claude Code CLI not found: set HARNESS_CLAUDE_BIN or add claude to PATH"
        )
    if not Path(bin_path).is_file() and shutil.which(bin_path) is None:
        raise PrereqFailedError(f"Claude Code CLI {bin_path!r} does not exist")
    return f"OK claude: {bin_path}"


async def sweep_at_startup() -> str:
    """Mark any leftover `running` jobs as `interrupted`."""
    await sweep_running_to_interrupted()
    return "OK sweep: prior `running` jobs flipped to `interrupted`"


# ---------- Codex SDK shape probe ----------


_OVERRIDE_FORMS: tuple[tuple[str, ...], ...] = (
    ("sandbox_mode=workspace-write", "approval_policy=never"),  # TOML field, hyphenated value
    ("sandbox_mode=workspaceWrite", "approval_policy=never"),  # TOML field, camelCase value
    ("sandbox=workspace-write", "approval_policy=never"),  # alias key, hyphenated value
    ("sandbox=workspaceWrite", "approval_policy=never"),  # alias key, camelCase value
)


async def probe_codex_sdk_shape() -> tuple[str, tuple[str, ...]]:
    """Verify the Codex SDK install + sandbox override semantics.

    Tries each override form in `_OVERRIDE_FORMS`. For each, opens
    `AsyncCodex(config=cfg)`, calls `thread.turn(TextInput("write probe.txt..."))`,
    drains the stream, then checks whether `probe.txt` actually got written.
    Returns the first form that works. If none does, raises PrereqFailedError.
    """
    bin_path = os.environ.get("HARNESS_CODEX_BIN") or shutil.which("codex")
    if not bin_path:
        raise PrereqFailedError("HARNESS_CODEX_BIN / `codex` on PATH is required for the probe")

    last_error: str = ""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # The Python SDK requires a real git repo (no `skip_git_repo_check` flag).
        # The git CLI is fast and synchronous; running it under the event loop
        # at startup is acceptable. ASYNC221 silenced for the same reason.
        try:
            subprocess.run(["git", "init", "-q"], cwd=str(tmp_dir), check=True)  # noqa: ASYNC221
            subprocess.run(  # noqa: ASYNC221
                ["git", "config", "user.email", "probe@harness"],
                cwd=str(tmp_dir),
                check=True,
            )
            subprocess.run(  # noqa: ASYNC221
                ["git", "config", "user.name", "Probe"],
                cwd=str(tmp_dir),
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise PrereqFailedError(f"git init failed during probe: {e}") from e

        for form in _OVERRIDE_FORMS:
            (tmp_dir / "probe.txt").unlink(missing_ok=True)
            cfg = _AppServerConfig(
                codex_bin=bin_path,
                cwd=str(tmp_dir),
                config_overrides=form,
                client_name="harness-mcp",
                client_title="Harness Probe",
                client_version=__version__,
            )
            saw_tool_call = False
            try:
                async with _AsyncCodex(config=cfg) as codex:
                    thread = await codex.thread_start()
                    # Imperative shell-command prompt — natural-language "write a
                    # file" doesn't reliably trigger tool use across codex backends
                    # (e.g., gpt-5-3-codex via Azure text-replies "Done" without
                    # invoking any tool). Explicit "Run this shell command"
                    # consistently triggers the shell tool on every backend tested.
                    turn = await thread.turn(
                        _TextInput("Run this shell command exactly: echo ok > probe.txt")
                    )
                    async for event in turn.stream():
                        # Track whether the model actually invoked a shell command;
                        # CommandExecutionThreadItem is codex_app_server's marker for
                        # an executed shell tool. Without this, we can't distinguish
                        # "sandbox blocked the write" from "model never called a tool".
                        payload = getattr(event, "payload", None)
                        item = getattr(payload, "item", None)
                        root = getattr(item, "root", None) if item is not None else None
                        if root is not None and type(root).__name__ == "CommandExecutionThreadItem":
                            saw_tool_call = True
            except Exception as e:
                last_error = f"override {form!r} raised {e!r}"
                continue

            if (tmp_dir / "probe.txt").is_file():
                return (f"OK codex-shape: accepted overrides {form}", form)
            if not saw_tool_call:
                last_error = (
                    f"override {form!r} accepted but model invoked no shell command "
                    "(model didn't attempt the write — check codex backend's tool-use "
                    "behavior; e.g., gpt-5-3-codex may text-reply without calling tools)"
                )
            else:
                last_error = (
                    f"override {form!r} accepted, model ran a shell command, "
                    "but probe.txt not written (sandbox blocked the write)"
                )

    raise PrereqFailedError(
        "Codex sandbox override accepted but no form actually permitted writes. "
        f"Last attempt: {last_error}"
    )


# ---------- Skill + MCP probes ----------


async def probe_skill(
    *,
    client_factory: Callable[..., Any],
    skill_name: str = "superpowers:writing-plans",
) -> tuple[str, list[str]]:
    """Verify `superpowers:writing-plans` is installed at user scope.

    Returns (status_message, resolved_setting_sources). resolved_setting_sources
    is recorded as `_resolved_setting_sources` for spawn calls (spec §10.1).

    Matching handles two formats `get_server_info` returns for plugin commands:
      * Prefixed name: ``{"name": "code-review:code-review", ...}``
      * Bare name + plugin tag in description: ``{"name": "writing-plans",
        "description": "(superpowers) ..."}``
    Plus a legacy substring fallback for SDK builds that return strings.
    """
    sources = ["user"]
    client = client_factory(setting_sources=sources)
    async with client as c:
        # Some SDK versions need a no-op query before get_server_info().
        await c.query("ready?")
        async for _ in c.receive_response():
            break  # drain one message; some clients hang otherwise
        info = await c.get_server_info()
    commands = info.get("commands") or []

    plugin_name, _, skill_local = skill_name.partition(":")

    def _matches(cmd: Any) -> bool:  # noqa: ANN401
        if isinstance(cmd, dict):
            name = cmd.get("name", "")
            desc = cmd.get("description", "")
            if name == skill_name:
                return True
            if skill_local and name == skill_local and f"({plugin_name})" in desc:
                return True
        return skill_name in str(cmd)

    if any(_matches(cmd) for cmd in commands):
        return f"OK skill: {skill_name} found at setting_sources={sources}", sources
    raise PrereqFailedError(
        f"skill {skill_name} not available at setting_sources={sources}; "
        "install superpowers plugin at user scope: "
        "https://github.com/anthropics/claude-superpowers"
    )


async def probe_mcp_servers(
    *,
    project_root: Path | None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Capture context7 (hard) + playwright (soft) MCP server stanzas from disk.

    File-based capture only. Does NOT spawn a probe claude. A spawned claude
    would load every user-scope MCP — including harness-mcp itself (recursive
    lifespan) and the playwright plugin (npx + chromium) — cascading into a
    fork bomb at startup. parse_user_config_files reads the same stanzas
    directly from ~/.claude.json, project .mcp.json, and plugin caches, which
    is sufficient for capture; we don't need a live "is it currently connected"
    signal here, only the stanza to forward to spawned agents later.
    """
    want = ("context7", "playwright")
    captured = parse_user_config_files(want, project_root=project_root)

    if "context7" not in captured:
        raise PrereqFailedError(
            "context7 MCP server not configured. "
            "Add a context7 stanza to ~/.claude.json mcpServers."
        )

    msg = f"OK mcp: captured {sorted(captured.keys())}"
    if "playwright" not in captured:
        msg += (
            " (warning: playwright absent - UI sprints will fail "
            "if they reach dynamic verification)"
        )
    return msg, captured


async def assert_strict_mcp_config_works(
    *,
    client_factory: Callable[..., Any],
    captured: dict[str, dict[str, Any]],
    setting_sources: list[str],
) -> str:
    """Verify `extra_args={"strict-mcp-config": None}` actually overrides settings inheritance.

    Boots a probe client with strict-mcp-config + only context7 captured,
    then calls get_mcp_status(). Expect exactly one server. If extra
    servers leak through (e.g., user has more servers in settings), the
    flag isn't enforcing - refuse startup.
    """
    client = client_factory(
        setting_sources=setting_sources,
        mcp_servers={"context7": captured["context7"]},
        extra_args={"strict-mcp-config": None},
    )
    async with client as c:
        await c.query("ready?")
        async for _ in c.receive_response():
            break
        status = await c.get_mcp_status()
    names = {e.get("name") for e in status.get("mcpServers", []) if e.get("name")}
    if names != {"context7"}:
        raise PrereqFailedError(
            "strict-mcp-config flag did not enforce override; SDK behavior unexpected. "
            f"Expected just {{'context7'}}, got {names}. Update the dep or report a bug."
        )
    return "OK strict-mcp-config: enforced"


# ---------- run_prereqs: the complete §10.1 sequence ----------


@dataclass(frozen=True)
class PrereqsResult:
    captured_mcp: dict[str, dict[str, Any]]
    setting_sources: list[str]
    codex_overrides: tuple[str, ...]


async def run_prereqs(
    *,
    client_factory: Callable[..., Any],
    project_root: Path | None,
    report: DoctorReport | None = None,
) -> PrereqsResult:
    """Run the complete startup sequence per spec §10.1.

    Each step's status is added to `report` if provided (used by `harness-mcp doctor`).
    On any failure, raises PrereqFailedError immediately.
    """
    if report is None:
        report = DoctorReport()

    msg = await check_paths_and_db()
    report.add("paths_and_db", "OK", msg)

    status, msg = check_env()
    report.add("env", status, msg)

    msg = check_codex_binary()
    report.add("codex_binary", "OK", msg)

    msg = check_claude_binary()
    report.add("claude_binary", "OK", msg)

    codex_msg, codex_overrides = await probe_codex_sdk_shape()
    report.add("codex_sdk_shape", "OK", codex_msg)

    skill_msg, sources = await probe_skill(client_factory=client_factory)
    report.add("skill", "OK", skill_msg)

    mcp_msg, captured = await probe_mcp_servers(project_root=project_root)
    report.add("mcp", "OK", mcp_msg)

    strict_msg = await assert_strict_mcp_config_works(
        client_factory=client_factory,
        captured=captured,
        setting_sources=sources,
    )
    report.add("strict_mcp_config", "OK", strict_msg)

    msg = await sweep_at_startup()
    report.add("restart_sweep", "OK", msg)

    return PrereqsResult(
        captured_mcp=captured,
        setting_sources=sources,
        codex_overrides=codex_overrides,
    )
