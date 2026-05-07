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
import contextvars
import json
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
