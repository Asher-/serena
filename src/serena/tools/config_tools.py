from serena.tools import Tool, ToolMarkerDoesNotRequireActiveProject, ToolMarkerOptional


class GetLanguageServerStatusTool(Tool, ToolMarkerOptional, ToolMarkerDoesNotRequireActiveProject):
    """
    Reports a JSON snapshot of the active project's per-language language-server state,
    mirroring the dashboard's ``/get_language_server_status`` response. Useful for checking
    which LSPs actually started (and which failed) without having to reactivate the project.
    """

    def apply(self) -> str:
        """
        Returns a JSON object with two keys:
          * ``active``: sorted list of languages (by :class:`Language` ``.value``) whose servers
            are currently running in the manager for the active project.
          * ``unavailable``: mapping from language value to a short textual description of the
            exception captured when that language's server failed to start or restart.

        Both collections are empty when there is no active project or no language-server manager
        has been constructed yet.
        """
        active = sorted(lang.value for lang in self.agent.get_active_lsp_languages())
        unavailable = {lang.value: str(exc) for lang, exc in self.agent.get_unavailable_lsp_languages().items()}
        return self._to_json({"active": active, "unavailable": unavailable})


class OpenDashboardTool(Tool, ToolMarkerOptional, ToolMarkerDoesNotRequireActiveProject):
    """
    Opens the Serena web dashboard in the default web browser.
    The dashboard provides logs, session information, and tool usage statistics.
    """

    def apply(self) -> str:
        """
        Opens the Serena web dashboard in the default web browser.
        """
        if self.agent.open_dashboard():
            return f"Serena web dashboard has been opened in the user's default web browser: {self.agent.get_dashboard_url()}"
        else:
            return f"Serena web dashboard could not be opened automatically; tell the user to open it via {self.agent.get_dashboard_url()}"


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
        if not is_new_activation:
            result = "Project was already active."
        else:
            result = self.agent.get_project_activation_message()
        result += "\nIMPORTANT: If you have not yet read the 'Serena Instructions Manual', do it now before continuing!"
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


class GetCurrentConfigTool(Tool):
    """
    Prints the current configuration of the agent, including the active and available projects, tools, contexts, and modes.
    """

    def apply(self) -> str:
        """
        Print the current configuration of the agent, including the active and available projects, tools, contexts, and modes.
        """
        return self.agent.get_current_config_overview()
