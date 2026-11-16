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
from solidlsp.ls_config import LanguageServerId

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

    def create_language_server(self, language: LanguageServerId) -> SolidLanguageServer:  # type: ignore[override]
        server = MagicMock(spec=SolidLanguageServer)
        server.language = language
        server.ls_id = language
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
    manager = LanguageServerManager.from_languages([LanguageServerId.PYTHON], factory, project=None)
    assert factory.created and manager.get_active_language_server_ids() == [LanguageServerId.PYTHON]
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

        replacement = manager.restart_language_server(LanguageServerId.PYTHON)

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
            manager.restart_language_server(LanguageServerId.PYTHON)

        # the replacement that failed to start was stopped
        assert len(factory.created) == 2
        factory.created[1].stop.assert_called_once()


class TestStopAllStartsNoServer:
    """verifies that stopping a manager, or saving its caches, starts no server and stops every server it holds."""

    def test_stop_all_of_a_server_found_not_running_starts_no_replacement_and_stops_it(self) -> None:
        # the held server reports not running
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        held = factory.created[0]
        held.is_running.return_value = False

        manager.stop_all()

        # no replacement was created and the held server was stopped
        assert factory.created == [held]
        held.stop.assert_called_once()

    def test_stop_all_whose_first_stop_raises_stops_every_other_server_and_raises(self) -> None:
        # the first held server's stop raises
        factory = _GatedLanguageServerFactory()
        manager = LanguageServerManager.from_languages([LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT], factory, project=None)
        first, second = factory.created
        failure = OSError(24, "Too many open files")
        first.stop.side_effect = failure

        with pytest.raises(OSError) as raised:
            manager.stop_all()

        # that failure was raised once the other server had been stopped
        assert raised.value is failure
        second.stop.assert_called_once()

    def test_stop_all_saving_caches_saves_the_running_server_s_cache_and_not_the_stopped_one_s(self) -> None:
        # one held server runs, the other reports not running
        factory = _GatedLanguageServerFactory()
        manager = LanguageServerManager.from_languages([LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT], factory, project=None)
        running, stopped = factory.created
        stopped.is_running.return_value = False

        manager.stop_all(save_cache=True)

        # the running server's cache was saved and both were stopped
        running.save_cache.assert_called_once()
        stopped.save_cache.assert_not_called()
        running.stop.assert_called_once()
        stopped.stop.assert_called_once()

    def test_stop_all_whose_cache_save_raises_still_stops_that_server_and_raises(self) -> None:
        # the held server's cache save raises
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        held = factory.created[0]
        failure = OSError(24, "Too many open files")
        held.save_cache.side_effect = failure

        with pytest.raises(OSError) as raised:
            manager.stop_all(save_cache=True)

        # the server was stopped all the same, and the failure was raised
        held.stop.assert_called_once()
        assert raised.value is failure

    def test_stop_all_whose_cache_save_and_later_stop_raise_raises_the_stop_failure(self) -> None:
        # the first held server's cache save raises, and the second server's stop raises
        factory = _GatedLanguageServerFactory()
        manager = LanguageServerManager.from_languages([LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT], factory, project=None)
        first, second = factory.created
        first.save_cache.side_effect = OSError(24, "Too many open files")
        stop_failure = RuntimeError("stop failed")
        second.stop.side_effect = stop_failure

        with pytest.raises(RuntimeError) as raised:
            manager.stop_all(save_cache=True)

        # the stop's failure was raised, after both servers' stops were attempted
        assert raised.value is stop_failure
        first.stop.assert_called_once()

    def test_save_all_caches_of_a_server_found_not_running_starts_no_replacement(self) -> None:
        # the held server reports not running
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        held = factory.created[0]
        held.is_running.return_value = False

        manager.save_all_caches()

        # no replacement was created and no cache was saved
        assert factory.created == [held]
        held.save_cache.assert_not_called()

    def test_save_all_caches_of_a_running_server_saves_its_cache(self) -> None:
        # the held server runs
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        held = factory.created[0]

        manager.save_all_caches()

        # its cache was saved and no replacement was created
        held.save_cache.assert_called_once()
        assert factory.created == [held]


class TestConcurrentAdd:
    """verifies that callers adding one language at the same time leave one server for it running."""

    def test_caller_adding_a_language_while_it_is_being_added_is_refused(self) -> None:
        # a manager holding Python, whose next server start is held
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        factory.hold_starts()
        errors: dict[str, Exception] = {}

        def add(caller: str) -> None:
            try:
                manager.add_language_server(LanguageServerId.TYPESCRIPT)
            except Exception as e:
                errors[caller] = e

        # the first caller adds TypeScript and holds in its server's start; the second caller adds it meanwhile
        first = threading.Thread(target=add, args=("first",), daemon=True)
        first.start()
        assert factory.start_entered.wait(timeout=_GATE_TIMEOUT)
        second = threading.Thread(target=add, args=("second",), daemon=True)
        second.start()
        second.join(timeout=0.5)

        # release the start and let both callers return
        factory.start_gate.set()
        first.join(timeout=_GATE_TIMEOUT)
        second.join(timeout=_GATE_TIMEOUT)
        assert not first.is_alive() and not second.is_alive()

        # the second caller was refused, and every created server the manager does not hold was stopped
        assert set(errors) == {"second"} and isinstance(errors["second"], ValueError)
        held = list(manager.iter_language_servers())
        assert len(held) == 2
        for server in factory.created:
            assert server in held or server.stop.called


class TestConcurrentRemove:
    """verifies that a language removed while its server is being restarted stays removed."""

    def test_language_removed_during_its_restart_holds_no_server_and_every_created_server_is_stopped(self) -> None:
        # a manager holding Python, whose next server start is held
        factory = _GatedLanguageServerFactory()
        manager = _python_manager(factory)
        factory.hold_starts()

        # a restart of Python holds in its replacement's start; Python is removed meanwhile
        restart = threading.Thread(target=manager.restart_language_server, args=(LanguageServerId.PYTHON,), daemon=True)
        restart.start()
        assert factory.start_entered.wait(timeout=_GATE_TIMEOUT)
        remove = threading.Thread(target=manager.remove_language_server, args=(LanguageServerId.PYTHON,), daemon=True)
        remove.start()
        remove.join(timeout=0.5)

        # release the start and let both callers return
        factory.start_gate.set()
        restart.join(timeout=_GATE_TIMEOUT)
        remove.join(timeout=_GATE_TIMEOUT)
        assert not restart.is_alive() and not remove.is_alive()

        # the manager holds no Python server, and every server created for it was stopped
        assert manager.get_active_language_server_ids() == []
        assert len(factory.created) == 2
        for server in factory.created:
            server.stop.assert_called()

    def test_removing_a_language_the_manager_does_not_hold_is_refused(self) -> None:
        manager = _python_manager(_GatedLanguageServerFactory())

        with pytest.raises(ValueError):
            manager.remove_language_server(LanguageServerId.TYPESCRIPT)
