# SPDX-License-Identifier: GPL-3.0-or-later

from typing import TYPE_CHECKING, cast

from sensai.util.helper import mark_used

from serena.tools import Tool, ToolMarkerDoesNotRequireActiveProject, ToolMarkerOptional

if TYPE_CHECKING:
    from serena.repl.api.cfg_api import ConfigApi


class ConfigApiMixin:
    """
    Mixin for tools which delegate to the configuration API.
    The API is imported locally, since the API module refers to the tools (as corresponding tools).
    """

    def _api(self) -> "ConfigApi":
        from serena.repl.api.cfg_api import ConfigApi

        tool = cast(Tool, cast(object, self))
        return ConfigApi(tool.agent)


class GetLanguageServerStatusTool(Tool, ToolMarkerOptional, ToolMarkerDoesNotRequireActiveProject):
    """
    Reports a JSON snapshot of the active project's per-language language-server state,
    mirroring the dashboard's ``/get_language_server_status`` response. Useful for checking
    which LSPs actually started (and which failed) without having to reactivate the project.
    """

    def apply(self) -> str:
        """
        Returns a JSON object with two keys:
          * ``active``: sorted list of language servers (by key) whose servers
            are currently running in the manager for the active project.
          * ``unavailable``: mapping from language server key to a short textual description of the
            exception captured when that language's server failed to start or restart.

        Both collections are empty when there is no active project or no language-server manager
        has been constructed yet.
        """
        active = sorted(ls_id.get_key() for ls_id in self.agent.get_active_language_server_ids())
        unavailable = {ls_id.get_key(): str(exc) for ls_id, exc in self.agent.get_unavailable_lsp_languages().items()}
        return self._to_json({"active": active, "unavailable": unavailable})


class OpenDashboardTool(Tool, ToolMarkerOptional, ToolMarkerDoesNotRequireActiveProject, ConfigApiMixin):
    """
    Opens the Serena web dashboard in the default web browser.
    The dashboard provides logs, session information, and tool usage statistics.
    """

    def apply(self) -> str:
        """
        Opens the Serena web dashboard in the default web browser.
        """
        return self._api().open_dashboard()


class ActivateProjectTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """
    Activates a project based on the project name or path.
    """

    def apply(self, project: str) -> str:
        """
        Activates the project with the given name or path.

        :param project: the name of a registered project to activate or a path to a project directory
        """
        is_new_activation = self.agent.activate_project_from_path_or_name(project)
        mark_used(is_new_activation)
        result = self.agent.get_project_activation_message(self.agent.get_calling_session_id())
        return result


class RemoveProjectTool(Tool, ToolMarkerDoesNotRequireActiveProject, ToolMarkerOptional):
    """
    Removes a project from the Serena configuration.
    """

    def apply(self, project_name: str) -> str:
        """
        Removes a project from the Serena configuration.

        :param project_name: Name of the project to remove
        """
        self.agent.serena_config.remove_project(project_name)
        return f"Successfully removed project '{project_name}' from configuration."


class GetCurrentConfigTool(Tool, ConfigApiMixin):
    """
    Prints the current configuration of the agent, including the active and available projects, tools, contexts, and modes.
    """

    def apply(self) -> str:
        """
        Print the current configuration of the agent, including the active and available projects, tools, contexts, and modes.
        """
        return self._api().get_current_config()
