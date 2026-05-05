"""
Concurrent streamable-HTTP stress test for the Serena MCP server.

Verifies the be2ecc7f fix holds under N concurrent clients with active Swift
cursors:

* tool registration is one-shot in ``create_mcp_server``,
* ``server_lifespan`` is a no-op (no per-SSE-disconnect agent teardown),
* ``agent.on_shutdown`` runs only at process exit (atexit).

Concretely asserts:

1. Only **one** ``sourcekit-lsp`` process spawns for the project, regardless
   of how many concurrent HTTP clients open cursors against it (the
   ``LanguageServerManager._language_servers`` cache must deduplicate
   across sessions in the shared agent).
2. Forcing one client to disconnect mid-operation does not break the
   surviving clients (the 48025c7d regression — "No active project" plus
   15-min hangs — must not return).

Targets eight concurrent clients to mirror the user's actual Claude Code
load (~8 sessions × Swift projects).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from solidlsp.ls_config import Language
from test.conftest import get_repo_path, language_tests_enabled


pytestmark = pytest.mark.swift


# parameters of the concurrent stress run
N_CLIENTS = 8
SERVER_BIND_TIMEOUT_S = 30.0
END_TO_END_TIMEOUT_S = 180.0  # sourcekit-lsp index time + 8 client cursor_overview calls


# ---------------------------------------------------------------------------
# Helpers — process bookkeeping + free-port discovery
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """ask the OS for a free localhost TCP port and release it immediately."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _project_sourcekit_processes(scratch_path: Path) -> list[psutil.Process]:
    """all ``sourcekit-lsp`` processes whose command line references ``scratch_path``.

    sourcekit-lsp is invoked with ``--scratch-path <project>/.build/sourcekit-lsp``,
    so filtering on that string identifies the LSP for this specific project
    even when multiple Serena instances are running on the host.
    """
    target = str(scratch_path.resolve())
    found: list[psutil.Process] = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = proc.info["name"] or ""
            cmdline = proc.info["cmdline"] or []
        except psutil.Error:
            continue
        # match either by exe name or by any cmdline part containing 'sourcekit-lsp'
        if "sourcekit-lsp" not in name and not any("sourcekit-lsp" in part for part in cmdline):
            continue
        if any(target in part for part in cmdline):
            found.append(proc)
    return found


def _wait_for_http_server(port: int, deadline: float) -> None:
    """poll the listening socket until the HTTP server is accepting connections."""
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    raise TimeoutError(f"server did not bind to 127.0.0.1:{port}: {last_error!r}")


# ---------------------------------------------------------------------------
# Fixture — spawn ``serena start-mcp-server`` in streamable-http mode
# ---------------------------------------------------------------------------


@pytest.fixture
def serena_http_server(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """start a fresh Serena MCP server in streamable-http mode for the Swift test repo.

    Uses the same CLI invocation Asher will run from ``launchd`` once shared
    Serena is shipped, so the test exercises the production code path rather
    than an in-process shortcut.
    """
    if not language_tests_enabled(Language.SWIFT):
        pytest.skip("Swift tests not enabled (xcrun toolchain unavailable)")

    repo_path = get_repo_path(Language.SWIFT)
    port = _find_free_port()
    log_file = tmp_path / "serena-mcp.log"

    project_root = Path(__file__).resolve().parents[2]
    cmd = [
        "uv",
        "run",
        "serena",
        "start-mcp-server",
        "--project",
        str(repo_path),
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "INFO",
        "--enable-web-dashboard",
        "false",
        "--enable-gui-log-window",
        "false",
    ]
    log_handle = log_file.open("wb")
    proc = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        # bring up the HTTP listener before yielding control to the test
        _wait_for_http_server(port, time.monotonic() + SERVER_BIND_TIMEOUT_S)
        yield {
            "port": port,
            "repo_path": repo_path,
            "log_file": log_file,
            "process": proc,
        }
    finally:
        # SIGTERM triggers the atexit handler => agent.on_shutdown() => LSP child cleaned up
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()
        # belt-and-suspenders: nuke any straggler sourcekit-lsp tied to this scratch dir
        for stray in _project_sourcekit_processes(repo_path / ".build" / "sourcekit-lsp"):
            with contextlib.suppress(psutil.Error):
                stray.kill()


# ---------------------------------------------------------------------------
# Async client workers
# ---------------------------------------------------------------------------


async def _run_overview(url: str, label: str) -> str:
    """open one streamable-HTTP session, run ``cursor_overview`` against the Swift fixture.

    cursor_overview drives the language server's symbol retriever, which forces
    the LSP child to spawn (or reuses the cached one). If the per-session
    lifespan teardown ever returns, the second client through this path will
    fail with "No active project" rather than reach the assert.
    """
    async with streamablehttp_client(url, timeout=60.0, sse_read_timeout=120.0) as (
        read,
        write,
        _get_session_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "cursor_overview",
                {
                    "relative_path": "src/main.swift",
                    "because": (
                        f"concurrent SSE stress test client {label} verifying "
                        "the shared SerenaAgent serves multiple HTTP clients "
                        "against a single sourcekit-lsp child"
                    ),
                },
            )
            if result.isError:
                raise AssertionError(f"client {label} got tool error: {result.content!r}")
            return f"{label}:ok"


async def _run_overview_then_disconnect_midflight(url: str, label: str) -> str:
    """drive ``cursor_overview`` to completion then drop the connection ungracefully.

    Cancellation propagates through the streamable-HTTP context manager; the
    server's ``server_lifespan`` finally-block runs (logs "MCP session closed").
    Pre-be2ecc7f, that finally-block called ``agent.on_shutdown`` and broke
    every other concurrent session.
    """
    sleep_secs = 0.10  # land the cancellation after initialize, before completion
    try:
        async with streamablehttp_client(url, timeout=60.0, sse_read_timeout=120.0) as (
            read,
            write,
            _get_session_id,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # fire the call but cancel ourselves before it returns
                tool_task = asyncio.create_task(
                    session.call_tool(
                        "cursor_overview",
                        {
                            "relative_path": "src/main.swift",
                            "because": (
                                f"client {label} disconnects mid-operation to verify "
                                "the be2ecc7f fix prevents per-session teardown of agent state"
                            ),
                        },
                    )
                )
                await asyncio.sleep(sleep_secs)
                tool_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, BaseException):
                    await tool_task
                # raising here forces the streamable-HTTP context manager to exit
                # via an exception path, simulating an ungraceful client disconnect
                raise asyncio.CancelledError()
    except asyncio.CancelledError:
        return f"{label}:disconnected"


# ---------------------------------------------------------------------------
# Test body
# ---------------------------------------------------------------------------


def _run_concurrent_workload(url: str) -> tuple[list[str], list[BaseException]]:
    """drive 8 concurrent clients (1 disconnects mid-flight, 7 run to completion)."""

    async def driver() -> tuple[list[str], list[BaseException]]:
        survivors = [
            asyncio.create_task(_run_overview(url, f"c{i}"))
            for i in range(N_CLIENTS - 1)
        ]
        # start the dropper last so it is most likely to land its cancellation
        # while the LSP is still busy answering the survivors' overviews
        dropper = asyncio.create_task(
            _run_overview_then_disconnect_midflight(url, "drop")
        )
        all_tasks = [dropper, *survivors]
        results = await asyncio.gather(*all_tasks, return_exceptions=True)
        ok_results = [r for r in results if isinstance(r, str)]
        errors = [r for r in results if isinstance(r, BaseException)]
        return ok_results, errors

    return asyncio.run(asyncio.wait_for(driver(), timeout=END_TO_END_TIMEOUT_S))


def test_concurrent_sse_clients_share_one_swift_lsp(
    serena_http_server: dict[str, Any],
) -> None:
    """8 HTTP clients share one sourcekit-lsp; mid-flight disconnect does not break others.

    This is the verification gate for the shared-Serena rollout: until this
    test is green, swapping the Claude Code MCP config from stdio to
    streamable-http would re-introduce the "No active project" / 15-min-hang
    failure mode previously observed.
    """
    port = serena_http_server["port"]
    repo_path: Path = serena_http_server["repo_path"]
    log_file: Path = serena_http_server["log_file"]
    url = f"http://127.0.0.1:{port}/mcp"

    ok_results, errors = _run_concurrent_workload(url)

    # Survivors (7 clients) all finished cleanly
    survivor_oks = sorted(r for r in ok_results if r.endswith(":ok"))
    expected_survivors = sorted(f"c{i}:ok" for i in range(N_CLIENTS - 1))
    assert survivor_oks == expected_survivors, (
        "surviving clients did not all complete successfully after the "
        f"mid-flight disconnect — be2ecc7f regression suspected.\n"
        f"  got: {survivor_oks!r}\n"
        f"  errors: {errors!r}\n"
        f"  server log: {log_file}"
    )
    assert "drop:disconnected" in ok_results, (
        f"dropper client did not surface as disconnected; results: {ok_results!r}"
    )
    assert not errors, (
        f"unexpected exceptions from concurrent workload: {errors!r}\n"
        f"server log: {log_file}"
    )

    # IRONCLAD: only ONE sourcekit-lsp child for this project's scratch path
    scratch = repo_path / ".build" / "sourcekit-lsp"
    procs = _project_sourcekit_processes(scratch)
    pids = [p.pid for p in procs]
    cmdlines = [" ".join(p.cmdline()) for p in procs]
    assert len(procs) == 1, (
        f"expected exactly 1 sourcekit-lsp process for {scratch}; found {len(procs)}.\n"
        f"  pids: {pids}\n"
        f"  cmdlines: {cmdlines}\n"
        f"  server log: {log_file}\n"
        "Multiple LSPs imply LanguageServerManager._language_servers does not "
        "deduplicate across concurrent HTTP sessions — shared-Serena rollout is "
        "blocked until this is fixed."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-xvs"]))
