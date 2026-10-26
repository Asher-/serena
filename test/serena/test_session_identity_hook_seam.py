"""Tests composing brain's hooks/carry-serena-agent-id.sh with the MCP tool serena publishes, across Claude Code sessions.

These pin ``pg://serena/session/project/separation`` across the seam it depends on (task://session_identity.3). The
Claude desktop app sends the main-thread serena calls of more than one of its sessions through one connection, so they
arrive under one forwarded session id. The hook adds the reserved argument ``_serena_session_id``, carrying the calling
session's own session_id, to the tool input, and ``_serena_agent_id`` with it for a subagent's call; the tool serena
publishes takes both out of the raw arguments and keys the call's session on them. Every call below runs the real hook
on the call's PreToolUse input and passes the tool input the hook answers with as the raw arguments of the published
tool.

The hook is read from the path ``SERENA_CARRY_AGENT_ID_HOOK`` names, or else from the path brain's config/settings.json
registers; the module is skipped, naming that path, when no hook is there.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from serena.agent import SerenaAgent
from serena.mcp import SerenaMCPFactory
from test.serena.test_session_agent_hook_seam import _HOOK, _TOOL_NAME, _PublishedActivationTool
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

pytestmark = pytest.mark.skipif(not _HOOK.is_file(), reason=f"no hook at {_HOOK}; set SERENA_CARRY_AGENT_ID_HOOK")


def _through_hook(tool_name: str, tool_input: dict[str, Any], session_id: str, agent_id: str | None) -> tuple[dict[str, Any], str]:
    """
    Runs brain's hook on the PreToolUse input Claude Code builds for a call of the session ``session_id``, which carries
    agent_id and agent_type for a subagent's call and neither for the session's main conversation's.

    :return: the tool input the call reaches serena with, and what the hook printed
    """
    hook_input: dict[str, Any] = {"session_id": session_id, "hook_event_name": "PreToolUse", "tool_name": tool_name}
    hook_input["tool_input"] = tool_input
    if agent_id is not None:
        hook_input |= {"agent_id": agent_id, "agent_type": "general-purpose"}
    printed = subprocess.run(["bash", str(_HOOK)], input=json.dumps(hook_input), capture_output=True, text=True, check=True).stdout.strip()
    if not printed:
        return tool_input, printed
    return json.loads(printed)["hookSpecificOutput"]["updatedInput"], printed


def _activate_through_hook(
    agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, root: Path, session_id: str, agent_id: str | None = None
) -> None:
    """
    Activates a project at ``root`` through the hook and the published tool, as the caller ``session_id`` and
    ``agent_id`` name.
    """
    activation = SerenaMCPFactory.make_mcp_tool(
        _active(_PublishedActivationTool(agent, _project_at(root, f"project-{root.name}")), monkeypatch)
    )
    arguments, printed = _through_hook(_TOOL_NAME.format(activation.name), {}, session_id, agent_id)
    result = asyncio.run(activation.run(arguments, context=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION)))
    caller = f"session {session_id}" + (f" agent {agent_id}" if agent_id is not None else "")
    assert result == "activated", f"the activation of {root.name} by {caller} answered {result!r} (hook: {printed!r})"


def _write_through_hook(agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, session_id: str) -> tuple[str, str]:
    """
    Writes ``out.txt`` by relative path through the hook and the published tool, as the main conversation of the
    session ``session_id``.

    :return: the root the write ran against, and what the hook printed
    """
    write = SerenaMCPFactory.make_mcp_tool(_active(_PublishedRelativeWriteTool(agent), monkeypatch))
    arguments, printed = _through_hook(_TOOL_NAME.format(write.name), {"relative_path": "out.txt"}, session_id, None)
    return asyncio.run(write.run(arguments, context=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION))), printed


class TestHookCarriesSessionIdIntoPublishedTool:
    """pg://serena/session/project/separation across brain's hook and the tool serena publishes"""

    def test_session_write_through_hook_lands_under_own_project(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Sessions S1 and S2 activate P1 and P2 through the hook, and S1's relative-path write through the hook lands under P1."""
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        for session_id, root in (("S1", root_1), ("S2", root_2)):
            _activate_through_hook(agent, monkeypatch, root, session_id)

        # session S1 writes by relative path, its call answered by the hook before it reaches serena
        result, printed = _write_through_hook(agent, monkeypatch, "S1")

        assert result == str(root_1), f"session S1's write through the hook ran against {result!r} (hook: {printed!r})"
        assert (root_1 / "out.txt").is_file(), "session S1's write through the hook did not land under its own project P1"
        assert not (root_2 / "out.txt").exists(), "session S1's write through the hook landed under session S2's project P2"

    def test_subagent_activation_through_hook_leaves_its_sessions_project(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Session S1 activates P1 and its subagent A1 then activates P2, both through the hook, and S1's next call, a
        relative-path write through the hook, is answered from P1.
        """
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        _activate_through_hook(agent, monkeypatch, root_1, "S1")
        _activate_through_hook(agent, monkeypatch, root_2, "S1", "A1")

        # session S1 writes by relative path after its subagent's activation
        result, printed = _write_through_hook(agent, monkeypatch, "S1")

        assert result == str(root_1), f"session S1's write through the hook ran against {result!r} (hook: {printed!r})"
        assert (root_1 / "out.txt").is_file(), "session S1's write through the hook did not land under its own project P1"
        assert not (root_2 / "out.txt").exists(), "session S1's write through the hook landed under its subagent's project P2"
