"""
The cursor surface reaches upstream's modules only through the names it imports, and the structural package reaches the
serena and solidlsp packages only within itself and through the supervised starts (task://upstream_merge.15).
"""

import ast
import importlib
import os
from pathlib import Path

import pytest

from serena.agent import SerenaAgent

_SRC = Path(__file__).resolve().parents[2] / "src"
_CURSOR_MODULES = [_SRC / "serena" / "cursor.py", _SRC / "serena" / "tools" / "cursor_tools.py"]
_STRUCTURAL = _SRC / "solidlsp" / "structural"
_STRUCTURAL_SUPERVISION_MODULES = {"solidlsp.util.supervise", "solidlsp.ls_survival"}


def _is_source_module(name: str) -> bool:
    """:return: whether the dotted name is a module or package under src"""
    path = _SRC.joinpath(*name.split("."))
    return path.with_suffix(".py").is_file() or path.is_dir()


def _imported_modules(path: Path) -> set[str]:
    """
    :return: every module the file imports, at any depth of its code, relative imports resolved within its package;
        ``from A import B`` imports the module A.B where B is a module under src, else the module A
    """
    package = ".".join(path.relative_to(_SRC).with_suffix("").parts[:-1])
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                origin = ".".join([*base, node.module] if node.module else base)
            else:
                origin = node.module or ""
            for alias in node.names:
                submodule = f"{origin}.{alias.name}"
                modules.add(submodule if _is_source_module(submodule) else origin)
    return modules


def test_both_cursor_modules_import_and_name_their_tools() -> None:
    cursor = importlib.import_module("serena.cursor")
    cursor_tools = importlib.import_module("serena.tools.cursor_tools")
    assert cursor.CursorManager is cursor_tools.CursorManager
    assert cursor_tools.CursorRenameTool.get_name_from_cls() == "cursor_rename"


@pytest.mark.parametrize("path", _CURSOR_MODULES, ids=lambda p: p.name)
def test_a_cursor_module_imports_no_configuration_or_dashboard_module(path: Path) -> None:
    reached = sorted(m for m in _imported_modules(path) if m.startswith(("serena.config", "serena.dashboard")))
    assert reached == [], f"{path.name} imports {reached}"


@pytest.mark.parametrize("path", sorted(_STRUCTURAL.rglob("*.py")), ids=lambda p: p.relative_to(_STRUCTURAL).as_posix())
def test_a_structural_module_imports_only_structural_and_supervision_modules(path: Path) -> None:
    reached = sorted(
        m
        for m in _imported_modules(path)
        if m.split(".")[0] in ("serena", "solidlsp")
        and m != "solidlsp.structural"
        and not m.startswith("solidlsp.structural.")
        and m not in _STRUCTURAL_SUPERVISION_MODULES
    )
    assert reached == [], f"{path.relative_to(_SRC)} imports {reached}"


_RENAME_SANDBOX = "def sandbox_fn():\n    return 1\n\n\ndef caller():\n    return sandbox_fn()\n"


def test_cursor_rename_lands_on_a_python_file(python_serena_agent: SerenaAgent) -> None:
    """CursorRenameTool renames a function through the code editor of the agent's backend, its call site included."""
    from serena.tools.cursor_tools import CursorRenameTool, CursorStartTool

    relative_path = os.path.join("test_repo", "_cursor_rename_sandbox.py")
    path = Path(python_serena_agent.get_active_project_or_raise().project_root) / relative_path
    path.write_text(_RENAME_SANDBOX, encoding="utf-8")
    python_serena_agent.reset_language_server_manager()
    try:
        python_serena_agent.get_tool(CursorStartTool).apply(
            because="test fixture: a cursor on the function to rename",
            name_path="sandbox_fn",
            relative_path=relative_path,
            cursor_id="rename-lands",
        )
        python_serena_agent.get_tool(CursorRenameTool).apply(cursor_id="rename-lands", new_name="renamed_fn")

        renamed = path.read_text(encoding="utf-8")
        assert "def renamed_fn():" in renamed and "return renamed_fn()" in renamed, renamed
        assert "sandbox_fn" not in renamed, renamed
    finally:
        path.unlink(missing_ok=True)
        python_serena_agent.reset_language_server_manager()
