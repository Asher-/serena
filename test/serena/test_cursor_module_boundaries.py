"""The cursor surface reaches upstream's modules only through the names it imports (task://upstream_merge.15)."""

import ast
import importlib
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
_CURSOR_MODULES = [_SRC / "serena" / "cursor.py", _SRC / "serena" / "tools" / "cursor_tools.py"]


def _imported_modules(path: Path) -> set[str]:
    """:return: every module the file imports, at any depth of its code, relative imports resolved within its package"""
    package = ".".join(path.relative_to(_SRC).with_suffix("").parts[:-1])
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                modules.add(".".join([*base, node.module] if node.module else base))
            else:
                modules.add(node.module or "")
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
