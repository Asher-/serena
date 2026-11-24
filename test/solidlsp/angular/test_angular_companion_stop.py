"""
The stop of a real Angular language server ends every process its start spawned, the companion TypeScript and HTML
servers' included.
"""

import psutil
import pytest

from solidlsp.ls_config import LanguageServerId
from test.conftest import language_server_tests_enabled, start_ls_context

pytestmark = [
    pytest.mark.angular,
    pytest.mark.skipif(
        not language_server_tests_enabled(LanguageServerId.ANGULAR),
        reason="the Angular language server is not installed in this environment",
    ),
]


def _descendants() -> dict[int, psutil.Process]:
    return {process.pid: process for process in psutil.Process().children(recursive=True)}


def _command_line(process: psutil.Process) -> str:
    try:
        return " ".join(process.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def test_a_stop_ends_the_companion_typescript_and_html_servers() -> None:
    before = _descendants()

    with start_ls_context(LanguageServerId.ANGULAR):
        spawned = [process for pid, process in _descendants().items() if pid not in before]
        command_lines = [_command_line(process) for process in spawned]
        assert any("typescript-language-server" in command_line for command_line in command_lines)
        assert any("vscode-html-language-server" in command_line for command_line in command_lines)

    _, alive = psutil.wait_procs(spawned, timeout=30)
    assert alive == []
