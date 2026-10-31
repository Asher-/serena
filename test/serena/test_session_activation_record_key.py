"""
Tests of the key of the durable explicit-activation record (plan://serena/issue_bin_closure_activation_record_key).

A client whose connection ends and who initializes a new one is handed a new ``X-Forwarded-Mcp-Session-Id``, so the session
key of every later call changes, while the ``_serena_session_id`` those calls carry stays the same. The record of a call
carrying a session identifier is keyed on that identifier, so the call reads one record whatever forwarded session id it
arrives under. The record of any other call is keyed on its session key.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from serena.agent import _PIPE_SESSION_ID_VAR, _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.util.session_project_roots import ExplicitProjectRootStore
from test.serena.test_session_agent_separation import _activatable_project_at, _make_roots, _RecordingActivationTool
from test.serena.test_session_project_binding import _active, _project_at
from test.serena.test_session_project_binding import (
    agent as _agent_fixture,
)
from test.serena.test_streamable_http_cc_session_key import _make_mcp_ctx, _RequiresProjectTool

agent = _agent_fixture
"""the agent fixture of the session-binding tests, re-exported for pytest"""

_SESSION_ID_ARGUMENT = "_serena_session_id"
"""the reserved tool-call argument carrying the calling Claude Code session's session id"""

_AGENT_ID_ARGUMENT = "_serena_agent_id"
"""the reserved tool-call argument carrying the calling subagent's agent id"""

_FIRST_CONNECTION = "forwarded-session-1"
"""the client's forwarded session id on its first connection"""

_SECOND_CONNECTION = "forwarded-session-2"
"""the client's forwarded session id once the first connection has ended and a new one has initialized"""

_NO_ACTIVE_PROJECT = "No active project"
"""the text of the refusal of a call whose session has no active project and no record"""


class _Restorations:
    """
    Stands in for the re-activation a self-heal performs on an agent, binding a stand-in project at the recorded root
    and noting each root restored together with the session key it was restored for.
    """

    def __init__(self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch) -> None:
        self._agent = agent
        self.restored: list[tuple[str, str | int | None]] = []
        monkeypatch.setattr(agent, "activate_project_from_path_or_name", self._restore)

    def _restore(self, root: str, **kwargs: Any) -> bool:
        self.restored.append((root, _SESSION_KEY_VAR.get(None)))
        self._agent._active_project = _project_at(root, f"restored-{Path(root).name}")
        return True


@pytest.fixture
def store_path(agent: SerenaAgent, tmp_path: Path) -> str:
    """:return: the path of a store of the test's own, which ``agent`` records into"""
    path = str(tmp_path / "session_project_roots.json")
    agent._explicit_project_root_store = ExplicitProjectRootStore(path)
    return path


@pytest.fixture
def new_agent(store_path: str) -> Iterator[SerenaAgent]:
    """A second agent, holding nothing in memory, reading the store of ``agent``."""
    serena_agent = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    serena_agent.record_tool_usage = lambda *args, **kwargs: None  # type: ignore[method-assign]
    serena_agent._explicit_project_root_store = ExplicitProjectRootStore(store_path)
    yield serena_agent
    serena_agent.on_shutdown(timeout=0.5)


@contextmanager
def _pipe_session(root: Path) -> Iterator[None]:
    """
    Presents the calls of its body on the pipe transport, whose session key is the project root.

    :param root: the project root of the pipe session
    """
    token = _PIPE_SESSION_ID_VAR.set(str(root))
    try:
        yield
    finally:
        _PIPE_SESSION_ID_VAR.reset(token)


def _activate(
    agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, root: Path, forwarded_session_id: str | None, **caller_arguments: str
) -> None:
    """
    Records an activation of ``root`` for one caller through :meth:`SerenaAgent._activate_project`.

    :param agent: the agent the call is issued to
    :param monkeypatch: the test's monkeypatch fixture
    :param root: the project root to activate
    :param forwarded_session_id: the forwarded session id the call arrives under
    :param caller_arguments: the reserved arguments that identify the caller
    """
    activation = _active(_RecordingActivationTool(agent, _activatable_project_at(root, f"project-{root.name}")), monkeypatch)
    result = activation.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=forwarded_session_id), log_call=False, **caller_arguments)
    assert result == "activated", f"the activation of {root} for {caller_arguments!r} answered {result!r}"


def _call(agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, forwarded_session_id: str | None, **caller_arguments: str) -> str:
    """
    Presents a call of a tool that requires an active project.

    :param agent: the agent the call is issued to
    :param monkeypatch: the test's monkeypatch fixture
    :param forwarded_session_id: the forwarded session id the call arrives under
    :param caller_arguments: the reserved arguments that identify the caller
    :return: the call's answer
    """
    tool = _active(_RequiresProjectTool(agent), monkeypatch)
    return tool.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=forwarded_session_id), log_call=False, **caller_arguments)


def _write_records(path: str, roots_by_key: dict[str, Path]) -> None:
    """
    Writes the store's file with one record per key, however the store would key a record today.

    :param path: the path of the store's file
    :param roots_by_key: the project root of each record, by the key it is written under
    """
    entries = {key: {"root": str(root), "updated_at": time.time()} for key, root in roots_by_key.items()}
    ExplicitProjectRootStore(path)._write(entries)


def _recorded_keys(path: str) -> set[str]:
    """
    :param path: the path of the store's file
    :return: the keys the file holds
    """
    with open(path, encoding="utf-8") as f:
        return set(json.load(f))


class TestRecordOutlivesTheForwardedSessionId:
    """a call of one session reads the record of that session after its forwarded session id has changed"""

    def test_the_same_agent_restores_the_recorded_root(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """Session S1 activates P1 under F1, and its next call, under F2, is answered from P1."""
        (root_1,) = _make_roots(tmp_path, "P1")
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})
        restorations = _Restorations(agent, monkeypatch)

        result = _call(agent, monkeypatch, _SECOND_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})

        assert result == _RequiresProjectTool.RAN, f"the call under the new forwarded session id answered {result!r}"
        assert restorations.restored == [(str(root_1), f"{_SECOND_CONNECTION}/session/S1")]

    def test_a_new_agent_reading_the_store_restores_the_recorded_root(
        self, agent: SerenaAgent, new_agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Session S1 activates P1 under F1, and a new agent reading the same store answers its call under F2 from P1."""
        (root_1,) = _make_roots(tmp_path, "P1")
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})
        restorations = _Restorations(new_agent, monkeypatch)

        result = _call(new_agent, monkeypatch, _SECOND_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})

        assert result == _RequiresProjectTool.RAN, f"the call under the new forwarded session id answered {result!r}"
        assert restorations.restored == [(str(root_1), f"{_SECOND_CONNECTION}/session/S1")]

    def test_the_record_outlives_the_eviction_of_the_forwarded_session(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """Session S1 activates P1 under F1, the end of F1's connection evicts its slots, and the call under F2 is answered from P1."""
        (root_1,) = _make_roots(tmp_path, "P1")
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})
        restorations = _Restorations(agent, monkeypatch)

        agent.evict_forwarded_session(_FIRST_CONNECTION)
        result = _call(agent, monkeypatch, _SECOND_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})

        assert result == _RequiresProjectTool.RAN, f"the call after the eviction answered {result!r}"
        assert restorations.restored == [(str(root_1), f"{_SECOND_CONNECTION}/session/S1")]

    def test_the_record_outlives_the_eviction_of_the_claude_session(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """Session S1 activates P1 under F1, the end of session S1 evicts its slots, and its call under F2 is answered from P1."""
        (root_1,) = _make_roots(tmp_path, "P1")
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})
        restorations = _Restorations(agent, monkeypatch)

        agent.evict_claude_session("S1")
        result = _call(agent, monkeypatch, _SECOND_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})

        assert result == _RequiresProjectTool.RAN, f"the call after the eviction answered {result!r}"
        assert restorations.restored == [(str(root_1), f"{_SECOND_CONNECTION}/session/S1")]


class TestRecordsOfDistinctCallersStayApart:
    """the record key separates what the session key separates"""

    def test_two_session_identifiers_restore_their_own_roots(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """Sessions S1 and S2 activate P1 and P2 under F1, and under F2 each is answered from its own root."""
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})
        _activate(agent, monkeypatch, root_2, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S2"})
        restorations = _Restorations(agent, monkeypatch)

        results = [_call(agent, monkeypatch, _SECOND_CONNECTION, **{_SESSION_ID_ARGUMENT: session}) for session in ("S1", "S2")]

        assert results == [_RequiresProjectTool.RAN] * 2, f"the calls under the new forwarded session id answered {results!r}"
        assert restorations.restored == [
            (str(root_1), f"{_SECOND_CONNECTION}/session/S1"),
            (str(root_2), f"{_SECOND_CONNECTION}/session/S2"),
        ]

    def test_one_session_identifier_and_an_agent_identifier_restore_neither_others_root(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """
        Session S1 activates P1 and, carrying the agent identifier A1, activates P2 under F1. Under F2 each call is answered
        from its own root, and neither restores the other's root.
        """
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        session_key = f"{_SECOND_CONNECTION}/session/S1"
        agent_key = f"{session_key}/agent/A1"
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})
        _activate(agent, monkeypatch, root_2, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1", _AGENT_ID_ARGUMENT: "A1"})
        restorations = _Restorations(agent, monkeypatch)

        results = [
            _call(agent, monkeypatch, _SECOND_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1", _AGENT_ID_ARGUMENT: "A1"}),
            _call(agent, monkeypatch, _SECOND_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"}),
        ]

        assert results == [_RequiresProjectTool.RAN] * 2, f"the calls under the new forwarded session id answered {results!r}"
        assert (str(root_1), agent_key) not in restorations.restored, "the agent restored its session's root"
        assert (str(root_2), session_key) not in restorations.restored, "the session restored its agent's root"
        assert restorations.restored == [(str(root_2), agent_key), (str(root_1), session_key)]

    def test_two_agents_of_one_session_restore_neither_others_root(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """Agents A1 and A2 of session S1 activate P1 and P2 under F1, and under F2 neither restores the other's root."""
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        key_1, key_2 = (f"{_SECOND_CONNECTION}/session/S1/agent/{agent_id}" for agent_id in ("A1", "A2"))
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1", _AGENT_ID_ARGUMENT: "A1"})
        _activate(agent, monkeypatch, root_2, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1", _AGENT_ID_ARGUMENT: "A2"})
        restorations = _Restorations(agent, monkeypatch)

        results = [
            _call(agent, monkeypatch, _SECOND_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1", _AGENT_ID_ARGUMENT: agent_id})
            for agent_id in ("A1", "A2")
        ]

        assert results == [_RequiresProjectTool.RAN] * 2, f"the calls under the new forwarded session id answered {results!r}"
        assert restorations.restored == [(str(root_1), key_1), (str(root_2), key_2)]

    def test_an_agent_of_a_session_does_not_read_the_record_of_its_session(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """Session S1 activates P1 under F1, and its agent A1, which activated nothing, is refused under F2."""
        (root_1,) = _make_roots(tmp_path, "P1")
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})
        restorations = _Restorations(agent, monkeypatch)

        result = _call(agent, monkeypatch, _SECOND_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1", _AGENT_ID_ARGUMENT: "A1"})

        assert _NO_ACTIVE_PROJECT in result, f"the agent's call answered {result!r}"
        assert restorations.restored == []


class TestCallCarryingNoSessionIdentifier:
    """the record of a call carrying no session identifier stays keyed on its session key"""

    def test_a_changed_forwarded_session_id_is_refused(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """A call without identifiers activates P1 under F1, and a call without identifiers under F2 is refused."""
        (root_1,) = _make_roots(tmp_path, "P1")
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION)
        restorations = _Restorations(agent, monkeypatch)

        result = _call(agent, monkeypatch, _SECOND_CONNECTION)

        assert _NO_ACTIVE_PROJECT in result, f"the call under the new forwarded session id answered {result!r}"
        assert restorations.restored == []

    def test_a_call_with_no_record_is_refused(self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, store_path: str) -> None:
        """A call without identifiers, for which nothing was ever recorded, is refused."""
        restorations = _Restorations(agent, monkeypatch)

        result = _call(agent, monkeypatch, _FIRST_CONNECTION)

        assert _NO_ACTIVE_PROJECT in result, f"the call with no record answered {result!r}"
        assert restorations.restored == []

    def test_a_changed_forwarded_session_id_is_refused_to_an_agent_without_a_session(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """A call carrying only the agent id A1 activates P1 under F1, and its call under F2 is refused."""
        (root_1,) = _make_roots(tmp_path, "P1")
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_AGENT_ID_ARGUMENT: "A1"})
        restorations = _Restorations(agent, monkeypatch)

        result = _call(agent, monkeypatch, _SECOND_CONNECTION, **{_AGENT_ID_ARGUMENT: "A1"})

        assert _NO_ACTIVE_PROJECT in result, f"the call under the new forwarded session id answered {result!r}"
        assert restorations.restored == []


class TestKeyThatOnlyContainsTheSessionSegment:
    """a key is a session identifier's only when ``session/`` directly follows the forwarded session id"""

    def test_pipe_sessions_whose_roots_end_alike_keep_their_own_records(
        self, agent: SerenaAgent, new_agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two pipe sessions, keyed on roots that both end in ``/session/project``, are each answered from their own root."""
        roots = [tmp_path / parent / "session" / "project" for parent in ("one", "two")]
        for root in roots:
            root.mkdir(parents=True)
            with _pipe_session(root):
                _activate(agent, monkeypatch, root, None)
        restorations = _Restorations(new_agent, monkeypatch)

        results = []
        for root in roots:
            with _pipe_session(root):
                results.append(_call(new_agent, monkeypatch, None))

        assert results == [_RequiresProjectTool.RAN] * 2, f"the pipe sessions answered {results!r}"
        assert restorations.restored == [(str(root), str(root)) for root in roots]

    def test_discarding_a_pipe_session_leaves_the_record_of_a_root_that_ends_alike(self, agent: SerenaAgent, tmp_path: Path) -> None:
        """Evicting one of two pipe sessions keyed on roots that end alike leaves the other's record."""
        root_1, root_2 = (str(tmp_path / parent / "session" / "project") for parent in ("one", "two"))
        path = str(tmp_path / "session_project_roots.json")
        agent._explicit_project_root_store = ExplicitProjectRootStore(path)
        agent._explicit_project_root_store.set(root_1, root_1)
        agent._explicit_project_root_store.set(root_2, root_2)

        agent.evict_pipe_session(root_1)

        assert _recorded_keys(path) == {root_2}

    def test_an_agent_identifier_holding_the_segment_does_not_read_a_sessions_record(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """Session S1 activates P1 under F1, and a call whose agent id is ``x/session/S1`` is refused under F2."""
        (root_1,) = _make_roots(tmp_path, "P1")
        _activate(agent, monkeypatch, root_1, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})
        restorations = _Restorations(agent, monkeypatch)

        result = _call(agent, monkeypatch, _SECOND_CONNECTION, **{_AGENT_ID_ARGUMENT: "x/session/S1"})

        assert _NO_ACTIVE_PROJECT in result, f"the call under the agent id answered {result!r}"
        assert restorations.restored == []


class TestStoreKeys:
    """the keys :class:`ExplicitProjectRootStore` writes, reads and removes"""

    @pytest.mark.parametrize(
        ("session_key", "record_key"),
        [
            (f"{_FIRST_CONNECTION}/session/S1", "session/S1"),
            (f"{_FIRST_CONNECTION}/session/S1/agent/A1", "session/S1/agent/A1"),
            (_FIRST_CONNECTION, _FIRST_CONNECTION),
            (f"{_FIRST_CONNECTION}/agent/A1", f"{_FIRST_CONNECTION}/agent/A1"),
            (f"{_FIRST_CONNECTION}/agent/x/session/S1", f"{_FIRST_CONNECTION}/agent/x/session/S1"),
            ("/one/session/project", "/one/session/project"),
        ],
    )
    def test_a_record_is_written_under_its_record_key(self, tmp_path: Path, session_key: str, record_key: str) -> None:
        path = str(tmp_path / "session_project_roots.json")

        ExplicitProjectRootStore(path).set(session_key, "/recorded/root")

        assert _recorded_keys(path) == {record_key}

    def test_a_record_written_under_a_session_key_is_restored_for_that_key(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_path: str
    ) -> None:
        """A record written under ``<forwarded session id>/session/<session identifier>`` restores for a call under that key."""
        (root_1,) = _make_roots(tmp_path, "P1")
        session_key = f"{_FIRST_CONNECTION}/session/S1"
        _write_records(store_path, {session_key: root_1})
        restorations = _Restorations(agent, monkeypatch)

        result = _call(agent, monkeypatch, _FIRST_CONNECTION, **{_SESSION_ID_ARGUMENT: "S1"})

        assert result == _RequiresProjectTool.RAN, f"the call under the recorded key answered {result!r}"
        assert restorations.restored == [(str(root_1), session_key)]

    def test_the_record_key_is_read_before_the_session_key(self, tmp_path: Path) -> None:
        """A call holding a record under each key is answered from the record under its record key."""
        path = str(tmp_path / "session_project_roots.json")
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        session_key = f"{_FIRST_CONNECTION}/session/S1"
        _write_records(path, {"session/S1": root_1, session_key: root_2})

        assert ExplicitProjectRootStore(path).get(session_key) == str(root_1)

    @pytest.mark.parametrize(
        "recorded_keys",
        [
            ["session/S1", f"{_FIRST_CONNECTION}/session/S1"],
            ["session/S1"],
            [f"{_FIRST_CONNECTION}/session/S1"],
        ],
    )
    def test_discarding_removes_the_record_under_both_keys(self, tmp_path: Path, recorded_keys: list[str]) -> None:
        """Discarding a call's record leaves neither key in the file, and keeps the record of another session."""
        path = str(tmp_path / "session_project_roots.json")
        (root_1,) = _make_roots(tmp_path, "P1")
        _write_records(path, {**dict.fromkeys(recorded_keys, root_1), "session/S2": root_1})

        ExplicitProjectRootStore(path).discard(f"{_FIRST_CONNECTION}/session/S1")

        assert _recorded_keys(path) == {"session/S2"}

    def test_discarding_a_key_with_no_record_writes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "session_project_roots.json"

        ExplicitProjectRootStore(str(path)).discard(f"{_FIRST_CONNECTION}/session/S1")

        assert not path.exists()
