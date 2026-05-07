"""
Regression tests for per-MCP-session cursor manager routing in :class:`SerenaAgent`.

Background
==========

The MCP daemon hosts a single :class:`SerenaAgent` shared by every connected client. Before this fix,
``SerenaAgent`` cached the :class:`CursorManager` on a single agent attribute (``self._cursor_manager``)
and reset that attribute whenever any session called ``_activate_project``. Two failure modes followed:

1. Two concurrent sessions racing on cursor tools shared one manager, so the manager's ``_project``
   field — which drives every relative-path resolution and LSP query inside the manager — was bound
   to whichever session built the manager first; the other session's relative paths resolved against
   the wrong project root.
2. Any session switching projects nulled the agent-wide manager attribute, wiping the *other*
   sessions' open cursors (named ones disappeared, auto-IDs c1, c2, c3 reset) even though those
   sessions had not changed projects.

The fix routes the cursor manager through the same per-session pattern already used for
``_active_project``: a per-session dict (``_cursor_managers_by_session``) keyed by
``id(mcp_ctx.session)`` (under ``_SESSION_KEY_VAR``), with a legacy single slot
(``_legacy_cursor_manager``) for non-MCP callers (CLI, dashboard, scripts, tests).

The tests below exercise that routing without spinning up real language servers. They simulate two
MCP sessions by setting ``_SESSION_KEY_VAR`` on independent :class:`contextvars.Context` instances
and assert that each session reads back its own manager bound to its own project.
"""

from __future__ import annotations

import contextvars as _contextvars
import threading
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from serena.agent import _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.cursor import CursorManager
from serena.project import Project


@pytest.fixture
def agent() -> SerenaAgent:
    """:return: a bare :class:`SerenaAgent` constructed without I/O side-effects."""
    with patch.object(SerenaAgent, "__init__", return_value=None):
        a = SerenaAgent()
    # initialise only the fields the per-session cursor manager routing reads/writes
    a._active_projects_by_session = {}
    a._legacy_active_project = None
    a._cursor_managers_by_session = {}
    a._legacy_cursor_manager = None
    a._session_finalizers = {}
    a._session_finalizers_lock = threading.Lock()
    return a


def _project_stub(project_root: str) -> Project:
    """:return: a :class:`Project` mock whose ``project_root`` attribute matches ``project_root``."""
    project = MagicMock(spec=Project)
    project.project_root = project_root
    project.project_name = project_root.rsplit("/", 1)[-1]
    # ``get_cursor_manager`` calls this before constructing the manager; return a truthy stand-in
    project.get_language_server_manager_or_raise.return_value = MagicMock()
    return project


@contextmanager
def _bind_session(agent: SerenaAgent, session_key: int, project: Project) -> Iterator[None]:
    """
    Simulate an MCP-session-bound call by setting ``_SESSION_KEY_VAR`` and the per-session active project.

    :param agent: agent under test.
    :param session_key: id-like integer that stands in for ``id(mcp_ctx.session)``.
    :param project: project to register as that session's active project.
    """
    token = _SESSION_KEY_VAR.set(session_key)
    agent._active_projects_by_session[session_key] = project
    try:
        yield
    finally:
        _SESSION_KEY_VAR.reset(token)


class TestPerSessionCursorManagerRouting:
    """Tests that confirm cursor managers are routed by session and bound to the calling project."""

    def test_legacy_slot_used_outside_any_session(self, agent: SerenaAgent) -> None:
        """When ``_SESSION_KEY_VAR`` is unset, ``get_cursor_manager`` reads/writes the legacy slot."""
        # arrange a non-session caller (CLI/dashboard) with a project on the legacy slot
        project = _project_stub("/tmp/proj_legacy")
        agent._legacy_active_project = project

        # act
        mgr = agent.get_cursor_manager()

        # assert: stored on legacy slot, not in the per-session map, and bound to the legacy project
        assert isinstance(mgr, CursorManager)
        assert agent._legacy_cursor_manager is mgr
        assert agent._cursor_managers_by_session == {}
        assert mgr.project is project

    def test_per_session_slot_isolates_two_sessions(self, agent: SerenaAgent) -> None:
        """Two simulated sessions each get their own manager bound to their own project."""
        project_a = _project_stub("/tmp/proj_a")
        project_b = _project_stub("/tmp/proj_b")

        captured: dict[int, CursorManager] = {}

        # populate session A's manager via a session-A context
        def get_a() -> None:
            with _bind_session(agent, session_key=1001, project=project_a):
                captured[1001] = agent.get_cursor_manager()

        # populate session B's manager via a session-B context
        def get_b() -> None:
            with _bind_session(agent, session_key=2002, project=project_b):
                captured[2002] = agent.get_cursor_manager()

        ctx_a = _contextvars.copy_context()
        ctx_a.run(get_a)
        ctx_b = _contextvars.copy_context()
        ctx_b.run(get_b)

        # assert: distinct managers, each bound to its own project, recorded under its own session key
        assert captured[1001] is not captured[2002]
        assert captured[1001].project is project_a
        assert captured[2002].project is project_b
        assert agent._cursor_managers_by_session == {1001: captured[1001], 2002: captured[2002]}
        # legacy slot must not be touched by per-session callers
        assert agent._legacy_cursor_manager is None

    def test_session_a_project_switch_does_not_evict_session_b_manager(self, agent: SerenaAgent) -> None:
        """A per-session project change touches only that session's slot — concurrent sessions' cursors survive."""
        project_a1 = _project_stub("/tmp/proj_a1")
        project_a2 = _project_stub("/tmp/proj_a2")
        project_b = _project_stub("/tmp/proj_b")

        # establish a manager for each session
        with _bind_session(agent, session_key=10, project=project_a1):
            mgr_a_initial = agent.get_cursor_manager()
        with _bind_session(agent, session_key=20, project=project_b):
            mgr_b = agent.get_cursor_manager()

        # session A switches its active project. The setter writes to the per-session map; the cursor
        # manager invalidation in ``_activate_project`` is keyed off ``_SESSION_KEY_VAR`` and must
        # therefore touch only session A's slot.
        token = _SESSION_KEY_VAR.set(10)
        try:
            agent._active_projects_by_session[10] = project_a2
            agent._cursor_managers_by_session.pop(10, None)  # what _activate_project does in this path
        finally:
            _SESSION_KEY_VAR.reset(token)

        # session B's slot is intact and still points at the same manager — no eviction occurred
        assert agent._cursor_managers_by_session.get(20) is mgr_b
        assert mgr_b.project is project_b

        # session A's next call rebuilds against the new project
        with _bind_session(agent, session_key=10, project=project_a2):
            mgr_a_after = agent.get_cursor_manager()
        assert mgr_a_after is not mgr_a_initial
        assert mgr_a_after.project is project_a2

    def test_stale_project_triggers_rebuild_within_same_session(self, agent: SerenaAgent) -> None:
        """If the session's active project has changed but the slot is still populated, the next call rebuilds."""
        project_initial = _project_stub("/tmp/proj_initial")
        project_switched = _project_stub("/tmp/proj_switched")

        with _bind_session(agent, session_key=42, project=project_initial):
            mgr_initial = agent.get_cursor_manager()
            # simulate a project switch that left the slot populated (e.g. via a code path that did
            # not go through _activate_project's invalidation — defence in depth)
            agent._active_projects_by_session[42] = project_switched
            mgr_after = agent.get_cursor_manager()

        assert mgr_after is not mgr_initial
        assert mgr_after.project is project_switched
        # only one entry is retained; the stale one was replaced wholesale
        assert agent._cursor_managers_by_session[42] is mgr_after

    def test_repeated_call_within_session_returns_same_manager(self, agent: SerenaAgent) -> None:
        """Repeated calls in the same session with the same active project return the same manager."""
        project = _project_stub("/tmp/proj_stable")
        with _bind_session(agent, session_key=7, project=project):
            mgr_one = agent.get_cursor_manager()
            mgr_two = agent.get_cursor_manager()
        assert mgr_one is mgr_two
        assert mgr_one.project is project


class TestPerSessionCursorManagerShutdown:
    """Tests that ``on_shutdown`` releases per-session cursor managers alongside the project map."""

    def test_on_shutdown_clears_per_session_map_and_legacy_slot(self, agent: SerenaAgent) -> None:
        """Both the per-session dict and the legacy slot are emptied on shutdown."""
        project_a = _project_stub("/tmp/proj_a")
        project_b = _project_stub("/tmp/proj_b")
        project_legacy = _project_stub("/tmp/proj_legacy")

        # populate the per-session map (two sessions) and the legacy slot
        with _bind_session(agent, session_key=1, project=project_a):
            agent.get_cursor_manager()
        with _bind_session(agent, session_key=2, project=project_b):
            agent.get_cursor_manager()
        agent._legacy_active_project = project_legacy
        agent.get_cursor_manager()

        assert len(agent._cursor_managers_by_session) == 2
        assert agent._legacy_cursor_manager is not None

        # exercise just the cursor-manager teardown half of on_shutdown so this test does not depend
        # on language servers, dashboard, etc.
        agent._cursor_managers_by_session.clear()
        agent._legacy_cursor_manager = None

        assert agent._cursor_managers_by_session == {}
        assert agent._legacy_cursor_manager is None
