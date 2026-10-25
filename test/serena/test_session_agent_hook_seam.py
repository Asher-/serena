"""Tests composing brain's hooks/carry-serena-agent-id.sh with the MCP tool serena publishes.

These pin ``pg://serena/session/project/agent`` across the seam it depends on (task://session_agent.4). Claude Code
gives a PreToolUse hook the calling subagent's agent_id and sends no agent identity to serena, so the hook adds the
reserved argument ``_serena_agent_id`` to the tool input, and the tool serena publishes takes it out of the raw
arguments and keys the call's session on it. Every call below runs the real hook on the call's PreToolUse input and
passes the tool input the hook answers with as the raw arguments of the published tool.

The hook is read from the path ``SERENA_CARRY_AGENT_ID_HOOK`` names, or else from the path brain's config/settings.json
registers; the module is skipped, naming that path, when no hook is there.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from serena.agent import SerenaAgent
from serena.mcp import SerenaMCPFactory
from serena.project import Project
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject
from test.serena.test_session_agent_separation import _CLIENT_SESSION, _make_roots, _PublishedRelativeWriteTool
from test.serena.test_session_agent_separation import agent as _agent_fixture
from test.serena.test_session_project_binding import (
    _active,
    _no_pipe_session,  # noqa: F401 -- autouse fixture, imported so it applies to this module
    _project_at,
)
from test.serena.test_streamable_http_cc_session_key import _make_mcp_ctx

agent = _agent_fixture
"""the fixture of the binding tests, bound here under its own name so pytest serves it to this module's tests"""

_HOOK = Path(os.environ.get("SERENA_CARRY_AGENT_ID_HOOK", "~/Dropbox/Projects/claude/brain/hooks/carry-serena-agent-id.sh")).expanduser()
"""brain's PreToolUse hook that carries a subagent's agent_id into a serena tool call"""

_TOOL_NAME = "mcp____serena__{}"
"""the name a serena tool reaches the hook under through the brain multiplexer"""

pytestmark = pytest.mark.skipif(not _HOOK.is_file(), reason=f"no hook at {_HOOK}; set SERENA_CARRY_AGENT_ID_HOOK")


class _PublishedActivationTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """
    An activation documented as a published MCP tool must be, binding a given project to the calling session's slot.
    """

    def __init__(self, agent: SerenaAgent, project: Project) -> None:
        super().__init__(agent)
        self._project = project

    def apply(self) -> str:
        """
        Binds the tool's project to the calling session.
        """
        self.agent._active_project = self._project
        return "activated"


def _through_hook(tool_name: str, tool_input: dict[str, Any], agent_id: str | None) -> tuple[dict[str, Any], str]:
    """
    Runs brain's hook on the PreToolUse input Claude Code builds for a call, which carries agent_id and agent_type
    for a subagent's call and neither for the main conversation's.

    :return: the tool input the call reaches serena with, and what the hook printed
    """
    hook_input: dict[str, Any] = {"session_id": "cc-hook-session", "hook_event_name": "PreToolUse", "tool_name": tool_name}
    hook_input["tool_input"] = tool_input
    if agent_id is not None:
        hook_input |= {"agent_id": agent_id, "agent_type": "general-purpose"}
    printed = subprocess.run(["bash", str(_HOOK)], input=json.dumps(hook_input), capture_output=True, text=True, check=True).stdout.strip()
    if not printed:
        return tool_input, printed
    return json.loads(printed)["hookSpecificOutput"]["updatedInput"], printed


def _activate_through_hook(agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, root: Path, agent_id: str | None) -> None:
    """
    Activates a project at ``root`` through the hook and the published tool, as the caller ``agent_id`` names.
    """
    activation = SerenaMCPFactory.make_mcp_tool(
        _active(_PublishedActivationTool(agent, _project_at(root, f"project-{root.name}")), monkeypatch)
    )
    arguments, printed = _through_hook(_TOOL_NAME.format(activation.name), {}, agent_id)
    result = asyncio.run(activation.run(arguments, context=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION)))
    assert result == "activated", (
        f"the activation of {root.name} by {agent_id or 'the main conversation'} answered {result!r} (hook: {printed!r})"
    )


class TestHookCarriesAgentIdIntoPublishedTool:
    """pg://serena/session/project/agent across brain's hook and the tool serena publishes"""

    def test_subagent_write_through_hook_lands_under_own_project(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Agents A1 and A2 activate P1 and P2 through the hook, and A1's relative-path write through the hook lands under P1."""
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        for agent_id, root in (("A1", root_1), ("A2", root_2)):
            _activate_through_hook(agent, monkeypatch, root, agent_id)

        # agent A1 writes by relative path, its call answered by the hook before it reaches serena
        write = SerenaMCPFactory.make_mcp_tool(_active(_PublishedRelativeWriteTool(agent), monkeypatch))
        arguments, printed = _through_hook(_TOOL_NAME.format(write.name), {"relative_path": "out.txt"}, "A1")
        assert arguments == {"relative_path": "out.txt", "_serena_agent_id": "A1"}, f"the hook answered {printed!r}"
        result = asyncio.run(write.run(arguments, context=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION)))

        assert result == str(root_1), f"agent A1's write through the hook ran against {result!r}"
        assert (root_1 / "out.txt").is_file(), "agent A1's write through the hook did not land under its own project P1"
        assert not (root_2 / "out.txt").exists(), "agent A1's write through the hook landed under agent A2's project P2"

    def test_main_conversation_call_through_hook_is_answered_from_forwarded_session_slot(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The main conversation activates P0 and agent A1 then activates P1, both through the hook; the main
        conversation's write gets no hook output and lands under P0, from the forwarded session id's own slot.
        """
        root_0, root_1 = _make_roots(tmp_path, "P0", "P1")
        for agent_id, root in ((None, root_0), ("A1", root_1)):
            _activate_through_hook(agent, monkeypatch, root, agent_id)

        # the main conversation writes by relative path; the hook leaves its input unchanged
        write = SerenaMCPFactory.make_mcp_tool(_active(_PublishedRelativeWriteTool(agent), monkeypatch))
        arguments, printed = _through_hook(_TOOL_NAME.format(write.name), {"relative_path": "out.txt"}, None)
        assert printed == "", f"the hook answered the main conversation's call with {printed!r}"
        result = asyncio.run(write.run(arguments, context=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION)))

        assert result == str(root_0), f"the main conversation's write through the hook ran against {result!r}"
        assert (root_0 / "out.txt").is_file(), "the main conversation's write did not land under its own project P0"
        assert not (root_1 / "out.txt").exists(), "the main conversation's write landed under agent A1's project P1"
