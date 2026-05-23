"""
Tests for the auto-reactivate fallback in :meth:`Tool.apply_ex`.

Covers handoff://Serena:serena/implement-auto-reactivate-fallback-for-streamable-http-subagent-drops:
when a non-activate tool call arrives over the streamable-http path with an empty per-CC-session
slot (typical of a Claude Code Task subagent whose freshly-minted ``Mcp-Session-Id`` the daemon
has never seen), the daemon recovers by reactivating the project most recently activated on the
same multiplexer<->serena MCP session — instead of returning the IRONCLAD "No active project"
error and forcing the client into a manual ``activate_project`` round-trip.

The recovery source — :attr:`SerenaAgent._last_active_project_by_mcp_session`, keyed by
``id(mcp_ctx.session)`` — scopes the fallback to one multiplexer connection. Multiple agents
working on the same project (the common case for parent + subagents) converge on the same
ambient and recover correctly; cross-project parallel work on the same multiplexer is the
documented edge case where the wrong project may be picked and the client must call
``activate_project`` explicitly.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from serena.agent import (
    _MCP_CALL_IN_FLIGHT,
    _MCP_SESSION_ID_VAR,
    _PIPE_SESSION_ID_VAR,
    _SESSION_KEY_VAR,
    ProjectNotFoundError,
    SerenaAgent,
)
from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject


class _RequiresProjectProbeTool(Tool):
    """A :class:`Tool` subclass that *requires* an active project so the No-active-project
    branch (and its auto-reactivate fallback) is exercised."""

    def apply(self) -> str:
        return "OK"


class _NoProjectProbeTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """A :class:`Tool` subclass that does NOT require an active project; used to verify the
    auto-reactivate path is bypassed for tools marked as project-optional."""

    def apply(self) -> str:
        return "OK"


class _FakeSession:
    """A reference-bearing stand-in for ``mcp_ctx.session`` that supports both :func:`id` and
    :func:`weakref.finalize` (plain ``object()`` does not support weak references)."""

    def __init__(self) -> None:
        self.client_params = None


class _CaseInsensitiveHeaders(dict[str, str]):
    """Starlette-style header map: ``.get`` matches keys case-insensitively, like the FastMCP
    HTTP variant whose headers ``Tool.apply_ex`` probes."""

    def get(self, key: str, default: Any = None) -> Any:
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _make_mcp_ctx(
    *,
    forwarded_header: str | None = None,
    session: _FakeSession | None = None,
) -> MagicMock:
    """Build a mock ``mcp_ctx`` exposing the forwarded-CC-session header via the FastMCP
    ``request_context.request.headers`` path."""
    ctx = MagicMock()
    ctx.session = session if session is not None else _FakeSession()
    headers = _CaseInsensitiveHeaders()
    if forwarded_header is not None:
        headers["x-forwarded-mcp-session-id"] = forwarded_header
    ctx.request_context.request.headers = headers
    ctx.request = MagicMock()
    ctx.request.headers = _CaseInsensitiveHeaders()
    return ctx


def _project_stub(name: str) -> Project:
    """Return a :class:`Project`-typed mock that the agent's routing layer can store."""
    project = MagicMock(spec=Project)
    project.project_name = name
    project.project_root = f"/tmp/{name}"
    return project


@pytest.fixture
def agent() -> SerenaAgent:
    """Build a minimal :class:`SerenaAgent` with no active project."""
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    return SerenaAgent(serena_config=config)


@pytest.fixture(autouse=True)
def _reset_context_vars():
    """Reset transport-derived ContextVars between tests so a previous test's leak cannot bleed
    into the next."""
    pipe_token = _PIPE_SESSION_ID_VAR.set(None)
    mcp_session_token = _MCP_SESSION_ID_VAR.set(None)
    in_flight_token = _MCP_CALL_IN_FLIGHT.set(False)
    yield
    _PIPE_SESSION_ID_VAR.reset(pipe_token)
    _MCP_SESSION_ID_VAR.reset(mcp_session_token)
    _MCP_CALL_IN_FLIGHT.reset(in_flight_token)


# ---------------------------------------------------------------------------
# Direct unit tests for the SerenaAgent helpers backing the auto-reactivate.
# ---------------------------------------------------------------------------


class TestAmbientProjectRecording:
    """:meth:`_record_ambient_activation` writes through to the per-MCP-session map only when
    :data:`_MCP_SESSION_ID_VAR` is set."""

    def test_record_writes_when_mcp_session_id_is_set(self, agent: SerenaAgent) -> None:
        ctx = contextvars.copy_context()

        def record() -> None:
            _MCP_SESSION_ID_VAR.set(424242)
            agent._record_ambient_activation("/tmp/alpha")

        ctx.run(record)

        assert agent._last_active_project_by_mcp_session == {424242: "/tmp/alpha"}

    def test_record_is_no_op_without_mcp_session_id(self, agent: SerenaAgent) -> None:
        # CLI / dashboard / pipe-transport callers have no _MCP_SESSION_ID_VAR; no ambient entry.
        agent._record_ambient_activation("/tmp/beta")
        assert agent._last_active_project_by_mcp_session == {}

    def test_get_ambient_returns_recorded_value(self, agent: SerenaAgent) -> None:
        agent._last_active_project_by_mcp_session[100] = "/tmp/gamma"
        assert agent._get_ambient_project_root(100) == "/tmp/gamma"

    def test_get_ambient_returns_none_for_unknown_session(self, agent: SerenaAgent) -> None:
        assert agent._get_ambient_project_root(999) is None


class TestTryAutoReactivateFromAmbient:
    """:meth:`_try_auto_reactivate_from_ambient` looks up the ambient project, calls
    ``activate_project_from_path_or_name`` under the calling ContextVar context, and returns
    the resulting :class:`Project`."""

    def test_returns_project_when_ambient_is_set_and_activation_succeeds(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _project_stub("alpha")
        agent._last_active_project_by_mcp_session[123] = "/tmp/alpha"

        def fake_activate(path_or_name: str, update_active_modes: bool = True, update_active_tools: bool = True) -> bool:
            # mimic the property setter routing: write to the per-CC-session slot under
            # _SESSION_KEY_VAR (which the caller must have set before invoking us).
            session_key = _SESSION_KEY_VAR.get(None)
            assert session_key is not None, "caller must set _SESSION_KEY_VAR before auto-reactivate"
            agent._active_projects_by_session[session_key] = target
            return True

        monkeypatch.setattr(agent, "activate_project_from_path_or_name", fake_activate)

        ctx = contextvars.copy_context()

        def run_recover() -> Project | None:
            _SESSION_KEY_VAR.set("cc-session-subagent-uuid")
            _MCP_SESSION_ID_VAR.set(123)
            return agent._try_auto_reactivate_from_ambient(123)

        recovered = ctx.run(run_recover)
        assert recovered is target

    def test_returns_none_when_ambient_is_unset(self, agent: SerenaAgent) -> None:
        # no entry in _last_active_project_by_mcp_session for id 999
        assert agent._try_auto_reactivate_from_ambient(999) is None

    def test_clears_ambient_when_project_no_longer_registered(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent._last_active_project_by_mcp_session[55] = "/tmp/deleted-project"

        def fake_activate(path_or_name: str, **kwargs: Any) -> bool:
            raise ProjectNotFoundError(f"Project '{path_or_name}' not found")

        monkeypatch.setattr(agent, "activate_project_from_path_or_name", fake_activate)

        result = agent._try_auto_reactivate_from_ambient(55)
        assert result is None
        # the stale ambient entry was evicted so we do not keep retrying a missing project
        assert 55 not in agent._last_active_project_by_mcp_session


# ---------------------------------------------------------------------------
# End-to-end apply_ex tests: the fallback fires through the worker thread.
# ---------------------------------------------------------------------------


class TestApplyExAutoReactivateFallback:
    """``Tool.apply_ex`` invokes the auto-reactivate fallback when the per-CC-session slot
    is empty and the call is a streamable-http (mcp_ctx-bearing) request that targets a tool
    requiring an active project."""

    def _build_tool(
        self,
        agent: SerenaAgent,
        monkeypatch: pytest.MonkeyPatch,
        *,
        marker_class: type[Tool] = _RequiresProjectProbeTool,
    ) -> Tool:
        tool = marker_class(agent)
        monkeypatch.setattr(tool, "is_active", lambda: True)
        monkeypatch.setattr(agent, "record_tool_usage", lambda *a, **kw: None)
        return tool

    def test_auto_reactivate_succeeds_when_ambient_is_set(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _project_stub("alpha")
        session = _FakeSession()
        mcp_session_id = id(session)
        agent._last_active_project_by_mcp_session[mcp_session_id] = "/tmp/alpha"

        # patch _try_auto_reactivate_from_ambient to write the per-session slot the way the
        # real method would after calling activate_project_from_path_or_name -> _activate_project
        def fake_recover(call_mcp_session_id: int) -> Project | None:
            assert call_mcp_session_id == mcp_session_id
            session_key = _SESSION_KEY_VAR.get(None)
            assert session_key == "subagent-cc-session-uuid", (
                "auto-reactivate must run with the calling session_key in scope so the "
                "per-CC-session slot for THIS session, not the legacy slot, is populated"
            )
            agent._active_projects_by_session[session_key] = target
            return target

        monkeypatch.setattr(agent, "_try_auto_reactivate_from_ambient", fake_recover)

        tool = self._build_tool(agent, monkeypatch)
        mcp_ctx = _make_mcp_ctx(forwarded_header="subagent-cc-session-uuid", session=session)

        result = tool.apply_ex(log_call=False, mcp_ctx=mcp_ctx)

        assert result == "OK", f"expected the tool to run after auto-reactivate, got: {result!r}"
        assert agent._active_projects_by_session["subagent-cc-session-uuid"] is target

    def test_iron_clad_error_preserved_when_ambient_is_empty(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession()  # no ambient entry pre-populated
        tool = self._build_tool(agent, monkeypatch)
        mcp_ctx = _make_mcp_ctx(forwarded_header="novel-cc-session-uuid", session=session)

        # track that the helper was consulted but found nothing
        consulted_with: list[int] = []
        original = agent._try_auto_reactivate_from_ambient

        def spy(call_mcp_session_id: int) -> Project | None:
            consulted_with.append(call_mcp_session_id)
            return original(call_mcp_session_id)

        monkeypatch.setattr(agent, "_try_auto_reactivate_from_ambient", spy)

        result = tool.apply_ex(log_call=False, mcp_ctx=mcp_ctx)

        assert consulted_with == [id(session)], "auto-reactivate must be attempted before falling through"
        assert "No active project for this MCP session" in result
        assert "novel-cc-session-uuid" not in agent._active_projects_by_session

    def test_legacy_active_project_is_not_consulted_when_ambient_is_empty(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IRONCLAD guard from plan://Serena:serena/streamable-http-cc-session-id-pass-through-v2:
        the legacy single-slot field MUST be unreachable from the MCP path even when the auto-
        reactivate fallback declines to act. A populated _legacy_active_project must not satisfy
        an in-flight MCP call with an empty ambient."""
        agent._legacy_active_project = _project_stub("legacy")
        session = _FakeSession()  # no ambient
        tool = self._build_tool(agent, monkeypatch)
        mcp_ctx = _make_mcp_ctx(forwarded_header="another-novel-uuid", session=session)

        result = tool.apply_ex(log_call=False, mcp_ctx=mcp_ctx)

        assert "No active project for this MCP session" in result, (
            "with an empty ambient and no per-session entry, the IRONCLAD error must surface — "
            "_legacy_active_project must NOT be a silent fallback"
        )

    def test_pipe_session_id_takes_precedence_over_ambient(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On the pipe transport the ambient map is irrelevant — _PIPE_SESSION_ID_VAR keys the
        session by project_root and the auto-reactivate fallback must not fire."""
        session = _FakeSession()
        agent._last_active_project_by_mcp_session[id(session)] = "/tmp/alpha"

        pipe_project = _project_stub("pipe-project")
        agent._active_projects_by_session["/tmp/pipe-project"] = pipe_project

        recover_called = threading.Event()
        original = agent._try_auto_reactivate_from_ambient

        def spy(call_mcp_session_id: int) -> Project | None:
            recover_called.set()
            return original(call_mcp_session_id)

        monkeypatch.setattr(agent, "_try_auto_reactivate_from_ambient", spy)

        tool = self._build_tool(agent, monkeypatch)
        mcp_ctx = _make_mcp_ctx(forwarded_header="ignored-on-pipe-path", session=session)

        token = _PIPE_SESSION_ID_VAR.set("/tmp/pipe-project")
        try:
            result = tool.apply_ex(log_call=False, mcp_ctx=mcp_ctx)
        finally:
            _PIPE_SESSION_ID_VAR.reset(token)

        assert result == "OK", f"pipe path's pre-populated session slot should satisfy the call: {result!r}"
        assert not recover_called.is_set(), "auto-reactivate must NOT run when pipe session is in scope"

    def test_tool_marked_does_not_require_active_project_skips_recovery(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tools carrying :class:`ToolMarkerDoesNotRequireActiveProject` bypass the active-project
        check entirely; the auto-reactivate fallback must never fire for them, even when an
        ambient is present."""
        session = _FakeSession()
        agent._last_active_project_by_mcp_session[id(session)] = "/tmp/alpha"

        recover_called = threading.Event()
        original = agent._try_auto_reactivate_from_ambient

        def spy(call_mcp_session_id: int) -> Project | None:
            recover_called.set()
            return original(call_mcp_session_id)

        monkeypatch.setattr(agent, "_try_auto_reactivate_from_ambient", spy)

        tool = self._build_tool(agent, monkeypatch, marker_class=_NoProjectProbeTool)
        mcp_ctx = _make_mcp_ctx(forwarded_header="some-cc-session", session=session)

        result = tool.apply_ex(log_call=False, mcp_ctx=mcp_ctx)

        assert result == "OK"
        assert not recover_called.is_set(), (
            "tools that do not require an active project must not pay the auto-reactivate cost"
        )

    def test_two_mcp_sessions_have_independent_ambients(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two distinct multiplexer<->serena MCP sessions must not see each other's ambient.
        Multi-user setups where each user has their own multiplexer must remain isolated."""
        session_alpha = _FakeSession()
        session_beta = _FakeSession()
        project_alpha = _project_stub("alpha")
        project_beta = _project_stub("beta")

        agent._last_active_project_by_mcp_session[id(session_alpha)] = "/tmp/alpha"
        agent._last_active_project_by_mcp_session[id(session_beta)] = "/tmp/beta"

        def fake_recover(call_mcp_session_id: int) -> Project | None:
            session_key = _SESSION_KEY_VAR.get(None)
            assert session_key is not None
            if call_mcp_session_id == id(session_alpha):
                agent._active_projects_by_session[session_key] = project_alpha
                return project_alpha
            if call_mcp_session_id == id(session_beta):
                agent._active_projects_by_session[session_key] = project_beta
                return project_beta
            return None

        monkeypatch.setattr(agent, "_try_auto_reactivate_from_ambient", fake_recover)

        tool_alpha = self._build_tool(agent, monkeypatch)
        tool_beta = self._build_tool(agent, monkeypatch)
        mcp_ctx_alpha = _make_mcp_ctx(forwarded_header="subagent-alpha", session=session_alpha)
        mcp_ctx_beta = _make_mcp_ctx(forwarded_header="subagent-beta", session=session_beta)

        result_alpha = tool_alpha.apply_ex(log_call=False, mcp_ctx=mcp_ctx_alpha)
        result_beta = tool_beta.apply_ex(log_call=False, mcp_ctx=mcp_ctx_beta)

        assert result_alpha == "OK"
        assert result_beta == "OK"
        assert agent._active_projects_by_session["subagent-alpha"] is project_alpha
        assert agent._active_projects_by_session["subagent-beta"] is project_beta

    def test_already_populated_session_slot_skips_recovery(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the per-CC-session slot is already populated, the auto-reactivate must NOT
        run — it is purely a recovery for empty slots."""
        target = _project_stub("alpha")
        session = _FakeSession()
        agent._active_projects_by_session["existing-cc-session"] = target
        agent._last_active_project_by_mcp_session[id(session)] = "/tmp/different-project"

        recover_called = threading.Event()
        original = agent._try_auto_reactivate_from_ambient

        def spy(call_mcp_session_id: int) -> Project | None:
            recover_called.set()
            return original(call_mcp_session_id)

        monkeypatch.setattr(agent, "_try_auto_reactivate_from_ambient", spy)

        tool = self._build_tool(agent, monkeypatch)
        mcp_ctx = _make_mcp_ctx(forwarded_header="existing-cc-session", session=session)

        result = tool.apply_ex(log_call=False, mcp_ctx=mcp_ctx)

        assert result == "OK"
        assert not recover_called.is_set(), (
            "auto-reactivate must not run when the per-CC-session slot is already populated"
        )
        # the pre-existing session slot was NOT clobbered by the ambient
        assert agent._active_projects_by_session["existing-cc-session"] is target


class TestAmbientFinalizerEviction:
    """The ambient map is cleared by a weakref finalizer attached to ``mcp_ctx.session``, so a
    later MCP session that happens to reuse the same ``id()`` cannot inherit stale state."""

    def test_register_ambient_finalizer_is_idempotent_per_mcp_session(self, agent: SerenaAgent) -> None:
        session = _FakeSession()
        agent._register_ambient_finalizer(session)
        agent._register_ambient_finalizer(session)
        # only one finalizer, not two
        assert len(agent._ambient_finalizers) == 1
        assert id(session) in agent._ambient_finalizers

    def test_evict_ambient_project_drops_entry_and_finalizer(self, agent: SerenaAgent) -> None:
        session = _FakeSession()
        agent._register_ambient_finalizer(session)
        agent._last_active_project_by_mcp_session[id(session)] = "/tmp/alpha"

        agent._evict_ambient_project(id(session))

        assert id(session) not in agent._last_active_project_by_mcp_session
        assert id(session) not in agent._ambient_finalizers

    def test_finalizer_fires_when_mcp_session_is_collected(self, agent: SerenaAgent) -> None:
        """When the FastMCP session object becomes unreachable, the weakref finalizer pops the
        ambient entry — preventing id() reuse leaks across multiplexer reconnects."""
        session = _FakeSession()
        session_id = id(session)
        agent._register_ambient_finalizer(session)
        agent._last_active_project_by_mcp_session[session_id] = "/tmp/alpha"

        del session  # drop the only strong reference

        import gc

        gc.collect()

        # the finalizer ran during GC and dropped the entry
        assert session_id not in agent._last_active_project_by_mcp_session
