"""Tests that a tool parameter with a default is optional, both in the published schema and in the runtime validator."""

import inspect
import logging

import pytest

from serena.config.serena_config import SerenaConfig
from serena.mcp import SerenaMCPFactory
from serena.tools.cursor_tools import CursorGrepTool
from serena.tools.tools_base import Tool, ToolRegistry


@pytest.fixture(scope="module")
def published_tools() -> list[Tool]:
    cfg = SerenaConfig(log_level=logging.ERROR).with_headless_mode_overrides()
    cfg.included_optional_tools = tuple(ToolRegistry().get_tool_names_optional())
    factory = SerenaMCPFactory(transport="stdio", context="desktop-app")
    factory.agent = factory._create_serena_agent(cfg)
    return list(factory._iter_tools())


def test_no_tool_schema_requires_a_defaulted_parameter(published_tools: list[Tool]) -> None:
    issues = []
    for tool in published_tools:
        required = set(SerenaMCPFactory.make_mcp_tool(tool).parameters.get("required", []))
        defaulted = {name for name, p in inspect.signature(tool.apply).parameters.items() if p.default is not inspect.Parameter.empty}
        issues.extend(f"{tool.get_name()}.{name}" for name in sorted(required & defaulted))

    assert issues == [], f"defaulted parameters published as required: {issues}"


def test_cursor_grep_call_omitting_defaulted_parameters_is_validated_with_their_defaults(published_tools: list[Tool]) -> None:
    grep = next(tool for tool in published_tools if isinstance(tool, CursorGrepTool))
    published = SerenaMCPFactory.make_mcp_tool(grep)

    arguments = published.fn_metadata.arg_model.model_validate({"substring_pattern": "x", "because": "y"})

    assert arguments.paths_include_glob == ""
    assert arguments.paths_exclude_glob == ""
    assert arguments.max_matches == 20
    assert arguments.max_answer_chars == -1
