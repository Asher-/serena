"""The tools a desktop-app session exposes, with their required parameters, are those of main's deployed daemon.

The record is fact://serena/upstream_merge/baseline/d69e440a: the daemon deployed at d69e440a, context desktop-app, modes
editing and interactive. That daemon reads ~/.serena/serena_config.yml, since brain's launchd/ai.strong.serena.plist sets
HOME to the user's home, and the excluded_tools of that file name the six memory tools of _EXCLUDED_MEMORY_TOOLS.
"""

import pytest

from serena.agent import SerenaAgent
from serena.config.context_mode import SerenaAgentContext
from serena.config.serena_config import SerenaConfig
from serena.mcp import SerenaMCPFactory

_RECORDED = {
    "activate_project": ["project"],
    "check_onboarding_performed": [],
    "create_text_file": ["relative_path"],
    "cursor_close": ["cursor_id"],
    "cursor_configure": ["cursor_id"],
    "cursor_find": ["name_path_pattern", "because"],
    "cursor_grep": ["substring_pattern", "because"],
    "cursor_history": ["cursor_id"],
    "cursor_insert_after": ["cursor_id", "body", "expect_version"],
    "cursor_insert_at_end": ["cursor_id", "body", "expect_version"],
    "cursor_insert_at_start": ["cursor_id", "body", "expect_version"],
    "cursor_insert_before": ["cursor_id", "body", "expect_version"],
    "cursor_look": ["cursor_id"],
    "cursor_move": ["cursor_id", "target_name", "because"],
    "cursor_narrate": ["cursor_id", "because"],
    "cursor_overview": ["relative_path", "because"],
    "cursor_remove_member": ["cursor_id", "expect_version"],
    "cursor_rename": ["cursor_id", "new_name"],
    "cursor_replace_between": ["relative_path", "before_symbol", "after_symbol", "body", "expect_version"],
    "cursor_replace_body": ["cursor_id", "body", "expect_version"],
    "cursor_replace_range": ["relative_path", "start_line", "end_line", "body", "expect_version"],
    "cursor_replace_range_verified": ["relative_path", "start_line", "end_line", "expected_content", "body", "expect_version"],
    "cursor_start": ["name_path", "because"],
    "delete_file": ["relative_path"],
    "find_file": ["pattern"],
    "get_current_config": [],
    "get_language_server_status": [],
    "initial_instructions": [],
    "list_dir": [],
    "onboarding": [],
    "rename_file": ["source", "destination"],
    "restart_language_server": [],
    "safe_delete_symbol": ["name_path_pattern", "relative_path"],
    "search_for_pattern": ["substring_pattern"],
    "stat": ["relative_path"],
}
"""each tool's name and its required parameters, those named ``_serena*`` left out, as the deployed daemon listed them"""

_EXCLUDED_MEMORY_TOOLS = ["read_memory", "write_memory", "list_memories", "delete_memory", "rename_memory", "edit_memory"]
"""the excluded_tools of ~/.serena/serena_config.yml, the configuration the deployed daemon reads"""


@pytest.fixture
def agent():
    config = SerenaConfig(gui_log_window=False, web_dashboard=False, excluded_tools=_EXCLUDED_MEMORY_TOOLS)
    serena_agent = SerenaAgent(serena_config=config, context=SerenaAgentContext.load("desktop-app"))
    yield serena_agent
    serena_agent.on_shutdown(timeout=0.5)


def test_a_desktop_app_session_exposes_the_recorded_tools_with_their_required_parameters(agent: SerenaAgent) -> None:
    exposed = {}
    for tool in agent.get_exposed_tool_instances():
        parameters = SerenaMCPFactory.make_mcp_tool(tool).parameters
        exposed[tool.get_name()] = [name for name in parameters.get("required", []) if not name.startswith("_serena")]

    assert sorted(mode.name for mode in agent.get_active_modes().get_modes()) == ["editing", "interactive"]
    assert exposed == _RECORDED


@pytest.mark.parametrize("tool_name", sorted(_RECORDED))
def test_each_recorded_tool_is_active_for_a_session_holding_no_project(agent: SerenaAgent, tool_name: str) -> None:
    assert agent.tool_is_active(tool_name)
