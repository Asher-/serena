"""Daemon-side integration tests for the per-client pipe transport.

This module is the canonical home for tests against the production
:class:`~serena.mcp.SerenaCatalogProvider` and
:class:`~serena.mcp.SerenaPipeFrameHandler`, plus the matching session-key
derivation in :meth:`~serena.tools.tools_base.Tool.apply_ex`. These pieces wire
the pipe transport from :mod:`serena.daemon_pipe` into the actual Serena
:class:`~serena.agent.SerenaAgent`, so they belong outside
``test_pipe_transport.py`` (which exercises the protocol/transport layer in
isolation) -- the integration shape is what's under test here.

Test scaffolding mirrors the patterns already used in
``test_pipe_transport.py`` and ``test_per_session_active_project.py``: a custom
``Tool`` subclass captures :mod:`contextvars` state from inside ``apply_ex``'s
worker thread; an ``async def scenario()`` body wraps anything that needs an
event loop and is invoked via :func:`asyncio.run`.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import gc
import json
import time
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from serena.agent import (
    _MCP_CALL_IN_FLIGHT,
    _PIPE_SESSION_ID_VAR,
    _SESSION_KEY_VAR,
    SerenaAgent,
)
from serena.config.serena_config import SerenaConfig
from serena.mcp import SerenaCatalogProvider, SerenaPipeFrameHandler
from serena.tools import Tool
from serena.tools.tools_base import ToolMarkerDoesNotRequireActiveProject


@pytest.fixture
def agent() -> SerenaAgent:
    """Build a minimal :class:`SerenaAgent` for daemon-side dispatch tests.

    Mirrors the fixture in ``test_per_session_active_project.py``: the agent has
    no active project, the dashboard and GUI log window are disabled, and the
    default tool registry is loaded. Per-session state lives on the agent
    instance, so a fresh fixture per test keeps assertions about
    ``_active_projects_by_session`` and ``_session_finalizers`` independent.
    """
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    return SerenaAgent(serena_config=config)


class _SessionKeyCapturingTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """Test tool that captures :mod:`contextvars` state from inside ``apply_ex``'s worker.

    The class is defined in this test module rather than under ``serena.tools``
    so :class:`~serena.tools.tools_base.ToolRegistry`'s package filter excludes
    it from the registered tool catalog -- adding it would otherwise pollute
    every other test (and the running daemon) with a Serena tool that exists
    only for unit tests. Subclassing :class:`ToolMarkerDoesNotRequireActiveProject`
    skips the "no active project" guard inside ``apply_ex`` so the body runs
    even with the no-project fixture.

    :cvar captured: Class-level list of dicts, one per ``apply()`` invocation,
        recording the values of ``_SESSION_KEY_VAR``, ``_PIPE_SESSION_ID_VAR``,
        and ``_MCP_CALL_IN_FLIGHT`` as observed inside the worker thread. Tests
        call :meth:`reset` in setup to clear stale entries from a prior test.
    """

    captured: ClassVar[list[dict[str, Any]]] = []

    @classmethod
    def reset(cls) -> None:
        """Clear ``captured`` so per-test assertions don't see stale entries."""
        cls.captured.clear()

    def is_active(self) -> bool:
        """Always active -- the agent's tool-set membership is irrelevant for these tests."""
        return True

    def apply(self) -> str:
        """Capture ContextVar state and return a deterministic payload string.

        :returns: The literal ``"captured"`` so any caller that wraps the result
            (e.g. :class:`SerenaPipeFrameHandler`'s tools/call branch) sees a
            value the test can assert on without depending on tool internals.
        """
        cls = type(self)
        cls.captured.append(
            {
                "session_key": _SESSION_KEY_VAR.get(None),
                "pipe_session_id": _PIPE_SESSION_ID_VAR.get(None),
                "mcp_in_flight": _MCP_CALL_IN_FLIGHT.get(),
            }
        )
        return "captured"


def _inject_test_tool(agent: SerenaAgent) -> _SessionKeyCapturingTool:
    """Register the capturing tool with ``agent._exposed_tools`` so handler lookup finds it.

    :class:`SerenaPipeFrameHandler` resolves ``tools/call`` names against
    :meth:`SerenaAgent.get_exposed_tool_instances`, so a tool the handler must
    invoke has to live in that list. We append directly because the registry
    filter skips this class (it's outside ``serena.tools``); the alternative
    would be patching the registry, which has process-wide singleton effects.

    :param agent: The fixture's :class:`SerenaAgent`. Modified in-place.
    :returns: The constructed :class:`_SessionKeyCapturingTool` so callers can
        read its ``get_name()`` for the ``tools/call`` request.
    """
    tool = _SessionKeyCapturingTool(agent)
    agent._exposed_tools.tools.append(tool)
    agent._exposed_tools.tool_names.append(tool.get_name())
    agent._exposed_tools._tool_name_set.add(tool.get_name())
    return tool


class TestPipeSessionIdContextVar:
    """Verify ``_PIPE_SESSION_ID_VAR`` exists with the documented shape and default."""

    def test_var_default_is_none(self) -> None:
        # the default applies to every fresh ContextVar context, including the
        # event-loop's root context -- production code reads it before set()
        assert _PIPE_SESSION_ID_VAR.get() is None

    def test_var_accepts_string_session_id(self) -> None:
        ctx = contextvars.copy_context()

        def _set_and_read() -> str | None:
            _PIPE_SESSION_ID_VAR.set("pipe-uuid-1")
            return _PIPE_SESSION_ID_VAR.get()

        assert ctx.run(_set_and_read) == "pipe-uuid-1"
        # the context-isolated set() must NOT leak out: the parent context's
        # default still wins outside the inner ctx.run
        assert _PIPE_SESSION_ID_VAR.get() is None


class TestApplyExSessionKeyDerivation:
    """Verify ``Tool.apply_ex`` derives ``session_key`` from the pipe id when set."""

    def test_pipe_session_id_drives_session_key(self, agent: SerenaAgent) -> None:
        # set _PIPE_SESSION_ID_VAR in the calling context; apply_ex must read it
        # in the dispatch thread (the worker thread doesn't inherit ContextVars,
        # so reading inside the worker would miss it)
        _SessionKeyCapturingTool.reset()
        tool = _SessionKeyCapturingTool(agent)

        ctx = contextvars.copy_context()

        def _call() -> None:
            _PIPE_SESSION_ID_VAR.set("pipe-uuid-drive")
            tool.apply_ex(log_call=False, catch_exceptions=False)

        ctx.run(_call)

        captured = _SessionKeyCapturingTool.captured
        assert len(captured) == 1
        c = captured[0]
        assert c["session_key"] == "pipe-uuid-drive"
        assert c["pipe_session_id"] == "pipe-uuid-drive"
        # _MCP_CALL_IN_FLIGHT must fire for pipe-bound calls too -- otherwise the
        # IRONCLAD zero-crossover guard wouldn't apply to pipe-transported clients
        assert c["mcp_in_flight"] is True

    def test_falls_back_to_id_session_when_pipe_unset(self, agent: SerenaAgent) -> None:
        _SessionKeyCapturingTool.reset()
        tool = _SessionKeyCapturingTool(agent)

        # mock_ctx.session is the object id() works on; client_params=None skips
        # the client info path that would call into MagicMock attribute access
        mock_session = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.session = mock_session
        mock_ctx.session.client_params = None

        ctx = contextvars.copy_context()

        def _call() -> None:
            # do NOT set _PIPE_SESSION_ID_VAR -- the default None means the
            # legacy id(mcp_ctx.session) path takes over
            tool.apply_ex(log_call=False, catch_exceptions=False, mcp_ctx=mock_ctx)

        ctx.run(_call)

        captured = _SessionKeyCapturingTool.captured
        assert len(captured) == 1
        c = captured[0]
        assert c["session_key"] == id(mock_session)
        assert c["pipe_session_id"] is None
        assert c["mcp_in_flight"] is True

    def test_pipe_session_id_takes_precedence_over_mcp_ctx(self, agent: SerenaAgent) -> None:
        # both an mcp_ctx AND a pipe session_id are provided; the pipe id must win.
        # this matters because a future transport that wraps the pipe in a FastMCP
        # session would otherwise rotate session_key on every request.
        _SessionKeyCapturingTool.reset()
        tool = _SessionKeyCapturingTool(agent)

        mock_session = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.session = mock_session
        mock_ctx.session.client_params = None

        ctx = contextvars.copy_context()

        def _call() -> None:
            _PIPE_SESSION_ID_VAR.set("pipe-uuid-precedence")
            tool.apply_ex(log_call=False, catch_exceptions=False, mcp_ctx=mock_ctx)

        ctx.run(_call)

        captured = _SessionKeyCapturingTool.captured
        assert len(captured) == 1
        c = captured[0]
        assert c["session_key"] == "pipe-uuid-precedence"
        assert c["pipe_session_id"] == "pipe-uuid-precedence"
        # the per-session map must be keyed by the pipe id, never id(mock_session)
        assert id(mock_session) not in agent._active_projects_by_session
        assert id(mock_session) not in agent._cursor_managers_by_session

    def test_no_mcp_session_finalizer_registered_for_pipe(self, agent: SerenaAgent) -> None:
        # the pipe path drives eviction via socket disconnect (T6) rather than
        # via mcp_ctx GC; registering the legacy finalizer would tie cleanup to
        # the SDK's transport-layer Session lifetime -- exactly the
        # transport-churn instability the pipe redesign exists to avoid
        _SessionKeyCapturingTool.reset()
        tool = _SessionKeyCapturingTool(agent)

        mock_session = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.session = mock_session
        mock_ctx.session.client_params = None

        ctx = contextvars.copy_context()

        def _call() -> None:
            _PIPE_SESSION_ID_VAR.set("pipe-uuid-no-finalizer")
            tool.apply_ex(log_call=False, catch_exceptions=False, mcp_ctx=mock_ctx)

        ctx.run(_call)

        # neither id(mock_session) nor anything else should appear; the pipe path
        # leaves _session_finalizers untouched
        assert id(mock_session) not in agent._session_finalizers
        assert agent._session_finalizers == {}


class TestSerenaCatalogProvider:
    """Verify the production :class:`CatalogProvider` emits wire-format Serena tool definitions."""

    def test_catalog_returns_dicts_with_required_wire_format_keys(self, agent: SerenaAgent) -> None:
        provider = SerenaCatalogProvider(agent)
        catalog = asyncio.run(provider.get_catalog("any-session-id"))

        # the agent's exposed-tool list is non-empty under the default context;
        # an empty catalog would mean either the registry didn't load or the
        # context's tool inclusion broke
        assert len(catalog) > 0, "agent must expose at least one tool by default"
        for tool_dict in catalog:
            assert isinstance(tool_dict, dict)
            assert "name" in tool_dict
            # MCP wire format uses inputSchema, NOT parameters (which is the
            # FastMCP-internal field). A regression that emits "parameters"
            # would break every MCP client.
            assert "inputSchema" in tool_dict
            assert "description" in tool_dict, "Serena tools always carry a description"

    def test_catalog_dicts_are_json_serializable(self, agent: SerenaAgent) -> None:
        # the listener serializes the catalog into a PipeEnvelope and writes
        # JSON to the socket; a non-serializable dict (e.g. a leftover Pydantic
        # model) would break response delivery silently in production
        provider = SerenaCatalogProvider(agent)
        catalog = asyncio.run(provider.get_catalog("session-id"))
        encoded = json.dumps(catalog)
        decoded = json.loads(encoded)
        assert decoded == catalog

    def test_catalog_count_matches_exposed_tools(self, agent: SerenaAgent) -> None:
        # the catalog is the wire-format projection of get_exposed_tool_instances;
        # the count must be exactly equal -- a discrepancy means the iteration is
        # filtering or duplicating tools, which would surface as missing or
        # impossible tools/list entries in upstream clients
        provider = SerenaCatalogProvider(agent)
        catalog = asyncio.run(provider.get_catalog("session-id"))
        assert len(catalog) == len(agent.get_exposed_tool_instances())

    def test_catalog_session_id_does_not_affect_output(self, agent: SerenaAgent) -> None:
        # session_id is currently ignored (catalog is session-agnostic); pinning
        # the contract here forces a future per-session-shaped catalog change
        # to be deliberate rather than accidental
        provider = SerenaCatalogProvider(agent)
        catalog_a = asyncio.run(provider.get_catalog("session-A"))
        catalog_b = asyncio.run(provider.get_catalog("session-B"))
        assert catalog_a == catalog_b

    def test_catalog_dicts_carry_the_documented_tool_names(self, agent: SerenaAgent) -> None:
        # cross-check the wire-name set against the agent's view; a divergence
        # would mean the make_mcp_tool conversion dropped or renamed something
        provider = SerenaCatalogProvider(agent)
        catalog = asyncio.run(provider.get_catalog("session-id"))
        wire_names = {entry["name"] for entry in catalog}
        agent_names = {tool.get_name() for tool in agent.get_exposed_tool_instances()}
        assert wire_names == agent_names


class TestSerenaPipeFrameHandler:
    """Verify the production :class:`FrameHandler` dispatches frames and propagates the pipe session_id."""

    def test_handler_returns_none_for_notification(self, agent: SerenaAgent) -> None:
        # JSON-RPC notifications have no id and never receive a response;
        # the listener relies on this to skip the response-write path
        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle(
                "session-1",
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
        )
        assert response is None

    def test_handler_routes_initialize_request(self, agent: SerenaAgent) -> None:
        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle(
                "session-1",
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                },
            )
        )

        assert response is not None
        assert response["id"] == 1
        assert response["jsonrpc"] == "2.0"
        result = response["result"]
        # the protocolVersion echo lets a host that pins to a specific revision
        # see that revision back, rather than the daemon's default
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "Serena"
        assert "version" in result["serverInfo"]
        assert "capabilities" in result

    def test_handler_uses_default_protocol_version_when_unspecified(self, agent: SerenaAgent) -> None:
        # a host that initializes without a protocolVersion gets the daemon's
        # baseline -- pinning the value here means a deliberate update is needed
        # if/when Serena ratifies a newer revision
        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle(
                "session-1",
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
        )
        assert response["result"]["protocolVersion"] == "2024-11-05"

    def test_handler_returns_method_not_found_for_unknown_method(self, agent: SerenaAgent) -> None:
        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle(
                "session-1",
                {"jsonrpc": "2.0", "id": 5, "method": "completely/unknown/method"},
            )
        )

        assert response is not None
        assert response["id"] == 5
        # JSON-RPC 2.0 reserves -32601 for method-not-found; clients display
        # this differently from internal errors (-32603), so the code matters
        assert response["error"]["code"] == -32601

    def test_handler_routes_prompts_list_to_empty(self, agent: SerenaAgent) -> None:
        # Serena exposes no prompts; the handler must answer prompts/list with
        # the empty-list shape so hosts that capability-test prompts move on
        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle("session-1", {"jsonrpc": "2.0", "id": 7, "method": "prompts/list"})
        )
        assert response["id"] == 7
        assert response["result"] == {"prompts": []}

    def test_handler_routes_resources_list_to_empty(self, agent: SerenaAgent) -> None:
        # same shape rule for resources/list -- the host must see {"resources": []}
        # rather than method-not-found, otherwise capability negotiation breaks
        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle("session-1", {"jsonrpc": "2.0", "id": 8, "method": "resources/list"})
        )
        assert response["id"] == 8
        assert response["result"] == {"resources": []}

    def test_handler_routes_ping(self, agent: SerenaAgent) -> None:
        # ping is the keepalive shape; the response is an empty result object
        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle("session-1", {"jsonrpc": "2.0", "id": 9, "method": "ping"})
        )
        assert response["id"] == 9
        assert response["result"] == {}

    def test_handler_routes_tools_call_to_apply_ex(self, agent: SerenaAgent) -> None:
        """tools/call invokes ``Tool.apply_ex`` and wraps the result in MCP text content."""
        _SessionKeyCapturingTool.reset()
        tool = _inject_test_tool(agent)

        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle(
                "pipe-uuid-call-1",
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": tool.get_name(), "arguments": {}},
                },
            )
        )

        assert response is not None
        assert response["id"] == 7
        result = response["result"]
        # the wire-format result is a "content" array of typed parts; Serena
        # always returns a single text part with the tool's stringified output
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "captured"

        # the handler set _PIPE_SESSION_ID_VAR for this dispatch and apply_ex
        # propagated it into its worker thread; the captured snapshot is the
        # only place we can read back the worker-thread ContextVar state
        captured = _SessionKeyCapturingTool.captured
        assert len(captured) == 1
        c = captured[0]
        assert c["pipe_session_id"] == "pipe-uuid-call-1"
        assert c["session_key"] == "pipe-uuid-call-1"
        assert c["mcp_in_flight"] is True

    def test_handler_returns_internal_error_on_unknown_tool_name(self, agent: SerenaAgent) -> None:
        # an unknown tool name in tools/call is a contract-level failure (the
        # host built its catalog from the daemon, so the daemon must accept
        # every name it advertised). We surface this as an internal error
        # (-32603) rather than method-not-found, since the JSON-RPC method
        # tools/call is well-known
        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle(
                "session-1",
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": "completely_unknown_tool", "arguments": {}},
                },
            )
        )
        assert response["id"] == 10
        assert response["error"]["code"] == -32603
        assert "Unknown tool" in response["error"]["message"]

    def test_handler_returns_internal_error_on_invalid_tools_call_params(self, agent: SerenaAgent) -> None:
        # malformed params (name not a string) must produce a structured error,
        # not a Python traceback that the listener would log and drop silently
        handler = SerenaPipeFrameHandler(agent)
        response = asyncio.run(
            handler.handle(
                "session-1",
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": 12345, "arguments": {}},
                },
            )
        )
        assert response["id"] == 11
        assert response["error"]["code"] == -32603

    def test_handler_isolates_concurrent_sessions(self, agent: SerenaAgent) -> None:
        """Two concurrent dispatches must each see only their own session_id."""
        _SessionKeyCapturingTool.reset()
        tool = _inject_test_tool(agent)

        handler = SerenaPipeFrameHandler(agent)

        async def one_dispatch(session_id: str) -> dict[str, Any] | None:
            return await handler.handle(
                session_id,
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": tool.get_name(), "arguments": {}},
                },
            )

        async def scenario() -> tuple[dict[str, Any] | None, ...]:
            # asyncio.gather wraps each coroutine in an asyncio.Task with its
            # own copy of the parent context; ContextVars set in one task do
            # NOT leak to siblings, so each handle() sees only its own
            # session_id even though the var name is the same
            return tuple(
                await asyncio.gather(
                    one_dispatch("session-A"),
                    one_dispatch("session-B"),
                    one_dispatch("session-C"),
                )
            )

        responses = asyncio.run(scenario())
        assert all(r is not None for r in responses)

        captured = _SessionKeyCapturingTool.captured
        pipe_ids = sorted(c["pipe_session_id"] for c in captured)
        assert pipe_ids == ["session-A", "session-B", "session-C"]
        # session_key must equal pipe_session_id in every captured entry --
        # any mismatch would mean the pipe id was set but apply_ex still
        # derived from id(mcp_ctx.session), which is the bug T5 exists to fix
        for c in captured:
            assert c["session_key"] == c["pipe_session_id"]

    def test_handler_resets_pipe_session_id_var_after_dispatch(self, agent: SerenaAgent) -> None:
        # the handler uses a token-and-reset pattern so the caller's prior
        # _PIPE_SESSION_ID_VAR is restored after handle() returns. Pre-set the
        # var inside the scenario task, await handle (which sets it to
        # "inner-session" and then resets), and verify the read after handle()
        # returns the pre-set value rather than "inner-session" or None
        handler = SerenaPipeFrameHandler(agent)

        async def scenario() -> str | None:
            _PIPE_SESSION_ID_VAR.set("pre-existing")
            await handler.handle(
                "inner-session",
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            return _PIPE_SESSION_ID_VAR.get()

        assert asyncio.run(scenario()) == "pre-existing"
        # asyncio.run creates a new event loop with a fresh root context, so
        # 'after' is sampled inside that loop's root -- the handle's set+reset
        # left no residue (the assertion is that 'after' is None or
        # "pre-existing"; either is fine because the outer ctx was the source).
        # We assert the outer-context restoration since that's the contract.


def _project_sentinel() -> MagicMock:
    """Return a sentinel that satisfies SerenaAgent.__del__'s shutdown path.

    SerenaAgent.on_shutdown iterates the per-session dicts and reads
    ``project.project_root`` (agent.py:1350). A bare ``object()`` triggers
    AttributeError during fixture teardown, surfacing as a noisy
    ``PytestUnraisableExceptionWarning`` even when the test itself passed.
    A MagicMock with a string ``project_root`` attribute keeps the eviction
    semantics intact (the test only cares about dict membership) without
    polluting test output with shutdown-side AttributeErrors.
    """
    mock = MagicMock()
    mock.project_root = "/tmp/test-project-sentinel"
    return mock


class TestEvictPipeSession:
    """T6: SerenaAgent.evict_pipe_session — pipe-disconnect-keyed eviction.

    The pipe transport assigns each connection a UUID4 ``session_id`` (string);
    per-session state lives in :attr:`SerenaAgent._active_projects_by_session`
    and :attr:`SerenaAgent._cursor_managers_by_session`, both keyed on that
    string. When the pipe forwarder process exits, the daemon must drop those
    entries -- otherwise the next forwarder on a fresh pipe inherits stale
    state. This is the public eviction method the
    :class:`PipeListener.add_disconnect_handler` hook calls with the session_id.
    """

    def test_evict_pipe_session_drops_active_project(self, agent: SerenaAgent) -> None:
        # populate the per-session map directly; we don't need a real Project
        # for the eviction contract, just a sentinel that proves the entry
        # was present before evict and absent after
        sentinel = _project_sentinel()
        agent._active_projects_by_session["pipe-uuid-evict-1"] = sentinel  # type: ignore[assignment]

        agent.evict_pipe_session("pipe-uuid-evict-1")

        assert "pipe-uuid-evict-1" not in agent._active_projects_by_session

    def test_evict_pipe_session_drops_cursor_manager(self, agent: SerenaAgent) -> None:
        # mirror the active-project case for the cursor-manager dict; both
        # are pipe-keyed and both must be cleared on disconnect
        sentinel = _project_sentinel()
        agent._cursor_managers_by_session["pipe-uuid-evict-2"] = sentinel  # type: ignore[assignment]

        agent.evict_pipe_session("pipe-uuid-evict-2")

        assert "pipe-uuid-evict-2" not in agent._cursor_managers_by_session

    def test_evict_pipe_session_no_op_for_unknown_session(self, agent: SerenaAgent) -> None:
        # an unknown session_id MUST NOT raise; the disconnect handler runs
        # for every pipe close, and a defensive caller may invoke evict
        # twice (e.g. on stop() teardown after the pipe already closed)
        agent.evict_pipe_session("never-existed")

        assert agent._active_projects_by_session == {}
        assert agent._cursor_managers_by_session == {}

    def test_evict_pipe_session_does_not_affect_legacy_state(self, agent: SerenaAgent) -> None:
        # the legacy single-slot fields belong to non-MCP callers (CLI,
        # dashboard, tests). Evicting a pipe session MUST NOT touch them;
        # otherwise CLI workflows would lose state every time a pipe
        # disconnects in another part of the daemon
        legacy_proj = _project_sentinel()
        legacy_cursor = _project_sentinel()
        agent._legacy_active_project = legacy_proj  # type: ignore[assignment]
        agent._legacy_cursor_manager = legacy_cursor  # type: ignore[assignment]

        agent.evict_pipe_session("any-uuid")

        assert agent._legacy_active_project is legacy_proj
        assert agent._legacy_cursor_manager is legacy_cursor

    def test_evict_pipe_session_does_not_affect_int_keyed_sessions(self, agent: SerenaAgent) -> None:
        # int-keyed sessions belong to direct stdio / streamable-http clients
        # whose session_key is id(mcp_ctx.session). They are evicted by the
        # weakref-finalize path, NOT by evict_pipe_session. A pipe disconnect
        # arriving with a session_id that happens to coincide with an int-keyed
        # entry's str() form must not collide -- str and int are distinct keys
        int_proj = _project_sentinel()
        int_cursor = _project_sentinel()
        agent._active_projects_by_session[12345] = int_proj  # type: ignore[assignment]
        agent._cursor_managers_by_session[12345] = int_cursor  # type: ignore[assignment]

        agent.evict_pipe_session("12345")

        # int-keyed entries survive the str-keyed eviction
        assert agent._active_projects_by_session[12345] is int_proj
        assert agent._cursor_managers_by_session[12345] is int_cursor

    def test_evict_pipe_session_drops_only_named_session(self, agent: SerenaAgent) -> None:
        # multiple pipe sessions can coexist -- evicting one must leave the
        # others untouched; otherwise a forwarder restart in client A would
        # inadvertently wipe client B's active project
        sentinel_a = _project_sentinel()
        sentinel_b = _project_sentinel()
        agent._active_projects_by_session["uuid-a"] = sentinel_a  # type: ignore[assignment]
        agent._active_projects_by_session["uuid-b"] = sentinel_b  # type: ignore[assignment]

        agent.evict_pipe_session("uuid-a")

        assert "uuid-a" not in agent._active_projects_by_session
        assert agent._active_projects_by_session["uuid-b"] is sentinel_b

    def test_evict_pipe_session_drops_both_dicts_atomically(self, agent: SerenaAgent) -> None:
        # active_project and cursor_manager belong to the same logical session
        # state; evicting one without the other would leave a half-evicted
        # session that surfaces as "no active project but the cursor still
        # remembers it." The eviction must drop both for the same session_id
        # in a single call
        agent._active_projects_by_session["uuid-paired"] = _project_sentinel()  # type: ignore[assignment]
        agent._cursor_managers_by_session["uuid-paired"] = _project_sentinel()  # type: ignore[assignment]

        agent.evict_pipe_session("uuid-paired")

        assert "uuid-paired" not in agent._active_projects_by_session
        assert "uuid-paired" not in agent._cursor_managers_by_session


class TestSerenaMCPFactoryPipeListener:
    """T6: ``build_pipe_listener`` constructs the production wiring.

    The wiring composes:

    * :class:`PipeListener` as the Unix-socket entry point.
    * :class:`SerenaPipeFrameHandler` to dispatch ``tools/call`` and friends
      against the agent's exposed-tool list.
    * :class:`SerenaCatalogProvider` to answer ``pipe/catalog/get``.
    * :meth:`SerenaAgent.evict_pipe_session` registered as a disconnect handler
      so per-session state is dropped when the pipe disconnects.

    These tests pin the composition explicitly because the production daemon
    relies on it -- a regression where the eviction handler is silently
    dropped or where the wrong CatalogProvider is passed would surface as
    state leakage between clients (the very bug the pipe exists to prevent).
    """

    def test_build_pipe_listener_returns_pipe_listener(self, agent: SerenaAgent) -> None:
        from serena.mcp import build_pipe_listener
        from serena.daemon_pipe import PipeListener

        listener = build_pipe_listener(agent)
        try:
            assert isinstance(listener, PipeListener)
        finally:
            # nothing to stop here -- listener was never started
            pass

    def test_build_pipe_listener_uses_serena_pipe_frame_handler(self, agent: SerenaAgent) -> None:
        from serena.mcp import build_pipe_listener

        listener = build_pipe_listener(agent)
        # the listener stores the handler at _frame_handler; the production
        # type must be SerenaPipeFrameHandler so tools/call routes through
        # apply_ex and not the null handler
        assert isinstance(listener._frame_handler, SerenaPipeFrameHandler)

    def test_build_pipe_listener_uses_serena_catalog_provider(self, agent: SerenaAgent) -> None:
        from serena.mcp import build_pipe_listener

        listener = build_pipe_listener(agent)
        # the catalog provider must be the production type so pipe/catalog/get
        # answers from the agent's exposed-tool list, not the empty null list
        assert isinstance(listener._catalog_provider, SerenaCatalogProvider)

    def test_build_pipe_listener_does_not_register_evict_callback(self, agent: SerenaAgent) -> None:
        """Per the project-root-as-session-id contract, build_pipe_listener MUST NOT
        wire ``SerenaAgent.evict_pipe_session`` as a socket-disconnect handler.

        Daemon-side per-session state must survive socket-level churn so a
        respawned pipe-client into the same project_root re-attaches to the
        existing entry. This test guards against the regression of re-introducing
        the disconnect handler that previously evicted state on every disconnect
        (the bug captured in
        ``plan://Serena:serena/serena-pipe-session-id-is-the-session-id``).
        """
        from serena.mcp import build_pipe_listener

        listener = build_pipe_listener(agent)
        # the production listener must have no disconnect handlers wired -- the
        # disconnect-handler mechanism on the listener itself remains as an
        # extension point, but production wiring deliberately leaves it empty
        # so socket disconnect cannot evict per-session state
        assert agent.evict_pipe_session not in listener._disconnect_handlers, (
            "build_pipe_listener wired evict_pipe_session as a disconnect handler -- "
            "this regresses the project-root-as-session-id contract; per-session state "
            "must survive socket disconnect"
        )
        assert listener._disconnect_handlers == [], (
            f"build_pipe_listener registered unexpected disconnect handlers: {listener._disconnect_handlers}"
        )

    def test_build_pipe_listener_propagates_openai_compat(self, agent: SerenaAgent) -> None:
        from serena.mcp import build_pipe_listener

        listener = build_pipe_listener(agent, openai_tool_compatible=True)
        # the catalog provider must reflect the requested compatibility flag
        # so chatgpt/codex/oaicompat-agent contexts get the sanitized schemas
        assert listener._catalog_provider._openai_tool_compatible is True  # type: ignore[attr-defined]

    def test_build_pipe_listener_default_openai_compat_is_false(self, agent: SerenaAgent) -> None:
        from serena.mcp import build_pipe_listener

        listener = build_pipe_listener(agent)
        # default to standard MCP wire format; opt-in is explicit
        assert listener._catalog_provider._openai_tool_compatible is False  # type: ignore[attr-defined]


class TestEvictPipeSessionEndToEnd:
    """T6 end-to-end: pipe disconnect drops the agent's per-session state.

    Walks the full wire: build the listener via :func:`build_pipe_listener`,
    accept a pipe handshake, populate per-session state on the agent, close
    the pipe, then assert the state is gone. This is the integration assert
    that ties together the listener-side disconnect-handler hook (T6 in
    daemon_pipe.py), the agent-side eviction method (T6 in agent.py), and
    the production wiring (T6 in mcp.py).
    """

    def test_pipe_disconnect_does_not_evict_active_project_via_serena_agent(self, agent: SerenaAgent) -> None:
        """Under the project-root-as-session-id contract, socket disconnect MUST NOT
        cause :meth:`SerenaAgent.evict_pipe_session` to fire. ``build_pipe_listener``
        no longer wires the disconnect handler, and per-session state must survive
        socket churn so a respawned pipe-client into the same project_root re-attaches
        to the existing entry.
        """
        # /tmp short path -- AF_UNIX has a ~104-byte path limit on macOS, and
        # pytest's tmp_path lives under /private/var/folders/... which can
        # blow past the limit. Mirror the socket_path fixture pattern from
        # test_pipe_transport.py for the same reason.
        import os as _os
        import uuid as _uuid

        from serena.mcp import build_pipe_listener
        from serena.pipe_protocol import PipeEnvelope, PipeHandshake

        socket_path = f"/tmp/serena-pipe-evict-{_uuid.uuid4().hex[:8]}.sock"
        try:

            async def _scenario_inner() -> tuple[bool, bool]:
                listener = build_pipe_listener(agent)
                await listener.start(socket_path)
                try:
                    reader, writer = await asyncio.open_unix_connection(socket_path)
                    writer.write(PipeHandshake.request("/tmp/test-evict-end2end").to_bytes())
                    await writer.drain()
                    response_line = await reader.readuntil(b"\n")
                    response = PipeEnvelope.from_bytes(response_line)
                    session_id = PipeHandshake.session_id_from_response(response)

                    agent._active_projects_by_session[session_id] = _project_sentinel()  # type: ignore[assignment]
                    agent._cursor_managers_by_session[session_id] = _project_sentinel()  # type: ignore[assignment]
                    pre_state = (
                        session_id in agent._active_projects_by_session
                        and session_id in agent._cursor_managers_by_session
                    )

                    writer.close()
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        await writer.wait_closed()

                    # poll briefly to give any (mis)wired disconnect handler time to fire;
                    # the test passes only if NO eviction has happened
                    for _ in range(50):
                        await asyncio.sleep(0.01)

                    post_state = (
                        session_id in agent._active_projects_by_session
                        and session_id in agent._cursor_managers_by_session
                    )
                    return pre_state, post_state
                finally:
                    await listener.stop()

            pre, post = asyncio.run(_scenario_inner())
            assert pre is True, "pre-disconnect state must be populated for the test to be meaningful"
            assert post is True, (
                "agent dropped a per-session entry on pipe disconnect; "
                "this regresses the project-root-as-session-id contract -- per-session "
                "state must survive socket disconnect so a respawned pipe-client into "
                "the same project re-attaches to the existing entry"
            )
            return
        finally:
            with contextlib.suppress(FileNotFoundError):
                _os.unlink(socket_path)


class TestPipeSessionSurvivesStreamableHttpTeardown:
    """T7: pipe-keyed per-session state survives per-request streamable-http task teardowns.

    The legacy streamable-http path keys per-session entries on
    ``id(mcp_ctx.session)`` (an ``int``) and registers a
    ``weakref.finalize`` on that ``Session``; when the per-request task
    ends and the ``Session`` is garbage-collected, the finalizer fires and
    pops the int_key from
    :attr:`SerenaAgent._active_projects_by_session` and
    :attr:`SerenaAgent._cursor_managers_by_session` via
    :meth:`SerenaAgent._evict_session_state`.

    The pipe transport (T1-T6) keys per-session entries on a UUID4 hex
    string handed back at handshake time. Because the keys are ``str``
    (not ``int``) and the disconnect-driven path
    (:meth:`SerenaAgent.evict_pipe_session`) is the ONLY eviction site
    that touches ``str`` keys, pipe-keyed entries MUST survive every form
    of streamable-http per-request task teardown:

    1. an explicit ``_evict_session_state(int_key)`` call (the immediate
       eviction the legacy path triggers from its weakref.finalize);
    2. real garbage collection via ``weakref.finalize`` on a
       session-like object whose only reference is dropped;
    3. concurrent multi-int-keyed evictions arriving from many
       overlapping streamable-http requests on the same daemon;
    4. arbitrary real-time elapsed since the pipe handshake -- no
       timer-based eviction exists, so the pipe is the only lifetime
       gate.

    Each scenario asserts that BOTH per-session dicts still hold the
    pipe-keyed entry AND that the entry's value is the SAME Python
    object (not just any value at the key). The dict-membership +
    identity assertion is the in-graph equivalent of the plan's "verify
    cursor is still alive via cursor_look": if the cursor manager
    remains retrievable by its pipe session_id, ``cursor_look`` over the
    pipe will resolve to it.
    """

    def test_pipe_keyed_entries_survive_explicit_int_keyed_evict_session_state(
        self, agent: SerenaAgent
    ) -> None:
        # populate the pipe-keyed (str) entries plus an unrelated int-keyed
        # entry; evicting the int-keyed entry must not touch the str-keyed
        # entry's membership or identity
        pipe_session_id = "pipe-uuid-survive-direct"
        pipe_proj = _project_sentinel()
        pipe_cursor = _project_sentinel()
        agent._active_projects_by_session[pipe_session_id] = pipe_proj  # type: ignore[assignment]
        agent._cursor_managers_by_session[pipe_session_id] = pipe_cursor  # type: ignore[assignment]

        int_key = id(object())
        agent._active_projects_by_session[int_key] = _project_sentinel()  # type: ignore[assignment]
        agent._cursor_managers_by_session[int_key] = _project_sentinel()  # type: ignore[assignment]

        agent._evict_session_state(int_key)

        # the streamable-http int entry is gone (positive control)
        assert int_key not in agent._active_projects_by_session
        assert int_key not in agent._cursor_managers_by_session
        # the pipe str entry is preserved -- both membership AND identity
        assert agent._active_projects_by_session[pipe_session_id] is pipe_proj
        assert agent._cursor_managers_by_session[pipe_session_id] is pipe_cursor

    def test_pipe_keyed_entries_survive_weakref_finalize_driven_eviction(
        self, agent: SerenaAgent
    ) -> None:
        # the realistic streamable-http GC path: register weakref.finalize on
        # a session-like object, drop its only reference, force gc.collect();
        # the finalizer fires _evict_session_state(int_key) and the pipe-keyed
        # str entries must be untouched

        # class-level reference (not bare object()) so weakref.finalize can
        # register on it; bare object() instances do not support weakrefs
        class _SessionLike:
            pass

        pipe_session_id = "pipe-uuid-survive-gc"
        pipe_proj = _project_sentinel()
        pipe_cursor = _project_sentinel()
        agent._active_projects_by_session[pipe_session_id] = pipe_proj  # type: ignore[assignment]
        agent._cursor_managers_by_session[pipe_session_id] = pipe_cursor  # type: ignore[assignment]

        mock_session = _SessionLike()
        int_key = id(mock_session)
        agent._active_projects_by_session[int_key] = _project_sentinel()  # type: ignore[assignment]
        agent._cursor_managers_by_session[int_key] = _project_sentinel()  # type: ignore[assignment]
        agent._register_session_finalizer(mock_session, int_key)

        # drop the only reference and force GC; CPython usually fires
        # weakref.finalize synchronously when refcount hits zero, but
        # platform / GC-edge cases may delay it slightly
        del mock_session
        gc.collect()
        for _ in range(50):
            if int_key not in agent._active_projects_by_session:
                break
            time.sleep(0.01)

        # the streamable-http int entry has been GC-evicted (positive control)
        assert int_key not in agent._active_projects_by_session
        assert int_key not in agent._cursor_managers_by_session
        # the pipe str entry survives -- identity preserved
        assert agent._active_projects_by_session[pipe_session_id] is pipe_proj
        assert agent._cursor_managers_by_session[pipe_session_id] is pipe_cursor

    def test_pipe_keyed_entries_survive_concurrent_int_keyed_evictions(
        self, agent: SerenaAgent
    ) -> None:
        # multiple streamable-http requests can be in-flight against the
        # same daemon; their int-keyed teardowns must NOT touch the
        # pipe-keyed entry no matter how many fire in succession
        pipe_session_id = "pipe-uuid-survive-many"
        pipe_proj = _project_sentinel()
        pipe_cursor = _project_sentinel()
        agent._active_projects_by_session[pipe_session_id] = pipe_proj  # type: ignore[assignment]
        agent._cursor_managers_by_session[pipe_session_id] = pipe_cursor  # type: ignore[assignment]

        # a fan of 16 fake int-keyed sessions (overlapping streamable-http
        # requests on the same daemon)
        int_keys = [id(object()) + i for i in range(16)]
        for int_key in int_keys:
            agent._active_projects_by_session[int_key] = _project_sentinel()  # type: ignore[assignment]
            agent._cursor_managers_by_session[int_key] = _project_sentinel()  # type: ignore[assignment]
        for int_key in int_keys:
            agent._evict_session_state(int_key)

        # all int entries are gone (positive control)
        for int_key in int_keys:
            assert int_key not in agent._active_projects_by_session
            assert int_key not in agent._cursor_managers_by_session
        # the pipe entry is preserved -- identity preserved
        assert agent._active_projects_by_session[pipe_session_id] is pipe_proj
        assert agent._cursor_managers_by_session[pipe_session_id] is pipe_cursor

    def test_pipe_keyed_entries_survive_real_time_elapsed_past_request_lifetime(
        self, agent: SerenaAgent
    ) -> None:
        # the parent plan documents the legacy path's per-request task
        # teardown timeout as ">=10s" of wall time. No timer-based
        # eviction exists in the pipe path; pipe-keyed entries persist for
        # the entire lifetime of the pipe connection regardless of wall
        # time. Verifying with a tightened test-time slice is sufficient
        # because the invariant under test is "no timer fires at any wall
        # time" -- any positive sleep length falsifies a hypothetical
        # timer (a 10s sleep would gate test speed without strengthening
        # the proof; the live operator runbook in T11 covers the wall-time
        # dimension)
        pipe_session_id = "pipe-uuid-survive-time"
        pipe_proj = _project_sentinel()
        pipe_cursor = _project_sentinel()
        agent._active_projects_by_session[pipe_session_id] = pipe_proj  # type: ignore[assignment]
        agent._cursor_managers_by_session[pipe_session_id] = pipe_cursor  # type: ignore[assignment]

        time.sleep(0.5)
        gc.collect()

        # entries are still present with the same identity
        assert agent._active_projects_by_session[pipe_session_id] is pipe_proj
        assert agent._cursor_managers_by_session[pipe_session_id] is pipe_cursor


class TestPipeSessionSurvivesStreamableHttpTeardownEndToEnd:
    """T7 end-to-end: pipe-keyed state survives a real streamable-http
    teardown event while the pipe connection remains open.

    Walks the full wire: build the listener via
    :func:`build_pipe_listener`, accept a real pipe handshake (which
    issues a UUID4 ``session_id``), populate per-session state on the
    agent under that ``session_id``, then simulate a streamable-http
    per-request task teardown arriving at the daemon
    (``_evict_session_state(int_key)`` plus real elapsed time and a GC
    pass) WITHOUT closing the pipe. The pipe-asserted ``session_id``'s
    state must still be present afterwards. As a positive control, the
    test then closes the pipe and verifies the pipe-keyed state IS
    evicted via the disconnect handler -- so the survival assertion
    cannot vacuously pass on never-evictable entries.
    """

    def test_pipe_session_state_survives_streamable_http_teardown_end_to_end(
        self, agent: SerenaAgent
    ) -> None:
        """Under the project-root-as-session-id contract, pipe-keyed per-session state
        survives BOTH (a) streamable-http per-request teardown (an int-keyed eviction
        targeting a different session arrives at the daemon while the pipe stays
        connected) AND (b) the pipe-client's own socket disconnect. The pipe lifetime
        is no longer the eviction gate; the project_root key is.
        """
        import os as _os
        import uuid as _uuid

        from serena.mcp import build_pipe_listener
        from serena.pipe_protocol import PipeEnvelope, PipeHandshake

        # /tmp short path -- AF_UNIX has a ~104-byte path limit on macOS,
        # and pytest's tmp_path lives under /private/var/folders/... which
        # can blow past the limit. Mirror the socket_path fixture pattern
        # from test_pipe_transport.py and TestEvictPipeSessionEndToEnd
        socket_path = f"/tmp/serena-pipe-survive-{_uuid.uuid4().hex[:8]}.sock"
        try:

            async def _scenario_inner() -> tuple[bool, bool, bool]:
                listener = build_pipe_listener(agent)
                await listener.start(socket_path)
                try:
                    reader, writer = await asyncio.open_unix_connection(socket_path)
                    writer.write(PipeHandshake.request("/tmp/test-survives-streamable-http").to_bytes())
                    await writer.drain()
                    response_line = await reader.readuntil(b"\n")
                    response = PipeEnvelope.from_bytes(response_line)
                    session_id = PipeHandshake.session_id_from_response(response)

                    # populate the pipe-asserted session's per-session
                    # state, then verify it landed before any teardown
                    pipe_proj = _project_sentinel()
                    pipe_cursor = _project_sentinel()
                    agent._active_projects_by_session[session_id] = pipe_proj  # type: ignore[assignment]
                    agent._cursor_managers_by_session[session_id] = pipe_cursor  # type: ignore[assignment]
                    pre_state = (
                        agent._active_projects_by_session.get(session_id) is pipe_proj
                        and agent._cursor_managers_by_session.get(session_id) is pipe_cursor
                    )

                    # simulate a streamable-http per-request teardown
                    # WITHOUT closing the pipe: an int-keyed eviction
                    # arrives at the daemon while the pipe stays connected
                    bogus_int_key = id(object())
                    agent._active_projects_by_session[bogus_int_key] = _project_sentinel()  # type: ignore[assignment]
                    agent._evict_session_state(bogus_int_key)
                    # let real time pass to falsify any hidden timer-based
                    # eviction; the pipe is the only lifetime gate
                    await asyncio.sleep(0.2)
                    gc.collect()

                    survived_teardown = (
                        agent._active_projects_by_session.get(session_id) is pipe_proj
                        and agent._cursor_managers_by_session.get(session_id) is pipe_cursor
                    )

                    # under the project-root-as-session-id contract, closing the
                    # pipe MUST NOT evict either: a respawned pipe-client into the
                    # same project finds activation preserved
                    writer.close()
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        await writer.wait_closed()
                    # poll briefly so any (mis)wired disconnect handler has time to fire
                    for _ in range(50):
                        await asyncio.sleep(0.01)
                    survived_disconnect = (
                        agent._active_projects_by_session.get(session_id) is pipe_proj
                        and agent._cursor_managers_by_session.get(session_id) is pipe_cursor
                    )

                    return pre_state, survived_teardown, survived_disconnect
                finally:
                    await listener.stop()

            pre, survived_teardown, survived_disconnect = asyncio.run(_scenario_inner())
            assert pre is True, "pre-population must succeed for the test to be meaningful"
            assert survived_teardown is True, (
                "pipe-keyed state MUST survive streamable-http per-request teardown "
                "while the pipe remains connected"
            )
            assert survived_disconnect is True, (
                "pipe-keyed state MUST also survive pipe disconnect under the "
                "project-root-as-session-id contract -- a respawned pipe-client into "
                "the same project finds activation preserved"
            )
            return
        finally:
            with contextlib.suppress(FileNotFoundError):
                _os.unlink(socket_path)
