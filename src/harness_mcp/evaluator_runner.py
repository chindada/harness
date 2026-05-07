"""Evaluator launcher subprocess: drives ClaudeSDKClient inside its own pgroup.

Invoked via `python -m harness_mcp.evaluator_runner`. Reads a JSON payload
from stdin (schema in spec §8.4), runs static + dynamic queries, writes
eval.md, exits 0 on success or 1 on internal error.

Module-level imports:
  * ALLOWED: harness_mcp.types, harness_mcp.config, harness_mcp.evaluator,
    harness_mcp.prompts_loader, claude_agent_sdk, anyio, stdlib.
  * FORBIDDEN: harness_mcp.state (would race orchestrator's writer
    connection). Enforced by tests/test_evaluator_runner.py::TestImportIsolation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import anyio
from anyio import to_thread
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from harness_mcp.config import jobs_root  # safe — pure path helper, no DB
from harness_mcp.evaluator import (
    dynamic_verification_prompt,
    parse_eval_md,
    pipe_claude_msg_to_log,
    static_audit_prompt,
    sync_eval_md,
)
from harness_mcp.prompts_loader import _resolved_prompt_text

REQUIRED_PATH_KEYS = ("design", "plan", "contract", "eval", "app", "log")


def _validate_payload_paths(payload: dict[str, Any]) -> None:
    """Refuse payloads whose paths leave the job directory.

    Each path under `payload["paths"]` must resolve to a location inside
    `<jobs_root>/<payload["job_id"]>/`. This is the launcher's only
    sandbox primitive — without it, a malformed payload could direct the
    Evaluator to write outside ~/.harness.
    """
    job_id = payload.get("job_id")
    paths = payload.get("paths") or {}
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("payload missing string job_id")
    for key in REQUIRED_PATH_KEYS:
        if key not in paths:
            raise ValueError(f"payload.paths missing key {key!r}")

    job_root = (jobs_root() / job_id).resolve()
    for key, raw in paths.items():
        if not isinstance(raw, str):
            raise ValueError(f"payload.paths[{key!r}] must be a string")
        # The path may not exist yet (eval.md gets written by us), so we resolve
        # parents instead. Using is_relative_to() works for not-yet-existing files
        # because it operates on lexical path components.
        try:
            target = Path(raw).resolve(strict=False)
        except (OSError, RuntimeError) as e:
            raise ValueError(f"payload.paths[{key!r}] could not be resolved: {e}") from e
        if not (target == job_root or job_root in target.parents):
            raise ValueError(f"payload.paths[{key!r}]={raw} is outside job dir {job_root}")


async def _run(payload: dict[str, Any]) -> int:
    """Drive ClaudeSDKClient through static + dynamic queries."""
    paths = payload["paths"]
    job_dir = Path(paths["eval"]).parent.parent  # eval lives at <job>/sprint-N/eval.md
    sprint_seq = payload["sprint_seq"]
    log_path = Path(paths["log"])
    eval_path = Path(paths["eval"])
    contract_path = Path(paths["contract"])
    setting_sources = payload.get("setting_sources", ["user"])
    captured_mcp = payload.get("captured_mcp_stanzas", {})
    max_eval_seconds = int(payload.get("max_evaluation_seconds", 1800))

    contract_is_file = await to_thread.run_sync(contract_path.is_file)
    if contract_is_file:
        contract_text = await to_thread.run_sync(contract_path.read_text, "utf-8")
    else:
        contract_text = ""
    prior_tag = payload.get("prior_tag")  # str | None

    options = ClaudeAgentOptions(
        system_prompt=_resolved_prompt_text("evaluator.md"),
        cwd=str(job_dir),
        setting_sources=cast(Any, setting_sources),
        mcp_servers=cast(Any, {name: dict(stanza) for name, stanza in captured_mcp.items()}),
        extra_args={"strict-mcp-config": None},
        permission_mode="bypassPermissions",
    )

    with anyio.fail_after(max_eval_seconds):
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                static_audit_prompt(
                    job_dir=job_dir,
                    sprint_seq=sprint_seq,
                    prior_tag=prior_tag,
                    criteria_text=contract_text,
                )
            )
            async for msg in client.receive_response():
                await pipe_claude_msg_to_log(msg, log_path)
            await sync_eval_md(eval_path, expect_section="## Static audit")

            # Spec §4.4:280 — emit a phase marker on stderr so the orchestrator
            # can flip current_phase to `sprint-<N>/eval-dynamic` between the
            # two queries. Stderr (not stdout) keeps the marker out of any
            # piped log capture.
            print("PHASE:eval-dynamic", file=sys.stderr, flush=True)

            await client.query(
                dynamic_verification_prompt(
                    job_dir=job_dir,
                    sprint_seq=sprint_seq,
                    criteria_text=contract_text,
                )
            )
            async for msg in client.receive_response():
                await pipe_claude_msg_to_log(msg, log_path)
            await sync_eval_md(eval_path, expect_section="## Dynamic verification")

    # Sanity: parse_eval_md confirms structural validity before returning.
    parse_eval_md(eval_path, sprint_seq=sprint_seq)
    return 0


def main() -> int:
    """Entry point for `python -m harness_mcp.evaluator_runner`."""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"evaluator_runner: bad JSON on stdin: {e}", file=sys.stderr)
        return 1

    try:
        _validate_payload_paths(payload)
    except ValueError as e:
        print(f"evaluator_runner: invalid payload: {e}", file=sys.stderr)
        return 1

    try:
        return anyio.run(_run, payload)
    except Exception as e:
        print(f"evaluator_runner: failed: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
