"""harness-mcp CLI: `serve` and `doctor` subcommands."""

from __future__ import annotations

import argparse
import sys

import anyio


def _run_serve(args: argparse.Namespace) -> int:
    """Boot the FastMCP server with the chosen transport.

    FastMCP exposes async transport methods as `run_stdio_async()` and
    `run_streamable_http_async()` (plus `run_sse_async()` if needed) — the
    bare `run_stdio` / `run_streamable_http` names are NOT awaitable. Verify
    the exact method names against the pinned `mcp` version before lock-in;
    if the SDK rename happens, update both call sites here.
    """
    from harness_mcp.server import server  # noqa: PLC0415

    transport = args.transport
    if transport == "stdio":
        anyio.run(server.run_stdio_async)
    elif transport == "streamable-http":
        anyio.run(lambda: server.run_streamable_http_async(host=args.host, port=args.port))
    else:
        print(f"unknown transport: {transport}", file=sys.stderr)
        return 1
    return 0


def _run_doctor(_args: argparse.Namespace) -> int:
    """Run lifespan prereqs, print a human report."""
    from harness_mcp.prereqs import (  # noqa: PLC0415
        DoctorReport,
        PrereqFailedError,
        format_doctor_report,
        run_prereqs,
    )
    from harness_mcp.server import _client_factory  # noqa: PLC0415

    report = DoctorReport()

    async def _run() -> None:
        try:
            await run_prereqs(client_factory=_client_factory, project_root=None, report=report)
        except PrereqFailedError as e:
            report.add("FAILED", "FAIL", str(e))
            raise

    try:
        anyio.run(_run)
    except PrereqFailedError:
        print(format_doctor_report(report))
        return 1
    print(format_doctor_report(report))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="harness-mcp")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the MCP server")
    serve.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=_run_serve)

    doctor = sub.add_parser("doctor", help="Run lifespan prereq checks and exit")
    doctor.set_defaults(func=_run_doctor)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
