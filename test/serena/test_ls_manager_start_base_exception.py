"""
A language server whose start raises a BaseException that is not an Exception is reported unavailable and stopped, as one
that raises an Exception is.
"""

import pytest

from serena.ls_manager import LanguageServerManager
from solidlsp.ls_config import LanguageServerId


class _StartAborted(BaseException):
    """A BaseException that is not an Exception, raised from a language server's start."""


class _AbortingServer:
    """A language server whose start raises :class:`_StartAborted`."""

    def __init__(self) -> None:
        self.stopped = False

    def start(self) -> None:
        raise _StartAborted("start aborted")

    def is_running(self) -> bool:
        return False

    def stop(self) -> None:
        self.stopped = True


class _AbortingFactory:
    """A factory that creates an :class:`_AbortingServer` for every language."""

    def __init__(self) -> None:
        self.created: list[_AbortingServer] = []

    def create_language_server(self, ls_id: LanguageServerId) -> _AbortingServer:
        server = _AbortingServer()
        self.created.append(server)
        return server


def test_a_start_raising_a_base_exception_reports_the_language_unavailable_and_stops_the_server() -> None:
    factory = _AbortingFactory()

    manager = LanguageServerManager.from_languages([LanguageServerId.PYTHON], factory, project=None)  # type: ignore[arg-type]

    assert isinstance(manager.get_unavailable_languages()[LanguageServerId.PYTHON], _StartAborted)
    assert [server.stopped for server in factory.created] == [True]


def test_a_restart_whose_start_raises_a_base_exception_stops_the_server() -> None:
    factory = _AbortingFactory()
    manager = LanguageServerManager.from_languages([LanguageServerId.PYTHON], factory, project=None)  # type: ignore[arg-type]

    with pytest.raises(_StartAborted):
        manager.restart_language_server(LanguageServerId.PYTHON)

    assert [server.stopped for server in factory.created] == [True, True]
