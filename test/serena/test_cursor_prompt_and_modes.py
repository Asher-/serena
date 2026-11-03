"""The cursor surface within upstream's modes and system prompt: planning exposes no editing tool, and the prompt describes the cursor."""

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ModeSelectionDefinition, SerenaConfig
from serena.generated.tool_capabilities import EDIT_CAPABLE_TOOL_NAMES

_CURSOR_WRITERS = [
    "cursor_insert_after",
    "cursor_insert_at_end",
    "cursor_insert_at_start",
    "cursor_insert_before",
    "cursor_remove_member",
    "cursor_rename",
    "cursor_replace_between",
    "cursor_replace_body",
    "cursor_replace_range",
    "cursor_replace_range_verified",
]


def _agent(modes: list[str] | None = None) -> SerenaAgent:
    selection = ModeSelectionDefinition(default_modes=modes) if modes is not None else None
    return SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False), modes=selection)


@pytest.fixture
def planning_agent():
    agent = _agent(["planning"])
    yield agent
    agent.on_shutdown(timeout=0.5)


def test_planning_mode_exposes_no_edit_capable_tool(planning_agent: SerenaAgent) -> None:
    assert "planning" in [mode.name for mode in planning_agent.get_active_modes().get_modes()]
    active = set(planning_agent.get_active_tool_names())
    assert "cursor_start" in active
    assert active & EDIT_CAPABLE_TOOL_NAMES == set()
    assert active & set(_CURSOR_WRITERS) == set()


def test_the_system_prompt_with_cursor_start_available_carries_the_cursor_block() -> None:
    agent = _agent()
    try:
        assert "cursor_start" in agent.get_active_tool_names()
        prompt = agent.create_system_prompt()
    finally:
        agent.on_shutdown(timeout=0.5)

    assert "The cursor is the primary interface for symbol-level activity" in prompt
    assert "`cursor_start`" in prompt and "`cursor_find`" in prompt
