"""
Regression tests for per-MCP-session GC-driven eviction in :class:`SerenaAgent`.

Before this fix, ``_active_projects_by_session`` and ``_cursor_managers_by_session`` accumulated an
entry for every MCP session that ever connected: nothing dropped them when the session terminated.
That meant (a) a memory leak proportional to total session count, and (b) ``id()`` reuse — once the
original session object was garbage-collected, a future session's :meth:`id` could collide with the
key of a dead session and inherit its stale state.

The fix: ``Tool.apply_ex`` registers a ``weakref.finalize`` on ``mcp_ctx.session`` the first time a
tool runs from that session. When the session is GC'd, the finalizer pops both per-session dicts.

These tests verify the eviction behavior end-to-end without requiring a live MCP transport.
"""

from __future__ import annotations

import gc
import weakref
from unittest.mock import MagicMock

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.project import Project


class _FakeSession:
    """Minimal stand-in for ``mcp.server.session.ServerSession`` — weakrefable by default."""


@pytest.fixture
def agent() -> SerenaAgent:
    """Build a minimal :class:`SerenaAgent` with no active project for finalizer tests."""
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    return SerenaAgent(serena_config=config)


def _project_stub(name: str) -> Project:
    """Return a :class:`Project`-typed mock the routing layer can store and retrieve."""
    project = MagicMock(spec=Project)
    project.project_name = name
    project.project_root = f"/tmp/{name}"
    return project


class TestSessionFinalizerEviction:
    """Verify per-session entries are evicted when the MCP session object is GC'd."""

    def test_finalizer_drops_active_project_entry_on_session_gc(self, agent: SerenaAgent) -> None:
        """When the session is collected, its ``_active_projects_by_session`` entry must vanish."""
        session = _FakeSession()
        session_key = id(session)
        project = _project_stub("p1")

        agent._register_session_finalizer(session, session_key)
        agent._active_projects_by_session[session_key] = project

        # invariants while the session is alive
        assert agent._active_projects_by_session[session_key] is project
        assert session_key in agent._session_finalizers

        # drop the only strong reference and force GC; CPython refcounting collects it,
        # gc.collect() backstops free-threaded / cycle-affected interpreters.
        del session
        gc.collect()

        assert session_key not in agent._active_projects_by_session
        assert session_key not in agent._session_finalizers

    def test_finalizer_drops_cursor_manager_entry_on_session_gc(self, agent: SerenaAgent) -> None:
        """When the session is collected, its ``_cursor_managers_by_session`` entry must vanish."""
        session = _FakeSession()
        session_key = id(session)
        cursor_mgr = MagicMock(name="cursor_manager")

        agent._register_session_finalizer(session, session_key)
        agent._cursor_managers_by_session[session_key] = cursor_mgr

        del session
        gc.collect()

        assert session_key not in agent._cursor_managers_by_session
        assert session_key not in agent._session_finalizers

    def test_finalizer_evicts_both_dicts_in_one_pass(self, agent: SerenaAgent) -> None:
        """A single GC event clears active-project AND cursor-manager entries together."""
        session = _FakeSession()
        session_key = id(session)
        project = _project_stub("both")
        cursor_mgr = MagicMock(name="cursor_manager")

        agent._register_session_finalizer(session, session_key)
        agent._active_projects_by_session[session_key] = project
        agent._cursor_managers_by_session[session_key] = cursor_mgr

        del session
        gc.collect()

        assert agent._active_projects_by_session == {}
        assert agent._cursor_managers_by_session == {}
        assert agent._session_finalizers == {}

    def test_registration_is_idempotent_per_session(self, agent: SerenaAgent) -> None:
        """Repeated ``apply_ex`` calls from the same session must not register multiple finalizers."""
        session = _FakeSession()
        session_key = id(session)

        for _ in range(10):
            agent._register_session_finalizer(session, session_key)

        assert len(agent._session_finalizers) == 1
        assert session_key in agent._session_finalizers

    def test_two_sessions_register_independent_finalizers(self, agent: SerenaAgent) -> None:
        """Two live sessions get two finalizers; collecting one does not affect the other."""
        session_a = _FakeSession()
        session_b = _FakeSession()
        key_a, key_b = id(session_a), id(session_b)
        project_a, project_b = _project_stub("a"), _project_stub("b")

        agent._register_session_finalizer(session_a, key_a)
        agent._register_session_finalizer(session_b, key_b)
        agent._active_projects_by_session[key_a] = project_a
        agent._active_projects_by_session[key_b] = project_b

        del session_a
        gc.collect()

        assert key_a not in agent._active_projects_by_session
        assert key_a not in agent._session_finalizers
        assert agent._active_projects_by_session[key_b] is project_b
        assert key_b in agent._session_finalizers

    def test_session_id_reuse_does_not_inherit_dead_state(self, agent: SerenaAgent) -> None:
        """After GC + ``id()`` reuse, the new session must start with no inherited state.

        Python may reuse the address of a freed object for a later allocation. Without finalizer-
        driven eviction, the new session would silently inherit ``_active_projects_by_session[id]``
        from the dead one. With the fix, the entry is gone before the new session can land.
        """
        first = _FakeSession()
        first_key = id(first)
        agent._register_session_finalizer(first, first_key)
        agent._active_projects_by_session[first_key] = _project_stub("first")

        del first
        gc.collect()
        assert first_key not in agent._active_projects_by_session

        # simulate a fresh session arriving — even if its id() collides, no stale state shows up.
        second = _FakeSession()
        second_key = id(second)
        agent._register_session_finalizer(second, second_key)
        # the freshly-registered session must see an empty slot regardless of id() collision
        assert second_key not in agent._active_projects_by_session

    def test_on_shutdown_detaches_finalizers(self, agent: SerenaAgent) -> None:
        """After ``on_shutdown``, leftover finalizers must not fire (they would be no-ops anyway,
        but detaching avoids spurious GC-thread work and log noise)."""
        session = _FakeSession()
        session_key = id(session)
        agent._register_session_finalizer(session, session_key)
        finalizer = agent._session_finalizers[session_key]
        assert finalizer.alive

        agent.on_shutdown(timeout=0.1)
        # detach() marks finalize objects dead; weakref.finalize.alive becomes False
        assert not finalizer.alive
        assert agent._session_finalizers == {}

        # After shutdown, GC of the session must not raise or repopulate state
        del session
        gc.collect()
        assert agent._active_projects_by_session == {}
        assert agent._cursor_managers_by_session == {}

    def test_unweakrefable_session_is_handled_gracefully(self, agent: SerenaAgent) -> None:
        """``object()`` cannot accept weak references — registration must skip without raising."""
        plain = object()
        # this would raise TypeError without the try/except in _register_session_finalizer
        agent._register_session_finalizer(plain, id(plain))
        assert agent._session_finalizers == {}
