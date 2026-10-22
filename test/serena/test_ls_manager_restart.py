"""unit tests for the replacement of a language server by :class:`LanguageServerManager`.

a restart must stop the server it replaces, including a replacement whose own start failed, and several callers that
find a language's server not running at the same time must share one replacement, so that the only language-server
process a manager has started and not stopped is the one it holds for that language. Orderings are proven on gates;
the timeouts below only bound how long a broken implementation can hang a test.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from serena.ls_manager import LanguageServerFactory, LanguageServerManager, LanguageUnavailableError
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import Language

_GATE_TIMEOUT = 10.0
"""the seconds a test waits on a gate before failing; a bound on a hang, never the ordering under test"""


class _GatedLanguageServerFactory(LanguageServerFactory):
    """
    A factory double whose servers are mocks that report running. Once :meth:`hold_starts` has been called, the
    ``start`` of every server it creates signals :attr:`start_entered` and waits on :attr:`start_gate`; once
    :attr:`start_failure` is set, that ``start`` raises it.
    """

    def __init__(self) -> None:
        # the base constructor is deliberately not invoked: it validates paths and builds file filters, neither of
        # which is needed here
        self.created: list[SolidLanguageServer] = []
        self.start_entered = threading.Event()
        self.start_gate = threading.Event()
        self.start_failure: Exception | None = None
        self._hold_starts = False

    def hold_starts(self) -> None:
        """Makes the ``start`` of every server started from now on wait on :attr:`start_gate`."""
        self._hold_starts = True

    def create_language_server(self, language: Language) -> SolidLanguageServer:  # type: ignore[override]
        server = MagicMock(spec=SolidLanguageServer)
        server.language = language
        server.is_running.return_value = True
        server.start.side_effect = self._start
        self.created.append(server)
        return server

    def _start(self) -> None:
        # hold on the gate when asked to, then fail when asked to
        if self._hold_starts:
            self.start_entered.set()
            assert self.start_gate.wait(timeout=_GATE_TIMEOUT), "the test never released the start gate"
        if self.start_failure is not None:
            raise self.start_failure


def _python_manager(factory: _GatedLanguageServerFactory) -> LanguageServerManager:
    """
    :param factory: the factory double the manager creates its servers with
    :return: a manager holding one running Python server, ``factory.created[0]``
    """
    manager = LanguageServerManager.from_languages([Language.PYTHON], factory)
    assert factory.created and manager.get_active_languages() == [Language.PYTHON]
    return manager


class TestConcurrentRestart:
    """verifies that callers finding a language's server not running at the same time share one replacement."""

    def test_two_callers_finding_the_server_not_running_share_one_replacement(self) -> None:
        # a manager whose Python server then reports not running, counting every check of it
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        stale = factory.created[0]
        stale_checks = threading.Semaphore(0)

        def stale_is_running() -> bool:
            stale_checks.release()
            return False

        stale.is_running.side_effect = stale_is_running
        factory.hold_starts()

        # the first caller finds the server not running and holds in its replacement's start
        results: dict[str, SolidLanguageServer] = {}

        def request(caller: str) -> None:
            results[caller] = manager.get_language_server("module.py")

        first = threading.Thread(target=request, args=("first",), daemon=True)
        first.start()
        assert factory.start_entered.wait(timeout=_GATE_TIMEOUT)
        assert stale_checks.acquire(timeout=_GATE_TIMEOUT)

        # the second caller finds the same server not running while that replacement is still starting
        second = threading.Thread(target=request, args=("second",), daemon=True)
        second.start()
        assert stale_checks.acquire(timeout=_GATE_TIMEOUT)

        # release the start and let both callers return
        factory.start_gate.set()
        first.join(timeout=_GATE_TIMEOUT)
        second.join(timeout=_GATE_TIMEOUT)
        assert not first.is_alive() and not second.is_alive()

        # one replacement was created, both callers received it, and the manager holds it
        assert len(factory.created) == 2
        replacement = factory.created[1]
        assert results == {"first": replacement, "second": replacement}
        assert list(manager.iter_language_servers()) == [replacement]


class TestRestartStopsReplacedServer:
    """verifies that a restart stops the server it replaces."""

    def test_restart_of_a_running_server_stops_it(self) -> None:
        # restart a language whose server is running
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        replaced = factory.created[0]

        replacement = manager.restart_language_server(Language.PYTHON)

        # the replaced server was stopped and the manager holds the replacement
        replaced.stop.assert_called_once()
        assert replacement is factory.created[1]
        assert list(manager.iter_language_servers()) == [replacement]

    def test_replacement_of_a_server_found_not_running_stops_it(self) -> None:
        # a caller finds the held server not running
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        replaced = factory.created[0]
        replaced.is_running.return_value = False

        server = manager.get_language_server("module.py")

        # the replaced server was stopped and the caller received the replacement
        replaced.stop.assert_called_once()
        assert server is factory.created[1]

    def test_replacement_whose_start_fails_is_stopped(self) -> None:
        # restart a language whose replacement then fails to start
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        factory.start_failure = OSError(24, "Too many open files")

        with pytest.raises(LanguageUnavailableError):
            manager.restart_language_server(Language.PYTHON)

        # the replacement that failed to start was stopped
        assert len(factory.created) == 2
        factory.created[1].stop.assert_called_once()
