"""
The Serena Model Context Protocol (MCP) Server
"""
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import weakref
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from logging import Logger
from typing import TYPE_CHECKING, Optional, TypeVar, cast

import requests
import webview
from sensai.util import logging
from sensai.util.helper import mark_used
from sensai.util.logging import LogTime
from sensai.util.string import ToStringMixin, dict_string, list_string

import serena.util.shell  # noqa: F401 -- registers the continuation of a shell command's record
from interprompt.jinja_template import JinjaTemplate
from serena import serena_version
from serena.analytics import RegisteredTokenCountEstimator, ToolUsageStats
from serena.config.context_mode import SerenaAgentContext, SerenaAgentMode
from serena.config.serena_config import (
    AgentInterface,
    ModeSelectionDefinition,
    ModeSelectionDefinitionWithAddedModes,
    ModeSelectionDefinitionWithBaseModes,
    NamedApiInclusionDefinition,
    NamedToolInclusionDefinition,
    RegisteredProject,
    SerenaConfig,
    SerenaPaths,
    ToolInclusionDefinition,
)
from serena.dashboard.dashboard_api import SerenaDashboardAPI
from serena.dashboard.dashboard_tray_manager import SerenaDashboardTrayManager
from serena.dashboard.dashboard_viewer import SerenaDashboardViewer, open_url_in_browser
from serena.language_backend import BuiltinLanguageBackend, LanguageBackend
from serena.ls_manager import LanguageServerManager
from serena.memories.memory_manager import MemoryManager
from serena.project import Project
from serena.prompt_factory import SerenaPromptFactory
from serena.repl.api.cfg_api import ConfigApi
from serena.repl.api.edit_api import EditApi
from serena.repl.api.ext_api import ExternalProjectsApi
from serena.repl.api.fs_api import FsApi
from serena.repl.api.mem_api import MemoryApi
from serena.repl.api.shell_api import ShellApi
from serena.repl.facade import ApiScope, Facade
from serena.repl.repl import SerenaRepl
from serena.session import SerenaSession, SessionRegistry
from serena.task_executor import TaskExecutor
from serena.tools import (
    ActivateProjectTool,
    GetCurrentConfigTool,
    GetLanguageServerStatusTool,
    InitialInstructionsTool,
    OnboardingTool,
    OpenDashboardTool,
    ReadMemoryTool,
    RestartLanguageServerTool,
    SerenaReplTool,
    Tool,
    ToolMarker,
    ToolRegistry,
    tools_base,
)
from serena.util.gui import system_has_usable_display
from serena.util.inspection import iter_subclasses
from serena.util.logging import MemoryLogHandler
from serena.util.session_project_roots import ExplicitProjectRootStore
from solidlsp.ls_config import LanguageServerIdLike
from solidlsp.ls_survival import LANGUAGE_SERVER_KIND, LanguageServerStates
from solidlsp.structural.bridge_process import BRIDGE_KIND, BridgeStates
from solidlsp.util import subprocess_util, supervise
from solidlsp.util.subprocess_util import terminate_process_tree_with_kill_fallback

if TYPE_CHECKING:
    from serena.cursor import CursorManager
    from serena.gui_log_viewer import GuiLogViewer

import contextvars as _contextvars

_UNSET: object = object()
# Per-MCP-session active project, keyed by id(mcp_ctx.session). The MCP daemon
# holds one SerenaAgent across every connected client; without per-session
# isolation, sessions race on a single global active_project slot and resolve
# paths against each other's projects (see Tool.apply_ex for where this is set).
_SESSION_KEY_VAR: "_contextvars.ContextVar[int | None]" = _contextvars.ContextVar("serena_session_key", default=None)
# In-flight resolved active project for the current call. Read by every
# accessor (get_active_project, the _active_project property) so direct field
# reads scattered across the agent see the per-session value, not the global
# legacy slot.
_ACTIVE_PROJECT_VAR: "_contextvars.ContextVar[object]" = _contextvars.ContextVar("serena_active_project", default=_UNSET)
# IRONCLAD zero-crossover guard. Set to True inside Tool.apply_ex's worker-thread
# closure for every MCP call. While True, the per-session lookup miss path in
# the _active_project getter and get_cursor_manager MUST NOT fall through to the
# legacy single-slot fields (_legacy_active_project, _legacy_cursor_manager) --
# those slots exist only for non-MCP callers (CLI, dashboard, tests) and serving
# them inside an MCP call admits cross-project state confusion between
# simultaneous clients. On a miss, raise/return-None instead.
_MCP_CALL_IN_FLIGHT: "_contextvars.ContextVar[bool]" = _contextvars.ContextVar("serena_mcp_call_in_flight", default=False)
# Pipe-asserted stable session_id for clients connecting via the per-client stdio
# pipe (src/serena/pipe.py + src/serena/daemon_pipe.py). The daemon's pipe
# FrameHandler sets this ContextVar to PipeConnection.session_id BEFORE invoking
# any per-frame dispatch; Tool.apply_ex reads it to derive the session_key,
# routing per-session state under the pipe-asserted UUID rather than
# id(mcp_ctx.session). Stable across the pipe's lifetime, so per-session state
# survives MCP-transport churn (streamable-http reuses the connection but creates
# a fresh mcp_ctx.session per request, which would otherwise rotate the
# id()-derived key on every call).
_PIPE_SESSION_ID_VAR: "_contextvars.ContextVar[str | None]" = _contextvars.ContextVar("serena_pipe_session_id", default=None)
# The hold of the tool call running in this context, set by Tool._issue_call's task for every call of a session (see
# SerenaAgent._pin_session_project_for_call). It follows the session's slot while the call runs: a rebinding of the slot
# by the call itself moves the hold onto the newly bound project (see the _active_project setter).
_CALL_HOLD_VAR: "_contextvars.ContextVar[_CallHold | None]" = _contextvars.ContextVar("serena_call_hold", default=None)

_DASHBOARD_VIEWER_KIND = "serena.dashboard_viewer"
"""The waiting kind of the dashboard viewer's record, whose state names the dashboard's URL."""


log = logging.getLogger(__name__)

_SINGLE_SLOT_SESSION_ID = "single-slot"
"""the id of the Serena session of callers outside any session"""

TTool = TypeVar("TTool", bound="Tool")
T = TypeVar("T")
SUCCESS_RESULT = "OK"


class _CallHold:
    """
    The hold of one tool call in flight on the project it runs on, counted by :meth:`SerenaAgent._is_project_held` like a
    session's slot, so no release stops that project's language servers while the call runs.
    """

    def __init__(self, project: Project | None) -> None:
        """
        :param project: the project the call holds, or None while its session holds none
        """
        self.project = project


class ProjectNotFoundError(Exception):
    pass


class AvailableTools(ToStringMixin):
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

    def _tostring_object_info(self) -> str:
        return list_string(self.tool_names, brackets=None)

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

        def is_marker(name: str) -> bool:
            return name.startswith("ToolMarker")

        def get_marker(name: str) -> type[ToolMarker] | None:
            marker_cls = getattr(tools_base, name, None)
            if marker_cls is not None and issubclass(marker_cls, ToolMarker):
                return marker_cls
            return None

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
                    if is_marker(included_tool):
                        marker_class = get_marker(included_tool)
                        if marker_class is None:
                            log.warning(f"Tool marker '{included_tool}' not found; skipping inclusion.")
                            continue
                        for reg_tool in registry.iter_registered_tools():
                            if reg_tool.has_marker(marker_class) and reg_tool.tool_name not in tool_names:
                                tool_names.add(reg_tool.tool_name)
                                included_tools.append(reg_tool.tool_name)
                    else:
                        included_tool = get_updated_tool_name(included_tool)
                        if (
                            registry.check_valid_tool_name(included_tool, " (in included optional tools)")
                            and included_tool not in tool_names
                        ):
                            tool_names.add(included_tool)
                            included_tools.append(included_tool)
                for excluded_tool in definition.excluded_tools:
                    if is_marker(excluded_tool):
                        marker_class = get_marker(excluded_tool)
                        if marker_class is None:
                            log.warning(f"Tool marker '{excluded_tool}' not found; skipping exclusion.")
                            continue
                        for tool_name in list(tool_names):
                            reg_tool = registry.get_registered_tool_by_name(tool_name)
                            if reg_tool.has_marker(marker_class):
                                tool_names.remove(tool_name)
                                excluded_tools.append(tool_name)
                    else:
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
    _mode_instances: dict[str, SerenaAgentMode] = {}

    def __init__(self, background_base_modes: Sequence[SerenaAgentMode] | None = None) -> None:
        """
        :param background_base_modes: base modes that are always active in the background and will not be removed
            by applications of mode selection definitions via method `apply`
        """
        self._background_base_modes = background_base_modes or []
        self._configured_base_modes: Sequence[str] | None = None
        self._configured_default_modes: Sequence[str] | None = None
        self._added_modes: set[str] = set()
        self._dynamically_activated_mode_names: set[str] = set()
        """
        the subset of active mode names that are dynamically activated (not necessarily enabled after project change)
        """
        self._active_mode_names: Sequence[str] = []
        """
        the full list of active mode names (not including background base modes)
        """

    def apply(self, mode_selection: ModeSelectionDefinition) -> None:
        log.debug("Applying mode selection definition %s", mode_selection)

        # apply overrides
        if isinstance(mode_selection, ModeSelectionDefinitionWithBaseModes):
            if mode_selection.base_modes is not None:
                self._configured_base_modes = mode_selection.base_modes
        if mode_selection.default_modes is not None:
            self._configured_default_modes = mode_selection.default_modes
        log.debug("Current mode selection: base_modes=%s, default_modes=%s", self._configured_base_modes, self._configured_default_modes)

        # apply added modes (if any)
        if isinstance(mode_selection, ModeSelectionDefinitionWithAddedModes):
            if mode_selection.added_modes:
                log.debug("Adding modes: %s", mode_selection.added_modes)
                self._added_modes.update(mode_selection.added_modes)
                log.debug("Current added modes: %s", self._added_modes)

        self._dynamically_activated_mode_names = set(self._configured_default_modes or []) | self._added_modes
        self._active_mode_names = sorted(set(self._configured_base_modes or []) | self._dynamically_activated_mode_names)

    def get_mode_names(self) -> Sequence[str]:
        """
        :return: the ordered list of active mode names (not including the non-user-facing background base modes)
        """
        return self._active_mode_names

    @classmethod
    def get_mode_instance(cls, mode_name: str) -> SerenaAgentMode:
        if mode_name not in cls._mode_instances:
            cls._mode_instances[mode_name] = SerenaAgentMode.load(mode_name)
        return cls._mode_instances[mode_name]

    def get_modes(self, include_background_base_modes: bool = False) -> Sequence[SerenaAgentMode]:
        result: list[SerenaAgentMode] = []
        if include_background_base_modes:
            result.extend(self._background_base_modes)
        result.extend([self.get_mode_instance(mode_name) for mode_name in self._active_mode_names])
        return result

    def get_dynamically_activated_modes(self) -> Sequence[SerenaAgentMode]:
        return [self.get_mode_instance(mode_name) for mode_name in self._dynamically_activated_mode_names]

    def get_base_modes(self, include_background_base_modes: bool = False) -> Sequence[SerenaAgentMode]:
        result: list[SerenaAgentMode] = []
        if include_background_base_modes:
            result.extend(self._background_base_modes)
        result.extend([self.get_mode_instance(mode_name) for mode_name in self._configured_base_modes or []])
        return result


class ProjectPromptProvisionStatus:
    """
    Manages the status of the provision of project-specific prompts
    """

    @dataclass
    class SessionStatus:
        mode_prompts_provided: bool = False
        project_activation_message_provided: bool = False

    def __init__(self, newly_activated_mode_names: set[str] | None = None):
        """
        :param newly_activated_mode_names: list of mode names that have been newly activated (by dynamic project activation)
            and for which prompts must still be provided (either in the system prompt or via the activation message)
        """
        if newly_activated_mode_names is None:
            newly_activated_mode_names = set()
        self._newly_activated_mode_names: set[str] = newly_activated_mode_names
        self._session_status_dict: dict[str, ProjectPromptProvisionStatus.SessionStatus] = defaultdict(lambda: self.SessionStatus())

    def _get_session_status(self, session_id: str) -> SessionStatus:
        return self._session_status_dict[session_id]

    def is_mode_prompt_already_provided(self, mode_name: str, session_id: str) -> bool:
        """
        :param mode_name: the mode name
        :param session_id: the client session ID
        :return: whether the mode name was already provided (in a project-specific activation message) and therefore
            should not be included again (in the Serena instructions manual)
        """
        if not self._get_session_status(session_id).mode_prompts_provided:
            return False
        return mode_name in self._newly_activated_mode_names

    def get_modes_with_prompts_to_be_provided_for_project_activation(self, session_id: str) -> list[SerenaAgentMode]:
        """
        Gets the modes that have been newly activated and for which prompts still need to be provided
        (in dynamic project activation message).

        :param: session_id: the client session ID
        :return: the modes
        """
        result = []

        # Note: We always want to provide the prompts of newly activated modes in the activation message
        #   because some clients (e.g. Claude Desktop) use a single session for all chats.
        #   Therefore, we view project activation as an "entry action", which must always provide
        #   all the information that is relevant to the project
        # Because of this, we cannot use a condition like this:
        #   new_mode_prompts_must_be_provided_for_activation = not self._get_session_status(session_id).mode_prompts_provided
        new_mode_prompts_must_be_provided_for_activation = True
        mark_used(session_id)

        if new_mode_prompts_must_be_provided_for_activation:
            for mode_name in self._newly_activated_mode_names:
                mode = ActiveModes.get_mode_instance(mode_name)
                if mode.has_prompt():
                    result.append(mode)
        return result

    def mark_mode_prompts_as_provided(self, session_id: str) -> None:
        """
        Marks the prompts for all newly activated modes as provided, so that they will not be included in the project activation message.

        :param session_id: the client session ID
        """
        self._get_session_status(session_id).mode_prompts_provided = True

    def mark_project_activation_message_as_provided(self, session_id: str) -> None:
        """
        Marks the project activation message as provided, so that it will not be included again in case of multiple activations of the same project.

        :param session_id: the client session ID
        """
        self._get_session_status(session_id).project_activation_message_provided = True

    def is_project_activation_message_already_provided(self, session_id: str) -> bool:
        """
        :param session_id: the client session ID
        :return: whether the project activation message was already provided and therefore should not be included again
        """
        return self._get_session_status(session_id).project_activation_message_provided


class DashboardManager:
    class Mode(Enum):
        BROWSER = "browser"
        """
        Open the dashboard in the default browser; supported on all platforms.
        """
        WEBVIEW = "app"
        """
        Open the dashboard via a native window (using pywebview) which minimises to the tray;
        supported on Windows and macOS (but on macOS, tray apps for multiple instances accumulate 
        in the top bar, which users may not want)
        """
        TRAY_MANAGER = "tray_manager"
        """
        Register dashboard instance with a central manager tray app (single tray icon for all instances), 
        spawning the tray manager if not already running; supported on macOS and Windows.
        """

        @classmethod
        def from_platform(cls) -> "DashboardManager.Mode":
            match platform.system():
                case "Windows":
                    return cls.WEBVIEW
                case "Darwin":
                    return cls.TRAY_MANAGER
                case _:
                    return cls.BROWSER

        def is_supported(self) -> bool:
            """
            :return: whether the mode is supported on the current platform
            """
            if self == DashboardManager.Mode.WEBVIEW:
                return SerenaDashboardViewer.is_current_platform_supported()
            elif self == DashboardManager.Mode.TRAY_MANAGER:
                return SerenaDashboardTrayManager.is_current_platform_supported()
            else:
                return True

    def __init__(
        self,
        port: int,
        host_listen_address: str,
        open_dashboard_on_launch: bool,
        active_project: Project | None = None,
        mode_str: str | None = None,
    ):
        # determine requested mode
        if mode_str is not None:
            try:
                mode = self.Mode(mode_str)
            except ValueError:
                mode = self.Mode.from_platform()
                log.warning(f"Invalid dashboard interface mode '{mode_str}' specified; falling back to platform default '{mode.value}'.")
        else:
            mode = self.Mode.from_platform()

        # check for mode compatibility
        if not mode.is_supported():
            fallback_mode = self.Mode.from_platform()
            log.warning(
                f"Dashboard interface mode '{mode.value}' is not supported on the current platform; "
                f"falling back to '{fallback_mode.value}'."
            )
            mode = fallback_mode

        self._port = port
        self._mode = mode
        self._dashboard_viewer_process: supervise.Popen | None = None
        self._tray_manager_lock = threading.Lock()

        dashboard_host = host_listen_address
        if dashboard_host == "0.0.0.0":
            dashboard_host = "localhost"
        self.url = f"http://{dashboard_host}:{port}/dashboard/index.html"

        # handle startup
        match self._mode:
            case self.Mode.WEBVIEW:
                self._start_dashboard_viewer(minimized=not open_dashboard_on_launch)
            case self.Mode.TRAY_MANAGER:
                init_fn = lambda: self._tray_manager_register(open_on_launch=open_dashboard_on_launch, active_project=active_project)
                threading.Thread(target=init_fn, name="init-DashboardTrayManager", daemon=True).start()
            case self.Mode.BROWSER:
                if open_dashboard_on_launch:
                    if not system_has_usable_display():
                        log.info("Not opening the Serena dashboard because no usable display was detected.")
                    else:
                        self.open_dashboard_in_browser()

    def open_dashboard_in_browser(self) -> None:
        open_url_in_browser(self.url, use_subprocess=True)

    @staticmethod
    def _start_dashboard_viewer_process_function(url: str, minimized: bool) -> None:
        """
        Main function of the subprocess for starting the dashboard viewer
        """
        try:
            SerenaDashboardViewer(url, start_minimized=minimized).run()
        except webview.errors.WebViewException as e:
            log.warning(f"Could not open Serena Dashboard viewer. Cause:\n{e}")
            # Fall back to opening the browser window if the window was supposed to be shown directly
            if not minimized:
                open_url_in_browser(url, use_subprocess=True)

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

        if SerenaDashboardViewer.is_current_platform_supported():
            # the viewer of this dashboard an ended instance started, or else a new one; it watches no pid of serena's,
            # since it outlives serena as a language server does (bug://serena/language_server/initialize/process_id/client/watch)
            program = "import sys; from serena.agent import DashboardManager; DashboardManager._start_dashboard_viewer_process_function(sys.argv[1], sys.argv[2] == 'True')"
            self._dashboard_viewer_process = self._adopt_dashboard_viewer(self.url) or supervise.Popen(
                [sys.executable, "-c", program, self.url, str(minimized)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                call_id="",
                waiting=supervise.Waiting.of(_DASHBOARD_VIEWER_KIND, {"url": self.url}),
                keep=True,
            )
        else:
            log.info("Not starting Serena dashboard viewer because the current platform does not support it; using browser-based fallback")
            if not minimized:
                self.open_dashboard_in_browser()

    @staticmethod
    def _adopt_dashboard_viewer(url: str) -> supervise.Popen | None:
        """
        :param url: the dashboard's URL.
        :return: a live viewer of that URL an ended instance started, now this instance's, so that no second viewer is
            started (pg://brain/daemon/restart/readoption), or None when there is none.
        """
        for record in supervise.records():
            waiting = record.waiting
            if waiting.kind != _DASHBOARD_VIEWER_KIND or waiting.state != {"url": url} or waiting.started_by_a_live_instance():
                continue
            if not record.outcome().ended:
                return supervise.Popen.adopt(record.path)
        return None

    @property
    def viewer_process(self) -> supervise.Popen | None:
        """
        :return: the viewer process this manager started or took over, if any.
        """
        return self._dashboard_viewer_process

    def _tray_manager_register(self, open_on_launch: bool, active_project: Project | None) -> None:
        """
        Ensure the tray manager is running and register this dashboard instance with it.

        If the current platform supports the native tray manager, this method starts
        the manager (if not already running) and registers the instance. Otherwise,
        it falls back to opening the dashboard in the default web browser.

        :param open_on_launch: whether the dashboard should be opened immediately
        """
        with LogTime("Dashboard tray manager initialisation"):
            with self._tray_manager_lock:
                # ensure the singleton tray manager process is running
                SerenaDashboardTrayManager.ensure_running()

                # determine the current project name (if any)
                project_name = active_project.project_name if active_project is not None else None

                # register this instance with the tray manager
                SerenaDashboardTrayManager.register_instance(
                    port=self._port,
                    dashboard_url=self.url,
                    project=project_name,
                    started_at=datetime.now().isoformat(timespec="seconds"),
                    open_viewer=open_on_launch,
                )

    def shutdown(self) -> None:
        """
        Frees resources, terminating the dashboard viewer process (if any) and unregistering from the tray manager (if applicable).
        """
        if self._dashboard_viewer_process is not None:
            log.info("Stopping the dashboard viewer process ...")
            self._dashboard_viewer_process.terminate()
            self._dashboard_viewer_process = None

        if self._mode == self.Mode.TRAY_MANAGER:
            with self._tray_manager_lock:
                SerenaDashboardTrayManager.unregister_instance(port=self._port)

    def update_active_project(self, active_project: Project | None) -> None:
        """
        Updates the active project (where applicable).

        :param active_project: the currently active project or None if no project is active
        """
        if self._mode == self.Mode.TRAY_MANAGER:
            with self._tray_manager_lock:
                project_name = active_project.project_name if active_project is not None else None
                SerenaDashboardTrayManager.update_project(port=self._port, project=project_name)


class SerenaAgent:
    def __init__(
        self,
        project: str | None = None,
        *,
        project_activation_callback: Callable[[], None] | None = None,
        project_activation_error: str | None = None,
        serena_config: SerenaConfig | None = None,
        context: SerenaAgentContext | None = None,
        modes: ModeSelectionDefinition | None = None,
        memory_log_handler: MemoryLogHandler | None = None,
    ):
        """
            an already registered project;
        :param project_activation_callback: a callback function to be called when a project is activated.
        :param project_activation_error: an initial error to report back to the client/LLM pertaining to project determination/activation
            in Serena's initial prompts. This is only applicable if `project` is None.
        :param serena_config: the Serena configuration or None to read the configuration from the default location.
        :param context: the context in which the agent is operating, None for default context.
            The context may adjust prompts, tool availability, and tool descriptions.
        :param modes: mode selection definition to apply for this session
        :param memory_log_handler: a MemoryLogHandler instance from which to read log messages; if None, a new one will be created
            if necessary.
        """
        self._project_activation_callback = project_activation_callback
        self._project_activation_error: str | None = project_activation_error
        # per-MCP-session active project map, populated when Tool.apply_ex routes a call from a session,
        # so reads via the _active_project property return the project for the calling session rather than
        # whichever session most recently activated. Keys are either id(mcp_ctx.session) (int, for direct
        # stdio / streamable-http / sse transports) or the absolute project_root path the pipe-client
        # asserts at handshake (for clients connecting via the per-client stdio pipe; see
        # _PIPE_SESSION_ID_VAR and the project-root-as-session-id contract in
        # plan://Serena:serena/serena-pipe-session-id-is-the-session-id).
        self._active_projects_by_session: dict[str | int, Project] = {}
        # the absolute project_root each session EXPLICITLY activated (via activate_project, startup,
        # or an operator action), kept distinct from the multiplexer-forwarded cwd. On a self-heal
        # (Tool.apply_ex, empty per-session slot) this is the root restored -- NEVER the forwarded
        # origin -- so a session that ever explicitly activated a project is never silently rebound to
        # a different one. Keyed identically to _active_projects_by_session and evicted alongside it.
        self._explicit_project_roots_by_session: dict[str | int, str] = {}
        # the active modes and tools of each session, derived from the Serena configuration and that session's own
        # project (see _update_active_modes / _update_active_tools); keyed like _active_projects_by_session and
        # evicted alongside it, so no session's project enables or disables a tool for another session
        self._active_modes_by_session: dict[str | int, ActiveModes] = {}
        self._active_tools_by_session: dict[str | int, AvailableTools] = {}
        # the mode overrides each session set through set_modes, keyed and evicted like the maps above; a session that set
        # none has the modes the agent was started with
        self._mode_overrides_by_session: dict[str | int, ModeSelectionDefinition] = {}
        # the on-disk backing for the map above. The map alone survives an evicted per-session slot but
        # not a daemon restart, and losing it makes a session that explicitly activated a worktree
        # indistinguishable from one that never activated anything -- the self-heal then has nothing to
        # restore and the session is stranded. Written on every session-bound explicit activation and
        # consulted by the self-heal when the in-memory record misses, so the binding outlives the
        # process. Swap this attribute to redirect the store (tests).
        self._explicit_project_root_store = ExplicitProjectRootStore(SerenaPaths().explicit_project_roots_file)
        # fallback active project for non-MCP callers (CLI, dashboard, scripts, tests) that have no session
        # in scope; the property setter writes here when _SESSION_KEY_VAR is unset.
        self._legacy_active_project: Project | None = None
        # guards every write of a project into a session's slot together with the release of a project no session
        # holds (see _release_project_if_unheld), so a session taking a project cannot interleave with its release
        self._project_holders_lock = threading.Lock()
        # the hold of each tool call in flight, one entry per call (see _pin_session_project_for_call); a call holds the
        # project its hold is on like a session's slot does, so no release stops that project's servers under it
        self._projects_held_by_calls: list[_CallHold] = []
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
        self._session_finalizers: dict[str | int, weakref.finalize] = {}
        # one-time warning tracker for sessions that arrive without an
        # X-Forwarded-Mcp-Session-Id header (legacy direct-stdio path).
        self._warned_missing_forwarded_session_keys: set[str | int] = set()
        self._warned_missing_forwarded_session_keys: set[str | int] = set()
        self._session_finalizers_lock = threading.Lock()
        # the project can surface the real cause instead of a generic "No active project".
        self._startup_activation_error: Exception | None = None
        self._startup_activation_target: str | None = None
        self._gui_log_viewer: Optional["GuiLogViewer"] = None
        self._dashboard_manager: DashboardManager | None = None
        self._project_prompt_status = ProjectPromptProvisionStatus()
        self._session_registry = SessionRegistry()
        self.version = serena_version()
        self._config_changed_callbacks: list[Callable[[], None]] = []
        self._repl: SerenaRepl | None = None
        self._prompt_params: SerenaAgent.PromptParams | None = None

        # obtain serena configuration using the decoupled factory function
        self.serena_config = serena_config or SerenaConfig.from_config_file()

        # propagate configuration to other components
        self.serena_config.propagate_settings()

        # determine registered project to be activated (if any)
        registered_project_to_activate: RegisteredProject | None = (
            self.serena_config.get_registered_project(project, autoregister=True) if project is not None else None
        )

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
        # With the tool interface, the backend cannot change within a session (the toolset depends on it and is fixed);
        # with the REPL interface, it may change upon project activation (see _activate_project).
        self._language_backend = self.serena_config.determine_language_backend(
            project_config=registered_project_to_activate.project_config if registered_project_to_activate is not None else None,
            log_choice=True,
        )

        # determine the effective agent interface for this session (project configuration > global configuration).
        # Like the language backend, it is fixed for the session, since the set of exposed tools cannot change after startup.
        self._agent_interface = self.serena_config.determine_agent_interface(
            project_config=registered_project_to_activate.project_config if registered_project_to_activate is not None else None,
            log_choice=True,
        )

        # daemon-wide executor — reserved for non-MCP callers (CLI, dashboard, scripts, tests)
        # with no session in scope. MCP code paths route through the per-session executors below
        # so concurrent sessions do not serialize through one queue.
        self._task_executor = TaskExecutor("SerenaAgentTaskExecutor", self._task_completion_callback)
        # per-session task executors, keyed identically to _active_projects_by_session and
        # _cursor_managers_by_session. Each session gets its own queue and worker thread so
        # cross-session tool calls run in parallel; within a session, tasks remain serial
        # (the session's executor has a single worker thread). Torn down on session eviction.
        self._task_executors_by_session: dict[str | int, TaskExecutor] = {}
        self._task_executors_by_session_lock = threading.Lock()

        # Initialize the prompt factory
        self.prompt_factory = SerenaPromptFactory()

        # initialise active modes (baseline modes prior to project activation, allowing newly activated modes to be tracked)
        # the modes the agent was started with, which apply to every session that set none, and the mode overrides of the
        # single slot outside any session
        self._configured_mode_overrides = modes
        self._mode_overrides = modes
        self._active_modes: ActiveModes
        self._update_active_modes(log_message=False)

        # activate the given project (if any), also updating the active modes
        # Note: We cannot update the active tools yet, because the base toolset has not been computed yet
        #       (and its computation depends on the active project)
        self._active_modes: ActiveModes
        if project is not None:
            try:
                self.activate_project_from_path_or_name(project, update_active_modes=False, update_active_tools=False)
            except Exception as e:
                log.error(f"Error activating project '{project}' at startup: {e}", exc_info=e)
                self._project_activation_error = str(e)
                # preserve the failure so get_active_project_or_raise can surface it to the first tool call
                self._startup_activation_error = e
                self._startup_activation_target = project
        self._update_active_modes()

        # determine whether we are operating in a single-project session, i.e. the project that was activated at startup
        # (if any) is the only project that will be worked with throughout the session (no project switching)
        self._is_single_project = self._context.single_project and self._active_project is not None

        # determine the base toolset defining the set of exposed tools (which e.g. the MCP shall see),
        self._base_toolset = self._create_base_toolset(
            self.serena_config, self._context, self._active_modes, self._active_project, self._agent_interface, self._is_single_project
        )
        self._exposed_tools = self._base_toolset.to_available_tools(self._all_tools)
        log.info(f"Number of exposed tools: {len(self._exposed_tools)}. Exposed tools: {self._exposed_tools.tool_names}")

        # update the active tools (considering the active project, if any)
        self._active_tools: AvailableTools
        self._update_active_tools()

        # the active modes and tools of a session that has activated no project: those of the Serena configuration alone
        self._config_active_modes = self._derive_active_modes(None, self._configured_mode_overrides)
        self._config_active_tools = self._derive_active_tools(self._config_active_modes, None)

        # create the dashboard backend (if enabled), which will register callback.
        dashboard_api: SerenaDashboardAPI | None = None
        if self.serena_config.web_dashboard:
            dashboard_api = SerenaDashboardAPI(
                get_memory_log_handler(),
                tool_names,
                agent=self,
                tool_usage_stats=self._tool_usage_stats,
                host=self.serena_config.web_dashboard_listen_address,
                trusted_hosts=self.serena_config.web_dashboard_trusted_hosts,
            )

        # propagate the initial config/state to listeners, particularly to the dashboard API.
        # Note: The only ongoing task can be the LS initialisation, which does not change the config
        # This is executed before starting the dashboard thread to ensure that the dashboard
        # has the correct initial state before requests can come in.
        self._on_config_changed()

        # start the dashboard backend thread and the dashboard frontend manager (if enabled).
        # This should be the last thing to happen in the initialization since the dashboard
        # may access various parts of the agent
        if dashboard_api:
            dashboard_thread, port = dashboard_api.run_in_thread()
            self._dashboard_manager = DashboardManager(
                port,
                self.serena_config.web_dashboard_listen_address,
                self.serena_config.is_dashboard_opened_on_launch(),
                self._active_project,
                mode_str=self.serena_config.web_dashboard_interface,
            )
            log.info("Serena web dashboard started at %s", self._dashboard_manager.url)
            # inform the GUI window (if any)
            if self._gui_log_viewer is not None:
                self._gui_log_viewer.set_dashboard_url(self._dashboard_manager.url)

        self._send_usage_info()

    def _send_usage_info(self) -> None:
        if os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("SERENA_USAGE_REPORTING") == "false":
            return

        def send_usage_info():
            params: dict[str, str | int] = {
                "os": platform.system(),
                "dashboard": int(self.serena_config.web_dashboard),
                "version": self.version,
                "backend": self._language_backend.get_key(),
                "context": self._context.name,
                "agent_interface": self._agent_interface.value,
            }
            try:
                requests.get("https://oraios-software.de/serena_usage.php", params=params, timeout=5)
            except Exception as e:
                log.warning(f"Failed to send usage info: {e}")

        # start thread to send usage info asynchronously, so that it does not block the agent startup
        threading.Thread(target=send_usage_info, daemon=True).start()

    @staticmethod
    def _is_dashboard_openable(serena_config: SerenaConfig) -> bool:
        """
        :param serena_config: the configuration
        :return: whether the web dashboard is available and opening it is a meaningful operation
            (i.e. it is enabled and not opened automatically)
        """
        return serena_config.web_dashboard and not serena_config.is_dashboard_opened_on_launch() and not serena_config.gui_log_window

    @classmethod
    def _create_base_toolset(
        cls,
        serena_config: SerenaConfig,
        context: SerenaAgentContext,
        modes: ActiveModes,
        project: Project | None,
        agent_interface: AgentInterface,
        is_single_project: bool,
    ) -> ToolSet:
        """
        Determines the base toolset defining the set of exposed tools (which e.g. the MCP shall see).
        In REPL mode, the toolset is fixed. Otherwise, it depends on ...
           * dashboard availability/opening on launch
           * Serena config
           * the context (which is fixed for the session)
           * the base modes (including background base modes like JetBrains mode)
           * the optional tools enabled by initial dynamic modes
           * single-project mode reductions (if applicable)
        """
        # when in REPL mode, the toolset is fixed and does not depend on the configuration, context, modes or project
        if agent_interface.is_repl():
            tool_classes: list[type[Tool]] = [SerenaReplTool, InitialInstructionsTool]
            if not is_single_project:
                tool_classes.append(ActivateProjectTool)
            return ToolSet({tool_class.get_name_from_cls() for tool_class in tool_classes})

        # determine whether to include the OpenDashboardTool based on the Serena configuration
        tool_inclusion_definitions: list[ToolInclusionDefinition] = []
        if cls._is_dashboard_openable(serena_config):
            tool_inclusion_definitions.append(
                NamedToolInclusionDefinition(name="OpenDashboard", included_optional_tools=[OpenDashboardTool.get_name_from_cls()])
            )

        # expose GetLanguageServerStatusTool and RestartLanguageServerTool by default so agents
        # can query per-language LSP state and recover a hung language server without reactivating
        # the project; contexts that want to hide either can still exclude it by name
        tool_inclusion_definitions.append(
            NamedToolInclusionDefinition(
                name="OptionalLspTools",
                included_optional_tools=[
                    GetLanguageServerStatusTool.get_name_from_cls(),
                    RestartLanguageServerTool.get_name_from_cls(),
                ],
            )
        )

        # consider Serena configuration and the active context
        tool_inclusion_definitions.append(serena_config)
        tool_inclusion_definitions.append(context)

        # consider modes
        # * base modes: These cannot be changed, so they are fully applied
        for base_mode in modes.get_base_modes(include_background_base_modes=True):
            tool_inclusion_definitions.append(base_mode)
        # * dynamically activated modes:
        #    - When not in a single-project context, these modes can later be turned off,
        #      so we consider only their inclusions (but not their exclusions, because these must not be hard).
        #    - In a single-project context, we can consider them fully.
        for mode in modes.get_dynamically_activated_modes():
            if is_single_project:
                tool_inclusion_definitions.append(mode)
            else:
                # Since modes can be dynamically turned on and off, we don't include their definitions directly,
                # For the initially active dynamic modes, we make sure that the tools they enable are included.
                tool_inclusion_definitions.append(
                    NamedToolInclusionDefinition(
                        name=f"InitialDynamicModeInclusions[{mode.name}]", included_optional_tools=mode.included_optional_tools
                    )
                )

        # When in a single-project context, the agent is assumed to work on a single project, and we thus
        # want to apply that project's tool exclusions/inclusions from the get-go, limiting the set
        # of tools that will be exposed to the client.
        # Furthermore, we disable tools that are only relevant for project activation.
        # So if the project exists, we apply all the aforementioned exclusions.
        apply_read_only = False
        if is_single_project:
            assert project is not None
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
            apply_read_only = project.project_config.read_only

        # compute the resulting tool set
        base_toolset = ToolSet.default().apply(*tool_inclusion_definitions)
        if apply_read_only:
            base_toolset = base_toolset.without_editing_tools()
        log.info(f"Number of exposed tools: {len(base_toolset)}")
        return base_toolset

    def get_language_backend(self) -> LanguageBackend:
        return self._language_backend

    def is_single_project(self) -> bool:
        """
        :return: whether this is a single-project session, i.e. the project activated at startup is the only project
            that will be worked with throughout the session (no project switching); requires a single-project context
            and a project at startup
        """
        return self._is_single_project

    def get_agent_interface(self) -> AgentInterface:
        return self._agent_interface

    def get_current_tasks(self) -> list[TaskExecutor.TaskInfo]:
        """
        Gets the list of tasks currently running or queued for execution on the daemon-wide executor and on the
        executor of every session.
        The function returns a list of thread-safe TaskInfo objects (specifically created for the caller).

        :return: the tasks of the daemon-wide executor followed by those of each session's executor, each executor's
            tasks in its execution order (running task first)
        """
        tasks = self._task_executor.get_current_tasks()
        for executor in self._get_session_task_executors():
            tasks.extend(executor.get_current_tasks())
        return tasks

    def get_last_executed_task(self) -> TaskExecutor.TaskInfo | None:
        """
        Gets the logged task that finished most recently on the daemon-wide executor or on the executor of any session.

        :return: the last executed task info or None if no task has been executed yet
        """
        # collect the last executed task of every executor
        last_executed_tasks = []
        for executor in [self._task_executor, *self._get_session_task_executors()]:
            last_executed_task = executor.get_last_executed_task()
            if last_executed_task is not None:
                last_executed_tasks.append(last_executed_task)

        # select the one recorded last across all executors
        return max(
            last_executed_tasks,
            key=lambda task_info: -1 if task_info.completion_index is None else task_info.completion_index,
            default=None,
        )

    def _get_session_task_executors(self) -> list[TaskExecutor]:
        """
        :return: a snapshot of the executors of the sessions, in the order in which they were created
        """
        with self._task_executors_by_session_lock:
            return list(self._task_executors_by_session.values())

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
        the legacy slot applies as before. The manager is rebuilt when absent or when it is bound to
        another instance than the one the call runs on — a per-session project switch invalidates only
        that session's manager. A rebuilt manager is stored only when the instance the call runs on is
        the one the caller's slot holds: a call pinned to an instance a project.yml reload replaced gets a
        manager it alone uses, which therefore holds that instance no longer than the call does
        (pg://serena/session/project/instance/shutdown).

        :return: a manager whose ``project`` is the instance the call runs on.
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

        # rebuild when absent, or when bound to another instance (the caller switched projects, or the call runs on an
        # instance a project.yml reload replaced)
        if existing is None or existing.project is not project:
            project.get_language_server_manager_or_raise()  # validate LSP is available
            new_mgr = CursorManager(project)
            with self._project_holders_lock:
                if session_key is not None:
                    if self._active_projects_by_session.get(session_key) is project:
                        self._cursor_managers_by_session[session_key] = new_mgr
                elif self._legacy_active_project is project:
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
        if self._dashboard_manager is None:
            return None
        return self._dashboard_manager.url

    def adopt_processes(self, calls: supervise.Calls) -> list[threading.Thread]:
        """
        Takes up every process an ended instance of serena started whose outcome no instance has consumed, before serena
        serves its first call (pg://brain/daemon/restart/readoption). Each live language server and bridge is held for the
        first use of its command, whatever project it serves, and every other record is answered for by its continuation
        or waited on and discarded (decision://brain/daemon_restart_survival/journal/continuation).

        :param calls: the journal of the calls serena serves.
        :return: the threads waiting on the processes of those other records that still run.
        """
        # the kept processes, each taken over through its state
        LanguageServerStates.hold_everywhere()
        BridgeStates.hold_everywhere()

        # every other record, but that of the viewer this instance took over already
        kept_records = LanguageServerStates.named_records() | BridgeStates.named_records()
        viewer = self._dashboard_manager.viewer_process if self._dashboard_manager is not None else None
        held = [viewer.record] if viewer is not None else []
        return supervise.Adoption(calls, (LANGUAGE_SERVER_KIND, BRIDGE_KIND), kept_records).adopt(skip=held)

    def open_dashboard(self) -> bool:
        """
        Opens the Serena dashboard (for on-demand usage as triggered by the user, e.g. via a tool)

        :return: True if the dashboard was opened, False if it could not be opened
        """
        if self._dashboard_manager is None:
            raise Exception("Dashboard is not running.")

        if not system_has_usable_display():
            log.warning("Not opening the Serena web dashboard because no usable display was detected.")
            return False

        self._dashboard_manager.open_dashboard_in_browser()
        return True

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
        so any reads within the current call observe the new project immediately. Within a tool call, the call's hold
        moves onto the new project, and the project it leaves is released if nothing else holds it.
        """
        session_key = _SESSION_KEY_VAR.get(None)
        call_hold = _CALL_HOLD_VAR.get()
        left_project: Project | None = None
        with self._project_holders_lock:
            if session_key is not None:
                if project is None:
                    self._active_projects_by_session.pop(session_key, None)
                else:
                    self._active_projects_by_session[session_key] = project
            else:
                self._legacy_active_project = project
            # the calling tool call holds the project it binds, not the one it held before, so it cannot keep the latter
            # from being released (pg://serena/session/project/instance/shutdown)
            if call_hold is not None and call_hold.project is not project:
                left_project = call_hold.project
                call_hold.project = project
        self._release_project_if_unheld(left_project)
        # update the in-flight var only if it has been set on this call's context; otherwise leave the
        # default unset so callers without contextvar scoping (CLI/dashboard) keep falling through to
        # the per-session / legacy lookup in the getter.
        if _ACTIVE_PROJECT_VAR.get(_UNSET) is not _UNSET or session_key is not None:
            _ACTIVE_PROJECT_VAR.set(project)

    def _register_session_finalizer(self, mcp_session: object, session_key: str | int) -> None:
        """Lazily register a GC finalizer that drops this session's per-session entries.

        Called from ``Tool.apply_ex`` on every tool dispatch; the guard dict makes registration
        exactly-once per session. When the MCP session object is garbage-collected (its
        ``streamable-http`` task ends and the transport drops its references), the finalizer fires
        and pops ``session_key`` from :attr:`_active_projects_by_session` and
        :attr:`_cursor_managers_by_session`.

        Without this, per-session entries accumulate for every session that ever connected, and
        ``id()`` reuse lets a future session land on a dead session's key and inherit its state.

        :param mcp_session: the object whose lifetime gates eviction: the client's connection of ``mcp_ctx.session``, or
            ``mcp_ctx.session`` itself where it has none.
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

    def _is_project_held(self, project: Project) -> bool:
        """
        Must be called with :attr:`_project_holders_lock` held.

        :param project: a project
        :return: whether any session's slot or cursor manager, the legacy slot or cursor manager, or a tool call in
            flight holds this project instance
        """
        holders: list[Project | None] = [*self._active_projects_by_session.values(), self._legacy_active_project]
        holders.extend(cursor_manager.project for cursor_manager in list(self._cursor_managers_by_session.values()))
        if self._legacy_cursor_manager is not None:
            holders.append(self._legacy_cursor_manager.project)
        holders.extend(call_hold.project for call_hold in self._projects_held_by_calls)
        return any(holder is project for holder in holders)

    def _pin_session_project_for_call(self, session_key: str | int) -> _CallHold:
        """
        Creates the hold of a tool call of a session as the call starts, on the project the session holds, and counts the
        call as a holder of that project until :meth:`_unpin_project_for_call`, so that no release stops the project's
        language servers while the call runs on it (pg://serena/session/project/instance/shutdown). While the call runs,
        its hold follows the session's slot: a rebinding of the slot by the call moves the hold (see the
        :attr:`_active_project` setter).

        :param session_key: the key of the calling session
        :return: the call's hold, on the project the session holds, or on none if it holds none
        """
        with self._project_holders_lock:
            call_hold = _CallHold(self._active_projects_by_session.get(session_key))
            self._projects_held_by_calls.append(call_hold)
        return call_hold

    def _unpin_project_for_call(self, call_hold: _CallHold) -> None:
        """
        Ends the hold :meth:`_pin_session_project_for_call` created for a tool call, and stops the language servers of the
        project the hold is on at the call's end if this was its last holder.

        :param call_hold: the call's hold
        """
        with self._project_holders_lock:
            for index, held in enumerate(self._projects_held_by_calls):
                if held is call_hold:
                    del self._projects_held_by_calls[index]
                    break
            project = call_hold.project
            release = project is not None and not self._is_project_held(project)
        if release:
            self._release_project_if_unheld(project)

    def _move_holders_onto_replacement(self, replaced_project: Project, replacement: Project) -> None:
        """
        Moves every holder of ``replaced_project`` other than the calling context onto ``replacement``, the instance built
        in its place from a changed project configuration, and stops the language servers of ``replaced_project`` if
        nothing holds it any longer.

        The calling context is left to :meth:`_activate_project`, which binds it to ``replacement`` and starts the latter's
        language server manager. Each session moved gets the active modes and tools derived from ``replacement``, every
        cursor manager built over ``replaced_project`` is dropped, and a tool call in flight keeps holding
        ``replaced_project`` until it ends.

        :param replaced_project: the instance the changed configuration replaced
        :param replacement: the instance built from the changed configuration
        """
        calling_session_key = _SESSION_KEY_VAR.get(None)
        with self._project_holders_lock:
            # the sessions holding the replaced instance, with the modes and tools of the replacement
            for session_key, held_project in list(self._active_projects_by_session.items()):
                if held_project is replaced_project and session_key != calling_session_key:
                    self._active_projects_by_session[session_key] = replacement
                    active_modes = self._derive_active_modes(replacement, self._mode_overrides_of(session_key))
                    self._active_modes_by_session[session_key] = active_modes
                    self._active_tools_by_session[session_key] = self._derive_active_tools(active_modes, replacement)

            # the legacy slot, unless it is the calling context's own
            if calling_session_key is not None and self._legacy_active_project is replaced_project:
                self._legacy_active_project = replacement
                self._active_modes = self._derive_active_modes(replacement, self._mode_overrides)
                self._active_tools = self._derive_active_tools(self._active_modes, replacement)

            # the cursor managers built over the replaced instance
            for session_key, cursor_manager in list(self._cursor_managers_by_session.items()):
                if cursor_manager.project is replaced_project:
                    del self._cursor_managers_by_session[session_key]
            if self._legacy_cursor_manager is not None and self._legacy_cursor_manager.project is replaced_project:
                self._legacy_cursor_manager = None

        self._release_project_if_unheld(replaced_project)

    def _release_project_if_unheld(self, project: Project | None, timeout: float = 2.0) -> None:
        """
        Stops the language servers of ``project`` if nothing holds it, and leaves them running if anything does.

        Runs under the project's language-server-manager lock, so it cannot interleave with an initialisation of that
        project's manager, and decides and detaches under :attr:`_project_holders_lock`, so a session that takes the
        project afterwards finds it without a manager and initialises a new one. Once the servers have stopped, an
        instance replacing ``project`` may start its own (see :meth:`Project.mark_language_servers_stopped`).

        :param project: the project a holder stopped holding, or None
        :param timeout: the timeout for the shutdown of each language server
        """
        if project is None:
            return
        with project.language_server_manager_lock():
            with self._project_holders_lock:
                if self._is_project_held(project):
                    return
                manager = project.detach_language_server_manager()
            if manager is not None:
                log.info(f"No session holds project '{project.project_name}' any longer; stopping its language servers ...")
                try:
                    manager.stop_all(save_cache=True, timeout=timeout)
                except Exception as e:
                    log.warning(f"Error stopping the language servers of project '{project.project_name}': {e}", exc_info=e)
            project.mark_language_servers_stopped()

    def _release_project_if_unheld_in_background(self, project: Project | None) -> None:
        """
        Runs :meth:`_release_project_if_unheld` on a thread of its own, for callers such as a garbage-collection
        finalizer that must not wait for language servers to stop.

        :param project: the project a session stopped holding, or None
        """
        if project is None:
            return
        threading.Thread(
            target=self._release_project_if_unheld, args=(project,), name=f"ReleaseProject[{project.project_name}]", daemon=True
        ).start()

    def _evict_session_state(self, session_key: str | int, keep_durable_record: bool = False) -> None:
        """
        Pop ``session_key`` out of every per-session map so its entries cannot leak.

        Called by the per-session weakref finalizer registered in
        :meth:`_register_session_finalizer` (tier-3) and by any explicit eviction
        path that hands us a session key directly. Idempotent. Stops the language
        servers of the session's project, off the calling thread, if no other
        session holds that project.

        :param session_key: the key of the session to evict
        :param keep_durable_record: whether to keep the key's durable explicit-activation record, so a call that
            later arrives under the key restores its project; the default discards it
        """
        evicted_project = self._active_projects_by_session.pop(session_key, None)
        self._active_modes_by_session.pop(session_key, None)
        self._active_tools_by_session.pop(session_key, None)
        self._mode_overrides_by_session.pop(session_key, None)
        self._explicit_project_roots_by_session.pop(session_key, None)
        # a session that is genuinely gone drops its durable record too; leaving it would let a future
        # session that reuses the key inherit this one's project. An eviction that keeps the record leaves
        # the key restorable from the store.
        if not keep_durable_record:
            self._explicit_project_root_store.discard(session_key)
        self._cursor_managers_by_session.pop(session_key, None)
        self._warned_missing_forwarded_session_keys.discard(session_key)
        with self._session_finalizers_lock:
            self._session_finalizers.pop(session_key, None)
        # tear down the session's per-session executor so its worker thread does not idle in
        # ``time.sleep(0.1)`` for the rest of the daemon's lifetime; pending tasks have their
        # futures cancelled so any blocked caller raises CancelledError instead of hanging.
        with self._task_executors_by_session_lock:
            executor = self._task_executors_by_session.pop(session_key, None)
        if executor is not None:
            executor.shutdown()
        self._release_project_if_unheld_in_background(evicted_project)

    def evict_forwarded_session(self, forwarded_session_id: str) -> list[str]:
        """
        Evicts every per-session slot that lies under an ended forwarded session (pg://serena/session/eviction).

        A slot lies under ``forwarded_session_id`` when its key equals that id or begins with that id followed by
        ``/``, the separator of a tier-2 key's ``/session/<id>`` and ``/agent/<id>`` suffixes. Each such slot is evicted
        through :meth:`_evict_session_state`, keeping its durable explicit-activation record, so a call that later
        arrives under the key restores its explicitly activated project.

        :param forwarded_session_id: the forwarded session id the multiplexer reports ended
        :return: the evicted session keys
        """
        # the keys of every per-session map, so a slot holding only an executor or a cursor manager is found too
        with self._task_executors_by_session_lock:
            executor_keys = list(self._task_executors_by_session)
        candidate_keys = {
            *list(self._active_projects_by_session),
            *list(self._active_modes_by_session),
            *list(self._active_tools_by_session),
            *list(self._cursor_managers_by_session),
            *executor_keys,
        }

        # the slots under the forwarded id, never a key that merely shares its leading characters
        prefix = forwarded_session_id + "/"
        evicted = [key for key in candidate_keys if isinstance(key, str) and (key == forwarded_session_id or key.startswith(prefix))]
        for key in evicted:
            self._evict_session_state(key, keep_durable_record=True)
        return evicted

    def evict_claude_session(self, claude_session_id: str) -> list[str]:
        """
        Evicts every per-session slot that carries an ended Claude Code session (pg://serena/session/eviction/claude_session).

        A slot carries ``claude_session_id`` when its key is ``<F>/session/<claude_session_id>`` or begins with
        ``<F>/session/<claude_session_id>/``, for any forwarded session id ``F``: the ``/session/<id>`` segment of a tier-2
        key. A key whose session id merely begins with ``claude_session_id`` is not evicted. Each such slot is evicted
        through :meth:`_evict_session_state`, keeping its durable explicit-activation record, so a call that later arrives
        under the key restores its explicitly activated project.

        :param claude_session_id: the Claude Code session id the brain SessionEnd hook reports ended
        :return: the evicted session keys
        """
        # the keys of every per-session map, so a slot holding only an executor or a cursor manager is found too
        with self._task_executors_by_session_lock:
            executor_keys = list(self._task_executors_by_session)
        candidate_keys = {
            *list(self._active_projects_by_session),
            *list(self._active_modes_by_session),
            *list(self._active_tools_by_session),
            *list(self._cursor_managers_by_session),
            *executor_keys,
        }

        # the slots carrying the session id after a forwarded session id, followed by nothing or by "/"
        carrying = re.compile(r".+/session/" + re.escape(claude_session_id) + r"(/.*)?", re.DOTALL)
        evicted = [key for key in candidate_keys if isinstance(key, str) and carrying.fullmatch(key)]
        for key in evicted:
            self._evict_session_state(key, keep_durable_record=True)
        return evicted

    def _warn_missing_forwarded_session_id_once(self, session_key: str | int) -> None:
        """Emits a one-time warning when no X-Forwarded-Mcp-Session-Id header is present.

        Tier 3 of the session-key derivation in :meth:`Tool.apply_ex` falls back to
        ``id(mcp_ctx.session)`` when no header is present (e.g. legacy direct-stdio
        clients that bypass the multiplexer, or stateless streamable-http inbound).
        Per plan://Serena:serena/streamable-http-cc-session-id-pass-through-v2, that
        path is supported but must be visible in operator logs.

        Idempotent per ``session_key``: the first call emits a warning and tracks
        the key; subsequent calls for the same key are no-ops. The tracker entry
        is dropped by :meth:`_evict_session_state` when the session is GC'd.

        :param session_key: the per-session dict key used in
            :attr:`_active_projects_by_session` (an ``id()`` int for tier 3).
        """
        if session_key in self._warned_missing_forwarded_session_keys:
            return
        self._warned_missing_forwarded_session_keys.add(session_key)
        log.warning(
            "streamable-http session without X-Forwarded-Mcp-Session-Id header; "
            "falling back to the id of the MCP client's connection=%s. Multiple Claude Code sessions "
            "sharing a multiplexer will collapse onto this same daemon-side slot. "
            "See plan://Serena:serena/streamable-http-cc-session-id-pass-through-v2.",
            session_key,
        )

    def evict_pipe_session(self, session_id: str) -> None:
        """Drop ``session_id`` from every per-session map.

        :param session_id: The pipe-asserted session_id (an absolute project_root
            path under the project-root-as-session-id contract) to evict.

        Under the project-root-as-session-id contract, this method is NOT wired
        to socket-disconnect events: per-session state must survive socket churn
        so a respawned pipe-client into the same project_root re-attaches to the
        existing entry. ``build_pipe_listener`` therefore registers no disconnect
        handler. This method remains available as the implementation of explicit
        operator-driven eviction (daemon shutdown, an ``activate_project`` to a
        different project from a session that previously held this entry, or
        future operator-issued unregister flows). It is idempotent: calling it
        for an unknown ``session_id`` is a no-op. Stops the language servers of
        the session's project, off the calling thread, if no other session holds
        that project.
        """
        evicted_project = self._active_projects_by_session.pop(session_id, None)
        self._active_modes_by_session.pop(session_id, None)
        self._active_tools_by_session.pop(session_id, None)
        self._mode_overrides_by_session.pop(session_id, None)
        self._explicit_project_roots_by_session.pop(session_id, None)
        self._explicit_project_root_store.discard(session_id)
        self._cursor_managers_by_session.pop(session_id, None)
        # tear down the pipe session's per-session executor so its worker thread does not idle
        # past the forwarder's life; pending tasks have their futures cancelled.
        with self._task_executors_by_session_lock:
            executor = self._task_executors_by_session.pop(session_id, None)
        if executor is not None:
            executor.shutdown()
        self._release_project_if_unheld_in_background(evicted_project)
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

    def get_active_modes(self) -> ActiveModes:
        """
        :return: the active modes
        """
        return self._calling_active_modes()

    def set_modes(self, mode_names: list[str]) -> None:
        """
        Set the mode configurations of the calling session (or of the single slot outside any session); no other
        session's modes or tools change.

        :param mode_names: List of mode names or paths to use
        """
        mode_overrides = ModeSelectionDefinition(default_modes=mode_names)
        session_key = _SESSION_KEY_VAR.get(None)
        if session_key is not None:
            self._mode_overrides_by_session[session_key] = mode_overrides
        else:
            self._mode_overrides = mode_overrides
        self._update_active_modes()
        self._update_active_tools()

        log.info(f"Set modes to {list(self.get_active_modes().get_mode_names())}")

    @dataclass
    class PromptParams:
        """
        Holds parameters for prompt rendering
        """

        available_tools: set[str]
        """
        available tool names or, in REPL mode, the names of the raw facade methods (without facade name prefix) and 
        the names of the corresponding tools
        """
        available_markers: set[str]
        """
        names of the ToolMarkers (class names) that the available tools inherit from
        """
        tool_names_mapping: dict[str, str]
        """
        mapping from standard tool names to currently used and replacement tool/API method names.
        In particular, this maps 
          * legacy tool names to current tool names
          * LSP tool names to their backend- and interface-specific counterparts 
            (e.g. "find_symbol" to "jet_brains_find_symbol" in JetBrains mode, "find_symbol" to the corresponding API method name
            when using the REPL interface). 
        """

        def get_function_name(self, tool_class: type[Tool]) -> str:
            """
            :param tool_class: the tool for which to get the function name
            :return: the function name to use for this tool in prompts, which may be different from the tool's standard name
                (e.g. when using a different language backend or when using the REPL interface)
            """
            tool_name = tool_class.get_name_from_cls()
            return self.tool_names_mapping.get(tool_name, tool_name)

    def _get_prompt_params(self) -> PromptParams:
        """
        :return: parameters for prompt rendering depending on the current agent interface, language backend and active tools/methods
        """
        if self._prompt_params is not None:
            return self._prompt_params

        if self._agent_interface == AgentInterface.TOOLS:
            # available tool names are simply the exposed tools
            available_tool_names = set(self._exposed_tools.tool_names)
            available_tool_marker_names = set(self._exposed_tools.tool_marker_names)

            tool_name_mapping = dict(ToolSet.LEGACY_TOOL_NAME_MAPPING)
            class_replacements = self._language_backend.get_lsp_tool_class_replacements()
            for tool_class in ToolRegistry().get_all_tool_classes():
                new_tool_class: type[Tool] = class_replacements.get(tool_class, tool_class)
                tool_name_mapping[tool_class.get_name_from_cls()] = new_tool_class.get_name_from_cls()

        elif self._agent_interface == AgentInterface.REPL:
            # available tool names include both the names of the facade methods and the names of the corresponding tools
            repl = self.get_repl()
            enabled_methods = repl.entrypoint.get_enabled_methods()
            corresponding_tool_classes = [m.info.corresponding_tool for m in enabled_methods if m.info.corresponding_tool is not None]
            available_tools = AvailableTools(
                self._exposed_tools.tools + [self._all_tools[tool_class] for tool_class in corresponding_tool_classes]
            )
            available_tool_names = set(available_tools.tool_names).union({m.info.name for m in enabled_methods})
            available_tool_marker_names = set(available_tools.tool_marker_names)

            tool_class_replacements = self._language_backend.get_lsp_tool_class_replacements()
            methods_by_tool_class = {m.info.corresponding_tool: m for m in enabled_methods if m.info.corresponding_tool is not None}

            def get_name(tool_class: type[Tool]) -> str:
                # if there is a corresponding method in the API, return its qualified name (as used in REPL code)
                method = methods_by_tool_class.get(tool_class)
                if method is not None:
                    return method.qualified_name
                # if there is a corresponding method for the replacement class, return its qualified name
                replacement_class = tool_class_replacements.get(tool_class)
                if replacement_class is not None:
                    replacement_method = methods_by_tool_class.get(replacement_class)
                    if replacement_method is not None:
                        return replacement_method.qualified_name
                # otherwise, keep the tool's name
                return tool_class.get_name_from_cls()

            tool_name_mapping = {}
            for legacy_name, new_name in ToolSet.LEGACY_TOOL_NAME_MAPPING.items():
                tool_name_mapping[legacy_name] = get_name(ToolRegistry().get_tool_class_by_name(new_name))
            for tool_class in ToolRegistry().get_all_tool_classes():
                tool_name_mapping[tool_class.get_name_from_cls()] = get_name(tool_class)
        else:
            raise ValueError()

        return self.PromptParams(
            available_tools=available_tool_names, available_markers=available_tool_marker_names, tool_names_mapping=tool_name_mapping
        )

    @staticmethod
    def _format_prompt_tag(text: str, tag: str, tag_name_attr: str | None = None) -> str:
        open_tag = f"<{tag}" + (f' name="{tag_name_attr}"' if tag_name_attr is not None else "") + ">"
        close_tag = f"</{tag}>"
        return f"{open_tag}\n{text.strip()}\n{close_tag}"

    def _render_prompt(self, prompt_template: str, tag: str | None = None, tag_name_attr: str | None = None) -> str:
        """
        Renders the given prompt template, providing the necessary variables and functions

        :param prompt_template: the template text (jinja2) to render
        :param tag: if not None, wraps the rendered prompt text in a tag
        :param tag_name_attr: for the case where tag is not None, specifies the value of the "name" attribute of the tag
        :return: the rendered prompt
        """

        def embed_memory(memory_name: str) -> str:
            try:
                memory_manager = self._get_memory_manager()
                return self._format_prompt_tag(memory_manager.load_memory(memory_name), tag="memory", tag_name_attr=memory_name)
            except Exception as e:
                log.error("Tried to embed memory '%s' but failed to load it: %s", memory_name, e)
                return ""

        template = JinjaTemplate(prompt_template)
        prompt_params = self._get_prompt_params()
        text = template.render(
            available_tools=prompt_params.available_tools,
            available_markers=prompt_params.available_markers,
            tool_names=prompt_params.tool_names_mapping,
            embed_memory=embed_memory,
        )

        if tag is not None:
            text = self._format_prompt_tag(text, tag=tag, tag_name_attr=tag_name_attr)

        return text

    def create_connection_prompt(self) -> str:
        """
        Returns the bootstrap prompt to be sent at MCP connection time.

        :return: the prompt
        """
        return self.prompt_factory.create_connection_prompt()

    def _create_global_memory_manager(self) -> MemoryManager:
        """
        :return: a memory manager for global memories only (no project memories)
        """
        return MemoryManager(serena_data_folder=None, read_only_memory_patterns=self.serena_config.read_only_memory_patterns)

    def _get_memory_manager(self) -> MemoryManager:
        """
        :return: the memory manager for the active project (if any) or a global memory manager if no project is active
        """
        if self._active_project is not None:
            return self._active_project.memory_manager
        else:
            return self._create_global_memory_manager()

    def get_calling_session_id(self) -> str:
        """
        :return: the id of the Serena session of the caller: the key of the calling session, or for a caller outside any session,
            the id of the single slot's session
        """
        session_key = _SESSION_KEY_VAR.get(None)
        return str(session_key) if session_key is not None else _SINGLE_SLOT_SESSION_ID

    def create_session(self) -> SerenaSession:
        """
        :return: a new client session (with a random id)
        """
        return self._session_registry.create_session()

    def get_session(self, session_id: str) -> SerenaSession:
        """
        :param session_id: the session id (as supplied by the LLM)
        :return: the session, which is created if it is unknown
        """
        return self._session_registry.get_session(session_id)

    def create_system_prompt(self) -> str:
        """
        Returns the 'Serena Instructions Manual', i.e. Serena's system prompt.
        The prompt also establishes the calling session's Serena session (see `SerenaSession`), stating its id for use with
        the REPL tool.

        :return: the prompt
        """
        # the Serena session of the calling session
        session_id = self.get_calling_session_id()

        global_memories = self._create_global_memory_manager().list_global_memories()
        global_memories_str = dict_string(global_memories.to_dict()) if len(global_memories) > 0 else ""

        # determine modes for which prompts must (still) be provided, excluding modes that were already provided in a
        # previously provided project activation message (if any)
        relevant_modes = []
        for mode in self.get_active_modes().get_modes(include_background_base_modes=True):
            if mode.has_prompt():
                if not self._project_prompt_status.is_mode_prompt_already_provided(mode.name, session_id):
                    relevant_modes.append(mode)
        self._project_prompt_status.mark_mode_prompts_as_provided(session_id)

        prompt_params = self._get_prompt_params()
        system_prompt = self.prompt_factory.create_system_prompt(
            context_system_prompt=self._render_prompt(self._context.prompt, tag="context"),
            mode_system_prompts=[self._render_prompt(mode.prompt, tag="mode", tag_name_attr=mode.name) for mode in relevant_modes],
            available_tools=prompt_params.available_tools,
            available_markers=prompt_params.available_markers,
            global_memories_list=global_memories_str,
            tool_names=prompt_params.tool_names_mapping,
        )

        # provide the project activation message if it hasn't yet been provided
        if self._active_project is not None and not self._project_prompt_status.is_project_activation_message_already_provided(session_id):
            system_prompt += "\n\n" + self._format_prompt_tag(self.get_project_activation_message(session_id), tag="active-project")
        elif self._project_activation_error:
            system_prompt += f"\n\nNo project is active ({self._project_activation_error})."

        # inform about the session id
        system_prompt += "\n\n" + self._format_prompt_tag(
            f"Your Serena session id is `{session_id}`. Pass it as the `session` parameter to tools which require it.",
            tag="session",
        )

        return self._format_prompt_tag(system_prompt, tag="serena")

    def get_project_activation_message(self, session_id: str) -> str:
        """
        :return: a message providing information about the project upon activation (e.g. programming language, memories, initial prompt)
        :raise: AssertionError if no project is active
        """
        proj = self._active_project
        assert proj is not None, "A project must be active before calling this."

        prompt_params = self._get_prompt_params()

        # Note: The activation message is always returned in full, even if it was already provided in the current session,
        #   because some clients (e.g. Claude Desktop) will use the same session across multiple chats.
        #   So while we don't want the activation message to be additionally included in the system prompt
        #   (initial_instructions), an explicit project activation should always return it.
        # (The check below deliberately left in place for documentation purposes, preventing a regression)
        if self._project_prompt_status.is_project_activation_message_already_provided(session_id):
            pass  # no special handling

        # provide basic project information (name, location, languages, encoding)
        if proj.is_newly_created:
            msg = f"Created and activated a new project with name '{proj.project_name}' at {proj.project_root}.\n"
        else:
            msg = f"The project with name '{proj.project_name}' at {proj.project_root} is activated.\n"
        msg += self._language_backend.get_project_activation_statement(proj)
        msg += f"File encoding: {proj.project_config.encoding}.\n"

        # add list of memories (if memories are enabled)
        include_memories = self._calling_active_tools().contains_tool_class(ReadMemoryTool)
        if include_memories:
            project_memories = proj.memory_manager.list_project_memories()
            if project_memories:
                msg += (
                    f"{json.dumps(project_memories.to_dict())}\n"
                    + f"Use `{prompt_params.get_function_name(ReadMemoryTool)}` to read these memories later if they are relevant to the task.\n"
                )
            elif self.is_tool_function_available(OnboardingTool):
                msg += f"Onboarding has not been performed yet. Ask the user whether to perform onboarding and if so, call `{prompt_params.get_function_name(OnboardingTool)}`.\n"

        # add prompts for modes that were dynamically activated by the project
        modes_with_prompts = self._project_prompt_status.get_modes_with_prompts_to_be_provided_for_project_activation(session_id)
        if modes_with_prompts:
            for mode in modes_with_prompts:
                msg += self._render_prompt(mode.prompt, tag="mode", tag_name_attr=mode.name) + "\n"
        self._project_prompt_status.mark_mode_prompts_as_provided(session_id)

        # add the project's prompt (if any)
        if proj.project_config.initial_prompt:
            msg += "\n" + self._render_prompt(proj.project_config.initial_prompt, tag="project-instructions")

        # when the REPL is active, add information on available facades if the agent is not in single-project mode
        # (for single-project mode where the facades can't change, they are provided in the tool's description)
        if self._calling_active_tools().contains_tool_class(SerenaReplTool) and not self.is_single_project():
            msg += f"\n\nAvailable facades for the `{SerenaReplTool.get_name_from_cls()}` tool:\n" + self.get_repl().entrypoint.overview()

        self._project_prompt_status.mark_project_activation_message_as_provided(session_id)

        return msg

    def _mode_overrides_of(self, session_key: str | int | None) -> ModeSelectionDefinition | None:
        """
        :param session_key: the key of a session, or None for the single slot outside any session
        :return: the mode overrides that apply to that session: those it set, or else those the agent was started with;
            for the single slot, those set for it
        """
        if session_key is None:
            return self._mode_overrides
        return self._mode_overrides_by_session.get(session_key, self._configured_mode_overrides)

    def _derive_active_modes(self, project: Project | None, mode_overrides: ModeSelectionDefinition | None) -> ActiveModes:
        """
        :param project: the project whose configuration applies, or None
        :param mode_overrides: the mode overrides that apply, or None
        :return: the active modes derived from the Serena configuration, the given project's configuration (if any),
            and the given mode overrides (if any)
        """
        background_base_modes = []
        if self._language_backend.is_jetbrains():
            background_base_modes.append(SerenaAgentMode.from_name_internal("jetbrains"))
        active_modes = ActiveModes(background_base_modes=background_base_modes)
        active_modes.apply(self.serena_config)
        if project is not None:
            active_modes.apply(project.project_config)
        if mode_overrides:
            active_modes.apply(mode_overrides)
        return active_modes

    def _derive_active_tools(self, active_modes: ActiveModes, project: Project | None) -> AvailableTools:
        """
        :param active_modes: the active modes that apply
        :param project: the project whose configuration applies, or None
        :return: the active tools derived from the base tool set, which already takes the Serena configuration and the
            context into account (as well as many other aspects, such as JetBrains mode), the given modes, and the
            given project's configuration (if any)
        """
        if self._agent_interface.is_repl():
            # the REPL toolset is fixed; tool inclusion/exclusion definitions do not apply
            tool_set = self._base_toolset
        else:
            # apply modes
            tool_set = self._base_toolset.apply(*active_modes.get_modes())

            # apply the project configuration (if any)
            if project is not None:
                tool_set = tool_set.apply(project.project_config)
                if project.project_config.read_only:
                    tool_set = tool_set.without_editing_tools()

        return tool_set.to_available_tools(self._all_tools)

    def _calling_active_modes(self) -> ActiveModes:
        """
        :return: the active modes of the calling session, derived from that session's own project; a session that has
            activated no project has the modes of the Serena configuration alone. Outside any session, the single slot.
        """
        session_key = _SESSION_KEY_VAR.get(None)
        if session_key is None:
            return self._active_modes
        return self._active_modes_by_session.get(session_key, self._config_active_modes)

    def _calling_active_tools(self) -> AvailableTools:
        """
        :return: the active tools of the calling session, derived from that session's own project; a session that has
            activated no project has the tools of the Serena configuration alone. Outside any session, the single slot.
        """
        session_key = _SESSION_KEY_VAR.get(None)
        if session_key is None:
            return self._active_tools
        return self._active_tools_by_session.get(session_key, self._config_active_tools)

    def _update_active_modes(self, log_message: bool = True) -> None:
        """
        Updates the active modes of the calling session (or of the single slot outside any session) from the Serena
        configuration, that session's active project configuration (if any), and mode overrides (if any).

        :param log_message: whether to log the resulting active modes
        """
        session_key = _SESSION_KEY_VAR.get(None)
        active_modes = self._derive_active_modes(self._active_project, self._mode_overrides_of(session_key))
        if session_key is not None:
            self._active_modes_by_session[session_key] = active_modes
        else:
            self._active_modes = active_modes
        if log_message:
            active_mode_names = active_modes.get_mode_names()
            log.info(f"Active modes ({len(active_mode_names)}): {', '.join(active_mode_names)}")

    def _update_active_tools(self) -> None:
        """
        Updates the active tools of the calling session (or of the single slot outside any session) from its active
        modes and its active project (if any).
        """
        active_tools = self._derive_active_tools(self._calling_active_modes(), self._active_project)
        session_key = _SESSION_KEY_VAR.get(None)
        if session_key is not None:
            self._active_tools_by_session[session_key] = active_tools
        else:
            self._active_tools = active_tools
        log.info(f"Active tools ({len(active_tools)}): {', '.join(active_tools.tool_names)}")

        # reset members that depend on the active tools, so that they are re-created on demand with the new active tools
        self._repl = None
        self._prompt_params = None

        # check if a tool was activated that is not in the exposed tool set and issue a warning if so
        active_tools_not_exposed = set(active_tools.tool_names) - set(self._exposed_tools.tool_names)
        if active_tools_not_exposed:
            log.warning(
                "The following active tools are not in the exposed tool set and thus won't be available to clients:\n"
                f"{active_tools_not_exposed}\n"
                "Consider adjusting your configuration to include these tools if you want to use them."
            )

    def create_default_facade_list(self, api_scope: ApiScope) -> list[Facade]:
        """
        :return: the default list of facades provided by Serena itself, not including any language backend-specific facades
        """
        return [
            Facade.from_api(ConfigApi(self), api_scope),
            Facade.from_api(FsApi(self), api_scope),
            Facade.from_api(EditApi(self), api_scope),
            Facade.from_api(MemoryApi(self), api_scope),
            Facade.from_api(ShellApi(self), api_scope),
            Facade.from_api(ExternalProjectsApi(self), api_scope, is_optional=True),
        ]

    def get_repl(self) -> SerenaRepl:
        """
        :return: the REPL instance for this agent, creating it if necessary
        """
        if self._repl is None:
            # determine API scope
            api_scope = ApiScope()
            api_scope.process(self.serena_config)
            api_scope.process(self._context)
            for mode in self._calling_active_modes().get_modes():
                api_scope.process(mode)
            if self._active_project:
                api_scope.process(self._active_project.project_config)
                if self._active_project.project_config.read_only:
                    api_scope.exclude_editing()
            if not self._is_dashboard_openable(self.serena_config):
                api_scope.process(NamedApiInclusionDefinition(name="Dashboard", excluded_apis=["cfg.open_dashboard"]))

            facades = self.create_default_facade_list(api_scope) + self._language_backend.create_facades(self, api_scope)
            self._repl = SerenaRepl(facades, api_scope)
        return self._repl

    def issue_task(
        self, task: Callable[[], T], name: str | None = None, logged: bool = True, timeout: float | None = None
    ) -> TaskExecutor.Task[T]:
        """
        Issue a task to the executor for asynchronous execution.

        Within a single MCP session, tasks remain serialised in issue order: each session has its
        own :class:`TaskExecutor` whose single worker thread pulls one task at a time, preserving
        the activate_project → init_language_server_manager → tool-call ordering that the daemon
        used to enforce globally. Across sessions, tasks run in parallel: each session has its own
        queue, so a slow task in session A no longer blocks a fast task in session B. Non-MCP
        callers (CLI, dashboard, scripts, tests) with no session in scope route through the
        daemon-wide executor.

        :param task: the task to execute
        :param name: the name of the task for logging purposes; if None, use the task function's name
        :param logged: whether to log management of the task; if False, only errors will be logged
        :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
        :return: the task object, through which the task's future result can be accessed
        """
        # route to the per-session executor when a session is in scope; fall back to the daemon-wide
        # executor for non-MCP callers. Per-session executors are created lazily on first use and
        # torn down on session eviction (see :meth:`_evict_session_state` / :meth:`evict_pipe_session`).
        session_key = _SESSION_KEY_VAR.get(None)
        # IRONCLAD: while an MCP call is in flight, the daemon-wide executor is unreachable. Tool.apply_ex
        # always binds _SESSION_KEY_VAR before calling issue_task, so this is defense-in-depth: if the
        # binding ever leaks, the daemon-wide queue would let other sessions observe each other's active
        # project. Mirrors get_cursor_manager and _active_project.
        mcp_in_flight = _MCP_CALL_IN_FLIGHT.get()
        if mcp_in_flight and session_key is None:
            raise RuntimeError(
                "Internal error: MCP call is in flight but _SESSION_KEY_VAR is unset. "
                "This indicates Tool.apply_ex did not propagate the session key into the worker "
                "context; refusing to fall through to the daemon-wide task executor because "
                "doing so would risk cross-project state confusion."
            )
        if session_key is None:
            executor = self._task_executor
        else:
            with self._task_executors_by_session_lock:
                executor = self._task_executors_by_session.get(session_key)
                if executor is None:
                    # short label keeps logs readable; raw uuid hex / id() values are noisy
                    executor = TaskExecutor(f"SerenaAgentTaskExecutor[{str(session_key)[:8]}]")
                    self._task_executors_by_session[session_key] = executor
        return executor.issue_task(task, name=name, logged=logged, timeout=timeout)

    def execute_task(self, task: Callable[[], T], name: str | None = None, logged: bool = True, timeout: float | None = None) -> T:
        """
        Executes the given task synchronously.
        This is useful for tasks that need to be executed immediately and whose results are needed right away.

        Outside any session, the task runs on the daemon-wide executor. Within a session, it runs on a thread of its
        own: the calling task holds its session's queue for as long as it waits, so a task queued there would wait on
        its own caller, and the daemon-wide queue serves the callers outside any session.

        :param task: the task to execute
        :param name: the name of the task for logging purposes; if None, use the task function's name
        :param logged: whether to log management of the task; if False, only errors will be logged
        :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
        :return: the result of the task execution
        """
        # within a session, run on a thread of its own rather than on any queue
        if _SESSION_KEY_VAR.get(None) is not None:
            own_thread_task = TaskExecutor.Task(task, name=name or task.__name__, logged=logged, timeout=timeout)
            own_thread_task.start()
            return own_thread_task.result()

        return self._task_executor.execute_task(task, name=name, logged=logged, timeout=timeout)

    def _task_completion_callback(self) -> None:
        """
        Called after every task completion. Tasks potentially change the agent's state/configuration.
        """
        self._on_config_changed()

    def _on_config_changed(self) -> None:
        for callback in self._config_changed_callbacks:
            try:
                callback()
            except Exception as e:
                log.error(f"Error in config changed callback {callback}: {e}", exc_info=e)

    def register_config_changed_callback(self, callback: Callable[[], None]) -> None:
        """
        Registers a callback to be called when the agent configuration has (potentially) changed.

        :param callback: the callback function to register
        """
        self._config_changed_callbacks.append(callback)

    def is_using_language_server(self) -> bool:
        """
        :return: whether this agent uses language server-based code analysis
        """
        return self._language_backend == BuiltinLanguageBackend.LSP

    def _activate_project(
        self,
        project: Project,
        update_active_modes: bool = True,
        update_active_tools: bool = True,
        record_explicit: bool = True,
    ) -> bool:
        """
        :param record_explicit: when True (the default for user/startup activations) and a session is
            in scope, record ``project.project_root`` in :attr:`_explicit_project_roots_by_session` so a
            later self-heal restores exactly this project. The self-heal restore and the forwarded
            fallback pass False so the map stays a record of genuine activations only.
        :return: True if the project was newly activated, False if it was already active for the calling context
        """
        # check if the project is already active for the calling context (per session if one is in scope,
        # otherwise the legacy slot — see the _active_project property); an instance of the same root built from an
        # older configuration is not the project (pg://serena/session/project/instance/currency)
        # an in-flight pin is the instance the calling tool call runs on; otherwise the calling context's own slot, never
        # the single slot that a session with an empty slot falls through to outside an in-flight call
        in_flight = _ACTIVE_PROJECT_VAR.get(_UNSET)
        if in_flight is not _UNSET:
            current = in_flight
        elif _SESSION_KEY_VAR.get(None) is not None:
            current = self._active_projects_by_session.get(_SESSION_KEY_VAR.get(None))
        else:
            current = self._legacy_active_project
        if current is project:
            return False

        log.info(f"Activating {project.project_name} at {project.project_root}")

        self._project_activation_error = None

        # handle the case where the project requires a different language backend than the current one.
        # With the tool interface, the backend cannot change, since the set of exposed tools depends on it and is fixed
        # for the session. With the REPL interface, the backend can be switched, as all backend-dependent state
        # (background modes, REPL facades, prompt parameters, the project's language backend initialisation) is
        # recomputed upon activation.
        project_backend = project.project_config.language_backend
        if project_backend is not None and project_backend != self._language_backend:
            if self._agent_interface.is_tools():
                raise ValueError(
                    f"Cannot activate project '{project.project_name}': it requires the {project_backend} backend, "
                    f"but this session was initialized with {self._language_backend}. "
                    f"Workarounds: (1) Use project activation at startup via the --project flag, "
                    f"(2) Configure one MCP server per backend in your client, (3) use the REPL interface."
                )
            log.info(f"Switching language backend from {self._language_backend} to {project_backend} for project '{project.project_name}'")
            self._language_backend = project_backend

        # In a multi-session MCP daemon, "switching" is per-session: other concurrent sessions may
        # still hold the previous project as their active one, and tearing down its language servers
        # would break them (the same regression class addressed at process-exit-only teardown
        # in SerenaMCPFactory; see mcp.py around create_mcp_server). So the project this session held
        # is read from the session's own slot (not an in-flight override) and released below only if
        # no session holds it once this session has switched.
        session_key = _SESSION_KEY_VAR.get(None)
        previous_project = self._active_projects_by_session.get(session_key) if session_key is not None else self._legacy_active_project

        # update via the property setter, which routes to the per-session slot (or the legacy fallback)
        self._active_project = project
        # a successful activation invalidates any preserved startup-activation failure
        self._startup_activation_error = None
        self._startup_activation_target = None
        # invalidate the cursor manager for *this* calling context only — per-session if a session is in scope,
        # otherwise the legacy slot. A previous implementation cleared a single agent-wide attribute here, which
        # under multi-session use wiped concurrent sessions' cursors whenever any session switched projects.
        if session_key is not None:
            self._cursor_managers_by_session.pop(session_key, None)
            if record_explicit:
                # remember the root THIS session explicitly activated so a later self-heal
                # (Tool.apply_ex) restores exactly this project after a per-session slot loss,
                # rather than silently rebinding to the multiplexer-forwarded origin cwd.
                self._explicit_project_roots_by_session[session_key] = project.project_root
                # and durably, so the restore also survives a daemon restart -- which wipes the map
                # above and is the failure the in-memory record alone never covered.
                self._explicit_project_root_store.set(session_key, project.project_root)
        else:
            self._legacy_cursor_manager = None
        # this session no longer holds the previous project; stop its language servers unless another session does
        if previous_project is not project:
            self._release_project_if_unheld(previous_project)
        project.set_agent(self)

        if update_active_modes:
            active_mode_names_before = set(self._calling_active_modes().get_mode_names())
            self._update_active_modes()
            newly_activated_mode_names = set(self._calling_active_modes().get_mode_names()) - active_mode_names_before
        else:
            newly_activated_mode_names = None

        self._project_prompt_status = ProjectPromptProvisionStatus(newly_activated_mode_names=newly_activated_mode_names)

        if update_active_tools:
            self._update_active_tools()

        def init_project_services() -> None:
            self._run_project_activation_command(project)

            # wait for the replaced instance (if any) before taking the lock, so the wait never blocks a release of this
            # instance (pg://serena/session/project/instance/uniqueness)
            project.wait_for_replaced_project()
            with project.language_server_manager_lock():
                # a session that switched away before this ran has released the project; starting its language
                # servers now would leave servers running that no session holds
                with self._project_holders_lock:
                    if not self._is_project_held(project):
                        return

                # initialise the project's language backend
                if self._language_backend.is_lsp():
                    with LogTime("Language server initialization", logger=log):
                        project.create_language_server_manager()
                else:
                    self._language_backend.init_active_project(self)

        # initialise the project's language backend in the background; readiness is signalled on the project's
        # :attr:`Project._lsm_ready_event`, which both the activation-message path and downstream tool-call read paths
        # (:meth:`Project.get_language_server_manager_or_raise`) wait on. The signal lives on the project rather than on a
        # per-agent task handle so concurrent activations in other sessions cannot overwrite it. With a session in scope,
        # this activation runs inside a task of the session's own queue that goes on to wait for that signal, so the
        # initialization is started on a thread of its own rather than queued behind that task.
        if _SESSION_KEY_VAR.get(None) is None:
            self.issue_task(init_project_services)
        else:
            TaskExecutor.Task(init_project_services, name=f"InitProjectServices[{project.project_name}]").start()

        if self._project_activation_callback is not None:
            self._project_activation_callback()

        # notify the dashboard manager of the project change (if applicable)
        if self._dashboard_manager:
            self._dashboard_manager.update_active_project(self._active_project)

        return True

    @staticmethod
    def _run_project_activation_command(project: Project) -> None:
        """
        Runs the given project's activation_command (if set and the project is trusted).
        Failures are logged.
        """
        activation_command = project.project_config.activation_command
        if not activation_command:
            return
        if not project.is_trusted():
            log.warning(
                f"Project path {project.project_root} is not trusted, ignoring activation_command "
                "from project configuration. To trust the project, modify the trusted path patterns "
                "in the global configuration."
            )
            return
        timeout = project.project_config.activation_command_timeout
        cmd = subprocess_util.convert_shell_cmd(activation_command)
        log.info(f"Running activation_command for project '{project.project_name}': {cmd}")
        try:
            with LogTime("Project activation command", logger=log):
                p = supervise.Popen(
                    cmd,
                    shell=True,
                    cwd=project.project_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **subprocess_util.subprocess_kwargs(),
                )
                try:
                    _, stderr = p.communicate(timeout=timeout)
                    if p.returncode != 0:
                        log.error(f"activation_command for project '{project.project_name}' failed (exit {p.returncode}): {stderr.strip()}")
                except subprocess.TimeoutExpired:
                    log.error(
                        f"Activation_command for project '{project.project_name}' timed out after "
                        f"{timeout}s; terminating process and continuing with backend initialisation."
                    )
                    terminate_process_tree_with_kill_fallback(p, terminate_timeout=5.0, process_name="activation_command")  # type: ignore[arg-type]
                    p.release()
        except Exception:
            log.exception(f"Unexpected error running activation_command for project '{project.project_name}'")

    def activate_project_from_path_or_name(
        self,
        project_root_or_name: str,
        update_active_modes: bool = True,
        update_active_tools: bool = True,
        record_explicit: bool = True,
    ) -> bool:
        """
        Activate a project from a path or a name.
        If the project was already registered, it will just be activated. Any change to its ``project.yml``
        on disk since the MCP process started (or since the project was last activated) is picked up here:
        the on-disk configuration is re-read and, if it differs from the cached one, any memoized project
        instance is dropped and rebuilt. Every other session holding the replaced instance is moved onto the
        new one, the replaced instance stops its language servers once no session, cursor manager or tool call
        holds it, and the new instance starts its language server manager only after that, with the updated
        language list.
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
            project_instance = registered_project.get_project_instance(serena_config=self.serena_config)
            log.info(f"Found registered project '{project_instance.project_name}' at path {project_instance.project_root}")
            # the changed configuration replaced the project's instance: move the holders of the replaced instance onto
            # the new one rather than shutting it down under them (pg://serena/session/project/instance/shutdown)
            replaced_project = project_instance.replaced_project
            if config_changed and replaced_project is not None:
                log.info(
                    "Configuration changed for project '%s'; moving the sessions holding it onto a new instance.",
                    registered_project.project_name,
                )
                self._move_holders_onto_replacement(replaced_project, project_instance)
        elif os.path.isdir(project_root_or_name):
            project_instance = self.serena_config.add_project_from_path(project_root_or_name, asynchronous_autogen=True)
            log.info(f"Added new project {project_instance.project_name} for path {project_instance.project_root}")

        if project_instance is None:
            raise ProjectNotFoundError(
                f"Project '{project_root_or_name}' not found: Not a valid project name or directory. "
                f"Existing project names: {self.serena_config.project_names}"
            )

        return self._activate_project(
            project_instance,
            update_active_modes=update_active_modes,
            update_active_tools=update_active_tools,
            record_explicit=record_explicit,
        )

    def get_active_tool_names(self) -> list[str]:
        """
        :return: the list of names of the active tools for the current project, sorted alphabetically
        """
        return self._calling_active_tools().tool_names

    def get_active_tools(self) -> AvailableTools:
        """
        :return: the set of active tools
        """
        return self._calling_active_tools()

    def tool_is_active(self, tool_name: str) -> bool:
        """
        :param tool_class: the name of the tool to check
        :return: True if the tool is active, False otherwise
        """
        return self._calling_active_tools().contains_tool_name(tool_name)

    def is_tool_function_available(self, tool_class: type[Tool]) -> bool:
        """
        Checks whether the functionality offered by a tool is available - either through the tool
        itself being enabled or through the corresponding function being exposed in the REPL.

        :param tool_class: the tool class
        :return: whether the function is available
        """
        is_active_tool = self._calling_active_tools().contains_tool_class(tool_class)
        if self._agent_interface == AgentInterface.TOOLS:
            return is_active_tool
        elif self._agent_interface == AgentInterface.REPL:
            return is_active_tool or self.get_repl().entrypoint.is_tool_function_available(tool_class)
        else:
            raise NotImplementedError

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
        result_str += f"Agent interface: {self._agent_interface.value}\n"
        result_str += f"Language backend: {self._language_backend.get_key()}"
        if self._active_project and self._active_project.project_config.language_backend is not None:
            result_str += " (project override)"
        result_str += f" (global default: {self.serena_config.language_backend.get_key()})\n"
        if self._active_project:
            result_str += self._language_backend.get_config_overview_statement(self._active_project)
        result_str += "Available projects:\n" + "\n".join(list(self.serena_config.project_names)) + "\n"
        result_str += f"Active context: {self._context.name}\n"

        # Active modes
        active_mode_names = self.get_active_modes().get_mode_names()
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
        Tears down the active project's language server manager and rebuilds it from scratch.

        This is the explicit reset path -- callers (e.g. :class:`RestartLanguageServerTool`,
        :meth:`add_language`, :meth:`remove_language`) want a fresh manager regardless of
        whether one already exists. Use :meth:`Project.create_language_server_manager` (without
        ``force_recreate``) when you only need to ensure a manager is present.
        """
        self.get_active_project_or_raise().create_language_server_manager(force_recreate=True)

    def add_language_server(self, ls_id: LanguageServerIdLike) -> None:
        """
        Adds a new language server to the active project, spawning the respective language server and updating the project configuration.
        The addition is executed synchronously via :meth:`execute_task`, i.e. the method returns when the addition is complete.

        :param ls_id: the language server to add
        """
        self.execute_task(lambda: self.get_active_project_or_raise().add_language_server(ls_id), name=f"AddLanguage:{ls_id.get_key()}")

    def remove_language_server(self, ls_id: LanguageServerIdLike) -> None:
        """
        Removes a language server from the active project, shutting down the respective server and updating the project configuration.
        The removal is executed synchronously via :meth:`execute_task`, i.e. the method returns when the removal is complete.

        :param ls_id: the language to remove
        """
        self.execute_task(
            lambda: self.get_active_project_or_raise().remove_language_server(ls_id), name=f"RemoveLanguage:{ls_id.get_key()}"
        )

    def get_tool(self, tool_class: type[TTool]) -> TTool:
        return cast(TTool, self._all_tools[tool_class])

    def print_tool_overview(self) -> None:
        ToolRegistry().print_tool_overview(self._calling_active_tools().tools)

    def __del__(self) -> None:
        is_object_initialised = "_active_projects_by_session" in self.__dict__
        if is_object_initialised:
            self.on_shutdown()

    def on_shutdown(self, timeout: float = 2.0) -> None:
        """
        Shutdown handler of the agent, freeing resources and stopping background tasks.

        Tears down every project held by any session (the per-session map plus the legacy single
        slot), deduplicating in case multiple sessions share the same Project instance.
        """
        log.info("SerenaAgent is shutting down ...")
        # apply shutdown depending on allocated resources, handling quick ones first (dashboard manager & GUI viewer)
        if self._gui_log_viewer:
            log.info("Stopping the GUI log window ...")
            self._gui_log_viewer.stop()
            self._gui_log_viewer = None
        if self._dashboard_manager:
            self._dashboard_manager.shutdown()
            self._dashboard_manager = None
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
        self._explicit_project_roots_by_session.clear()
        # deliberately NOT clearing _explicit_project_root_store: shutdown is the event the durable
        # record exists to survive. Clearing it here would restore the pre-fix behaviour where a
        # restarted daemon cannot tell a session that chose a project from one that never did.
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

    def get_active_language_server_ids(self) -> list[LanguageServerIdLike]:
        ls_manager = self.get_language_server_manager()
        if ls_manager is None:
            return []
        return ls_manager.get_active_language_server_ids()

    def get_unavailable_lsp_languages(self) -> dict[LanguageServerIdLike, Exception]:
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
