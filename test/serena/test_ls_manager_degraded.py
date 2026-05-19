"""unit tests for the degraded-start behaviour of :class:`LanguageServerManager`.

these tests exercise the fault-isolation policy introduced to replace the earlier fail-fast one:
a failure in a single language's LS startup no longer tears down the other languages' servers;
instead, the manager is constructed with the successfully-started servers, and callers that ask for
an unavailable language receive a typed :class:`LanguageUnavailableError`.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import types
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import (
    LanguageBackend,
    ProjectConfig,
    RegisteredProject,
    SerenaConfig,
)
from serena.ls_manager import LanguageServerFactory, LanguageServerManager, LanguageUnavailableError
from serena.project import Project
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import Language
class _ScriptedLanguageServerFactory(LanguageServerFactory):
    def __init__(self, failures: dict[Language, Exception] | None = None) -> None:
        # stored as instance state only; the base constructor is deliberately not invoked because
        # it validates paths and builds file filters, neither of which is needed here
        self._failures: dict[Language, Exception] = dict(failures or {})
        self._created: dict[Language, SolidLanguageServer] = {}
        self.restart_attempts: dict[Language, int] = {}

    def create_language_server(self, language: Language) -> SolidLanguageServer:  # type: ignore[override]
        # count attempts so tests can verify restart retries actually call the factory
        self.restart_attempts[language] = self.restart_attempts.get(language, 0) + 1

        if language in self._failures:
            raise self._failures[language]

        mock_server = MagicMock(spec=SolidLanguageServer)
        mock_server.language = language
        # the manager's startup thread calls start(), then is_running(); is_running() must be True
        mock_server.is_running.return_value = True
        # stop() is called when the manager tears down
        mock_server.stop.return_value = None
        self._created[language] = mock_server
        return mock_server

    def clear_failure(self, language: Language) -> None:
        """Allows a test to flip a previously-failing language to healthy before a restart."""
        self._failures.pop(language, None)


@pytest.fixture
def scripted_factory() -> _ScriptedLanguageServerFactory:
    return _ScriptedLanguageServerFactory()


class TestDegradedStartup:
    """verifies that a single-language failure no longer takes out the whole manager."""

    def test_partial_failure_still_constructs_manager(self) -> None:
        # one language fails, one succeeds: the manager must come up with the working language only
        factory = _ScriptedLanguageServerFactory(failures={Language.SCALA: RuntimeError("metals crashed")})

        manager = LanguageServerManager.from_languages([Language.PYTHON, Language.SCALA], factory)

        assert manager.get_active_languages() == [Language.PYTHON]
        assert manager.is_language_available(Language.PYTHON)
        assert not manager.is_language_available(Language.SCALA)

        unavailable = manager.get_unavailable_languages()
        assert set(unavailable.keys()) == {Language.SCALA}
        assert isinstance(unavailable[Language.SCALA], RuntimeError)
        assert "metals crashed" in str(unavailable[Language.SCALA])

    def test_all_failures_still_produce_a_manager_whose_calls_raise_typed_error(self) -> None:
        # when every requested language fails, the manager still constructs (stable object) but each
        # request for a language server raises LanguageUnavailableError carrying the underlying cause
        scala_failure = RuntimeError("metals crashed")
        python_failure = RuntimeError("pyright missing")
        factory = _ScriptedLanguageServerFactory(failures={Language.SCALA: scala_failure, Language.PYTHON: python_failure})

        manager = LanguageServerManager.from_languages([Language.PYTHON, Language.SCALA], factory)

        assert manager.get_active_languages() == []
        assert manager.get_unavailable_languages().keys() == {Language.PYTHON, Language.SCALA}

        with pytest.raises(LanguageUnavailableError) as exc_info:
            manager.get_language_server("some_file.py")

        # the failure surface carries the full set of unavailable languages so the caller can report them
        assert exc_info.value.unavailable_languages.keys() == {Language.PYTHON, Language.SCALA}

    def test_successful_startup_leaves_no_unavailable_entries(self) -> None:
        # baseline: when every requested language starts, nothing is recorded as unavailable
        factory = _ScriptedLanguageServerFactory()

        manager = LanguageServerManager.from_languages([Language.PYTHON, Language.TYPESCRIPT], factory)

        assert manager.get_active_languages() == [Language.PYTHON, Language.TYPESCRIPT]
        assert manager.get_unavailable_languages() == {}


class TestRestartRecoversUnavailableLanguage:
    """verifies that restart_language_server can promote a previously-failed language back to running."""

    def test_restart_recovers_unavailable_language(self) -> None:
        # initial start fails for Scala; after the underlying fault is cleared, a restart must succeed
        # and move Scala from unavailable to available without affecting Python
        factory = _ScriptedLanguageServerFactory(failures={Language.SCALA: RuntimeError("metals crashed")})
        manager = LanguageServerManager.from_languages([Language.PYTHON, Language.SCALA], factory)
        assert not manager.is_language_available(Language.SCALA)

        factory.clear_failure(Language.SCALA)
        server = manager.restart_language_server(Language.SCALA)

        assert server is not None
        assert manager.is_language_available(Language.SCALA)
        assert Language.SCALA not in manager.get_unavailable_languages()
        # python was already running and must stay running
        assert manager.is_language_available(Language.PYTHON)

    def test_restart_still_failing_raises_typed_error_and_keeps_unavailable(self) -> None:
        # restart of a still-broken language re-records the unavailability with the new failure cause
        first_failure = RuntimeError("metals crashed")
        factory = _ScriptedLanguageServerFactory(failures={Language.SCALA: first_failure})
        manager = LanguageServerManager.from_languages([Language.SCALA], factory)

        # swap the scripted failure for a different exception so we can distinguish the two
        second_failure = RuntimeError("metals still broken")
        factory._failures[Language.SCALA] = second_failure

        with pytest.raises(LanguageUnavailableError) as exc_info:
            manager.restart_language_server(Language.SCALA)

        assert exc_info.value.language == Language.SCALA
        assert exc_info.value.cause is second_failure
        # the manager's unavailability record is updated with the newer failure
        assert manager.get_unavailable_languages()[Language.SCALA] is second_failure

    def test_restart_rejects_unknown_language(self) -> None:
        # a language that was never requested is neither running nor tracked as unavailable;
        # restart must refuse rather than silently trying to add it
        factory = _ScriptedLanguageServerFactory()
        manager = LanguageServerManager.from_languages([Language.PYTHON], factory)

        with pytest.raises(ValueError, match="cannot restart"):
            manager.restart_language_server(Language.SCALA)


class TestActivationMessageWaitsForLsInit:
    """
    Covers the race between `SerenaAgent.activate_project_from_path_or_name` returning and
    the backgrounded language-server-manager init task completing. The activation message
    must wait on the init task with a bounded timeout so per-language LSP startup failures
    are surfaced in the message itself, not deferred to the next tool call.
    """

    _PYTHON_REPO = str(Path(__file__).parent.parent / "resources" / "repos" / "python" / "test_repo")

    def _build_agent(
        self,
        create_manager: Callable[[Project], LanguageServerManager],
    ) -> tuple[SerenaAgent, Project]:
        # construct a project whose LS manager creation we can script from the test
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            log_level=logging.ERROR,
            language_backend=LanguageBackend.LSP,
        )
        project = Project(
            project_root=self._PYTHON_REPO,
            project_config=ProjectConfig(
                project_name="race_test",
                languages=[Language.PYTHON, Language.SCALA],
                language_backend=LanguageBackend.LSP,
            ),
            serena_config=config,
        )
        # patch the project's LS manager factory before activation so the backgrounded
        # init task runs our scripted body instead of spawning real language servers
        project.create_language_server_manager = types.MethodType(  # type: ignore[method-assign]
            create_manager, project
        )
        config.projects = [RegisteredProject.from_project_instance(project)]

        # constructing with project=None keeps the agent inactive so the test can drive
        # activation explicitly and observe the message produced by that specific call
        agent = SerenaAgent(project=None, serena_config=config)
        return agent, project

    def test_activation_message_surfaces_per_language_failure_after_init_completes(self) -> None:
        # scripted LS manager creation: sleep briefly so the init task is demonstrably
        # still running when get_project_activation_message is called, then install a
        # fake manager whose get_unavailable_languages mimics the Metals-crash case
        def create_manager(project: Project) -> LanguageServerManager:
            time.sleep(0.3)
            fake_manager = MagicMock(spec=LanguageServerManager)
            fake_manager.get_active_languages.return_value = [Language.PYTHON]
            fake_manager.get_unavailable_languages.return_value = {
                Language.SCALA: RuntimeError("metals crashed"),
            }
            fake_manager.stop_all.return_value = None
            project.language_server_manager = fake_manager
            # mirror the new contract: create_language_server_manager sets the readiness
            # event in its finally — the monkey-patched replacement must do the same so
            # the activation message wait in get_project_activation_message unblocks
            project._lsm_ready_event.set()
            return fake_manager

        # _activate_project is called directly rather than going through
        # activate_project_from_path_or_name so that reload_if_changed does not
        # replace our custom project instance (whose create_language_server_manager
        # is monkey-patched) with a fresh one rebuilt from the on-disk project.yml
        agent, project = self._build_agent(create_manager)
        try:
            agent._activate_project(project)
            msg = agent.get_project_activation_message()

            assert "Active language servers: python" in msg
            assert "Language servers that failed to start" in msg
            assert "scala: metals crashed" in msg
        finally:
            agent.on_shutdown(timeout=5)

    def test_activation_message_falls_back_to_not_finished_on_timeout(self) -> None:
        # shrink the project's tool_timeout so the test does not have to sleep the default
        # 240 seconds to observe the fallback path; the scripted factory blocks on a gate
        # that is never released within the patched timeout, guaranteeing the readiness
        # wait expires and the activation message falls through to the 'had not finished starting' branch.
        init_gate = threading.Event()

        def create_manager(project: Project) -> LanguageServerManager:
            init_gate.wait(timeout=5.0)
            fake_manager = MagicMock(spec=LanguageServerManager)
            fake_manager.get_active_languages.return_value = []
            fake_manager.get_unavailable_languages.return_value = {}
            fake_manager.stop_all.return_value = None
            project.language_server_manager = fake_manager
            # mirror the new contract: create_language_server_manager sets the readiness
            # event in its finally — the monkey-patched replacement must do the same so
            # read-path waiters in Project.get_language_server_manager_or_raise unblock
            project._lsm_ready_event.set()
            return fake_manager

        # _activate_project is called directly for the same reason as the companion
        # test: to keep our custom project instance (with the gated create_language_server_manager)
        agent, project = self._build_agent(create_manager)
        # tool_timeout is now the unified wait budget for both activation and the
        # read path; shrink it so the activation wait expires fast and the test does
        # not stall on the 240s default
        project.serena_config.tool_timeout = 0.1
        try:
            agent._activate_project(project)
            msg = agent.get_project_activation_message()

            assert "had not finished starting" in msg
        finally:
            # release the gate so the executor thread can finish before on_shutdown
            init_gate.set()
            agent.on_shutdown(timeout=5)


class TestGetLanguageServerManagerOrRaiseBlocksOnReadiness:
    """
    Covers the race between a tool call's read path (:meth:`Project.get_language_server_manager_or_raise`)
    and the backgrounded language-server-manager construction. The read path must block on
    the project's readiness event so an in-flight construction is not surfaced as the
    misleading "could not be constructed at all" exception.
    """

    _PYTHON_REPO = str(Path(__file__).parent.parent / "resources" / "repos" / "python" / "test_repo")

    def _build_project(
        self,
        create_manager: Callable[[Project], LanguageServerManager],
        tool_timeout: float = 30.0,
    ) -> Project:
        # construct a project whose LS manager creation we can script from the test;
        # tool_timeout drives the read-path wait budget (see Project.get_language_server_manager_or_raise)
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            log_level=logging.ERROR,
            language_backend=LanguageBackend.LSP,
            tool_timeout=tool_timeout,
        )
        project = Project(
            project_root=self._PYTHON_REPO,
            project_config=ProjectConfig(
                project_name="readiness_test",
                languages=[Language.PYTHON],
                language_backend=LanguageBackend.LSP,
            ),
            serena_config=config,
        )
        project.create_language_server_manager = types.MethodType(  # type: ignore[method-assign]
            create_manager, project
        )
        return project

    def test_read_path_blocks_until_in_flight_construction_completes(self) -> None:
        # scripted construction installs the fake manager only after a short sleep; a
        # read-path call issued while construction is still in flight must wait for the
        # readiness event rather than raising the scary "could not be constructed at all"
        # message
        def create_manager(project: Project) -> LanguageServerManager:
            time.sleep(0.3)
            fake_manager = MagicMock(spec=LanguageServerManager)
            project.language_server_manager = fake_manager
            project._lsm_ready_event.set()
            return fake_manager

        project = self._build_project(create_manager)

        # kick construction in a background thread so the read path on the main thread can
        # observe the transient None slot before the readiness event sets
        bg = threading.Thread(target=project.create_language_server_manager, daemon=True)
        bg.start()
        time.sleep(0.05)  # ensure read path enters wait while construction is still running

        manager = project.get_language_server_manager_or_raise()
        bg.join(timeout=5.0)
        assert manager is project.language_server_manager
        assert manager is not None

    def test_read_path_surfaces_init_error_after_construction_fails(self) -> None:
        # scripted construction raises after a brief delay; the read path must wait for
        # the readiness signal then surface the captured init error in the raised message
        # rather than the bare "construction not attempted" text
        synthetic_error_text = "synthetic config failure"

        def create_manager(project: Project) -> LanguageServerManager:
            time.sleep(0.2)
            err = RuntimeError(synthetic_error_text)
            project._language_server_manager_init_error = err
            project._lsm_ready_event.set()
            raise err

        project = self._build_project(create_manager)

        def call_construction() -> None:
            # swallow the synthetic failure so the executor thread does not log a noisy
            # uncaught exception; the captured error remains on the project for the
            # read-path assertion below
            with contextlib.suppress(RuntimeError):
                project.create_language_server_manager()

        bg = threading.Thread(target=call_construction, daemon=True)
        bg.start()
        time.sleep(0.05)

        with pytest.raises(Exception) as exc_info:
            project.get_language_server_manager_or_raise()
        bg.join(timeout=5.0)
        assert synthetic_error_text in str(exc_info.value)
        assert "could not be constructed at all" in str(exc_info.value)

    def test_read_path_times_out_when_construction_never_completes(self) -> None:
        # construction blocks on a gate that the test only releases at cleanup, so the
        # read path's bounded wait must expire and the original scary error must be raised.
        # tool_timeout is set just above the wait's resolution so the test does not hang
        init_gate = threading.Event()

        def create_manager(project: Project) -> LanguageServerManager:
            init_gate.wait(timeout=5.0)
            return MagicMock(spec=LanguageServerManager)

        # set the wait budget directly on the SerenaConfig instance (the
        # tool_timeout >= 10 validation in create_language_server_manager does not run
        # here because create_manager is monkey-patched)
        project = self._build_project(create_manager)
        project.serena_config.tool_timeout = 0.1

        bg = threading.Thread(target=create_manager, args=(project,), daemon=True)
        bg.start()
        try:
            with pytest.raises(Exception) as exc_info:
                project.get_language_server_manager_or_raise()
            assert "could not be constructed at all" in str(exc_info.value)
        finally:
            init_gate.set()
            bg.join(timeout=5.0)
