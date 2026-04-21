"""
Cross-language probe: does the LSP report a full-statement extent for multi-line
variable assignments, or an identifier-only extent (Python-style)?

Each probe creates a sandbox file containing a multi-line collection literal in
the target language's ``test_repo``, positions a cursor on the variable by name,
configures ``include_body=True`` via :class:`CursorConfigureTool`, and asserts
that the rendered body spans the full assignment statement (including the
closing bracket/brace line).

If a probe fails for a language, that language's LSP reports an identifier-only
extent and a per-language :class:`SymbolExtentStrategy` must be added (see
:mod:`serena.symbol_extent`). If all probes pass, cross-language body widening
is a no-op on these LSPs and Python-only widening is the correct final scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig
from serena.project import Project
from serena.tools.cursor_tools import CursorConfigureTool, CursorStartTool
from solidlsp.ls_config import Language
from test.conftest import get_repo_path, language_tests_enabled


def _build_agent(language: Language, project_name: str) -> SerenaAgent:
    """Construct a SerenaAgent pointing at ``language``'s ``test_repo``."""
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    repo_path = get_repo_path(language)
    project = Project(
        project_root=str(repo_path),
        project_config=ProjectConfig(
            project_name=project_name,
            languages=[language],
            ignored_paths=[],
            excluded_tools=[],
            read_only=False,
            ignore_all_files_in_gitignore=True,
            initial_prompt="",
            encoding="utf-8",
        ),
        serena_config=config,
    )
    config.projects = [RegisteredProject.from_project_instance(project)]
    agent = SerenaAgent(project=project_name, serena_config=config)
    agent.execute_task(lambda: None)
    return agent


def _assert_body_spans_full_literal(view: str, identifier: str, close_char: str) -> None:
    """Assert the ``include_body`` view contains the full multi-line literal.

    :param view: the rendered cursor view text.
    :param identifier: the variable identifier expected in the body.
    :param close_char: the literal's closing character (``]`` or ``}``).
    """
    assert "--- body ---" in view, f"Body delimiter missing from view:\n{view}"
    assert identifier in view, f"Identifier '{identifier}' missing from view:\n{view}"
    assert "1," in view, f"First literal element missing from view:\n{view}"
    assert "2," in view, f"Second literal element missing from view:\n{view}"
    assert "3," in view, f"Third literal element missing from view:\n{view}"

    body_start = view.index("--- body ---")
    body_end = view.index("--- end body ---")
    body_block = view[body_start:body_end]
    assert close_char in body_block, (
        f"Closing '{close_char}' missing from body block (LSP reports identifier-only "
        f"extent -- per-language widening strategy needed):\n{body_block}"
    )


# --- TypeScript ---------------------------------------------------------------

@pytest.fixture(scope="module")
def typescript_serena_agent() -> Iterator[SerenaAgent]:
    """SerenaAgent configured for the TypeScript test repo."""
    if not language_tests_enabled(Language.TYPESCRIPT):
        pytest.skip("TypeScript tests not enabled")
    agent = _build_agent(Language.TYPESCRIPT, "test_repo_typescript_body_probe")
    try:
        yield agent
    finally:
        agent.on_shutdown(timeout=5)


@pytest.mark.typescript
def test_typescript_multiline_array_literal_body(typescript_serena_agent: SerenaAgent) -> None:
    """Probe: does the TypeScript LSP report full-statement extent for a multi-line const array?"""
    start_tool = typescript_serena_agent.get_tool(CursorStartTool)
    configure_tool = typescript_serena_agent.get_tool(CursorConfigureTool)

    # fixture: a multi-line const with array literal
    project_root = Path(typescript_serena_agent.get_active_project_or_raise().project_root)
    rel_path = "_cursor_variable_body_widening_sandbox.ts"
    abs_path = project_root / rel_path
    abs_path.write_text(
        "export const FOO = [\n"
        "    1,\n"
        "    2,\n"
        "    3,\n"
        "];\n"
    )

    try:
        typescript_serena_agent.reset_language_server_manager()
        start_tool.apply(
            name_path="FOO",
            relative_path=rel_path,
            cursor_id="ts-var-body",
        )
        view = configure_tool.apply(
            cursor_id="ts-var-body",
            include_body=True,
        )
        _assert_body_spans_full_literal(view, identifier="FOO", close_char="]")
    finally:
        if abs_path.exists():
            abs_path.unlink()
        try:
            typescript_serena_agent.reset_language_server_manager()
        except Exception:
            pass


# --- Go -----------------------------------------------------------------------

@pytest.fixture(scope="module")
def go_serena_agent() -> Iterator[SerenaAgent]:
    """SerenaAgent configured for the Go test repo."""
    if not language_tests_enabled(Language.GO):
        pytest.skip("Go tests not enabled")
    agent = _build_agent(Language.GO, "test_repo_go_body_probe")
    try:
        yield agent
    finally:
        agent.on_shutdown(timeout=5)


@pytest.mark.go
def test_go_multiline_slice_literal_body(go_serena_agent: SerenaAgent) -> None:
    """Probe: does the Go LSP report full-statement extent for a multi-line package-level slice var?"""
    start_tool = go_serena_agent.get_tool(CursorStartTool)
    configure_tool = go_serena_agent.get_tool(CursorConfigureTool)

    # fixture: a package-level var with a multi-line slice literal (same package as main.go)
    project_root = Path(go_serena_agent.get_active_project_or_raise().project_root)
    rel_path = "cursor_variable_body_widening_sandbox.go"
    abs_path = project_root / rel_path
    abs_path.write_text(
        "package main\n"
        "\n"
        "var FOO = []int{\n"
        "    1,\n"
        "    2,\n"
        "    3,\n"
        "}\n"
    )

    try:
        go_serena_agent.reset_language_server_manager()
        start_tool.apply(
            name_path="FOO",
            relative_path=rel_path,
            cursor_id="go-var-body",
        )
        view = configure_tool.apply(
            cursor_id="go-var-body",
            include_body=True,
        )
        _assert_body_spans_full_literal(view, identifier="FOO", close_char="}")
    finally:
        if abs_path.exists():
            abs_path.unlink()
        try:
            go_serena_agent.reset_language_server_manager()
        except Exception:
            pass


# --- Swift --------------------------------------------------------------------

@pytest.fixture(scope="module")
def swift_serena_agent() -> Iterator[SerenaAgent]:
    """SerenaAgent configured for the Swift test repo."""
    if not language_tests_enabled(Language.SWIFT):
        pytest.skip("Swift tests not enabled")
    agent = _build_agent(Language.SWIFT, "test_repo_swift_body_probe")
    try:
        yield agent
    finally:
        agent.on_shutdown(timeout=5)


@pytest.mark.swift
def test_swift_multiline_array_literal_body(swift_serena_agent: SerenaAgent) -> None:
    """Probe: does the Swift LSP report full-statement extent for a multi-line let with array literal?"""
    start_tool = swift_serena_agent.get_tool(CursorStartTool)
    configure_tool = swift_serena_agent.get_tool(CursorConfigureTool)

    # fixture: a top-level let under src/ (where Package.swift sources live)
    project_root = Path(swift_serena_agent.get_active_project_or_raise().project_root)
    rel_path = "src/CursorVariableBodyWideningSandbox.swift"
    abs_path = project_root / rel_path
    abs_path.write_text(
        "let FOO: [Int] = [\n"
        "    1,\n"
        "    2,\n"
        "    3,\n"
        "]\n"
    )

    try:
        swift_serena_agent.reset_language_server_manager()
        start_tool.apply(
            name_path="FOO",
            relative_path=rel_path,
            cursor_id="swift-var-body",
        )
        view = configure_tool.apply(
            cursor_id="swift-var-body",
            include_body=True,
        )
        _assert_body_spans_full_literal(view, identifier="FOO", close_char="]")
    finally:
        if abs_path.exists():
            abs_path.unlink()
        try:
            swift_serena_agent.reset_language_server_manager()
        except Exception:
            pass
