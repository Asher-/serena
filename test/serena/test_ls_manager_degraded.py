"""unit tests for the degraded-start behaviour of :class:`LanguageServerManager`.

these tests exercise the fault-isolation policy introduced to replace the earlier fail-fast one:
a failure in a single language's LS startup no longer tears down the other languages' servers;
instead, the manager is constructed with the successfully-started servers, and callers that ask for
an unavailable language receive a typed :class:`LanguageUnavailableError`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from serena.ls_manager import LanguageServerFactory, LanguageServerManager, LanguageUnavailableError
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import Language


class _ScriptedLanguageServerFactory(LanguageServerFactory):
    """factory that can be told, per language, to either return a running mock or raise on creation."""

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
        """allows a test to flip a previously-failing language to healthy before a restart."""
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
