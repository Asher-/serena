"""
The stop of a real Vue language server ends every process its start spawned, the companion TypeScript server's included.
"""

import psutil
import pytest

from solidlsp.ls_config import LanguageServerId
from test.conftest import language_server_tests_enabled, start_ls_context

pytestmark = [
    pytest.mark.vue,
    pytest.mark.skipif(
        not language_server_tests_enabled(LanguageServerId.VUE), reason="the Vue language server is not installed in this environment"
    ),
]


def _descendants() -> dict[int, psutil.Process]:
    return {process.pid: process for process in psutil.Process().children(recursive=True)}


def _command_line(process: psutil.Process) -> str:
    try:
        return " ".join(process.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def test_a_stop_ends_the_companion_typescript_server() -> None:
    before = _descendants()

    with start_ls_context(LanguageServerId.VUE):
        spawned = [process for pid, process in _descendants().items() if pid not in before]
        assert any("typescript-language-server" in _command_line(process) for process in spawned)

    _, alive = psutil.wait_procs(spawned, timeout=30)
    assert alive == []
