"""
The Dart language server names a read-only stdin: it sizes its reads with FIONREAD, which answers 0 for a FIFO opened
read-write on macOS, so it would never read a request the FIFO holds.
"""

import dataclasses
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from solidlsp.language_servers.dart_language_server import DartLanguageServer
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_process import LanguageServerProcess
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo
from solidlsp.util import supervise


def test_the_dart_server_is_started_with_a_read_only_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    launches: list[ProcessLaunchInfo] = []

    def record_launch(_server: Any, _config: Any, _root: str, launch: ProcessLaunchInfo, *_rest: Any) -> None:
        launches.append(launch)

    monkeypatch.setattr(DartLanguageServer, "_setup_runtime_dependencies", lambda *_args: "dart language-server")
    monkeypatch.setattr(SolidLanguageServer, "__init__", record_launch)

    DartLanguageServer(None, "/root", None)  # type: ignore[arg-type]

    assert [(launch.cmd, launch.read_only_stdin) for launch in launches] == [("dart language-server", True)]


@pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")
def test_the_dart_launch_reaches_its_supervised_process_as_a_read_only_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    launches: list[ProcessLaunchInfo] = []

    def record_launch(_server: Any, _config: Any, _root: str, launch: ProcessLaunchInfo, *_rest: Any) -> None:
        launches.append(launch)

    monkeypatch.setattr(DartLanguageServer, "_setup_runtime_dependencies", lambda *_args: "dart language-server")
    monkeypatch.setattr(SolidLanguageServer, "__init__", record_launch)
    DartLanguageServer(None, "/root", None)  # type: ignore[arg-type]

    # a stand-in for dart, which reports the mode its stdin was opened in and then runs until it is stopped
    mode = tmp_path / "mode"
    program = tmp_path / "dart.py"
    program.write_text(
        "import fcntl, os, time\n"
        f"written = open({str(mode)!r} + '.tmp', 'w')\n"
        "written.write(str(fcntl.fcntl(0, fcntl.F_GETFL) & os.O_ACCMODE))\n"
        "written.close()\n"
        f"os.rename({str(mode)!r} + '.tmp', {str(mode)!r})\n"
        "time.sleep(120)\n"
    )
    launch = dataclasses.replace(launches[0], cmd=[sys.executable, str(program)], cwd=str(tmp_path))
    process = LanguageServerProcess(launch, LanguageServerId.PYTHON, lambda _: logging.INFO)
    process.start()

    try:
        deadline = time.monotonic() + 60
        while not mode.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert mode.read_text() == str(os.O_RDONLY)
    finally:
        process.stop()
