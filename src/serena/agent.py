"""
The Serena Model Context Protocol (MCP) Server
"""

import json
import multiprocessing
import os
import platform
import signal
import subprocess
import sys
import threading
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from logging import Logger
from typing import TYPE_CHECKING, Optional, TypeVar

import requests
import webview
from sensai.util import logging
from sensai.util.logging import LogTime
from sensai.util.string import dict_string

from interprompt.jinja_template import JinjaTemplate
from serena import serena_version
from serena.analytics import RegisteredTokenCountEstimator, ToolUsageStats
from serena.config.context_mode import SerenaAgentContext, SerenaAgentMode
from serena.config.serena_config import (
    LanguageBackend,
    ModeSelectionDefinition,
    NamedToolInclusionDefinition,
    RegisteredProject,
    SerenaConfig,
    SerenaPaths,
    ToolInclusionDefinition,
)
from serena.dashboard import SerenaDashboardAPI, SerenaDashboardViewer
from serena.ls_manager import LanguageServerManager
from serena.project import MemoriesManager, Project
from serena.prompt_factory import SerenaPromptFactory
from serena.task_executor import TaskExecutor
from serena.tools import (
    ActivateProjectTool,
    GetCurrentConfigTool,
    GetLanguageServerStatusTool,
    OpenDashboardTool,
    ReadMemoryTool,
    Tool,
    ToolMarker,
    ToolRegistry,
)
from serena.util.gui import system_has_usable_display
from serena.util.inspection import iter_subclasses
from serena.util.logging import MemoryLogHandler
from solidlsp.ls_config import Language

if TYPE_CHECKING:
    from serena.cursor import CursorManager
    from serena.gui_log_viewer import GuiLogViewer

import contextvars as _contextvars

_UNSET: object = object()
# Per-MCP-session active project, keyed by id(mcp_ctx.session). The MCP daemon
# holds one SerenaAgent across every connected client; without per-session
# isolation, sessions race on a single global active_project slot and resolve
# paths against each other's projects (see Tool.apply_ex for where this is set).
_SESSION_KEY_VAR: "_contextvars.ContextVar[int | None]" = _contextvars.ContextVar(
    "serena_session_key", default=None
)
# In-flight resolved active project for the current call. Read by every
# accessor (get_active_project, the _active_project property) so direct field
# reads scattered across the agent see the per-session value, not the global
# legacy slot.
_ACTIVE_PROJECT_VAR: "_contextvars.ContextVar[object]" = _contextvars.ContextVar(
    "serena_active_project", default=_UNSET
)
# IRONCLAD zero-crossover guard. Set to True inside Tool.apply_ex's worker-thread
# closure for every MCP call. While True, the per-session lookup miss path in
# the _active_project getter and get_cursor_manager MUST NOT fall through to the
# legacy single-slot fields (_legacy_active_project, _legacy_cursor_manager) --
# those slots exist only for non-MCP callers (CLI, dashboard, tests) and serving
# them inside an MCP call admits cross-project state confusion between
# simultaneous clients. On a miss, raise/return-None instead.
_MCP_CALL_IN_FLIGHT: "_contextvars.ContextVar[bool]" = _contextvars.ContextVar(
    "serena_mcp_call_in_flight", default=False
)
# Pipe-asserted stable session_id for clients connecting via the per-client stdio
# pipe (src/serena/pipe.py + src/serena/daemon_pipe.py). The daemon's pipe
# FrameHandler sets this ContextVar to PipeConnection.session_id BEFORE invoking
# any per-frame dispatch; Tool.apply_ex reads it to derive the session_key,
# routing per-session state under the pipe-asserted UUID rather than
# id(mcp_ctx.session). Stable across the pipe's lifetime, so per-session state
# survives MCP-transport churn (streamable-http reuses the connection but creates
# a fresh mcp_ctx.session per request, which would otherwise rotate the
# id()-derived key on every call).
_PIPE_SESSION_ID_VAR: "_contextvars.ContextVar[str | None]" = _contextvars.ContextVar(
    "serena_pipe_session_id", default=None
)


log = logging.getLogger(__name__)

# maximum number of seconds get_project_activation_message waits for the backgrounded
# language-server-manager init task to complete before reporting 'still initializing';
# larger than a typical LSP startup (a few seconds for Pyright, longer for Metals), but
# bounded so a hung init cannot stall the activation response indefinitely
LS_MANAGER_INIT_WAIT_SECONDS = 10.0

TTool = TypeVar("TTool", bound="Tool")
T = TypeVar("T")
SUCCESS_RESULT = "OK"


class ProjectNotFoundError(Exception):
    pass


class AvailableTools:
    """
    Represents the set of available/exposed tools of a SerenaAgent.
    """

    def __init__(self, tools: list[Tool]):
        """
        :param tools: the list of available tools
        """
        self.tools = tools
        self.tool_names = sorted([tool.get_name_from_cls() for tool in tools])
        """
        the list of available tool names, sorted alphabetically
        """
        self._tool_name_set = set(self.tool_names)
        self.tool_marker_names = set()
        for marker_class in iter_subclasses(ToolMarker):
            for tool in tools:
                if isinstance(tool, marker_class):
                    self.tool_marker_names.add(marker_class.__name__)

    def __len__(self) -> int:
        return len(self.tools)

    def contains_tool_name(self, tool_name: str) -> bool:
        return tool_name in self._tool_name_set

    def contains_tool_class(self, tool_class: type[Tool]) -> bool:
        return self.contains_tool_name(tool_class.get_name_from_cls())


class ToolSet:
    """
    Represents a set of tools by their names.
    """

    LEGACY_TOOL_NAME_MAPPING: dict[str, str] = {}
    """
    maps legacy tool names to their new names for backward compatibility
    """

    def __init__(self, tool_names: set[str]) -> None:
        self._tool_names = tool_names

    def __len__(self) -> int:
        return len(self._tool_names)

    @classmethod
    def default(cls) -> "ToolSet":
        """
        :return: the default tool set, which contains all tools that are enabled by default
        """
        from serena.tools import ToolRegistry

        return cls(set(ToolRegistry().get_tool_names_default_enabled()))

    def apply(self, *tool_inclusion_definitions: "ToolInclusionDefinition") -> "ToolSet":
        """
        Applies one or more tool inclusion definitions to this tool set,
        resulting in a new tool set.

        :param tool_inclusion_definitions: the definitions to apply
        :return: a new tool set with the definitions applied
        """
        from serena.tools import ToolRegistry

        def get_updated_tool_name(tool_name: str) -> str:
            """Retrieves the updated tool name if the provided tool name is deprecated, logging a warning."""
            if tool_name in self.LEGACY_TOOL_NAME_MAPPING:
                new_tool_name = self.LEGACY_TOOL_NAME_MAPPING[tool_name]
                log.warning("Tool name '%s' is deprecated, please use '%s' instead", tool_name, new_tool_name)
                return new_tool_name
            return tool_name

        registry = ToolRegistry()
        tool_names = set(self._tool_names)
        for definition in tool_inclusion_definitions:
            if definition.is_fixed_tool_set():
                tool_names = set()
                for fixed_tool in definition.fixed_tools:
                    fixed_tool = get_updated_tool_name(fixed_tool)
                    if registry.check_valid_tool_name(fixed_tool, " (in fixed tools set)"):
                        tool_names.add(fixed_tool)
                log.info(f"{definition} defined a fixed tool set with {len(tool_names)} tools: {', '.join(tool_names)}")
            else:
                included_tools = []
                excluded_tools = []
                for included_tool in definition.included_optional_tools:
                    included_tool = get_updated_tool_name(included_tool)
                    if registry.check_valid_tool_name(included_tool, " (in included optional tools)") and included_tool not in tool_names:
                        tool_names.add(included_tool)
                        included_tools.append(included_tool)
                for excluded_tool in definition.excluded_tools:
                    excluded_tool = get_updated_tool_name(excluded_tool)
                    registry.check_valid_tool_name(excluded_tool, " (in excluded tools)")
                    if excluded_tool in tool_names:
                        tool_names.remove(excluded_tool)
                        excluded_tools.append(excluded_tool)
                if included_tools:
                    log.info(f"{definition} included {len(included_tools)} tools: {', '.join(included_tools)}")
                if excluded_tools:
                    log.info(f"{definition} excluded {len(excluded_tools)} tools: {', '.join(excluded_tools)}")
        return ToolSet(tool_names)

    def without_editing_tools(self) -> "ToolSet":
        """
        :return: a new tool set that excludes all tools that can edit
        """
        from serena.tools import ToolRegistry

        registry = ToolRegistry()
        tool_names = set(self._tool_names)
        for tool_name in self._tool_names:
            if registry.get_tool_class_by_name(tool_name).can_edit():
                tool_names.remove(tool_name)
        return ToolSet(tool_names)

    def get_tool_names(self) -> set[str]:
        """
        Returns the names of the tools that are currently included in the tool set.
        """
        return self._tool_names

    def includes_name(self, tool_name: str) -> bool:
        return tool_name in self._tool_names

    def to_available_tools(self, all_tools: dict[type[Tool], Tool]) -> AvailableTools:
        return AvailableTools([t for t in all_tools.values() if self.includes_name(t.get_name())])


class ActiveModes:
    def __init__(self) -> None:
        self._base_modes: Sequence[str] | None = None
        self._default_modes: Sequence[str] | None = None
        self._active_mode_names: Sequence[str] | None = []
        self._active_modes: Sequence[SerenaAgentMode] | None = []

    def apply(self, mode_selection: ModeSelectionDefinition) -> None:
        # invalidate active modes
        self._active_mode_names = None
        self._active_modes = None

        # apply overrides
        log.debug("Applying mode selection: default_modes=%s, base_modes=%s", mode_selection.default_modes, mode_selection.base_modes)
        if mode_selection.base_modes is not None:
            self._base_modes = mode_selection.base_modes
        if mode_selection.default_modes is not None:
            self._default_modes = mode_selection.default_modes
        log.debug("Current mode selection: base_modes=%s, default_modes=%s", self._base_modes, self._default_modes)

    def get_mode_names(self) -> Sequence[str]:
        if self._active_mode_names is not None:
            return self._active_mode_names
        active_mode_names: set[str] = set()
        if self._base_modes is not None:
            active_mode_names.update(self._base_modes)
        if self._default_modes is not None:
            active_mode_names.update(self._default_modes)
        self._active_mode_names = sorted(active_mode_names)
        log.info("Active modes: %s", self._active_mode_names)
        return self._active_mode_names

    def get_modes(self) -> Sequence[SerenaAgentMode]:
        if self._active_modes is not None:
            return self._active_modes
        self._active_modes = []
        for mode_name in self.get_mode_names():
            mode = SerenaAgentMode.load(mode_name)
            self._active_modes.append(mode)
        return self._active_modes

    # TODO: apply caching like in get_modes
    def get_default_modes(self) -> Sequence[SerenaAgentMode]:
        return [SerenaAgentMode.load(mode_name) for mode_name in self._default_modes or []]

    def get_base_modes(self) -> Sequence[SerenaAgentMode]:
        return [SerenaAgentMode.load(mode_name) for mode_name in self._base_modes or []]


class SerenaAgent:
    def __init__(
        self,
        project: str | None = None,
        project_activation_callback: Callable[[], None] | None = None,
        serena_config: SerenaConfig | None = None,
        context: SerenaAgentContext | None = None,
        modes: ModeSelectionDefinition | None = None,
        memory_log_handler: MemoryLogHandler | None = None,
    ):
        """
        :param project: the project to load immediately or None to not load any project; may be a path to the project or a name of
            an already registered project;
        :param project_activation_callback: a callback function to be called when a project is activated.
        :param serena_config: the Serena configuration or None to read the configuration from the default location.
        :param context: the context in which the agent is operating, None for default context.
            The context may adjust prompts, tool availability, and tool descriptions.
        :param modes: list of modes in which the agent is operating (they will be combined), None for default modes.
            The modes may adjust prompts, tool availability, and tool descriptions.
        :param memory_log_handler: a MemoryLogHandler instance from which to read log messages; if None, a new one will be created
            if necessary.
        """
        # per-MCP-session active project map, populated when Tool.apply_ex routes a call from a session,
        # so reads via the _active_project property return the project for the calling session rather than
        # whichever session most recently activated. Keys are either id(mcp_ctx.session) (int, for direct
        # stdio / streamable-http / sse transports) or the pipe-asserted UUID4 hex string (for clients
        # connecting via the per-client stdio pipe; see _PIPE_SESSION_ID_VAR).
        self._active_projects_by_session: dict[str | int, Project] = {}
        # fallback active project for non-MCP callers (CLI, dashboard, scripts, tests) that have no session
        # in scope; the property setter writes here when _SESSION_KEY_VAR is unset.
        self._legacy_active_project: Project | None = None
        self._active_project = None  # routed through the _active_project property to the per-session map or legacy slot
        # per-session cursor managers, keyed identically to _active_projects_by_session. Each entry is
        # bound to that session's active project at the moment the manager was constructed; a per-session
        # project switch invalidates only that session's entry (see _activate_project) so concurrent
        # sessions' cursors survive.
        self._cursor_managers_by_session: dict[str | int, "CursorManager"] = {}
        # fallback cursor manager for non-MCP callers (CLI, dashboard, scripts, tests) with no session in scope;
        # get_cursor_manager writes here when _SESSION_KEY_VAR is unset.
        self._legacy_cursor_manager: "CursorManager | None" = None
        # weakref-based finalizers for evicting per-session entries when the MCP session object is GC'd.
        # Without this, _active_projects_by_session and _cursor_managers_by_session accumulate entries for
        # every session that ever connected (leak), and id() reuse can let a future session inherit stale
        # state at the same memory address. Registration happens lazily on the first tool call from a
        # given session in Tool.apply_ex; the finalizer drops both per-session dict entries on GC.
        self._session_finalizers: dict[int, weakref.finalize] = {}
        self._session_finalizers_lock = threading.Lock()
        # startup activation error preserved here so the first tool call that requires
        # the project can surface the real cause instead of a generic "No active project".
        self._startup_activation_error: Exception | None = None
        self._startup_activation_target: str | None = None
        # task handle for the backgrounded language-server-manager initialization of the
        # currently-activating project; the activation message path waits on this handle so
        # per-language LSP startup failures can be surfaced synchronously to the caller
        self._ls_manager_init_task: TaskExecutor.Task[None] | None = None
        self._gui_log_viewer: Optional["GuiLogViewer"] = None
        self._dashboard_viewer_process: multiprocessing.Process | None = None

        self.version = serena_version()

        # obtain serena configuration using the decoupled factory function
        self.serena_config = serena_config or SerenaConfig.from_config_file()

        # propagate configuration to other components
        self.serena_config.propagate_settings()

        # determine registered project to be activated (if any)
        registered_project_to_activate: RegisteredProject | None = (
            self.serena_config.get_registered_project(project, autoregister=True) if project is not None else None
        )

        # dashboard URL (set when dashboard is started)
        self._dashboard_url: str | None = None

        # adjust log level
        serena_log_level = self.serena_config.log_level
        if Logger.root.level != serena_log_level:
            log.info(f"Changing the root logger level to {serena_log_level}")
            Logger.root.setLevel(serena_log_level)

        def get_memory_log_handler() -> MemoryLogHandler:
            nonlocal memory_log_handler
            if memory_log_handler is None:
                memory_log_handler = MemoryLogHandler(level=serena_log_level)
                Logger.root.addHandler(memory_log_handler)
            return memory_log_handler

        # open GUI log window if enabled
        if self.serena_config.gui_log_window:
            log.info("Opening GUI window")
            if platform.system() == "Darwin":
                log.warning("GUI log window is not supported on macOS")
            else:
                # even importing on macOS may fail if tkinter dependencies are unavailable (depends on Python interpreter installation
                # which uv used as a base, unfortunately)
                from serena.gui_log_viewer import GuiLogViewer

                self._gui_log_viewer = GuiLogViewer(
                    "dashboard",
                    title="Serena Logs",
                    memory_log_handler=get_memory_log_handler(),
                    shutdown_handler=lambda: self.shutdown(),
                )
                self._gui_log_viewer.start()
        else:
            log.debug("GUI window is disabled")

        # set the agent context
        if context is None:
            context = SerenaAgentContext.load_default()
        self._context = context

        # instantiate all tool classes
        self._all_tools: dict[type[Tool], Tool] = {tool_class: tool_class(self) for tool_class in ToolRegistry().get_all_tool_classes()}
        tool_names = [tool.get_name_from_cls() for tool in self._all_tools.values()]

        # If GUI log window is enabled, set the tool names for highlighting
        if self._gui_log_viewer is not None:
            self._gui_log_viewer.set_tool_names(tool_names)

        token_count_estimator = RegisteredTokenCountEstimator[self.serena_config.token_count_estimator]
        log.info(f"Will record tool usage statistics with token count estimator: {token_count_estimator.name}.")
        self._tool_usage_stats = ToolUsageStats(token_count_estimator)

        # log fundamental information
        log.info(
            f"Starting Serena server (version={self.version}, process id={os.getpid()}, parent process id={os.getppid()}; "
            f"language backend={self.serena_config.language_backend.name}); Python version={platform.python_version()}, platform={platform.platform()}"
        )
        log.info("Configuration file: %s", self.serena_config.config_file_path)
        log.info("Available projects: {}".format(", ".join(self.serena_config.project_names)))
        log.info(f"Loaded tools ({len(self._all_tools)}): {', '.join([tool.get_name_from_cls() for tool in self._all_tools.values()])}")

        self._check_shell_settings()

        # determine the effective language backend for this session.
        # If a startup project is provided and has a per-project override, use it; otherwise use the global config.
        # Since we don't want to change the toolset after startup, the language backend cannot be changed within a running Serena session
        self._language_backend = self.serena_config.language_backend
        if registered_project_to_activate is not None and registered_project_to_activate.project_config.language_backend is not None:
            self._language_backend = registered_project_to_activate.project_config.language_backend
            log.info(f"Using language backend as configured in project.yml: {self._language_backend.name}")
        else:
            log.info(f"Using language backend from global configuration: {self._language_backend.name}")

        # create executor for starting the language server and running tools in another thread
        # This executor is used to achieve linear task execution
        self._task_executor = TaskExecutor("SerenaAgentTaskExecutor")

        # Initialize the prompt factory
        self.prompt_factory = SerenaPromptFactory()
        self._project_activation_callback = project_activation_callback

        # activate the given project (if any), also updating the active modes
        # Note: We cannot update the active tools yet, because the base toolset has not been computed yet
        #       (and its computation depends on the active project)
        self._active_modes: ActiveModes
        self._mode_overrides = modes
        if project is not None:
            try:
                self.activate_project_from_path_or_name(project, update_active_modes=False, update_active_tools=False)
            except Exception as e:
                log.error(f"Error activating project '{project}' at startup: {e}", exc_info=e)
                # preserve the failure so get_active_project_or_raise can surface it to the first tool call
                self._startup_activation_error = e
                self._startup_activation_target = project
        self._update_active_modes()

        # determine the base toolset defining the set of exposed tools (which e.g. the MCP shall see),
        self._base_toolset = self._create_base_toolset(
            self.serena_config, self._language_backend, self._context, self._active_modes, self._active_project
        )
        self._exposed_tools = self._base_toolset.to_available_tools(self._all_tools)
        log.info(f"Number of exposed tools: {len(self._exposed_tools)}. Exposed tools: {self._exposed_tools.tool_names}")

        # update the active tools (considering the active project, if any)
        self._active_tools: AvailableTools
        self._update_active_tools()

        # start the dashboard (web frontend), registering its log handler
        # should be the last thing to happen in the initialization since the dashboard
        # may access various parts of the agent
        if self.serena_config.web_dashboard:
            self._dashboard_thread, port = SerenaDashboardAPI(
                get_memory_log_handler(), tool_names, agent=self, tool_usage_stats=self._tool_usage_stats
            ).run_in_thread(host=self.serena_config.web_dashboard_listen_address)
            dashboard_host = self.serena_config.web_dashboard_listen_address
            if dashboard_host == "0.0.0.0":
                dashboard_host = "localhost"
            dashboard_url = f"http://{dashboard_host}:{port}/dashboard/index.html"
            self._dashboard_url = dashboard_url
            log.info("Serena web dashboard started at %s", dashboard_url)
            self._start_dashboard_viewer(minimized=not self.serena_config.web_dashboard_open_on_launch)
            # inform the GUI window (if any)
            if self._gui_log_viewer is not None:
                self._gui_log_viewer.set_dashboard_url(dashboard_url)

        self._send_usage_info()

    def _send_usage_info(self) -> None:
        if os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("SERENA_USAGE_REPORTING") == "false":
            return
        params: dict[str, str | int] = {
            "os": platform.system(),
            "dashboard": int(self.serena_config.web_dashboard),
            "version": self.version,
            "backend": self._language_backend.value,
        }
        try:
            requests.get("https://oraios-software.de/serena_usage.php", params=params, timeout=1)
        except Exception as e:
            log.debug(f"Failed to send usage info: {e}")

    @classmethod
    def _create_base_toolset(
        cls,
        serena_config: SerenaConfig,
        language_backend: LanguageBackend,
        context: SerenaAgentContext,
        modes: ActiveModes,
        project: Project | None,
    ) -> ToolSet:
        """
        Determines the base toolset defining the set of exposed tools (which e.g. the MCP shall see).
        It depends on ...
           * dashboard availability/opening on launch
           * Serena config
           * the context (which is fixed for the session)
           * the optional tools enabled by initial modes
           * single-project mode reductions (if applicable)
           * JetBrains mode
        """
        # determine whether to include the OpenDashboardTool based on the Serena configuration
        tool_inclusion_definitions: list[ToolInclusionDefinition] = []
        if serena_config.web_dashboard and not serena_config.web_dashboard_open_on_launch and not serena_config.gui_log_window:
            tool_inclusion_definitions.append(
                NamedToolInclusionDefinition(name="OpenDashboard", included_optional_tools=[OpenDashboardTool.get_name_from_cls()])
            )

        # expose GetLanguageServerStatusTool by default so agents can query per-language LSP state
        # without reactivating the project; contexts that want to hide it can still exclude it by name
        tool_inclusion_definitions.append(
            NamedToolInclusionDefinition(
                name="OptionalLspStatus",
                included_optional_tools=[GetLanguageServerStatusTool.get_name_from_cls()],
            )
        )

        # consider Serena configuration and the active context
        tool_inclusion_definitions.append(serena_config)
        tool_inclusion_definitions.append(context)

        # consider modes
        # Since modes can be dynamically turned on and off, we don't include their definitions directly,
        # For the initially active dynamic modes, we make sure that the tools they enable are included.
        for mode in modes.get_default_modes():
            tool_inclusion_definitions.append(
                NamedToolInclusionDefinition(
                    name=f"InitialDynamicModeInclusions[{mode.name}]", included_optional_tools=mode.included_optional_tools
                )
            )
        # For the base modes, we also apply the tool exclusions, since they apply throughout the entire session
        for base_mode in modes.get_base_modes():
            tool_inclusion_definitions.append(
                NamedToolInclusionDefinition(
                    name=f"BaseMode[{base_mode.name}]",
                    included_optional_tools=base_mode.included_optional_tools,
                    excluded_tools=base_mode.excluded_tools,
                )
            )

        # When in a single-project context, the agent is assumed to work on a single project, and we thus
        # want to apply that project's tool exclusions/inclusions from the get-go, limiting the set
        # of tools that will be exposed to the client.
        # Furthermore, we disable tools that are only relevant for project activation.
        # So if the project exists, we apply all the aforementioned exclusions.
        if context.single_project and project is not None:
            log.info(
                "Applying tool inclusion/exclusion definitions for single-project context based on project '%s'",
                project.project_name,
            )
            tool_inclusion_definitions.append(
                NamedToolInclusionDefinition(
                    name="SingleProjectExclusions",
                    excluded_tools=[ActivateProjectTool.get_name_from_cls(), GetCurrentConfigTool.get_name_from_cls()],
                )
            )
            tool_inclusion_definitions.append(project.project_config)

        # enabled the internal 'jetbrains' mode for the JetBrains backend
        if language_backend == LanguageBackend.JETBRAINS:
            tool_inclusion_definitions.append(SerenaAgentMode.from_name_internal("jetbrains"))

        # compute the resulting tool set
        base_toolset = ToolSet.default().apply(*tool_inclusion_definitions)
        log.info(f"Number of exposed tools: {len(base_toolset)}")
        return base_toolset

    def get_language_backend(self) -> LanguageBackend:
        return self._language_backend

    def get_current_tasks(self) -> list[TaskExecutor.TaskInfo]:
        """
        Gets the list of tasks currently running or queued for execution.
        The function returns a list of thread-safe TaskInfo objects (specifically created for the caller).

        :return: the list of tasks in the execution order (running task first)
        """
        return self._task_executor.get_current_tasks()

    def get_last_executed_task(self) -> TaskExecutor.TaskInfo | None:
        """
        Gets the last executed task.

        :return: the last executed task info or None if no task has been executed yet
        """
        return self._task_executor.get_last_executed_task()

    def get_language_server_manager(self) -> LanguageServerManager | None:
        if self._active_project is not None:
            return self._active_project.language_server_manager
        return None

    def get_language_server_manager_or_raise(self) -> LanguageServerManager:
        active_project = self.get_active_project_or_raise()
        return active_project.get_language_server_manager_or_raise()

    def get_cursor_manager(self) -> "CursorManager":
        """
        Get or create the :class:`CursorManager` for cursor-based code navigation.

        Cursor managers are routed per MCP session, mirroring how ``_active_project`` is routed:
        when ``_SESSION_KEY_VAR`` is set (Tool.apply_ex bound it), the manager is looked up in
        :attr:`_cursor_managers_by_session`. While an MCP call is in flight
        (``_MCP_CALL_IN_FLIGHT`` is True) the legacy single-slot field
        :attr:`_legacy_cursor_manager` is unreachable — IRONCLAD zero-crossover guard, mirroring
        the rule on :attr:`_active_project`. For non-MCP callers (CLI, dashboard, scripts, tests)
        the legacy slot applies as before. The manager is rebuilt when absent or when its bound
        project no longer matches the caller's active project — a per-session project switch
        invalidates only that session's manager.

        :return: a manager whose ``project`` matches the caller's active project.
        """
        from serena.cursor import CursorManager

        # determine the caller's slot — per-session if a session is in scope, otherwise the legacy slot.
        # While an MCP call is in flight, the legacy slot is unreachable to prevent cross-project leakage.
        session_key = _SESSION_KEY_VAR.get(None)
        mcp_in_flight = _MCP_CALL_IN_FLIGHT.get()
        if mcp_in_flight and session_key is None:
            raise RuntimeError(
                "Internal error: MCP call is in flight but _SESSION_KEY_VAR is unset. "
                "This indicates Tool.apply_ex did not propagate the session key into the worker "
                "context; refusing to fall through to the legacy cursor-manager slot because "
                "doing so would risk cross-project state confusion."
            )

        project = self.get_active_project_or_raise()
        existing: CursorManager | None
        if session_key is not None:
            existing = self._cursor_managers_by_session.get(session_key)
        else:
            # Non-MCP path (CLI/dashboard/tests). _MCP_CALL_IN_FLIGHT is False here by the guard above.
            existing = self._legacy_cursor_manager

        # rebuild when absent, or when bound to a stale project (the caller switched projects)
        if existing is None or existing.project.project_root != project.project_root:
            project.get_language_server_manager_or_raise()  # validate LSP is available
            new_mgr = CursorManager(project)
            if session_key is not None:
                self._cursor_managers_by_session[session_key] = new_mgr
            else:
                self._legacy_cursor_manager = new_mgr
            return new_mgr
        return existing

    def get_log_inspection_instructions(self) -> str:
        if self.serena_config.web_dashboard:
            return f"Live logs can be inspected via the dashboard at {self.get_dashboard_url()}"
        else:
            log_path = SerenaPaths().last_returned_log_file_path
            if log_path is not None:
                return f"Find the current log file here: {log_path}"
            else:
                return "Unfortunately, logs are not available. We recommend enabling the web dashboard/logging in general."

    def get_context(self) -> SerenaAgentContext:
        return self._context

    def get_tool_description_override(self, tool_name: str) -> str | None:
        return self._context.tool_description_overrides.get(tool_name, None)

    def _check_shell_settings(self) -> None:
        # On Windows, Claude Code sets COMSPEC to Git-Bash (often even with a path containing spaces),
        # which causes all sorts of trouble, preventing language servers from being launched correctly.
        # So we make sure that COMSPEC is unset if it has been set to bash specifically.
        if platform.system() == "Windows":
            comspec = os.environ.get("COMSPEC", "")
            if "bash" in comspec:
                os.environ["COMSPEC"] = ""  # force use of default shell
                log.info("Adjusting COMSPEC environment variable to use the default shell instead of '%s'", comspec)

    def record_tool_usage(self, input_kwargs: dict, tool_result: str | dict, tool: Tool) -> None:
        """
        Record the usage of a tool with the given input and output strings if tool usage statistics recording is enabled.
        """
        tool_name = tool.get_name()
        input_str = str(input_kwargs)
        output_str = str(tool_result)
        log.debug(f"Recording tool usage for tool '{tool_name}'")
        self._tool_usage_stats.record_tool_usage(tool_name, input_str, output_str)

    def get_dashboard_url(self) -> str | None:
        """
        :return: the URL of the web dashboard, or None if the dashboard is not running
        """
        return self._dashboard_url

    @staticmethod
    def _start_dashboard_viewer_process_function(url: str, minimized: bool, parent_process_id: int) -> None:
        """
        Main function of the subprocess for starting the dashboard viewer
        """
        try:
            SerenaDashboardViewer(url, start_minimized=minimized, parent_process_id=parent_process_id).run()
        except webview.errors.WebViewException as e:
            log.warning(f"Could not open Serena Dashboard viewer. Cause:\n{e}")
            # Fall back to opening the browser window if the window was supposed to be shown directly
            if not minimized:
                SerenaAgent._open_dashboard_in_browser(url)

    def _start_dashboard_viewer(self, minimized: bool) -> None:
        """
        Starts the dashboard viewer (in a separate process) or, if the current platform does not support it,
        opens the dashboard in the default web browser.

        :param minimized: whether the dashboard viewer should be started minimized (if supported on the current platform).
            If the viewer is not supported on the current platform, then this controls whether to open the browser window.
        """
        if not system_has_usable_display():
            log.info("Not starting the Serena dashboard viewer because no usable display was detected.")
            return

        url = self.get_dashboard_url()
        assert url is not None
        if SerenaDashboardViewer.is_current_platform_supported():
            self._dashboard_viewer_process = multiprocessing.Process(
                target=self._start_dashboard_viewer_process_function, args=(url, minimized, os.getpid()), daemon=True
            )
            self._dashboard_viewer_process.start()
        else:
            log.info("Not starting Serena dashboard viewer because the current platform does not support it; using browser-based fallback")
            if not minimized:
                self._open_dashboard_in_browser(url)

    def open_dashboard(self) -> bool:
        """
        Opens the Serena dashboard (for on-demand usage as triggered by the user, e.g. via a tool)

        :return: True if the dashboard was opened, False if it could not be opened
        """
        if self._dashboard_url is None:
            raise Exception("Dashboard is not running.")

        if not system_has_usable_display():
            log.warning("Not opening the Serena web dashboard because no usable display was detected.")
            return False

        self._open_dashboard_in_browser(self._dashboard_url)
        return True

    @staticmethod
    def _open_dashboard_in_browser(url: str) -> None:
        # Use a subprocess to avoid any output from webbrowser.open being written to stdout
        subprocess.Popen(
            [sys.executable, "-c", f"import webbrowser; webbrowser.open({url!r})"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=False,
        )

    def get_exposed_tool_instances(self) -> list["Tool"]:
        """
        :return: the tool instances which are exposed (e.g. to the MCP client).
            Note that the set of exposed tools is fixed for the session, as
            clients don't react to changes in the set of tools, so this is the superset
            of tools that can be offered during the session.
            If a client should attempt to use a tool that is dynamically disabled
            (e.g. because a project is activated that disables it), it will receive an error.
        """
        return list(self._exposed_tools.tools)

    @property
    def _active_project(self) -> "Project | None":
        """
        :return: the active project for the current call context. While an MCP call is in flight
            (``_MCP_CALL_IN_FLIGHT`` is True), the value MUST come from this session's own slot
            (in-flight ContextVar override or the per-session map); the process-global
            ``_legacy_active_project`` slot is unreachable from MCP code paths to make
            cross-project state confusion between simultaneous clients impossible.
            For non-MCP callers (CLI, dashboard, scripts, tests), the legacy single-slot
            fallback applies as before.
        """
        # in-flight override: takes precedence so active_project_context() and Tool.apply_ex() can pin
        # a project for the duration of a call without mutating the per-session dict.
        in_flight = _ACTIVE_PROJECT_VAR.get(_UNSET)
        if in_flight is not _UNSET:
            return in_flight  # type: ignore[return-value]

        # per-session lookup: when an MCP session is in scope, route to its dedicated slot.
        session_key = _SESSION_KEY_VAR.get(None)
        if session_key is not None:
            per_session = self._active_projects_by_session.get(session_key)
            if per_session is not None:
                return per_session

        # IRONCLAD zero-crossover guard: while an MCP call is in flight, the legacy single-slot
        # field is off-limits. Returning it would surface another simultaneous client's project
        # to this caller -- exactly the cross-project confusion this design forbids. Return None
        # so the caller's "no active project for this MCP session" error path fires loudly.
        if _MCP_CALL_IN_FLIGHT.get():
            return None

        # legacy single-slot: used by callers outside any MCP session (CLI, dashboard, tests).
        return self._legacy_active_project

    @_active_project.setter
    def _active_project(self, project: "Project | None") -> None:
        """
        Route assignments based on whether an MCP session is in scope.

        Within a session-bound call (Tool.apply_ex sets _SESSION_KEY_VAR), the per-session dict is updated;
        outside any session, the legacy fallback slot is updated. The in-flight ContextVar is also updated
        so any reads within the current call observe the new project immediately.
        """
        session_key = _SESSION_KEY_VAR.get(None)
        if session_key is not None:
            if project is None:
                self._active_projects_by_session.pop(session_key, None)
            else:
                self._active_projects_by_session[session_key] = project
        else:
            self._legacy_active_project = project
        # update the in-flight var only if it has been set on this call's context; otherwise leave the
        # default unset so callers without contextvar scoping (CLI/dashboard) keep falling through to
        # the per-session / legacy lookup in the getter.
        if _ACTIVE_PROJECT_VAR.get(_UNSET) is not _UNSET or session_key is not None:
            _ACTIVE_PROJECT_VAR.set(project)

    def _register_session_finalizer(self, mcp_session: object, session_key: int) -> None:
        """Lazily register a GC finalizer that drops this session's per-session entries.

        Called from ``Tool.apply_ex`` on every tool dispatch; the guard dict makes registration
        exactly-once per session. When the MCP session object is garbage-collected (its
        ``streamable-http`` task ends and the transport drops its references), the finalizer fires
        and pops ``session_key`` from :attr:`_active_projects_by_session` and
        :attr:`_cursor_managers_by_session`.

        Without this, per-session entries accumulate for every session that ever connected, and
        ``id()`` reuse lets a future session land on a dead session's key and inherit its state.

        :param mcp_session: the ``mcp_ctx.session`` object whose lifetime gates eviction.
        :param session_key: the per-session dict key (``id(mcp_session)``).
        """
        with self._session_finalizers_lock:
            if session_key in self._session_finalizers:
                return
            try:
                finalizer = weakref.finalize(mcp_session, self._evict_session_state, session_key)
            except TypeError as e:
                # weakref.finalize requires the target to support weak references; mock sessions
                # using plain ``object()`` don't. Skip silently — eviction will not fire, but
                # without a real session lifecycle there is nothing to evict against either.
                log.debug(f"Could not register session finalizer for key {session_key}: {e}")
                return
            self._session_finalizers[session_key] = finalizer

    def _evict_session_state(self, session_key: int) -> None:
        """Drop per-session entries for ``session_key``; invoked by the GC finalizer.

        Mirrors the dict pops performed by the ``_active_project`` setter and ``_activate_project``
        when the same session re-activates a project — does not shut down the project itself, since
        other concurrent sessions or the legacy slot may still reference it (project teardown is
        owned by :meth:`on_shutdown`).
        """
        self._active_projects_by_session.pop(session_key, None)
        self._cursor_managers_by_session.pop(session_key, None)
        with self._session_finalizers_lock:
            self._session_finalizers.pop(session_key, None)
        log.debug(f"Evicted per-session state for session_key={session_key}")

    def evict_pipe_session(self, session_id: str) -> None:
        """T6: Drop per-session entries for the pipe-asserted ``session_id``.

        Registered as a disconnect handler on the daemon's
        :class:`~serena.daemon_pipe.PipeListener`; fires when a pipe forwarder
        process exits (Unix-socket EOF, transport error, or listener stop).

        The eviction is deliberately distinct from
        :meth:`_evict_session_state`: that path is GC-driven and keyed on
        ``id(mcp_ctx.session)`` (an int) for direct stdio / streamable-http
        clients, while this method is disconnect-driven and keyed on the
        pipe-asserted UUID4 hex string. Both ultimately drop entries from the
        same per-session dicts, but the trigger and the key shape differ —
        see ``convention://global/handoff/...`` and
        ``plan://Serena:serena/serena-pipe-implementation`` for the design
        rationale (eviction must NOT fire on per-request streamable-http task
        end, only on pipe disconnect, so per-session state survives transport
        churn within a single client's lifetime).

        :param session_id: The pipe-asserted UUID4 hex string allocated by the
            daemon at handshake time. An unknown ``session_id`` is a no-op so
            defensive callers (e.g. listener stop() teardown after the pipe
            already closed) cannot raise.
        """
        self._active_projects_by_session.pop(session_id, None)
        self._cursor_managers_by_session.pop(session_id, None)
        log.debug(f"Evicted pipe session state for session_id={session_id}")

    def get_active_project(self) -> Project | None:
        """
        :return: the active project or None if no project is active
        """
        return self._active_project

    def get_active_project_or_raise(self) -> Project:
        """
        :return: the active project or raises an exception if no project is active.
            If the startup project activation failed, the original error is chained as the cause
            so the first tool call that requires the project reveals the real failure instead of
            a generic "No active project" message.
        """
        project = self.get_active_project()
        if project is None:
            startup_error = self._startup_activation_error
            if startup_error is not None:
                target = self._startup_activation_target or "the project specified at startup"
                raise ValueError(f"Project '{target}' specified at startup failed to activate: {startup_error}") from startup_error
            raise ValueError(
                "No active project for this MCP session. Call `activate_project` with the absolute "
                "path of the project root (it will be auto-registered if Serena does not yet know it). "
                "Per-session state does not survive daemon restarts; clients must re-activate on each "
                "fresh session."
            )
        return project

    def set_modes(self, mode_names: list[str]) -> None:
        """
        Set the current mode configurations.

        :param mode_names: List of mode names or paths to use
        """
        self._mode_overrides = ModeSelectionDefinition(default_modes=mode_names)
        self._update_active_modes()
        self._update_active_tools()

        log.info(f"Set modes to {[mode.name for mode in self.get_active_modes()]}")

    def get_active_modes(self) -> list[SerenaAgentMode]:
        """
        :return: the list of active modes
        """
        return list(self._active_modes.get_modes())

    def _format_prompt(self, prompt_template: str) -> str:
        template = JinjaTemplate(prompt_template)
        return template.render(available_tools=self._exposed_tools.tool_names, available_markers=self._exposed_tools.tool_marker_names)

    def create_system_prompt(self) -> str:
        available_tools = self._active_tools
        available_markers = available_tools.tool_marker_names
        global_memories = MemoriesManager(
            serena_data_folder=None, read_only_memory_patterns=self.serena_config.read_only_memory_patterns
        ).list_global_memories()
        global_memories_str = dict_string(global_memories.to_dict()) if len(global_memories) > 0 else ""
        log.info("Generating system prompt with available_tools=(see active tools), available_markers=%s", available_markers)
        system_prompt = self.prompt_factory.create_system_prompt(
            context_system_prompt=self._format_prompt(self._context.prompt),
            mode_system_prompts=[self._format_prompt(mode.prompt) for mode in self.get_active_modes()],
            available_tools=available_tools.tool_names,
            available_markers=available_markers,
            global_memories_list=global_memories_str,
        )

        # If a project is active at startup, append its activation message
        if self._active_project is not None:
            system_prompt += "\n\n" + self.get_project_activation_message()

        log.info("System prompt:\n%s", system_prompt)
        return system_prompt

    def get_project_activation_message(self) -> str:
        """
        :return: a message providing information about the project upon activation (e.g. programming language, memories, initial prompt)
        :raise: AssertionError if no project is active
        """
        proj = self._active_project
        assert proj is not None, "A project must be active before calling this."
        if proj.is_newly_created:
            msg = f"Created and activated a new project with name '{proj.project_name}' at {proj.project_root}. "
        else:
            msg = f"The project with name '{proj.project_name}' at {proj.project_root} is activated."
        if self._language_backend == LanguageBackend.LSP:
            languages_str = ", ".join([lang.value for lang in proj.project_config.languages])
            msg += f"\nProgramming languages: {languages_str}."

            # report per-language LSP health at activation time: the language server manager is initialized
            # asynchronously, so if we arrive here before startup has completed, wait on the init task with
            # a bounded timeout and then re-check; this closes the race between activate_project returning
            # and the backgrounded init task completing, so per-language failures (e.g. Metals crashing for
            # Scala) are surfaced in the activation message itself rather than only from logs or the next
            # tool call
            ls_manager = self.get_language_server_manager()
            if ls_manager is None and self._ls_manager_init_task is not None:
                self._ls_manager_init_task.wait_until_done(timeout=LS_MANAGER_INIT_WAIT_SECONDS)
                ls_manager = self.get_language_server_manager()
            if ls_manager is None:
                msg += "\nLanguage servers are still initializing; check logs or query get_current_config for the latest status."
            else:
                active_languages = ls_manager.get_active_languages()
                unavailable = ls_manager.get_unavailable_languages()
                if active_languages:
                    msg += f"\nActive language servers: {', '.join(lang.value for lang in active_languages)}."
                if unavailable:
                    failures = "; ".join(f"{lang.value}: {exc}" for lang, exc in unavailable.items())
                    msg += (
                        f"\nLanguage servers that failed to start: {failures}."
                        f" Tools that target these languages will raise a LanguageUnavailableError until the servers are"
                        f" restarted (use restart_language_server)."
                    )
        msg += f"File encoding: {proj.project_config.encoding}."

        include_memories = self._active_tools.contains_tool_class(ReadMemoryTool)
        if include_memories:
            project_memories = proj.memories_manager.list_project_memories()
            if project_memories:
                msg += (
                    f"\n{json.dumps(project_memories.to_dict())}\n"
                    + "Use the `read_memory` tool to read these memories later if they are relevant to the task."
                )
        if proj.project_config.initial_prompt:
            msg += f"\nAdditional project-specific instructions:\n {proj.project_config.initial_prompt}"
        return msg

    def _update_active_modes(self) -> None:
        """
        Updates the active modes based on the Serena configuration, the active project configuration (if any),
        and mode overrides (if any).
        """
        self._active_modes = ActiveModes()
        self._active_modes.apply(self.serena_config)
        if self._active_project:
            self._active_modes.apply(self._active_project.project_config)
        if self._mode_overrides:
            self._active_modes.apply(self._mode_overrides)

    def _update_active_tools(self) -> None:
        """
        Updates the active tools based on the active modes and the active project.
        The base tool set already takes the Serena configuration and the context into account
        (as well as many other aspects, such as JetBrains mode).
        """
        # apply modes
        tool_set = self._base_toolset.apply(*self._active_modes.get_modes())

        # apply active project configuration (if any)
        if self._active_project is not None:
            tool_set = tool_set.apply(self._active_project.project_config)
            if self._active_project.project_config.read_only:
                tool_set = tool_set.without_editing_tools()

        self._active_tools = tool_set.to_available_tools(self._all_tools)
        log.info(f"Active tools ({len(self._active_tools)}): {', '.join(self._active_tools.tool_names)}")

        # check if a tool was activated that is not in the exposed tool set and issue a warning if so
        active_tools_not_exposed = set(self._active_tools.tool_names) - set(self._exposed_tools.tool_names)
        if active_tools_not_exposed:
            log.warning(
                "The following active tools are not in the exposed tool set and thus won't be available to clients:\n"
                f"{active_tools_not_exposed}\n"
                "Consider adjusting your configuration to include these tools if you want to use them."
            )

    def issue_task(
        self, task: Callable[[], T], name: str | None = None, logged: bool = True, timeout: float | None = None
    ) -> TaskExecutor.Task[T]:
        """
        Issue a task to the executor for asynchronous execution.
        It is ensured that tasks are executed in the order they are issued, one after another.

        :param task: the task to execute
        :param name: the name of the task for logging purposes; if None, use the task function's name
        :param logged: whether to log management of the task; if False, only errors will be logged
        :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
        :return: the task object, through which the task's future result can be accessed
        """
        return self._task_executor.issue_task(task, name=name, logged=logged, timeout=timeout)

    def execute_task(self, task: Callable[[], T], name: str | None = None, logged: bool = True, timeout: float | None = None) -> T:
        """
        Executes the given task synchronously via the agent's task executor.
        This is useful for tasks that need to be executed immediately and whose results are needed right away.

        :param task: the task to execute
        :param name: the name of the task for logging purposes; if None, use the task function's name
        :param logged: whether to log management of the task; if False, only errors will be logged
        :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
        :return: the result of the task execution
        """
        return self._task_executor.execute_task(task, name=name, logged=logged, timeout=timeout)

    def is_using_language_server(self) -> bool:
        """
        :return: whether this agent uses language server-based code analysis
        """
        return self._language_backend == LanguageBackend.LSP

    def _activate_project(self, project: Project, update_active_modes: bool = True, update_active_tools: bool = True) -> bool:
        """
        :return: True if the project was newly activated, False if it was already active for the calling context
        """
        # check if the project is already active for the calling context (per session if one is in scope,
        # otherwise the legacy slot — see the _active_project property)
        current = self._active_project
        if current is not None and current.project_root == project.project_root:
            return False

        log.info(f"Activating {project.project_name} at {project.project_root}")

        # check if the project requires a different language backend than the one initialized at startup
        project_backend = project.project_config.language_backend
        if project_backend is not None and project_backend != self._language_backend:
            raise ValueError(
                f"Cannot activate project '{project.project_name}': it requires the {project_backend.value} backend, "
                f"but this session was initialized with {self._language_backend.value}. "
                f"Workarounds: (1) Use project activation at startup via the --project flag, "
                f"(2) Configure one MCP server per backend in your client."
            )

        # Note: the previous "shut down the previously active project" step has been removed.
        # In a multi-session MCP daemon, "switching" is per-session: other concurrent sessions may
        # still hold the previous project as their active one, and tearing down its language servers
        # here would break them (the same regression class addressed at process-exit-only teardown
        # in SerenaMCPFactory; see mcp.py around create_mcp_server). Projects now live until
        # process exit (on_shutdown) or an explicit configuration-changed reload.

        # update via the property setter, which routes to the per-session slot (or the legacy fallback)
        self._active_project = project
        # a successful activation invalidates any preserved startup-activation failure
        self._startup_activation_error = None
        self._startup_activation_target = None
        # invalidate the cursor manager for *this* calling context only — per-session if a session is in scope,
        # otherwise the legacy slot. A previous implementation cleared a single agent-wide attribute here, which
        # under multi-session use wiped concurrent sessions' cursors whenever any session switched projects.
        session_key = _SESSION_KEY_VAR.get(None)
        if session_key is not None:
            self._cursor_managers_by_session.pop(session_key, None)
        else:
            self._legacy_cursor_manager = None
        project.set_agent(self)

        if update_active_modes:
            self._update_active_modes()

        if update_active_tools:
            self._update_active_tools()

        def init_language_server_manager() -> None:
            # start the language server
            with LogTime("Language server initialization", logger=log):
                self.reset_language_server_manager()

        # initialize the language server in the background (if in language server mode);
        # the task handle is captured so the activation-message path can wait on it with
        # a bounded timeout before reporting on per-language LSP health
        self._ls_manager_init_task = None
        if self.get_language_backend().is_lsp():
            self._ls_manager_init_task = self.issue_task(init_language_server_manager)

        if self._project_activation_callback is not None:
            self._project_activation_callback()

        return True

    def activate_project_from_path_or_name(
        self, project_root_or_name: str, update_active_modes: bool = True, update_active_tools: bool = True
    ) -> bool:
        """
        Activate a project from a path or a name.
        If the project was already registered, it will just be activated. Any change to its ``project.yml``
        on disk since the MCP process started (or since the project was last activated) is picked up here:
        the on-disk configuration is re-read and, if it differs from the cached one, any memoized project
        instance is dropped and rebuilt. If the project with the changed configuration happens to be the
        currently active one, it is shut down first so that the activation fully re-initialises the language
        server manager with the updated language list.
        If the argument is a path at which no Serena project previously existed, the project will be created beforehand.
        Raises ProjectNotFoundError if the project could neither be found nor created.

        :return: True if the project was newly activated, False if it was already active
        """
        # locate the registered project (if any) so we can refresh its configuration from disk
        registered_project = self.serena_config.get_registered_project(project_root_or_name)

        project_instance: Project | None = None
        if registered_project is not None:
            # pick up any edits to project.yml made since the process started or the project was last activated
            config_changed = registered_project.reload_if_changed(self.serena_config)
            if (
                config_changed
                and self._active_project is not None
                and self._active_project.project_root == str(registered_project.project_root)
            ):
                log.info(
                    "Configuration changed for currently active project '%s'; shutting it down to re-initialise.",
                    registered_project.project_name,
                )
                self._active_project.shutdown()
                self._active_project = None
            project_instance = registered_project.get_project_instance(serena_config=self.serena_config)
            log.info(f"Found registered project '{project_instance.project_name}' at path {project_instance.project_root}")
        elif os.path.isdir(project_root_or_name):
            project_instance = self.serena_config.add_project_from_path(project_root_or_name)
            log.info(f"Added new project {project_instance.project_name} for path {project_instance.project_root}")

        if project_instance is None:
            raise ProjectNotFoundError(
                f"Project '{project_root_or_name}' not found: Not a valid project name or directory. "
                f"Existing project names: {self.serena_config.project_names}"
            )

        return self._activate_project(project_instance, update_active_modes=update_active_modes, update_active_tools=update_active_tools)

    def get_active_tool_names(self) -> list[str]:
        """
        :return: the list of names of the active tools for the current project, sorted alphabetically
        """
        return self._active_tools.tool_names

    def tool_is_active(self, tool_name: str) -> bool:
        """
        :param tool_class: the name of the tool to check
        :return: True if the tool is active, False otherwise
        """
        return self._active_tools.contains_tool_name(tool_name)

    def tool_is_exposed(self, tool_name: str) -> bool:
        """
        :param tool_name: the name of the tool to check
        :return: True if the tool is in the exposed tool set, False otherwise
        """
        return self._exposed_tools.contains_tool_name(tool_name)

    def get_current_config_overview(self) -> str:
        """
        :return: a string overview of the current configuration, including the active and available configuration options
        """
        result_str = "Current configuration:\n"
        result_str += f"Serena version: {self.version}\n"
        result_str += f"Loglevel: {self.serena_config.log_level}, trace_lsp_communication={self.serena_config.trace_lsp_communication}\n"
        if self._active_project is not None:
            result_str += f"Active project: {self._active_project.project_name}\n"
        else:
            result_str += "No active project\n"
        result_str += f"Language backend: {self._language_backend.value}"
        if self._active_project and self._active_project.project_config.language_backend is not None:
            result_str += " (project override)"
        result_str += f" (global default: {self.serena_config.language_backend.value})\n"
        result_str += "Available projects:\n" + "\n".join(list(self.serena_config.project_names)) + "\n"
        result_str += f"Active context: {self._context.name}\n"

        # Active modes
        active_mode_names = [mode.name for mode in self.get_active_modes()]
        result_str += "Active modes: {}\n".format(", ".join(active_mode_names)) + "\n"

        # Available but not active modes
        all_available_modes = SerenaAgentMode.list_registered_mode_names()
        inactive_modes = [mode for mode in all_available_modes if mode not in active_mode_names]
        if inactive_modes:
            result_str += "Available but not active modes: {}\n".format(", ".join(inactive_modes)) + "\n"

        # Active tools
        result_str += "Active tools (after all exclusions from the project, context, and modes):\n"
        active_tool_names = self.get_active_tool_names()
        # print the tool names in chunks
        chunk_size = 4
        for i in range(0, len(active_tool_names), chunk_size):
            chunk = active_tool_names[i : i + chunk_size]
            result_str += "  " + ", ".join(chunk) + "\n"

        # Available but not active tools
        all_tool_names = sorted([tool.get_name_from_cls() for tool in self._all_tools.values()])
        inactive_tool_names = [tool for tool in all_tool_names if tool not in active_tool_names]
        if inactive_tool_names:
            result_str += "Available but not active tools:\n"
            for i in range(0, len(inactive_tool_names), chunk_size):
                chunk = inactive_tool_names[i : i + chunk_size]
                result_str += "  " + ", ".join(chunk) + "\n"

        return result_str

    def reset_language_server_manager(self) -> None:
        """
        Starts/resets the language server manager for the current project
        """
        self.get_active_project_or_raise().create_language_server_manager()

    def add_language(self, language: Language) -> None:
        """
        Adds a new language to the active project, spawning the respective language server and updating the project configuration.
        The addition is scheduled via the agent's task executor and executed synchronously, i.e. the method returns
        when the addition is complete.

        :param language: the language to add
        """
        self.execute_task(lambda: self.get_active_project_or_raise().add_language(language), name=f"AddLanguage:{language.value}")

    def remove_language(self, language: Language) -> None:
        """
        Removes a language from the active project, shutting down the respective language server and updating the project configuration.
        The removal is scheduled via the agent's task executor and executed asynchronously.

        :param language: the language to remove
        """
        self.issue_task(lambda: self.get_active_project_or_raise().remove_language(language), name=f"RemoveLanguage:{language.value}")

    def get_tool(self, tool_class: type[TTool]) -> TTool:
        return self._all_tools[tool_class]  # type: ignore

    def print_tool_overview(self) -> None:
        ToolRegistry().print_tool_overview(self._active_tools.tools)

    def __del__(self) -> None:
        self.on_shutdown()

    def on_shutdown(self, timeout: float = 2.0) -> None:
        """
        Shutdown handler of the agent, freeing resources and stopping background tasks.

        Tears down every project held by any session (the per-session map plus the legacy single
        slot), deduplicating in case multiple sessions share the same Project instance.
        """
        log.info("SerenaAgent is shutting down ...")

        # collect every project instance held in any slot, deduplicated by identity
        projects: list[Project] = []
        seen_roots: set[str] = set()

        def add_project(project: Project | None) -> None:
            if project is None:
                return
            root = project.project_root
            if root in seen_roots:
                return
            seen_roots.add(root)
            projects.append(project)

        for project in self._active_projects_by_session.values():
            add_project(project)
        add_project(self._legacy_active_project)

        for project in projects:
            log.info(f"Shutting down active project '{project.project_name}' ...")
            project.shutdown(timeout=timeout)

        self._active_projects_by_session.clear()
        self._legacy_active_project = None
        # the cursor managers reference the projects we just shut down; drop them so they cannot be re-used
        self._cursor_managers_by_session.clear()
        self._legacy_cursor_manager = None
        # detach any live GC finalizers so they don't fire later as no-ops once the agent has been
        # torn down. ``finalize.detach()`` cancels the registration; ``clear()`` then drops the dict.
        with self._session_finalizers_lock:
            for finalizer in self._session_finalizers.values():
                finalizer.detach()
            self._session_finalizers.clear()

        if self._gui_log_viewer:
            log.info("Stopping the GUI log window ...")
            self._gui_log_viewer.stop()
            self._gui_log_viewer = None
        if self._dashboard_viewer_process:
            log.info("Stopping the dashboard viewer process ...")
            self._dashboard_viewer_process.terminate()
            self._dashboard_viewer_process = None

    def shutdown(self) -> None:
        """
        Triggers a hard shutdown of the agent, freeing resources and signalling the process to terminate
        """
        # perform clean-up right away, because kill does not result in normal deletion of the object
        self.on_shutdown()

        # signal process termination
        os.kill(os.getpid(), signal.SIGTERM)

    def get_tool_by_name(self, tool_name: str) -> Tool:
        tool_class = ToolRegistry().get_tool_class_by_name(tool_name)
        return self.get_tool(tool_class)

    def get_active_lsp_languages(self) -> list[Language]:
        ls_manager = self.get_language_server_manager()
        if ls_manager is None:
            return []
        return ls_manager.get_active_languages()

    def get_unavailable_lsp_languages(self) -> dict[Language, Exception]:
        """
        :return: a mapping from language to the captured startup exception for each language whose
            server failed to start (or failed a subsequent restart). Empty if no active project, no
            manager yet, or all requested languages are running.
        """
        ls_manager = self.get_language_server_manager()
        if ls_manager is None:
            return {}
        return ls_manager.get_unavailable_languages()

    @contextmanager
    def active_project_context(self, project: Project) -> Iterator[None]:
        """
        Context manager for temporarily setting/overriding the active project.

        The override uses _ACTIVE_PROJECT_VAR (a ContextVar): the per-session map and the legacy
        single-slot field are left untouched, so the override is observed by code reached from
        the ``with`` block but does not bleed into the calling session's persistent state.

        :param project: the project to be active
        """
        token = _ACTIVE_PROJECT_VAR.set(project)
        try:
            yield
        finally:
            _ACTIVE_PROJECT_VAR.reset(token)
