"""
Tools supporting the general workflow of the agent
"""
# SPDX-License-Identifier: GPL-3.0-or-later

from serena.tools import ReadMemoryTool, Tool, ToolMarkerCanEdit, ToolMarkerDoesNotRequireActiveProject, ToolMarkerOptional, WriteMemoryTool
from serena.tools.memory_tools import MemoryApiMixin


class CheckOnboardingPerformedTool(Tool, MemoryApiMixin):
    """
    Checks whether project onboarding was already performed.
    """

    def apply(self) -> str:
        """
        Checks whether project onboarding was already performed.
        You should always call this tool before beginning to actually work on the project/after activating a project.
        """
        read_memory_tool_available = self.agent.is_tool_function_available(ReadMemoryTool)
        perform_onboarding_tool_available = self.agent.is_tool_function_available(OnboardingTool)

        if not read_memory_tool_available:
            return "Memory reading tool not activated, skipping onboarding check."
        project_memories = self._api().list_memories().memories
        if len(project_memories) == 0:
            msg = "Onboarding not performed yet (no memories available). "
            if perform_onboarding_tool_available:
                msg += "You should perform onboarding by calling the `onboarding` tool before proceeding with the task. "
        else:
            # Not reporting the list of memories here, as they were already reported at project activation
            # (with the system prompt if the project was activated at startup)
            msg = (
                f"Onboarding was already performed: {len(project_memories)} project memories are available. "
                "Consider reading memories if they appear relevant to the task at hand."
            )
        msg += " If you have not read the 'Serena Instructions Manual', do so now."
        return msg


class OnboardingTool(Tool, MemoryApiMixin, ToolMarkerCanEdit):
    """
    Performs onboarding (identifying the project structure and essential tasks, e.g. for testing or building).
    """

    def apply(self) -> str:
        """
        Call this tool if onboarding was not performed yet.
        You will call this tool at most once per conversation.

        :return: instructions on how to create the onboarding information
        """
        write_memory_tool_available = self.agent.is_tool_function_available(WriteMemoryTool)
        if not write_memory_tool_available:
            return "Memory writing tool not activated, skipping onboarding."
        return self._api().onboarding()


class InitialInstructionsTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """
    Provides instructions on Serena usage (i.e. the 'Serena Instructions Manual')
    for clients that do not read the initial instructions when the MCP server is connected.
    """

    def apply(self) -> str:
        """
        Provides the 'Serena Instructions Manual', which contains essential information on how to use the Serena toolbox,
        and the Serena session ID.
        """
        return self.agent.create_system_prompt()


class SerenaInfoTool(Tool, ToolMarkerOptional, ToolMarkerDoesNotRequireActiveProject):
    """
    Provides information about an advanced topic on demand, facilitating context-efficiency.
    """

    def apply(self, topic: str) -> str:
        """
        Retrieves Serena-specific information
        :param topic: the topic, which you must have been given explicitly
        """
        match topic:
            case "jet_brains_debug_repl":
                return self.agent.prompt_factory.create_info_jet_brains_debug_repl()
            case _:
                raise ValueError("Invalid topic: " + topic)
