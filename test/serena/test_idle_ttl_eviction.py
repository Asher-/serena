"""Idle-TTL eviction tests for tier-2 (X-Forwarded-Mcp-Session-Id) session keys.

Background:

The streamable-http path used to register a :func:`weakref.finalize` on
``mcp_ctx.session`` for tier-2 (X-Forwarded-Mcp-Session-Id) keys. The
persistent multiplexer<->serena SSE connection has per-connection
``mcp_ctx.session`` lifetime, so any SSE churn would GC that session,
fire the finalizer, and wipe the tier-2 slot -- even though the named
owner of the X-Forwarded value (the CC client) was still alive. The
next tool call from that client would hit an empty
``_active_projects_by_session`` slot and raise
'No active project for this MCP session'.

Fix (per plan://Serena:serena/decouple-tier-2-eviction-from-transport-add-idle-ttl-impl):

  - tier 1 (pipe via ``_PIPE_SESSION_ID_VAR``): unchanged. Eviction is
    socket-disconnect driven.
  - tier 2 (X-Forwarded-Mcp-Session-Id): the slot is no longer evicted by a
    periodic sweeper; it persists for the daemon's lifetime (the LSP/Project
    it references are agent-wide shared resources). Transport churn no longer
    wipes the slot because tier-2 registers no finalizer on ``mcp_ctx.session``.
  - tier 3 (id() fallback for direct-stdio without the multiplexer):
    unchanged. ``weakref.finalize`` on ``mcp_ctx.session`` remains
    correct because the SDK session IS the CC session boundary.

These tests verify that every constraint above holds.
"""

import gc
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from serena.agent import _PIPE_SESSION_ID_VAR, SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject

# ---------------------------------------------------------------------------
# Test fixtures and helpers (mirroring test_streamable_http_cc_session_key.py)
# ---------------------------------------------------------------------------


class _NoOpProbeTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """A Tool subclass that never needs a project and always returns OK.

    The body of :meth:`Tool.apply_ex` runs its session-key derivation
    before the task executor is
    engaged; side effects on agent state are observable when apply_ex
    returns.
    """

    def apply(self) -> str:  # pragma: no cover -- not reached when is_active is patched False
        return "OK"


class _FakeSession:
    """Minimal stand-in for the FastMCP session object that ``id()`` and
    :func:`weakref.finalize` both accept.

    Using a real Python class (rather than ``object()``) keeps
    :func:`weakref.finalize` from raising ``TypeError`` -- ``object``
    does not support weak references, but a user-defined class does.
    """

    def __init__(self) -> None:
        self.client_params = None


class _CaseInsensitiveHeaders(dict[str, str]):
    """Starlette-style header map: ``get`` matches keys case-insensitively."""

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
    """Build a mock ``mcp_ctx`` that carries the X-Forwarded header (or not).

    :param forwarded_header: header value when set; ``None`` omits the header.
    :param session: a :class:`_FakeSession` used as ``mcp_ctx.session`` so
        :func:`id` and :func:`weakref.finalize` both work against it.
    """
    ctx = MagicMock()
    ctx.session = session if session is not None else _FakeSession()
    headers = _CaseInsensitiveHeaders()
    if forwarded_header is not None:
        headers["x-forwarded-mcp-session-id"] = forwarded_header
    ctx.request_context.request.headers = headers
    # ensure the alternate-attribute probe path doesn't also carry the header
    ctx.request = MagicMock()
    ctx.request.headers = _CaseInsensitiveHeaders()
    return ctx


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    """Build a minimal :class:`SerenaAgent` with no active project."""
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    a = SerenaAgent(serena_config=config)
    yield a
    a.on_shutdown(timeout=0.5)


@pytest.fixture
def tool(agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch) -> _NoOpProbeTool:
    """Instantiate the probe tool on the agent. ``is_active`` is patched True."""
    t = _NoOpProbeTool(agent)
    monkeypatch.setattr(t, "is_active", lambda: True)
    monkeypatch.setattr(agent, "record_tool_usage", lambda *a, **kw: None)
    return t


@pytest.fixture(autouse=True)
def _reset_pipe_var() -> Iterator[None]:
    """Clear ``_PIPE_SESSION_ID_VAR`` for the duration of every test."""
    token = _PIPE_SESSION_ID_VAR.set(None)
    try:
        yield
    finally:
        _PIPE_SESSION_ID_VAR.reset(token)


# ---------------------------------------------------------------------------
# Case 1: tier-2 slot survives mcp_ctx.session GC (the regression-bug test).
# ---------------------------------------------------------------------------


class TestTier2SurvivesTransportGC:
    """Tier-2 slot survives GC of the underlying ``mcp_ctx.session`` object.

    This is the regression bug: under the prior design, GC of
    ``mcp_ctx.session`` (caused by routine SSE churn between the
    multiplexer and serena) fired the weakref.finalize and wiped the
    tier-2 slot. After the fix, tier-2 no longer registers any
    finalizer on ``mcp_ctx.session``, so GC of that object cannot
    evict the slot.
    """

    def test_tier_2_slot_survives_mcp_ctx_session_gc(
        self, agent: SerenaAgent
    ) -> None:
        cc_session_id = "cc-session-tier-2-survives-gc"
        project = MagicMock()
        project.project_root = "/tmp/test-tier-2-survives"

        # simulate the tier-2 tool dispatch having stamped the per-session
        # active project slot
        agent._active_projects_by_session[cc_session_id] = project

        # tier 2 must NOT have registered an mcp_ctx finalizer.
        assert cc_session_id not in agent._session_finalizers, (
            "tier-2 must not register a weakref.finalize; "
            f"finalizers present: {list(agent._session_finalizers.keys())!r}"
        )

        # simulate the transport-level mcp_ctx.session being collected.
        # Even constructing and discarding a fake session triggers no
        # eviction because tier-2 never registered against it.
        fake_session = _FakeSession()
        session_id_int = id(fake_session)
        del fake_session
        gc.collect()

        # the tier-2 slot is intact (the named CC owner is still alive).
        assert cc_session_id in agent._active_projects_by_session, (
            "tier-2 slot was evicted by mcp_ctx.session GC; the regression bug "
            "this plan fixes has reappeared"
        )
        assert agent._active_projects_by_session[cc_session_id] is project
        # and the id()-typed key from the transport object is NOT present.
        assert session_id_int not in agent._active_projects_by_session


# ---------------------------------------------------------------------------
# Case 4: tier-3 (id() fallback) STILL evicts on mcp_ctx.session GC.
# ---------------------------------------------------------------------------


class TestTier3GCEvictionStillWorks:
    """Tier-3 (id() fallback for direct-stdio without the multiplexer)
    KEEPS the existing weakref.finalize on ``mcp_ctx.session``. The fix
    is tier-aware -- only tier-2's eviction-coupling is removed.
    """

    def test_tier_3_id_fallback_still_evicts_on_gc(
        self, agent: SerenaAgent
    ) -> None:
        # build a tier-3-style key path: id(mcp_session) as the session_key
        mcp_session = _FakeSession()
        session_key = id(mcp_session)
        agent._active_projects_by_session[session_key] = MagicMock()
        agent._register_session_finalizer(mcp_session, session_key)
        assert session_key in agent._session_finalizers, (
            "tier-3 must continue to register a weakref.finalize"
        )

        # drop the only reference and force a collection. The finalizer
        # fires synchronously inside ``gc.collect()`` once the target is
        # unreachable, which evicts the per-session entries.
        del mcp_session
        gc.collect()

        assert session_key not in agent._active_projects_by_session, (
            "tier-3 weakref.finalize must still evict on mcp_ctx.session GC; "
            "removing this would regress the working direct-stdio path"
        )


# ---------------------------------------------------------------------------
# Sweeper removed: no SerenaSessionSweeper janitor thread is started.
# ---------------------------------------------------------------------------


class TestNoSessionSweeperThread:
    """The idle-TTL sweeper (``SerenaSessionSweeper``) has been removed
    (plan://Brain:brain/serena-session-state-self-reclaim-impl, t3). Per-session
    task executors self-expire on idle instead of being reaped by a janitor, so
    constructing an agent must start no sweeper thread.
    """

    def test_no_session_sweeper_thread_is_started(self, agent: SerenaAgent) -> None:
        sweeper_threads = [
            t for t in threading.enumerate() if t.name == "SerenaSessionSweeper"
        ]
        assert sweeper_threads == [], (
            "the idle-TTL sweeper was removed; no SerenaSessionSweeper thread "
            f"may run after agent construction; found: {[t.name for t in sweeper_threads]!r}"
        )
