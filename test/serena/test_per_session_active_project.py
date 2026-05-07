"""
Regression tests for per-MCP-session active_project routing in :class:`SerenaAgent`.

The MCP daemon constructs a single :class:`SerenaAgent` per process and shares it across every
connected client. Before the fix, ``SerenaAgent._active_project`` was a single instance slot, so two
concurrent Claude Code sessions racing on it would clobber each other -- session A activates project
X, session B activates project Y, and A's next call resolves paths against Y, producing
``FileNotFoundError`` on cross-project paths.

The fix routes ``_active_project`` through a property whose getter consults
``_ACTIVE_PROJECT_VAR`` (an in-flight :class:`contextvars.ContextVar` override) then a per-session
dict keyed by ``id(mcp_ctx.session)`` (under ``_SESSION_KEY_VAR``), and finally falls back to a
legacy single slot for non-MCP callers (CLI, dashboard, scripts, tests). The setter writes to the
per-session dict when a session is in scope and to the legacy slot otherwise.

The tests below exercise that routing without spinning up language servers or MCP transports: they
simulate two MCP sessions by setting ``_SESSION_KEY_VAR`` on independent
:class:`contextvars.Context` instances and assert that each session reads back its own project.
"""

import contextvars
import threading
from unittest.mock import MagicMock

import pytest

from serena.agent import _ACTIVE_PROJECT_VAR, _MCP_CALL_IN_FLIGHT, _SESSION_KEY_VAR, _UNSET, SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.project import Project


@pytest.fixture
def agent() -> SerenaAgent:
    """Build a minimal :class:`SerenaAgent` with no active project for routing tests."""
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    return SerenaAgent(serena_config=config)


def _project_stub(name: str) -> Project:
    """Return a :class:`Project`-typed mock that the routing layer can store and retrieve."""
    project = MagicMock(spec=Project)
    project.project_name = name
    project.project_root = f"/tmp/{name}"
    return project


class TestPerSessionActiveProject:
    """Verify that the active project is routed by MCP session and never clobbered cross-session."""

    def test_legacy_slot_used_outside_any_session(self, agent: SerenaAgent) -> None:
        """No session in scope: the setter writes to the legacy slot and the getter reads it back."""
        project = _project_stub("legacy")

        agent._active_project = project

        assert agent._legacy_active_project is project
        assert agent._active_projects_by_session == {}
        assert agent.get_active_project() is project

    def test_per_session_slot_isolates_two_sessions(self, agent: SerenaAgent) -> None:
        """Two sessions writing to the same agent must not see each other's project."""
        project_a = _project_stub("alpha")
        project_b = _project_stub("beta")

        # session A activates project_a in its own ContextVar context
        ctx_a = contextvars.copy_context()

        def activate_a() -> None:
            _SESSION_KEY_VAR.set(1001)
            agent._active_project = project_a

        ctx_a.run(activate_a)

        # session B activates project_b in a separate ContextVar context
        ctx_b = contextvars.copy_context()

        def activate_b() -> None:
            _SESSION_KEY_VAR.set(2002)
            agent._active_project = project_b

        ctx_b.run(activate_b)

        # routing went to the per-session map, not the legacy slot
        assert agent._legacy_active_project is None
        assert agent._active_projects_by_session == {1001: project_a, 2002: project_b}

        # session A reads back project_a; session B reads back project_b -- no contamination
        def read_in(session_key: int) -> Project | None:
            ctx = contextvars.copy_context()

            def _read() -> Project | None:
                _SESSION_KEY_VAR.set(session_key)
                return agent.get_active_project()

            return ctx.run(_read)

        assert read_in(1001) is project_a
        assert read_in(2002) is project_b

    def test_session_a_mutation_does_not_affect_session_b(self, agent: SerenaAgent) -> None:
        """Re-activating in session A must leave session B's view unchanged."""
        project_a1 = _project_stub("alpha-v1")
        project_a2 = _project_stub("alpha-v2")
        project_b = _project_stub("beta")

        def write_in(session_key: int, project: Project) -> None:
            ctx = contextvars.copy_context()

            def _write() -> None:
                _SESSION_KEY_VAR.set(session_key)
                agent._active_project = project

            ctx.run(_write)

        def read_in(session_key: int) -> Project | None:
            ctx = contextvars.copy_context()

            def _read() -> Project | None:
                _SESSION_KEY_VAR.set(session_key)
                return agent.get_active_project()

            return ctx.run(_read)

        # initial activations
        write_in(1, project_a1)
        write_in(2, project_b)
        assert read_in(1) is project_a1
        assert read_in(2) is project_b

        # session A switches projects; session B must be unaffected
        write_in(1, project_a2)
        assert read_in(1) is project_a2
        assert read_in(2) is project_b

    def test_threading_thread_does_not_inherit_contextvars(self, agent: SerenaAgent) -> None:
        """Verify the architectural premise the fix is built on.

        ``threading.Thread`` (which Serena's :class:`TaskExecutor` uses to run tools) does NOT
        inherit the caller's :class:`contextvars.ContextVar` values. The fix therefore sets the
        ContextVars *inside* the worker-thread closure (``Tool.apply_ex.task``), not in the calling
        thread. If this premise ever changes (e.g. Python adds ContextVar inheritance to
        ``Thread``), the fix can be simplified -- but until then, the test guards against silent
        regression.
        """
        project = _project_stub("guard")
        agent._active_projects_by_session[42] = project

        observed: list[Project | None] = []

        def worker() -> None:
            # ``_SESSION_KEY_VAR`` was NOT set inside this thread, so the getter must NOT see the
            # caller's session key; the per-session map entry above must be invisible.
            observed.append(agent.get_active_project())

        # set the caller's session key, then spawn a thread; the thread should see neither the
        # caller's session key nor (transitively) the per-session project entry
        _SESSION_KEY_VAR.set(42)
        try:
            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=5)
        finally:
            _SESSION_KEY_VAR.set(None)

        assert observed == [None], "threading.Thread must not inherit ContextVars; if it does, revisit Tool.apply_ex"

    def test_active_project_context_does_not_leak_to_persistent_state(self, agent: SerenaAgent) -> None:
        """``active_project_context`` must use the in-flight ContextVar, not the per-session map."""
        persistent = _project_stub("persistent")
        temporary = _project_stub("temporary")

        ctx = contextvars.copy_context()

        def scenario() -> None:
            _SESSION_KEY_VAR.set(7)
            agent._active_project = persistent
            assert agent.get_active_project() is persistent

            with agent.active_project_context(temporary):
                # inside the with block, the override wins
                assert agent.get_active_project() is temporary

            # after the with block, the per-session slot is restored unchanged
            assert agent.get_active_project() is persistent
            assert agent._active_projects_by_session[7] is persistent

        ctx.run(scenario)

    def test_setter_with_none_clears_per_session_entry(self, agent: SerenaAgent) -> None:
        """Setting ``_active_project = None`` inside a session must remove that session's entry."""
        project = _project_stub("ephemeral")

        ctx = contextvars.copy_context()

        def scenario() -> None:
            _SESSION_KEY_VAR.set(99)
            agent._active_project = project
            assert agent._active_projects_by_session == {99: project}

            agent._active_project = None
            assert agent._active_projects_by_session == {}
            assert agent.get_active_project() is None

        ctx.run(scenario)

    def test_default_contextvar_values(self) -> None:
        """The module-level ContextVars must have the documented defaults."""
        assert _SESSION_KEY_VAR.get() is None
        assert _SESSION_KEY_VAR.get() is None
        assert _ACTIVE_PROJECT_VAR.get() is _UNSET
        assert _MCP_CALL_IN_FLIGHT.get() is False

    def test_legacy_slot_unreachable_when_mcp_call_in_flight(self, agent: SerenaAgent) -> None:
        """IRONCLAD zero-crossover: while an MCP call is in flight, a per-session-map miss must
        return None and NEVER fall through to ``_legacy_active_project``. Returning the legacy
        value would surface a sibling client's project to this caller — exactly the cross-project
        confusion the design forbids.
        """
        sibling_project = _project_stub("sibling-client-project")
        agent._legacy_active_project = sibling_project

        ctx = contextvars.copy_context()

        def scenario() -> Project | None:
            # simulate Tool.apply_ex's worker-thread context for an MCP call whose per-session map
            # is empty (e.g. transport churn evicted it before the next request arrived).
            _MCP_CALL_IN_FLIGHT.set(True)
            _SESSION_KEY_VAR.set(31415)
            assert agent._active_projects_by_session.get(31415) is None  # confirm the miss
            return agent.get_active_project()

        observed = ctx.run(scenario)
        assert observed is None, (
            "MCP call must NOT see another client's project via the legacy slot. "
            f"Got {observed!r} from _legacy_active_project={sibling_project!r}."
        )
        # the legacy slot is intact — we did not consult it, did not modify it
        assert agent._legacy_active_project is sibling_project

    def test_legacy_cursor_manager_unreachable_when_mcp_call_in_flight(self, agent: SerenaAgent) -> None:
        """IRONCLAD zero-crossover: get_cursor_manager must refuse to read or write
        _legacy_cursor_manager while an MCP call is in flight; if the per-session map miss happens
        with no session_key set under MCP, that's a programming error and must raise rather than
        silently fall through.
        """
        ctx = contextvars.copy_context()

        def scenario() -> None:
            _MCP_CALL_IN_FLIGHT.set(True)
            # _SESSION_KEY_VAR deliberately unset to simulate a propagation bug — the guard must trip
            with pytest.raises(RuntimeError, match="MCP call is in flight but _SESSION_KEY_VAR is unset"):
                agent.get_cursor_manager()

        ctx.run(scenario)

    def test_two_concurrent_clients_never_cross(self, agent: SerenaAgent) -> None:
        """End-to-end zero-crossover: two threads simulating two simultaneous MCP clients each
        activate a different project and read it back many times in interleaved fashion. Each
        client must see ONLY its own project across the entire interleaving — never the other's,
        never None, never the legacy slot's value.
        """
        project_a = _project_stub("client-a-project")
        project_b = _project_stub("client-b-project")
        # set the legacy slot to a recognisable third value; if it ever leaks through, the test fails
        sibling = _project_stub("legacy-sibling-leak")
        agent._legacy_active_project = sibling

        observations_a: list[Project | None] = []
        observations_b: list[Project | None] = []

        def client(session_key: int, project: Project, observations: list[Project | None]) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _SESSION_KEY_VAR.set(session_key)
                agent._active_project = project
                for _ in range(100):
                    observations.append(agent.get_active_project())

            ctx.run(run)

        thread_a = threading.Thread(target=client, args=(11, project_a, observations_a))
        thread_b = threading.Thread(target=client, args=(22, project_b, observations_b))
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

        assert all(p is project_a for p in observations_a), (
            f"Client A saw something other than its own project: distinct values = "
            f"{set(id(p) for p in observations_a)}"
        )
        assert all(p is project_b for p in observations_b), (
            f"Client B saw something other than its own project: distinct values = "
            f"{set(id(p) for p in observations_b)}"
        )
        # the sibling/legacy value never leaked to anyone
        assert sibling not in observations_a and sibling not in observations_b


class TestShutdownAcrossSessions:
    """Verify ``on_shutdown`` tears down every project held in any slot."""

    def test_on_shutdown_iterates_per_session_map_and_legacy_slot(self, agent: SerenaAgent) -> None:
        """Both per-session entries and the legacy fallback must have ``shutdown`` called once each."""
        project_a = _project_stub("a")
        project_b = _project_stub("b")
        project_legacy = _project_stub("legacy")

        agent._active_projects_by_session = {1: project_a, 2: project_b}
        agent._legacy_active_project = project_legacy

        agent.on_shutdown(timeout=0.1)

        project_a.shutdown.assert_called_once_with(timeout=0.1)
        project_b.shutdown.assert_called_once_with(timeout=0.1)
        project_legacy.shutdown.assert_called_once_with(timeout=0.1)
        assert agent._active_projects_by_session == {}
        assert agent._legacy_active_project is None

    def test_on_shutdown_dedupes_by_project_root(self, agent: SerenaAgent) -> None:
        """If two sessions share the same Project (by project_root), shutdown must run once."""
        shared = _project_stub("shared")
        unique = _project_stub("unique")

        agent._active_projects_by_session = {1: shared, 2: shared, 3: unique}
        agent._legacy_active_project = shared  # same project_root as the per-session entries

        agent.on_shutdown(timeout=0.1)

        # shared's shutdown runs exactly once across all four references
        assert shared.shutdown.call_count == 1
        assert unique.shutdown.call_count == 1
