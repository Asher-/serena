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
    socket-disconnect driven. Pipe keys NEVER enter
    ``_session_last_touched``.
  - tier 2 (X-Forwarded-Mcp-Session-Id): eviction is now idle-TTL
    driven by :meth:`SerenaAgent._sweep_idle_sessions`, fed by
    :meth:`SerenaAgent._touch_session` calls from
    :meth:`Tool.apply_ex`. Transport churn no longer wipes the slot.
  - tier 3 (id() fallback for direct-stdio without the multiplexer):
    unchanged. ``weakref.finalize`` on ``mcp_ctx.session`` remains
    correct because the SDK session IS the CC session boundary.

These six tests verify that every constraint above holds.
"""

import contextvars
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
    (and ``_touch_session`` for tier-2) before the task executor is
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

        # simulate the tier-2 tool dispatch having stamped both the
        # last-touched timestamp and the per-session active project
        agent._touch_session(cc_session_id)
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
# Case 2: TTL-elapsed slots are evicted on sweep.
# ---------------------------------------------------------------------------


class TestTier2TtlEviction:
    """Tier-2 slots are evicted by :meth:`_sweep_idle_sessions` once
    ``time.monotonic() - last_touched > SESSION_IDLE_TTL_SECONDS``.
    """

    def test_tier_2_slot_evicted_after_ttl_elapses_plus_sweep(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cc_session_id = "cc-session-tier-2-ttl-evict"
        project = MagicMock()
        project.project_root = "/tmp/test-tier-2-ttl"
        agent._active_projects_by_session[cc_session_id] = project

        # touch at simulated t=0
        monkeypatch.setattr("serena.agent.time.monotonic", lambda: 0.0)
        agent._touch_session(cc_session_id)
        assert cc_session_id in agent._session_last_touched

        # advance the clock just past TTL and sweep
        ttl = agent.SESSION_IDLE_TTL_SECONDS
        monkeypatch.setattr(
            "serena.agent.time.monotonic", lambda: ttl + 1.0
        )
        agent._sweep_idle_sessions()

        assert cc_session_id not in agent._active_projects_by_session, (
            "TTL-cold slot should have been evicted by _sweep_idle_sessions; "
            f"keys still present: {list(agent._active_projects_by_session.keys())!r}"
        )
        assert cc_session_id not in agent._session_last_touched, (
            "TTL-cold slot's last-touched timestamp should also have been "
            f"dropped; keys still present: {list(agent._session_last_touched.keys())!r}"
        )


# ---------------------------------------------------------------------------
# Case 3: every apply_ex dispatch refreshes last-touched.
# ---------------------------------------------------------------------------


class TestTier2TouchRefresh:
    """:meth:`_touch_session` writes the current monotonic timestamp on
    every call, so an active CC client never goes TTL-cold.
    """

    def test_every_apply_ex_dispatch_refreshes_last_touched(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cc_session_id = "cc-session-tier-2-touch-refresh"

        # touch at t=0; record the stored timestamp
        clock = {"t": 0.0}
        monkeypatch.setattr("serena.agent.time.monotonic", lambda: clock["t"])
        agent._touch_session(cc_session_id)
        assert agent._session_last_touched[cc_session_id] == pytest.approx(0.0)

        # advance to t=3600 (half the default TTL of 7200s) and touch again;
        # the stored value must update to reflect the second touch.
        clock["t"] = 3600.0
        agent._touch_session(cc_session_id)
        assert agent._session_last_touched[cc_session_id] == pytest.approx(3600.0)

        # advance to t=3600 + TTL + 1 (so the gap since the last touch
        # exceeds TTL) and sweep; the slot is evicted.
        agent._active_projects_by_session[cc_session_id] = MagicMock()
        ttl = agent.SESSION_IDLE_TTL_SECONDS
        clock["t"] = 3600.0 + ttl + 1.0
        agent._sweep_idle_sessions()
        assert cc_session_id not in agent._active_projects_by_session
        assert cc_session_id not in agent._session_last_touched

        # now exercise continuous touching: touch at t=0, TTL/2, TTL, 3*TTL/2.
        # Re-populate the slot, then sweep at t=TTL+TTL/2 WITHOUT touching
        # in the final TTL/2 gap; the cold slot must be evicted since the
        # gap exceeds TTL.
        key2 = "cc-session-tier-2-touch-refresh-2"
        agent._active_projects_by_session[key2] = MagicMock()
        for t in (0.0, ttl / 2.0, ttl, 3.0 * ttl / 2.0):
            clock["t"] = t
            agent._touch_session(key2)
        # last touch was at 3*ttl/2; advance to 3*ttl/2 + ttl + 1.
        # The gap from last touch is exactly ttl+1, which exceeds TTL.
        clock["t"] = 3.0 * ttl / 2.0 + ttl + 1.0
        agent._sweep_idle_sessions()
        assert key2 not in agent._active_projects_by_session, (
            "after a gap longer than TTL, the slot must be evicted "
            "regardless of prior touch density"
        )


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
# Case 5: pipe-path keys NEVER enter _session_last_touched.
# ---------------------------------------------------------------------------


class TestPipeNeverTouched:
    """Pipe-path session keys (project_root strings) must NEVER enter
    :attr:`SerenaAgent._session_last_touched`. The sweeper must
    therefore never evict pipe-path slots; pipe eviction stays driven
    by socket disconnect (:meth:`SerenaAgent.evict_pipe_session`).
    """

    def test_pipe_path_session_keys_never_enter_session_last_touched(
        self, agent: SerenaAgent, tool: _NoOpProbeTool
    ) -> None:
        pipe_project_root = "/tmp/test-pipe-not-touched"

        # drive apply_ex through the pipe-tier path: _PIPE_SESSION_ID_VAR set,
        # X-Forwarded header also present (should be ignored), mcp_ctx given.
        ctx = _make_mcp_ctx(forwarded_header="should-be-ignored-by-tier-1")
        token = _PIPE_SESSION_ID_VAR.set(pipe_project_root)
        try:
            tool.apply_ex(mcp_ctx=ctx, log_call=False)
        finally:
            _PIPE_SESSION_ID_VAR.reset(token)

        # the pipe project_root must NOT have been stamped in last-touched
        assert pipe_project_root not in agent._session_last_touched, (
            "pipe-tier session_keys must never enter _session_last_touched; "
            f"keys present: {list(agent._session_last_touched.keys())!r}"
        )
        # the ignored header value must also not have been stamped
        assert "should-be-ignored-by-tier-1" not in agent._session_last_touched


# ---------------------------------------------------------------------------
# Case 6: concurrent touch + sweep is race-safe.
# ---------------------------------------------------------------------------


class TestConcurrentTouchSweepRaceSafe:
    """Concurrent threads calling :meth:`_touch_session` and
    :meth:`_sweep_idle_sessions` must not crash and must not wrongly
    evict an actively-touched slot.
    """

    def test_concurrent_touch_and_sweep_is_race_safe(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Use a generous TTL so that any session touched in the loop is
        # never TTL-cold; the test catches: (a) data-race crashes inside
        # touch or sweep, (b) wrongful eviction of warm slots.
        monkeypatch.setattr(agent, "SESSION_IDLE_TTL_SECONDS", 10.0)

        n_threads = 20
        run_duration = 0.5  # seconds of concurrent activity
        stop_event = threading.Event()
        keys = [f"sess-{i}" for i in range(n_threads)]

        # populate the active-projects map under each key so an
        # accidental sweep would evict observable state
        for key in keys:
            agent._active_projects_by_session[key] = MagicMock()

        errors: list[BaseException] = []

        def toucher(key: str) -> None:
            try:
                while not stop_event.is_set():
                    agent._touch_session(key)
                    # let other threads progress; busy spinning would
                    # starve the sweeper and mask races
                    threading.Event().wait(0.001)
            except BaseException as e:
                errors.append(e)

        def sweeper() -> None:
            try:
                while not stop_event.is_set():
                    agent._sweep_idle_sessions()
                    threading.Event().wait(0.001)
            except BaseException as e:
                errors.append(e)

        toucher_threads = [
            threading.Thread(target=toucher, args=(k,), name=f"toucher-{k}")
            for k in keys
        ]
        sweeper_thread = threading.Thread(target=sweeper, name="sweeper")

        for t in toucher_threads:
            t.start()
        sweeper_thread.start()

        threading.Event().wait(run_duration)
        stop_event.set()

        for t in toucher_threads:
            t.join(timeout=2.0)
        sweeper_thread.join(timeout=2.0)

        assert not errors, f"race-stress raised exceptions: {errors!r}"

        # every actively-touched slot survived: the sweeper never
        # falsely evicted a TTL-warm slot
        for key in keys:
            assert key in agent._active_projects_by_session, (
                f"actively-touched key {key!r} was wrongly evicted by sweeper; "
                "the TTL guard or the lock discipline is broken"
            )
            assert key in agent._session_last_touched
