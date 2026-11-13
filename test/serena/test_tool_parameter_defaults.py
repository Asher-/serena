"""Tests that a tool parameter with a default is optional, both in the published schema and in the runtime validator."""

import asyncio
import inspect

from serena.agent import SerenaAgent
from serena.mcp import SerenaMCPFactory
from serena.tools.config_tools import ActivateProjectTool
from serena.tools.cursor_tools import CursorGrepTool
from serena.tools.tools_base import ToolRegistry
from test.serena.test_streamable_http_cc_session_key import _make_mcp_ctx

_CLIENT_SESSION = "parameter-defaults"
"""the forwarded session id the published calls of the test arrive under"""


def test_no_tool_schema_requires_a_defaulted_parameter(python_serena_agent: SerenaAgent) -> None:
    issues = []
    for tool_class in ToolRegistry().get_all_tool_classes():
        tool = python_serena_agent.get_tool(tool_class)
        required = set(SerenaMCPFactory.make_mcp_tool(tool).parameters.get("required", []))
        defaulted = {name for name, p in inspect.signature(tool.apply).parameters.items() if p.default is not inspect.Parameter.empty}
        issues.extend(f"{tool.get_name()}.{name}" for name in sorted(required & defaulted))

    assert issues == [], f"defaulted parameters published as required: {issues}"


def test_cursor_grep_call_omitting_defaulted_parameters_is_answered(python_serena_agent: SerenaAgent) -> None:
    root = python_serena_agent.get_active_project_or_raise().project_root

    # the session's project, bound through the published activate_project
    activation = SerenaMCPFactory.make_mcp_tool(python_serena_agent.get_tool(ActivateProjectTool))
    asyncio.run(activation.run({"project": root}, context=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION)))

    # a call omitting paths_include_glob, paths_exclude_glob, max_matches and max_answer_chars
    grep = SerenaMCPFactory.make_mcp_tool(python_serena_agent.get_tool(CursorGrepTool))
    answer = asyncio.run(
        grep.run(
            {"substring_pattern": "def ", "because": "test fixture: exercising the tool behaviour"},
            context=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION),
        )
    )

    assert "match(es)" in answer, f"a cursor_grep call omitting its defaulted parameters answered {answer!r}"
