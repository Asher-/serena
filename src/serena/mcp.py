"""
The Serena Model Context Protocol (MCP) Server
"""

import asyncio
import atexit
import contextvars
import sys
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, cast

import docstring_parser
from mcp.server.fastmcp import server
from mcp.server.fastmcp.server import FastMCP, Settings
from mcp.server.fastmcp.tools.base import Tool as MCPTool
from mcp.types import Tool as WireTool
from mcp.types import ToolAnnotations
from pydantic_settings import SettingsConfigDict
from sensai.util import logging

from serena import serena_version
from serena.agent import (
    SerenaAgent,
    SerenaConfig,
)
from serena.config.context_mode import SerenaAgentContext
from serena.config.serena_config import LanguageBackend, ModeSelectionDefinition
from serena.constants import DEFAULT_CONTEXT, SERENA_LOG_FORMAT
from serena.daemon_pipe import CatalogProvider, FrameHandler
from serena.tools import Tool
from serena.util.exception import show_fatal_exception_safe
from serena.util.logging import MemoryLogHandler

log = logging.getLogger(__name__)


def configure_logging(*args, **kwargs) -> None:  # type: ignore
    # We only do something here if logging has not yet been configured.
    # Normally, logging is configured in the MCP server startup script.
    if not logging.is_enabled():
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format=SERENA_LOG_FORMAT)


# patch the logging configuration function in fastmcp, because it's hard-coded and broken
server.configure_logging = configure_logging  # type: ignore


@dataclass
class SerenaMCPRequestContext:
    agent: SerenaAgent


class SerenaMCPFactory:
    """
    Factory for the creation of the Serena MCP server with an associated SerenaAgent.
    """

    def __init__(self, context: str = DEFAULT_CONTEXT, project: str | None = None, memory_log_handler: MemoryLogHandler | None = None):
        """
        :param context: The context name or path to context file
        :param project: Either an absolute path to the project directory or a name of an already registered project.
            If the project passed here hasn't been registered yet, it will be registered automatically and can be activated by its name
            afterward.
        :param memory_log_handler: the in-memory log handler to use for the agent's logging
        """
        self.context = SerenaAgentContext.load(context)
        self.project = project
        self.agent: SerenaAgent | None = None
        self.memory_log_handler = memory_log_handler

    @staticmethod
    def _sanitize_for_openai_tools(schema: dict) -> dict:
        """
        This method was written by GPT-5, I have not reviewed it in detail.
        Only called when `openai_tool_compatible` is True.

        Make a Pydantic/JSON Schema object compatible with OpenAI tool schema.
        - 'integer' -> 'number' (+ multipleOf: 1)
        - remove 'null' from union type arrays
        - coerce integer-only enums to number
        - best-effort simplify oneOf/anyOf when they only differ by integer/number
        """
        s = deepcopy(schema)

        def walk(node):  # type: ignore
            if not isinstance(node, dict):
                # lists get handled by parent calls
                return node

            # ---- handle type ----
            t = node.get("type")
            if isinstance(t, str):
                if t == "integer":
                    node["type"] = "number"
                    # preserve existing multipleOf but ensure it's integer-like
                    if "multipleOf" not in node:
                        node["multipleOf"] = 1
            elif isinstance(t, list):
                # remove 'null' (OpenAI tools don't support nullables)
                t2 = [x if x != "integer" else "number" for x in t if x != "null"]
                if not t2:
                    # fall back to object if it somehow becomes empty
                    t2 = ["object"]
                node["type"] = t2[0] if len(t2) == 1 else t2
                if "integer" in t or "number" in t2:
                    # if integers were present, keep integer-like restriction
                    node.setdefault("multipleOf", 1)

            # ---- enums of integers -> number ----
            if "enum" in node and isinstance(node["enum"], list):
                vals = node["enum"]
                if vals and all(isinstance(v, int) for v in vals):
                    node.setdefault("type", "number")
                    # keep them as ints; JSON 'number' covers ints
                    node.setdefault("multipleOf", 1)

            # ---- simplify anyOf/oneOf if they only differ by integer/number ----
            for key in ("oneOf", "anyOf"):
                if key in node and isinstance(node[key], list):
                    # Special case: anyOf or oneOf with "type X" and "null"
                    if len(node[key]) == 2:
                        types = [sub.get("type") for sub in node[key]]
                        if "null" in types:
                            non_null_type = next(t for t in types if t != "null")
                            if isinstance(non_null_type, str):
                                node["type"] = non_null_type
                                node.pop(key, None)
                                continue
                    simplified = []
                    changed = False
                    for sub in node[key]:
                        sub = walk(sub)  # recurse
                        simplified.append(sub)
                    # If all subs are the same after integer→number, collapse
                    try:
                        import json

                        canon = [json.dumps(x, sort_keys=True) for x in simplified]
                        if len(set(canon)) == 1:
                            # copy the single schema up
                            only = simplified[0]
                            node.pop(key, None)
                            for k, v in only.items():
                                if k not in node:
                                    node[k] = v
                            changed = True
                    except Exception:
                        pass
                    if not changed:
                        node[key] = simplified

            # ---- recurse into known schema containers ----
            for child_key in ("properties", "patternProperties", "definitions", "$defs"):
                if child_key in node and isinstance(node[child_key], dict):
                    for k, v in list(node[child_key].items()):
                        node[child_key][k] = walk(v)

            # arrays/items
            if "items" in node:
                node["items"] = walk(node["items"])

            # allOf/if/then/else - pass through with integer→number conversions applied inside
            for key in ("allOf",):
                if key in node and isinstance(node[key], list):
                    node[key] = [walk(x) for x in node[key]]

            if "if" in node:
                node["if"] = walk(node["if"])
            if "then" in node:
                node["then"] = walk(node["then"])
            if "else" in node:
                node["else"] = walk(node["else"])

            return node

        return walk(s)

    @staticmethod
    def make_mcp_tool(tool: Tool, openai_tool_compatible: bool = True) -> MCPTool:
        """
        Create an MCP tool from a Serena Tool instance.

        :param tool: The Serena Tool instance to convert.
        :param openai_tool_compatible: whether to process the tool schema to be compatible with OpenAI tools
            (doesn't accept integer, needs number instead, etc.). This allows using Serena MCP within codex.
        """
        func_name = tool.get_name()
        func_doc = tool.get_apply_docstring() or ""
        func_arg_metadata = tool.get_apply_fn_metadata()
        is_async = False
        parameters = func_arg_metadata.arg_model.model_json_schema()
        if openai_tool_compatible:
            parameters = SerenaMCPFactory._sanitize_for_openai_tools(parameters)

        docstring = docstring_parser.parse(func_doc)

        # Mount the tool description as a combination of the docstring description and
        # the return value description, if it exists.
        overridden_description = tool.agent.get_context().tool_description_overrides.get(func_name, None)

        if overridden_description is not None:
            func_doc = overridden_description
        elif docstring.description:
            func_doc = docstring.description
        else:
            func_doc = ""
        func_doc = func_doc.strip().strip(".")
        if func_doc:
            func_doc += "."
        if docstring.returns and (docstring_returns_descr := docstring.returns.description):
            # Only add a space before "Returns" if func_doc is not empty
            prefix = " " if func_doc else ""
            func_doc = f"{func_doc}{prefix}Returns {docstring_returns_descr.strip().strip('.')}."

        # Parse the parameter descriptions from the docstring and add pass its description
        # to the parameter schema.
        docstring_params = {param.arg_name: param for param in docstring.params}
        parameters_properties: dict[str, dict[str, Any]] = parameters["properties"]
        for parameter, properties in parameters_properties.items():
            if (param_doc := docstring_params.get(parameter)) and param_doc.description:
                param_desc = f"{param_doc.description.strip().strip('.') + '.'}"
                properties["description"] = param_desc[0].upper() + param_desc[1:]

        def execute_fn(**kwargs) -> str:  # type: ignore
            return tool.apply_ex(log_call=True, catch_exceptions=True, **kwargs)

        # Generate human-readable title from snake_case tool name
        tool_title = " ".join(word.capitalize() for word in func_name.split("_"))

        # Create annotations with appropriate hints based on tool capabilities
        can_edit = tool.can_edit()
        annotations = ToolAnnotations(
            title=tool_title,
            readOnlyHint=not can_edit,
            destructiveHint=can_edit,
        )

        return MCPTool(
            fn=execute_fn,
            name=func_name,
            description=func_doc,
            parameters=parameters,
            fn_metadata=func_arg_metadata,
            is_async=is_async,
            # keep the value in sync with the kwarg name in Tool.apply_ex. The mcp sdk uses reflection to infer this
            # when the tool is constructed via from_function (which is a bit crazy IMO, but well...)
            context_kwarg="mcp_ctx",
            annotations=annotations,
            title=tool_title,
        )

    def _iter_tools(self) -> Iterator[Tool]:
        assert self.agent is not None
        yield from self.agent.get_exposed_tool_instances()

    # noinspection PyProtectedMember
    def _set_mcp_tools(self, mcp: FastMCP, openai_tool_compatible: bool = False) -> None:
        """Update the tools in the MCP server"""
        if mcp is not None:
            mcp._tool_manager._tools = {}
            for tool in self._iter_tools():
                mcp_tool = self.make_mcp_tool(tool, openai_tool_compatible=openai_tool_compatible)
                mcp._tool_manager._tools[tool.get_name()] = mcp_tool
            log.info(f"Starting MCP server with {len(mcp._tool_manager._tools)} tools: {list(mcp._tool_manager._tools.keys())}")

    def _create_serena_agent(self, serena_config: SerenaConfig, modes: ModeSelectionDefinition | None = None) -> SerenaAgent:
        return SerenaAgent(
            project=self.project, serena_config=serena_config, context=self.context, modes=modes, memory_log_handler=self.memory_log_handler
        )

    def _create_default_serena_config(self) -> SerenaConfig:
        return SerenaConfig.from_config_file()

    def create_mcp_server(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        modes: Sequence[str] = (),
        language_backend: LanguageBackend | None = None,
        enable_web_dashboard: bool | None = None,
        enable_gui_log_window: bool | None = None,
        open_web_dashboard: bool | None = None,
        log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
        trace_lsp_communication: bool | None = None,
        tool_timeout: float | None = None,
    ) -> FastMCP:
        """
        Create an MCP server with process-isolated SerenaAgent to prevent asyncio contamination.

        :param host: The host to bind to
        :param port: The port to bind to
        :param modes: List of mode names or paths to mode files
        :param language_backend: the language backend to use, overriding the configuration setting.
        :param enable_web_dashboard: Whether to enable the web dashboard. If not specified, will take the value from the serena configuration.
        :param enable_gui_log_window: Whether to enable the GUI log window. It currently does not work on macOS, and setting this to True will be ignored then.
            If not specified, will take the value from the serena configuration.
        :param open_web_dashboard: Whether to open the web dashboard on launch.
            If not specified, will take the value from the serena configuration.
        :param log_level: Log level. If not specified, will take the value from the serena configuration.
        :param trace_lsp_communication: Whether to trace the communication between Serena and the language servers.
            This is useful for debugging language server issues.
        :param tool_timeout: Timeout in seconds for tool execution. If not specified, will take the value from the serena configuration.
        """
        try:
            config = self._create_default_serena_config()

            # update configuration with the provided parameters
            if enable_web_dashboard is not None:
                config.web_dashboard = enable_web_dashboard
            if enable_gui_log_window is not None:
                config.gui_log_window = enable_gui_log_window
            if open_web_dashboard is not None:
                config.web_dashboard_open_on_launch = open_web_dashboard
            if log_level is not None:
                log_level = cast(Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], log_level.upper())
                config.log_level = logging.getLevelNamesMapping()[log_level]
            if trace_lsp_communication is not None:
                config.trace_lsp_communication = trace_lsp_communication
            if tool_timeout is not None:
                config.tool_timeout = tool_timeout
            if language_backend is not None:
                config.language_backend = language_backend

            mode_selection_def: ModeSelectionDefinition | None = None
            if modes:
                mode_selection_def = ModeSelectionDefinition(default_modes=modes)
            self.agent = self._create_serena_agent(config, mode_selection_def)

        except Exception as e:
            show_fatal_exception_safe(e)
            raise

        # Override model_config to disable the use of `.env` files for reading settings, because user projects are likely to contain
        # `.env` files (e.g. containing LOG_LEVEL) that are not supposed to override the MCP settings;
        # retain only FASTMCP_ prefix for already set environment variables.
        Settings.model_config = SettingsConfigDict(env_prefix="FASTMCP_")
        instructions = self._get_initial_instructions()
        mcp = FastMCP(
            name="Serena",
            lifespan=self.server_lifespan,
            website_url="https://oraios.github.io/serena",
            host=host,
            port=port,
            instructions=instructions,
        )
        # Register tools once at server creation, not per SSE session: the MCP
        # SDK enters server_lifespan per Server.run() (i.e. per SSE connection),
        # so per-session registration would race on the shared tool dict.
        openai_tool_compatible = self.context.name in ["chatgpt", "codex", "oaicompat-agent"]
        self._set_mcp_tools(mcp, openai_tool_compatible=openai_tool_compatible)

        # Tear down the agent at process exit, not per SSE session: calling
        # agent.on_shutdown() in server_lifespan's finally block killed the
        # language server on every client disconnect, breaking other concurrent
        # sessions with "No active project" and 15-min hangs (regression from 48025c7d).
        atexit.register(self._on_process_exit)

        return mcp

    @asynccontextmanager
    async def server_lifespan(self, mcp_server: FastMCP) -> AsyncIterator[None]:
        """Per-SSE-session lifespan; intentionally a no-op.

        The MCP SDK enters this context per Server.run(), which FastMCP invokes
        per SSE connection. Tool registration and agent shutdown live in
        create_mcp_server, not here.
        """
        log.info("MCP session opened")
        try:
            yield
        finally:
            log.info("MCP session closed")

    def _on_process_exit(self) -> None:
        """Tear down the agent at process exit (atexit handler)."""
        if self.agent is not None:
            self.agent.on_shutdown()

    def _get_initial_instructions(self) -> str:
        assert self.agent is not None
        return self.agent.create_system_prompt()


class SerenaCatalogProvider(CatalogProvider):
    """Production :class:`CatalogProvider` that emits Serena's tool catalog as wire-format dicts.

    Iterates the agent's exposed tools (the same set returned by
    :meth:`SerenaMCPFactory._iter_tools`) and converts each Serena ``Tool`` to a
    wire-format ``mcp.types.Tool`` dict via :meth:`SerenaMCPFactory.make_mcp_tool`.
    The resulting list is returned verbatim from every ``pipe/catalog/get`` request;
    the catalog is session-agnostic in this iteration since Serena's exposed tool
    set is fixed at server creation. Per-session shaping (e.g. project-conditional
    tool availability) would re-use the ``session_id`` parameter in a future revision.
    """

    def __init__(self, agent: SerenaAgent, openai_tool_compatible: bool = False) -> None:
        """
        :param agent: The :class:`SerenaAgent` whose exposed tools form the catalog.
        :param openai_tool_compatible: Whether to apply the OpenAI-compatible schema sanitization
            (mirrors :meth:`SerenaMCPFactory._set_mcp_tools`'s switch). Default ``False`` matches
            the standard MCP wire format; OpenAI-compatible clients (``chatgpt``, ``codex``,
            ``oaicompat-agent`` contexts) opt in by setting this ``True``.
        """
        self._agent = agent
        self._openai_tool_compatible = openai_tool_compatible

    async def get_catalog(self, session_id: str) -> list[dict[str, Any]]:
        """Return the JSON-serializable tool definitions for the requesting pipe connection.

        :param session_id: The daemon-allocated session_id; ignored in this iteration since
            Serena's catalog does not vary per session, but accepted to satisfy the
            :class:`CatalogProvider` contract and leave room for session-scoped catalogs.
        :returns: A list of dicts, each shaped as a wire-format ``mcp.types.Tool`` JSON
            object (``name``, ``description``, ``inputSchema``, optional ``annotations``,
            optional ``title``). Empty when the agent exposes no tools.
        """
        catalog: list[dict[str, Any]] = []
        for tool in self._agent.get_exposed_tool_instances():
            mcp_tool = SerenaMCPFactory.make_mcp_tool(tool, openai_tool_compatible=self._openai_tool_compatible)
            wire_tool = WireTool(
                name=mcp_tool.name,
                description=mcp_tool.description,
                inputSchema=mcp_tool.parameters,
                annotations=mcp_tool.annotations,
                title=mcp_tool.title,
            )
            catalog.append(wire_tool.model_dump(by_alias=True, exclude_none=True))
        return catalog


class SerenaPipeFrameHandler(FrameHandler):
    """Production :class:`FrameHandler` that dispatches JSON-RPC frames to Serena tools directly.

    The daemon-side dispatch is a thin JSON-RPC method router that handles the small set of
    methods a host issues after handshake (``initialize``, ``notifications/initialized``,
    ``tools/call``, ``prompts/list``, ``resources/list``, ``ping``). For every dispatch
    ``_PIPE_SESSION_ID_VAR`` is set to the pipe-asserted session_id BEFORE any handler runs,
    so :meth:`Tool.apply_ex`'s session keying picks it up via the ContextVar; the per-session
    ``_active_projects_by_session`` and ``_cursor_managers_by_session`` maps then route
    correctly without needing the SDK's transport-layer Session machinery.

    ``tools/list`` requests never reach this handler -- the pipe forwarder
    (:func:`serena.pipe._run_forwarder`) answers them locally from the catalog fetched at
    handshake time, and the listener intercepts ``pipe/catalog/get`` envelopes before
    FrameHandler dispatch. The handler therefore deliberately omits a ``tools/list`` branch.

    The dispatch is direct rather than a wrapping of the SDK's lowlevel ``Server.run``
    coroutine. The wrapped form is per-connection long-lived and keeps a separate
    anyio-stream pump per session; the per-frame form here is simpler, has fewer moving
    parts, and exercises the same :meth:`Tool.apply_ex` invocation path the SDK would have
    routed to. The ``_PIPE_SESSION_ID_VAR`` keying is the only hard requirement -- whichever
    integration shape we choose, that ContextVar must be set before any tool dispatch.
    """

    def __init__(self, agent: SerenaAgent) -> None:
        """
        :param agent: The :class:`SerenaAgent` whose exposed tools handle ``tools/call`` requests.
        """
        self._agent = agent

    async def handle(self, session_id: str, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Set ``_PIPE_SESSION_ID_VAR`` and dispatch one JSON-RPC frame.

        :param session_id: The daemon-allocated session_id for the originating pipe connection.
            Set into ``_PIPE_SESSION_ID_VAR`` for the duration of this dispatch so any tool
            invoked during the call sees it via the ContextVar.
        :param frame: The JSON-RPC frame as a parsed dict (already JSON-decoded by
            :class:`PipeEnvelope`).
        :returns: The JSON-RPC response frame to ferry back over the pipe, or ``None`` for
            JSON-RPC notifications (frames without an ``id``) and any other request that
            does not produce a response.
        """
        from serena.agent import _PIPE_SESSION_ID_VAR

        # set on this Task's ContextVar context so concurrent connections (each running in its
        # own _forward_frames Task with its own context copy) cannot race on the var; the
        # finally-reset preserves the caller's prior value if there ever is one (there will not
        # be in production, but tests sometimes pre-set the var to assert handler behaviour)
        token = _PIPE_SESSION_ID_VAR.set(session_id)
        try:
            return await self._dispatch(frame)
        finally:
            _PIPE_SESSION_ID_VAR.reset(token)

    async def _dispatch(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Route ``frame`` to the matching method handler and produce the response shape.

        :param frame: The JSON-RPC frame as a parsed dict.
        :returns: The JSON-RPC response frame, or ``None`` for notifications.
        """
        method = frame.get("method")
        frame_id = frame.get("id")

        if frame_id is None:
            # JSON-RPC notification: no response is sent. We deliberately do not
            # raise on unknown notifications -- the host can send transport-level
            # signals (notifications/initialized, notifications/cancelled, ...) that
            # the daemon side has no explicit handler for and yet must not error on.
            log.debug("SerenaPipeFrameHandler: notification %r dropped", method)
            return None

        try:
            if method == "initialize":
                result = self._handle_initialize(frame.get("params") or {})
            elif method == "tools/call":
                # tools/call delegates to Tool.apply_ex, which is synchronous and blocks on its
                # own internal task_executor. Calling that directly inside the asyncio Task would
                # pin the event loop until the tool finishes -- starving every other live pipe
                # connection. Run it via the loop's default executor with the current Task's
                # context (so _PIPE_SESSION_ID_VAR stays set inside apply_ex's worker too).
                ctx = contextvars.copy_context()
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None, partial(ctx.run, self._handle_tools_call, frame.get("params") or {})
                )
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "ping":
                result = {}
            else:
                return self._error_response(frame_id, -32601, f"Method not found: {method!r}")
            return {"jsonrpc": "2.0", "id": frame_id, "result": result}
        except Exception as exc:
            log.exception("SerenaPipeFrameHandler: dispatch failed for method=%r", method)
            return self._error_response(frame_id, -32603, f"Internal error: {exc}")

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Build the ``initialize`` response from the host's request parameters.

        Echoes the host's ``protocolVersion`` if supplied (so a host that pins to a specific
        revision sees that revision back), advertises Serena's tools/prompts/resources
        capabilities, and stamps ``serverInfo`` with the running version so the host's
        diagnostic output reflects the daemon it actually connected to.
        """
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {
                "tools": {},
                "prompts": {},
                "resources": {},
            },
            "serverInfo": {
                "name": "Serena",
                "version": serena_version(),
            },
        }

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """Look up the named Serena tool and invoke ``apply_ex`` with the request arguments.

        ``mcp_ctx=None`` is passed deliberately: the pipe transport supplies the session
        identity via ``_PIPE_SESSION_ID_VAR`` (set by :meth:`handle`), and the SDK's
        transport-layer Session is not present in the daemon-side dispatch path. Tool errors
        are caught by :meth:`Tool.apply_ex` itself (with ``catch_exceptions=True``); the
        method returns the tool's stringified output, which we wrap in the standard MCP
        text-content shape.
        """
        name = params.get("name")
        if not isinstance(name, str):
            raise ValueError("tools/call params.name must be a string")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("tools/call params.arguments must be an object")

        # exposed-tools lookup -- the catalog (advertised at handshake) is keyed off the
        # same list, so any name the host can issue must resolve here. An unknown name is
        # a contract violation by the host (or an indication the daemon catalog drifted
        # from the pipe's cached catalog) and surfaces as a tools-call-level error rather
        # than a JSON-RPC method-not-found, which is reserved for unknown JSON-RPC methods.
        tool = next((t for t in self._agent.get_exposed_tool_instances() if t.get_name() == name), None)
        if tool is None:
            raise ValueError(f"Unknown tool: {name!r}")

        result = tool.apply_ex(log_call=True, catch_exceptions=True, mcp_ctx=None, **arguments)
        return {"content": [{"type": "text", "text": result}]}

    @staticmethod
    def _error_response(frame_id: Any, code: int, message: str) -> dict[str, Any]:
        """Build a JSON-RPC error response with the given code and message."""
        return {
            "jsonrpc": "2.0",
            "id": frame_id,
            "error": {"code": code, "message": message},
        }
