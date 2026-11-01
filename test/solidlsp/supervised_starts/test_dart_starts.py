"""
The Dart language server names a read-only stdin: it sizes its reads with FIONREAD, which answers 0 for a FIFO opened
read-write on macOS, so it would never read a request the FIFO holds.
"""

from typing import Any

import pytest

from solidlsp.language_servers.dart_language_server import DartLanguageServer
from solidlsp.ls import SolidLanguageServer
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo


def test_the_dart_server_is_started_with_a_read_only_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    launches: list[ProcessLaunchInfo] = []

    def record_launch(_server: Any, _config: Any, _root: str, launch: ProcessLaunchInfo, *_rest: Any) -> None:
        launches.append(launch)

    monkeypatch.setattr(DartLanguageServer, "_setup_runtime_dependencies", lambda *_args: "dart language-server")
    monkeypatch.setattr(SolidLanguageServer, "__init__", record_launch)

    DartLanguageServer(None, "/root", None)  # type: ignore[arg-type]

    assert [(launch.cmd, launch.read_only_stdin) for launch in launches] == [("dart language-server", True)]
